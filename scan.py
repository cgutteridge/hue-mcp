# /// script
# requires-python = ">=3.11"
# dependencies = ["bleak>=3,<4"]
# ///
"""
Step 1 — Discover: scan for Bluetooth Low Energy (BLE) devices and flag likely Hue bulbs.

Run it with:   uv run scan.py
(uv reads the "script" block above, installs bleak in a throwaway environment, and runs this.)

How BLE discovery works, in one paragraph:
Devices shout short "advertisement" packets a few times a second. Each packet can carry a
name, a list of service UUIDs (what the device can do), and "manufacturer data" tagged with
a company ID. We just listen for a few seconds and look at what we heard. We don't connect
to anything yet — that's step 2.
"""

import asyncio

from bleak import BleakScanner

SCAN_SECONDS = 10.0

# Clues that a device might be a Hue bulb. These are educated guesses to be checked
# against the real bulb, which is the whole point of this step.
from hue_config import LIGHT_SERVICE as HUE_LIGHT_SERVICE  # verified on the real bulb
SIGNIFY_SHORT_SERVICE = "0000fe0f-0000-1000-8000-00805f9b34fb"  # 16-bit UUID 0xFE0F, registered to Philips/Signify
SIGNIFY_COMPANY_ID = 0x010F  # Bluetooth SIG company ID believed to be Signify


def hue_clues(device, adv):
    """Return a list of reasons this device looks like a Hue bulb (empty list = no clues)."""
    clues = []
    name = (adv.local_name or device.name or "").lower()
    if "hue" in name or "philips" in name:
        clues.append("name")
    uuids = [u.lower() for u in adv.service_uuids]
    if HUE_LIGHT_SERVICE in uuids:
        clues.append("hue light service")
    if SIGNIFY_SHORT_SERVICE in uuids:
        clues.append("Signify service FE0F")
    if SIGNIFY_COMPANY_ID in adv.manufacturer_data:
        clues.append("Signify manufacturer data")
    return clues


async def main():
    print(f"Scanning for {SCAN_SECONDS:.0f} seconds... (keep the bulb powered on)")

    # return_adv=True gives us {address: (device, advertisement_data)} rather than
    # just devices, so we can see service UUIDs, signal strength and so on.
    found = await BleakScanner.discover(timeout=SCAN_SECONDS, return_adv=True)

    # Strongest signal (closest device) first. RSSI is in dBm: -40 is close, -90 is far.
    rows = sorted(found.values(), key=lambda pair: pair[1].rssi, reverse=True)

    likely = []
    print(f"\nHeard {len(rows)} devices:\n")
    for device, adv in rows:
        clues = hue_clues(device, adv)
        marker = "  <-- possible Hue" if clues else ""
        name = adv.local_name or device.name or "(no name)"
        # Note: on macOS, device.address is a per-Mac UUID, not the real MAC address.
        print(f"{adv.rssi:>4} dBm  {device.address}  {name}{marker}")
        if clues:
            likely.append((device, adv, clues))

    print()
    if not likely:
        print("No likely Hue bulbs found.")
        print("If the list above is empty, check System Settings > Privacy & Security >")
        print("Bluetooth and allow your terminal app, then run this again.")
        return

    print("Details for possible Hue bulbs:\n")
    for device, adv, clues in likely:
        print(f"  {adv.local_name or device.name or '(no name)'}  [{device.address}]")
        print(f"    why:           {', '.join(clues)}")
        print(f"    signal:        {adv.rssi} dBm")
        print(f"    service UUIDs: {adv.service_uuids or 'none advertised'}")
        for company_id, data in adv.manufacturer_data.items():
            print(f"    manufacturer:  0x{company_id:04X} -> {data.hex()}")
        print()

    print("Copy the right address into hue.toml:  address = \"<address>\"")


if __name__ == "__main__":
    asyncio.run(main())
