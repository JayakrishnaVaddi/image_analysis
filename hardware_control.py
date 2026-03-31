"""
Minimal hardware control wrapper for the Raspberry Pi heater relay and IR sensor.

This module is based on the behavior shown in auto_heat.py while keeping the
implementation reusable for the main project server.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Optional


LOGGER = logging.getLogger(__name__)
REPO_ROOT = Path(__file__).resolve().parent
RELAY_CHANNEL = 1
RELAY_COMMAND_CANDIDATES = (
    REPO_ROOT / "3relind-rpi" / "3relind",
    Path("/home/pi/3relind-rpi/3relind"),
)

try:  # pragma: no cover - depends on Raspberry Pi environment.
    import board
    import busio
    import adafruit_mlx90614
except ImportError:  # pragma: no cover - depends on Raspberry Pi environment.
    board = None
    busio = None
    adafruit_mlx90614 = None


class HardwareController:
    """
    Control the heater relay and MLX90614 non-contact IR sensor.
    """

    def __init__(self) -> None:
        self._mlx = None

    def turn_heater_on(self) -> bool:
        return self._relay_write(RELAY_CHANNEL, "on")

    def turn_heater_off(self) -> bool:
        return self._relay_write(RELAY_CHANNEL, "off")

    def read_temperature_celsius(self) -> Optional[float]:
        sensor = self._ensure_sensor()
        if sensor is None:
            return None

        try:
            return float(sensor.object_temperature)
        except Exception:
            LOGGER.exception("MLX90614 temperature read failed")
            return None

    def _relay_write(self, channel: int, state: str) -> bool:
        relay_command = self._resolve_relay_command()
        if relay_command is None:
            LOGGER.error("Relay command not found; unable to switch heater %s", state)
            return False

        try:
            subprocess.run(
                [str(relay_command), "0", "write", str(channel), state],
                check=True,
                capture_output=True,
                text=True,
            )
            LOGGER.info("Heater relay set to %s via %s", state, relay_command)
            return True
        except Exception:
            LOGGER.exception("Relay command failed while switching heater %s", state)
            return False

    def _resolve_relay_command(self) -> Optional[Path]:
        for candidate in RELAY_COMMAND_CANDIDATES:
            if candidate.exists():
                return candidate
        return None

    def _ensure_sensor(self):
        if self._mlx is not None:
            return self._mlx

        if board is None or busio is None or adafruit_mlx90614 is None:
            LOGGER.warning("MLX90614 dependencies are unavailable; temperature readings disabled")
            return None

        try:
            i2c = busio.I2C(board.SCL, board.SDA)
            self._mlx = adafruit_mlx90614.MLX90614(i2c)
            LOGGER.info("Initialized MLX90614 sensor")
        except Exception:
            LOGGER.exception("MLX90614 init failed")
            return None

        return self._mlx
