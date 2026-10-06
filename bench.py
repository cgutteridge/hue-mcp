# /// script
# requires-python = ">=3.11"
# dependencies = ["bleak>=3,<4", "webcolors>=25"]
# ///
"""
Step 6: speed test. How fast can we change the bulb over Bluetooth, and can the
bulb do fades itself? Run from this folder with:   uv run bench.py

Takes about a minute. The bulb will flicker, change colour and fade a few times, then go
back to how it was. Results print as a summary and are saved to bench-results.json.

It's fine if Claude's hue server is connected at the same time: macOS shares the link.
What it found on an LCA001 is in the README ("What we found on the bulb").
"""

import asyncio
import json
import statistics
import time
from pathlib import Path

from bleak import BleakClient

from hue_bulb import parse_color, percent_to_raw, raw_to_percent
from hue_config import (BRIGHTNESS_CHAR, COLOR_CHAR, POWER_CHAR, STATE_CHAR,
                        TEMPERATURE_CHAR, load_address)
results: dict = {}


def now() -> float:
    return time.perf_counter()


def ms(seconds: float) -> float:
    return round(seconds * 1000, 1)


def summary(samples: list[float]) -> dict:
    s = sorted(samples)
    return {"n": len(s), "median_ms": ms(statistics.median(s)), "p90_ms": ms(s[int(len(s) * 0.9) - 1]),
            "min_ms": ms(s[0]), "max_ms": ms(s[-1])}


async def bright(client: BleakClient, percent: int, response: bool = True) -> None:
    await client.write_gatt_char(BRIGHTNESS_CHAR, bytes([percent_to_raw(percent)]), response=response)


async def read_bright(client: BleakClient) -> int:
    return raw_to_percent((await client.read_gatt_char(BRIGHTNESS_CHAR))[0])


async def sample_curve(client: BleakClient, seconds: float) -> list[tuple[float, int]]:
    """Read brightness back as fast as possible to see how the bulb moves between values."""
    start, curve = now(), []
    while now() - start < seconds:
        curve.append((ms(now() - start), await read_bright(client)))
    return curve


def curve_duration(curve: list[tuple[float, int]], target: int) -> float | None:
    """Time until the read-back brightness first reaches (within 2%) the target."""
    for t, v in curve:
        if abs(v - target) <= 2:
            return t
    return None


async def test_latency(client: BleakClient) -> None:
    print("\n2. Single-write round trip (20 each, with and without waiting for the bulb's ack)")
    for response in (True, False):
        times = []
        for i in range(20):
            t = now()
            await bright(client, 40 if i % 2 else 60, response=response)
            times.append(now() - t)
        key = "write_with_ack" if response else "write_no_ack"
        results[key] = summary(times)
        print(f"   {key:16} {results[key]}")
    t = now()
    await read_bright(client)
    results["single_read_ms"] = ms(now() - t)


async def test_max_rate(client: BleakClient) -> None:
    print("\n3. Maximum sustained rate (3 s of brightness writes, acked)")
    count, start = 0, now()
    while now() - start < 3:
        await bright(client, 30 + (count % 2) * 40)
        count += 1
    rate = count / (now() - start)
    results["max_writes_per_sec_acked"] = round(rate, 1)
    print(f"   {rate:.1f} writes/s")


async def test_default_fade(client: BleakClient) -> None:
    print("\n4. The bulb's built-in fade (10% -> 90%, reading back)")
    await bright(client, 10)
    await asyncio.sleep(1.5)
    t0 = now()
    await bright(client, 90)
    curve = await sample_curve(client, 2.0)
    results["default_fade"] = {"reached_target_after_ms": curve_duration(curve, 90),
                               "write_ms": None, "curve": curve}
    distinct = len({v for _, v in curve})
    results["default_fade"]["distinct_readback_values"] = distinct
    print(f"   reached 90% after ~{results['default_fade']['reached_target_after_ms']} ms "
          f"({distinct} distinct read-back values; 1-2 means read-back shows the target, not the fade)")


async def test_native_transition(client: BleakClient) -> None:
    """
    The experiment that found bulb-side fades: write the same type-length-value records 0007
    reports, plus type 5 = transition time, trying two plausible units (100 ms steps, and
    milliseconds) for a 3-second fade, and watch what happens. (100 ms steps won.)
    """
    print("\n5. Can the bulb do a long fade itself? (probing characteristic 0007)")
    try:
        raw = bytes(await client.read_gatt_char(STATE_CHAR))
        results["state_char_read_hex"] = raw.hex()
        print(f"   0007 reads as {raw.hex()}")
    except Exception as err:
        results["state_char_read_hex"] = f"error: {err}"

    attempts = {"type5_deciseconds_30": (30).to_bytes(2, "little"),
                "type5_milliseconds_3000": (3000).to_bytes(2, "little")}
    results["native_transition"] = {}
    for name, tt in attempts.items():
        await bright(client, 10)
        await asyncio.sleep(1.5)
        packet = bytes([0x02, 0x01, percent_to_raw(90), 0x05, 0x02]) + tt
        entry: dict = {"packet_hex": packet.hex()}
        try:
            await client.write_gatt_char(STATE_CHAR, packet, response=True)
            entry["write"] = "accepted"
            curve = await sample_curve(client, 4.0)
            entry["reached_target_after_ms"] = curve_duration(curve, 90)
            entry["curve"] = curve
        except Exception as err:
            entry["write"] = f"rejected: {err}"
        results["native_transition"][name] = entry
        print(f"   {name}: {entry['write']}, reached 90% after ~{entry.get('reached_target_after_ms')} ms "
              f"(~3000 would mean the bulb did a 3 s fade itself)")


async def test_stepped_fade(client: BleakClient) -> None:
    print("\n6. Software fade: 2 s from 10% to 90%, one acknowledged write per step")
    await bright(client, 10)
    await asyncio.sleep(1)
    duration, stamps, start = 2.0, [], now()
    while (elapsed := now() - start) < duration:
        pct = round(10 + 80 * elapsed / duration)
        await bright(client, pct)  # acked: unacked writes just pile up in macOS's queue
        stamps.append(now() - start)
    await bright(client, 90)
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    results["stepped_fade_2s"] = {"steps": len(stamps), "gap": summary(gaps)}
    print(f"   {len(stamps)} steps in 2 s, gap between steps {results['stepped_fade_2s']['gap']}")


async def test_sequence(client: BleakClient) -> None:
    print("\n7. Sequence: 6 colours, 400 ms apart, timed locally (what a run_sequence tool would do)")
    colors = ["red", "orange", "yellow", "green", "blue", "purple"]
    await bright(client, 70)
    start, drift = now(), []
    for i, c in enumerate(colors):
        due = start + i * 0.4
        await asyncio.sleep(max(0, due - now()))
        char, data = parse_color(c)
        await client.write_gatt_char(char, data, response=True)
        drift.append(now() - due)
    results["sequence_6x400ms"] = {"lateness": summary(drift), "total_ms": ms(now() - start)}
    print(f"   each step landed late by {results['sequence_6x400ms']['lateness']}")


async def main() -> None:
    address = load_address()
    print(f"1. Connecting to {address} ...")
    t = now()
    async with BleakClient(address, timeout=20) as client:
        results["connect_ms"] = ms(now() - t)
        print(f"   connected in {results['connect_ms']} ms")

        # Remember the starting state so we can put it back.
        saved = {c: bytes(await client.read_gatt_char(c))
                 for c in (POWER_CHAR, BRIGHTNESS_CHAR, TEMPERATURE_CHAR, COLOR_CHAR)}
        await client.write_gatt_char(POWER_CHAR, b"\x01", response=True)
        try:
            for test in (test_latency, test_max_rate, test_default_fade,
                         test_native_transition, test_stepped_fade, test_sequence):
                try:
                    await test(client)
                except Exception as err:
                    results[f"{test.__name__}_error"] = repr(err)
                    print(f"   FAILED: {err!r}")
        finally:
            print("\nRestoring the bulb to how it was ...")
            in_white = saved[COLOR_CHAR] == b"\xff\xff\xff\xff"
            await client.write_gatt_char(TEMPERATURE_CHAR if in_white else COLOR_CHAR,
                                         saved[TEMPERATURE_CHAR if in_white else COLOR_CHAR], response=True)
            await client.write_gatt_char(BRIGHTNESS_CHAR, saved[BRIGHTNESS_CHAR], response=True)
            await client.write_gatt_char(POWER_CHAR, saved[POWER_CHAR], response=True)

    out = Path(__file__).with_name("bench-results.json")
    out.write_text(json.dumps(results, indent=2))
    print(f"\nDone. Full results saved to {out.name}")


if __name__ == "__main__":
    asyncio.run(main())
