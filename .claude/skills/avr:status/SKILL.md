---
name: avr:status
version: 1.0.0
description: |
  Current system state review. Shows EQ state, last measurement,
  distance from target, and calibration history.
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

# /avr:status

Review the current state of the calibration system.

## Arguments

None.

## Workflow

### Step 1 — Gather state

Call these MCP tools in parallel:
- `get_device_state` — current AVR + DSP hardware state
- `get_measurement_history` with limit=3 — recent measurements
- `get_calibration_runs` with limit=3 — recent calibration runs (the latest run's
  `full_state_snapshot` is the authoritative record of EQ/delay/gain state; the
  miniDSP itself is write-only)

### Step 2 — Report current EQ

Show the active EQ filters on the DSP:
- Preset number
- List of filters: frequency, gain, Q, type
- Note if no filters are applied (fresh/flat state)

### Step 3 — Report last measurement

If measurements exist:
- When the last measurement was taken
- Key metrics: bass extension, smoothness, average level
- If a target comparison is possible, show RMS deviation

### Step 4 — Calibration history

If calibration runs exist:
- Recipe used
- Convergence status (converged or not)
- Final RMS deviation
- Number of iterations

### Step 5 — Hardware state

- AVR: volume, input, mute status
- DSP: preset, source, connection status

### Step 6 — Summary

One-paragraph summary: "System is calibrated to X with Y dB deviation" or
"System has no active calibration. Run /avr:calibrate to start."

## Important rules

1. **Read-only.** This skill never modifies anything.
2. **Be concise.** Show the important numbers, not raw data dumps.
3. **Suggest next steps.** If the system isn't calibrated, suggest /avr:calibrate.
