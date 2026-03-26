"""
Transport helpers for live annotated video frame delivery.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

import cv2
import numpy as np

from config import VIDEO_STREAM


LOGGER = logging.getLogger(__name__)


class HttpFrameStreamSender:
    """
    Send annotated JPEG frames to a remote HTTP endpoint on a background thread.

    The sender uses a tiny bounded queue so live processing never blocks on a
    slow or temporarily unavailable endpoint. Older frames are dropped in favor
    of the newest view when the network falls behind.
    """

    def __init__(
        self,
        endpoint: str,
        jpeg_quality: Optional[int] = None,
        reconnect_delay_seconds: Optional[float] = None,
    ) -> None:
        self.endpoint = endpoint
        self._jpeg_quality = jpeg_quality if jpeg_quality is not None else VIDEO_STREAM.jpeg_quality
        self._reconnect_delay_seconds = (
            reconnect_delay_seconds
            if reconnect_delay_seconds is not None
            else VIDEO_STREAM.reconnect_delay_seconds
        )
        self._queue: "queue.Queue[Optional[bytes]]" = queue.Queue(maxsize=VIDEO_STREAM.queue_size)
        self._stop_event = threading.Event()
        self._worker = threading.Thread(target=self._run, name="http-frame-stream", daemon=True)
        self._successful_connection_logged = False

    def start(self) -> None:
        """
        Start the background sender thread.
        """

        LOGGER.info("Live annotated streaming enabled: endpoint configured at %s", self.endpoint)
        self._worker.start()

    def stop(self) -> None:
        """
        Stop the background sender thread.
        """

        self._stop_event.set()
        self._offer(None)
        self._worker.join(timeout=VIDEO_STREAM.send_timeout_seconds + 1.0)

    def submit_frame(self, frame: np.ndarray) -> None:
        """
        Encode a frame as JPEG and enqueue it for asynchronous delivery.
        """

        success, encoded = cv2.imencode(
            ".jpg",
            frame,
            [int(cv2.IMWRITE_JPEG_QUALITY), int(self._jpeg_quality)],
        )
        if not success:
            LOGGER.warning("Failed to JPEG-encode annotated frame for streaming")
            return

        self._offer(encoded.tobytes())

    def _offer(self, payload: Optional[bytes]) -> None:
        """
        Enqueue the newest payload, dropping an older one if necessary.
        """

        try:
            self._queue.put_nowait(payload)
            return
        except queue.Full:
            pass

        try:
            self._queue.get_nowait()
        except queue.Empty:
            pass

        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            LOGGER.debug("Dropping annotated frame because the streaming queue is full")

    def _run(self) -> None:
        """
        Deliver frames until streaming is stopped.
        """

        while not self._stop_event.is_set():
            try:
                payload = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if payload is None:
                continue

            try:
                request = urllib.request.Request(
                    self.endpoint,
                    data=payload,
                    headers={"Content-Type": "image/jpeg"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=VIDEO_STREAM.send_timeout_seconds) as response:
                    response.read(1)

                if not self._successful_connection_logged:
                    LOGGER.info("Connected to live video endpoint successfully")
                    self._successful_connection_logged = True
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                LOGGER.warning(
                    "Live frame send failed for %s: %s. Retrying in %.1f seconds.",
                    self.endpoint,
                    exc,
                    self._reconnect_delay_seconds,
                )
                self._successful_connection_logged = False
                time.sleep(self._reconnect_delay_seconds)
