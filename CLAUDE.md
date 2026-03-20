# avr-calibration

AI-first home theater calibration — closed-loop bass optimization for Denon X3800H + miniDSP 2x4 HD + SVS PB12-NSD.

## Architecture

```
[ PyTTa measurement ]  [ Subjective feedback log ]
         |                         |
         └──────── AI Analysis (Claude API) ──────
                          |
                   SafetyValidator
                   (hard limits, never bypassed)
                          |
              ┌───────────┴──────────┐
       miniDSP adapter          Denon adapter
       (→ minidspd HTTP)        (→ denonavr)
              |
       re-measure → delta vs. Harman target → loop or stop
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

## Key design decisions

- **PyTTa** replaces REW as the measurement engine (REW Pro API costs $100; PyTTa is free and sufficient for bass calibration)
- **minidsp-rs** daemon handles USB control of the 2x4 HD; Python speaks HTTP to it
- **denonavr** library handles Denon X3800H control (no reverse-engineering)
- **SQLite** for measurement history storage (single file, queryable)
- **Harman target curve** as the optimization convergence target
- **Claude API with structured JSON output** for AI analysis
