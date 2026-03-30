"""
Small test web app for exercising the Pi socket-server session flow end to end.
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

try:
    from websockets.exceptions import WebSocketException
    from websockets.sync.server import serve as websocket_serve
except ImportError:  # pragma: no cover - depends on deployment environment.
    WebSocketException = Exception
    websocket_serve = None


LOGGER = logging.getLogger(__name__)
INDEX_HTML_PATH = Path(__file__).resolve().parent / "static" / "index.html"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 5.0
DEFAULT_SESSION_TIMEOUT_SECONDS = 60.0 * 15.0
DEFAULT_STATUS_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_WS_PORT_OFFSET = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class HarnessState:
    """
    Shared in-memory state for the test harness.
    """

    status: str = "idle"
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    last_frame_at: Optional[str] = None
    last_error: Optional[str] = None
    latest_frame: Optional[bytes] = None
    final_result: Optional[dict[str, Any]] = None
    connection_status: str = "unknown"
    connection_checked_at: Optional[str] = None
    connection_details: Optional[dict[str, Any]] = None
    session_id: Optional[str] = None
    session_state: str = "idle"
    remaining_seconds: Optional[int] = None
    latest_ir_temperature_c: Optional[float] = None
    abort_requested: bool = False
    pi_status_checked_at: Optional[str] = None
    websocket_status: str = "idle"
    websocket_checked_at: Optional[str] = None
    websocket_error: Optional[str] = None
    stream_transport: str = "websocket"
    final_result_binary: Optional[list[int]] = None
    active_request: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "status": self.status,
                "running": self.active_request,
                "startedAt": self.started_at,
                "completedAt": self.completed_at,
                "lastFrameAt": self.last_frame_at,
                "lastError": self.last_error,
                "finalResult": self.final_result,
                "connectionStatus": self.connection_status,
                "connectionCheckedAt": self.connection_checked_at,
                "connectionDetails": self.connection_details,
                "sessionId": self.session_id,
                "sessionState": self.session_state,
                "remainingSeconds": self.remaining_seconds,
                "latestIrTemperatureC": self.latest_ir_temperature_c,
                "abortRequested": self.abort_requested,
                "piStatusCheckedAt": self.pi_status_checked_at,
                "websocketStatus": self.websocket_status,
                "websocketCheckedAt": self.websocket_checked_at,
                "websocketError": self.websocket_error,
                "streamTransport": self.stream_transport,
                "finalResultBinary": self.final_result_binary,
            }


class TestHarnessHandler(BaseHTTPRequestHandler):
    """
    HTTP handler for the test UI and trigger endpoints.
    """

    state: HarnessState
    camera_index: int
    stream_endpoint: str
    websocket_endpoint: str
    pi_host: str
    pi_port: int

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._serve_index()
            return
        if parsed.path == "/api/status":
            self._send_json(self.state.snapshot())
            return
        if parsed.path == "/api/latest-frame.jpg":
            self._serve_latest_frame()
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/check-connection":
            self._check_connection()
            return
        if parsed.path == "/api/start-session":
            self._start_session()
            return
        if parsed.path == "/api/abort-session":
            self._abort_session()
            return
        if parsed.path == "/api/live-frame":
            self._receive_live_frame()
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args) -> None:
        LOGGER.info("%s - %s", self.address_string(), format % args)

    def _serve_index(self) -> None:
        body = INDEX_HTML_PATH.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_latest_frame(self) -> None:
        with self.state.lock:
            payload = self.state.latest_frame

        if payload is None:
            self.send_error(HTTPStatus.NOT_FOUND, "No frame available yet")
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _check_connection(self) -> None:
        try:
            response = _send_pi_request(
                host=self.pi_host,
                port=self.pi_port,
                payload={"action": "health"},
                timeout_seconds=DEFAULT_REQUEST_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            with self.state.lock:
                self.state.connection_status = "error"
                self.state.connection_checked_at = _utc_now()
                self.state.connection_details = None
                self.state.last_error = f"Unable to reach Pi server: {exc}"
            self._send_json(self.state.snapshot(), status=HTTPStatus.BAD_GATEWAY)
            return

        with self.state.lock:
            self.state.connection_status = "connected"
            self.state.connection_checked_at = _utc_now()
            self.state.connection_details = response
            if self.state.status == "idle":
                self.state.last_error = None

        self._send_json(self.state.snapshot())

    def _start_session(self) -> None:
        with self.state.lock:
            if self.state.active_request:
                self._send_json(
                    {
                        "status": self.state.status,
                        "message": "Session already running",
                    },
                    status=HTTPStatus.CONFLICT,
                )
                return

            self.state.status = "running"
            self.state.active_request = True
            self.state.started_at = _utc_now()
            self.state.completed_at = None
            self.state.last_frame_at = None
            self.state.last_error = None
            self.state.latest_frame = None
            self.state.final_result = None
            self.state.session_id = None
            self.state.session_state = "starting"
            self.state.remaining_seconds = None
            self.state.latest_ir_temperature_c = None
            self.state.abort_requested = False
            self.state.pi_status_checked_at = None
            self.state.websocket_status = "waiting" if websocket_serve is not None else "unavailable"
            self.state.websocket_checked_at = None
            self.state.websocket_error = None if websocket_serve is not None else "websockets package is not installed"
            self.state.stream_transport = "websocket" if websocket_serve is not None else "http-fallback"
            self.state.final_result_binary = None

        threading.Thread(
            target=_run_remote_session,
            args=(
                self.state,
                self.pi_host,
                self.pi_port,
                self._build_start_request_payload(),
            ),
            name="test-web-app-session-watch",
            daemon=True,
        ).start()
        threading.Thread(
            target=_poll_remote_status,
            args=(self.state, self.pi_host, self.pi_port),
            name="test-web-app-status-poll",
            daemon=True,
        ).start()
        self._send_json(self.state.snapshot(), status=HTTPStatus.ACCEPTED)

    def _abort_session(self) -> None:
        with self.state.lock:
            if not self.state.active_request:
                self._send_json(
                    {
                        "status": self.state.status,
                        "message": "No timed session is currently running",
                    },
                    status=HTTPStatus.CONFLICT,
                )
                return
            self.state.status = "aborting"

        try:
            response = _send_pi_request(
                host=self.pi_host,
                port=self.pi_port,
                payload={"action": "abort_test"},
                timeout_seconds=DEFAULT_REQUEST_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            with self.state.lock:
                self.state.last_error = f"Unable to send abort request to Pi server: {exc}"
                self.state.status = "error"
            self._send_json(self.state.snapshot(), status=HTTPStatus.BAD_GATEWAY)
            return

        self._send_json(
            {
                **self.state.snapshot(),
                "abortResponse": response,
            },
            status=HTTPStatus.ACCEPTED,
        )

    def _receive_live_frame(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        payload = self.rfile.read(content_length)

        with self.state.lock:
            self.state.latest_frame = payload
            self.state.last_frame_at = _utc_now()
            if self.state.status == "idle":
                self.state.status = "running"

        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def _send_json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _build_start_request_payload(self) -> dict[str, Any]:
        """
        Prefer the local WebSocket receiver when available, otherwise fall back
        to the existing HTTP frame receiver for compatibility.
        """

        payload: dict[str, Any] = {
            "action": "start_test",
            "cameraIndex": self.camera_index,
        }

        if websocket_serve is not None:
            payload["websocketEndpoint"] = self.websocket_endpoint
        else:
            payload["streamEndpoint"] = self.stream_endpoint

        return payload


def _send_pi_request(
    host: str,
    port: int,
    payload: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    """
    Send one newline-delimited JSON request to the Pi server and decode the reply.
    """

    encoded = (json.dumps(payload) + "\n").encode("utf-8")
    with socket.create_connection((host, port), timeout=timeout_seconds) as sock:
        sock.settimeout(timeout_seconds)
        sock.sendall(encoded)
        reader = sock.makefile("r", encoding="utf-8", newline="\n")
        line = reader.readline()

    if not line:
        raise RuntimeError("Pi server closed the connection without sending a response")

    response = json.loads(line)
    if not isinstance(response, dict):
        raise RuntimeError("Pi server returned a non-object response")
    return response


def _run_remote_session(
    state: HarnessState,
    pi_host: str,
    pi_port: int,
    request_payload: dict[str, Any],
) -> None:
    """
    Start one remote timed session on the Pi server and store the final response.
    """

    try:
        response = _send_pi_request(
            host=pi_host,
            port=pi_port,
            payload=request_payload,
            timeout_seconds=DEFAULT_SESSION_TIMEOUT_SECONDS,
        )
        with state.lock:
            state.completed_at = _utc_now()
            state.final_result = response
            if response.get("status") == "success":
                state.status = "completed"
                state.last_error = None
            elif response.get("status") == "aborted":
                state.status = "aborted"
                state.last_error = response.get("message")
            elif response.get("status") == "busy":
                state.status = "busy"
                state.last_error = response.get("message", "Pi session already active")
            else:
                state.status = "error"
                state.last_error = response.get("message", "Pi session failed")
    except Exception as exc:
        with state.lock:
            state.completed_at = _utc_now()
            state.status = "error"
            state.last_error = f"Unable to complete remote session: {exc}"
            state.final_result = None
    finally:
        with state.lock:
            state.active_request = False


def _poll_remote_status(
    state: HarnessState,
    pi_host: str,
    pi_port: int,
) -> None:
    """
    Poll the Pi status endpoint while a timed session is active so the harness
    can display remaining time and IR telemetry.
    """

    while True:
        with state.lock:
            if not state.active_request:
                break

        try:
            response = _send_pi_request(
                host=pi_host,
                port=pi_port,
                payload={"action": "status"},
                timeout_seconds=DEFAULT_REQUEST_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            with state.lock:
                if state.active_request:
                    state.last_error = f"Unable to refresh Pi status: {exc}"
            break

        with state.lock:
            state.session_id = response.get("session_id")
            state.session_state = response.get("session_state", state.session_state)
            state.remaining_seconds = response.get("remaining_seconds")
            state.latest_ir_temperature_c = response.get("latest_ir_temperature_c")
            state.abort_requested = bool(response.get("abort_requested", False))
            state.pi_status_checked_at = _utc_now()
            if state.active_request and response.get("session_active") is True:
                state.status = response.get("session_state", state.status)

        time.sleep(DEFAULT_STATUS_POLL_INTERVAL_SECONDS)


def _decode_binary_message(payload: bytes) -> tuple[dict[str, Any], bytes]:
    """
    Decode one packed binary WebSocket message from the Pi stream.
    """

    if len(payload) < 4:
        raise ValueError("Binary message is too short to contain a header length")

    header_length = int.from_bytes(payload[:4], byteorder="big")
    header_start = 4
    header_end = header_start + header_length
    if header_length <= 0 or header_end > len(payload):
        raise ValueError("Binary message contains an invalid header length")

    header = json.loads(payload[header_start:header_end].decode("utf-8"))
    if not isinstance(header, dict):
        raise ValueError("Binary message header must decode to a JSON object")
    return header, payload[header_end:]


def _handle_websocket_message(state: HarnessState, message: Any) -> None:
    """
    Apply one incoming WebSocket message to the harness state.
    """

    now = _utc_now()
    with state.lock:
        state.websocket_status = "connected"
        state.websocket_checked_at = now
        state.websocket_error = None

        if isinstance(message, str):
            payload = json.loads(message)
            if not isinstance(payload, dict):
                return
            message_type = payload.get("type")
            if message_type == "session_status":
                state.session_id = payload.get("session_id", state.session_id)
                state.session_state = payload.get("session_state", state.session_state)
                state.remaining_seconds = payload.get("remaining_seconds")
                state.latest_ir_temperature_c = payload.get("ir_temperature_c")
                state.abort_requested = bool(payload.get("abort_requested", state.abort_requested))
            elif message_type:
                state.session_id = payload.get("session_id", state.session_id)
                if message_type.startswith("session_"):
                    state.session_state = message_type.removeprefix("session_")
            return

        header, binary_payload = _decode_binary_message(message)
        message_type = header.get("type")
        state.session_id = header.get("session_id", state.session_id)
        state.websocket_checked_at = now

        if message_type == "annotated_frame":
            state.latest_frame = binary_payload
            state.last_frame_at = now
            if state.status in {"idle", "starting"}:
                state.status = "running"
            if state.session_state in {"idle", "starting"}:
                state.session_state = "running"
            return

        if message_type == "final_result":
            decoded = [int(value) for value in binary_payload]
            state.final_result_binary = decoded
            state.status = "completed"
            state.session_state = "completed"
            if state.final_result is None:
                state.final_result = {
                    "status": "success",
                    "message": "Final result received over WebSocket",
                    "session": {
                        "session_id": header.get("session_id"),
                        "binaryData": decoded,
                        "encoding": header.get("encoding"),
                        "value_count": header.get("value_count"),
                        "run_id": header.get("run_id"),
                        "plateId": header.get("plate_id"),
                    },
                }


def _run_websocket_server(
    state: HarnessState,
    host: str,
    port: int,
) -> None:
    """
    Run a tiny self-contained WebSocket receiver for local Pi stream testing.
    """

    if websocket_serve is None:
        LOGGER.warning("websockets is not installed; local test harness WebSocket receiver is disabled")
        with state.lock:
            state.websocket_status = "unavailable"
            state.websocket_error = "websockets package is not installed"
            state.websocket_checked_at = _utc_now()
        return

    def _handler(websocket) -> None:
        LOGGER.info("Test harness WebSocket connection opened")
        with state.lock:
            state.websocket_status = "connected"
            state.websocket_error = None
            state.websocket_checked_at = _utc_now()

        try:
            for message in websocket:
                _handle_websocket_message(state, message)
        except (OSError, TimeoutError, WebSocketException, ValueError, json.JSONDecodeError) as exc:
            LOGGER.warning("Test harness WebSocket stream error: %s", exc)
            with state.lock:
                state.websocket_status = "error"
                state.websocket_error = str(exc)
                state.websocket_checked_at = _utc_now()
        finally:
            with state.lock:
                if state.websocket_status != "error":
                    state.websocket_status = "waiting"
                state.websocket_checked_at = _utc_now()
            LOGGER.info("Test harness WebSocket connection closed")

    with websocket_serve(_handler, host, port):
        LOGGER.info("Test harness WebSocket receiver listening on ws://%s:%s", host, port)
        while True:
            time.sleep(3600)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the image_analysis test web app")
    parser.add_argument("--host", default="0.0.0.0", help="Host interface to bind")
    parser.add_argument("--port", type=int, default=8081, help="Port to bind")
    parser.add_argument(
        "--ws-port",
        type=int,
        help="Port for the local test harness WebSocket receiver. Defaults to HTTP port + 1.",
    )
    parser.add_argument("--camera-index", type=int, default=0, help="Camera index for remote live sessions")
    parser.add_argument("--pi-host", default="127.0.0.1", help="Raspberry Pi socket server host")
    parser.add_argument("--pi-port", type=int, default=5000, help="Raspberry Pi socket server port")
    parser.add_argument(
        "--public-host",
        default="127.0.0.1",
        help="Host name or IP address that the Pi should use to POST live frames back to this web app",
    )
    return parser


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def run_server(
    host: str,
    port: int,
    ws_port: int,
    camera_index: int,
    pi_host: str,
    pi_port: int,
    public_host: str,
) -> int:
    state = HarnessState()
    handler_class = type(
        "ConfiguredTestHarnessHandler",
        (TestHarnessHandler,),
        {
            "state": state,
            "camera_index": camera_index,
            "pi_host": pi_host,
            "pi_port": pi_port,
            "stream_endpoint": f"http://{public_host}:{port}/api/live-frame",
            "websocket_endpoint": f"ws://{public_host}:{ws_port}",
        },
    )

    threading.Thread(
        target=_run_websocket_server,
        args=(state, host, ws_port),
        name="test-web-app-ws-server",
        daemon=True,
    ).start()

    server = ThreadingHTTPServer((host, port), handler_class)
    LOGGER.info("Test web app listening on http://%s:%s", host, port)
    LOGGER.info("Test harness WebSocket receiver target: ws://%s:%s", public_host, ws_port)
    LOGGER.info("Configured Pi socket server target: %s:%s", pi_host, pi_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("Stopping test web app")
    finally:
        server.server_close()
    return 0


def main() -> int:
    configure_logging()
    args = build_argument_parser().parse_args()
    ws_port = args.ws_port if args.ws_port is not None else args.port + DEFAULT_WS_PORT_OFFSET
    return run_server(
        host=args.host,
        port=args.port,
        ws_port=ws_port,
        camera_index=args.camera_index,
        pi_host=args.pi_host,
        pi_port=args.pi_port,
        public_host=args.public_host,
    )


if __name__ == "__main__":
    raise SystemExit(main())
