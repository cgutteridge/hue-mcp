# /// script
# requires-python = ">=3.11"
# dependencies = ["bleak>=3,<4"]
# ///
"""
Step 4c (part 1) — Probe colour: read every characteristic in the light service and show
the raw bytes several ways, so we can work out the colour formats.

Run it with:   uv run color_probe.py

Read-only: it doesn't change the light.

Why show numbers "several ways"? A characteristic is just bytes; the meaning is up to
the device. Colour is probably more than one byte, so we try the usual guesses:
- uint16 little-endian: pairs of bytes as numbers 0-65535, low byte first (the BLE norm).
  If the colour is CIE xy, we'd expect two of these, and dividing by 65535 should give
  x and y between 0 and ~0.8.
- mireds: colour temperature is often stored as "mireds" = 1,000,000 / kelvin,
  roughly 153 (cool, 6500 K) to 500 (warm, 2000 K).
"""

import asyncio

from bleak import BleakClient

from hue_config import LIGHT_SERVICE, load_address

# What we know or suspect about each characteristic (short ID = the 4 hex digits after 932c32bd-).
KNOWN = {
    "0001": "unknown",
    "0002": "power (verified)",
    "0003": "brightness (verified)",
    "0004": "colour temperature? (notes)",
    "0005": "colour xy? (notes)",
    "0006": "unknown, write-only",
    "0007": "unknown, maybe combined state?",
    "1005": "unknown",
}


def interpretations(value: bytes) -> list[str]:
    """Show the bytes as a few plausible number formats."""
    out = [f"bytes: {' '.join(f'{b:02x}' for b in value)}  (decimal {list(value)})"]
    if len(value) >= 2 and len(value) % 2 == 0:
        nums = [int.from_bytes(value[i:i + 2], "little") for i in range(0, len(value), 2)]
        out.append(f"uint16 LE: {nums}")
        out.append(f"  /65535:  {[round(n / 65535, 4) for n in nums]}   (CIE xy would be 0-0.8)")
        if 100 <= nums[0] <= 600:
            out.append(f"  as mireds: {nums[0]} -> about {round(1_000_000 / nums[0])} K")
    return out


async def main():
    async with BleakClient(load_address(), timeout=20) as client:
        service = client.services.get_service(LIGHT_SERVICE)
        for char in service.characteristics:
            short = char.uuid[9:13]  # "932c32bd-0005-..." -> "0005"
            print(f"[{short}] {KNOWN.get(short, 'unknown')}   ({', '.join(char.properties)})")
            if "read" not in char.properties:
                print("        (not readable)\n")
                continue
            try:
                value = await client.read_gatt_char(char)
            except Exception as err:
                print(f"        read failed: {type(err).__name__}: {err}\n")
                continue
            for line in interpretations(bytes(value)):
                print(f"        {line}")
            print()


if __name__ == "__main__":
    asyncio.run(main())
