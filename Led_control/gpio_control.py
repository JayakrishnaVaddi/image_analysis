"""Standalone transistor control for LED outputs on Raspberry Pi 4B."""

import atexit

try:
    import RPi.GPIO as GPIO
except ImportError:
    GPIO = None


# BCM GPIO17 and GPIO27
LED_CONTROL_PIN_1 = 17
LED_CONTROL_PIN_2 = 27
LED_CONTROL_PINS = (LED_CONTROL_PIN_1, LED_CONTROL_PIN_2)

_gpio_initialized = False


class GPIOUnavailableError(RuntimeError):
    """Raised when Raspberry Pi GPIO control is unavailable."""


def _require_gpio() -> None:
    if GPIO is None:
        raise GPIOUnavailableError("RPi.GPIO is not installed or is unavailable.")


def _setup_gpio() -> None:
    """Prepare LED output pins once, no matter which script imports this module."""
    global _gpio_initialized

    _require_gpio()
    if _gpio_initialized:
        return

    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    for pin in LED_CONTROL_PINS:
        GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)
    _gpio_initialized = True


def _set_leds(enabled: bool) -> None:
    _setup_gpio()
    state = GPIO.HIGH if enabled else GPIO.LOW
    for pin in LED_CONTROL_PINS:
        GPIO.output(pin, state)


def LED_ON() -> None:
    """Turn on both LED transistor control outputs."""
    _set_leds(True)


def LED_NO() -> None:
    """Backward-compatible spelling for LED_ON()."""
    LED_ON()


def LED_OFF() -> None:
    """Turn off both LED transistor control outputs."""
    _set_leds(False)


def on_led() -> None:
    """Backward-compatible alias for LED_ON()."""
    LED_ON()


def off_led() -> None:
    """Backward-compatible alias for LED_OFF()."""
    LED_OFF()


def cleanup() -> None:
    """Turn LEDs off and release only the GPIO pins owned by this module."""
    global _gpio_initialized

    if GPIO is None or not _gpio_initialized:
        return

    LED_OFF()
    GPIO.cleanup(LED_CONTROL_PINS)
    _gpio_initialized = False


atexit.register(cleanup)
