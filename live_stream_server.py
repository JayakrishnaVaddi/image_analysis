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
from dataclasses import dataclass, field
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
STREAM_PATH = "/stream"
SESSION_DURATION_SECONDS = 150
TEMPERATURE_INTERVAL_SECONDS = 1.0


@dataclass
class ActiveSession:
    """
    One coordinated timed hardware session bound to one WebSocket client.
    """

    websocket: ServerConnection
    camera_index: int
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    end_reason: str = "timeout"
    error_message: Optional[str] = None


class SessionCoordinator:
    """
    Manage a single timed session with guaranteed heater shutdown.
    """

    def __init__(self, camera_index: int) -> None:
        self._camera_index = camera_index
        self._hardware = HardwareController()
        self._lock = asyncio.Lock()
        self._active_session: Optional[ActiveSession] = None

    async def start_session(self, websocket: ServerConnection) -> None:
        async with self._lock:
            if self._active_session is not None:
                await self._send_json(
                    websocket,
                    {
                        "type": "session_busy",
                        "message": "Another session is already active.",
                    },
                )
                return

            session = ActiveSession(websocket=websocket, camera_index=self._camera_index)
            self._active_session = session
            asyncio.create_task(self._run_session(session))

    async def stop_session(self, websocket: ServerConnection, reason: str) -> None:
        async with self._lock:
            if self._active_session is None:
                return
            if self._active_session.websocket is not websocket:
                return
            if self._active_session.stop_event.is_set():
                return
            self._active_session.end_reason = reason
            self._active_session.stop_event.set()

    async def _run_session(self, session: ActiveSession) -> None:
        video_task: Optional[asyncio.Task] = None
        temperature_task: Optional[asyncio.Task] = None
        heater_enabled = False

        try:
            await self._send_json(
                session.websocket,
                {
                    "type": "session_started",
                    "durationSeconds": SESSION_DURATION_SECONDS,
                },
            )

            heater_enabled = await asyncio.to_thread(self._hardware.turn_heater_on)
            if not heater_enabled:
                session.end_reason = "error"
                session.error_message = "Failed to turn on the heating pad."
                return

            await self._send_json(
                session.websocket,
                {
                    "type": "heater_state",
                    "enabled": True,
                },
            )

            video_task = asyncio.create_task(self._stream_video(session))
            temperature_task = asyncio.create_task(self._stream_temperature(session))

            try:
                await asyncio.wait_for(session.stop_event.wait(), timeout=SESSION_DURATION_SECONDS)
            except asyncio.TimeoutError:
                session.end_reason = "timeout"
        except Exception as exc:
            session.end_reason = "error"
            session.error_message = str(exc)
            LOGGER.exception("Session failed")
        finally:
            if video_task is not None:
                video_task.cancel()
            if temperature_task is not None:
                temperature_task.cancel()
            for task in (video_task, temperature_task):
                if task is None:
                    continue
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

            await asyncio.to_thread(self._hardware.turn_heater_off)
            await self._send_json(
                session.websocket,
                {
                    "type": "heater_state",
                    "enabled": False,
                },
            )

            if session.error_message:
                await self._send_json(
                    session.websocket,
                    {
                        "type": "session_error",
                        "message": session.error_message,
                    },
                )

            await self._send_json(
                session.websocket,
                {
                    "type": "session_ended",
                    "reason": session.end_reason,
                },
            )

            with contextlib.suppress(Exception):
                await session.websocket.close()

            async with self._lock:
                if self._active_session is session:
                    self._active_session = None

    async def _stream_video(self, session: ActiveSession) -> None:
        try:
            for frame in iter_live_frames(camera_index=session.camera_index):
                if session.stop_event.is_set():
                    break
                await session.websocket.send(frame)
                await asyncio.sleep(0)
        except CameraCaptureError as exc:
            session.end_reason = "error"
            session.error_message = str(exc)
            session.stop_event.set()
        except ConnectionClosed:
            session.end_reason = "client_disconnect"
            session.stop_event.set()
        except Exception as exc:
            session.end_reason = "error"
            session.error_message = str(exc)
            LOGGER.exception("Video streaming task failed")
            session.stop_event.set()

    async def _stream_temperature(self, session: ActiveSession) -> None:
        try:
            while not session.stop_event.is_set():
                temperature = await asyncio.to_thread(self._hardware.read_temperature_celsius)
                await self._send_json(
                    session.websocket,
                    {
                        "type": "temperature",
                        "celsius": temperature,
                    },
                )
                await asyncio.sleep(TEMPERATURE_INTERVAL_SECONDS)
        except ConnectionClosed:
            session.end_reason = "client_disconnect"
            session.stop_event.set()
        except Exception as exc:
            session.end_reason = "error"
            session.error_message = str(exc)
            LOGGER.exception("Temperature streaming task failed")
            session.stop_event.set()

    async def _send_json(self, websocket: ServerConnection, payload: dict) -> None:
        with contextlib.suppress(ConnectionClosed, Exception):
            await websocket.send(json.dumps(payload))


async def client_handler(websocket: ServerConnection, coordinator: SessionCoordinator) -> None:
    """
    Accept control messages for one client and coordinate a timed hardware session.
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
            if action == "start_session":
                await coordinator.start_session(websocket)
            elif action == "stop_session":
                await coordinator.stop_session(websocket, reason="manual_stop")
    finally:
        await coordinator.stop_session(websocket, reason="client_disconnect")


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
