"""
Non-contact IR sensor polling for session-based runs.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

try:
    import board
    import busio
    import adafruit_mlx90614
except ImportError:  # pragma: no cover - depends on Raspberry Pi hardware packages.
    board = None
    busio = None
    adafruit_mlx90614 = None


LOGGER = logging.getLogger(__name__)


class IRSensorService:
    """
    Poll the MLX90614 non-contact IR sensor on a background thread.
    """

    def __init__(self, poll_interval_seconds: float = 2.0) -> None:
        self._poll_interval_seconds = poll_interval_seconds
        self._sensor = None
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._latest_temperature_c: Optional[float] = None

    def start(self) -> None:
        """
        Initialize the sensor and begin polling.
        """

        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._sensor = self._init_sensor()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="ir-sensor-poll",
            daemon=True,
        )
        LOGGER.info("IR sensor start")
        self._thread.start()

    def stop(self) -> None:
        """
        Stop polling and clear the active thread.
        """

        thread = self._thread
        if thread is None:
            return

        self._stop_event.set()
        thread.join(timeout=self._poll_interval_seconds + 1.0)
        self._thread = None
        LOGGER.info("IR sensor stop")

    def latest_temperature_c(self) -> Optional[float]:
        """
        Return the most recent temperature reading, if one is available.
        """

        with self._lock:
            return self._latest_temperature_c

    def _init_sensor(self):
        """
        Create an MLX90614 sensor using the same I2C access pattern as the
        existing standalone heating script.
        """

        if board is None or busio is None or adafruit_mlx90614 is None:
            LOGGER.warning("IR sensor libraries are unavailable; temperature polling is disabled")
            return None

        try:
            i2c = busio.I2C(board.SCL, board.SDA)
            return adafruit_mlx90614.MLX90614(i2c)
        except Exception as exc:  # pragma: no cover - hardware runtime path.
            LOGGER.warning("IR sensor initialization failed: %s", exc)
            return None

    def _poll_loop(self) -> None:
        """
        Continuously update the latest IR temperature reading.
        """

        while not self._stop_event.is_set():
            temperature_c = self._read_temperature()
            if temperature_c is not None:
                with self._lock:
                    self._latest_temperature_c = temperature_c
            time.sleep(self._poll_interval_seconds)

    def _read_temperature(self) -> Optional[float]:
        """
        Read the current object temperature in Celsius.
        """

        if self._sensor is None:
            return None

        try:
            return float(self._sensor.object_temperature)
        except Exception as exc:  # pragma: no cover - hardware runtime path.
            LOGGER.warning("IR temperature read failed: %s", exc)
            return None
