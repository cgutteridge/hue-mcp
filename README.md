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
| tool | `get_hue` | Read the bulb's on/off state, colour and brightness, and any pattern playing (read-only) |
| tool | `set_hue` | Change any of `on`, `color` (CSS name, hex or e.g. `"2700K"`), `brightness` (0–100), with an optional `fade_ms` the bulb runs itself |
| tool | `play_hue_pattern` | Play a saved pattern by name, or a timed pattern of changes, waits, fades and loops given as steps, in the background ([Patterns](#patterns)) |
| tool | `stop_hue_pattern` | Stop it                                                         |
| tool | `list_hue_patterns` | The saved patterns: name, one-line description, built-in or yours (read-only) |
| tool | `get_hue_pattern` | One saved pattern's steps (read-only) |
| tool | `save_hue_pattern` | Save a pattern under a name; `replace: true` to update one ([Library](#the-pattern-library)) |
| tool | `delete_hue_pattern` | Delete one of your saved patterns (marked destructive) |
| resource | `hue://patterns`, `hue://patterns/{name}` | The same library, as documents you can attach to a chat |

`mcp-lamp` also has `start_lamp` (there's no window to open here). Adding a `hue://state`
resource would be a good first exercise.

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
| 6    | `bench.py`       | Measuring: how fast can we change the bulb, and can it fade by itself? (It can.) |
| 7    | `pattern.py`, `play.py` | Timed patterns, playable on the bulb, the on-screen lamp or a simulation |

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
| 0007 | whole state        | type-length-value: 1 power, 2 brightness, 3 temperature, 4 xy. **Writable**, see below |
| 1005 | whole state        | the same records, read-only                                         |

`bench.py` measured (LCA001, macOS, one connection kept open):

| What | Result |
|---|---|
| Connecting | ~6 s, which is why the server keeps one connection open |
| One acknowledged write | ~60 ms, so ~15 changes a second at most |
| One read | ~90 ms. Reading 0007 gets the whole state in one go |
| The bulb's own fade | ~0.36 s for any change |
| **Writing 0007** | One packet changes several things at once. Add a record of **type 5, 2 bytes LE: the fade time in 100 ms units**, and the bulb fades smoothly by itself: a requested 3 s fade took 2.9 s. (Sending 3000, meaning milliseconds, gave a 5-minute fade.) |
| Timing done on the Mac | Steps 400 ms apart landed 40–60 ms late (one write), with no build-up |

Colour and white fades work the same way (checked by reading back mid-fade: red to blue over
20 s passed through pinks and violets on schedule; 2200 K to 6500 K over 12 s read 3226 K halfway,
an even fade in mireds). If a bulb refuses 0007 writes, `hue_bulb.py` falls back to writing the
characteristics one by one (without custom fades).

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
- "Fade to a warm 2700K reading light over two minutes."
- "Slowly cycle through the colours of the rainbow, forever."
- "Make it flicker like a candle." / "Play the sunrise example."
- "Red for five seconds, fade to green over one, hold a second; do that for a minute, then off."
- "What colour is the bulb right now?"

## Patterns

`play_hue_pattern` takes a list of steps. A step is **one** of:

| Step | Looks like | Notes |
|---|---|---|
| change | `{"color": "red", "brightness": 80, "fade_ms": 1000}` | any of `on`, `color`, `brightness` (as in `set_hue`), optional `fade_ms`. A fade holds the pattern until it's done; without `fade_ms` the pattern moves straight on |
| wait | `{"wait_ms": 5000}` | |
| loop | `{"loop": [steps], "times": 3}` | `times`: a count, `{"min", "max"}` or `"forever"`; and/or `for_ms`: a time limit. With both, whichever comes first |

- **Random values:** `wait_ms`, `fade_ms`, `brightness` and `times` also take `{"min": a, "max": b}`
  (inclusive), picked afresh each time the step runs.
- **Time limits cut through:** a loop's `for_ms` stops everything inside it, even an inner loop
  halfway through a wait, and the pattern carries on after that loop.
- **New replaces old:** another pattern, `set_hue` or `stop_hue_pattern` stops the current pattern
  at once.
- **Safe by construction:** the whole pattern is checked before anything plays, and errors say which
  step is wrong (`pattern[0].loop[2].color: "gren" is not a CSS colour name…`). Every loop must take
  some time, and the player sends at most one change every 70 ms whatever the pattern says.

```json
[
  {"loop": [
    {"color": "red"},
    {"wait_ms": 5000},
    {"color": "green", "fade_ms": 1000},
    {"wait_ms": 1000}
  ], "for_ms": 60000},
  {"on": false}
]
```

That's red for 5 s, a 1 s fade to green, a 1 s hold, repeated for a minute (stopping exactly at
60 s, wherever it's got to), then off. The built-in patterns in [`patterns/`](patterns):

| Pattern | What it is | Shows off |
|---|---|---|
| `sunrise`, `sunset` | 20-minute wake-up; 30-minute wind-down ending in off | long fades the bulb runs itself |
| `box-breathing`, `breathing` | breathing guides | exact timing; nested loops |
| `pomodoro` | 4 × (25 min focus, 5 min break), then a gold blink | a practical timer |
| `candle`, `fireplace` | flickering flame; fire with flare-ups | random brightness, fades and waits |
| `aurora` | slow green/teal/violet drifts | colour fades |
| `lighthouse` | a beam sweeping past every 10 s | a simple steady rhythm |
| `forest-railway` | gloomy carriage, hard-cut sun through the trees | random bursts of random length |
| `thunderstorm` | gloom with single/double/triple lightning | the same, harsher |
| `someones-home` | TV-like flicker for an empty house | scene cuts among slow drift |
| `seasick`, `party`, `red-green` | rolling whites; colour hops; red/green for a minute | random timing; random counts; time limits |
| `disco-strobe` | 3 s dark, 3 s strobe, for a minute | the speed limit (~6 flashes/s). **Fast flashing: not for anyone with photosensitive epilepsy** |

### The pattern library

Patterns can be saved by name with a one-line description, then played by name. Ask Claude to
"save that as forest-railway", "play thunderstorm", "what patterns are there?" or "make a slower
version of lighthouse".

- **Built-in patterns** live in `patterns/` and are part of the project (in git). The tools can read
  and play them but never change them.
- **Your patterns** live in `my-patterns/`, which git ignores, like `hue.toml`. Everything you save
  goes there. Set `HUE_MY_PATTERNS` (in the server's `env` in Claude's config) to keep them somewhere
  else, such as a synced folder.
- **Same name, yours wins.** Saving your own `candle` (it needs `replace: true`) takes the built-in's
  place; delete yours and the built-in one is back.
- **Saving checks first**, with the same step-by-step errors as playing, so a saved pattern always
  plays. Names are lowercase words joined by hyphens (`forest-railway`), up to 40 characters.
- Each pattern is a small readable JSON file (one step per line), so editing one by hand or adding a
  new built-in is just a file in the right folder.

### Playing patterns without Claude, or without a bulb

`pattern.py` knows nothing about MCP or Bluetooth: it plays on anything with an
`async apply(on, color, brightness, fade_ms)` method. `play.py` uses that:

```bash
uv run play.py candle                      # simulated: prints a timeline, in real time
uv run play.py pomodoro --fast             # simulated, instantly
uv run play.py breathing --light lamp      # the on-screen lamp from ../mcp-lamp
uv run play.py sunrise --light hue         # the real bulb
uv run play.py path/to/some.json           # a pattern file that isn't in the library
```

To drive something else (WLED, a smart plug, a terminal UI), copy a class from `lights.py`.
Tests: `uv run --with pytest pytest` (they use a fake clock, so hours of pattern take milliseconds).

## Things worth noticing in the code

Everything from `mcp-lamp` applies (stdout is sacred, descriptions are the prompt, schemas
validate for you, annotations are hints). New here:

- **The MCP layer is thin.** All the Bluetooth work is in `hue_bulb.py` and all the pattern logic in
  `pattern.py`; neither knows about MCP, so `play.py` reuses both from the command line.
- **Python type hints are the schema.** In the Python SDK, `brightness: Annotated[int | None,
  Field(ge=0, le=100)]` does what a zod schema does in TypeScript. Returning a pydantic model
  gives the tool an output schema and structured results.
- **Errors the model can act on.** A bad colour raises `ToolError` with a clear message *before*
  anything is sent to the bulb, so Claude can correct itself and retry.
- **Real hardware is slow and flaky.** The server keeps one connection open between calls and
  reconnects and retries once if it drops. A lock stops two calls talking over each other.
- **Nested input without a nested schema.** Patterns nest (loops hold steps). Recursive JSON
  schemas are handled unevenly by MCP clients, so `play_hue_pattern` declares just "a list of
  objects", explains the format in its description, and `pattern.parse` does the real checking with
  errors that point at the exact step. Schema for shape, code for meaning.
- **Tools, not resources, for things the model needs.** The library is also offered as resources
  (`hue://patterns/…`), but in Claude Desktop resources are things *you* attach to a chat: the model
  can't open them by itself. Anything Claude should be able to look up mid-conversation has to be a
  tool, so `list_hue_patterns` and `get_hue_pattern` exist, and playing by name means the steps never
  have to be pasted into a call.
- **Long jobs in the background.** A pattern can run for hours, so the tool starts it and returns at
  once with how long it will take; `get_hue` reports progress, and any error that stopped it.
- **Friendly in, friendly out.** `get_hue` reports colours in the same formats `set_hue` accepts,
  so the model can read a value and send it straight back.
- **SDK v2 naming.** Older tutorials say `FastMCP`. In `mcp` 2.x it's
  `from mcp.server.mcpserver import MCPServer`.

## Layout

```
hue.toml.example    template for hue.toml, your bulb's address (hue.toml itself is git-ignored)
hue_config.py       loads hue.toml; the protocol's UUIDs
hue_bulb.py         colour conversions, whole-state records + the Bluetooth connection (no MCP)
server.py           the MCP server (stdio)
pattern.py          the pattern format, checking and player (no MCP, no Bluetooth)
lights.py           other things a pattern can play on: a simulation, the mcp-lamp window
play.py             play a pattern file from the command line
library.py          the pattern library: built-in patterns/ + your my-patterns/ (no MCP)
patterns/           the built-in patterns
my-patterns/        your saved patterns (created on first save; git-ignored)
test_*.py           tests for pattern.py and library.py
scan.py … bench.py  the step-by-step scripts above
CLAUDE.md           the original brief
```

Built with the MCP Python SDK v2 (`mcp`), `bleak` 3 and `webcolors`.
