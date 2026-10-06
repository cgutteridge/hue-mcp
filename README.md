# hue-mcp

A small demo of the Model Context Protocol (MCP): Claude gets tools that control a real
Philips Hue bulb over **Bluetooth Low Energy**, straight from your Mac. There's no Hue Bridge.

This is the sequel to [`mcp-lamp`](../mcp-lamp), which controls a pretend lamp in a window.
The tools have the same shape, so you can compare the two: same MCP layer, different device.

```
┌──────────────┐  JSON-RPC over   ┌──────────────┐  Bluetooth LE   ┌──────────────┐
│ Claude       │  stdin/stdout    │ MCP server   │  (encrypted,    │ Hue bulb     │
│ Desktop/Code │ ◀──────────────▶ │ server.py    │ ◀─────────────▶ │ LCA001       │
└──────────────┘                  │ hue_bulb.py  │   paired once)  └──────────────┘
                                  └──────────────┘
```

| Kind | Name      | What it does                                                               |
|------|-----------|----------------------------------------------------------------------------|
| tool | `get_hue` | Read the bulb's on/off state, colour and brightness (read-only)            |
| tool | `set_hue` | Change any of `on`, `color` (CSS name, hex or e.g. `"2700K"`), `brightness` (0–100) |

`mcp-lamp` also has `start_lamp` (there's no window to open here) and a resource. Adding a
`hue://state` resource would be a good first exercise.

## How it was built: the step scripts

The project was built one small script at a time, and they're kept because each one teaches
one idea. All are run with `uv run <script>`. uv reads the dependency block at the top of
each file and sets up a throwaway environment, so there's no install step.

| Step | Script           | What it shows                                                        |
|------|------------------|----------------------------------------------------------------------|
| 1    | `scan.py`        | BLE *advertisements*: how devices announce themselves                |
| 2    | `explore.py`     | GATT *services* and *characteristics*: what a connected device offers |
| 3    | `pair.py`        | Pairing, and why the light controls were locked until we did it     |
| 4a   | `power.py`       | Writing one byte to switch the bulb on and off                       |
| 4b   | `brightness.py`  | Converting 0–100 % to the bulb's 1–254 (and the fade that caught us out) |
| 4c   | `color_probe.py`, `color.py` | Decoding unknown bytes; colour temperature and CIE xy colour |
| 5    | `server.py` + `hue_bulb.py` | The MCP server                                             |

## What we found on the bulb

Community reverse-engineering got us started. Everything below was checked on an LCA001
(Hue White and Colour Ambiance, firmware 1.116.5). All the light characteristics need an
**encrypted link**, so the Mac must pair with the bulb first.

Light-control service `932c32bd-0000-47a2-835a-a8d455b859dd`. Characteristics are
`932c32bd-XXXX-47a2-835a-a8d455b859dd`:

| XXXX | Meaning            | Format                                                              |
|------|--------------------|---------------------------------------------------------------------|
| 0002 | power              | 1 byte: `01` on, `00` off                                           |
| 0003 | brightness         | 1 byte, 1–254. The bulb *fades*, so read back after a moment        |
| 0004 | colour temperature | 2 bytes little-endian, in mireds (1,000,000 ÷ kelvin), 153–500      |
| 0005 | colour             | 4 bytes: x then y, 2 bytes LE each, 1.0 = 65535. `ffffffff` in white mode |
| 0001 | capabilities       | type-length-value records, incl. the mired range                    |
| 0007, 1005 | whole state  | type-length-value: 1 power, 2 brightness, 3 temperature, 4 xy       |

The settings for your setup live in `hue.toml` (just the bulb's address). It's specific to your
Mac, so git ignores it; `hue.toml.example` is the template. The protocol constants
are in `hue_config.py`. The `HUE_ADDRESS` environment variable overrides the file.

## Setup (macOS)

You need [uv](https://docs.astral.sh/uv/) (`brew install uv`). It fetches Python 3.11+ if needed.

1. **Find the bulb:** `uv run scan.py`, then `cp hue.toml.example hue.toml` and put its address there.
   (On macOS this is a per-Mac ID, not the bulb's real MAC, and it changes if the bulb is reset.)
2. **Pair:** `uv run pair.py` and accept the macOS pop-up. The bulb only accepts a new pairing
   in pairing mode. A freshly factory-reset bulb is in pairing mode. Pair the Mac *before*
   adding the bulb to the Hue app, or your phone may take the pairing slot.
3. **Check:** `uv run power.py off`, then `uv run power.py on`.
4. **Try the server by hand** with the MCP Inspector:
   `npx @modelcontextprotocol/inspector uv run server.py`

## Connect it to Claude

You need the **absolute** path to `server.py`. Run `echo "$PWD/server.py"` in this folder.

### Claude Code

```bash
claude mcp add hue -- uv run /ABSOLUTE/PATH/hue-mcp/server.py
# add --scope user to make it available in every project, not just this one
claude mcp list    # should show "hue" as connected
```

### Claude Desktop

Settings → Developer → Edit Config (opens
`~/Library/Application Support/Claude/claude_desktop_config.json`) and add:

```json
{
  "mcpServers": {
    "hue": {
      "command": "/ABSOLUTE/PATH/TO/uv",
      "args": ["run", "/ABSOLUTE/PATH/hue-mcp/server.py"]
    }
  }
}
```

Use the output of `which uv` for `command`: apps launched from the Dock don't see your shell's
`PATH`. Fully quit and restart Claude Desktop. **The first time, macOS asks whether Claude may
use Bluetooth. Allow it**, or the server can't reach the bulb (fix it later in System Settings →
Privacy & Security → Bluetooth). Logs: `~/Library/Logs/Claude/mcp-server-hue.log`.

### Things to ask Claude

- "Turn my Hue bulb on."
- "Make it a calm ocean blue at 40%."
- "Set it to a warm 2700K reading light."
- "Slowly cycle through the colours of the rainbow."
- "What colour is the bulb right now?"

## Things worth noticing in the code

Everything from `mcp-lamp` applies (stdout is sacred, descriptions are the prompt, schemas
validate for you, annotations are hints). New here:

- **The MCP layer is thin.** `server.py` is ~90 lines; all the Bluetooth work is in
  `hue_bulb.py`, which knows nothing about MCP. You could reuse it in a CLI or a notebook.
- **Python type hints are the schema.** In the Python SDK, `brightness: Annotated[int | None,
  Field(ge=0, le=100)]` does what a zod schema does in TypeScript. Returning a pydantic model
  gives the tool an output schema and structured results.
- **Errors the model can act on.** A bad colour raises `ToolError` with a clear message *before*
  anything is sent to the bulb, so Claude can correct itself and retry.
- **Real hardware is slow and flaky.** The server keeps one connection open between calls and
  reconnects and retries once if it drops. A lock stops two calls talking over each other.
- **Friendly in, friendly out.** `get_hue` reports colours in the same formats `set_hue` accepts,
  so the model can read a value and send it straight back.
- **SDK v2 naming.** Older tutorials say `FastMCP`. In `mcp` 2.x it's
  `from mcp.server.mcpserver import MCPServer`.

## Layout

```
hue.toml.example    template for hue.toml, your bulb's address (hue.toml itself is git-ignored)
hue_config.py       loads hue.toml; the protocol's UUIDs
hue_bulb.py         colour conversions + the Bluetooth connection (no MCP)
server.py           the MCP server (stdio)
scan.py … color.py  the step-by-step scripts above
CLAUDE.md           the original brief
```

Built with the MCP Python SDK v2 (`mcp`), `bleak` 3 and `webcolors`.
