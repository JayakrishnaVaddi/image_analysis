"""
Thin command adapter for the Raspberry Pi TCP socket server.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional, Sequence

from camera_capture import capture_frame
from live_stream_service import analyze_live_frame
from main import persist_live_analysis_snapshot
from plate_analyzer import PlateAnalyzer
from session_orchestrator import SESSION_ORCHESTRATOR


LOGGER = logging.getLogger(__name__)

_COLOR_RESPONSE_NAMES = {
    "light_pink": "light pink",
    "red": "red",
    "yellow": "yellow",
    None: "unknown",
}


class CommandHandler:
    """
    Route socket requests into the existing Raspberry Pi analysis workflow.
    """

    def __init__(self, camera_index: int = 0) -> None:
        self._camera_index = camera_index
        self._analysis_lock = threading.Lock()
        self._analyzer = PlateAnalyzer()

    def handle_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validate and dispatch one decoded JSON request.
        """

        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object")

        action = payload.get("action")
        if action is not None:
            if not isinstance(action, str) or not action.strip():
                raise ValueError("'action' must be a non-empty string when provided")
            normalized_action = action.strip().lower()
            if normalized_action == "health":
                return self._handle_health()
            if normalized_action == "status":
                return self._handle_status()
            if normalized_action == "start_test":
                return self._handle_start_test(payload)
            if normalized_action == "abort_test":
                return self._handle_abort_test()
            if normalized_action == "detect_colors":
                return self._handle_detect_colors(payload)
            raise ValueError(f"Unsupported action {action!r}")

        if "wells" in payload:
            return self._handle_detect_colors(payload)

        raise ValueError("Unsupported request. Expected action 'health' or 'start_test', or a JSON object with a 'wells' field.")

    def _handle_health(self) -> Dict[str, Any]:
        """
        Return a lightweight server status response without side effects.
        """

        return {
            "status": "success",
            "server": "raspberry-pi-image-analysis",
            "session_active": SESSION_ORCHESTRATOR.is_session_active(),
            "supported_actions": ["health", "status", "start_test", "abort_test", "detect_colors"],
        }

    def _handle_status(self) -> Dict[str, Any]:
        """
        Return live session status, remaining time, and latest IR temperature.
        """

        snapshot = SESSION_ORCHESTRATOR.status_snapshot()
        return {
            "status": "success",
            "server": "raspberry-pi-image-analysis",
            **snapshot,
        }

    def _handle_start_test(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Run the existing full timed session workflow and return its final result.
        """

        plate_id = self._optional_string(payload, "plateId")
        mongo_uri = self._optional_string(payload, "mongoUri")
        stream_endpoint = self._optional_string(payload, "streamEndpoint")
        websocket_endpoint = self._optional_string(payload, "websocketEndpoint")
        camera_index = self._optional_int(payload, "cameraIndex", self._camera_index)
        display = self._optional_bool(payload, "display", False)

        LOGGER.info("Running start_test request")
        session_result = SESSION_ORCHESTRATOR.run_triggered_session(
            plate_id=plate_id,
            mongo_uri=mongo_uri,
            camera_index=camera_index,
            display=display,
            persist_result=persist_live_analysis_snapshot,
            stream_endpoint=stream_endpoint,
            websocket_endpoint=websocket_endpoint,
        )

        if session_result.get("status") == "already_active":
            return {
                "status": "busy",
                "message": "A timed session is already active",
            }
        if session_result.get("status") == "aborted":
            return {
                "status": "aborted",
                "message": session_result.get("message", "Timed session aborted"),
                "session": session_result,
            }

        return {
            "status": "success",
            "message": "Timed session completed",
            "session": session_result,
        }

    def _handle_abort_test(self) -> Dict[str, Any]:
        """
        Request early shutdown of the active timed session.
        """

        if not SESSION_ORCHESTRATOR.abort_session():
            return {
                "status": "idle",
                "message": "No active timed session to abort",
            }

        return {
            "status": "success",
            "message": "Abort requested; timed session cleanup is in progress",
        }

    def _handle_detect_colors(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Capture and analyze one frame, then map the requested well labels to
        the existing analysis output.
        """

        requested_wells = payload.get("wells")
        if not isinstance(requested_wells, list) or not requested_wells:
            raise ValueError("'wells' must be a non-empty JSON array")

        labels = [self._normalize_request_label(item) for item in requested_wells]
        if len(labels) > 96:
            raise ValueError("A maximum of 96 well labels can be requested at once")

        LOGGER.info("Running detect-colors request for %s labels", len(labels))
        with self._analysis_lock:
            frame = capture_frame(camera_index=self._camera_index)
            snapshot = analyze_live_frame(self._analyzer, frame)

        colors = self._build_color_map(labels, snapshot.analysis_result.well_colors)
        return {"status": "success", "colors": colors}

    @staticmethod
    def _normalize_request_label(item: Any) -> str:
        """
        Convert one request label into a non-empty response key.
        """

        if item is None:
            raise ValueError("Well labels cannot be null")

        if isinstance(item, (str, int)):
            label = str(item).strip()
            if label:
                return label

        raise ValueError("Each well label must be a non-empty string or integer")

    @staticmethod
    def _optional_string(payload: Dict[str, Any], key: str) -> Optional[str]:
        """
        Validate one optional string field.
        """

        value = payload.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(f"'{key}' must be a string when provided")
        stripped = value.strip()
        return stripped or None

    @staticmethod
    def _optional_int(payload: Dict[str, Any], key: str, default: int) -> int:
        """
        Validate one optional integer field.
        """

        value = payload.get(key)
        if value is None:
            return default
        if not isinstance(value, int):
            raise ValueError(f"'{key}' must be an integer when provided")
        return value

    @staticmethod
    def _optional_bool(payload: Dict[str, Any], key: str, default: bool) -> bool:
        """
        Validate one optional boolean field.
        """

        value = payload.get(key)
        if value is None:
            return default
        if not isinstance(value, bool):
            raise ValueError(f"'{key}' must be a boolean when provided")
        return value

    def _build_color_map(
        self,
        labels: Sequence[str],
        analyzed_colors: Sequence[Optional[str]],
    ) -> Dict[str, str]:
        """
        Resolve request labels against the current analysis result.

        Known well references such as `1` or `well_1` target an explicit well.
        Unknown labels fall back to the next unused analyzed well in output order,
        which keeps compatibility with clients that send placeholder names like
        `test` but still expect one detected color per requested entry.
        """

        response: Dict[str, str] = {}
        used_indexes = set()
        next_fallback_index = 0

        for label in labels:
            well_index = self._resolve_well_index(label)
            if well_index is not None:
                used_indexes.add(well_index)
            else:
                while next_fallback_index in used_indexes and next_fallback_index < len(analyzed_colors):
                    next_fallback_index += 1
                if next_fallback_index >= len(analyzed_colors):
                    raise ValueError("Not enough analyzed wells available for the requested labels")
                well_index = next_fallback_index
                used_indexes.add(well_index)
                next_fallback_index += 1

            response[label] = _COLOR_RESPONSE_NAMES.get(analyzed_colors[well_index], "unknown")

        return response

    @staticmethod
    def _resolve_well_index(label: str) -> Optional[int]:
        """
        Resolve well identifiers into zero-based well indexes when possible.
        """

        normalized = label.strip().lower()
        if not normalized:
            return None

        if normalized.isdigit():
            return _parse_one_based_well_number(int(normalized))

        for prefix in ("well_", "well-", "well "):
            if normalized.startswith(prefix):
                suffix = normalized[len(prefix):].strip()
                if suffix.isdigit():
                    return _parse_one_based_well_number(int(suffix))

        return None


def _parse_one_based_well_number(value: int) -> Optional[int]:
    """
    Convert a one-based well number to a zero-based list index.
    """

    if 1 <= value <= 96:
        return value - 1
    return None
