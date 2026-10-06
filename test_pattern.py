"""
Tests for pattern.py. They use a fake clock, so hours of pattern run in milliseconds.
Run with:   uv run --with pytest pytest
"""

import asyncio
import json
import random
from pathlib import Path

import pytest

import pattern
from pattern import FakeClock, PatternError, Player, describe, parse


class RecordingLight:
    def __init__(self, clock):
        self.clock, self.log = clock, []

    async def apply(self, on, color, brightness, fade_ms):
        self.log.append((round(self.clock.now(), 3), on, color, brightness, fade_ms))


def run(data, limit_ms=None, seed=0):
    clock = FakeClock()
    light = RecordingLight(clock)
    player = Player(light, clock, random.Random(seed))
    asyncio.run(player.play(parse(data), limit_ms))
    return light.log, round(clock.now(), 3)


def check_color(color):
    if color not in {"red", "green", "blue", "2700K"}:
        raise ValueError(f'"{color}" is not a colour')


# ── Parsing ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("data, message", [
    ([], "pattern: is empty"),
    ({"color": "red"}, "pattern: should be a list"),
    ([{"colour": "red"}], "pattern[0]: unknown key 'colour' (did you mean 'color'?)"),
    ([{"color": "red", "wait_ms": 5}], "pattern[0]: mixes a change with a wait"),
    ([{"fade_ms": 500}], "fade_ms on its own changes nothing"),
    ([{"wait_ms": -1}], "pattern[0].wait_ms: -1 is out of range"),
    ([{"wait_ms": {"min": 5, "max": 1}}], "min (5) is bigger than max (1)"),
    ([{"wait_ms": "5s"}], "pattern[0].wait_ms: should be a whole number"),
    ([{"loop": [{"wait_ms": 5}]}], "a loop needs \"times\""),
    ([{"loop": [{"wait_ms": 5}], "times": "lots"}], "should be a number, {\"min\", \"max\"} or \"forever\""),
    ([{"loop": [{"wait_ms": 0}], "times": "forever"}], "pattern[0].loop: takes no time"),
    ([{"wait_ms": 1}, {"loop": [{"loop": [{"color": "gren"}], "times": 2}], "times": 2}],
     "pattern[1].loop[0].loop[0].color: \"gren\" is not a colour"),
])
def test_errors_say_where(data, message):
    with pytest.raises(PatternError) as err:
        parse(data, check_color)
    assert message in str(err.value)


def test_nesting_and_size_limits():
    deep = [{"wait_ms": 1}]
    for _ in range(pattern.MAX_DEPTH + 1):
        deep = [{"loop": deep, "times": 1}]
    with pytest.raises(PatternError, match="nested more than"):
        parse(deep)
    with pytest.raises(PatternError, match="too many steps"):
        parse([{"wait_ms": 1}] * (pattern.MAX_STEPS + 1))


def test_examples_are_valid():
    for path in Path(__file__).with_name("patterns").glob("*.json"):
        parse(json.loads(path.read_text())["pattern"])


# ── Timing ─────────────────────────────────────────────────────────────────────

def test_change_wait_fade():
    log, end = run([{"color": "red"}, {"wait_ms": 5000}, {"color": "green", "fade_ms": 1000}])
    assert log == [(0.0, None, "red", None, None), (5.0, None, "green", None, 1000)]
    assert end == 6.0  # the fade holds the pattern until it finishes


def test_rate_limit():
    log, _ = run([{"color": "red"}, {"color": "green"}, {"color": "blue"}])
    assert [t for t, *_ in log] == [0.0, 0.07, 0.14]


def test_time_limit_cuts_through_nested_loops():
    data = [
        {"loop": [
            {"color": "red"},
            {"loop": [{"wait_ms": 400}, {"brightness": 50}], "times": "forever"},  # never ends by itself
        ], "for_ms": 1000},
        {"on": False},
    ]
    log, end = run(data)
    assert all(t < 1.0 for t, *_ in log[:-1])
    assert log[-1] == (1.0, False, None, None, None)  # the step after the loop runs exactly at the limit
    assert end == 1.0


def test_inner_limit_does_not_stop_outer_loop():
    data = [{"loop": [{"loop": [{"color": "red"}, {"wait_ms": 100}], "for_ms": 250}], "times": 2}]
    log, end = run(data)
    # 3 reds per 250 ms pass; the second pass's first red waits for the 70 ms rate limit
    assert [t for t, *_ in log] == [0.0, 0.1, 0.2, 0.27, 0.37, 0.47]
    assert end == 0.5


def test_counted_loop_and_random_counts():
    log, _ = run([{"loop": [{"color": "red"}, {"wait_ms": 100}], "times": 3}])
    assert len(log) == 3
    counts = {len(run([{"loop": [{"color": "red"}, {"wait_ms": 10}], "times": {"min": 2, "max": 4}}],
                      seed=s)[0]) for s in range(40)}
    assert counts == {2, 3, 4}


def test_random_values_and_seed():
    data = [{"loop": [{"brightness": {"min": 10, "max": 20}, "fade_ms": {"min": 100, "max": 200}}],
             "times": 50}]
    log, _ = run(data, seed=7)
    assert all(10 <= b <= 20 and 100 <= f <= 200 for _, _, _, b, f in log)
    assert run(data, seed=7) == run(data, seed=7)


def test_overall_limit_stops_forever():
    log, end = run([{"loop": [{"color": "red"}, {"wait_ms": 1000}], "times": "forever"}], limit_ms=3500)
    assert len(log) == 4 and end == 3.5


def test_describe():
    assert describe(parse([{"wait_ms": 5000}, {"color": "red", "fade_ms": 1000}])) == "takes about 6 s"
    assert describe(parse([{"wait_ms": {"min": 1000, "max": 3000}}])) == "takes 1 s to 3 s"
    assert describe(parse([{"loop": [{"wait_ms": 7000}], "times": "forever", "for_ms": 60000}])) == "takes about 60 s"
    assert describe(parse([{"loop": [{"wait_ms": 1}], "times": "forever"}])) == "runs until stopped"


# ── Replacing a running pattern ────────────────────────────────────────────────

def test_cancel_stops_immediately_even_mid_wait():
    async def scenario():
        light = RecordingLight(pattern.RealClock())
        task = asyncio.create_task(Player(light).play(parse([{"color": "red"}, {"wait_ms": 60000}, {"color": "blue"}])))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return light.log
    assert [c for _, _, c, _, _ in asyncio.run(scenario())] == ["red"]
