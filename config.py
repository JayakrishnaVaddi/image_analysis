"""
Central configuration for the Raspberry Pi image analysis project.

This module keeps tunable values in one place so the capture and analysis
pipeline can stay small, readable, and easy to maintain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple


GridSize = Tuple[int, int]
Point = Tuple[int, int]


@dataclass(frozen=True)
class ManualCropConfig:
    """
    Fallback crop coordinates used when contour-based slab detection fails.

    Coordinates are in pixel space relative to the input image.
    """

    enabled: bool = False
    top_left: Point = (100, 100)
    bottom_right: Point = (1100, 800)


@dataclass(frozen=True)
class PreprocessCropConfig:
    """
    Optional input crop applied before slab detection.

    Ratios are relative to the full captured image and let us focus analysis on
    the central fixture area without changing camera capture settings.
    """

    enabled: bool = True
    left_ratio: float = 0.225
    top_ratio: float = 0
    right_ratio: float = 0.72
    bottom_ratio: float = 0.90


@dataclass(frozen=True)
class PreprocessSmoothingConfig:
    """
    Optional light smoothing applied before analysis starts.

    This helps stabilize color sampling and slab detection when the camera
    introduces small frame-to-frame noise.
    """

    enabled: bool = True
    gaussian_kernel: GridSize = (5, 5)
    gaussian_sigma: float = 0.0


@dataclass(frozen=True)
class StreamCropConfig:
    """
    Crop applied only to the outgoing live WebSocket stream.

    This is intentionally separate from PREPROCESS_CROP so stream framing can be
    tuned without affecting the analysis pipeline.
    """

    enabled: bool = True
    left_ratio: float = 0.225
    top_ratio: float = 0.01
    right_ratio: float = 0.68
    bottom_ratio: float = 0.885


@dataclass(frozen=True)
class PlateGeometry:
    """
    Geometry configuration for the known vertical slab layout.
    """

    rows: int = 12
    cols: int = 8
    warp_width: int = 800
    warp_height: int = 1200
    sample_radius_ratio: float = 0.28
    visualization_radius_ratio: float = 0.42
    # The warped slab includes non-well label gutters on the top and right.
    # These ratios define the usable well grid area inside the warped slab.
    active_left_ratio: float = 0.05
    active_top_ratio: float = 0.01
    active_right_ratio: float = 0.90
    active_bottom_ratio: float = 0.99


@dataclass(frozen=True)
class DetectionConfig:
    """
    Tunable parameters for slab detection.
    """

    gaussian_blur_kernel: GridSize = (5, 5)
    min_contour_area_ratio: float = 0.10
    max_contour_area_ratio: float = 0.98
    white_low_saturation_max: int = 110
    white_value_min: int = 120
    grayscale_value_min: int = 120
    close_kernel: GridSize = (31, 31)
    open_kernel: GridSize = (7, 7)
    helper_close_kernel: GridSize = (91, 91)
    helper_open_kernel: GridSize = (17, 17)
    helper_dilate_kernel: GridSize = (15, 15)
    target_aspect_ratio: float = 1.5
    max_aspect_ratio_deviation: float = 0.9
    component_min_area_ratio: float = 0.03
    component_center_weight: float = 0.45
    component_area_weight: float = 0.55
    helper_quad_expand_scale: float = 1.10
    helper_quad_min_area_ratio: float = 0.42


@dataclass(frozen=True)
class WellDetectionConfig:
    """
    Tunables for per-well candidate detection and grid assignment.
    """

    adaptive_block_size: int = 41
    adaptive_c: int = 8
    saturation_threshold: int = 45
    value_threshold: int = 70
    contour_area_min_scale: float = 0.28
    contour_area_max_scale: float = 1.85
    min_circularity: float = 0.32
    max_ellipse_axis_ratio: float = 2.2
    duplicate_center_distance_scale: float = 0.85
    duplicate_radius_delta_scale: float = 0.7
    hough_dp: float = 1.2
    hough_min_dist_scale: float = 1.5
    hough_param1: int = 110
    hough_param2: int = 12
    # Fixed well radius in helper-warp pixels for this controlled device setup.
    # This is the main value to tweak if candidate circles look too large/small.
    fixed_candidate_radius_px: float = 24.0
    hough_min_radius_scale: float = 0.55
    hough_max_radius_scale: float = 1.15
    row_cluster_count: int = 12
    col_cluster_count: int = 8
    min_detected_wells: int = 56
    max_inferred_wells: int = 40
    max_assignment_error_scale: float = 0.72
    sample_radius_scale: float = 0.42
    visualization_radius_scale: float = 0.88
    local_refine_window_scale: float = 1.35
    local_refine_max_shift_scale: float = 0.6
    local_refine_saturation_min: int = 28
    local_refine_value_min: int = 25
    local_refine_min_area_scale: float = 0.18


@dataclass(frozen=True)
class OutputConfig:
    """
    Output and persistence defaults.
    """

    output_dir: str = "output"
    file_timestamp_format: str = "%Y%m%dT%H%M%S"
    run_directory_prefix: str = "run_"


@dataclass(frozen=True)
class CameraConfig:
    """
    Camera capture tuning for Raspberry Pi deployments.
    """

    capture_timeout_seconds: float = 8.0

    frame_width: int = 4056
    frame_height: int = 3040
    rpicam_timeout_ms: int = 1500
    rpicam_command_candidates: Tuple[str, ...] = ("rpicam-still", "libcamera-still")
    rpicam_extra_args: Tuple[str, ...] = ("--nopreview",)
    stream_source_width: int = 2028
    stream_source_height: int = 1520
    stream_width: int = 960
    stream_height: int = 1280
    stream_framerate: int = 12
    rpicam_video_command_candidates: Tuple[str, ...] = ("rpicam-vid", "libcamera-vid")


@dataclass(frozen=True)
class MongoConfig:
    """
    MongoDB Atlas configuration.
    """

    uri_env_var: str = "MONGO_URI"
    database_name_env_var: str = "MONGO_DB_NAME"
    collection_name_env_var: str = "MONGO_COLLECTION_NAME"
    source_database_name_env_var: str = "MONGO_SOURCE_DB_NAME"
    source_collection_name_env_var: str = "MONGO_SOURCE_COLLECTION_NAME"
    database_name: str = "image_analysis"
    collection_name: str = "well_plate_results"
    source_database_name: str = "Gene_set"
    source_collection_name: str = "Gene_panel"
    server_selection_timeout_ms: int = 3000


@dataclass(frozen=True)
class RunTimingConfig:
    """
    Heating wait durations for production and local testing.
    """

    test_wait_seconds: int = 30
    production_wait_seconds: int = 600


PLATE_GEOMETRY = PlateGeometry()
DETECTION = DetectionConfig()
WELL_DETECTION = WellDetectionConfig()
OUTPUT = OutputConfig()
CAMERA = CameraConfig()
MONGO = MongoConfig()
RUN_TIMING = RunTimingConfig()
PREPROCESS_CROP = PreprocessCropConfig()
PREPROCESS_SMOOTHING = PreprocessSmoothingConfig()
STREAM_CROP = StreamCropConfig()
MANUAL_CROP = ManualCropConfig()
