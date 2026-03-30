"""
Raw TCP socket server for Raspberry Pi analysis requests.
"""

from __future__ import annotations

import argparse
import json
import logging
import socketserver
from typing import Optional

from command_handler import CommandHandler
from heating_pad_service import HeatingPadService
from main import configure_logging
from session_orchestrator import SESSION_ORCHESTRATOR


LOGGER = logging.getLogger(__name__)
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 5000


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """
    Thread-per-connection TCP server with predictable shutdown behavior.
    """

    allow_reuse_address = True
    daemon_threads = True


class JsonLineRequestHandler(socketserver.StreamRequestHandler):
    """
    Handle newline-delimited JSON requests over a raw TCP socket.
    """

    def handle(self) -> None:
        client_host, client_port = self.client_address
        LOGGER.info("Socket connection opened from %s:%s", client_host, client_port)

        while True:
            raw_line = self.rfile.readline()
            if not raw_line:
                LOGGER.info("Socket connection closed from %s:%s", client_host, client_port)
                break

            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            LOGGER.info("Received command from %s:%s: %s", client_host, client_port, line)

            try:
                payload = json.loads(line)
                response = self.server.command_handler.handle_request(payload)
            except json.JSONDecodeError as exc:
                LOGGER.warning("Malformed JSON from %s:%s: %s", client_host, client_port, exc)
                response = {"status": "error", "message": "Malformed JSON request"}
            except Exception as exc:  # pragma: no cover - defensive runtime path.
                LOGGER.exception("Command failed for %s:%s: %s", client_host, client_port, exc)
                response = {"status": "error", "message": str(exc)}

            self._send_response(response)

    def _send_response(self, payload: dict) -> None:
        """
        Write one newline-delimited JSON response.
        """

        encoded = (json.dumps(payload) + "\n").encode("utf-8")
        self.wfile.write(encoded)
        self.wfile.flush()
        LOGGER.info("Sent response: %s", payload.get("status", "unknown"))


def build_argument_parser() -> argparse.ArgumentParser:
    """
    Create a small CLI for the standalone socket server.
    """

    parser = argparse.ArgumentParser(description="TCP socket server for Raspberry Pi image analysis")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Host interface to bind")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="TCP port to listen on")
    parser.add_argument("--camera-index", type=int, default=0, help="Camera index used for capture requests")
    return parser


def run_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, camera_index: int = 0) -> None:
    """
    Start the blocking TCP server.
    """

    command_handler = CommandHandler(camera_index=camera_index)
    with ThreadedTCPServer((host, port), JsonLineRequestHandler) as server:
        server.command_handler = command_handler
        LOGGER.info("Socket server listening on %s:%s", host, port)
        try:
            server.serve_forever()
        finally:
            _perform_server_shutdown_cleanup()


def _perform_server_shutdown_cleanup() -> None:
    """
    Best-effort hardware-safe cleanup for server process shutdown.
    """

    try:
        SESSION_ORCHESTRATOR.shutdown_for_server_stop()
    except Exception as exc:  # pragma: no cover - defensive runtime path.
        LOGGER.exception("Server shutdown cleanup failed while stopping active session: %s", exc)

    try:
        HeatingPadService.force_off_safely()
    except Exception as exc:  # pragma: no cover - defensive runtime path.
        LOGGER.exception("Server shutdown cleanup failed while forcing heating pad off: %s", exc)


def main() -> int:
    """
    Parse CLI arguments and run the socket server.
    """

    configure_logging()
    args = build_argument_parser().parse_args()

    try:
        run_server(host=args.host, port=args.port, camera_index=args.camera_index)
    except KeyboardInterrupt:
        LOGGER.info("Socket server stopped by user")
        _perform_server_shutdown_cleanup()
        return 0
    except Exception as exc:  # pragma: no cover - defensive runtime path.
        LOGGER.exception("Socket server failed: %s", exc)
        _perform_server_shutdown_cleanup()
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
