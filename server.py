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
(the format and the player live in pattern.py), and the *_hue_pattern(s) library tools keep
named patterns (library.py). Neither of those files knows anything about MCP.
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

import pattern as patterns  # 'pattern' is the tool's argument name
from hue_bulb import MAX_FADE_MS, HueBulb, check_color
from hue_config import load_address
from library import Library, LibraryError

logging.basicConfig(level=logging.INFO, format="[hue-mcp] %(message)s")  # -> stderr
log = logging.getLogger("hue-mcp")

bulb = HueBulb(load_address())
library = Library()


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
                    'a shade of white in kelvin ("2700K" warm to "6500K" cool), or "black" for dark')] = None,
    brightness: Annotated[int | None, Field(ge=0, le=100, description=
        "Brightness, 1-100 percent; 0 means dark")] = None,
    fade_ms: Annotated[int | None, Field(ge=0, le=MAX_FADE_MS, description=
        "How long the bulb takes to fade to the new values, in ms (100 ms steps, up to ~1 h 49 min). "
        "0 is instant; leave out for the bulb's own ~0.4 s fade.")] = None,
) -> HueResult:
    """
    Change the real Philips Hue bulb in the user's room. Any field you leave out stays as it is.
    Black (or brightness 0) means dark: the bulb switches off, fading out if fade_ms is given, and
    remembers its colour and brightness. Any other colour or brightness switches it on (fading in).
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
Give EITHER the name of a saved pattern (see list_hue_patterns) OR the steps themselves.
It plays in the background and this returns straight away with how long it will take.
Anything new (another pattern, set_hue, stop_hue_pattern) replaces it immediately.

The pattern is a list of steps. Each step is ONE of:
- a change: any of "on", "color", "brightness" (as in set_hue), plus optional "fade_ms".
  A fade holds the pattern until it finishes. Without fade_ms the pattern moves straight on.
  "black" or brightness 0 is dark (a fade to black fades out); any other colour or brightness
  lights the bulb again, so "on" is rarely needed.
- a wait: {"wait_ms": 5000}
- a loop: {"loop": [steps], "times": 3} or "times": "forever", and/or "for_ms": 60000 (a time
  limit; it also stops any loop inside it, even mid-wait). With both, whichever comes first.
wait_ms, fade_ms, brightness and times can be random: {"min": 2000, "max": 6000} (inclusive,
picked afresh each time). A loop must take some time. At most ~14 changes a second are sent.

Example: red for 5 s, fade to green over 1 s, hold 1 s; repeat for a minute; then off:
[{"loop": [{"color": "red"}, {"wait_ms": 5000}, {"color": "green", "fade_ms": 1000},
           {"wait_ms": 1000}], "for_ms": 60000},
 {"on": false}]

To keep a pattern for later, use save_hue_pattern. get_hue_pattern shows a saved one's steps,
which make good starting points for new patterns.
"""

PatternName = Annotated[str, Field(description='A pattern name: lowercase words joined by hyphens, e.g. "forest-railway"')]
Steps = Annotated[list[dict[str, Any]], Field(description="The list of steps (see play_hue_pattern's description)")]


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
    name: Annotated[str | None, Field(description=
        "The name of a saved pattern to play; or, with steps, a label for them shown by get_hue")] = None,
    pattern: Annotated[list[dict[str, Any]] | None, Field(description="The steps, if not playing a saved pattern")] = None,
) -> str:
    # Steps nest (loops hold steps), so the schema only says "a list of objects" and the real
    # checking happens in pattern.parse, whose errors say exactly which step is wrong.
    if pattern is None:
        if not name:
            raise ToolError("Give the name of a saved pattern, or the steps to play.")
        try:
            pattern = library.get(name).pattern
        except LibraryError as err:
            raise ToolError(f"Nothing played: {err}")
    try:
        steps = patterns.parse(pattern, check_color=check_color)
    except patterns.PatternError as err:
        raise ToolError(f"Nothing played: {err}")
    stopped = await Playing.stop()
    Playing.name, Playing.summary, Playing.error = name or "pattern", patterns.describe(steps), ""
    Playing.started = asyncio.get_running_loop().time()
    Playing.task = asyncio.create_task(_play(steps))
    log.info("playing %r: %s", Playing.name, Playing.summary)
    return (f"Playing {Playing.name!r}: it {Playing.summary}."
            + (" (Replaced the pattern that was playing.)" if stopped else ""))


@mcp.tool(title="Stop the Hue pattern", annotations=ToolAnnotations(idempotentHint=True))
async def stop_hue_pattern() -> str:
    """Stop the pattern that's playing. The bulb stays as it is (a fade already under way finishes)."""
    return f"Stopped {Playing.name!r}." if await Playing.stop() else "No pattern was playing."


# ── The pattern library ────────────────────────────────────────────────────────
class PatternInfo(BaseModel):
    name: str
    description: str
    source: str = Field(description='"built-in" (ships with the project) or "yours" (saved by the user)')


class PatternDetail(PatternInfo):
    pattern: list[Any] = Field(description="The steps, ready to pass to play_hue_pattern or adapt")


@mcp.tool(title="List saved Hue patterns", annotations=ToolAnnotations(readOnlyHint=True))
def list_hue_patterns() -> list[PatternInfo]:
    """List the saved light patterns: built-in ones and the user's own, with a one-line description each."""
    return [PatternInfo(name=e.name, description=e.description,
                        source=e.source + (" (in place of the built-in one)" if e.overrides_built_in else ""))
            for e in library.list()]


@mcp.tool(title="Show a saved Hue pattern", annotations=ToolAnnotations(readOnlyHint=True))
def get_hue_pattern(name: PatternName) -> PatternDetail:
    """Show a saved pattern's description and steps, e.g. to explain it or as a starting point for a new one."""
    try:
        e = library.get(name)
    except LibraryError as err:
        raise ToolError(str(err))
    return PatternDetail(name=e.name, description=e.description, source=e.source, pattern=e.pattern)


@mcp.tool(title="Save a Hue pattern", annotations=ToolAnnotations(idempotentHint=True))
def save_hue_pattern(
    name: PatternName,
    description: Annotated[str, Field(description="One line saying what it looks like (up to 200 characters)")],
    pattern: Steps,
    replace: Annotated[bool, Field(description=
        "Set true to update a pattern that already exists, or to save your own version of a built-in one")] = False,
) -> str:
    """
    Save a light pattern under a name, to play later with play_hue_pattern(name=...).
    The steps are checked first, so a saved pattern always plays. It is saved to the user's own
    patterns; built-in ones are never changed.
    """
    try:
        return library.save(name, description, pattern, replace, check_color=check_color)
    except (LibraryError, patterns.PatternError) as err:
        raise ToolError(f"Not saved: {err}")


@mcp.tool(title="Delete a saved Hue pattern", annotations=ToolAnnotations(destructiveHint=True))
def delete_hue_pattern(name: PatternName) -> str:
    """Delete one of the user's saved patterns. Built-in patterns can't be deleted."""
    try:
        return library.delete(name)
    except LibraryError as err:
        raise ToolError(f"Not deleted: {err}")


# Resources: the same library as readable documents, for clients that let the user attach them.
@mcp.resource("hue://patterns", title="Hue patterns", mime_type="application/json")
def patterns_resource() -> str:
    """All saved patterns: name, description and where each comes from."""
    return json.dumps([{"name": e.name, "description": e.description, "source": e.source}
                       for e in library.list()], indent=2)


@mcp.resource("hue://patterns/{name}", title="Hue pattern", mime_type="application/json")
def pattern_resource(name: str) -> str:
    """One saved pattern: its description and steps."""
    e = library.get(name)  # raises LibraryError (a ValueError) for bad or unknown names
    return json.dumps({"name": e.name, "description": e.description, "pattern": e.pattern}, indent=2)


if __name__ == "__main__":
    mcp.run()  # stdio by default
