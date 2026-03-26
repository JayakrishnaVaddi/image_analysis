"""
Live annotated frame processing and optional HTTP streaming.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import cv2
import numpy as np

from camera_capture import CameraCaptureError, iter_live_frames
from config import PLATE_GEOMETRY, VIDEO_STREAM
from main import preprocess_image
from plate_analyzer import AnalysisResult, PlateAnalyzer, SlabDetectionError
from project_env import load_local_env
from stream_sender import HttpFrameStreamSender


LOGGER = logging.getLogger(__name__)


@dataclass
class LiveAnalysisSnapshot:
    """
    One successful analyzed live frame.
    """

    source_frame: np.ndarray
    analysis_input: np.ndarray
    analysis_result: AnalysisResult


def decode_live_frame(frame_bytes: bytes) -> np.ndarray:
    """
    Decode a JPEG frame from the live camera generator into a BGR image.
    """

    image_buffer = np.frombuffer(frame_bytes, dtype=np.uint8)
    frame = cv2.imdecode(image_buffer, cv2.IMREAD_COLOR)
    if frame is None or frame.size == 0:
        raise CameraCaptureError("Failed to decode a live frame from the camera stream")
    return frame


def analyze_live_frame(analyzer: PlateAnalyzer, frame: np.ndarray) -> LiveAnalysisSnapshot:
    """
    Reuse the existing analysis pipeline and keep the annotated frame at the
    same size as the normal annotated result artifact.
    """

    analysis_input = preprocess_image(frame)
    analysis_result = analyzer.analyze(analysis_input)

    expected_size = (PLATE_GEOMETRY.warp_width, PLATE_GEOMETRY.warp_height)
    annotated = analysis_result.artifacts.annotated_result
    actual_size = (annotated.shape[1], annotated.shape[0])
    if actual_size != expected_size:
        analysis_result.artifacts.annotated_result = cv2.resize(
            annotated,
            expected_size,
            interpolation=cv2.INTER_LINEAR,
        )

    return LiveAnalysisSnapshot(
        source_frame=frame,
        analysis_input=analysis_input,
        analysis_result=analysis_result,
    )


class LiveAnnotatedStreamService:
    """
    Analyze live frames continuously and optionally stream annotated output.
    """

    def __init__(
        self,
        camera_index: int = 0,
        on_analysis_success: Optional[Callable[[LiveAnalysisSnapshot], None]] = None,
        stream_endpoint: Optional[str] = None,
    ) -> None:
        self._camera_index = camera_index
        self._on_analysis_success = on_analysis_success
        self._stream_endpoint = stream_endpoint.strip() if stream_endpoint else None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._error: Optional[BaseException] = None

    def start(self) -> None:
        """
        Start the background live annotation loop.
        """

        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._error = None
        self._thread = threading.Thread(
            target=self._run,
            name="live-annotated-stream",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """
        Stop the background loop and wait briefly for shutdown.
        """

        thread = self._thread
        if thread is None:
            return

        self._stop_event.set()
        thread.join(timeout=VIDEO_STREAM.send_timeout_seconds + 5.0)
        self._thread = None

    def error(self) -> Optional[BaseException]:
        """
        Return any terminal error raised while opening the live stream.
        """

        return self._error

    def _run(self) -> None:
        """
        Process live camera frames until stopped.
        """

        load_local_env()
        endpoint = self._stream_endpoint
        if endpoint is None:
            endpoint = os.getenv(VIDEO_STREAM.endpoint_env_var, "").strip()
        target_fps = max(1, _resolve_int_env("STREAM_FPS", VIDEO_STREAM.target_fps))
        sender: Optional[HttpFrameStreamSender] = None

        if endpoint:
            LOGGER.info("Streaming enabled")
            sender = HttpFrameStreamSender(
                endpoint=endpoint,
                jpeg_quality=_resolve_int_env("STREAM_JPEG_QUALITY", VIDEO_STREAM.jpeg_quality),
                reconnect_delay_seconds=_resolve_float_env(
                    "STREAM_RECONNECT_DELAY",
                    VIDEO_STREAM.reconnect_delay_seconds,
                ),
            )
            sender.start()
        else:
            LOGGER.info("Streaming disabled because %s is not configured", VIDEO_STREAM.endpoint_env_var)

        analyzer = PlateAnalyzer()
        frame_interval = 1.0 / target_fps
        next_frame_at = 0.0

        try:
            for frame_bytes in iter_live_frames(camera_index=self._camera_index):
                if self._stop_event.is_set():
                    break

                now = time.monotonic()
                if now < next_frame_at:
                    continue
                next_frame_at = now + frame_interval

                try:
                    frame = decode_live_frame(frame_bytes)
                    snapshot = analyze_live_frame(analyzer, frame)
                    if self._on_analysis_success is not None:
                        self._on_analysis_success(snapshot)
                    if sender is not None:
                        sender.submit_frame(snapshot.analysis_result.artifacts.annotated_result)
                except SlabDetectionError as exc:
                    LOGGER.warning("Live annotation skipped because slab detection failed: %s", exc)
                except CameraCaptureError as exc:
                    LOGGER.warning("Live annotation skipped because frame decoding failed: %s", exc)
                except Exception as exc:  # pragma: no cover - defensive runtime logging.
                    LOGGER.exception("Unexpected live streaming error: %s", exc)
        except Exception as exc:  # pragma: no cover - defensive runtime logging.
            self._error = exc
            LOGGER.exception("Live stream terminated unexpectedly: %s", exc)
        finally:
            if sender is not None:
                sender.stop()


def _resolve_int_env(name: str, default: int) -> int:
    """
    Parse an integer setting from the environment with a safe fallback.
    """

    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default

    try:
        return int(raw_value)
    except ValueError:
        LOGGER.warning("Invalid integer for %s=%r; using %s", name, raw_value, default)
        return default


def _resolve_float_env(name: str, default: float) -> float:
    """
    Parse a float setting from the environment with a safe fallback.
    """

    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default

    try:
        return float(raw_value)
    except ValueError:
        LOGGER.warning("Invalid float for %s=%r; using %s", name, raw_value, default)
        return default
