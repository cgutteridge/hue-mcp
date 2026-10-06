"""
Everything about talking to the bulb, with no MCP in sight.

Two parts:
1. Colour conversions: friendly colours (CSS names, hex, "2700K") <-> the bulb's bytes.
2. HueBulb: one long-lived Bluetooth connection, reconnecting when it drops.

server.py is a thin MCP layer on top of this, the same split as mcp-lamp
(where the "device" was a state file plus a signal).
"""

import asyncio
import logging
import re
from dataclasses import dataclass

import webcolors
from bleak import BleakClient

from hue_config import BRIGHTNESS_CHAR, COLOR_CHAR, POWER_CHAR, TEMPERATURE_CHAR

log = logging.getLogger("hue-mcp")  # logging goes to stderr, never stdout

MIN_MIREDS, MAX_MIREDS = 153, 500   # this bulb's white range: ~6500 K to 2000 K
MIN_BRIGHT, MAX_BRIGHT = 1, 254
XY_NOT_SET = b"\xff\xff\xff\xff"    # what 0005 reads while the bulb is in white mode

KELVIN_PATTERN = re.compile(r"^\s*(\d{4,5})\s*k\s*$", re.IGNORECASE)  # e.g. "2700K"


# ── 1. Colour conversions ──────────────────────────────────────────────────────

def srgb_to_xy(r: int, g: int, b: int) -> tuple[float, float]:
    """Screen colour (0-255 per channel) -> CIE xy, using the standard sRGB maths."""
    def linear(c: int) -> float:  # undo the screen "gamma" curve
        c = c / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = linear(r), linear(g), linear(b)
    X = 0.4124 * r + 0.3576 * g + 0.1805 * b
    Y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    Z = 0.0193 * r + 0.1192 * g + 0.9505 * b
    total = X + Y + Z
    if total == 0:
        raise ValueError("black has no colour; switch the bulb off instead")
    return X / total, Y / total


def xy_to_hex(x: float, y: float) -> str:
    """CIE xy -> approximate screen colour, at full brightness (the reverse of the above)."""
    if y == 0:
        return "#000000"
    X, Y, Z = x / y, 1.0, (1 - x - y) / y
    r = 3.2406 * X - 1.5372 * Y - 0.4986 * Z
    g = -0.9689 * X + 1.8758 * Y + 0.0415 * Z
    b = 0.0557 * X - 0.2040 * Y + 1.0570 * Z
    r, g, b = (max(c, 0.0) for c in (r, g, b))
    peak = max(r, g, b) or 1.0                  # scale so the brightest channel is 1
    def gamma(c: float) -> int:
        c = c / peak
        c = 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055
        return round(min(max(c, 0.0), 1.0) * 255)
    return "#{:02x}{:02x}{:02x}".format(gamma(r), gamma(g), gamma(b))


def parse_color(color: str) -> tuple[str, bytes]:
    """
    Turn a friendly colour into (characteristic, bytes to write).
    Accepts "2700K" (a shade of white), a CSS name like "tomato", or hex like "#ff8800".
    """
    if match := KELVIN_PATTERN.match(color):
        mireds = round(1_000_000 / int(match.group(1)))
        mireds = max(MIN_MIREDS, min(MAX_MIREDS, mireds))
        return TEMPERATURE_CHAR, mireds.to_bytes(2, "little")

    try:
        hex_value = color if color.startswith("#") else webcolors.name_to_hex(color.strip().lower())
        rgb = webcolors.hex_to_rgb(hex_value)
    except ValueError:
        raise ValueError(f'"{color}" is not a CSS colour name, a hex value like "#ff8800", or a white like "2700K"')
    x, y = srgb_to_xy(*rgb)
    return COLOR_CHAR, round(x * 65535).to_bytes(2, "little") + round(y * 65535).to_bytes(2, "little")


def percent_to_raw(percent: int) -> int:
    return round(MIN_BRIGHT + (MAX_BRIGHT - MIN_BRIGHT) * percent / 100)


def raw_to_percent(raw: int) -> int:
    return round((raw - MIN_BRIGHT) * 100 / (MAX_BRIGHT - MIN_BRIGHT))


# ── 2. The bulb ────────────────────────────────────────────────────────────────

@dataclass
class HueState:
    on: bool
    color: str       # "#rrggbb" in colour mode, or e.g. "2732K" in white mode
    brightness: int  # 0-100 %


class HueBulb:
    """
    Keeps one Bluetooth connection open between tool calls: connecting takes a second
    or two, so reconnecting for every call would make the tools feel sluggish.
    """

    def __init__(self, address: str):
        self.address = address
        self._client: BleakClient | None = None
        # One conversation with the bulb at a time, even if two tool calls overlap.
        self._lock = asyncio.Lock()

    async def _connected(self) -> BleakClient:
        """Return a connected client, (re)connecting if needed."""
        if self._client is None or not self._client.is_connected:
            log.info("connecting to %s", self.address)
            self._client = BleakClient(
                self.address,
                timeout=20,
                disconnected_callback=lambda _: log.info("bulb disconnected"),
            )
            await self._client.connect()
            log.info("connected")
        return self._client

    async def _run(self, operation):
        """Run operation(client), reconnecting and retrying once if the link has dropped."""
        async with self._lock:
            try:
                return await operation(await self._connected())
            except Exception as err:
                log.warning("bulb call failed (%s); reconnecting and retrying once", err)
                await self.disconnect()
                return await operation(await self._connected())

    async def get_state(self) -> HueState:
        async def read(client: BleakClient) -> HueState:
            power = await client.read_gatt_char(POWER_CHAR)
            bright = await client.read_gatt_char(BRIGHTNESS_CHAR)
            xy = bytes(await client.read_gatt_char(COLOR_CHAR))
            if xy == XY_NOT_SET:  # white mode: report the colour temperature instead
                mireds = int.from_bytes(await client.read_gatt_char(TEMPERATURE_CHAR), "little")
                color = f"{round(1_000_000 / mireds)}K"
            else:
                color = xy_to_hex(int.from_bytes(xy[0:2], "little") / 65535,
                                  int.from_bytes(xy[2:4], "little") / 65535)
            return HueState(on=power[0] == 1, color=color, brightness=raw_to_percent(bright[0]))

        return await self._run(read)

    async def set_state(self, on: bool | None, color: str | None, brightness: int | None) -> HueState:
        # Validate everything before touching the bulb, so a bad colour changes nothing.
        writes: list[tuple[str, bytes]] = []
        if on is not None:
            writes.append((POWER_CHAR, b"\x01" if on else b"\x00"))
        if color is not None:
            writes.append(parse_color(color))
        if brightness is not None:
            writes.append((BRIGHTNESS_CHAR, bytes([percent_to_raw(brightness)])))

        async def write(client: BleakClient) -> None:
            for char, data in writes:
                await client.write_gatt_char(char, data, response=True)

        await self._run(write)
        await asyncio.sleep(0.5)  # the bulb fades to new values; let it settle before reading back
        return await self.get_state()

    async def disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass  # already gone
            self._client = None


# ── 3. Fast path: the whole-state characteristic (0007) ────────────────────────
#
# Found by bench.py on this bulb (LCA001, firmware 1.116.5):
# - 0007 reads as type-length-value records: 1 power, 2 brightness, 3 mireds, 4 xy.
#   One read gives the whole state (~90 ms) instead of 3-4 separate reads.
# - Writing records to 0007 works too, and type 5 is the TRANSITION TIME in units of
#   100 ms (2 bytes LE). The bulb then fades smoothly by itself: no stream of writes needed.
#   (We tried milliseconds as well: the bulb read 3000 as 3000 x 100 ms = a 5-minute fade.)
# Verified for brightness. Colour and white records in the same packet are assumed to
# behave the same way; set_state_fast falls back to the separate characteristics if not.

STATE_CHAR = "932c32bd-0007-47a2-835a-a8d455b859dd"
REC_POWER, REC_BRIGHT, REC_MIREDS, REC_XY, REC_TRANSITION = 1, 2, 3, 4, 5
MAX_TRANSITION_MS = 65535 * 100  # ~1.8 hours

CHAR_TO_RECORD = {POWER_CHAR: REC_POWER, BRIGHTNESS_CHAR: REC_BRIGHT,
                  TEMPERATURE_CHAR: REC_MIREDS, COLOR_CHAR: REC_XY}


def decode_records(raw: bytes) -> dict[int, bytes]:
    records, i = {}, 0
    while i + 2 <= len(raw):
        kind, length = raw[i], raw[i + 1]
        records[kind] = raw[i + 2:i + 2 + length]
        i += 2 + length
    return records


def encode_records(writes: list[tuple[str, bytes]], transition_ms: int | None) -> bytes:
    packet = b"".join(bytes([CHAR_TO_RECORD[char], len(data)]) + data for char, data in writes)
    if transition_ms is not None:
        tenths = max(0, min(65535, round(transition_ms / 100)))
        packet += bytes([REC_TRANSITION, 2]) + tenths.to_bytes(2, "little")
    return packet


def state_from_records(records: dict[int, bytes]) -> HueState | None:
    if REC_POWER not in records or REC_BRIGHT not in records:
        return None
    xy = records.get(REC_XY)
    if xy and xy != XY_NOT_SET:
        color = xy_to_hex(int.from_bytes(xy[0:2], "little") / 65535, int.from_bytes(xy[2:4], "little") / 65535)
    elif REC_MIREDS in records:
        color = f"{round(1_000_000 / int.from_bytes(records[REC_MIREDS], 'little'))}K"
    else:
        return None
    return HueState(on=records[REC_POWER][0] == 1, color=color, brightness=raw_to_percent(records[REC_BRIGHT][0]))


def plan_writes(on: bool | None, color: str | None, brightness: int | None) -> list[tuple[str, bytes]]:
    """Validate and convert a request into (characteristic, bytes) pairs. Raises ValueError on a bad colour."""
    writes: list[tuple[str, bytes]] = []
    if on is not None:
        writes.append((POWER_CHAR, b"\x01" if on else b"\x00"))
    if color is not None:
        writes.append(parse_color(color))
    if brightness is not None:
        writes.append((BRIGHTNESS_CHAR, bytes([percent_to_raw(brightness)])))
    return writes


class FastHueBulb(HueBulb):
    """HueBulb plus: one-read state, bulb-side fades, and no fixed half-second wait."""

    async def get_state(self) -> HueState:
        async def read(client: BleakClient) -> HueState | None:
            return state_from_records(decode_records(bytes(await client.read_gatt_char(STATE_CHAR))))
        state = await self._run(read)
        return state if state is not None else await super().get_state()  # old 3-4 read method

    async def write(self, writes: list[tuple[str, bytes]], transition_ms: int | None) -> None:
        """Send everything in one 0007 packet; fall back to separate writes if the bulb refuses."""
        if not writes:
            return
        packet = encode_records(writes, transition_ms)

        async def send(client: BleakClient) -> None:
            try:
                await client.write_gatt_char(STATE_CHAR, packet, response=True)
            except Exception as err:
                log.warning("0007 write refused (%s); using separate characteristics, no custom fade", err)
                for char, data in writes:
                    await client.write_gatt_char(char, data, response=True)

        await self._run(send)

    async def set_state_fast(self, on: bool | None, color: str | None, brightness: int | None,
                             fade_ms: int | None = None, wait: bool = True) -> HueState:
        writes = plan_writes(on, color, brightness)  # validates before touching the bulb
        await self.write(writes, fade_ms)
        if not wait:
            return await self.get_state()
        # Wait for the fade to finish (built-in fade is ~0.36 s) only when it's short.
        settle = (fade_ms if fade_ms is not None else 400) / 1000
        if settle <= 1.5:
            await asyncio.sleep(settle)
        return await self.get_state()
