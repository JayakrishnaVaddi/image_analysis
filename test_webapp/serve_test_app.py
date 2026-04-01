"""
Serve the standalone browser test app locally for manual validation.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import socketserver
from pathlib import Path


class ThreadingHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class NoCacheHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve the standalone test web app")
    parser.add_argument("--host", default="0.0.0.0", help="Host interface to bind")
    parser.add_argument("--port", type=int, default=8080, help="HTTP port for the test page")
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()
    web_root = Path(__file__).resolve().parent
    handler = functools.partial(NoCacheHTTPRequestHandler, directory=str(web_root))

    with ThreadingHTTPServer((args.host, args.port), handler) as httpd:
        print(f"Serving test web app from {web_root} at http://{args.host}:{args.port}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopping test web app server")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
