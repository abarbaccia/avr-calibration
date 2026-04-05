# MCP Server Setup

Connect Claude Code to your Pi's hardware so you can calibrate without touching the browser.

## What this gives you

Once connected, you can ask Claude Code things like:
- "Measure the room and apply Harman bass corrections"
- "What's my current EQ?"
- "What does my sub response look like at 80 Hz?"
- "Compare my last two measurements and show what changed"

Claude calls the MCP tools directly — sweep, read EQ, apply corrections, re-sweep.

## Prerequisites

- Pi 5 (recommended) or Pi Zero 2 W with the avr-calibration container running
- UMIK-1 or UMIK-2 connected to the Pi via USB
- Claude Code installed on your laptop
- Pi reachable on your local network

## Step 1 — Deploy the container

The MCP server is included in the `:latest` image.

```bash
# On the Pi — pull latest and restart:
sudo docker pull ghcr.io/abarbaccia/avr-calibration:latest
sudo systemctl restart avr-calibration
```

If you haven't deployed yet, run the one-line installer:
```bash
bash <(curl -sL https://raw.githubusercontent.com/abarbaccia/avr-calibration/main/deploy/install.sh)
```

## Step 2 — Configure your Pi

Edit `/home/pi/.avr-calibration/config.yaml`:

```yaml
denon:
  host: "192.168.1.209"          # your Denon IP
minidsp:
  host: "localhost"
  port: 5380
mic:
  name: "UMIK"
measurement:
  denon_sweep_input: "Videocore" # exact input name from your Denon — see tip below
  denon_sweep_volume: -25.0      # master volume during sweep (restored after)
```

> **Finding your exact Denon input name:** Input names depend on your AVR model and any renaming you've done in the Denon setup menu. The endpoint will return HTTP 503 with the available list if the configured name is wrong:
> ```bash
> curl -sk https://<pi-ip>:8000/api/equipment/denon | python3 -m json.tool | grep -A20 inputs
> ```

## Step 3 — Verify the MCP server is running

```bash
# From your laptop:
curl http://<pi-ip>:8765/sse
# SSE connection opens and hangs — that's correct

# Or check container logs on the Pi:
sudo docker logs avr-calibration | grep -i mcp
# Should show: "MCP server starting on 0.0.0.0:8765"
```

## Step 4 — Configure Claude Code

The `.claude/mcp.json` file in this repo has the config. Update the URL with your Pi's IP:

```json
{
  "mcpServers": {
    "avr-calibration": {
      "type": "sse",
      "url": "http://<pi-ip>:8765/sse"
    }
  }
}
```

Claude Code picks this up automatically when run from the repo directory. For global access, add it to `~/.claude/mcp.json` instead.

## Step 5 — Test the connection

Run `claude` from this repo and try:

```
What's my current EQ?
```

Claude should call `get_current_eq` and describe the filter state. If it doesn't use the tools, check that you're running from the repo directory and that the MCP URL is reachable.

## Available tools

| Tool | Description |
|------|-------------|
| `trigger_measurement` | Run a headless sweep via UMIK, returns session ID |
| `get_frequency_response` | Fetch FR data for a session |
| `get_current_eq` | Read current miniDSP EQ settings |
| `apply_eq_corrections` | Write EQ band changes (SafetyValidator enforced) |
| `get_sessions` | List measurement history |
| `check_hardware` | Verify Denon, miniDSP, and mic are reachable |
| `get_device_state` | Current Denon + miniDSP status |
| `set_denon_volume` | Set Denon AVR master volume |

## Security note

The MCP server has no authentication — same LAN trust model as the web UI. Don't expose port 8765 to the internet. For remote access, use SSH tunneling:

```bash
ssh -L 8765:localhost:8765 pi@<pi-ip>
# Then set the MCP URL to: http://localhost:8765/sse
```

## Troubleshooting

**Claude doesn't use the MCP tools:**
- Make sure you're running `claude` from the repo directory
- Check that `.claude/mcp.json` exists and has the correct Pi IP

**Connection refused on port 8765:**
- Verify the container is running: `sudo docker ps | grep avr`
- Check logs: `sudo docker logs avr-calibration | tail -50`

**HTTP 503 on sweep — "Input not found":**
- The configured `denon_sweep_input` doesn't match your Denon's input list
- Check available inputs: `curl -sk https://<pi-ip>:8000/api/equipment/denon`
- Update `config.yaml` with the exact name shown

**SafetyValidator errors when applying EQ:**
- The error message specifies which limit was violated
- Reduce boost amount or move the frequency above 25 Hz
- Claude will adjust automatically if you ask it to retry
