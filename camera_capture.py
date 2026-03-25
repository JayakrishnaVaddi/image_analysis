"""
Camera and image loading utilities.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Generator, Optional

import cv2
import numpy as np

from config import CAMERA


LOGGER = logging.getLogger(__name__)


class CameraCaptureError(RuntimeError):
    """
    Raised when a frame cannot be captured from the camera.
    """


class ImageLoadError(RuntimeError):
    """
    Raised when an image cannot be loaded from disk.
    """


def capture_frame(
    camera_index: int = 0,
    scratch_dir: Optional[Path] = None,
) -> np.ndarray:
    """
    Capture a single frame using Raspberry Pi camera tools.

    The OpenCV live camera path was removed because it is not reliable on this
    Raspberry Pi setup, while `rpicam-still` works consistently.
    """

    return _capture_frame_with_rpicam(
        camera_index=camera_index,
        scratch_dir=scratch_dir,
    )


def _capture_frame_with_rpicam(
    camera_index: int = 0,
    scratch_dir: Optional[Path] = None,
) -> np.ndarray:
    """
    Capture using Raspberry Pi camera command-line tools.

    This is the most reliable path on modern Raspberry Pi OS images where the
    camera is managed by libcamera rather than exposed as a streaming V4L2 node
    that OpenCV can read directly.
    """

    command_name = _resolve_rpicam_command()
    if command_name is None:
        raise CameraCaptureError(
            "OpenCV capture failed and no Raspberry Pi camera capture command was found. "
            "Expected one of: rpicam-still, libcamera-still."
        )

    if scratch_dir is None:
        scratch_dir = Path.cwd()

    scratch_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        suffix=".jpg",
        prefix="camera_capture_",
        dir=str(scratch_dir),
        delete=False,
    ) as handle:
        temp_path = Path(handle.name)

    command = [
        command_name,
        "--output",
        str(temp_path),
        "--timeout",
        str(CAMERA.rpicam_timeout_ms),
        "--width",
        str(CAMERA.frame_width),
        "--height",
        str(CAMERA.frame_height),
        "--camera",
        str(camera_index),
        *CAMERA.rpicam_extra_args,
    ]

    LOGGER.info("Capturing frame via %s", command_name)

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=max(1.0, CAMERA.capture_timeout_seconds),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        temp_path.unlink(missing_ok=True)
        raise CameraCaptureError(
            f"{command_name} timed out while capturing a frame: {exc}"
        ) from exc
    except OSError as exc:
        temp_path.unlink(missing_ok=True)
        raise CameraCaptureError(
            f"Failed to start {command_name} for camera capture: {exc}"
        ) from exc

    if completed.returncode != 0:
        temp_path.unlink(missing_ok=True)
        stderr_text = (completed.stderr or "").strip()
        stdout_text = (completed.stdout or "").strip()
        detail = stderr_text or stdout_text or f"exit code {completed.returncode}"
        raise CameraCaptureError(f"{command_name} failed to capture an image: {detail}")

    try:
        frame = cv2.imread(str(temp_path))
        if frame is None or frame.size == 0:
            raise CameraCaptureError(
                f"{command_name} created an unreadable image file at {temp_path}"
            )
        LOGGER.info("Camera backend: %s", command_name)
        LOGGER.info("Captured frame with shape %s", frame.shape)
        return frame
    finally:
        temp_path.unlink(missing_ok=True)


def _resolve_rpicam_command() -> Optional[str]:
    """
    Return the first available Raspberry Pi camera capture command.
    """

    for candidate in CAMERA.rpicam_command_candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


def iter_live_frames(camera_index: int = 0) -> Generator[bytes, None, None]:
    """
    Stream JPEG frames from Raspberry Pi camera tools for MJPEG display.
    """

    command_name = _resolve_rpicam_video_command()
    if command_name is None:
        raise CameraCaptureError(
            "No Raspberry Pi video streaming command was found. "
            "Expected one of: rpicam-vid, libcamera-vid."
        )

    command = [
        command_name,
        "--nopreview",
        "--codec",
        "mjpeg",
        "--inline",
        "--timeout",
        "0",
        "--width",
        str(CAMERA.stream_width),
        "--height",
        str(CAMERA.stream_height),
        "--framerate",
        str(CAMERA.stream_framerate),
        "--camera",
        str(camera_index),
        "-o",
        "-",
    ]

    LOGGER.info("Starting live stream via %s", command_name)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    )

    buffer = bytearray()
    try:
        if process.stdout is None:
            raise CameraCaptureError("Live stream process did not provide stdout")

        while True:
            chunk = process.stdout.read(4096)
            if not chunk:
                break
            buffer.extend(chunk)

            while True:
                start = buffer.find(b"\xff\xd8")
                end = buffer.find(b"\xff\xd9", start + 2 if start != -1 else 0)
                if start == -1 or end == -1:
                    if start > 0:
                        del buffer[:start]
                    break

                frame = bytes(buffer[start:end + 2])
                del buffer[:end + 2]
                yield frame
    finally:
        process.terminate()
        process.wait(timeout=2)


def _resolve_rpicam_video_command() -> Optional[str]:
    """
    Return the first available Raspberry Pi camera video command.
    """

    for candidate in CAMERA.rpicam_video_command_candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


def load_image(image_path: str) -> np.ndarray:
    """
    Load an image from disk and validate that it exists.
    """

    path = Path(image_path)
    if not path.exists():
        raise ImageLoadError(f"Image file does not exist: {path}")

    image = cv2.imread(str(path))
    if image is None:
        raise ImageLoadError(f"Failed to load image from disk: {path}")

    LOGGER.info("Loaded test image from %s with shape %s", path, image.shape)
    return image


def display_image(window_name: str, image: np.ndarray, delay_ms: int = 0) -> Optional[int]:
    """
    Display an image for debugging when requested by the user.

    This function is intentionally separate from the main workflow so display
    behavior stays optional.
    """

    cv2.imshow(window_name, image)
    key = cv2.waitKey(delay_ms)
    return key


def close_display_windows() -> None:
    """
    Close any OpenCV windows opened during the session.
    """

    cv2.destroyAllWindows()
