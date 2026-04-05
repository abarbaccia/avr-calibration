# avr-calibration

AI-first home theater calibration — closed-loop bass optimization for Denon X3800H + miniDSP 2x4 HD + SVS PB12-NSD.

## Architecture

```
[ Claude Code (MCP) ]  ←── control plane
         |
         ▼
[ Pi 5 @ 192.168.1.117 — Docker (arm64) ]
  ├── MCP server     ← Claude drives calibration
  ├── Web UI         ← browser dashboard (read-only)
  ├── MeasurementEngine (PyTTa)
  │     └── PlaybackStrategy (USB | HDMI)
  ├── DenonDriver    ← AVR control (denonavr)
  │     └── DenonSweepContext (input/volume lifecycle)
  ├── MinidspDriver  ← DSP control (minidsp-rs HTTP)
  │     └── SafetyValidator (hard limits, never bypassed)
  └── SessionStore   ← SQLite history
         |
  measure → AI analysis → propose EQ → validate → apply → re-measure → converge
```

## Hardware

- **AVR:** Denon X3800H (denonavr library)
- **DSP:** miniDSP 2x4 HD (minidsp-rs daemon → HTTP)
- **Mic:** UMIK-1 or UMIK-2 (UMIK .cal correction applied)
- **Measurement:** PyTTa (log sweep + deconvolution)
- **Subs:** SVS PB12-NSD (ported, ~22Hz tuning)

## Safety Limits (SVS PB12-NSD)

These are enforced in `SafetyValidator` before any write to miniDSP:
- Minimum boost frequency: **25Hz**
- Max boost per EQ band: **+6 dB**
- Max cumulative boost in any 1/3 octave: **+9 dB**
- Max change per iteration: **+3 dB/band**
- Mandatory infrasonic HPF: **18Hz, 4th-order Butterworth** (always on)
- Cuts: no floor (cuts are always safe)

## Development

```bash
# Set up environment
uv venv .venv && source .venv/bin/activate
uv sync --extra dev

# Run tests
uv run python -m pytest tests/ -v

# Run the CLI
calibrate --help
calibrate check
calibrate measure [--label TEXT]
```

## Testing

100% test coverage is the goal — tests make vibe coding safe.

- Run: `pytest tests/ -v`
- Test files: `tests/test_*.py`
- See `TESTING.md` for conventions

When writing new functions, write a corresponding test.
When fixing a bug, write a regression test.
When adding error handling, write a test that triggers the error.
When adding a conditional, write tests for BOTH branches.
Never commit code that makes existing tests fail.

## Deployment

- Docker image built by GitHub Actions on every branch push
- Branch push → `ghcr.io/abarbaccia/avr-calibration:<branch-name>`
- Main push → also tagged `:latest`
- arm64 cross-compiled in CI; no compilation on the Pi
- Source installed at `/opt/venv/lib/python3.11/site-packages/calibrate/` inside container
- Pi 5 at `192.168.1.117` (user `pi`)

**Primary workflow:** hotfix first, pipeline second.

```
SSH hotfix → validate → git push → CI build → pull latest image → validate → merge
```

**SSH hotfix (seconds, no rebuild):**
```bash
./deploy/hotfix.sh                    # auto-detects modified calibrate/ files
./deploy/hotfix.sh calibrate/web.py   # specific file
```

**Pull latest image after CI build completes:**
```bash
ssh pi@192.168.1.117 "sudo docker pull ghcr.io/abarbaccia/avr-calibration:latest && sudo systemctl restart avr-calibration"
```

## Key design decisions

- **PyTTa** replaces REW as the measurement engine (REW Pro API costs $100; PyTTa is free and sufficient for bass calibration)
- **minidsp-rs** daemon handles USB control of the 2x4 HD; Python speaks HTTP to it
- **denonavr** library handles Denon X3800H control (no reverse-engineering)
- **SQLite** for measurement history storage (single file, queryable)
- **Harman target curve** as the optimization convergence target
- **Claude API with structured JSON output** for AI analysis

## Skill routing

When the user's request matches an available skill, ALWAYS invoke it using the Skill
tool as your FIRST action. Do NOT answer directly, do NOT use other tools first.
The skill has specialized workflows that produce better results than ad-hoc answers.

Key routing rules:
- Product ideas, "is this worth building", brainstorming → invoke office-hours
- Bugs, errors, "why is this broken", 500 errors → invoke investigate
- Ship, deploy, push, create PR → invoke ship
- QA, test the site, find bugs → invoke qa
- Code review, check my diff → invoke review
- Update docs after shipping → invoke document-release
- Weekly retro → invoke retro
- Design system, brand → invoke design-consultation
- Visual audit, design polish → invoke design-review
- Architecture review → invoke plan-eng-review
