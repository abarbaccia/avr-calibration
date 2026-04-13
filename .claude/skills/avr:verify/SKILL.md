---
name: avr:verify
version: 1.0.0
description: |
  Post-calibration full-room verification. Measures the combined system response
  after sub calibration and Audyssey, checks crossover integration, sub-to-main
  level balance, and recommends Denon setting adjustments.
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

# /avr:verify

Verify full-room integration after sub calibration and Audyssey.

## Arguments

- `$ARGUMENTS` — optional: "quick" for subs-only check, or default for full verification.

## Workflow

### Step 1 — Pre-flight and context

1. Call `check_system` to verify hardware is reachable.
2. Call `get_config` to understand the output layout (sub/shaker/unused outputs).
3. Call `get_device_state` to check the Denon's current state (volume, input, sound mode).
4. If the Denon is in Pure Direct or Direct mode, **STOP** and ask the user to switch
   to their normal listening mode. Audyssey must be active for verification.

Ask the user:
1. "What crossover frequency is set on the Denon?" (default: 80Hz)
2. "What Audyssey curve are you using?" (Reference, Flat, or Off)
3. "Is Dynamic EQ enabled?"

These affect interpretation of the results.

### Step 2 — Load the verification recipe

Read the recipe at `recipes/core/full-room-verify.md` for the full procedure.
Follow it step by step, using the MCP tools listed below.

### Step 3 — Sub-only baseline (Phase 1)

1. Measure subs through the normal calibration path.
   Use label "verify-subs-only", position "MLP".
2. Call `get_measurement_history(limit=1, min_hz=20, max_hz=200, format="compact")`
   to retrieve detailed FR.
3. If previous calibration runs exist (check `get_calibration_runs`), compare current
   sub response to the last calibration result. Flag any drift.
4. Record: average SPL 30-80Hz, RMS from target, -3dB rolloff, notable peaks/nulls.

If `$ARGUMENTS` is "quick", skip to Step 6 after this — report subs-only status.

### Step 4 — Full-system measurement (Phase 2)

1. Confirm the Denon is in normal listening mode (not Pure Direct).
2. Call `measure` with `full_range=true` to capture the combined room response
   with all speakers and Audyssey active.
   Use label "verify-full-system", position "MLP".
3. Call `get_measurement_history(limit=1, min_hz=20, max_hz=200, format="compact")`.

**If `full_range` is not yet supported:**
- Tell the user: "Full-range measurement mode isn't available yet. I'll measure
  the sub-bass range and focus the crossover analysis on what we can see."
- Take a standard measurement and note the limited range in the report.
- Ask the user to confirm Denon is in normal listening mode and volume is moderate.

### Step 5 — Crossover and level analysis (Phases 3-4)

Compare the sub-only and full-system measurements:

**Crossover integration (40-160Hz around the user's crossover frequency):**
- Level continuity: full-system should be 3-6dB above subs-only at crossover
- Dip at crossover = phase cancellation between subs and mains
- Peak >6dB at crossover = excessive overlap
- Rate: Good / Fair / Poor / Critical (see recipe for thresholds)

**Sub level balance:**
- Sub band (30-60Hz) vs main band (200-2kHz) from full-system measurement
- For Harman target: expect sub band +3 to +5dB above main band
- For flat target: expect sub band to match main band (±1dB)
- Compute specific trim adjustment if outside expected range

**Dynamic EQ interaction:**
- If enabled, note it adds bass boost at lower volumes
- Warn if combined boost (our EQ + Dynamic EQ) seems excessive

### Step 6 — Report

Present a structured integration report:

```
## Full-Room Integration Report

### System Configuration
- Audyssey curve: [Reference/Flat/Off]
- Crossover: [X]Hz
- Dynamic EQ: [On/Off]
- Sub calibration: [recipe name, date if available]

### Sub Calibration Status
- Average SPL (30-80Hz): XX dB
- RMS from target: X.X dB
- Bass extension (-3dB): XX Hz
- Status: [Holding / Drifted — re-run /avr:calibrate if drifted]

### Crossover Integration (40-160Hz)
Rating: [Good/Fair/Poor/Critical]
[Specific findings — level continuity, phase, slope matching]

### Level Balance
Sub band (30-60Hz): XX dB
Main band (200-2kHz): XX dB
Difference: XX dB → [matches target / subs too loud by X / subs too quiet by X]

### Recommendations (priority order)
1. [Most impactful adjustment with specific values and reason]
2. [Next adjustment]
...

### Denon Settings to Check
| Setting | Current | Suggested | Menu path |
|---------|---------|-----------|-----------|
| Sub trim | ? | ±X dB | Settings → Audio → Subwoofer Level |
| Crossover | X Hz | [keep/change] | Settings → Speakers → Crossovers |
| Sub distance | ? | ±X ft | Settings → Speakers → Distance |
| Audyssey curve | X | [keep/change] | Settings → Audio → MultEQ XT32 |
```

After presenting the report, suggest: "Make the recommended adjustments on the Denon,
then run `/avr:verify` again to confirm they helped."

### Step 7 — Re-run sub calibration?

If the analysis suggests sub calibration needs refreshing (sub drift detected,
crossover frequency changed, large sub trim adjustment needed), recommend:
"Run `/avr:calibrate` to re-tune the subs for the new Denon settings, then `/avr:verify` again."

## Key MCP tools

- `check_system` — pre-flight
- `get_config` — output layout
- `get_device_state` — Denon state
- `measure` — frequency response sweeps (sub-only and full-range)
- `get_measurement_history(min_hz=20, max_hz=200, format="compact")` — detailed FR data
- `get_fr_summary` — quick 1/3-octave overview
- `get_calibration_runs` — past calibration history

## Important rules

1. **Don't apply EQ.** This skill measures and recommends. Sub EQ changes go through `/avr:calibrate`.
2. **Denon adjustments are manual.** Give the user exact menu paths and specific dB values.
3. **Check sound mode first.** Verification MUST be done with Audyssey active, not Pure Direct.
4. **After adjustments, re-verify.** Always suggest running `/avr:verify` again after changes.
5. **Describe every measurement.** Before each sweep, explain what's being measured and why.
6. **Use compact format.** Always pass `format="compact"` to `get_measurement_history`.
