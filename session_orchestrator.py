"""
Timed session orchestration for triggered live analysis runs.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Dict, Optional

from heating_pad_service import HeatingPadService
from ir_sensor_service import IRSensorService
from live_stream_service import LiveAnalysisSnapshot, LiveAnnotatedStreamService
from project_env import load_local_env


LOGGER = logging.getLogger(__name__)

PersistResultCallback = Callable[..., Dict[str, Any]]


class SessionOrchestrator:
    """
    Run one timed session at a time and prevent overlaps.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._session_active = False

    def run_triggered_session(
        self,
        plate_id: Optional[str],
        mongo_uri: Optional[str],
        camera_index: int,
        display: bool,
        persist_result: PersistResultCallback,
        stream_endpoint: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Run one full timed session from trigger to cleanup.
        """

        with self._lock:
            if self._session_active:
                LOGGER.warning("Trigger received while a session is already active; ignoring duplicate trigger")
                return {"status": "already_active"}
            self._session_active = True

        LOGGER.info("Trigger received")
        load_local_env()
        duration_seconds = _resolve_session_duration_seconds()
        session_deadline = time.monotonic() + duration_seconds
        latest_snapshot: Optional[LiveAnalysisSnapshot] = None
        snapshot_lock = threading.Lock()
        persisted_run: Optional[Dict[str, Any]] = None
        stream_error_logged = False

        def _store_snapshot(snapshot: LiveAnalysisSnapshot) -> None:
            nonlocal latest_snapshot
            with snapshot_lock:
                latest_snapshot = snapshot

        ir_sensor = IRSensorService()
        heating_pad = HeatingPadService(ir_sensor=ir_sensor)
        stream_service = LiveAnnotatedStreamService(
            camera_index=camera_index,
            on_analysis_success=_store_snapshot,
            stream_endpoint=stream_endpoint,
        )

        LOGGER.info("Session start")

        try:
            ir_sensor.start()
            heating_pad.start()
            stream_service.start()

            while time.monotonic() < session_deadline:
                if stream_service.error() is not None:
                    if not stream_error_logged:
                        LOGGER.warning("Live stream encountered a terminal error; session timing will continue")
                        stream_error_logged = True

                if persisted_run is None:
                    with snapshot_lock:
                        snapshot_to_persist = latest_snapshot
                    if snapshot_to_persist is not None:
                        persisted_run = persist_result(
                            snapshot=snapshot_to_persist,
                            plate_id=plate_id,
                            mongo_uri=mongo_uri,
                            display=display,
                        )

                time.sleep(min(1.0, max(0.0, session_deadline - time.monotonic())))

            if persisted_run is None:
                with snapshot_lock:
                    snapshot_to_persist = latest_snapshot
                if snapshot_to_persist is not None:
                    persisted_run = persist_result(
                        snapshot=snapshot_to_persist,
                        plate_id=plate_id,
                        mongo_uri=mongo_uri,
                        display=display,
                    )
                else:
                    LOGGER.warning("Session completed without a successful analyzed frame to persist")

            return {
                "status": "completed",
                "session_duration_seconds": duration_seconds,
                "analysis_run": persisted_run,
            }
        finally:
            stream_service.stop()
            LOGGER.info("Live stream stopped")
            heating_pad.stop()
            ir_sensor.stop()
            LOGGER.info("Cleanup completed")
            LOGGER.info("Session end")
            with self._lock:
                self._session_active = False

    def is_session_active(self) -> bool:
        """
        Return whether a timed session is currently active.
        """

        with self._lock:
            return self._session_active


def _resolve_session_duration_seconds(default: int = 720) -> int:
    """
    Resolve the timed session length from the environment.
    """

    raw_value = os.getenv("SESSION_DURATION_SECONDS")
    if raw_value is None or not raw_value.strip():
        return default

    try:
        return max(1, int(raw_value))
    except ValueError:
        LOGGER.warning("Invalid integer for SESSION_DURATION_SECONDS=%r; using %s", raw_value, default)
        return default


SESSION_ORCHESTRATOR = SessionOrchestrator()
