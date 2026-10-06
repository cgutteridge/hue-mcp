# /// script
# requires-python = ">=3.11"
# dependencies = ["bleak>=3,<4"]
# ///
"""
Step 3 — Pairing: get macOS to pair (bond) with the bulb, then read the power state.

Run it with:   uv run pair.py            (address from hue.toml)
          or:  uv run pair.py <address>  (e.g. straight after a reset, before editing hue.toml)

Why this is needed: step 2 showed the light-control characteristics refuse to talk to us
with "Insufficient Encryption". The bulb only allows them over an encrypted link, and the
link is only encrypted once the two devices are *paired* (they've swapped keys).

On macOS we can't ask for pairing directly from Python. Instead, CoreBluetooth (Apple's
Bluetooth layer) starts pairing by itself when we touch a protected characteristic. So
this script simply tries to read one. If the bulb accepts, macOS shows a "Bluetooth
Pairing Request" pop-up; click Connect/Pair. After that, macOS remembers the keys and
future connections are encrypted automatically.

The bulb only accepts NEW pairings while it's in pairing mode (see README / chat notes).
This script only reads; it doesn't change the light.
"""

import asyncio
import sys

from bleak import BleakClient

from hue_config import POWER_CHAR, load_address

ATTEMPTS = 3  # give you time to click the macOS pop-up


async def main(address):
    print(f"Connecting to {address} ...")
    async with BleakClient(address, timeout=20) as client:
        print("Connected. Reading the power characteristic (this should trigger pairing)...")

        for attempt in range(1, ATTEMPTS + 1):
            try:
                value = await client.read_gatt_char(POWER_CHAR)
            except Exception as err:
                print(f"  attempt {attempt}: {type(err).__name__}: {err}")
                if attempt < ATTEMPTS:
                    print("  (if a pairing pop-up appeared, accept it; retrying in 5 s)")
                    await asyncio.sleep(5)
                continue

            # Success: we're paired and the link is encrypted.
            print(f"\nPaired! Raw power value: {value.hex()}")
            if value == b"\x01":
                print("That looks like ON (01).")
            elif value == b"\x00":
                print("That looks like OFF (00).")
            else:
                print("Unexpected value; we'll decode it in step 4.")
            return

        print("\nStill locked. The bulb probably isn't in pairing mode; see the chat for next steps.")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else load_address()))
