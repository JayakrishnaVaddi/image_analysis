"""
Small test web app for exercising the live session flow end to end.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse


LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML_PATH = Path(__file__).resolve().parent / "static" / "index.html"


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
    process: Optional[subprocess.Popen[str]] = None
    logs: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> dict[str, Optional[str] | bool]:
        with self.lock:
            running = self.process is not None and self.process.poll() is None
            return {
                "status": self.status,
                "running": running,
                "startedAt": self.started_at,
                "completedAt": self.completed_at,
                "lastFrameAt": self.last_frame_at,
                "lastError": self.last_error,
            }


class TestHarnessHandler(BaseHTTPRequestHandler):
    """
    HTTP handler for the test UI and trigger endpoints.
    """

    state: HarnessState
    camera_index: int
    stream_endpoint: str

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

    def _start_session(self) -> None:
        with self.state.lock:
            process = self.state.process
            if process is not None and process.poll() is None:
                self._send_json(
                    {
                        "status": self.state.status,
                        "message": "Session already running",
                    },
                    status=HTTPStatus.CONFLICT,
                )
                return

            self.state.status = "running"
            self.state.started_at = _utc_now()
            self.state.completed_at = None
            self.state.last_frame_at = None
            self.state.last_error = None
            self.state.latest_frame = None

            command = [
                sys.executable,
                str(PROJECT_ROOT / "main.py"),
                "--mode",
                "live",
                "--camera-index",
                str(self.camera_index),
            ]
            env = os.environ.copy()
            env["VIDEO_STREAM_ENDPOINT"] = self.stream_endpoint
            env["PYTHONUNBUFFERED"] = "1"

            LOGGER.info("Starting test session with command: %s", " ".join(command))
            process = subprocess.Popen(
                command,
                cwd=str(PROJECT_ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            self.state.process = process

        threading.Thread(
            target=_watch_process,
            args=(self.state, process),
            name="test-web-app-process-watch",
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


def _watch_process(state: HarnessState, process: subprocess.Popen[str]) -> None:
    """
    Watch the session subprocess and update harness state when it exits.
    """

    output_lines: list[str] = []
    if process.stdout is not None:
        for line in process.stdout:
            clean_line = line.rstrip()
            output_lines.append(clean_line)
            LOGGER.info("[session] %s", clean_line)

    return_code = process.wait()
    with state.lock:
        state.logs = output_lines[-100:]
        state.process = None
        state.completed_at = _utc_now()
        if return_code == 0:
            state.status = "completed"
            state.last_error = None
        else:
            state.status = "error"
            state.last_error = f"Session process exited with code {return_code}"


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the image_analysis test web app")
    parser.add_argument("--host", default="0.0.0.0", help="Host interface to bind")
    parser.add_argument("--port", type=int, default=8081, help="Port to bind")
    parser.add_argument("--camera-index", type=int, default=0, help="Camera index for live sessions")
    return parser


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def run_server(host: str, port: int, camera_index: int) -> int:
    state = HarnessState()
    handler_class = type(
        "ConfiguredTestHarnessHandler",
        (TestHarnessHandler,),
        {
            "state": state,
            "camera_index": camera_index,
            "stream_endpoint": f"http://127.0.0.1:{port}/api/live-frame",
        },
    )

    server = ThreadingHTTPServer((host, port), handler_class)
    LOGGER.info("Test web app listening on http://%s:%s", host, port)
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
    return run_server(host=args.host, port=args.port, camera_index=args.camera_index)


if __name__ == "__main__":
    raise SystemExit(main())
