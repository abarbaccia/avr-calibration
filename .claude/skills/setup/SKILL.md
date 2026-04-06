---
name: setup
version: 1.0.0
description: |
  Interactive equipment configuration. Asks about hardware (AVR, DSP, subs, mic),
  generates config.yaml, configures MCP server connection, and verifies connectivity.
allowed-tools:
  - Bash
  - Read
  - Write
  - Edit
  - Glob
  - Grep
  - Agent
  - AskUserQuestion
---

# /setup

Configure your calibration equipment interactively.

## Arguments

- `$ARGUMENTS` — optional: "reset" to start fresh, otherwise resume/update existing config.

## Workflow

### Step 1 — Discover existing config

```
Read ~/.avr-calibration/config.yaml if it exists.
If found, show the user what's currently configured and ask if they want to update or start fresh.
```

### Step 2 — Ask about hardware

Ask the user about their equipment. Be conversational, not a form. Ask one topic at a time:

1. **AVR**: Model and IP address. Offer to auto-discover via `discover_avr` MCP tool.
2. **DSP**: Model (miniDSP 2x4 HD is default). IP/port of minidspd daemon.
3. **Subwoofers**: How many, which miniDSP outputs they're connected to, any bass shakers?
4. **Microphone**: UMIK-1 or UMIK-2? Do they have a .cal correction file?
5. **Playback route**: USB (direct to miniDSP) or HDMI (through AVR)?
6. **DSP signal path**: If the DSP has multiple inputs (e.g. miniDSP 2x4 HD has 2 analog inputs), ask which input the AVR feeds. This determines routing — the active input must be routed to all sub outputs. Also ask if any inputs are broken/unused so they can be muted to avoid noise.

### Step 3 — Generate config.yaml

Write `~/.avr-calibration/config.yaml` with the collected information:

```yaml
denon:
  host: "192.168.1.xxx"  # from discovery or user input

minidsp:
  host: "localhost"
  port: 5380
  active_input: 1  # which analog input the AVR feeds (0 or 1)
  output_slots:
    - index: 0
      type: sub
      label: "Sub 1"
    - index: 1
      type: sub
      label: "Sub 2"
    - index: 2
      type: shaker
      label: "Bass shaker"
    - index: 3
      type: unused

mic:
  name: "UMIK"
  cal_file: null  # or path to .cal file

measurement:
  playback_route: "usb"  # or "hdmi"
  sample_rate: 48000
```

### Step 4 — Verify connectivity

Call the `check_system` MCP tool to verify all hardware is reachable.
Report results and help debug any failures.

### Step 5 — Configure MCP connection

Check if `.claude/mcp.json` exists and has the avr-calibration server configured.
If not, create/update it:

```json
{
  "mcpServers": {
    "avr-calibration": {
      "url": "http://192.168.1.117:8765/mcp"
    }
  }
}
```

## Important rules

1. **Auto-discover when possible.** Use `discover_avr` to find the Denon automatically.
2. **Validate inputs.** IP addresses should be valid, ports should be in range.
3. **Don't overwrite without asking.** If config exists, confirm before replacing.
4. **Test the connection.** Always run `check_system` after writing config.
