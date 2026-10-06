# /// script
# requires-python = ">=3.11"
# dependencies = ["bleak>=3,<4", "mcp>=2.2,<3", "webcolors>=25"]
# ///
"""
The MCP server. An MCP client (Claude Desktop, Claude Code, the MCP Inspector...)
launches this as a child process and talks JSON-RPC to it over stdin/stdout.

GOLDEN RULE for stdio servers: stdout belongs to the protocol.
Never print() here; log to stderr instead (the logging module does by default).

Built with the MCP Python SDK v2, where the class once called FastMCP is MCPServer.
get_hue / set_hue mirror mcp-lamp. play_hue_pattern / stop_hue_pattern add timed patterns
(the format and the player live in pattern.py, which knows nothing about MCP).
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

import pattern as patterns  # 'pattern' is the tool's argument name
from hue_bulb import MAX_FADE_MS, HueBulb, parse_color
from hue_config import load_address

logging.basicConfig(level=logging.INFO, format="[hue-mcp] %(message)s")  # -> stderr
log = logging.getLogger("hue-mcp")

bulb = HueBulb(load_address())
EXAMPLES = Path(__file__).with_name("examples")


class Playing:
    """The one pattern that may be playing. Starting anything new stops it first."""
    task: asyncio.Task | None = None
    name = summary = error = ""
    started = 0.0

    @classmethod
    def running(cls) -> bool:
        return cls.task is not None and not cls.task.done()

    @classmethod
    async def stop(cls) -> bool:
        if not cls.running():
            return False
        cls.task.cancel()
        try:
            await cls.task
        except asyncio.CancelledError:
            pass
        return True

    @classmethod
    def status(cls) -> str:
        if cls.running():
            elapsed = asyncio.get_running_loop().time() - cls.started
            return f"Pattern {cls.name!r} playing, {elapsed:.0f} s in (it {cls.summary})."
        return f"Pattern {cls.name!r} stopped with an error: {cls.error}" if cls.error else ""


@asynccontextmanager
async def lifespan(server: MCPServer):
    """Runs around the server's whole life: here, just tidy up at the end."""
    log.info("MCP server running on stdio (bulb %s)", bulb.address)
    try:
        yield
    finally:
        await Playing.stop()
        await bulb.disconnect()


mcp = MCPServer("hue-mcp", version="0.2.0", lifespan=lifespan)


class HueResult(BaseModel):
    """What get_hue and set_hue return. A typed return value becomes the tool's output schema,
    and the client gets it as structured JSON as well as text."""
    on: bool = Field(description="Is the bulb switched on?")
    color: str = Field(description='"#rrggbb" when showing a colour, or e.g. "2700K" when showing white')
    brightness: int = Field(description="Brightness, 0-100 percent")
    note: str = Field(default="", description="Anything else worth knowing: a pattern playing, a fade in progress")


# ── Tool 1: read-only ──────────────────────────────────────────────────────────
@mcp.tool(title="Get Hue bulb state", annotations=ToolAnnotations(readOnlyHint=True))
async def get_hue() -> HueResult:
    """Report the real Hue bulb's current on/off state, colour and brightness, and any pattern playing."""
    try:
        state = await bulb.get_state()
    except Exception as err:
        raise ToolError(f"Couldn't reach the bulb over Bluetooth: {err}")
    return HueResult(**vars(state), note=Playing.status())


# ── Tool 2: change something ───────────────────────────────────────────────────
@mcp.tool(title="Set Hue bulb", annotations=ToolAnnotations(idempotentHint=True))
async def set_hue(
    on: Annotated[bool | None, Field(description="Switch the bulb on (true) or off (false)")] = None,
    color: Annotated[str | None, Field(
        description='A CSS colour name ("tomato", "teal"), a hex value ("#ff8800"), '
                    'or a shade of white in kelvin ("2700K" warm to "6500K" cool)')] = None,
    brightness: Annotated[int | None, Field(ge=0, le=100, description="Brightness, 0-100 percent")] = None,
    fade_ms: Annotated[int | None, Field(ge=0, le=MAX_FADE_MS, description=
        "How long the bulb takes to fade to the new values, in ms (100 ms steps, up to ~1 h 49 min). "
        "0 is instant; leave out for the bulb's own ~0.4 s fade.")] = None,
) -> HueResult:
    """
    Change the real Philips Hue bulb in the user's room. Any field you leave out stays as it is.
    For a slow change, use fade_ms (e.g. 600000 fades over 10 minutes); the bulb runs the fade itself.
    Stops any pattern that's playing.
    """
    stopped = await Playing.stop()
    try:
        state = await bulb.set_state(on, color, brightness, fade_ms)
    except ValueError as err:          # a colour we couldn't understand: the model can fix and retry
        raise ToolError(str(err))
    except Exception as err:
        raise ToolError(f"Couldn't reach the bulb over Bluetooth: {err}")
    notes = (["Stopped the pattern that was playing."] if stopped else []) + (
        [f"Fading over {fade_ms / 1000:g} s: these values are where it is now, not where it's going."]
        if fade_ms and fade_ms > 1500 else [])
    log.info("set on=%s color=%s brightness=%s fade_ms=%s -> %s", on, color, brightness, fade_ms, state)
    return HueResult(**vars(state), note=" ".join(notes))


# ── Tool 3: patterns ───────────────────────────────────────────────────────────
PATTERN_HELP = """
Play a timed light pattern on the real Hue bulb: changes, waits, fades, nested loops, randomness.
It plays in the background and this returns straight away with how long it will take.
Anything new (another pattern, set_hue, stop_hue_pattern) replaces it immediately.

The pattern is a list of steps. Each step is ONE of:
- a change: any of "on", "color", "brightness" (as in set_hue), plus optional "fade_ms".
  A fade holds the pattern until it finishes. Without fade_ms the pattern moves straight on.
- a wait: {"wait_ms": 5000}
- a loop: {"loop": [steps], "times": 3} or "times": "forever", and/or "for_ms": 60000 (a time
  limit; it also stops any loop inside it, even mid-wait). With both, whichever comes first.
wait_ms, fade_ms, brightness and times can be random: {"min": 2000, "max": 6000} (inclusive,
picked afresh each time). A loop must take some time. At most ~14 changes a second are sent.

Example: red for 5 s, fade to green over 1 s, hold 1 s; repeat for a minute; then off:
[{"loop": [{"color": "red"}, {"wait_ms": 5000}, {"color": "green", "fade_ms": 1000},
           {"wait_ms": 1000}], "for_ms": 60000},
 {"on": false}]

More examples are available as resources: hue://patterns lists them, hue://patterns/{name} has each.
"""


async def _play(steps: list[patterns.Step]) -> None:
    try:
        await patterns.Player(bulb).play(steps)
        log.info("pattern %r finished", Playing.name)
    except asyncio.CancelledError:
        raise
    except Exception as err:  # e.g. Bluetooth dropped and the retry failed too
        Playing.error = str(err) or type(err).__name__
        log.warning("pattern %r stopped: %s", Playing.name, Playing.error)


@mcp.tool(title="Play a Hue pattern", description=PATTERN_HELP)
async def play_hue_pattern(
    pattern: Annotated[list[dict[str, Any]], Field(description="The list of steps (see the tool description)")],
    name: Annotated[str, Field(description="A short name for the pattern, shown by get_hue")] = "pattern",
) -> str:
    # Steps nest (loops hold steps), so the schema only says "a list of objects" and the real
    # checking happens in pattern.parse, whose errors say exactly which step is wrong.
    try:
        steps = patterns.parse(pattern, check_color=parse_color)
    except patterns.PatternError as err:
        raise ToolError(f"Nothing played: {err}")
    stopped = await Playing.stop()
    Playing.name, Playing.summary, Playing.error = name, patterns.describe(steps), ""
    Playing.started = asyncio.get_running_loop().time()
    Playing.task = asyncio.create_task(_play(steps))
    log.info("playing %r: %s", name, Playing.summary)
    return f"Playing {name!r}: it {Playing.summary}." + (" (Replaced the pattern that was playing.)" if stopped else "")


@mcp.tool(title="Stop the Hue pattern", annotations=ToolAnnotations(idempotentHint=True))
async def stop_hue_pattern() -> str:
    """Stop the pattern that's playing. The bulb stays as it is (a fade already under way finishes)."""
    return f"Stopped {Playing.name!r}." if await Playing.stop() else "No pattern was playing."


# ── Resources: the example patterns ────────────────────────────────────────────
@mcp.resource("hue://patterns", title="Example patterns", mime_type="application/json")
def list_patterns() -> str:
    """The example patterns: names and what each one shows."""
    return json.dumps({p.stem: json.loads(p.read_text()).get("description", "")
                       for p in sorted(EXAMPLES.glob("*.json"))}, indent=2)


@mcp.resource("hue://patterns/{name}", title="Example pattern", mime_type="application/json")
def get_pattern(name: str) -> str:
    """One example pattern: a description, and the steps to pass to play_hue_pattern."""
    path = EXAMPLES / f"{name}.json"
    if path.parent != EXAMPLES or not path.is_file():
        raise ValueError(f"No example called {name!r}; see hue://patterns")
    return path.read_text()


if __name__ == "__main__":
    mcp.run()  # stdio by default
