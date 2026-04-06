---
name: measure
version: 1.0.0
description: |
  Take a single measurement and analyze the frequency response.
  Compares to target curve if specified.
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

# /measure

Take a single frequency response measurement and analyze the result.

## Arguments

- `$ARGUMENTS` — optional: target curve name (e.g. "harman") to compare against.

## Workflow

### Step 1 — Pre-flight

1. Call `check_system` to verify hardware is ready.
2. If any check fails, report and ask the user if they want to proceed anyway.

### Step 2 — Take measurement

1. Call `measure` MCP tool to trigger a sweep.
2. Call `get_measurement_history` with limit=1 to retrieve the result.

### Step 3 — Analyze

Report the frequency response analysis:

- **Bass extension**: -3dB and -6dB points (where the response drops off)
- **Room modes**: Frequencies with peaks or dips > 6dB from the mean
- **Smoothness**: Standard deviation of SPL across the measurement band
- **Overall level**: Average SPL in the measurement band

If a target curve was specified:
- **RMS deviation** from target across the measurement band
- **Per-frequency deviation**: which frequencies are above/below target and by how much
- **Suggested EQ corrections**: what filters would improve the match (informational only, not applied)

### Step 4 — Compare to history

If there are previous measurements:
- Compare to the most recent measurement.
- Note any changes (better/worse extension, mode changes, level changes).

### Step 5 — Report

Present results clearly:
- Use a simple table for frequency response data if helpful.
- Highlight the most important finding (e.g. "You have a 12dB null at 45Hz — this is likely a room mode").
- Suggest next steps if appropriate (e.g. "Try /calibrate to EQ this response to a target").

## Important rules

1. **Don't apply anything.** This skill only measures and reports. Use /calibrate to apply EQ.
2. **Be specific.** Name exact frequencies and dB values.
3. **Explain room modes.** Users often don't understand why they have a null at 45Hz. Briefly explain.
