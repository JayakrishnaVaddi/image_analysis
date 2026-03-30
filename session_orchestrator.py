"""
Timed session orchestration for triggered live analysis runs.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
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
        self._abort_event = threading.Event()
        self._session_state = "idle"
        self._session_deadline_monotonic: Optional[float] = None
        self._active_session_id: Optional[str] = None
        self._active_ir_sensor: Optional[IRSensorService] = None
        self._active_heating_pad: Optional[HeatingPadService] = None
        self._active_stream_service: Optional[LiveAnnotatedStreamService] = None

    def run_triggered_session(
        self,
        plate_id: Optional[str],
        mongo_uri: Optional[str],
        camera_index: int,
        display: bool,
        persist_result: PersistResultCallback,
        stream_endpoint: Optional[str] = None,
        websocket_endpoint: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Run one full timed session from trigger to cleanup.
        """

        with self._lock:
            if self._session_active:
                LOGGER.warning("Trigger received while a session is already active; ignoring duplicate trigger")
                return {"status": "already_active"}
            self._session_active = True
            self._abort_event.clear()
            self._session_state = "starting"

        LOGGER.info("Trigger received")
        load_local_env()
        duration_seconds = _resolve_session_duration_seconds()
        session_id = f"session_{uuid.uuid4().hex}"
        session_deadline = time.monotonic() + duration_seconds
        latest_snapshot: Optional[LiveAnalysisSnapshot] = None
        snapshot_lock = threading.Lock()
        persisted_run: Optional[Dict[str, Any]] = None
        stream_error_logged = False
        final_result_sent = False

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
            websocket_endpoint=websocket_endpoint,
            session_id=session_id,
            plate_id=plate_id,
        )
        with self._lock:
            self._session_deadline_monotonic = session_deadline
            self._active_session_id = session_id
            self._active_ir_sensor = ir_sensor
            self._active_heating_pad = heating_pad
            self._active_stream_service = stream_service

        LOGGER.info("Session start")

        try:
            ir_sensor.start()
            heating_pad.start()
            stream_service.start()
            with self._lock:
                self._session_state = "running"
            stream_service.publish_session_status(
                session_state="running",
                remaining_seconds=duration_seconds,
                ir_temperature_c=ir_sensor.latest_temperature_c(),
                abort_requested=False,
            )

            while time.monotonic() < session_deadline:
                remaining_seconds = max(0, int(round(session_deadline - time.monotonic())))
                latest_ir_temperature_c = ir_sensor.latest_temperature_c()
                stream_service.publish_session_status(
                    session_state=self._session_state,
                    remaining_seconds=remaining_seconds,
                    ir_temperature_c=latest_ir_temperature_c,
                    abort_requested=self._abort_event.is_set(),
                )

                if self._abort_event.is_set():
                    LOGGER.warning("Abort requested; ending timed session early")
                    with self._lock:
                        self._session_state = "aborting"
                    if persisted_run is not None and not final_result_sent:
                        stream_service.publish_final_result(persisted_run)
                        final_result_sent = True
                    stream_service.publish_session_event(
                        "session_aborted",
                        remaining_seconds=remaining_seconds,
                        ir_temperature_c=latest_ir_temperature_c,
                    )
                    return {
                        "status": "aborted",
                        "session_id": session_id,
                        "message": "Timed session aborted",
                        "session_duration_seconds": duration_seconds,
                        "analysis_run": persisted_run,
                    }

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

            if persisted_run is not None and not final_result_sent:
                stream_service.publish_final_result(persisted_run)
                final_result_sent = True
            stream_service.publish_session_event(
                "session_completed",
                remaining_seconds=0,
                ir_temperature_c=ir_sensor.latest_temperature_c(),
                result_available=persisted_run is not None,
            )
            return {
                "status": "completed",
                "session_id": session_id,
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
                self._session_state = "idle"
                self._session_deadline_monotonic = None
                self._active_session_id = None
                self._active_ir_sensor = None
                self._active_heating_pad = None
                self._active_stream_service = None

    def is_session_active(self) -> bool:
        """
        Return whether a timed session is currently active.
        """

        with self._lock:
            return self._session_active

    def abort_session(self) -> bool:
        """
        Request that the active timed session stop early.
        """

        with self._lock:
            if not self._session_active:
                return False
            self._abort_event.set()
            self._session_state = "aborting"
            return True

    def status_snapshot(self) -> Dict[str, Any]:
        """
        Return the current live session status for lightweight polling.
        """

        with self._lock:
            active = self._session_active
            state = self._session_state
            deadline = self._session_deadline_monotonic
            session_id = self._active_session_id
            ir_sensor = self._active_ir_sensor

        remaining_seconds: Optional[int] = None
        if active and deadline is not None:
            remaining_seconds = max(0, int(round(deadline - time.monotonic())))

        latest_ir_temperature_c = ir_sensor.latest_temperature_c() if ir_sensor is not None else None

        return {
            "session_active": active,
            "session_id": session_id,
            "session_state": state,
            "remaining_seconds": remaining_seconds,
            "latest_ir_temperature_c": latest_ir_temperature_c,
            "abort_requested": self._abort_event.is_set() if active else False,
        }

    def shutdown_for_server_stop(self) -> Dict[str, bool]:
        """
        Best-effort server-stop cleanup to leave hardware in a safe state.
        """

        with self._lock:
            self._abort_event.set()
            self._session_state = "aborting" if self._session_active else "idle"
            stream_service = self._active_stream_service
            heating_pad = self._active_heating_pad
            ir_sensor = self._active_ir_sensor

        if stream_service is not None:
            try:
                stream_service.publish_session_event("session_server_shutdown")
                stream_service.stop()
            except Exception as exc:  # pragma: no cover - defensive runtime path.
                LOGGER.exception("Server-stop cleanup failed while stopping live stream: %s", exc)

        if heating_pad is not None:
            try:
                heating_pad.stop()
            except Exception as exc:  # pragma: no cover - defensive runtime path.
                LOGGER.exception("Server-stop cleanup failed while stopping heating pad: %s", exc)

        if ir_sensor is not None:
            try:
                ir_sensor.stop()
            except Exception as exc:  # pragma: no cover - defensive runtime path.
                LOGGER.exception("Server-stop cleanup failed while stopping IR sensor: %s", exc)

        return {
            "session_active_was_running": stream_service is not None or heating_pad is not None or ir_sensor is not None,
            "abort_requested": True,
        }


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
