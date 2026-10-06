"""
Tests for hue_bulb.py's conversions and rules (no Bluetooth needed).
Run with:   uv run --with pytest --with bleak --with webcolors pytest
"""

import pytest

from hue_bulb import (BRIGHTNESS_CHAR, COLOR_CHAR, POWER_CHAR, TEMPERATURE_CHAR, check_color,
                      encode_records, is_black, percent_to_raw, plan_writes, raw_to_percent)

ON, OFF = (POWER_CHAR, b"\x01"), (POWER_CHAR, b"\x00")


def chars(writes):
    return [char for char, _ in writes]


@pytest.mark.parametrize("color", ["black", "Black", " BLACK ", "#000", "#000000"])
def test_black_is_recognised(color):
    assert is_black(color)
    check_color(color)  # accepted, not an error


@pytest.mark.parametrize("color", ["red", "#010101", "2700K", "navy"])
def test_other_colours_are_not_black(color):
    assert not is_black(color)


def test_unknown_colour_still_rejected():
    with pytest.raises(ValueError, match="not a CSS colour name"):
        check_color("gren")


def test_black_means_off_and_keeps_the_remembered_colour():
    assert plan_writes(None, "black", None) == [OFF]


def test_brightness_zero_means_off_and_keeps_the_remembered_level():
    assert plan_writes(None, None, 0) == [OFF]


def test_dark_with_a_colour_presets_that_colour():
    writes = plan_writes(None, "red", 0)
    assert writes[0] == OFF and chars(writes) == [POWER_CHAR, COLOR_CHAR]


def test_a_visible_colour_switches_on():
    assert chars(plan_writes(None, "red", None)) == [POWER_CHAR, COLOR_CHAR]
    assert plan_writes(None, "red", None)[0] == ON
    assert chars(plan_writes(None, "2700K", 40)) == [POWER_CHAR, TEMPERATURE_CHAR, BRIGHTNESS_CHAR]


def test_a_brightness_switches_on():
    assert plan_writes(None, None, 50) == [ON, (BRIGHTNESS_CHAR, bytes([percent_to_raw(50)]))]


def test_explicit_off_wins_and_presets_values():
    writes = plan_writes(False, "red", 50)
    assert writes[0] == OFF and chars(writes) == [POWER_CHAR, COLOR_CHAR, BRIGHTNESS_CHAR]


def test_black_wins_over_explicit_on():
    assert plan_writes(True, "black", None) == [OFF]


def test_just_on_or_off():
    assert plan_writes(True, None, None) == [ON]
    assert plan_writes(False, None, None) == [OFF]
    assert plan_writes(None, None, None) == []


def test_brightness_scale_uses_the_full_range():
    assert percent_to_raw(1) == 1 and percent_to_raw(100) == 254
    assert all(raw_to_percent(percent_to_raw(p)) == p for p in range(1, 101))


def test_fade_to_black_packet():
    # power off with a 2 s fade: the bulb fades out by itself (checked on an LCA001)
    assert encode_records(plan_writes(None, "black", None), 2000).hex() == "010100" + "05021400"
