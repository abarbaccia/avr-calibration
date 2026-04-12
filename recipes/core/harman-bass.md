# Recipe: Harman Bass Target
version: 1.0
target: harman_bass
hardware: ported sub, tuning_freq ~22Hz (SVS PB12-NSD)

## Filter Strategy

**This recipe uses PEQ only. FIR filters are not used.**

| Layer | Tool | Purpose |
|-------|------|---------|
| Output PEQ | `apply_eq` | Harman target curve + room correction |
| FIR | — | **Not used in this recipe** |

If you have multiple subs, use `harman-bass-persub` or `harman-bass-aligned` instead.
If you want FIR-based correction, use a FIR-capable recipe.

## Overview

Calibrate a ported subwoofer to the Harman bass target curve. The Harman target
applies a gentle downward slope from ~80 Hz to 20 Hz (+5 to +6 dB shelf at the
bass frequencies) rather than a flat response. Research by Sean Olive and others
at Harman shows this preference across a wide range of listeners.

## Strategy

### Step 0 — Clear existing EQ

Before taking the baseline measurement, reset the sub output to a known zero state:
call `apply_eq` with **only the mandatory 18Hz HPF** (no other filters).

`read_eq` only tracks in-memory state since server start and returns [] after a
restart even if old filters remain on the hardware. Always clear explicitly so the
baseline measurement reflects the true room response, not the room plus prior EQ.

### Step 1 — Baseline measurement

Call `get_measurement_history(limit=1)` to retrieve the most recent measurement.
If no measurement exists, instruct the user to take one in the browser
(https://avr-cal.local:8000) and return once done.

### Step 2 — Anchor the target curve

Compute the optimal reference level for the Harman target. Find the highest
reference SPL where no frequency band requires more than +6 dB of boost:

  ref = min(measured(f) - harman_offset(f) + 6) across all frequencies in 20-200Hz

This maximizes bass extension while staying within the +6 dB safety limit.
Most corrections will be cuts. Report the chosen reference level.

### Step 3 — Analyze fixability

Call `analyze_phase(session_id)` on the baseline measurement to determine which
deviations from target are fixable with EQ:
- `fixable=True`: minimum-phase error — PEQ can correct it
- `fixable=False`: excess-phase (cancellation) — repositioning the sub is more effective
- Check coherence in measurement data — low coherence (<0.8) means unreliable data

This avoids wasting PEQ slots on unfixable problems.

### Step 4 — Analyse the current response

Compare the measured SPL at each 1/3-octave band (20–200 Hz) against the Harman
bass target anchored at the reference from Step 2. The target relative to 80 Hz is:

| Frequency (Hz) | Target relative to 80 Hz |
|---------------|--------------------------|
| 200            | -2 dB                    |
| 160            | -1 dB                    |
| 125            | 0 dB                     |
| 100            | 0 dB                     |
| 80             | 0 dB (reference)         |
| 63             | +1 dB                    |
| 50             | +2 dB                    |
| 40             | +3 dB                    |
| 31.5           | +4 dB                    |
| 25             | +5 dB                    |

Note: the Harman target is a preference model, not a physical law. Adjust
interpretation based on room acoustics — a room with significant bass buildup
below 50 Hz should target less bass lift than a flat room.

### Step 5 — Calculate corrections

For each 1/3-octave band, calculate the correction needed:
  correction_db = target_db - measured_db

Only design corrections for bands where `analyze_phase` reported `fixable=True`.
Skip unfixable bands — they are cancellation nulls that EQ cannot help.

For each filter, call `optimize_q(session_id, freq_hz, target_gain_db)` to find
the best Q value. For ringing modes (from `analyze_decay`), use the `suggested_q`.

Apply corrections as peaking EQ bands. Prefer cuts over boosts where possible
— cuts are always safe; boosts are limited by SafetyValidator.

Safety constraints (enforced automatically by apply_eq):
- Minimum boost frequency: 25 Hz
- Maximum boost per band: +6 dB
- Maximum cumulative boost in any 1/3-octave: +9 dB
- Maximum change per iteration: +3 dB/band
- Mandatory infrasonic HPF at 18 Hz must always be included

If a correction exceeds a limit, clip it to the maximum safe value and note
it as a partial correction. Do not attempt to apply the over-limit value.

### Step 6 — Verify in simulation

Call `simulate_eq(session_id, filters)` with the proposed filter set. Check the
predicted FR against the Harman target. If the prediction shows remaining issues,
adjust filters and re-simulate. Iterate in simulation until satisfied — this is
free (no hardware writes, no new measurements needed).

### Step 7 — Apply corrections

Call `apply_eq(filters)` with the simulation-verified filter list. Always include the
mandatory 18 Hz HPF:

```json
[
  {"freq": 18.0, "gain_db": 0.0, "q": 0.707, "type": "hpf"},
  {"freq": 80.0, "gain_db": -2.0, "q": 0.707, "type": "peaking"},
  ...
]
```

If `apply_eq` returns `{ok: false, error: "SafetyValidator: ..."}`:
1. Read the specific violation in the error message
2. Adjust the offending filter (reduce the gain or move the frequency)
3. Retry with the adjusted filter set

### Step 8 — Re-measure and iterate

After applying filters, ask the user to take a new measurement in the browser.
Retrieve it with `get_measurement_history(limit=1)` and compare to the anchored target.

Do NOT re-anchor the target between iterations (it was set in Step 2).

On each subsequent iteration:
- Call `read_eq` to get the **currently applied** filter set
- Compute the residual deviation from the anchored target based on the new measurement
- Merge the additional correction into the existing filters (adjust gains at the
  same frequency, add new bands for new frequencies)
- Call `apply_eq` with the **full merged set** — never with just the delta.
  `apply_eq` replaces all PEQ slots: a delta-only write discards all prior corrections.

Repeat until convergence or the maximum iteration count is reached.

## Convergence

Stop when: RMS deviation from Harman target < 2 dB across 20–200 Hz
(computed over 1/3-octave bands — 10 measurement points)

Maximum iterations: 5

Per-iteration maximum change: +3 dB/band (SafetyValidator enforces this
automatically — each iteration applies at most 3 dB of additional boost
in any direction per band)

If convergence is not reached after 5 iterations, report the final RMS
deviation and remaining deviations per band. Advise on whether room
acoustics or placement are likely contributing factors.

## Notes

**Ported sub below port resonance:** The SVS PB12-NSD has a port tuned to ~22 Hz.
Below this frequency, output rolls off steeply. Do not boost below 25 Hz —
SafetyValidator enforces this. Deep bass at 20 Hz should be handled by the
mandatory HPF, not boost.

**Room modes:** Large peaks in the 40–80 Hz range are often room modes, not
sub response. Cut them rather than boosting the surroundings. Cuts are always
safe and are not limited by SafetyValidator.

**Multiple measurements:** For more reliable results, ask the user to take 3
measurements at slightly different mic positions (within 0.5 m) and average them
by calling `get_measurement_history(limit=3)`. Average the SPL values across
measurements at each frequency before calculating corrections.

**Harman target calibration:** The relative target values above assume the
listening position is in a room with typical bass gain (rooms tend to add
3–6 dB at low frequencies relative to open space). If the room is unusually
well-damped at low frequencies, reduce the bass lift values by 2–3 dB.
