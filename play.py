# /// script
# requires-python = ">=3.11"
# dependencies = ["bleak>=3,<4", "webcolors>=25"]
# ///
"""
Play a pattern from the command line, without Claude or MCP.

    uv run play.py candle                         # a saved pattern, simulated: prints a timeline
    uv run play.py pomodoro --fast                # simulated, instantly (good for checking)
    uv run play.py sunrise --light lamp           # the on-screen lamp from ../mcp-lamp
    uv run play.py breathing --light hue          # the real bulb
    uv run play.py some/file.json                 # a pattern file that isn't in the library

A pattern file is either a list of steps, or {"description": "...", "pattern": [steps]}.
The format is described at the top of pattern.py and in the README. Ctrl-C stops.
"""

import argparse
import asyncio
import json
import random
import sys
from pathlib import Path

import pattern
from hue_bulb import check_color
from library import Library, LibraryError
from lights import LampLight, SimLight


def load(name_or_path: str) -> tuple[str, list]:
    """A pattern from the library by name, or from a JSON file by path."""
    path = Path(name_or_path)
    if path.suffix != ".json" and not path.exists():
        try:
            entry = Library().get(name_or_path)
        except LibraryError as err:
            sys.exit(str(err))
        return entry.description, entry.pattern
    data = json.loads(path.read_text())
    if isinstance(data, dict):
        return data.get("description", ""), data.get("pattern")
    return "", data


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("file", metavar="pattern", help="a saved pattern's name, or a .json file")
    parser.add_argument("--light", choices=["sim", "lamp", "hue"], default="sim")
    parser.add_argument("--fast", action="store_true", help="simulated only: skip the waiting")
    parser.add_argument("--limit", type=float, help="stop after this many seconds (default with --fast: one day)")
    parser.add_argument("--seed", type=int, help="fix the random choices, to replay the same run")
    args = parser.parse_args()

    description, data = load(args.file)
    try:
        steps = pattern.parse(data, check_color=check_color)
    except pattern.PatternError as err:
        sys.exit(f"{args.file}: {err}")
    if args.fast and args.light != "sim":
        sys.exit("--fast only makes sense with the simulated light")

    clock = pattern.FakeClock() if args.fast else pattern.RealClock()
    limit_s = args.limit or (86400 if args.fast else None)
    print(f"{Path(args.file).stem}: {description or ''}\n  {pattern.describe(steps)}"
          + (f" (stopping after {args.limit:g} s)" if args.limit else ""))

    bulb = None
    if args.light == "sim":
        light = SimLight(clock)
    elif args.light == "lamp":
        light = LampLight()
    else:
        from hue_bulb import HueBulb
        from hue_config import load_address
        light = bulb = HueBulb(load_address())

    player = pattern.Player(light, clock, random.Random(args.seed))
    try:
        await player.play(steps, limit_ms=limit_s * 1000 if limit_s else None)
        print(f"done: {player.changes_sent} changes")
    finally:
        if bulb:
            await bulb.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped")
