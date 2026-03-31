#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import time
import subprocess
from gpiozero import LED

# ---- MLX90614 imports (I2C) ----
import board
import busio
import adafruit_mlx90614


# ---------------- LED SETUP ----------------
# Old LEDs
led1 = LED(17)   # physical pin 11
led2 = LED(27)   # physical pin 13
led3 = LED(22)   # physical pin 15
led4 = LED(26)   # physical pin 37
led5 = LED(23)   # GPIO23
led6 = LED(5)

ALL_LEDS = (led1, led2, led3, led4, led5, led6)


def leds_on():
    for led in ALL_LEDS:
        led.on()


def leds_off():
    for led in ALL_LEDS:
        led.off()


# ---------------- RELAY COMMAND ----------------
RELAY_CMD = "/home/pi/3relind-rpi/3relind"   # use full path if needed

def relay_write(channel: int, state: str) -> bool:
    try:
        subprocess.run([RELAY_CMD, "0", "write", str(channel), state], check=True)
        return True
    except Exception as e:
        print(f"[ERROR] Relay command failed: {e}")
        return False


# ---------------- TEMPERATURE THRESHOLDS ----------------
TEMP_ON_C  = 100.0
TEMP_OFF_C = 104.0


# ---------------- MLX90614 SETUP ----------------
def init_mlx90614():
    try:
        i2c = busio.I2C(board.SCL, board.SDA)
        return adafruit_mlx90614.MLX90614(i2c)
    except Exception as e:
        print(f"[ERROR] MLX90614 init failed: {e}")
        return None


mlx = init_mlx90614()


def read_temp():
    if mlx is None:
        return None
    try:
        return float(mlx.object_temperature)
    except Exception:
        return None


# ---------------- MAIN ----------------
print("[INFO] Starting heater control")

relay_on = False

# Start safe
relay_write(1, "off")
leds_off()

try:
    while True:
        temp = read_temp()

        if temp is None:
            time.sleep(1)
            continue

        print(f"Temperature: {temp:.2f} C")

        # Relay OFF condition
        if temp >= TEMP_OFF_C and relay_on:
            if relay_write(1, "off"):
                relay_on = False
                leds_off()
                print("[INFO] Relay OFF → LEDs OFF")

        # Relay ON condition
        elif temp <= TEMP_ON_C and not relay_on:
            if relay_write(1, "on"):
                relay_on = True
                leds_on()
                print("[INFO] Relay ON → LEDs ON")

        time.sleep(2)

except KeyboardInterrupt:
    print("\n[INFO] Stopping")

finally:
    relay_write(1, "off")
    leds_off()
