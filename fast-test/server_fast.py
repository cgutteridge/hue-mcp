# /// script
# requires-python = ">=3.11"
# dependencies = ["bleak>=3,<4", "mcp>=2.2,<3", "webcolors>=25"]
# ///
"""
EXPERIMENTAL copy of server.py with faster changes, bulb-side fades and sequences.
Registered as a separate MCP server ("hue-fast") so the original "hue" keeps working.

What's different (numbers from bench.py, see bench-results.json):
- get_hue reads the whole state in one go (~90 ms instead of ~350 ms).
- set_hue takes fade_ms: the bulb fades by itself (verified 3 s fade took 2.9 s),
  and there's no fixed half-second wait.
- run_hue_sequence: Claude sends a whole list of steps in one call; this server plays it
  with local timing (steps land ~50 ms after they're due, with no build-up), in the
  background, so the tool returns straight away. stop_hue_sequence (or any set_hue) stops it.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from hue_bulb import MAX_TRANSITION_MS, FastHueBulb, plan_writes
from hue_config import load_address

logging.basicConfig(level=logging.INFO, format="[hue-fast] %(message)s")  # -> stderr
log = logging.getLogger("hue-mcp")

bulb = FastHueBulb(load_address())
sequence_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(server: MCPServer):
    log.info("experimental MCP server running on stdio (bulb %s)", bulb.address)
    try:
        yield
    finally:
        await stop_running_sequence()
        await bulb.disconnect()


mcp = MCPServer("hue-fast", version="0.2.0-experimental", lifespan=lifespan)

FadeMs = Annotated[int | None, Field(ge=0, le=MAX_TRANSITION_MS,
                   description="How long the bulb takes to fade to the new values, in milliseconds "
                               "(100 ms resolution; 0 = instant). Leave out for the bulb's default (~0.4 s).")]
ColorArg = Annotated[str | None, Field(
    description='A CSS colour name ("tomato", "teal"), a hex value ("#ff8800"), '
                'or a shade of white in kelvin ("2700K" warm to "6500K" cool)')]
BrightnessArg = Annotated[int | None, Field(ge=0, le=100, description="Brightness, 0-100 percent")]


class HueResult(BaseModel):
    on: bool = Field(description="Is the bulb switched on?")
    color: str = Field(description='"#rrggbb" when showing a colour, or e.g. "2700K" when showing white')
    brightness: int = Field(description="Brightness, 0-100 percent")
    note: str = Field(default="", description="Anything worth knowing, e.g. that a fade is still running")


class Step(BaseModel):
    """One step of a sequence. Fields left out stay as they are."""
    on: bool | None = None
    color: ColorArg = None
    brightness: BrightnessArg = None
    fade_ms: FadeMs = None
    hold_ms: Annotated[int, Field(ge=0, le=3_600_000,
                       description="Time from the start of this step to the start of the next, in ms. "
                                   "Usually at least fade_ms, so the fade finishes.")] = 1000


async def stop_running_sequence() -> bool:
    global sequence_task
    if sequence_task is not None and not sequence_task.done():
        sequence_task.cancel()
        try:
            await sequence_task
        except asyncio.CancelledError:
            pass
        sequence_task = None
        return True
    return False


async def play(steps: list[Step], repeat: int) -> None:
    """Play steps on a fixed timeline (each due time counted from the start, so delays don't add up)."""
    loop = asyncio.get_running_loop()
    start, offset = loop.time(), 0.0
    for _ in range(repeat):
        for step in steps:
            await asyncio.sleep(max(0.0, start + offset - loop.time()))
            await bulb.write(plan_writes(step.on, step.color, step.brightness), step.fade_ms)
            offset += step.hold_ms / 1000
    log.info("sequence finished")


def as_result(state, note: str = "") -> HueResult:
    return HueResult(**vars(state), note=note)


@mcp.tool(title="Get Hue bulb state", annotations=ToolAnnotations(readOnlyHint=True))
async def get_hue() -> HueResult:
    """Report the real Hue bulb's current on/off state, colour and brightness."""
    try:
        state = await bulb.get_state()
    except Exception as err:
        raise ToolError(f"Couldn't reach the bulb over Bluetooth: {err}")
    running = sequence_task is not None and not sequence_task.done()
    return as_result(state, "A sequence is playing." if running else "")


@mcp.tool(title="Set Hue bulb", annotations=ToolAnnotations(idempotentHint=True))
async def set_hue(on: Annotated[bool | None, Field(description="Switch the bulb on (true) or off (false)")] = None,
                  color: ColorArg = None, brightness: BrightnessArg = None, fade_ms: FadeMs = None) -> HueResult:
    """
    Change the real Philips Hue bulb in the user's room. Any field you leave out stays as it is.
    Use fade_ms for a slow fade (e.g. 600000 to fade over 10 minutes); the bulb does the fade itself.
    Stops any running sequence.
    """
    stopped = await stop_running_sequence()
    try:
        state = await bulb.set_state_fast(on, color, brightness, fade_ms)
    except ValueError as err:
        raise ToolError(str(err))
    except Exception as err:
        raise ToolError(f"Couldn't reach the bulb over Bluetooth: {err}")
    notes = []
    if stopped:
        notes.append("Stopped the running sequence.")
    if fade_ms and fade_ms > 1500:
        notes.append(f"Fading over {fade_ms / 1000:g} s; the values above are where it is now, not the target.")
    log.info("set on=%s color=%s brightness=%s fade_ms=%s -> %s", on, color, brightness, fade_ms, state)
    return as_result(state, " ".join(notes))


@mcp.tool(title="Play a Hue sequence")
async def run_hue_sequence(
    steps: Annotated[list[Step], Field(min_length=1, max_length=200,
                     description="The steps, in order. Each can set on/color/brightness, a fade_ms, and a hold_ms.")],
    repeat: Annotated[int, Field(ge=1, le=1000, description="How many times to play the whole list")] = 1,
) -> str:
    """
    Play a timed sequence on the bulb (e.g. a sunrise, a rainbow cycle, a pulse) in one call.
    It runs in the background and this returns straight away. Replaces any running sequence;
    set_hue or stop_hue_sequence stop it.
    """
    global sequence_task
    try:
        for step in steps:  # validate every colour before starting
            plan_writes(step.on, step.color, step.brightness)
    except ValueError as err:
        raise ToolError(str(err))
    await stop_running_sequence()
    sequence_task = asyncio.create_task(play(steps, repeat))
    total = sum(s.hold_ms for s in steps) * repeat / 1000
    return f"Playing {len(steps)} steps x {repeat}, about {total:g} s in total."


@mcp.tool(title="Stop the Hue sequence", annotations=ToolAnnotations(idempotentHint=True))
async def stop_hue_sequence() -> str:
    """Stop a running sequence. A bulb-side fade already under way still finishes."""
    return "Stopped." if await stop_running_sequence() else "No sequence was playing."


if __name__ == "__main__":
    mcp.run()  # stdio by default
