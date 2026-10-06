"""
Shared settings for the hue-mcp scripts.

Two kinds of value live here:
- *Your setup* (the bulb's address): read from hue.toml, or from the HUE_ADDRESS
  environment variable, which wins if set. MCP clients pass settings to servers
  as environment variables, so this keeps the later MCP server easy to configure.
- *The Hue protocol* (characteristic UUIDs): the same for every Hue bulb, so they're
  plain constants in code rather than config.
"""

import os
import tomllib  # in the standard library since Python 3.11
from pathlib import Path

CONFIG_FILE = Path(__file__).with_name("hue.toml")  # hue.toml next to this file

# Hue light-control service and its characteristics.
# All verified on an LCA001 bulb (firmware 1.116.5).
# 0001 (capabilities), 0007 and 1005 (whole state as type-length-value records) are
# readable too; see color_probe.py. We don't need them yet.
LIGHT_SERVICE = "932c32bd-0000-47a2-835a-a8d455b859dd"
POWER_CHAR = "932c32bd-0002-47a2-835a-a8d455b859dd"        # verified: 1 byte, 01 on, 00 off
BRIGHTNESS_CHAR = "932c32bd-0003-47a2-835a-a8d455b859dd"   # verified: 1 byte, 1-254 (the bulb fades to new values)
TEMPERATURE_CHAR = "932c32bd-0004-47a2-835a-a8d455b859dd"  # verified: 2 bytes LE, mireds (1e6 / kelvin), 153-500
COLOR_CHAR = "932c32bd-0005-47a2-835a-a8d455b859dd"        # verified: 4 bytes, x then y, 2 bytes LE each, 1.0 = 65535; ffffffff in white mode


def load_address() -> str:
    """Return the bulb's address from $HUE_ADDRESS or hue.toml, with a helpful error."""
    if address := os.environ.get("HUE_ADDRESS"):
        return address
    try:
        with CONFIG_FILE.open("rb") as f:  # tomllib wants a binary file
            return tomllib.load(f)["address"]
    except FileNotFoundError:
        raise SystemExit(f"No {CONFIG_FILE.name} found. Run `uv run scan.py`, then\n"
                         f"`cp hue.toml.example {CONFIG_FILE.name}` and put the bulb's address in it.")
    except KeyError:
        raise SystemExit(f"{CONFIG_FILE.name} has no `address = ...` line.")
