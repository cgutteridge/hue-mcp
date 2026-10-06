# /// script
# requires-python = ">=3.11"
# dependencies = ["bleak>=3,<4"]
# ///
"""
Step 2 — Connect and explore: list the bulb's GATT services and characteristics.

Run it with:   uv run explore.py
          or:  uv run explore.py <address>     (overrides hue.toml)

Some vocabulary:
- GATT is how a connected BLE device organises what it offers.
- A *service* is a group of related features (e.g. "light control"), identified by a UUID.
- A *characteristic* is one value inside a service (e.g. "power" or "brightness"). Each has
  *properties* saying what you may do with it: read, write, notify (push updates to us)...

This script only READS. It never writes to the bulb, so it can't change its state.
If reads fail with an authentication/permission error, that's expected before pairing
(step 3) and still tells us something useful.
"""

import asyncio
import sys

from bleak import BleakClient

from hue_config import LIGHT_SERVICE as HUE_LIGHT_SERVICE, load_address



async def main(address):
    print(f"Connecting to {address} ...")
    # "async with" connects on entry and disconnects on exit, even if something fails.
    async with BleakClient(address, timeout=20) as client:
        print("Connected.\n")

        found_hue_service = False
        for service in client.services:
            if service.uuid.lower() == HUE_LIGHT_SERVICE:
                found_hue_service = True
                flag = "   <-- Hue light control (per the notes)"
            else:
                flag = ""
            print(f"[Service] {service.uuid}  {service.description}{flag}")

            for char in service.characteristics:
                props = ", ".join(char.properties)
                print(f"    [Char] {char.uuid}  ({props})  {char.description}")

                # Try to read anything readable, and show the raw bytes. We decode them
                # later, once we know which characteristic is which.
                if "read" in char.properties:
                    try:
                        value = await client.read_gatt_char(char)
                        print(f"           value: {value.hex() or '(empty)'}  {printable(value)}")
                    except Exception as err:
                        print(f"           read failed: {type(err).__name__}: {err}")
            print()

        print("Hue light service found." if found_hue_service
              else "Hue light service NOT found; the notes may be wrong for this bulb.")


def printable(value: bytes) -> str:
    """Show bytes as text too, if they look like text (handy for names/versions)."""
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        return ""
    return f'"{text}"' if text and text.isprintable() else ""


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else load_address()))
