"""
Heating pad relay control for timed analysis sessions.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

try:
    from gpiozero import LED
except ImportError:  # pragma: no cover - depends on Raspberry Pi hardware packages.
    LED = None

from ir_sensor_service import IRSensorService


LOGGER = logging.getLogger(__name__)

RELAY_CMD = Path(__file__).resolve().parent / "3relind-rpi" / "3relind"
TEMP_ON_C = 100.0
TEMP_OFF_C = 104.0
LED_GPIO_PINS = (17, 27, 22, 26, 23, 5)


class HeatingPadService:
    """
    Manage the heating pad relay while reusing the threshold behavior from the
    standalone auto-heat script.
    """

    def __init__(
        self,
        ir_sensor: IRSensorService,
        control_interval_seconds: float = 2.0,
        temp_on_c: float = TEMP_ON_C,
        temp_off_c: float = TEMP_OFF_C,
    ) -> None:
        self._ir_sensor = ir_sensor
        self._control_interval_seconds = control_interval_seconds
        self._temp_on_c = temp_on_c
        self._temp_off_c = temp_off_c
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._relay_on = False
        self._leds = self._build_leds()

    def start(self) -> None:
        """
        Activate heating control for the current session.
        """

        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._set_relay_state(False)
        self._set_relay_state(True)
        LOGGER.info("Heating pad on")
        self._thread = threading.Thread(
            target=self._control_loop,
            name="heating-pad-control",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """
        Stop relay control and force the heating pad off.
        """

        thread = self._thread
        if thread is not None:
            self._stop_event.set()
            thread.join(timeout=self._control_interval_seconds + 1.0)
            self._thread = None

        self._set_relay_state(False)
        LOGGER.info("Heating pad off")

    def _control_loop(self) -> None:
        """
        Maintain the relay using the same threshold pattern as auto_heat.py.
        """

        while not self._stop_event.is_set():
            temperature_c = self._ir_sensor.latest_temperature_c()

            if temperature_c is not None:
                if temperature_c >= self._temp_off_c and self._relay_on:
                    self._set_relay_state(False)
                elif temperature_c <= self._temp_on_c and not self._relay_on:
                    self._set_relay_state(True)

            time.sleep(self._control_interval_seconds)

    def _build_leds(self):
        """
        Create gpiozero LED handles when the package is available.
        """

        if LED is None:
            LOGGER.warning("gpiozero is unavailable; heating status LEDs are disabled")
            return ()

        return tuple(LED(pin) for pin in LED_GPIO_PINS)

    def _relay_write(self, channel: int, state: str) -> bool:
        """
        Write the requested state to the relay board.
        """

        try:
            subprocess.run(
                [str(RELAY_CMD), "0", "write", str(channel), state],
                check=True,
                capture_output=True,
                text=True,
            )
            return True
        except Exception as exc:  # pragma: no cover - hardware runtime path.
            LOGGER.error("Heating pad relay command failed: %s", exc)
            return False

    def _set_relay_state(self, enabled: bool) -> None:
        """
        Update the relay and mirror the state to the status LEDs.
        """

        if enabled == self._relay_on:
            return

        desired_state = "on" if enabled else "off"
        if not self._relay_write(1, desired_state):
            return

        self._relay_on = enabled
        for led in self._leds:
            if enabled:
                led.on()
            else:
                led.off()
