"""
Live annotated frame processing with default WebSocket streaming and optional
HTTP fallback compatibility.
"""

from __future__ import annotations

import logging
import os
import json
import queue
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Union

import cv2
import numpy as np

from camera_capture import CameraCaptureError, iter_live_frames
from config import PLATE_GEOMETRY, VIDEO_STREAM
from main import preprocess_image
from plate_analyzer import AnalysisResult, PlateAnalyzer, SlabDetectionError
from project_env import load_local_env
from stream_sender import HttpFrameStreamSender

try:
    from websockets.exceptions import WebSocketException
    from websockets.sync.client import connect as websocket_connect
except ImportError:  # pragma: no cover - depends on deployment environment.
    WebSocketException = Exception
    websocket_connect = None


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
    Analyze live frames continuously and stream annotated output with
    WebSocket-first transport selection.
    """

    def __init__(
        self,
        camera_index: int = 0,
        on_analysis_success: Optional[Callable[[LiveAnalysisSnapshot], None]] = None,
        stream_endpoint: Optional[str] = None,
        websocket_endpoint: Optional[str] = None,
        session_id: Optional[str] = None,
        plate_id: Optional[str] = None,
    ) -> None:
        self._camera_index = camera_index
        self._on_analysis_success = on_analysis_success
        self._stream_endpoint = stream_endpoint.strip() if stream_endpoint else None
        self._websocket_endpoint = websocket_endpoint.strip() if websocket_endpoint else None
        self._session_id = session_id or f"session_{uuid.uuid4().hex}"
        self._plate_id = plate_id
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._error: Optional[BaseException] = None
        self._websocket_sender: Optional[WebSocketSessionSender] = None
        self._frame_sequence = 0

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

    @property
    def session_id(self) -> str:
        """
        Return the active session identifier carried on WebSocket messages.
        """

        return self._session_id

    def publish_session_event(
        self,
        event_type: str,
        **payload: Any,
    ) -> None:
        """
        Send one JSON session lifecycle event over the WebSocket channel.
        """

        sender = self._websocket_sender
        if sender is None:
            return

        sender.submit_json(
            self._build_message_header(
                message_type=event_type,
                **payload,
            )
        )

    def publish_session_status(
        self,
        session_state: str,
        remaining_seconds: Optional[int],
        ir_temperature_c: Optional[float],
        abort_requested: bool,
    ) -> None:
        """
        Send one lightweight session telemetry update.
        """

        sender = self._websocket_sender
        if sender is None:
            return

        sender.submit_json(
            self._build_message_header(
                message_type="session_status",
                session_state=session_state,
                remaining_seconds=remaining_seconds,
                ir_temperature_c=ir_temperature_c,
                abort_requested=abort_requested,
            )
        )

    def publish_final_result(self, analysis_run: Dict[str, Any]) -> None:
        """
        Send the final 96-value binary result payload over the WebSocket channel.
        """

        sender = self._websocket_sender
        if sender is None:
            return

        payload = analysis_run.get("payload") or {}
        binary_values = payload.get("binaryData")
        if not isinstance(binary_values, list):
            LOGGER.warning("Skipping final-result WebSocket message because binaryData is unavailable")
            return

        try:
            binary_payload = bytes(int(value) for value in binary_values)
        except (TypeError, ValueError) as exc:
            LOGGER.warning("Skipping final-result WebSocket message because binaryData is invalid: %s", exc)
            return

        sender.submit_binary(
            self._build_message_header(
                message_type="final_result",
                plate_id=payload.get("plateId"),
                run_id=analysis_run.get("run_id"),
                value_count=len(binary_values),
                encoding="uint8-array",
            ),
            binary_payload,
        )

    def _run(self) -> None:
        """
        Process live camera frames until stopped.
        """

        load_local_env()
        websocket_endpoint = self._websocket_endpoint
        if websocket_endpoint is None:
            websocket_endpoint = os.getenv(VIDEO_STREAM.websocket_endpoint_env_var, "").strip()
        endpoint = self._stream_endpoint
        if endpoint is None:
            endpoint = os.getenv(VIDEO_STREAM.endpoint_env_var, "").strip()
        target_fps = max(1, _resolve_int_env("STREAM_FPS", VIDEO_STREAM.target_fps))
        sender: Optional[HttpFrameStreamSender] = None
        websocket_sender: Optional[WebSocketSessionSender] = None

        websocket_stream_enabled = False
        if websocket_endpoint:
            websocket_sender = WebSocketSessionSender(endpoint=websocket_endpoint)
            websocket_stream_enabled = websocket_sender.start()
            if websocket_stream_enabled:
                self._websocket_sender = websocket_sender
                self.publish_session_event("session_started")
                LOGGER.info("WebSocket streaming selected as the default live-session transport")
                if endpoint:
                    LOGGER.info(
                        "HTTP stream endpoint is configured but will remain idle because WebSocket streaming is active"
                    )

        if not websocket_stream_enabled:
            LOGGER.info(
                "WebSocket streaming unavailable because %s is not configured; falling back to HTTP if available",
                VIDEO_STREAM.websocket_endpoint_env_var,
            )
            if endpoint:
                sender = HttpFrameStreamSender(
                    endpoint=endpoint,
                    jpeg_quality=_resolve_int_env("STREAM_JPEG_QUALITY", VIDEO_STREAM.jpeg_quality),
                    reconnect_delay_seconds=_resolve_float_env(
                        "STREAM_RECONNECT_DELAY",
                        VIDEO_STREAM.reconnect_delay_seconds,
                    ),
                )
                sender.start()
                LOGGER.info("HTTP streaming fallback enabled")
            else:
                LOGGER.info("HTTP fallback streaming disabled because %s is not configured", VIDEO_STREAM.endpoint_env_var)

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
                    if websocket_sender is not None:
                        self._frame_sequence += 1
                        websocket_sender.submit_binary(
                            self._build_message_header(
                                message_type="annotated_frame",
                                frame_sequence=self._frame_sequence,
                                frame_format="jpeg",
                                frame_width=int(snapshot.analysis_result.artifacts.annotated_result.shape[1]),
                                frame_height=int(snapshot.analysis_result.artifacts.annotated_result.shape[0]),
                            ),
                            _encode_frame_as_jpeg(
                                snapshot.analysis_result.artifacts.annotated_result,
                                jpeg_quality=_resolve_int_env("STREAM_JPEG_QUALITY", VIDEO_STREAM.jpeg_quality),
                            ),
                        )
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
            if websocket_sender is not None and websocket_stream_enabled:
                self.publish_session_event("session_stream_stopping")
                websocket_sender.stop()
                self._websocket_sender = None
            if sender is not None:
                sender.stop()

    def _build_message_header(
        self,
        message_type: str,
        **payload: Any,
    ) -> Dict[str, Any]:
        """
        Build the shared message metadata for WebSocket consumers.
        """

        header: Dict[str, Any] = {
            "type": message_type,
            "session_id": self._session_id,
            "timestamp": _utc_now_iso(),
        }
        if self._plate_id:
            header["plate_id"] = self._plate_id
        header.update(payload)
        return header


class WebSocketSessionSender:
    """
    Push session-aware JSON and binary messages to one backend WebSocket.
    """

    def __init__(self, endpoint: str) -> None:
        self._endpoint = endpoint
        self._enabled = websocket_connect is not None
        self._queue: "queue.Queue[Optional[Union[str, bytes]]]" = queue.Queue(maxsize=VIDEO_STREAM.queue_size)
        self._stop_event = threading.Event()
        self._worker = threading.Thread(target=self._run, name="ws-session-stream", daemon=True)
        self._successful_connection_logged = False

    def start(self) -> bool:
        """
        Start the background WebSocket sender.
        """

        if websocket_connect is None:
            LOGGER.warning("websockets is not installed; WebSocket session streaming is disabled")
            return False

        LOGGER.info("WebSocket session streaming enabled: endpoint configured at %s", self._endpoint)
        self._worker.start()
        return True

    def stop(self) -> None:
        """
        Stop the background WebSocket sender.
        """

        if not self._worker.is_alive():
            return

        self._stop_event.set()
        self._offer(None)
        self._worker.join(timeout=VIDEO_STREAM.send_timeout_seconds + 1.0)

    def submit_json(self, payload: Dict[str, Any]) -> None:
        """
        Queue one JSON text message for the backend.
        """

        if not self._enabled:
            return
        self._offer(json.dumps(payload, separators=(",", ":")))

    def submit_binary(self, metadata: Dict[str, Any], payload: bytes) -> None:
        """
        Queue one binary message that carries metadata and a raw payload.
        """

        if not self._enabled:
            return
        header = json.dumps(metadata, separators=(",", ":")).encode("utf-8")
        packed = len(header).to_bytes(4, byteorder="big") + header + payload
        self._offer(packed)

    def _offer(self, payload: Optional[Union[str, bytes]]) -> None:
        """
        Enqueue the newest payload, dropping an older one if necessary.
        """

        try:
            self._queue.put_nowait(payload)
            return
        except queue.Full:
            pass

        try:
            self._queue.get_nowait()
        except queue.Empty:
            pass

        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            LOGGER.debug("Dropping WebSocket session payload because the queue is full")

    def _run(self) -> None:
        """
        Deliver queued messages until session streaming is stopped.
        """

        if websocket_connect is None:
            return

        while not self._stop_event.is_set():
            try:
                with websocket_connect(
                    self._endpoint,
                    open_timeout=VIDEO_STREAM.websocket_open_timeout_seconds,
                    close_timeout=VIDEO_STREAM.send_timeout_seconds,
                ) as websocket:
                    if not self._successful_connection_logged:
                        LOGGER.info("Connected to session WebSocket endpoint successfully")
                        self._successful_connection_logged = True

                    while not self._stop_event.is_set():
                        try:
                            payload = self._queue.get(timeout=0.5)
                        except queue.Empty:
                            continue

                        if payload is None:
                            continue

                        websocket.send(payload)
            except (OSError, TimeoutError, WebSocketException) as exc:
                LOGGER.warning(
                    "WebSocket session send failed for %s: %s. Retrying in %.1f seconds.",
                    self._endpoint,
                    exc,
                    VIDEO_STREAM.reconnect_delay_seconds,
                )
                self._successful_connection_logged = False
                time.sleep(VIDEO_STREAM.reconnect_delay_seconds)


def _encode_frame_as_jpeg(frame: np.ndarray, jpeg_quality: int) -> bytes:
    """
    Encode one frame to JPEG bytes for outbound WebSocket delivery.
    """

    success, encoded = cv2.imencode(
        ".jpg",
        frame,
        [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)],
    )
    if not success:
        raise CameraCaptureError("Failed to JPEG-encode annotated frame for WebSocket streaming")
    return encoded.tobytes()


def _utc_now_iso() -> str:
    """
    Return a UTC timestamp suitable for stream message metadata.
    """

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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
