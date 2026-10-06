# /// script
# requires-python = ">=3.11"
# dependencies = ["bleak>=3,<4"]
# ///
"""
Step 4c (part 2) — Colour: set the bulb to a white temperature or a colour.

Run it with:   uv run color.py warm | cool | red | green | blue
               uv run color.py kelvin 4000
               uv run color.py xy 0.3 0.3
               uv run color.py                  (just show the current values)

Two ways a Hue bulb describes its colour (from reading the bulb in color_probe.py):

1. Colour temperature (characteristic 0004), for shades of white. Stored as 2 bytes,
   little-endian, in *mireds* = 1,000,000 / kelvin. This bulb reports a range of
   153 (≈6500 K, cool blue-white) to 500 (2000 K, warm candle-light).

2. CIE xy colour (characteristic 0005), for any colour. It's a point on the standard
   "horseshoe" chart of colours the eye can see: x and y are each 0-1. Our best guess
   at the format is 4 bytes: x then y, each as 2 bytes little-endian, scaled so
   1.0 = 65535. It reads ff ff ff ff while the bulb is in white mode.

Setting one mode switches the bulb out of the other.
"""

import asyncio
import sys

from bleak import BleakClient

from hue_config import COLOR_CHAR, TEMPERATURE_CHAR, load_address

MIN_MIREDS, MAX_MIREDS = 153, 500  # reported by the bulb (characteristic 0001)

# Rough xy points for primary colours, inside what Hue colour bulbs can show.
PRESETS_XY = {"red": (0.675, 0.322), "green": (0.409, 0.518), "blue": (0.167, 0.04)}
PRESETS_KELVIN = {"warm": 2200, "cool": 6500}


# --- converting between friendly numbers and the bulb's bytes ----------------------

def kelvin_to_bytes(kelvin: int) -> bytes:
    mireds = round(1_000_000 / kelvin)
    mireds = max(MIN_MIREDS, min(MAX_MIREDS, mireds))  # clamp to what the bulb supports
    return mireds.to_bytes(2, "little")


def xy_to_bytes(x: float, y: float) -> bytes:
    return round(x * 65535).to_bytes(2, "little") + round(y * 65535).to_bytes(2, "little")


def describe_temperature(value: bytes) -> str:
    mireds = int.from_bytes(value, "little")
    return f"{value.hex()} = {mireds} mireds ≈ {round(1_000_000 / mireds)} K" if mireds else value.hex()


def describe_xy(value: bytes) -> str:
    if value == b"\xff\xff\xff\xff":
        return f"{value.hex()} (not set: bulb is in white mode)"
    x = int.from_bytes(value[0:2], "little") / 65535
    y = int.from_bytes(value[2:4], "little") / 65535
    return f"{value.hex()} = x {x:.3f}, y {y:.3f}"


# --- talking to the bulb -----------------------------------------------------------

async def show(client):
    print("  temperature:", describe_temperature(await client.read_gatt_char(TEMPERATURE_CHAR)))
    print("  colour xy:  ", describe_xy(await client.read_gatt_char(COLOR_CHAR)))


async def main(char, data):
    async with BleakClient(load_address(), timeout=20) as client:
        print("Before:")
        await show(client)
        if char is None:
            return
        await client.write_gatt_char(char, data, response=True)
        print(f"Sent {data.hex()}")
        await asyncio.sleep(1.0)  # let the fade finish
        print("After:")
        await show(client)


def parse(args):
    """Turn the command line into (characteristic, bytes to write), or (None, None) to just read."""
    if not args:
        return None, None
    cmd = args[0].lower()
    if cmd in PRESETS_KELVIN:
        return TEMPERATURE_CHAR, kelvin_to_bytes(PRESETS_KELVIN[cmd])
    if cmd in PRESETS_XY:
        return COLOR_CHAR, xy_to_bytes(*PRESETS_XY[cmd])
    if cmd == "kelvin" and len(args) == 2:
        return TEMPERATURE_CHAR, kelvin_to_bytes(int(args[1]))
    if cmd == "xy" and len(args) == 3:
        return COLOR_CHAR, xy_to_bytes(float(args[1]), float(args[2]))
    sys.exit(__doc__.split("Two ways")[0])  # print the usage part of the docstring


if __name__ == "__main__":
    asyncio.run(main(*parse(sys.argv[1:])))
