"""
Light patterns: a small, data-only format for changes, waits, fades and loops, plus a player.

This file knows nothing about MCP or Bluetooth. A pattern plays on any "light" that has

    async def apply(on, color, brightness, fade_ms) -> None

so the same pattern can drive the real bulb (hue_bulb.HueBulb), the on-screen mcp-lamp, or a
simulated light that just prints a timeline (lights.py). The format, with an example:

    [
      {"loop": [
        {"color": "red"},                          a change: any of on / color / brightness,
        {"wait_ms": 5000},                         plus an optional fade_ms
        {"color": "green", "fade_ms": 1000},       a fade holds the pattern until it finishes
        {"wait_ms": 1000}
      ], "for_ms": 60000},                         repeat that 7 s loop for a minute...
      {"on": false}                                ...then switch off
    ]

- A step is a change, a wait ({"wait_ms": ...}) or a loop ({"loop": [steps], ...}).
- A loop needs "times" (a count, {"min", "max"}, or "forever") and/or "for_ms" (a time limit).
  With both, it stops at whichever comes first. A time limit also stops any loop inside it,
  even halfway through a wait.
- Numbers that can vary (wait_ms, fade_ms, brightness, times) take {"min": a, "max": b} for a
  random value, picked afresh each time the step runs. Random counts are inclusive.
- Without fade_ms the bulb does its short built-in fade, but the pattern moves straight on.
- Every loop body must take some time (a change, a wait or a fade), so nothing can spin.
- The player sends at most one change every MIN_GAP_MS, whatever the pattern says, because
  the Bluetooth link manages about 15 changes a second.
"""

from __future__ import annotations

import asyncio
import difflib
import math
import random
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

MIN_GAP_MS = 70             # at most ~14 changes a second (measured: ~60 ms per acknowledged write)
MAX_DEPTH = 8
MAX_STEPS = 500             # steps as written, not as played
MAX_MS = 7 * 24 * 3600 * 1000
MAX_FADE_MS = 65535 * 100   # the bulb's limit (fade time is 2 bytes of 100 ms steps)

Range = tuple[int, int]     # (min, max), inclusive; a fixed number is (n, n)


class PatternError(ValueError):
    """A pattern that can't be played. The message starts with where the problem is."""


# ── The parsed form ────────────────────────────────────────────────────────────

@dataclass
class Change:
    on: bool | None = None
    color: str | None = None
    brightness: Range | None = None
    fade_ms: Range | None = None


@dataclass
class Wait:
    ms: Range


@dataclass
class Loop:
    steps: list[Step]
    times: Range | None = None   # None means "forever"
    for_ms: int | None = None


Step = Change | Wait | Loop


# ── Parsing, with errors that say exactly where the problem is ─────────────────

CHANGE_KEYS = {"on", "color", "brightness", "fade_ms"}
KEYS = {"change": CHANGE_KEYS, "wait": {"wait_ms"}, "loop": {"loop", "times", "for_ms"}}
ALL_KEYS = set().union(*KEYS.values())


def parse(data: Any, check_color: Callable[[str], Any] | None = None) -> list[Step]:
    """
    Check a pattern (as decoded from JSON) and return it in parsed form, or raise PatternError.
    check_color is called on every colour and should raise ValueError for a bad one.
    """
    counter = [0]
    steps = _parse_steps(data, "pattern", 1, counter, check_color)
    if not steps:
        raise PatternError("pattern: is empty; give at least one step")
    return steps


def _parse_steps(data, path, depth, counter, check_color) -> list[Step]:
    if not isinstance(data, list):
        raise PatternError(f"{path}: should be a list of steps")
    if depth > MAX_DEPTH:
        raise PatternError(f"{path}: loops are nested more than {MAX_DEPTH} deep")
    return [_parse_step(item, f"{path}[{i}]", depth, counter, check_color) for i, item in enumerate(data)]


def _parse_step(data, path, depth, counter, check_color) -> Step:
    counter[0] += 1
    if counter[0] > MAX_STEPS:
        raise PatternError(f"{path}: too many steps (the limit is {MAX_STEPS}); use loops")
    if not isinstance(data, dict) or not data:
        raise PatternError(f"{path}: each step should be an object, e.g. {{\"color\": \"red\"}} or {{\"wait_ms\": 500}}")

    for key in data:
        if key not in ALL_KEYS:
            hint = difflib.get_close_matches(key, ALL_KEYS, n=1)
            raise PatternError(f"{path}: unknown key {key!r}" + (f" (did you mean {hint[0]!r}?)" if hint else "")
                               + f". A step can use: {', '.join(sorted(ALL_KEYS))}")
    kinds = [kind for kind, keys in KEYS.items() if keys & data.keys()]
    if len(kinds) > 1:
        raise PatternError(f"{path}: mixes a {kinds[0]} with a {kinds[1]} ({', '.join(data)}); "
                           "split it into separate steps")
    kind = kinds[0]

    if kind == "wait":
        return Wait(_range(data["wait_ms"], f"{path}.wait_ms", 0, MAX_MS))

    if kind == "change":
        if not {"on", "color", "brightness"} & data.keys():
            raise PatternError(f"{path}: fade_ms on its own changes nothing; add on, color or brightness")
        on = data.get("on")
        if on is not None and not isinstance(on, bool):
            raise PatternError(f"{path}.on: should be true or false")
        color = data.get("color")
        if color is not None:
            if not isinstance(color, str):
                raise PatternError(f"{path}.color: should be a string like \"red\", \"#ff8800\" or \"2700K\"")
            if check_color:
                try:
                    check_color(color)
                except ValueError as err:
                    raise PatternError(f"{path}.color: {err}")
        return Change(on=on, color=color,
                      brightness=_range(data["brightness"], f"{path}.brightness", 0, 100) if "brightness" in data else None,
                      fade_ms=_range(data["fade_ms"], f"{path}.fade_ms", 0, MAX_FADE_MS) if "fade_ms" in data else None)

    # a loop
    steps = _parse_steps(data["loop"], f"{path}.loop", depth + 1, counter, check_color)
    if not steps:
        raise PatternError(f"{path}.loop: is empty")
    if "times" not in data and "for_ms" not in data:
        raise PatternError(f"{path}: a loop needs \"times\" (a number, {{\"min\", \"max\"}} or \"forever\") "
                           "and/or \"for_ms\" (a time limit)")
    times = data.get("times", "forever")
    times = None if times == "forever" else _range(times, f"{path}.times", 0, 1_000_000)
    for_ms = None
    if "for_ms" in data:
        for_ms = data["for_ms"]
        if isinstance(for_ms, bool) or not isinstance(for_ms, int) or not 1 <= for_ms <= MAX_MS:
            raise PatternError(f"{path}.for_ms: should be a whole number of milliseconds, 1 to {MAX_MS}")
    loop = Loop(steps, times, for_ms)
    if duration(steps)[0] == 0:
        raise PatternError(f"{path}.loop: takes no time, so it would spin; add a wait_ms, a fade_ms or a change")
    return loop


def _range(value, path, lo, hi) -> Range:
    """A whole number, or {"min": a, "max": b}, within lo..hi."""
    if isinstance(value, dict):
        if set(value) != {"min", "max"}:
            raise PatternError(f"{path}: a random value is {{\"min\": a, \"max\": b}}")
        a, b = (_range(value[k], f"{path}.{k}", lo, hi)[0] for k in ("min", "max"))
        if a > b:
            raise PatternError(f"{path}: min ({a}) is bigger than max ({b})")
        return a, b
    if isinstance(value, bool) or not isinstance(value, int):
        if path.endswith(".times") and isinstance(value, str):
            raise PatternError(f"{path}: should be a number, {{\"min\", \"max\"}} or \"forever\"")
        raise PatternError(f"{path}: should be a whole number (or {{\"min\": a, \"max\": b}})")
    if not lo <= value <= hi:
        raise PatternError(f"{path}: {value} is out of range ({lo} to {hi})")
    return value, value


# ── How long will it take? ─────────────────────────────────────────────────────

def duration(steps: list[Step]) -> tuple[float, float]:
    """Shortest and longest running time in ms (math.inf for "until stopped")."""
    lo = hi = 0.0
    for step in steps:
        if isinstance(step, Change):
            fade = step.fade_ms or (0, 0)
            lo, hi = lo + max(MIN_GAP_MS, fade[0]), hi + max(MIN_GAP_MS, fade[1])
        elif isinstance(step, Wait):
            lo, hi = lo + step.ms[0], hi + step.ms[1]
        else:
            body_lo, body_hi = duration(step.steps)
            times = step.times or (math.inf, math.inf)
            loop_lo = 0 if times[0] == 0 else body_lo * times[0]
            loop_hi = 0 if times[1] == 0 else body_hi * times[1]
            if step.for_ms is not None:
                loop_lo, loop_hi = min(loop_lo, step.for_ms), min(loop_hi, step.for_ms)
            lo, hi = lo + loop_lo, hi + loop_hi
    return lo, hi


def describe(steps: list[Step]) -> str:
    lo, hi = duration(steps)
    if hi == math.inf:
        return "runs until stopped" if lo == math.inf else f"runs at least {_secs(lo)}, possibly until stopped"
    if hi - lo < 50:
        return f"takes about {_secs(hi)}"
    return f"takes {_secs(lo)} to {_secs(hi)}"


def _secs(ms: float) -> str:
    s = ms / 1000
    if s < 120:
        return f"{s:.1f} s".replace(".0 s", " s")
    return f"{s / 60:.1f} min".replace(".0 min", " min")


# ── Playing ────────────────────────────────────────────────────────────────────

class Light(Protocol):
    async def apply(self, on: bool | None, color: str | None, brightness: int | None,
                    fade_ms: int | None) -> None: ...


class RealClock:
    def now(self) -> float:
        return asyncio.get_running_loop().time()

    async def sleep_until(self, t: float) -> None:
        await asyncio.sleep(max(0.0, t - self.now()))


class FakeClock:
    """Jumps straight to the time asked for: patterns 'play' instantly. For tests and --fast."""
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    async def sleep_until(self, t: float) -> None:
        self.t = max(self.t, t)
        await asyncio.sleep(0)  # still a point where the task can be cancelled


class _TimeUp(Exception):
    """Raised when the timeline reaches a loop's time limit; caught by the loop that owns it."""
    def __init__(self, deadline: float):
        self.deadline = deadline


@dataclass
class Player:
    """
    Plays steps on a timeline. self.t is when the current step is *due*; we only actually sleep
    just before sending a change. Due times come from the timeline, not from when the previous
    send finished, so small delays never add up.
    """
    light: Light
    clock: Any = field(default_factory=RealClock)
    rng: random.Random = field(default_factory=random.Random)
    t: float = 0.0
    last_send: float = -math.inf
    changes_sent: int = 0

    async def play(self, steps: list[Step], limit_ms: float | None = None) -> None:
        self.t = self.clock.now()
        deadline = self.t + limit_ms / 1000 if limit_ms else math.inf
        try:
            await self._steps(steps, deadline)
        except _TimeUp:
            pass
        await self.clock.sleep_until(self.t)  # let a final wait or fade run its course

    def _pick(self, r: Range | None) -> int | None:
        return None if r is None else self.rng.randint(*r)

    def _advance(self, ms: float, deadline: float) -> None:
        self.t += ms / 1000
        if self.t >= deadline:
            self.t = deadline
            raise _TimeUp(deadline)

    async def _steps(self, steps: list[Step], deadline: float) -> None:
        for step in steps:
            if self.t >= deadline:
                raise _TimeUp(deadline)
            if isinstance(step, Change):
                await self._change(step, deadline)
            elif isinstance(step, Wait):
                self._advance(self._pick(step.ms), deadline)
            else:
                await self._loop(step, deadline)

    async def _change(self, step: Change, deadline: float) -> None:
        self.t = max(self.t, self.last_send + MIN_GAP_MS / 1000)  # the rate limit
        if self.t >= deadline:
            self.t = deadline
            raise _TimeUp(deadline)
        await self.clock.sleep_until(self.t)
        fade = self._pick(step.fade_ms)
        await self.light.apply(step.on, step.color, self._pick(step.brightness), fade)
        self.last_send, self.changes_sent = self.t, self.changes_sent + 1
        self._advance(fade or 0, deadline)  # a fade holds the pattern until it's done

    async def _loop(self, step: Loop, deadline: float) -> None:
        own = self.t + step.for_ms / 1000 if step.for_ms is not None else math.inf
        times = math.inf if step.times is None else self._pick(step.times)
        inner = min(deadline, own)
        try:
            done = 0
            while done < times:
                await self._steps(step.steps, inner)
                done += 1
        except _TimeUp as up:
            if up.deadline != own or own > deadline:
                raise  # an outer loop's limit: let it through
            self.t = own  # our own limit: stop this loop, carry on after it
