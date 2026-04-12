# Recipe: Multi-Sub Harman Bass Calibration

## Goal

Calibrate multiple subwoofers to the Harman bass target curve (20-80Hz).
Level-match and align the subs so they reinforce rather than cancel,
then EQ the combined response to match the target.

## Filter Strategy

**This recipe uses PEQ only. FIR filters are not used.**

| Layer | Tool | Purpose |
|-------|------|---------|
| Output PEQ (shared across subs) | `apply_eq` | Harman target curve + room correction |
| Input PEQ | — | Not used in this recipe |
| FIR | — | **Not used in this recipe** |

If you want per-sub room correction before applying the Harman target, use `harman-bass-persub`.
If you want FIR-based correction, use a FIR-capable recipe.

## Pre-flight

Verify all hardware is connected and reachable before starting.
Mute any non-sub outputs (e.g. bass shakers) during calibration.

## Phase 0 — Level Setup

### 0.0 Clear all sub output EQ

Before any measurements, reset every sub output to a known zero state:
call `apply_eq` with **only the mandatory 18Hz HPF** on each sub output.
This ensures level comparisons and alignment measurements are taken from
a clean baseline, not through invisible prior EQ from a previous session.

### 0.1 Set initial volume

Set AVR to -10 dB as a known-good starting point for measurements.

### 0.2 Measure each sub solo

For each subwoofer output in the config:
1. Mute all other sub outputs
2. Take a measurement
3. Record the peak SPL from the frequency response
4. Unmute

### 0.3 Compare levels and compute trim

The loudest sub is the reference (trim = 0 dB).
For each quieter sub: trim = reference_spl - measured_spl.

### 0.4 Check for large level gaps

If any sub needs more than **10 dB** of digital trim:
- **STOP and tell the user.** Large digital trims waste headroom.
- Report: "Sub [label] is [X] dB quieter than [reference label].
  Turn up the volume knob on [label] to reduce the gap, then re-run."
- Wait for user to confirm they've adjusted the knobs before continuing.

### 0.5 Apply level trims

Apply the computed gain trims to miniDSP output gains.
The loudest sub stays at 0 dB gain. Quieter subs get positive trim.

### 0.6 Calibrate sweep level

Call `calibrate_level` to ramp volume from -10 dB toward reference (0 dB),
finding the optimal sweep volume with good SNR. This volume will be used
for all subsequent measurements in this session.

## Phase 1 — Sub Alignment

### 1.1 Measure each sub solo

For each subwoofer:
1. Mute all other subs
2. Take a measurement — note the session_id
3. Call `analyze_ir(session_id)` — get `peak_time_s`, `peak_sign`, `spl_db`
4. Unmute

### 1.2 Analyze phase relationship

Call `compare_sub_phase(session_a, session_b)` using the solo session IDs.
This shows per-band phase difference, predicted coherent sum, and whether
subs reinforce or cancel at each frequency. Use this to understand the
interaction before applying corrections.

### 1.3 Apply corrections

Compare the per-sub `analyze_ir` results:
- **Delay**: If one sub arrives earlier, delay it to match the others
- **Polarity**: If one sub is out of phase (IR peak inverted), flip its polarity

### 1.4 Verify alignment

Measure all subs together. The combined response should be louder than any
individual sub (reinforcement). If combined is quieter at some frequencies,
subs are still cancelling — use `compare_sub_phase` analysis from 1.2 to
guide delay/polarity adjustments.

Repeat alignment until combined response shows reinforcement across the band.
Maximum 3 alignment iterations.

## Phase 2 — EQ to Harman Target

### 2.0 Clear all sub output EQ

Before measuring, reset every sub output to a known zero state:
for each sub output, call `apply_eq` with **only the mandatory 18Hz HPF**.
This ensures Phase 2 measurements reflect the true room response, not the
room plus whatever filters were left from a prior session.

`read_eq` only tracks in-memory state since server start. If the server
restarted between sessions it returns [] while old filters remain on the
hardware. Always clear explicitly.

### Harman bass target (relative to 80Hz)

| Hz  | Target |
|-----|--------|
| 25  | +5 dB  |
| 31  | +4 dB  |
| 40  | +3 dB  |
| 50  | +2 dB  |
| 63  | +1 dB  |
| 80  | 0 dB   |

### 2.1 Baseline

Measure the combined sub response. Calculate RMS deviation from the Harman target.

### 2.2 Analyze fixability and design corrections

Call `analyze_phase(session_id)` on the combined measurement to determine which
bands are fixable with EQ vs excess-phase (cancellation). Only design corrections
for `fixable=True` bands.

For each filter, use `optimize_q` to find the best Q. Then call
`simulate_eq(session_id, filters)` to verify the predicted FR before applying.
Iterate in simulation until the predicted response meets the target.

Apply corrections:
- Above target: cut (always safe)
- Below target AND fixable: boost (limited by safety)
- Below target AND NOT fixable: skip — EQ cannot help cancellation

Prefer cuts over boosts. Always include the mandatory infrasonic high-pass filter.

### 2.3 Re-measure and iterate

After applying EQ, re-measure and check convergence.
- RMS deviation < 2.0 dB: done
- Maximum 5 EQ iterations

On each subsequent iteration:
- Call `read_eq` to get the **currently applied** filter set
- Compute the residual deviation from the anchored target
- Merge the additional correction into the existing filters (adjust gains at the
  same frequency, add new bands for new frequencies)
- Call `apply_eq` with the **full merged set** — never with just the delta

## Convergence

- **Level match**: All subs within 3 dB before digital trim
- **Alignment**: Combined response reinforces vs individual subs
- **EQ**: RMS deviation from Harman target < 2.0 dB across 20-80Hz

## When convergence fails

If max iterations reached:
- Report final state and remaining deviations
- Deep nulls indicate room/placement cancellation — suggest sub repositioning
- Frequencies below the sub's capability cannot be boosted — that's expected
- Large level differences between subs suggest repositioning or knob adjustment

## Phase 3 — Retrospective

After calibration completes, analyze the run and present a roadmap for improvement.
Always run this phase, even if calibration converged.

### Before/after scorecard

Use `compare_sessions` between the baseline (first combined measurement) and final
measurement. Present RMS deviation, worst peak/null, alignment corrections applied,
PEQ slots used, and convergence status.

### Unfixable problems — room improvement recommendations

Review all analytics data from the run:

**Sub placement (from `compare_sub_phase` + `analyze_phase`):**
- Identify cancellation nulls and which sub contributes
- Recommend repositioning the sub with more nulls
- Corner placement increases coupling; midpoint placement creates the deepest modes

**Room treatment (from `analyze_decay`):**
- Modes with T60 > 500ms are rattle/mud candidates for bass traps
- Prioritize by audibility: higher SPL + longer T60 = most audible

**Rattle detection (from coherence):**
- Narrow coherence drops at specific frequencies → mechanical resonance
- Broad low coherence → ambient noise issue

**FIR opportunities:**
- Ringing modes where PEQ reduced the peak but T60 is still long → FIR candidate

### Next steps — prioritized action list

Numbered list ordered by expected impact, in plain language:
1. Physical changes (sub placement, bass traps, rattle fixes)
2. EQ improvements (FIR for ringing, slot optimization)
3. Re-run calibration after changes
