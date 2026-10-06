"""
Lights a pattern can play on, besides the real bulb (hue_bulb.HueBulb).
Anything with the same apply() method works: copy one of these to drive your own device.
"""

import json
import os
import signal
from pathlib import Path


class SimLight:
    """Prints a timeline instead of changing anything: try patterns with no hardware at all."""

    def __init__(self, clock):
        self.clock = clock
        self.start = clock.now()

    async def apply(self, on, color, brightness, fade_ms) -> None:
        parts = [f"on={str(on).lower()}" if on is not None else "",
                 f"color={color}" if color is not None else "",
                 f"brightness={brightness}%" if brightness is not None else "",
                 f"fading {fade_ms} ms" if fade_ms else ""]
        print(f"{self.clock.now() - self.start:9.3f} s  " + "  ".join(p for p in parts if p), flush=True)


def kelvin_to_hex(kelvin: int) -> str:
    """Rough screen colour for a shade of white (Tanner Helland's approximation)."""
    import math
    t = kelvin / 100
    r = 255 if t <= 66 else 329.7 * (t - 60) ** -0.1332
    g = 99.47 * math.log(t) - 161.12 if t <= 66 else 288.12 * (t - 60) ** -0.0755
    b = 255 if t >= 66 else (0 if t <= 19 else 138.52 * math.log(t - 10) - 305.04)
    return "#{:02x}{:02x}{:02x}".format(*(round(min(255, max(0, c))) for c in (r, g, b)))


class LampLight:
    """
    The on-screen lamp from ../mcp-lamp: write its state file, then poke it with SIGUSR2.
    Its page always fades over 0.6 s, so fade_ms only affects the pattern's timing here.
    """

    def __init__(self):
        self.dir = Path(os.environ.get("LAMP_DIR", Path.home() / ".mcp-lamp"))

    async def apply(self, on, color, brightness, fade_ms) -> None:
        state_file = self.dir / "state.json"
        try:
            state = json.loads(state_file.read_text())
        except (OSError, ValueError):
            state = {"on": True, "color": "#ffcc33", "brightness": 80}
        if on is not None:
            state["on"] = on
        if color is not None:
            state["color"] = kelvin_to_hex(int(color.strip()[:-1])) if color.strip().upper().endswith("K") else color
        if brightness is not None:
            state["brightness"] = brightness
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2) + "\n")
        tmp.replace(state_file)
        try:
            os.kill(int((self.dir / "lamp.pid").read_text()), signal.SIGUSR2)
        except (OSError, ValueError):
            raise RuntimeError("the lamp window isn't open: run `npm run lamp` in mcp-lamp")
