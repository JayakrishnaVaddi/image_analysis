"""
Small test web app for exercising the Pi socket-server session flow end to end.
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse


LOGGER = logging.getLogger(__name__)
INDEX_HTML_PATH = Path(__file__).resolve().parent / "static" / "index.html"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 5.0
DEFAULT_SESSION_TIMEOUT_SECONDS = 60.0 * 15.0


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
            }


class TestHarnessHandler(BaseHTTPRequestHandler):
    """
    HTTP handler for the test UI and trigger endpoints.
    """

    state: HarnessState
    camera_index: int
    stream_endpoint: str
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

        threading.Thread(
            target=_run_remote_session,
            args=(
                self.state,
                self.pi_host,
                self.pi_port,
                {
                    "action": "start_test",
                    "cameraIndex": self.camera_index,
                    "streamEndpoint": self.stream_endpoint,
                },
            ),
            name="test-web-app-session-watch",
            daemon=True,
        ).start()
        self._send_json(self.state.snapshot(), status=HTTPStatus.ACCEPTED)

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


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the image_analysis test web app")
    parser.add_argument("--host", default="0.0.0.0", help="Host interface to bind")
    parser.add_argument("--port", type=int, default=8081, help="Port to bind")
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
        },
    )

    server = ThreadingHTTPServer((host, port), handler_class)
    LOGGER.info("Test web app listening on http://%s:%s", host, port)
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
    return run_server(
        host=args.host,
        port=args.port,
        camera_index=args.camera_index,
        pi_host=args.pi_host,
        pi_port=args.pi_port,
        public_host=args.public_host,
    )


if __name__ == "__main__":
    raise SystemExit(main())
