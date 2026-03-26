"""
Standalone live annotated video streamer for the image-analysis project.
"""

from __future__ import annotations

import argparse
import logging
import signal
import time

from live_stream_service import LiveAnnotatedStreamService


LOGGER = logging.getLogger(__name__)


def build_argument_parser() -> argparse.ArgumentParser:
    """
    Build the CLI for live annotated frame streaming.
    """

    parser = argparse.ArgumentParser(description="Stream annotated live video frames")
    parser.add_argument(
        "--camera-index",
        type=int,
        default=0,
        help="Camera index passed to Raspberry Pi camera tools",
    )
    return parser


def configure_logging() -> None:
    """
    Configure application-wide structured logging.
    """

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def stream_live_annotations(camera_index: int) -> int:
    """
    Continuously analyze live frames and send annotated output to the configured
    remote endpoint until interrupted.
    """
    stream_service = LiveAnnotatedStreamService(camera_index=camera_index)
    stop_requested = False

    def _request_stop(signum, _frame) -> None:
        nonlocal stop_requested
        stop_requested = True
        LOGGER.info("Received signal %s, stopping live annotated stream", signum)

    previous_sigint = signal.signal(signal.SIGINT, _request_stop)
    previous_sigterm = signal.signal(signal.SIGTERM, _request_stop)

    try:
        stream_service.start()
        while not stop_requested:
            if stream_service.error() is not None:
                return 1
            time.sleep(0.25)
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)
        stream_service.stop()

    return 0


def main() -> int:
    """
    Entry point for live annotated streaming.
    """

    configure_logging()
    parser = build_argument_parser()
    args = parser.parse_args()
    return stream_live_annotations(camera_index=args.camera_index)


if __name__ == "__main__":
    raise SystemExit(main())
