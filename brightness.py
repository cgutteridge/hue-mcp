# /// script
# requires-python = ">=3.11"
# dependencies = ["bleak>=3,<4"]
# ///
"""
Step 4b — Brightness: read the raw brightness value, and optionally set it.

Run it with:   uv run brightness.py         (just show the current value)
               uv run brightness.py 20      (set to 20%, then read back)

We use 0-100 % like mcp-lamp does. The community notes say the bulb stores brightness
as one byte from 1 (dimmest) to 254 (brightest), so we convert between the two.
That's unverified, so the script shows the raw bytes and only writes if the current
value really is a single byte.
"""

import asyncio
import sys

from bleak import BleakClient

from hue_config import BRIGHTNESS_CHAR, load_address

MIN_RAW, MAX_RAW = 1, 254


def percent_to_raw(percent: int) -> int:
    """0-100 % -> 1-254. 0 % maps to the dimmest level, not off (use power.py for off)."""
    return round(MIN_RAW + (MAX_RAW - MIN_RAW) * percent / 100)


def raw_to_percent(raw: int) -> int:
    return round((raw - MIN_RAW) * 100 / (MAX_RAW - MIN_RAW))


async def main(percent):
    async with BleakClient(load_address(), timeout=20) as client:
        value = await client.read_gatt_char(BRIGHTNESS_CHAR)
        print(f"Current raw value: {value.hex()}  ({len(value)} byte(s))")

        if len(value) != 1:
            print("Not a single byte, so the notes' format is wrong. Not writing; paste this to Claude.")
            return
        print(f"  = {value[0]} of {MAX_RAW}, about {raw_to_percent(value[0])}%")

        if percent is None:
            return

        raw = percent_to_raw(percent)
        await client.write_gatt_char(BRIGHTNESS_CHAR, bytes([raw]), response=True)
        print(f"Sent: {percent}% (raw {raw})")

        # The bulb fades to the new level rather than jumping. Reading straight away
        # catches it mid-fade, so give the transition a moment to finish.
        await asyncio.sleep(1.0)
        value = await client.read_gatt_char(BRIGHTNESS_CHAR)
        print(f"Read back: {value.hex()}  = {value[0]}, about {raw_to_percent(value[0])}%")


if __name__ == "__main__":
    percent = None
    if len(sys.argv) > 1:
        try:
            percent = int(sys.argv[1])
            assert 0 <= percent <= 100
        except (ValueError, AssertionError):
            sys.exit("usage: uv run brightness.py [0-100]")
    asyncio.run(main(percent))
