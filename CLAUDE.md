# hue-mcp — brief for Claude Code

## Goal
A demo MCP server, in Python, that lets Claude control the user's Philips Hue bulb
over **Bluetooth Low Energy** directly from this Mac. There is **no Hue Bridge**.

This follows on from `../mcp-lamp` (TypeScript, MCP SDK v2), which controls a fake on-screen
"lamp" with tools `get_lamp`, `set_lamp` (on / color / brightness 0–100) and `start_lamp`.
Mirror that tool design so the two demos are easy to compare. The user is learning MCP with a
long-term view to building MCP servers and skills for university researchers, so keep the code
small, readable and well commented. Teaching value matters more than features.

## Environment
- macOS on Apple Silicon. Python via `uv` is preferred (check what's installed first).
- BLE library: `bleak`. MCP: the official Python SDK (`mcp`, FastMCP). Check current versions
  and APIs before writing code rather than relying on memory.
- macOS will ask for Bluetooth permission for whichever app runs Python (Terminal, iTerm,
  Claude Desktop…). If scanning finds nothing, check System Settings → Privacy & Security → Bluetooth.

## Suggested plan (do it in this order, confirm each step with the user)
1. **Discover:** a script that scans for BLE devices and lists likely Hue bulbs.
2. **Connect and explore:** connect to the bulb, list its services and characteristics.
3. **Pairing:** Hue bulbs usually only accept a new device while in pairing mode (normally
   triggered from the Hue app, e.g. via the voice-assistant or "make visible" option; a reset
   may be needed). Expect this to be the fiddly part and walk the user through it.
4. **On/off**, then **brightness**, then **colour**, each as a tiny standalone script first.
5. **Wrap in MCP:** a stdio FastMCP server exposing `get_hue` / `set_hue` (+ maybe `scan`).
   Keep one long-lived BLE connection if practical, and reconnect gracefully.
6. **Connect it** to Claude Code (`claude mcp add`) and Claude Desktop, and write a README.

## Unverified notes (check on the real bulb before trusting)
Community reverse-engineering suggests the Hue light-control GATT service is
`932c32bd-0000-47a2-835a-a8d455b859dd`, with characteristics `932c32bd-0002-…` (power),
`…-0003-…` (brightness, ~1–254), `…-0004-…` (colour temperature) and `…-0005-…` (colour, CIE xy).
Confirm by listing the bulb's characteristics in step 2.

## Rules
- stdio MCP server: never print to stdout; log to stderr.
- Ask before resetting or re-pairing the bulb.
