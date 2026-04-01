# MCP Server Setup

Connect Claude Code to your Pi's hardware so you can calibrate without the browser.

## What this gives you

Once connected, you can ask Claude Code things like:
- "What's my current EQ?"
- "Apply Harman bass corrections from my last measurement"
- "What does my sub response look like at 80 Hz?"

Claude calls the tools directly — no browser clicks required.

## Prerequisites

- Pi Zero 2 W or Pi 4 with the AVR calibration container running
- Claude Code installed on your laptop (`npm install -g @anthropic-ai/claude-code`)
- Pi reachable on your local network (test: `ping avr-calibration.local`)

## Step 1 — Deploy the updated container

The MCP server is included in the container image starting from this release.

**Option A — hotfix (fastest, for Pi deployments already running):**
```bash
./deploy/hotfix.sh calibrate/mcp_server.py calibrate/safety.py calibrate/dsp.py
```

**Option B — pull the latest branch image:**
```bash
# On the Pi:
sudo docker pull ghcr.io/abarbaccia/avr-calibration:feat-mcp-server
sudo systemctl restart avr-calibration
```

**Option C — docker compose (development machine):**
```bash
docker compose up --build
```

## Step 2 — Verify the MCP server is running

From your laptop:
```bash
curl http://avr-calibration.local:8765/sse
# Should respond (SSE connection opens, may hang — that's correct)
```

Or check the container logs:
```bash
# On the Pi:
sudo docker logs avr-calibration | grep -i mcp
# Should show: "MCP server starting on 0.0.0.0:8765"
```

## Step 3 — Configure Claude Code

The `.claude/mcp.json` file in this repo already has the configuration:

```json
{
  "mcpServers": {
    "avr-calibration": {
      "url": "http://avr-calibration.local:8765/sse"
    }
  }
}
```

If your Pi has a different hostname or IP, edit this file:
```bash
# Replace with your Pi's hostname or IP:
# "url": "http://192.168.1.50:8765/sse"
```

Claude Code picks up `.claude/mcp.json` automatically when you run it from
this repo directory.

## Step 4 — Test the connection

In a Claude Code session (run `claude` from this repo):

```
What's my current EQ?
```

Claude should call `read_eq()` and describe the filter state. If it doesn't
use the tools, check that you ran `claude` from the repo directory (where
`.claude/mcp.json` lives).

## Available tools

| Tool | What it does |
|------|-------------|
| `get_device_state` | Current Denon + miniDSP status |
| `get_measurement_history(limit)` | Last N measurements from the Pi's database |
| `read_eq` | Current EQ filter state |
| `apply_eq(filters)` | Apply EQ filters (SafetyValidator enforced) |
| `set_denon_volume(level_db)` | Set Denon AVR volume |
| `trigger_measurement` | Take a measurement (Pi 4 + UMIK-1 required) |
| `fetch_recipe(name)` | Get a calibration recipe (e.g. `core/harman-bass`) |

## Resources

| URI | Contents |
|-----|---------|
| `measurements://latest` | Most recent measurement session |
| `eq://current` | Current EQ state |

## Pi Zero 2 W — trigger_measurement

The Pi Zero 2 W has one USB port, taken by the miniDSP. `trigger_measurement`
returns a structured error on Pi Zero:

```
trigger_measurement requires Pi 4 — take a measurement in the browser
and use get_measurement_history() to retrieve it.
```

All other tools (read_eq, apply_eq, get_measurement_history, fetch_recipe, etc.)
work on Pi Zero.

## Security note

The MCP server has no authentication — same LAN trust model as the FastAPI server.
Do not expose port 8765 to the internet. If you need remote access, use a VPN or
SSH tunnel:

```bash
ssh -L 8765:localhost:8765 pi@avr-calibration.local
# Then set the MCP URL to: http://localhost:8765/sse
```

## Troubleshooting

**Claude doesn't use the MCP tools:**
- Make sure you're running `claude` from the repo directory (`.claude/mcp.json` must be present)
- Check `claude --version` — MCP support requires a recent version

**Connection refused on port 8765:**
- Check the container is running the new image (with MCP server included)
- On Pi: `sudo docker logs avr-calibration | tail -50`

**SafetyValidator errors when applying EQ:**
- The error message tells you exactly which limit was violated
- Reduce the boost amount or move the frequency above 25 Hz
- Claude will adjust automatically if you ask it to retry

**EQ state lost after restart:**
- `read_eq` tracks state in-memory — it resets when the container restarts
- Re-apply your filters, or check the measurement history for previously applied filter sets
