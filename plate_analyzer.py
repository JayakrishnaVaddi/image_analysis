"""
Plate detection, warping, well sampling, and color classification.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

from config import DETECTION, HSV_THRESHOLDS, MANUAL_CROP, PLATE_GEOMETRY


LOGGER = logging.getLogger(__name__)


class SlabDetectionError(RuntimeError):
    """
    Raised when the white slab cannot be detected or cropped.
    """

    def __init__(self, message: str, debug_image: Optional[np.ndarray] = None) -> None:
        super().__init__(message)
        self.debug_image = debug_image


@dataclass
class AnalysisArtifacts:
    """
    Image artifacts produced during plate detection and analysis.
    """

    original: np.ndarray
    slab_detection: np.ndarray
    warped_slab: np.ndarray
    grid_overlay: np.ndarray
    annotated_result: np.ndarray
    clean_result: np.ndarray


@dataclass
class AnalysisResult:
    """
    Structured result returned to the main application.
    """

    gene_presence: List[int]
    well_colors: List[Optional[str]]
    artifacts: AnalysisArtifacts
    slab_corners: List[List[int]]
    used_manual_crop: bool


class PlateAnalyzer:
    """
    Analyze a 96-well slab image and classify the color of each well.
    """

    def analyze(self, image: np.ndarray) -> AnalysisResult:
        """
        Run the full detection and well analysis workflow.
        """

        detection_view = image.copy()
        slab_corners, used_manual_crop = self._locate_slab(image, detection_view)
        warped = self._warp_slab(image, slab_corners)
        gene_presence, well_colors, grid_overlay, annotated, clean_result = self._analyze_wells(warped)

        return AnalysisResult(
            gene_presence=gene_presence,
            well_colors=well_colors,
            artifacts=AnalysisArtifacts(
                original=image.copy(),
                slab_detection=detection_view,
                warped_slab=warped,
                grid_overlay=grid_overlay,
                annotated_result=annotated,
                clean_result=clean_result,
            ),
            slab_corners=[[int(x), int(y)] for x, y in slab_corners],
            used_manual_crop=used_manual_crop,
        )

    def _locate_slab(
        self,
        image: np.ndarray,
        debug_image: np.ndarray,
    ) -> Tuple[np.ndarray, bool]:
        """
        Locate the white slab using a centered white-component detector.
        """

        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        blurred = cv2.GaussianBlur(gray, DETECTION.gaussian_blur_kernel, 0)

        white_mask = cv2.inRange(
            hsv,
            (0, 0, DETECTION.white_value_min),
            (179, DETECTION.white_low_saturation_max, 255),
        )
        _, gray_mask = cv2.threshold(
            blurred,
            DETECTION.grayscale_value_min,
            255,
            cv2.THRESH_BINARY,
        )
        combined_mask = cv2.bitwise_and(white_mask, gray_mask)

        close_kernel = np.ones(DETECTION.close_kernel, dtype=np.uint8)
        open_kernel = np.ones(DETECTION.open_kernel, dtype=np.uint8)
        combined_mask = cv2.morphologyEx(
            combined_mask,
            cv2.MORPH_CLOSE,
            close_kernel,
        )
        combined_mask = cv2.morphologyEx(
            combined_mask,
            cv2.MORPH_OPEN,
            open_kernel,
        )

        cv2.putText(
            debug_image,
            "Green: detected slab contour",
            (20, 35),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (0, 255, 0),
            2,
            cv2.LINE_AA,
        )
        cv2.putText(
            debug_image,
            "Yellow: centered white component",
            (20, 70),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (0, 255, 255),
            2,
            cv2.LINE_AA,
        )
        selected_component = self._extract_main_slab_component(
            combined_mask=combined_mask,
            image_shape=image.shape,
        )
        if selected_component is not None:
            cv2.polylines(
                debug_image,
                [selected_component.astype(np.int32)],
                isClosed=True,
                color=(0, 255, 255),
                thickness=2,
            )

            ordered = self._fit_portrait_quad(selected_component)
            cv2.polylines(
                debug_image,
                [ordered.astype(np.int32)],
                isClosed=True,
                color=(0, 255, 0),
                thickness=3,
            )
            LOGGER.info("Detected slab from centered white component")
            return ordered, False

        if MANUAL_CROP.enabled:
            top_left = np.array(MANUAL_CROP.top_left, dtype=np.float32)
            bottom_right = np.array(MANUAL_CROP.bottom_right, dtype=np.float32)
            manual_points = np.array(
                [
                    top_left,
                    [bottom_right[0], top_left[1]],
                    bottom_right,
                    [top_left[0], bottom_right[1]],
                ],
                dtype=np.float32,
            )
            cv2.rectangle(
                debug_image,
                tuple(MANUAL_CROP.top_left),
                tuple(MANUAL_CROP.bottom_right),
                (0, 165, 255),
                3,
            )
            LOGGER.warning("Contour detection failed; using manual crop fallback")
            return manual_points, True

        raise SlabDetectionError(
            "Unable to detect the white slab contour and manual crop is disabled",
            debug_image=debug_image,
        )

    def _extract_main_slab_component(
        self,
        combined_mask: np.ndarray,
        image_shape: Tuple[int, ...],
    ) -> Optional[np.ndarray]:
        """
        Isolate the main white slab region and return its outer contour.
        """

        component_count, labels, stats, centroids = cv2.connectedComponentsWithStats(
            combined_mask,
            connectivity=8,
        )
        if component_count <= 1:
            return None

        image_height, image_width = image_shape[:2]
        image_area = float(image_height * image_width)
        image_center = np.array([image_width / 2.0, image_height / 2.0], dtype=np.float32)

        best_label: Optional[int] = None
        best_score = -1.0

        for label in range(1, component_count):
            area = float(stats[label, cv2.CC_STAT_AREA])
            area_ratio = area / image_area
            if area_ratio < DETECTION.component_min_area_ratio:
                continue
            if area_ratio > DETECTION.max_contour_area_ratio:
                continue

            component_center = centroids[label]
            center_distance = np.linalg.norm(component_center - image_center)
            normalized_center_distance = center_distance / max(np.linalg.norm(image_center), 1.0)

            component_mask = np.zeros_like(combined_mask)
            component_mask[labels == label] = 255
            contour = self._largest_contour(component_mask)
            if contour is None:
                continue

            portrait_quad = self._fit_portrait_quad(contour)
            aspect_ratio = self._quad_aspect_ratio(portrait_quad)
            aspect_penalty = abs(aspect_ratio - DETECTION.target_aspect_ratio)
            if aspect_penalty > DETECTION.max_aspect_ratio_deviation:
                continue

            score = (
                area_ratio * DETECTION.component_area_weight
                - normalized_center_distance * DETECTION.component_center_weight
                - aspect_penalty * 0.2
            )

            if score > best_score:
                best_label = label
                best_score = score

        if best_label is None:
            return None

        selected_mask = np.zeros_like(combined_mask)
        selected_mask[labels == best_label] = 255
        return self._largest_contour(selected_mask)

    def _largest_contour(self, mask: np.ndarray) -> Optional[np.ndarray]:
        """
        Return the largest external contour from a binary mask.
        """

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None
        return max(contours, key=cv2.contourArea)

    def _fit_portrait_quad(self, contour: np.ndarray) -> np.ndarray:
        """
        Fit one portrait-oriented quadrilateral around the slab component.
        """

        rect = cv2.minAreaRect(contour)
        quad = cv2.boxPoints(rect).astype(np.float32)
        ordered = self._order_points(quad)

        width = np.linalg.norm(ordered[1] - ordered[0])
        height = np.linalg.norm(ordered[3] - ordered[0])
        if width > height:
            ordered = np.array([ordered[1], ordered[2], ordered[3], ordered[0]], dtype=np.float32)
        return ordered

    def _quad_aspect_ratio(self, quad: np.ndarray) -> float:
        """
        Compute height/width ratio for an ordered slab quad.
        """

        width = max(
            (
                np.linalg.norm(quad[1] - quad[0]) +
                np.linalg.norm(quad[2] - quad[3])
            ) / 2.0,
            1.0,
        )
        height = max(
            (
                np.linalg.norm(quad[3] - quad[0]) +
                np.linalg.norm(quad[2] - quad[1])
            ) / 2.0,
            1.0,
        )
        return height / width


    def _warp_slab(self, image: np.ndarray, corners: np.ndarray) -> np.ndarray:
        """
        Perspective-warp the slab into a normalized top-down portrait view.

        This does not rotate the source frame itself. It only maps the detected
        slab corners onto the configured portrait slab plane.
        """

        destination = np.array(
            [
                [0, 0],
                [PLATE_GEOMETRY.warp_width - 1, 0],
                [PLATE_GEOMETRY.warp_width - 1, PLATE_GEOMETRY.warp_height - 1],
                [0, PLATE_GEOMETRY.warp_height - 1],
            ],
            dtype=np.float32,
        )
        transform = cv2.getPerspectiveTransform(corners, destination)
        warped = cv2.warpPerspective(
            image,
            transform,
            (PLATE_GEOMETRY.warp_width, PLATE_GEOMETRY.warp_height),
        )
        return warped

    def _analyze_wells(
        self,
        warped: np.ndarray,
    ) -> Tuple[List[int], List[Optional[str]], np.ndarray, np.ndarray, np.ndarray]:
        """
        Divide the slab into a grid and produce the 96-value gene presence array.
        """

        grid_overlay = warped.copy()
        annotated = warped.copy()
        clean_result = np.full(
            (warped.shape[0], warped.shape[1], 3),
            245,
            dtype=np.uint8,
        )
        warped_hsv = cv2.cvtColor(warped, cv2.COLOR_BGR2HSV)

        active_left = int(round(warped.shape[1] * PLATE_GEOMETRY.active_left_ratio))
        active_top = int(round(warped.shape[0] * PLATE_GEOMETRY.active_top_ratio))
        active_right = int(round(warped.shape[1] * PLATE_GEOMETRY.active_right_ratio))
        active_bottom = int(round(warped.shape[0] * PLATE_GEOMETRY.active_bottom_ratio))

        active_right = max(active_left + 1, min(active_right, warped.shape[1]))
        active_bottom = max(active_top + 1, min(active_bottom, warped.shape[0]))

        cell_width = (active_right - active_left) / PLATE_GEOMETRY.cols
        cell_height = (active_bottom - active_top) / PLATE_GEOMETRY.rows
        base_cell_size = min(cell_width, cell_height)
        sample_radius = int(base_cell_size * PLATE_GEOMETRY.sample_radius_ratio)
        visualization_radius = int(base_cell_size * PLATE_GEOMETRY.visualization_radius_ratio)

        cv2.rectangle(
            grid_overlay,
            (active_left, active_top),
            (active_right, active_bottom),
            (0, 255, 0),
            2,
        )

        well_values: List[int] = [0] * (PLATE_GEOMETRY.rows * PLATE_GEOMETRY.cols)
        well_colors: List[Optional[str]] = [None] * (PLATE_GEOMETRY.rows * PLATE_GEOMETRY.cols)

        for row_index in range(PLATE_GEOMETRY.rows):
            for col_index in range(PLATE_GEOMETRY.cols):
                x1 = int(round(active_left + (col_index * cell_width)))
                y1 = int(round(active_top + (row_index * cell_height)))
                x2 = int(round(active_left + ((col_index + 1) * cell_width)))
                y2 = int(round(active_top + ((row_index + 1) * cell_height)))

                center_x = int(round((x1 + x2) / 2.0))
                center_y = int(round((y1 + y2) / 2.0))

                mask = np.zeros(warped.shape[:2], dtype=np.uint8)
                cv2.circle(mask, (center_x, center_y), sample_radius, 255, -1)

                mean_bgr = cv2.mean(warped, mask=mask)[:3]
                mean_hsv = cv2.mean(warped_hsv, mask=mask)[:3]

                avg_bgr = [round(float(value), 2) for value in mean_bgr]
                avg_hsv = [round(float(value), 2) for value in mean_hsv]
                classified_color = self._classify_hsv(avg_hsv)
                gene_value = self._gene_value_from_color(classified_color)
                well_number = self._well_number(row_index=row_index, col_index=col_index)
                well_values[well_number - 1] = gene_value
                well_colors[well_number - 1] = classified_color
                render_color = self._render_bgr_from_color(classified_color)

                cv2.rectangle(grid_overlay, (x1, y1), (x2, y2), (255, 255, 255), 1)
                cv2.circle(grid_overlay, (center_x, center_y), visualization_radius, render_color, 2)
                cv2.putText(
                    annotated,
                    f"{well_number}:{gene_value}",
                    (x1 + 4, min(y2 - 8, y1 + 20)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.38,
                    (0, 0, 0),
                    2,
                    cv2.LINE_AA,
                )
                cv2.putText(
                    annotated,
                    f"{well_number}:{gene_value}",
                    (x1 + 4, min(y2 - 8, y1 + 20)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.38,
                    (255, 255, 255),
                    1,
                    cv2.LINE_AA,
                )
                cv2.circle(annotated, (center_x, center_y), visualization_radius, render_color, 2)
                cv2.circle(clean_result, (center_x, center_y), visualization_radius, render_color, -1)
                cv2.circle(clean_result, (center_x, center_y), visualization_radius, (200, 200, 200), 2)

        return well_values, well_colors, grid_overlay, annotated, clean_result

    def _classify_hsv(self, hsv: List[float]) -> Optional[str]:
        """
        Classify a well color using the configured HSV threshold ranges.
        """

        h, s, v = hsv
        hsv_int = (int(round(h)), int(round(s)), int(round(v)))

        for color_name, ranges in HSV_THRESHOLDS.items():
            for threshold in ranges:
                lower = threshold["lower"]
                upper = threshold["upper"]
                if self._in_range(hsv_int, lower, upper):
                    return color_name
        return None

    @staticmethod
    def _gene_value_from_color(color_name: Optional[str]) -> int:
        """
        Convert the recognized color into gene presence output.
        """

        return 1 if color_name == "yellow" else 0

    @staticmethod
    def _render_bgr_from_color(color_name: Optional[str]) -> Tuple[int, int, int]:
        """
        Convert an internal color label to a clean visualization color.
        """

        mapping: Dict[Optional[str], Tuple[int, int, int]] = {
            "light_pink": (203, 192, 255),
            "red": (0, 0, 255),
            "yellow": (0, 255, 255),
            None: (220, 220, 220),
        }
        return mapping.get(color_name, (220, 220, 220))

    @staticmethod
    def _well_number(row_index: int, col_index: int) -> int:
        """
        Number wells top-to-bottom and right-to-left within each row.

        Physical columns are processed left-to-right in the image, so the
        numbered output reverses the column index.
        """

        row_offset = row_index * PLATE_GEOMETRY.cols
        return row_offset + (PLATE_GEOMETRY.cols - col_index)

    @staticmethod
    def _in_range(
        value: Tuple[int, int, int],
        lower: Tuple[int, int, int],
        upper: Tuple[int, int, int],
    ) -> bool:
        """
        Check whether an HSV value falls inside an inclusive threshold range.
        """

        return all(lower[index] <= value[index] <= upper[index] for index in range(3))

    @staticmethod
    def _order_points(points: np.ndarray) -> np.ndarray:
        """
        Order quadrilateral points as top-left, top-right, bottom-right, bottom-left.
        """

        sums = points.sum(axis=1)
        diffs = np.diff(points, axis=1)

        top_left = points[np.argmin(sums)]
        bottom_right = points[np.argmax(sums)]
        top_right = points[np.argmin(diffs)]
        bottom_left = points[np.argmax(diffs)]

        return np.array([top_left, top_right, bottom_right, bottom_left], dtype=np.float32)
