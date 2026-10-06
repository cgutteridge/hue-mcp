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
The tools mirror mcp-lamp: get_hue / set_hue, same fields, same 0-100 brightness.
"""

import logging
from contextlib import asynccontextmanager
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from hue_bulb import HueBulb
from hue_config import load_address

logging.basicConfig(level=logging.INFO, format="[hue-mcp] %(message)s")  # -> stderr
log = logging.getLogger("hue-mcp")

bulb = HueBulb(load_address())


@asynccontextmanager
async def lifespan(server: MCPServer):
    """Runs around the server's whole life: here, just disconnect cleanly at the end."""
    log.info("MCP server running on stdio (bulb %s)", bulb.address)
    try:
        yield
    finally:
        await bulb.disconnect()


mcp = MCPServer("hue-mcp", version="0.1.0", lifespan=lifespan)


class HueResult(BaseModel):
    """What both tools return. A typed return value becomes the tool's output schema,
    and the client gets it as structured JSON as well as text."""
    on: bool = Field(description="Is the bulb switched on?")
    color: str = Field(description='"#rrggbb" when showing a colour, or e.g. "2700K" when showing white')
    brightness: int = Field(description="Brightness, 0-100 percent")


# ── Tool 1: read-only ──────────────────────────────────────────────────────────
@mcp.tool(title="Get Hue bulb state", annotations=ToolAnnotations(readOnlyHint=True))
async def get_hue() -> HueResult:
    """Report the real Hue bulb's current on/off state, colour and brightness."""
    try:
        state = await bulb.get_state()
    except Exception as err:
        raise ToolError(f"Couldn't reach the bulb over Bluetooth: {err}")
    return HueResult(**vars(state))


# ── Tool 2: change something ───────────────────────────────────────────────────
@mcp.tool(title="Set Hue bulb", annotations=ToolAnnotations(idempotentHint=True))
async def set_hue(
    on: Annotated[bool | None, Field(description="Switch the bulb on (true) or off (false)")] = None,
    color: Annotated[str | None, Field(
        description='A CSS colour name ("tomato", "teal"), a hex value ("#ff8800"), '
                    'or a shade of white in kelvin ("2700K" warm to "6500K" cool)')] = None,
    brightness: Annotated[int | None, Field(ge=0, le=100, description="Brightness, 0-100 percent")] = None,
) -> HueResult:
    """
    Change the real Philips Hue bulb in the user's room. Any field you leave out stays as it is.
    Changes fade in over about half a second.
    """
    try:
        state = await bulb.set_state(on, color, brightness)
    except ValueError as err:          # a colour we couldn't understand: the model can fix and retry
        raise ToolError(str(err))
    except Exception as err:
        raise ToolError(f"Couldn't reach the bulb over Bluetooth: {err}")
    log.info("set on=%s color=%s brightness=%s -> %s", on, color, brightness, state)
    return HueResult(**vars(state))


if __name__ == "__main__":
    mcp.run()  # stdio by default
