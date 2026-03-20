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

## Quick start

```bash
# Install
pip install avr-calibration

# Configure
calibrate check   # creates ~/.avr-calibration/config.yaml if missing
# edit the config with your Denon IP, then:
calibrate check   # verify all hardware is reachable
```

## Requirements

- Python 3.11+
- [minidsp-rs](https://github.com/mrene/minidsp-rs) daemon running (`minidspd`)
- Denon AVR on your local network
- UMIK-1/2 connected via USB

## Status

Early development. Currently implemented: hardware pre-flight check (`calibrate check`).

Next: measurement spike (PyTTa integration), AI analysis module, full calibration loop.

See [TODOS.md](TODOS.md) for the roadmap.
