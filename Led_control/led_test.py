"""Simple manual test for gpio_control.py."""

import time

from gpio_control import GPIOUnavailableError, off_led, on_led


BLINK_COUNT = 5
INTERVAL_SECONDS = 0.5


def main() -> None:
    led_was_started = False

    print("Testing LED transistor control outputs on BCM GPIO17 and GPIO27.")
    print("GPIO17 is physical pin 11. GPIO27 is physical pin 13.")
    print("These pins should connect to transistor control inputs, not directly to LED power.")

    try:
        for count in range(1, BLINK_COUNT + 1):
            print(f"Blink {count}/{BLINK_COUNT}: ON")
            on_led()
            led_was_started = True
            time.sleep(INTERVAL_SECONDS)

            print(f"Blink {count}/{BLINK_COUNT}: OFF")
            off_led()
            time.sleep(INTERVAL_SECONDS)
    except GPIOUnavailableError as exc:
        print(f"GPIO error: {exc}")
    except KeyboardInterrupt:
        print("\nTest stopped by user.")
    finally:
        if led_was_started:
            off_led()
        print("LED control outputs are off.")


if __name__ == "__main__":
    main()
