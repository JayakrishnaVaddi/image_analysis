"""
Plate-localized well detection, ordering, and color classification.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

from color_profiles import COLOR_PROFILES, COLOR_PROFILE_BY_NAME, DEFAULT_RENDER_COLOR
from config import DETECTION, MANUAL_CROP, PLATE_GEOMETRY, WELL_DETECTION


LOGGER = logging.getLogger(__name__)


class SlabDetectionError(RuntimeError):
    """
    Raised when the white slab cannot be detected or cropped.
    """

    def __init__(self, message: str, debug_image: Optional[np.ndarray] = None) -> None:
        super().__init__(message)
        self.debug_image = debug_image


class WellDetectionError(RuntimeError):
    """
    Raised when enough wells cannot be detected or assigned confidently.
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
    ordered_result: np.ndarray
    candidate_wells: np.ndarray
    labeled_wells: np.ndarray
    sample_regions: np.ndarray


@dataclass
class WellDetail:
    """
    One detected or inferred well with its assigned output identity.
    """

    well_number: int
    row_index: int
    col_index: int
    label: str
    center: Tuple[int, int]
    sample_radius: int
    source_radius: float
    score: float
    detected: bool
    color: Optional[str] = None
    gene_value: int = 0


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
    well_details: List[WellDetail]


@dataclass
class WellCandidate:
    """
    One candidate well detected in analysis-input coordinates.
    """

    center: Tuple[float, float]
    radius: float
    score: float
    source: str
    circularity: float


class PlateAnalyzer:
    """
    Analyze a 96-well slab image by detecting wells individually.
    """

    def analyze(self, image: np.ndarray) -> AnalysisResult:
        """
        Run the full detection and per-well analysis workflow.
        """

        detection_view = image.copy()
        slab_corners, used_manual_crop = self._locate_slab(image, detection_view)
        helper_warp = self._warp_slab(image, slab_corners)

        candidates, candidate_overlay, accepted_overlay = self._detect_well_candidates(
            image=image,
            helper_warp=helper_warp,
            slab_corners=slab_corners,
        )
        assigned_wells, labeled_overlay = self._assign_well_ids(
            candidates=candidates,
            slab_corners=slab_corners,
            image=image,
            accepted_overlay=accepted_overlay,
        )
        gene_presence, well_colors, annotated, clean_result, ordered_result, sample_regions = self._classify_assigned_wells(
            image=image,
            assigned_wells=assigned_wells,
        )

        return AnalysisResult(
            gene_presence=gene_presence,
            well_colors=well_colors,
            artifacts=AnalysisArtifacts(
                original=image.copy(),
                slab_detection=detection_view,
                warped_slab=helper_warp,
                grid_overlay=accepted_overlay,
                annotated_result=annotated,
                clean_result=clean_result,
                ordered_result=ordered_result,
                candidate_wells=candidate_overlay,
                labeled_wells=labeled_overlay,
                sample_regions=sample_regions,
            ),
            slab_corners=[[int(x), int(y)] for x, y in slab_corners],
            used_manual_crop=used_manual_crop,
            well_details=assigned_wells,
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
        combined_mask = cv2.morphologyEx(combined_mask, cv2.MORPH_CLOSE, close_kernel)
        combined_mask = cv2.morphologyEx(combined_mask, cv2.MORPH_OPEN, open_kernel)
        helper_mask = self._build_helper_slab_mask(combined_mask)

        cv2.putText(
            debug_image,
            "Green: slab helper contour",
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
        cv2.putText(
            debug_image,
            "Cyan: fused slab silhouette",
            (20, 105),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (255, 255, 0),
            2,
            cv2.LINE_AA,
        )

        selected_component = self._extract_main_slab_component(
            combined_mask=helper_mask,
            image_shape=image.shape,
        )
        if selected_component is not None:
            cv2.polylines(
                debug_image,
                [selected_component.astype(np.int32)],
                isClosed=True,
                color=(255, 255, 0),
                thickness=2,
            )

            ordered = self._fit_portrait_quad(selected_component)
            ordered = self._expand_and_clip_quad(ordered, image.shape)
            cv2.polylines(
                debug_image,
                [ordered.astype(np.int32)],
                isClosed=True,
                color=(0, 255, 0),
                thickness=3,
            )
            if self._quad_area_ratio(ordered, image.shape) >= DETECTION.helper_quad_min_area_ratio:
                LOGGER.info("Detected slab helper ROI from centered white component")
                return ordered, False

            LOGGER.warning(
                "Detected slab helper quad was too small after expansion; using full analysis image as helper ROI"
            )

        full_frame_quad = self._full_frame_quad(image.shape)
        cv2.polylines(
            debug_image,
            [full_frame_quad.astype(np.int32)],
            isClosed=True,
            color=(255, 200, 0),
            thickness=2,
        )
        cv2.putText(
            debug_image,
            "Blue: full-frame helper ROI fallback",
            (20, 140),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (255, 200, 0),
            2,
            cv2.LINE_AA,
        )

        if not MANUAL_CROP.enabled:
            LOGGER.warning("Using full analysis image as slab helper ROI fallback")
            return full_frame_quad, False

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
            LOGGER.warning("Slab helper ROI fallback enabled via manual crop")
            return manual_points, True

        raise SlabDetectionError(
            "Unable to detect the slab helper ROI and manual crop is disabled",
            debug_image=debug_image,
        )

    def _detect_well_candidates(
        self,
        image: np.ndarray,
        helper_warp: np.ndarray,
        slab_corners: np.ndarray,
    ) -> Tuple[List[WellCandidate], np.ndarray, np.ndarray]:
        """
        Detect raw well candidates on the helper-warped slab and map them back to image space.
        """

        warp_height, warp_width = helper_warp.shape[:2]
        gray = cv2.cvtColor(helper_warp, cv2.COLOR_BGR2GRAY)
        hsv = cv2.cvtColor(helper_warp, cv2.COLOR_BGR2HSV)
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        expected_radius = max(4.0, float(WELL_DETECTION.fixed_candidate_radius_px))

        helper_roi_mask = np.zeros((warp_height, warp_width), dtype=np.uint8)
        left = max(0, int(round(warp_width * max(0.0, PLATE_GEOMETRY.active_left_ratio - 0.03))))
        top = max(0, int(round(warp_height * max(0.0, PLATE_GEOMETRY.active_top_ratio - 0.02))))
        right = min(warp_width, int(round(warp_width * min(1.0, PLATE_GEOMETRY.active_right_ratio + 0.04))))
        bottom = min(warp_height, int(round(warp_height * min(1.0, PLATE_GEOMETRY.active_bottom_ratio + 0.01))))
        cv2.rectangle(helper_roi_mask, (left, top), (right, bottom), 255, -1)

        adaptive_mask = cv2.adaptiveThreshold(
            blurred,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY_INV,
            WELL_DETECTION.adaptive_block_size,
            WELL_DETECTION.adaptive_c,
        )
        saturation_mask = cv2.inRange(
            hsv,
            (0, WELL_DETECTION.saturation_threshold, WELL_DETECTION.value_threshold),
            (179, 255, 255),
        )
        combined_mask = cv2.bitwise_or(adaptive_mask, saturation_mask)
        combined_mask = cv2.bitwise_and(combined_mask, helper_roi_mask)
        combined_mask = cv2.morphologyEx(
            combined_mask,
            cv2.MORPH_OPEN,
            np.ones((3, 3), dtype=np.uint8),
        )
        combined_mask = cv2.morphologyEx(
            combined_mask,
            cv2.MORPH_CLOSE,
            np.ones((5, 5), dtype=np.uint8),
        )

        raw_overlay = image.copy()
        accepted_overlay = image.copy()
        cv2.polylines(raw_overlay, [slab_corners.astype(np.int32)], True, (0, 255, 255), 2)
        cv2.polylines(accepted_overlay, [slab_corners.astype(np.int32)], True, (0, 255, 255), 2)
        inverse_transform = cv2.getPerspectiveTransform(
            self._helper_destination_corners(),
            slab_corners.astype(np.float32),
        )

        contour_candidates: List[WellCandidate] = []
        contours, _ = cv2.findContours(combined_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            accepted, candidate, reason = self._candidate_from_contour(
                contour=contour,
                gray=gray,
                roi_mask=helper_roi_mask,
                expected_radius=expected_radius,
            )
            if candidate is None:
                if contour.size > 0:
                    contour_points = contour.astype(np.float32).reshape(-1, 1, 2)
                    mapped = cv2.perspectiveTransform(contour_points, inverse_transform).astype(np.int32)
                    cv2.polylines(raw_overlay, [mapped.reshape(-1, 2)], True, (0, 0, 255), 1)
                continue
            mapped_candidate = self._map_helper_candidate_to_image(
                candidate,
                inverse_transform,
                radius_override=expected_radius,
            )
            center = tuple(int(round(value)) for value in mapped_candidate.center)
            radius = max(2, int(round(mapped_candidate.radius)))
            if accepted:
                contour_candidates.append(mapped_candidate)
                cv2.circle(raw_overlay, center, radius, (0, 255, 0), 2)
            else:
                cv2.circle(raw_overlay, center, radius, (0, 0, 255), 1)
                if reason:
                    cv2.putText(
                        raw_overlay,
                        reason,
                        (center[0] + 3, center[1] - 3),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.35,
                        (0, 0, 255),
                        1,
                        cv2.LINE_AA,
                    )

        hough_candidates = self._hough_circle_candidates(
            gray=gray,
            roi_mask=helper_roi_mask,
            expected_radius=expected_radius,
        )
        mapped_hough_candidates = [
            self._map_helper_candidate_to_image(
                candidate,
                inverse_transform,
                radius_override=expected_radius,
            )
            for candidate in hough_candidates
        ]
        for candidate in mapped_hough_candidates:
            center = tuple(int(round(value)) for value in candidate.center)
            radius = max(2, int(round(candidate.radius)))
            cv2.circle(raw_overlay, center, radius, (255, 140, 0), 1)

        merged_candidates = self._merge_candidate_lists(
            contour_candidates + mapped_hough_candidates,
            expected_radius=expected_radius,
        )
        if len(merged_candidates) < WELL_DETECTION.min_detected_wells:
            LOGGER.info(
                "Global well candidate detector found only %s wells; switching to anchored helper-warp search",
                len(merged_candidates),
            )
            merged_candidates = self._anchored_candidates_from_helper(
                helper_warp=helper_warp,
                combined_mask=combined_mask,
                slab_corners=slab_corners,
                expected_radius=expected_radius,
            )
        for candidate in merged_candidates:
            center = tuple(int(round(value)) for value in candidate.center)
            radius = max(2, int(round(candidate.radius)))
            cv2.circle(accepted_overlay, center, radius, (0, 255, 0), 2)

        cv2.putText(
            raw_overlay,
            f"raw candidates={len(contour_candidates) + len(hough_candidates)}",
            (20, image.shape[0] - 40),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )
        cv2.putText(
            accepted_overlay,
            f"merged candidates={len(merged_candidates)}",
            (20, image.shape[0] - 40),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )
        return merged_candidates, raw_overlay, accepted_overlay

    def _anchored_candidates_from_helper(
        self,
        helper_warp: np.ndarray,
        combined_mask: np.ndarray,
        slab_corners: np.ndarray,
        expected_radius: float,
    ) -> List[WellCandidate]:
        """
        Search each expected helper-warp neighborhood locally for one well candidate.
        """

        inverse_transform = cv2.getPerspectiveTransform(
            self._helper_destination_corners(),
            slab_corners.astype(np.float32),
        )
        candidates: List[WellCandidate] = []
        window_radius = max(7, int(round(expected_radius * 1.15)))

        for center_x, center_y in self._expected_helper_centers():
            x1 = max(0, int(round(center_x - window_radius)))
            y1 = max(0, int(round(center_y - window_radius)))
            x2 = min(helper_warp.shape[1], int(round(center_x + window_radius)))
            y2 = min(helper_warp.shape[0], int(round(center_y + window_radius)))
            local_mask = combined_mask[y1:y2, x1:x2]
            contours, _ = cv2.findContours(local_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            best_local: Optional[WellCandidate] = None
            best_distance = float("inf")
            for contour in contours:
                contour = contour + np.array([[[x1, y1]]], dtype=np.int32)
                accepted, candidate, _ = self._candidate_from_contour(
                    contour=contour,
                    gray=cv2.cvtColor(helper_warp, cv2.COLOR_BGR2GRAY),
                    roi_mask=np.ones(helper_warp.shape[:2], dtype=np.uint8) * 255,
                    expected_radius=expected_radius,
                )
                if candidate is None or not accepted:
                    continue
                distance = math.dist(candidate.center, (center_x, center_y))
                if distance < best_distance:
                    best_distance = distance
                    best_local = candidate

            if best_local is None:
                helper_candidate = WellCandidate(
                    center=(center_x, center_y),
                    radius=expected_radius,
                    score=0.05,
                    source="anchor_inferred",
                    circularity=0.0,
                )
            else:
                helper_candidate = WellCandidate(
                    center=best_local.center,
                    radius=best_local.radius,
                    score=max(best_local.score, 0.72),
                    source="anchor_window",
                    circularity=best_local.circularity,
                )

            candidates.append(
                self._map_helper_candidate_to_image(
                    helper_candidate,
                    inverse_transform,
                    radius_override=expected_radius,
                )
            )

        return candidates

    def _candidate_from_contour(
        self,
        contour: np.ndarray,
        gray: np.ndarray,
        roi_mask: np.ndarray,
        expected_radius: float,
    ) -> Tuple[bool, Optional[WellCandidate], Optional[str]]:
        """
        Score one contour as a possible well candidate.
        """

        area = cv2.contourArea(contour)
        if area <= 1.0:
            return False, None, None

        perimeter = cv2.arcLength(contour, True)
        if perimeter <= 0:
            return False, None, None

        circularity = float((4.0 * math.pi * area) / (perimeter * perimeter))
        (center_x, center_y), radius = cv2.minEnclosingCircle(contour)
        expected_area = math.pi * expected_radius * expected_radius
        if radius <= 0:
            return False, None, "radius"

        center = (int(round(center_x)), int(round(center_y)))
        if center[0] < 0 or center[1] < 0 or center[0] >= roi_mask.shape[1] or center[1] >= roi_mask.shape[0]:
            return False, None, "bounds"
        if roi_mask[center[1], center[0]] == 0:
            return False, None, "roi"

        axis_ratio = 1.0
        if len(contour) >= 5:
            (_, _), (major_axis, minor_axis), _ = cv2.fitEllipse(contour)
            if minor_axis > 0:
                axis_ratio = float(max(major_axis, minor_axis) / max(min(major_axis, minor_axis), 1.0))

        area_scale = area / max(expected_area, 1.0)
        accepted = True
        reason = None

        if area_scale < WELL_DETECTION.contour_area_min_scale:
            accepted = False
            reason = "small"
        elif area_scale > WELL_DETECTION.contour_area_max_scale:
            accepted = False
            reason = "large"
        elif circularity < WELL_DETECTION.min_circularity:
            accepted = False
            reason = "shape"
        elif axis_ratio > WELL_DETECTION.max_ellipse_axis_ratio:
            accepted = False
            reason = "ellipse"

        area_score = max(0.0, 1.0 - abs(1.0 - area_scale))
        score = (0.55 * circularity) + (0.35 * area_score) + (0.10 * (1.0 / max(axis_ratio, 1.0)))

        candidate = WellCandidate(
            center=(float(center_x), float(center_y)),
            radius=float(radius),
            score=float(score),
            source="contour",
            circularity=float(circularity),
        )
        return accepted, candidate, reason

    def _hough_circle_candidates(
        self,
        gray: np.ndarray,
        roi_mask: np.ndarray,
        expected_radius: float,
    ) -> List[WellCandidate]:
        """
        Use Hough circles as a rescue detector for faint wells.
        """

        masked_gray = gray.copy()
        masked_gray[roi_mask == 0] = 255
        blurred = cv2.GaussianBlur(masked_gray, (9, 9), 1.4)
        min_dist = max(6.0, expected_radius * WELL_DETECTION.hough_min_dist_scale)
        circles = cv2.HoughCircles(
            blurred,
            cv2.HOUGH_GRADIENT,
            dp=WELL_DETECTION.hough_dp,
            minDist=min_dist,
            param1=WELL_DETECTION.hough_param1,
            param2=WELL_DETECTION.hough_param2,
            minRadius=max(2, int(round(expected_radius * WELL_DETECTION.hough_min_radius_scale))),
            maxRadius=max(3, int(round(expected_radius * WELL_DETECTION.hough_max_radius_scale))),
        )
        if circles is None:
            return []

        candidates: List[WellCandidate] = []
        for x, y, radius in np.round(circles[0, :]).astype(np.int32):
            if x < 0 or y < 0 or x >= roi_mask.shape[1] or y >= roi_mask.shape[0]:
                continue
            if roi_mask[y, x] == 0:
                continue
            candidates.append(
                WellCandidate(
                    center=(float(x), float(y)),
                    radius=float(radius),
                    score=0.52,
                    source="hough",
                    circularity=1.0,
                )
            )
        return candidates

    def _merge_candidate_lists(
        self,
        candidates: List[WellCandidate],
        expected_radius: float,
    ) -> List[WellCandidate]:
        """
        Merge overlapping candidates from multiple detectors.
        """

        if not candidates:
            return []

        candidates = sorted(candidates, key=lambda candidate: candidate.score, reverse=True)
        merged: List[WellCandidate] = []
        for candidate in candidates:
            duplicate = False
            for existing in merged:
                center_distance = math.dist(candidate.center, existing.center)
                radius_delta = abs(candidate.radius - existing.radius)
                if (
                    center_distance <= expected_radius * WELL_DETECTION.duplicate_center_distance_scale
                    and radius_delta <= expected_radius * WELL_DETECTION.duplicate_radius_delta_scale
                ):
                    duplicate = True
                    break
            if not duplicate:
                merged.append(candidate)
        return merged

    def _assign_well_ids(
        self,
        candidates: List[WellCandidate],
        slab_corners: np.ndarray,
        image: np.ndarray,
        accepted_overlay: np.ndarray,
    ) -> Tuple[List[WellDetail], np.ndarray]:
        """
        Assign each candidate to one well in the 12x8 output lattice.
        """

        if len(candidates) < WELL_DETECTION.min_detected_wells:
            failure = accepted_overlay.copy()
            cv2.putText(
                failure,
                f"Too few well candidates: {len(candidates)}",
                (20, 40),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (0, 0, 255),
                2,
                cv2.LINE_AA,
            )
            raise WellDetectionError(
                f"Detected only {len(candidates)} well candidates; need at least {WELL_DETECTION.min_detected_wells}",
                debug_image=failure,
            )

        normalized_candidates = self._normalize_candidates(candidates, slab_corners)
        normalized_points = np.array(
            [[candidate["normalized_center"][0], candidate["normalized_center"][1]] for candidate in normalized_candidates],
            dtype=np.float32,
        )
        row_centers = self._cluster_axis(normalized_points[:, 1], WELL_DETECTION.row_cluster_count)
        col_centers = self._cluster_axis(normalized_points[:, 0], WELL_DETECTION.col_cluster_count)

        if row_centers is None or col_centers is None:
            raise WellDetectionError(
                "Unable to cluster well candidates into a 12x8 lattice",
                debug_image=accepted_overlay,
            )

        row_spacing = self._median_spacing(row_centers)
        col_spacing = self._median_spacing(col_centers)
        max_row_error = row_spacing * WELL_DETECTION.max_assignment_error_scale
        max_col_error = col_spacing * WELL_DETECTION.max_assignment_error_scale

        selected_by_cell: Dict[Tuple[int, int], Dict[str, object]] = {}
        for candidate in normalized_candidates:
            normalized_x, normalized_y = candidate["normalized_center"]
            row_index = self._nearest_center_index(normalized_y, row_centers)
            col_index = self._nearest_center_index(normalized_x, col_centers)
            row_error = abs(normalized_y - row_centers[row_index])
            col_error = abs(normalized_x - col_centers[col_index])
            if row_error > max_row_error or col_error > max_col_error:
                continue

            distance_penalty = (row_error / max(row_spacing, 1.0)) + (col_error / max(col_spacing, 1.0))
            effective_score = float(candidate["candidate"].score) - (0.18 * distance_penalty)
            cell_key = (row_index, col_index)
            existing = selected_by_cell.get(cell_key)
            if existing is None or effective_score > float(existing["effective_score"]):
                selected_by_cell[cell_key] = {
                    "candidate": candidate["candidate"],
                    "effective_score": effective_score,
                }

        detected_count = sum(
            1
            for entry in selected_by_cell.values()
            if str(entry["candidate"].source) != "anchor_inferred"
        )
        inferred_count = (PLATE_GEOMETRY.rows * PLATE_GEOMETRY.cols) - detected_count
        if detected_count < WELL_DETECTION.min_detected_wells or inferred_count > WELL_DETECTION.max_inferred_wells:
            failure = accepted_overlay.copy()
            cv2.putText(
                failure,
                f"detected={detected_count} inferred={inferred_count}",
                (20, 40),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (0, 0, 255),
                2,
                cv2.LINE_AA,
            )
            raise WellDetectionError(
                (
                    "Unable to assign a complete well lattice: "
                    f"detected={detected_count}, inferred={inferred_count}"
                ),
                debug_image=failure,
            )

        median_radius = float(np.median([entry["candidate"].radius for entry in selected_by_cell.values()]))
        inverse_transform = cv2.getPerspectiveTransform(
            self._helper_destination_corners(),
            slab_corners.astype(np.float32),
        )

        uniform_sample_radius = max(
            3,
            int(round(median_radius * WELL_DETECTION.sample_radius_scale)),
        )
        uniform_visualization_radius = max(
            3,
            int(round(median_radius * WELL_DETECTION.visualization_radius_scale)),
        )

        labeled = image.copy()
        cv2.polylines(labeled, [slab_corners.astype(np.int32)], True, (0, 255, 255), 2)
        assigned: List[WellDetail] = []
        for row_index in range(PLATE_GEOMETRY.rows):
            for col_index in range(PLATE_GEOMETRY.cols):
                cell_key = (row_index, col_index)
                selection = selected_by_cell.get(cell_key)
                if selection is None:
                    image_point = self._inverse_normalized_point(
                        inverse_transform=inverse_transform,
                        normalized_point=(col_centers[col_index], row_centers[row_index]),
                    )
                    score = 0.0
                    detected = False
                    source_radius = median_radius
                else:
                    candidate = selection["candidate"]
                    image_point = candidate.center
                    score = float(selection["effective_score"])
                    detected = True
                    source_radius = float(candidate.radius)

                center = (int(round(image_point[0])), int(round(image_point[1])))
                well_number = self._well_number(row_index=row_index, col_index=col_index)
                well_detail = WellDetail(
                    well_number=well_number,
                    row_index=row_index,
                    col_index=col_index,
                    label=self._well_label(row_index=row_index, col_index=col_index),
                    center=center,
                    sample_radius=uniform_sample_radius,
                    source_radius=source_radius,
                    score=score,
                    detected=detected,
                )
                assigned.append(well_detail)

                render_color = (0, 255, 0) if detected else (0, 165, 255)
                cv2.circle(
                    labeled,
                    center,
                    uniform_visualization_radius,
                    render_color,
                    2,
                )
                cv2.putText(
                    labeled,
                    str(well_number),
                    (center[0] + 4, center[1] - 4),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.36,
                    render_color,
                    1,
                    cv2.LINE_AA,
                )

        cv2.putText(
            labeled,
            f"assigned detected={detected_count} inferred={inferred_count}",
            (20, image.shape[0] - 40),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )
        assigned.sort(key=lambda well: well.well_number)
        return assigned, labeled

    def _classify_assigned_wells(
        self,
        image: np.ndarray,
        assigned_wells: List[WellDetail],
    ) -> Tuple[List[int], List[Optional[str]], np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """
        Extract per-well sample regions and classify colors from detected centers.
        """

        annotated = image.copy()
        sample_regions = image.copy()
        clean_result = np.full(image.shape, 245, dtype=np.uint8)
        hsv_image = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        uniform_visualization_radius = max(
            3,
            int(
                round(
                    np.median([well.sample_radius for well in assigned_wells])
                    * (WELL_DETECTION.visualization_radius_scale / max(WELL_DETECTION.sample_radius_scale, 1e-6))
                )
            ),
        )

        well_values: List[int] = [0] * (PLATE_GEOMETRY.rows * PLATE_GEOMETRY.cols)
        well_colors: List[Optional[str]] = [None] * (PLATE_GEOMETRY.rows * PLATE_GEOMETRY.cols)

        for well in assigned_wells:
            mask = np.zeros(image.shape[:2], dtype=np.uint8)
            cv2.circle(mask, well.center, well.sample_radius, 255, -1)
            mean_hsv = cv2.mean(hsv_image, mask=mask)[:3]
            avg_hsv = [round(float(value), 2) for value in mean_hsv]
            classified_color = self._classify_hsv(avg_hsv)
            gene_value = self._gene_value_from_color(classified_color)
            render_color = self._render_bgr_from_color(classified_color)

            well.color = classified_color
            well.gene_value = gene_value
            well_values[well.well_number - 1] = gene_value
            well_colors[well.well_number - 1] = classified_color

            cv2.circle(sample_regions, well.center, well.sample_radius, render_color, 2)
            cv2.circle(
                clean_result,
                well.center,
                uniform_visualization_radius,
                render_color,
                -1,
            )
            cv2.circle(
                clean_result,
                well.center,
                uniform_visualization_radius,
                (200, 200, 200),
                2,
            )
            cv2.putText(
                annotated,
                f"{well.well_number}:{gene_value}",
                (well.center[0] + 4, well.center[1] - 4),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.36,
                (0, 0, 0),
                2,
                cv2.LINE_AA,
            )
            cv2.putText(
                annotated,
                f"{well.well_number}:{gene_value}",
                (well.center[0] + 4, well.center[1] - 4),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.36,
                render_color,
                1,
                cv2.LINE_AA,
            )
            cv2.circle(
                annotated,
                well.center,
                max(3, int(round(well.source_radius * WELL_DETECTION.visualization_radius_scale))),
                render_color,
                2,
            )

        ordered_result = self._render_ordered_result(
            image_shape=image.shape,
            assigned_wells=assigned_wells,
            uniform_visualization_radius=uniform_visualization_radius,
        )

        return well_values, well_colors, annotated, clean_result, ordered_result, sample_regions

    def _render_ordered_result(
        self,
        image_shape: Tuple[int, int, int],
        assigned_wells: List[WellDetail],
        uniform_visualization_radius: int,
    ) -> np.ndarray:
        """
        Render one clean ordered grid of classified wells using output order.
        """

        ordered_result = np.full(image_shape, 245, dtype=np.uint8)
        image_height, image_width = image_shape[:2]
        cols = max(PLATE_GEOMETRY.cols, 1)
        rows = max(PLATE_GEOMETRY.rows, 1)
        margin_x = int(round(image_width * 0.09))
        margin_y = int(round(image_height * 0.06))
        usable_width = max(image_width - (2 * margin_x), cols)
        usable_height = max(image_height - (2 * margin_y), rows)
        cell_width = usable_width / cols
        cell_height = usable_height / rows
        circle_radius = max(
            uniform_visualization_radius,
            max(8, int(round(min(cell_width, cell_height) * 0.38))),
        )
        ordered_wells = sorted(assigned_wells, key=lambda well: well.well_number)

        for index, well in enumerate(ordered_wells):
            display_row = index // cols
            display_col = (cols - 1) - (index % cols)
            center_x = int(round(margin_x + ((display_col + 0.5) * cell_width)))
            center_y = int(round(margin_y + ((display_row + 0.5) * cell_height)))
            center = (center_x, center_y)
            render_color = self._render_bgr_from_color(well.color)

            cv2.circle(ordered_result, center, circle_radius, render_color, -1)
            cv2.circle(ordered_result, center, circle_radius, (200, 200, 200), 2)

        return ordered_result

    def _normalize_candidates(
        self,
        candidates: List[WellCandidate],
        slab_corners: np.ndarray,
    ) -> List[Dict[str, object]]:
        """
        Transform image-space candidate centers onto the helper slab plane.
        """

        transform = cv2.getPerspectiveTransform(
            slab_corners.astype(np.float32),
            self._helper_destination_corners(),
        )
        points = np.array([[candidate.center] for candidate in candidates], dtype=np.float32)
        normalized = cv2.perspectiveTransform(points, transform)
        normalized_candidates: List[Dict[str, object]] = []
        for candidate, point in zip(candidates, normalized, strict=False):
            normalized_candidates.append(
                {
                    "candidate": candidate,
                    "normalized_center": (float(point[0][0]), float(point[0][1])),
                }
            )
        return normalized_candidates

    def _expected_helper_centers(self) -> List[Tuple[float, float]]:
        """
        Return one rough helper-plane center for each expected well.
        """

        active_left = PLATE_GEOMETRY.warp_width * PLATE_GEOMETRY.active_left_ratio
        active_top = PLATE_GEOMETRY.warp_height * PLATE_GEOMETRY.active_top_ratio
        active_right = PLATE_GEOMETRY.warp_width * PLATE_GEOMETRY.active_right_ratio
        active_bottom = PLATE_GEOMETRY.warp_height * PLATE_GEOMETRY.active_bottom_ratio
        cell_width = (active_right - active_left) / max(PLATE_GEOMETRY.cols, 1)
        cell_height = (active_bottom - active_top) / max(PLATE_GEOMETRY.rows, 1)

        centers: List[Tuple[float, float]] = []
        for row_index in range(PLATE_GEOMETRY.rows):
            for col_index in range(PLATE_GEOMETRY.cols):
                centers.append(
                    (
                        active_left + ((col_index + 0.5) * cell_width),
                        active_top + ((row_index + 0.5) * cell_height),
                    )
                )
        return centers

    def _build_slab_mask(self, image_shape: Tuple[int, int], slab_corners: np.ndarray) -> np.ndarray:
        """
        Build a filled ROI mask from the helper slab quadrilateral.
        """

        mask = np.zeros(image_shape, dtype=np.uint8)
        cv2.fillConvexPoly(mask, slab_corners.astype(np.int32), 255)
        return mask

    def _build_helper_slab_mask(self, combined_mask: np.ndarray) -> np.ndarray:
        """
        Fuse fragmented bright slab structure into one helper silhouette.
        """

        helper_close_kernel = np.ones(DETECTION.helper_close_kernel, dtype=np.uint8)
        helper_open_kernel = np.ones(DETECTION.helper_open_kernel, dtype=np.uint8)
        helper_dilate_kernel = np.ones(DETECTION.helper_dilate_kernel, dtype=np.uint8)

        helper_mask = cv2.morphologyEx(combined_mask, cv2.MORPH_CLOSE, helper_close_kernel)
        helper_mask = cv2.morphologyEx(helper_mask, cv2.MORPH_OPEN, helper_open_kernel)
        helper_mask = cv2.dilate(helper_mask, helper_dilate_kernel, iterations=1)
        return helper_mask

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

    def _expand_and_clip_quad(self, quad: np.ndarray, image_shape: Tuple[int, ...]) -> np.ndarray:
        """
        Expand the helper quad outward around its center and keep it inside the image.
        """

        center = quad.mean(axis=0)
        expanded = center + ((quad - center) * DETECTION.helper_quad_expand_scale)
        image_height, image_width = image_shape[:2]
        expanded[:, 0] = np.clip(expanded[:, 0], 0.0, float(image_width - 1))
        expanded[:, 1] = np.clip(expanded[:, 1], 0.0, float(image_height - 1))
        return expanded.astype(np.float32)

    @staticmethod
    def _quad_area_ratio(quad: np.ndarray, image_shape: Tuple[int, ...]) -> float:
        """
        Return the ratio of the quad area to the full analysis image area.
        """

        image_height, image_width = image_shape[:2]
        image_area = max(float(image_height * image_width), 1.0)
        return float(abs(cv2.contourArea(quad.astype(np.float32))) / image_area)

    @staticmethod
    def _full_frame_quad(image_shape: Tuple[int, ...]) -> np.ndarray:
        """
        Return a portrait-ordered quadrilateral covering the full analysis image.
        """

        image_height, image_width = image_shape[:2]
        return np.array(
            [
                [0, 0],
                [image_width - 1, 0],
                [image_width - 1, image_height - 1],
                [0, image_height - 1],
            ],
            dtype=np.float32,
        )

    def _warp_slab(self, image: np.ndarray, corners: np.ndarray) -> np.ndarray:
        """
        Produce a helper warp for diagnostics and lattice normalization.
        """

        transform = cv2.getPerspectiveTransform(
            corners.astype(np.float32),
            self._helper_destination_corners(),
        )
        return cv2.warpPerspective(
            image,
            transform,
            (PLATE_GEOMETRY.warp_width, PLATE_GEOMETRY.warp_height),
        )

    @staticmethod
    def _helper_destination_corners() -> np.ndarray:
        """
        Return helper plane corners for normalization and debug warps.
        """

        return np.array(
            [
                [0, 0],
                [PLATE_GEOMETRY.warp_width - 1, 0],
                [PLATE_GEOMETRY.warp_width - 1, PLATE_GEOMETRY.warp_height - 1],
                [0, PLATE_GEOMETRY.warp_height - 1],
            ],
            dtype=np.float32,
        )

    def _cluster_axis(self, values: np.ndarray, cluster_count: int) -> Optional[np.ndarray]:
        """
        Cluster one axis of normalized candidate centers into evenly ordered rows or columns.
        """

        if values.size < cluster_count:
            return None

        samples = values.reshape(-1, 1).astype(np.float32)
        criteria = (
            cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER,
            50,
            0.2,
        )
        compactness, labels, centers = cv2.kmeans(
            samples,
            cluster_count,
            None,
            criteria,
            8,
            cv2.KMEANS_PP_CENTERS,
        )
        if labels is None or centers is None:
            return None
        del compactness
        return np.sort(centers.flatten())

    @staticmethod
    def _median_spacing(centers: np.ndarray) -> float:
        """
        Return the median distance between consecutive sorted cluster centers.
        """

        if centers.size <= 1:
            return 1.0
        diffs = np.diff(centers)
        positive = diffs[diffs > 0]
        if positive.size == 0:
            return 1.0
        return float(np.median(positive))

    @staticmethod
    def _nearest_center_index(value: float, centers: np.ndarray) -> int:
        """
        Return the nearest sorted center index for one normalized coordinate.
        """

        return int(np.argmin(np.abs(centers - value)))

    @staticmethod
    def _inverse_normalized_point(
        inverse_transform: np.ndarray,
        normalized_point: Tuple[float, float],
    ) -> Tuple[float, float]:
        """
        Map one helper-plane point back into image coordinates.
        """

        point = np.array([[normalized_point]], dtype=np.float32)
        transformed = cv2.perspectiveTransform(point, inverse_transform)
        return float(transformed[0][0][0]), float(transformed[0][0][1])

    def _map_helper_candidate_to_image(
        self,
        candidate: WellCandidate,
        inverse_transform: np.ndarray,
        radius_override: Optional[float] = None,
    ) -> WellCandidate:
        """
        Map one helper-warp candidate back into analysis-input coordinates.
        """

        helper_radius = candidate.radius if radius_override is None else radius_override
        image_center = self._inverse_normalized_point(
            inverse_transform=inverse_transform,
            normalized_point=candidate.center,
        )
        edge_point = self._inverse_normalized_point(
            inverse_transform=inverse_transform,
            normalized_point=(candidate.center[0] + helper_radius, candidate.center[1]),
        )
        mapped_radius = max(2.0, math.dist(image_center, edge_point))
        return WellCandidate(
            center=image_center,
            radius=mapped_radius,
            score=candidate.score,
            source=candidate.source,
            circularity=candidate.circularity,
        )

    def _classify_hsv(self, hsv: List[float]) -> Optional[str]:
        """
        Classify a well color using the configured color profiles.
        """

        h, s, v = hsv
        hsv_int = (int(round(h)), int(round(s)), int(round(v)))

        for profile in COLOR_PROFILES:
            for threshold in profile.ranges:
                lower = threshold["lower"]
                upper = threshold["upper"]
                if self._in_range(hsv_int, lower, upper):
                    return profile.name

        closest_name: Optional[str] = None
        closest_distance = float("inf")
        for profile in COLOR_PROFILES:
            for threshold in profile.ranges:
                distance = self._distance_to_hsv_range(
                    value=hsv_int,
                    lower=threshold["lower"],
                    upper=threshold["upper"],
                )
                if distance < closest_distance:
                    closest_distance = distance
                    closest_name = profile.name

        return closest_name

    @staticmethod
    def _gene_value_from_color(color_name: Optional[str]) -> int:
        """
        Convert the recognized color into gene presence output.
        """

        if color_name is None:
            return 0
        profile = COLOR_PROFILE_BY_NAME.get(color_name)
        if profile is None:
            return 0
        return profile.gene_value

    @staticmethod
    def _render_bgr_from_color(color_name: Optional[str]) -> Tuple[int, int, int]:
        """
        Convert an internal color label to a clean visualization color.
        """

        if color_name is None:
            return DEFAULT_RENDER_COLOR
        profile = COLOR_PROFILE_BY_NAME.get(color_name)
        if profile is None:
            return DEFAULT_RENDER_COLOR
        return profile.render_bgr

    @staticmethod
    def _well_number(row_index: int, col_index: int) -> int:
        """
        Number wells top-to-bottom and right-to-left within each row.
        """

        row_offset = row_index * PLATE_GEOMETRY.cols
        return row_offset + (PLATE_GEOMETRY.cols - col_index)

    @staticmethod
    def _well_label(row_index: int, col_index: int) -> str:
        """
        Return a human-readable row/column label on the current 12x8 layout.
        """

        return f"R{row_index + 1}C{PLATE_GEOMETRY.cols - col_index}"

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
    def _distance_to_hsv_range(
        value: Tuple[int, int, int],
        lower: Tuple[int, int, int],
        upper: Tuple[int, int, int],
    ) -> float:
        """
        Measure how far an HSV sample lies outside one configured range.
        """

        weighted_distance = 0.0
        channel_weights = (3.0, 0.08, 0.05)

        for index, channel_value in enumerate(value):
            if channel_value < lower[index]:
                delta = float(lower[index] - channel_value)
            elif channel_value > upper[index]:
                delta = float(channel_value - upper[index])
            else:
                delta = 0.0
            weighted_distance += delta * channel_weights[index]

        return weighted_distance

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
