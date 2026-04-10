"""
Command-line entry point for Raspberry Pi image analysis.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import cv2
import numpy as np

from camera_capture import (
    CameraCaptureError,
    ImageLoadError,
    close_display_windows,
    capture_frame,
    display_image,
    load_image,
)
from config import OUTPUT, PREPROCESS_CROP, PREPROCESS_SMOOTHING
from db_handler import upload_run_document
from plate_analyzer import PlateAnalyzer, SlabDetectionError, WellDetectionError


LOGGER = logging.getLogger(__name__)


def build_argument_parser() -> argparse.ArgumentParser:
    """
    Build the CLI for live capture or image-based analysis.
    """

    parser = argparse.ArgumentParser(description="Analyze a 96-well slab image")
    parser.add_argument(
        "--mode",
        required=True,
        choices=["live", "image"],
        help="Capture from the Pi camera or analyze an image from disk",
    )
    parser.add_argument(
        "--image",
        help="Path to a test image when running in image mode",
    )
    parser.add_argument(
        "--plate-id",
        help="Plate identifier used in the final JSON/MongoDB payload",
    )
    parser.add_argument(
        "--mongo-uri",
        help="MongoDB connection string override. Defaults to MONGODB_ATLAS_URI if set.",
    )
    parser.add_argument(
        "--display",
        action="store_true",
        help="Display OpenCV windows for visual inspection",
    )
    parser.add_argument(
        "--camera-index",
        type=int,
        default=0,
        help="Camera index passed to Raspberry Pi camera tools",
    )
    return parser


def configure_logging() -> None:
    """
    Configure application-wide structured logging.
    """

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def ensure_output_directory() -> Path:
    """
    Create the output directory if it does not already exist.
    """

    output_dir = Path(__file__).resolve().parent / OUTPUT.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def create_run_directory(run_id: str) -> Path:
    """
    Create a dedicated folder for one analysis iteration.
    """

    run_directory = ensure_output_directory() / run_id
    run_directory.mkdir(parents=True, exist_ok=True)
    return run_directory


def current_iso_timestamp() -> str:
    """
    Return a timezone-aware UTC timestamp in ISO 8601 format.
    """

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def timestamp_for_filename() -> str:
    """
    Return a compact timestamp suitable for file names.
    """

    return datetime.now(timezone.utc).strftime(OUTPUT.file_timestamp_format)


def save_image(path: Path, image) -> None:
    """
    Save an image and raise a helpful error if OpenCV fails.
    """

    success = cv2.imwrite(str(path), image)
    if not success:
        raise IOError(f"Failed to write image to {path}")


def save_json(path: Path, payload: Dict[str, Any]) -> None:
    """
    Save JSON output with readable formatting.
    """

    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)


def acquire_image(args: argparse.Namespace):
    """
    Acquire an image from the configured source.
    """

    if args.mode == "live":
        scratch_dir = ensure_output_directory()
        return capture_frame(
            camera_index=args.camera_index,
            scratch_dir=scratch_dir,
        )

    if not args.image:
        raise ValueError("--image is required when --mode image is used")

    return load_image(args.image)


def preprocess_image(image: np.ndarray) -> np.ndarray:
    """
    Optionally smooth and crop the input image before slab detection.
    """

    processed = image.copy()

    if PREPROCESS_SMOOTHING.enabled:
        processed = cv2.GaussianBlur(
            processed,
            PREPROCESS_SMOOTHING.gaussian_kernel,
            PREPROCESS_SMOOTHING.gaussian_sigma,
        )
        LOGGER.info(
            "Applied preprocessing smoothing: kernel=%s sigma=%s",
            PREPROCESS_SMOOTHING.gaussian_kernel,
            PREPROCESS_SMOOTHING.gaussian_sigma,
        )

    if not PREPROCESS_CROP.enabled:
        return processed

    image_height, image_width = processed.shape[:2]
    left = max(0, min(image_width - 1, int(round(image_width * PREPROCESS_CROP.left_ratio))))
    top = max(0, min(image_height - 1, int(round(image_height * PREPROCESS_CROP.top_ratio))))
    right = max(left + 1, min(image_width, int(round(image_width * PREPROCESS_CROP.right_ratio))))
    bottom = max(top + 1, min(image_height, int(round(image_height * PREPROCESS_CROP.bottom_ratio))))

    cropped = processed[top:bottom, left:right].copy()
    LOGGER.info(
        "Applied preprocessing crop: left=%s top=%s right=%s bottom=%s shape=%s",
        left,
        top,
        right,
        bottom,
        cropped.shape,
    )
    return cropped


def build_run_document(
    plate_id: str,
    timestamp: str,
    analysis_result,
) -> Dict[str, Any]:
    """
    Assemble the document persisted locally and optionally uploaded to MongoDB.
    """

    binary_data = validate_binary_data(analysis_result.gene_presence)
    return {
        "plateId": plate_id,
        "timestamp": timestamp,
        "binaryData": binary_data,
    }


def build_mongo_document(plate_id: str, timestamp: str, analysis_result) -> Dict[str, Any]:
    """
    Build the minimal MongoDB payload for one run.
    """

    binary_data = validate_binary_data(analysis_result.gene_presence)
    return {
        "plateId": plate_id,
        "timestamp": timestamp,
        "binaryData": binary_data,
    }


def validate_binary_data(binary_data: Any) -> list[int]:
    """
    Ensure the final result payload is always a 96-element 0/1 array.
    """

    if not isinstance(binary_data, list):
        raise ValueError("binaryData must be a list")
    if len(binary_data) != 96:
        raise ValueError(f"binaryData must contain exactly 96 values, got {len(binary_data)}")
    if any(value not in (0, 1) for value in binary_data):
        raise ValueError("binaryData values must be 0 or 1")
    return binary_data


def save_artifacts(
    output_dir: Path,
    input_image,
    analysis_result,
) -> Dict[str, str]:
    """
    Save required output images for a single analysis run.
    """

    saved_files: Dict[str, str] = {}

    original_path = output_dir / "original.jpg"
    save_image(original_path, input_image)
    saved_files["original_image"] = str(original_path)

    analyzed_input_path = output_dir / "analyzed_input.jpg"
    save_image(analyzed_input_path, analysis_result.artifacts.original)
    saved_files["analyzed_input_image"] = str(analyzed_input_path)

    slab_detection_path = output_dir / "slab_detection.jpg"
    save_image(slab_detection_path, analysis_result.artifacts.slab_detection)
    saved_files["slab_detection_image"] = str(slab_detection_path)

    warped_path = output_dir / "warped_slab.jpg"
    save_image(warped_path, analysis_result.artifacts.warped_slab)
    saved_files["warped_slab_image"] = str(warped_path)

    grid_overlay_path = output_dir / "grid_overlay.jpg"
    save_image(grid_overlay_path, analysis_result.artifacts.grid_overlay)
    saved_files["grid_overlay_image"] = str(grid_overlay_path)

    candidate_wells_path = output_dir / "candidate_wells.jpg"
    save_image(candidate_wells_path, analysis_result.artifacts.candidate_wells)
    saved_files["candidate_wells_image"] = str(candidate_wells_path)

    labeled_wells_path = output_dir / "labeled_wells.jpg"
    save_image(labeled_wells_path, analysis_result.artifacts.labeled_wells)
    saved_files["labeled_wells_image"] = str(labeled_wells_path)

    sample_regions_path = output_dir / "sample_regions.jpg"
    save_image(sample_regions_path, analysis_result.artifacts.sample_regions)
    saved_files["sample_regions_image"] = str(sample_regions_path)

    annotated_path = output_dir / "annotated_result.jpg"
    save_image(annotated_path, analysis_result.artifacts.annotated_result)
    saved_files["annotated_result_image"] = str(annotated_path)

    clean_result_path = output_dir / "clean_result.jpg"
    save_image(clean_result_path, analysis_result.artifacts.clean_result)
    saved_files["clean_result_image"] = str(clean_result_path)

    ordered_result_path = output_dir / "result.jpg"
    save_image(ordered_result_path, analysis_result.artifacts.ordered_result)
    saved_files["ordered_result_image"] = str(ordered_result_path)

    return saved_files


def run_analysis(
    mode: str,
    image_path: Optional[str] = None,
    plate_id: Optional[str] = None,
    mongo_uri: Optional[str] = None,
    camera_index: int = 0,
    display: bool = False,
) -> Dict[str, Any]:
    """
    Run one analysis pass and return the saved run information.
    """

    run_id = f"{OUTPUT.run_directory_prefix}{timestamp_for_filename()}"
    run_dir = create_run_directory(run_id)
    timestamp = current_iso_timestamp()
    args = argparse.Namespace(
        mode=mode,
        image=image_path,
        plate_id=plate_id,
        mongo_uri=mongo_uri,
        camera_index=camera_index,
        display=display,
    )

    try:
        image = acquire_image(args)
    except (CameraCaptureError, ImageLoadError, ValueError, OSError) as exc:
        LOGGER.error("Failed to acquire image: %s", exc)
        raise

    analysis_input = preprocess_image(image)
    analyzer = PlateAnalyzer()

    try:
        analysis_result = analyzer.analyze(analysis_input)
    except (SlabDetectionError, WellDetectionError) as exc:
        LOGGER.error("Analysis detection failed: %s", exc)
        original_failure_path = run_dir / "original_failed_detection.jpg"
        analyzed_failure_path = run_dir / "analyzed_input_failed_detection.jpg"
        debug_failure_path = run_dir / "detection_failed.jpg"
        save_image(original_failure_path, image)
        save_image(analyzed_failure_path, analysis_input)
        if exc.debug_image is not None:
            save_image(debug_failure_path, exc.debug_image)
        raise

    saved_files = save_artifacts(run_dir, image, analysis_result)
    json_output_path = run_dir / "results.json"
    final_plate_id = plate_id or run_id
    run_document = build_run_document(final_plate_id, timestamp, analysis_result)

    mongo_document = build_mongo_document(final_plate_id, timestamp, analysis_result)
    mongo_inserted_id = upload_run_document(mongo_document, args.mongo_uri)

    save_json(json_output_path, run_document)
    LOGGER.info("Saved JSON results to %s", json_output_path)

    if display:
        try:
            display_image("Annotated Result", analysis_result.artifacts.annotated_result, delay_ms=0)
        finally:
            close_display_windows()

    return {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "payload": run_document,
        "saved_files": saved_files,
        "mongo_inserted_id": mongo_inserted_id,
    }


def main() -> int:
    """
    Execute the end-to-end image analysis pipeline.
    """

    configure_logging()
    parser = build_argument_parser()
    args = parser.parse_args()
    try:
        run_analysis(
            mode=args.mode,
            image_path=args.image,
            plate_id=args.plate_id,
            mongo_uri=args.mongo_uri,
            camera_index=args.camera_index,
            display=args.display,
        )
    except (CameraCaptureError, ImageLoadError, ValueError, OSError, SlabDetectionError, WellDetectionError) as exc:
        LOGGER.error("Analysis failed: %s", exc)
        return 1
    except Exception as exc:  # pragma: no cover - defensive catch for field usage.
        LOGGER.exception("Unexpected analysis error: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
