# avr-calibration

AI-first home theater calibration — closed-loop bass optimization.

Tired of endless manual loops with REW, miniDSP, and OCA? This tool closes the loop: measure → AI analyzes (measurements + how it sounds) → apply changes → re-measure → repeat until converged on a target curve.

## What it does

- **Automated measurement** via PyTTa (log sweep + UMIK mic calibration)
- **AI analysis** with Claude — reads both frequency response graphs and subjective feedback ("the Fury Road chase scene sounded muddy")
- **Hardware control** via miniDSP 2x4 HD (through minidsp-rs) and Denon X3800H (through denonavr)
- **Safety rails** — hard limits on boost depth and frequency floor protect your drivers
- **Convergence** against the Harman target curve (or your own)

## Hardware

- Denon X3800H (or other Denon/Marantz AVR)
- miniDSP 2x4 HD
- UMIK-1 or UMIK-2 measurement microphone
- Subwoofer(s) — initially tuned for SVS PB12-NSD

## Quick start (local dev)

```bash
# Set up environment
uv venv .venv && source .venv/bin/activate
uv sync --extra dev

# Configure
calibrate check           # creates ~/.avr-calibration/config.yaml if missing
# edit the config with your Denon IP, then:
calibrate check           # verify all hardware is reachable

# Inspect results
calibrate history         # list all past sessions
calibrate show 1          # detail view: ASCII plot, peak SPL, band
calibrate show 1 --csv    # export frequency response as CSV
calibrate show 1 --json   # export as JSON
```

## Deployment

Designed to run on a **Raspberry Pi Zero W** permanently installed in your rack as a
Docker container. The image is pre-built for `linux/arm/v6` and `linux/amd64` — no
source compilation on the Pi.

```bash
# On the Pi Zero W — one command installs Docker, pulls image, and starts the service:
bash <(curl -sL https://raw.githubusercontent.com/abarbaccia/avr-calibration/main/deploy/install.sh)
# Web UI: http://<pi-ip>:8000
```

See [docs/deployment/pi-zero-w.md](docs/deployment/pi-zero-w.md) for the full guide.

## Requirements

- Python 3.11+
- [minidsp-rs](https://github.com/mrene/minidsp-rs) daemon running (`minidspd`)
- Denon AVR on your local network
- UMIK-1/2 connected via USB (to the measurement client, not the Pi)

## Status

Early development. Currently implemented:

- `calibrate check` — hardware pre-flight verification (UMIK mic, miniDSP, Denon AVR)
- `calibrate measure` — log-sweep frequency response measurement via PyTTa + SQLite session history
- `calibrate history` — list past sessions with timestamp, label, peak SPL, and point count
- `calibrate show <id>` — session detail with ASCII frequency response plot; `--csv` and `--json` export
- `calibrate web` — start web server (Pi serves UI; browser captures UMIK audio)
- `Dockerfile` + `.github/workflows/docker.yml` — multi-platform Docker image (arm/v6 + amd64), built and pushed to GHCR via GitHub Actions
- `deploy/install.sh` — Pi Zero W bootstrap: installs Docker, pulls GHCR image, starts systemd service

Next: AI analysis, miniDSP write adapter, closed loop.

See [TODOS.md](TODOS.md) for the roadmap.
