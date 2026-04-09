# avr-calibration

AI-first home theater calibration — closed-loop bass optimization via Claude Code + MCP.

Tired of endless manual loops with REW, miniDSP, UMIK-1? This closes the loop: **measure → AI analyzes → apply EQ changes → re-measure → converge**.

The primary interface is Claude Code talking directly to your hardware through an MCP server. No browser clicks required for calibration.

## How it works

```
Claude Code (your laptop)
    │
    │  MCP tools (trigger sweep, read EQ, apply corrections)
    ▼
Pi 5 — avr-calibration service (Docker)
    ├── UMIK-1 (USB) — headless log sweep via sounddevice + PyTTa
    ├── Denon X3800H — auto power on/off, input switch, volume control
    ├── miniDSP 2x4 HD — EQ reads and writes via minidsp CLI (WebSocket)
    └── SQLite — measurement history
```

Claude reads your frequency response, compares against the Harman target curve, proposes EQ corrections within safety limits, applies them, and re-measures. You stay in the loop — writes require your confirmation.

## Hardware

- **Pi 5** (recommended) or Pi Zero 2 W — runs the service permanently in your rack
- **Denon X3800H** (or other Denon/Marantz AVR with denonavr support)
- **miniDSP 2x4 HD** — subwoofer EQ and routing
- **UMIK-1 or UMIK-2** — connected to the Pi via USB
- Subwoofer(s) — initially tuned for SVS PB12-NSD

## Quick start

### 1. Deploy to Pi

```bash
# One command: installs Docker, pulls image, starts service
bash <(curl -sL https://raw.githubusercontent.com/abarbaccia/avr-calibration/main/deploy/install.sh)
```

Edit `/home/pi/.avr-calibration/config.yaml` with your hardware details:

```yaml
denon:
  host: "192.168.1.209"
minidsp:
  host: "localhost"
  port: 5380
mic:
  name: "UMIK"
measurement:
  denon_sweep_input: "Videocore"   # must match Denon's exact name — see tip below
  denon_sweep_volume: -25.0
```

> **Finding your Denon input name:** Input names vary by AVR and user renaming. Check what yours are called:
> ```bash
> curl -sk https://<pi-ip>:8000/api/equipment/denon | python3 -m json.tool | grep -A20 inputs
> ```

### 2. Connect Claude Code via MCP

Add to your MCP config (`.claude/mcp.json` in this repo, or `~/.claude/mcp.json` globally):

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

Then just talk to Claude Code:

```
measure the room and apply Harman bass corrections
```

Claude will sweep, analyze, propose EQ changes, and apply them — asking before any write.

See [docs/mcp-setup.md](docs/mcp-setup.md) for the full MCP setup guide.

## MCP tools

| Tool | Description |
|------|-------------|
| `measure` | Trigger a sweep via UMIK, saves session |
| `get_measurement_history` | Fetch FR data for recent sessions |
| `read_eq` | Read current miniDSP EQ filter state |
| `apply_eq` | Write EQ filters to DSP output(s) (SafetyValidator enforced) |
| `apply_input_eq` | Write EQ filters to the shared DSP input channel |
| `get_calibration_runs` | List calibration run history |
| `check_system` | Pre-flight: verify Denon, miniDSP, and mic are reachable |
| `get_device_state` | Current AVR + DSP hardware state |
| `set_volume` | Set AVR master volume |
| `calibrate_level` | Auto-calibrate sweep volume by SNR |
| `mute_output` | Mute DSP outputs (for solo sub measurement) |
| `unmute_output` | Unmute DSP outputs |
| `set_delay` | Set per-output delay in ms (sub time alignment) |
| `set_polarity` | Set per-output polarity inversion |
| `set_output_gain` | Set per-output gain trim in dB |
| `get_output_state` | Per-output gain, delay, polarity, and FIR tap count |
| `analyze_ir` | Extract IR peak time, polarity, and SPL for alignment |
| `analyze_decay` | Analyze room-mode T60 decay; identify ringing frequencies |
| `apply_fir` | Write FIR coefficients to a DSP output |
| `clear_fir` | Clear FIR on a DSP output (reset to passthrough) |
| `configure_matrix` | Configure DSP routing matrix |
| `fetch_recipe` | Load a calibration recipe by name |
| `get_config` | Return current config.yaml |
| `set_config` | Deep-merge updates into config.yaml |
| `discover_avr` | SSDP scan to find Denon/Marantz AVRs on the network |

## Headless measurement API

```bash
# Trigger a sweep from anywhere on the network
curl -sk -X POST https://<pi-ip>:8000/api/measure \
  -H 'Content-Type: application/json' \
  -d '{"label": "post-eq"}'
# → {"session_id": 4, "status": "ok"}
```

The endpoint automatically: validates your configured Denon input against the live input list, powers on the Denon if off, switches input and volume, records the sweep, then restores original state.

## Safety limits (SVS PB12-NSD)

All EQ writes go through `SafetyValidator` before reaching the miniDSP:

| Limit | Value |
|-------|-------|
| Minimum boost frequency | 25 Hz |
| Max boost per band | +6 dB |
| Max cumulative boost (1/3 oct) | +9 dB |
| Max change per iteration | +3 dB/band |
| Infrasonic HPF | 18 Hz, 4th-order Butterworth (always on) |

Cuts have no floor — they're always safe.

## Web UI

Available at `https://<pi-ip>:8000` — history viewer, frequency response charts, Harman target overlay, before/after EQ comparison, PNG export.

## Development

```bash
uv venv .venv && source .venv/bin/activate
uv sync --extra dev

# Run tests (100% coverage is the goal)
uv run python -m pytest tests/ -v

# CLI
calibrate --help
calibrate check        # verify all hardware is reachable
```

## Deployment

Docker image built by GitHub Actions on every push:
- Branch push → `ghcr.io/abarbaccia/avr-calibration:<branch-name>`
- Main push → also tagged `:latest`
- Targets: `linux/arm64` (Pi 5), `linux/arm/v7` (Pi Zero 2 W), `linux/amd64`

**Hotfix deploy** (seconds, no rebuild):
```bash
./deploy/hotfix.sh                    # auto-detects modified calibrate/ files
./deploy/hotfix.sh calibrate/web.py   # specific file
```

**Pull new image after CI:**
```bash
sudo docker pull ghcr.io/abarbaccia/avr-calibration:latest
sudo systemctl restart avr-calibration
```

See [docs/deployment/pi-zero-w.md](docs/deployment/pi-zero-w.md) for the full setup guide.
