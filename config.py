"""
Central configuration for the Raspberry Pi image analysis project.

This module keeps tunable values in one place so the capture and analysis
pipeline can stay small, readable, and easy to maintain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple


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
    left_ratio: float = 0.18
    top_ratio: float = 0.03
    right_ratio: float = 0.80
    bottom_ratio: float = 0.93


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
    active_bottom_ratio: float = 0.965


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
    target_aspect_ratio: float = 1.5
    max_aspect_ratio_deviation: float = 0.9
    component_min_area_ratio: float = 0.03
    component_center_weight: float = 0.45
    component_area_weight: float = 0.55


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
    stream_width: int = 1280
    stream_height: int = 960
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
    database_name: str = "image_analysis"
    collection_name: str = "well_plate_results"
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
OUTPUT = OutputConfig()
CAMERA = CameraConfig()
MONGO = MongoConfig()
RUN_TIMING = RunTimingConfig()
PREPROCESS_CROP = PreprocessCropConfig()
MANUAL_CROP = ManualCropConfig()

# HSV ranges are deliberately grouped in a simple list format so they can be
# tuned in the field without touching analysis logic. OpenCV HSV uses:
# H: 0-179, S: 0-255, V: 0-255.
HSV_THRESHOLDS: Dict[str, List[Dict[str, Tuple[int, int, int]]]] = {
    "light_pink": [
        {"lower": (160, 20, 140), "upper": (179, 120, 255)},
    ],
    "red": [
        {"lower": (0, 100, 60), "upper": (10, 255, 255)},
        {"lower": (170, 121, 60), "upper": (179, 255, 255)},
    ],
    "yellow": [
        {"lower": (23, 70, 80), "upper": (35, 255, 255)},
    ],
}
