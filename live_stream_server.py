"""
Main-project WebSocket MJPEG stream server for Raspberry Pi camera testing.

This module belongs to the main project codebase but remains opt-in so the
existing analysis workflow stays unchanged unless this server is started
manually.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from camera_capture import CameraCaptureError, iter_live_frames
from hardware_control import HardwareController

try:
    from websockets.asyncio.server import ServerConnection, serve
    from websockets.exceptions import ConnectionClosed
except ImportError as exc:  # pragma: no cover - depends on local environment.
    raise SystemExit(
        "The 'websockets' package is required for live_stream_server.py. "
        "Install it with: pip install -r requirements.txt"
    ) from exc


LOGGER = logging.getLogger(__name__)
REPO_ROOT = Path(__file__).resolve().parent
MAIN_SCRIPT_PATH = REPO_ROOT / "main.py"
STREAM_PATH = "/stream"
SESSION_DURATION_SECONDS = 600
TEMPERATURE_INTERVAL_SECONDS = 1.0
TARGET_TEMPERATURE_C = 105.0
HEATER_ON_BELOW_C = 104.0
HEATER_OFF_ABOVE_C = 106.0


@dataclass
class ActiveSession:
    """
    One device-active session bound to one WebSocket client.
    """

    websocket: ServerConnection
    camera_index: int
    device_stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    device_end_reason: str = "running"
    test_stop_event: Optional[asyncio.Event] = None
    test_end_reason: Optional[str] = None
    error_message: Optional[str] = None
    heater_enabled: bool = False
    target_reached: bool = False
    test_active: bool = False
    analysis_in_progress: bool = False
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    device_task: Optional[asyncio.Task] = None
    test_task: Optional[asyncio.Task] = None


class SessionCoordinator:
    """
    Manage a single device-active session with guaranteed heater shutdown.
    """

    def __init__(self, camera_index: int) -> None:
        self._camera_index = camera_index
        self._hardware = HardwareController()
        self._lock = asyncio.Lock()
        self._active_session: Optional[ActiveSession] = None

    async def start_device(self, websocket: ServerConnection) -> None:
        async with self._lock:
            if self._active_session is not None:
                if self._active_session.websocket is websocket:
                    await self._send_json(
                        websocket,
                        {
                            "type": "device_already_active",
                            "message": "Device is already active.",
                        },
                    )
                else:
                    await self._send_json(
                        websocket,
                        {
                            "type": "session_busy",
                            "message": "Another device session is already active.",
                        },
                    )
                return

            session = ActiveSession(websocket=websocket, camera_index=self._camera_index)
            self._active_session = session
            session.device_task = asyncio.create_task(self._run_device_session(session))
            LOGGER.info("Device started for client; telemetry launching in ready state.")

    async def run_test(self, websocket: ServerConnection) -> None:
        async with self._lock:
            session = self._active_session
            if session is None or session.websocket is not websocket:
                await self._send_json(
                    websocket,
                    {
                        "type": "invalid_action",
                        "message": "Start Device before running a test.",
                    },
                )
                return
            if session.device_stop_event.is_set():
                await self._send_json(
                    websocket,
                    {
                        "type": "invalid_action",
                        "message": "Device is stopping; wait for cleanup to finish.",
                    },
                )
                return
            if session.test_task is not None and not session.test_task.done():
                await self._send_json(
                    websocket,
                    {
                        "type": "test_already_running",
                        "message": "A test is already running.",
                    },
                )
                return
            if session.analysis_in_progress:
                await self._send_json(
                    websocket,
                    {
                        "type": "invalid_action",
                        "message": "Analysis is still running from the previous test.",
                    },
                )
                return

            session.test_stop_event = asyncio.Event()
            session.test_end_reason = None
            session.test_task = asyncio.create_task(
                self._run_test(session, session.test_stop_event)
            )
            LOGGER.info("Run Test accepted; starting 60-second timed test.")

    async def start_session(self, websocket: ServerConnection) -> None:
        await self.start_device(websocket)
        await self.run_test(websocket)

    async def stop_device(self, websocket: ServerConnection, reason: str) -> None:
        async with self._lock:
            session = self._active_session
            if session is None or session.websocket is not websocket:
                return
            self._request_device_stop(session, reason)

    async def stop_session(self, websocket: ServerConnection, reason: str) -> None:
        await self.stop_device(websocket, reason)

    def _request_device_stop(
        self,
        session: ActiveSession,
        reason: str,
        error_message: Optional[str] = None,
    ) -> None:
        if session.device_stop_event.is_set():
            return

        session.device_end_reason = reason
        if error_message and session.error_message is None:
            session.error_message = error_message

        if session.test_stop_event is not None and not session.test_stop_event.is_set():
            if session.test_end_reason is None:
                session.test_end_reason = reason
            session.test_stop_event.set()

        session.device_stop_event.set()

    async def _run_device_session(self, session: ActiveSession) -> None:
        temperature_task: Optional[asyncio.Task] = None
        try:
            await self._send_json(
                session.websocket,
                {
                    "type": "device_started",
                    "targetCelsius": TARGET_TEMPERATURE_C,
                },
                session=session,
            )
            LOGGER.info("Device ready; temperature telemetry starting.")

            temperature_task = asyncio.create_task(self._run_temperature_control(session))
            await session.device_stop_event.wait()
        except Exception as exc:
            self._request_device_stop(
                session,
                reason="error",
                error_message=str(exc),
            )
            LOGGER.exception("Device session failed")
        finally:
            if session.test_task is not None:
                session.test_task.cancel()
            for task in (session.test_task, temperature_task):
                if task is None:
                    continue
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

            await asyncio.to_thread(self._hardware.turn_heater_off)
            session.heater_enabled = False
            await self._send_json(
                session.websocket,
                {
                    "type": "heater_state",
                    "enabled": False,
                },
                session=session,
            )
            LOGGER.info("Stop device triggered; heater shut down and cleanup running.")

            if session.error_message:
                await self._send_json(
                    session.websocket,
                    {
                        "type": "device_error",
                        "message": session.error_message,
                    },
                    session=session,
                )

            await self._send_json(
                session.websocket,
                {
                    "type": "device_stopped",
                    "reason": session.device_end_reason,
                },
                session=session,
            )
            LOGGER.info("Device cleanup completed with reason=%s", session.device_end_reason)
            async with self._lock:
                if self._active_session is session:
                    self._active_session = None

    async def _run_temperature_control(self, session: ActiveSession) -> None:
        LOGGER.info("Temperature telemetry started; device ready with target %.1f C", TARGET_TEMPERATURE_C)
        try:
            while not session.device_stop_event.is_set():
                temperature = await asyncio.to_thread(self._hardware.read_temperature_celsius)
                await self._send_json(
                    session.websocket,
                    {
                        "type": "temperature",
                        "celsius": temperature,
                    },
                    session=session,
                )

                if temperature is not None and session.test_active:
                    if temperature >= TARGET_TEMPERATURE_C and not session.target_reached:
                        session.target_reached = True
                        LOGGER.info("Target %.1f C reached at %.1f C", TARGET_TEMPERATURE_C, temperature)
                        await self._send_json(
                            session.websocket,
                            {
                                "type": "target_reached",
                                "targetCelsius": TARGET_TEMPERATURE_C,
                                "celsius": temperature,
                            },
                            session=session,
                        )

                    if temperature >= HEATER_OFF_ABOVE_C and session.heater_enabled:
                        if await asyncio.to_thread(self._hardware.turn_heater_off):
                            session.heater_enabled = False
                            LOGGER.info("Holding temperature: heater OFF at %.1f C", temperature)
                            await self._send_json(
                                session.websocket,
                                {
                                    "type": "heater_state",
                                    "enabled": False,
                                },
                                session=session,
                            )
                    elif temperature <= HEATER_ON_BELOW_C and not session.heater_enabled:
                        if await asyncio.to_thread(self._hardware.turn_heater_on):
                            session.heater_enabled = True
                            LOGGER.info("Heating temperature: heater ON at %.1f C", temperature)
                            await self._send_json(
                                session.websocket,
                                {
                                    "type": "heater_state",
                                    "enabled": True,
                                },
                                session=session,
                            )

                await asyncio.sleep(TEMPERATURE_INTERVAL_SECONDS)
        except ConnectionClosed:
            self._request_device_stop(session, reason="client_disconnect")
        except Exception as exc:
            LOGGER.exception("Temperature control task failed")
            self._request_device_stop(session, reason="error", error_message=str(exc))

    async def _run_test(self, session: ActiveSession, test_stop_event: asyncio.Event) -> None:
        video_task: Optional[asyncio.Task] = None
        completed_normally = False
        try:
            session.test_active = True
            session.target_reached = False
            session.heater_enabled = await asyncio.to_thread(self._hardware.turn_heater_on)
            if not session.heater_enabled:
                session.test_active = False
                self._request_device_stop(
                    session,
                    reason="error",
                    error_message="Failed to turn on the heating pad.",
                )
                return

            await self._send_json(
                session.websocket,
                {
                    "type": "heater_state",
                    "enabled": True,
                },
                session=session,
            )
            await self._send_json(
                session.websocket,
                {
                    "type": "test_started",
                    "durationSeconds": SESSION_DURATION_SECONDS,
                },
                session=session,
            )
            LOGGER.info("Run test triggered; heater and video started for %s seconds.", SESSION_DURATION_SECONDS)
            video_task = asyncio.create_task(self._stream_video(session, test_stop_event))

            try:
                await asyncio.wait_for(test_stop_event.wait(), timeout=SESSION_DURATION_SECONDS)
            except asyncio.TimeoutError:
                session.test_end_reason = "completed"
                completed_normally = True
                test_stop_event.set()
        except ConnectionClosed:
            self._request_device_stop(session, reason="client_disconnect")
        except Exception as exc:
            LOGGER.exception("Test task failed")
            self._request_device_stop(session, reason="error", error_message=str(exc))
        finally:
            if video_task is not None:
                video_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await video_task

            if session.heater_enabled:
                await asyncio.to_thread(self._hardware.turn_heater_off)
                session.heater_enabled = False
                await self._send_json(
                    session.websocket,
                    {
                        "type": "heater_state",
                        "enabled": False,
                    },
                    session=session,
                )
            session.test_active = False

            if session.test_stop_event is test_stop_event:
                session.test_stop_event = None
            session.test_task = None

            if completed_normally:
                LOGGER.info("Test 60-second completion reached; starting result/analysis handoff.")
                await self._send_json(
                    session.websocket,
                    {
                        "type": "test_completed",
                        "reason": "completed",
                    },
                    session=session,
                )
                session.analysis_in_progress = True
                try:
                    await self._launch_analysis_subprocess(session.camera_index)
                finally:
                    session.analysis_in_progress = False
            else:
                reason = session.test_end_reason or "stopped"
                LOGGER.info("Test ended early with reason=%s", reason)
                await self._send_json(
                    session.websocket,
                    {
                        "type": "test_stopped",
                        "reason": reason,
                    },
                    session=session,
                )

    async def _stream_video(
        self,
        session: ActiveSession,
        test_stop_event: asyncio.Event,
    ) -> None:
        try:
            for frame in iter_live_frames(camera_index=session.camera_index):
                if session.device_stop_event.is_set() or test_stop_event.is_set():
                    break
                await self._send_bytes(session, frame)
                await asyncio.sleep(0)
        except CameraCaptureError as exc:
            self._request_device_stop(session, reason="error", error_message=str(exc))
        except ConnectionClosed:
            self._request_device_stop(session, reason="client_disconnect")
        except Exception as exc:
            LOGGER.exception("Video streaming task failed")
            self._request_device_stop(session, reason="error", error_message=str(exc))

    async def _send_json(
        self,
        websocket: ServerConnection,
        payload: dict,
        session: Optional[ActiveSession] = None,
    ) -> None:
        message = json.dumps(payload)
        if session is None:
            with contextlib.suppress(ConnectionClosed):
                await websocket.send(message)
            return

        await self._send_text(session, message)

    async def _send_text(self, session: ActiveSession, message: str) -> None:
        try:
            async with session.send_lock:
                await session.websocket.send(message)
        except ConnectionClosed:
            pass
        except Exception:
            LOGGER.exception("Failed to send text update to client")

    async def _send_bytes(self, session: ActiveSession, payload: bytes) -> None:
        try:
            async with session.send_lock:
                await session.websocket.send(payload)
        except ConnectionClosed:
            pass
        except Exception:
            LOGGER.exception("Failed to send video frame to client")

    async def _launch_analysis_subprocess(self, camera_index: int) -> None:
        command = [
            sys.executable,
            str(MAIN_SCRIPT_PATH),
            "--mode",
            "live",
            "--camera-index",
            str(camera_index),
        ]
        LOGGER.info("Launching analysis command: %s", " ".join(command))

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(REPO_ROOT),
            )
        except Exception:
            LOGGER.exception("Failed to launch main.py --mode live")
            return

        LOGGER.info("Started analysis subprocess with pid %s", process.pid)
        return_code = await process.wait()
        if return_code == 0:
            LOGGER.info("Analysis subprocess completed successfully")
        else:
            LOGGER.error("Analysis subprocess exited with code %s", return_code)

async def client_handler(websocket: ServerConnection, coordinator: SessionCoordinator) -> None:
    """
    Accept control messages for one client and coordinate a device-active session.
    """

    request = getattr(websocket, "request", None)
    request_path = getattr(request, "path", "")
    if request_path != STREAM_PATH:
        LOGGER.warning("Rejected WebSocket connection on unexpected path: %s", request_path)
        await websocket.close(code=1008, reason=f"Use {STREAM_PATH}")
        return

    try:
        await websocket.send('{"type":"info","message":"connected"}')
        async for raw_message in websocket:
            if not isinstance(raw_message, str):
                continue

            try:
                message = json.loads(raw_message)
            except json.JSONDecodeError:
                await websocket.send(
                    json.dumps(
                        {
                            "type": "session_error",
                            "message": "Invalid control payload.",
                        }
                    )
                )
                continue

            action = message.get("action")
            if action == "start_device":
                await coordinator.start_device(websocket)
            elif action == "run_test":
                await coordinator.run_test(websocket)
            elif action == "stop_device":
                await coordinator.stop_device(websocket, reason="manual_stop")
            elif action == "start_session":
                await coordinator.start_session(websocket)
            elif action == "stop_session":
                await coordinator.stop_session(websocket, reason="manual_stop")
    finally:
        await coordinator.stop_device(websocket, reason="client_disconnect")


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Raspberry Pi WebSocket live stream server")
    parser.add_argument("--host", default="0.0.0.0", help="Host interface to bind")
    parser.add_argument("--port", type=int, default=8765, help="WebSocket port")
    parser.add_argument("--camera-index", type=int, default=0, help="Camera index passed to rpicam")
    return parser


async def run_server(host: str, port: int, camera_index: int) -> None:
    coordinator = SessionCoordinator(camera_index=camera_index)

    async with serve(
        lambda ws: client_handler(ws, coordinator),
        host,
        port,
        max_size=None,
    ):
        LOGGER.info("Live stream server listening on ws://%s:%s%s", host, port, STREAM_PATH)
        await asyncio.Future()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    args = build_argument_parser().parse_args()

    try:
        asyncio.run(run_server(args.host, args.port, args.camera_index))
    except KeyboardInterrupt:
        LOGGER.info("Stopping live stream server")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
