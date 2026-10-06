# /// script
# requires-python = ">=3.11"
# dependencies = ["bleak>=3,<4"]
# ///
"""
Step 4a — On/off: switch the bulb on or off, then read the state back to check.

Run it with:   uv run power.py on
               uv run power.py off
               uv run power.py          (just report the current state)

Needs the pairing from step 3 (macOS remembers it, so no pop-up this time).

The power characteristic holds one byte: 01 = on, 00 = off.
Writing that byte changes the light; reading it tells us the current state.
"""

import asyncio
import sys

from bleak import BleakClient

from hue_config import POWER_CHAR, load_address

ON, OFF = b"\x01", b"\x00"


async def main(command):
    async with BleakClient(load_address(), timeout=20) as client:
        if command in ("on", "off"):
            # response=True asks the bulb to acknowledge the write, so an error
            # (e.g. lost pairing) shows up here instead of failing silently.
            await client.write_gatt_char(POWER_CHAR, ON if command == "on" else OFF, response=True)
            print(f"Sent: {command}")

        value = await client.read_gatt_char(POWER_CHAR)
        state = {ON: "on", OFF: "off"}.get(bytes(value), f"unknown ({value.hex()})")
        print(f"Bulb is now: {state}")


if __name__ == "__main__":
    arg = sys.argv[1].lower() if len(sys.argv) > 1 else None
    if arg not in (None, "on", "off"):
        sys.exit("usage: uv run power.py [on|off]")
    asyncio.run(main(arg))
