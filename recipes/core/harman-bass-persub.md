# Recipe: Per-Sub EQ + Harman Bass Target

## Goal

Calibrate multiple subwoofers using a three-phase approach:
1. **Time alignment** (Phase 1): measure each sub solo, compute delay and polarity corrections so all subs arrive at the mic in phase
2. **Output PEQ** (Phase 2, per-sub): flatten each sub's individual room response
3. **Input PEQ** (Phase 3, shared): apply the Harman bass target curve to all subs equally

This separation gives the combined Harman pass a much smoother starting point
and keeps per-sub corrections independent from the target curve.

## Filter Strategy

**This recipe uses PEQ only. FIR filters are not used.**

| Layer | Tool | Slots | Purpose |
|-------|------|-------|---------|
| Output PEQ (per sub) | `apply_eq` with `output_index` | 8 slots each | Room correction — flatten each sub independently |
| Input PEQ (shared) | `apply_input_eq` | 8 slots | Harman target curve + mandatory 18Hz HPF |
| FIR | — | 2048 taps/output (4096 shared @ 96kHz) | **Not used in this recipe** |

If you want FIR-based correction (e.g. linear phase room EQ), use a FIR-capable recipe.

## EQ Architecture

Call `get_config` to discover `eq_capabilities`. This tells you:
- Which input and outputs have PEQ slots available
- How many slots each has
- Which tool to call for each (`apply_input_eq` vs `apply_eq` with `output_index`)
- What filters are currently loaded

Use this to map the strategy below to the actual hardware:
- **Input PEQ** → shared Harman target curve + mandatory 18Hz HPF
- **Output PEQ** → per-sub room correction (one filter set per sub output)

## Pre-flight

Call `check_system` to verify all hardware is connected and reachable.
Call `get_config` to discover output slots, EQ capabilities, and mic configuration.
Mute any non-sub outputs (e.g. shakers) during calibration.

## Phase 0 — Level Setup

### 0.0 Clear all sub output EQ

Before any measurements, reset every sub output to a known zero state:
call `apply_eq` with **only the mandatory 18Hz HPF** on each sub output.
This ensures Phase 0 level comparisons, Phase 1 alignment, and Phase 2
room correction measurements are all taken from a clean baseline — not
through invisible prior EQ from a previous session.

`read_eq` only tracks in-memory state since server start. Always clear
explicitly rather than relying on it to tell you what's on the hardware.

### 0.1 Configure input routing

Call `configure_matrix` with the `active_input` from config. This routes the active
analog input to ALL four outputs and mutes the unused input. Without this step, the
miniDSP 2x4 HD default matrix splits inputs across outputs, and input PEQ in Phase 3
will only affect a subset of outputs.

### 0.2 Set initial volume

Set AVR to -10 dB as a known-good starting point for measurements.

### 0.3 Measure each sub solo

For each subwoofer output in the config:
1. Mute all other sub outputs
2. Take a measurement
3. Record the peak SPL from the frequency response
4. Unmute

### 0.4 Compare levels and compute trim

The loudest sub is the reference (trim = 0 dB).
For each quieter sub: trim = reference_spl - measured_spl.

### 0.5 Check for large level gaps

If any sub needs more than **10 dB** of digital trim:
- **STOP and tell the user.** Large digital trims waste headroom.
- Suggest turning up the volume knob on the quieter sub.
- Wait for user confirmation before continuing.

### 0.6 Apply level trims

Apply the computed gain trims to miniDSP output gains.
The loudest sub stays at 0 dB gain. Quieter subs get positive trim.

### 0.7 Calibrate sweep level

Call `calibrate_level` to find the optimal sweep volume with good SNR.

## Phase 1 — Sub Alignment

### 1.1 Measure each sub solo

For each subwoofer:
1. Mute all other subs
2. Take a measurement — note the session_id
3. Call `analyze_ir(session_id)` — get `peak_time_s`, `peak_sign`, `spl_db`
4. Unmute

### 1.2 Analyze phase relationship

Call `compare_sub_phase(session_a, session_b)` using the solo measurement session
IDs from 1.1. This shows per-band:
- Phase difference between the two subs
- Predicted coherent sum (constructive vs destructive)
- Classification: reinforcing / partial / cancelling

Use this to understand WHERE the subs reinforce vs cancel BEFORE applying
corrections. If subs already reinforce well, delay/polarity changes may not
be needed. If many bands cancel, the `analyze_ir` timing data guides the fix.

### 1.3 Apply corrections

Compare the per-sub `analyze_ir` results:
- **Delay**: The sub with the latest `peak_time_s` is the reference (delay = 0).
  Each other sub gets delay = (reference_peak_time_s − its_peak_time_s) × 1000 ms.
  Apply via `set_delay`.
- **Polarity**: Sub 0 is the polarity reference. Any sub with opposite `peak_sign`
  gets `set_polarity(inverted=True)`.

### 1.4 Verify alignment

Measure all subs together. Combined response should be louder than any
individual sub (reinforcement). If combined is quieter at some frequencies,
subs are still cancelling — revisit delay and polarity using the
`compare_sub_phase` analysis from 1.3 to guide adjustments.

Maximum 3 alignment iterations.

## Phase 2 — Per-Sub Room Correction (Output PEQ)

Each sub gets its own EQ to flatten its individual room response before
the shared Harman target is applied. Use `eq_capabilities.output_peq`
from `get_config` to find the available per-sub PEQ resources.

### 2.1 Measure each sub solo (post-alignment)

For each sub listed in `eq_capabilities.output_peq`:
1. Mute all other subs
2. Take a measurement
3. Compute a "flat" target: the average SPL across 25-80Hz for this sub
4. Unmute

### 2.2 Analyze fixability

For each sub's solo measurement, call `analyze_phase(session_id)` to get per-band
fixability data. This tells you which deviations are minimum-phase (correctable with
EQ) and which are excess-phase (cancellation — only repositioning helps).

- `fixable=True` bands: safe to design PEQ corrections
- `fixable=False` bands: skip — EQ will waste a slot without improving the null
- `fixable=None`: no phase data available — fall back to the heuristic below

Also check coherence in the measurement data (`get_measurement_history` with compact
format includes coherence summary). Low coherence (<0.8) means unreliable data at
that frequency — don't design precise corrections based on noisy measurements.

### 2.3 Analyze decay for ringing modes

Call `analyze_decay(session_id)` on each sub's solo measurement. If any modes
have T60 > 500ms, note the frequency and `suggested_q` — these ringing modes
need narrower Q values than a typical room peak. Use this data in the filter
design step below.

### 2.4 Design per-sub correction filters

For each sub, compare its solo FR to flat (its own average level):
- Peaks above the average: cut with peaking filter (always safe)
- Dips below the average: leave alone if narrow (likely a null — unfixable).
  Confirm with `analyze_phase` fixability before deciding.
- Broad dips: gentle boost if > 3 dB below average AND `fixable=True` (limited by safety)
- **Q selection:** Call `optimize_q(session_id, freq_hz, target_gain_db)` to find the
  best Q for each filter. For ringing modes from `analyze_decay` (step 2.3), prefer
  the `suggested_q` — it accounts for mode width and targets ringing surgically.
- **Q near adjacent features:** When a peak is adjacent to a dip (< 1/3 octave apart),
  use Q ≥ 4 to avoid the cut bleeding into the dip.

**Verify before applying:** Call `simulate_eq(session_id, filters)` with the proposed
filter set. Check the predicted FR — iterate on filter design in simulation until
satisfied, then apply once. This avoids unnecessary hardware writes and wasted iterations.

**Prefer cuts heavily.** The goal is to flatten each sub's response,
not to boost it to match a target. Nulls cannot be filled with EQ.

Always include the mandatory 18Hz HPF.

### 2.5 Apply per-sub EQ

For each sub, call `apply_eq` with `output_index` set to that sub's
output index. Each sub gets its own independent filter set.

### 2.6 Re-measure each sub solo and iterate

> **Iteration tip:** Before applying corrective filters, always run `simulate_eq`
> with the proposed merged filter set to predict the result. Only `apply_eq` when
> the simulation looks good.

After applying per-sub EQ:
1. Measure each sub solo again
2. Check if its response is flatter (lower variance across 25-80Hz)
3. If variance > 3 dB, design additional corrections:
   - Call `read_eq(output_index)` to get the **currently applied** filter set
   - Compute the residual deviation from the new measurement
   - Design only the additional filters needed to correct the residual
   - Merge the new filters into the existing set (add to existing gains at the
     same frequency, or add new bands for new frequencies)
   - Call `apply_eq` with the **full merged set** — existing corrections plus
     the new ones. Never call `apply_eq` with only the delta: `apply_eq`
     replaces all slots and a delta-only write discards all prior corrections.
4. Maximum 3 iterations per sub

### 2.7 Slot utilization guidance

Each sub output has 8 PEQ slots. Target usage:
- **Iteration 1:** 2-3 filters — HPF + largest peaks. Keep it simple.
- **Iteration 2-3:** Add 1-2 filters for remaining deviations if variance > 2 dB.
- **Final state:** 3-5 filters typical. Using all 8 is fine if each addresses a real feature.

Don't leave slots unused when measurable improvements remain. After each iteration,
check if residual peaks > 2 dB above average could benefit from an additional filter.
Use `analyze_decay` to identify ringing modes that benefit from narrow-Q treatment.

## Phase 3 — Harman Target (Input PEQ)

All subs are now individually flattened. Apply the shared Harman target
curve to the DSP input using `eq_capabilities.input_peq` from `get_config`.

### Harman bass target (relative to 80Hz)

| Hz  | Target |
|-----|--------|
| 25  | +5 dB  |
| 31  | +4 dB  |
| 40  | +3 dB  |
| 50  | +2 dB  |
| 63  | +1 dB  |
| 80  | 0 dB   |

### 3.1 Baseline

Measure the combined sub response (all subs unmuted, per-sub EQ active).
Use label "combined" and position "MLP" when calling `measure`.

### 3.2 Anchor the target curve

Compute the optimal reference level for the Harman target curve. The reference
determines where the target sits relative to the measured response.

**Strategy: max safe extension.** Find the highest reference level where no
frequency band requires more than +6 dB of boost (the safety limit). This gets
the most bass performance the subs can deliver within safety constraints.

Algorithm:
1. For each frequency in the baseline measurement (20-200 Hz band):
   - required_boost = harman_offset(freq) + ref - measured_spl(freq)
   - If required_boost > 0, that's how much boost this band needs
2. The constraint is: max(required_boost across all bands) <= 6 dB
3. So: ref <= min(measured_spl(freq) - harman_offset(freq)) + 6
   across all frequencies in the band
   Exclude from this calculation:
   - Frequencies where measured SPL is > 15 dB below the band average (cancellation nulls)
   - Frequencies below 28 Hz (below port tuning rolloff, unfixable)
   Nulls would otherwise drag the reference down, wasting headroom on frequencies
   that EQ cannot fix.
4. Use that as the reference SPL for the Harman target

This places the target as high as possible while keeping all boosts within the
+6 dB per-band safety limit. Most corrections will be cuts (peaks above target).
A few bands may need small boosts where the room is weakest.

Report the chosen reference level and the resulting max boost needed.

### 3.3 Apply Harman EQ to input

Design filters to match the Harman target (anchored in 3.2):
- Above target: cut (always safe)
- Below target: boost (limited by safety, max +6 dB per band)

Call `apply_input_eq` with the target curve filters. This writes to the
input PEQ so all subs receive the same correction.

Always include the mandatory 18Hz HPF.

### 3.4 Re-measure and iterate

After applying EQ, re-measure combined response (label "iter-N @ MLP"):
- Call `compute_deviation(session_id, target_curve)` to get RMS deviation with automatic null exclusion
- RMS deviation < 2.0 dB from the anchored target (excluding nulls): done
- The tool automatically excludes deep cancellation nulls (>15 dB below band average)
  and below-port rolloff from the RMS calculation
- Do NOT re-anchor the target between iterations (it was set in 3.2)
- Maximum 5 EQ iterations

On each subsequent iteration:
- Call `read_eq` (or `read_input_eq` if using input PEQ) to get the **currently applied** filter set
- Compute the residual deviation from the anchored target
- Design only the additional filters needed to correct the residual
- Merge with existing filters (add gains at the same frequency, add new bands for new frequencies)
- Call `apply_eq` / `apply_input_eq` with the **full merged set** — never with just the delta

## Convergence

- **Level match**: All subs within 3 dB before digital trim
- **Alignment**: Combined response reinforces vs individual subs
- **Per-sub EQ**: Each sub's solo FR variance < 3 dB across 25-80Hz
- **Harman target**: Combined RMS deviation < 2.0 dB (excluding null zones and below-port rolloff)

## When convergence fails

If max iterations reached:
- Report final state and remaining deviations per phase
- Deep nulls in per-sub solo measurements indicate placement problems
- Frequencies below the sub's capability cannot be boosted — expected
- If per-sub EQ can't flatten a sub, suggest repositioning that sub
- If the combined Harman pass can't converge, the per-sub corrections
  may need revisiting — check for cancellation between subs

## Phase 4 — Retrospective

After calibration completes (whether converged or not), analyze everything from
the run and give the user a roadmap for further improvement. This is where the
LLM synthesizes data that no automated tool can — physical room advice derived
from measurement analytics.

**Always run this phase**, even if calibration converged perfectly. There may be
room improvements that would make the NEXT calibration even better.

### 4.1 Gather run data

Collect the key data from all phases:
- Solo measurement session IDs from Phase 1 and Phase 2
- Combined measurement session IDs from Phase 3
- `analyze_phase` results from Phase 2 (fixability per band)
- `compare_sub_phase` results from Phase 1 (reinforcement/cancellation)
- `analyze_decay` results if run in Phase 2
- Final `compute_deviation` from Phase 3 (what converged, what didn't)
- `compare_sessions` between baseline (first combined) and final measurement

### 4.2 Before/after scorecard

Present a clear before → after comparison:

```
                    Before          After         Δ
RMS deviation:      5.8 dB    →    1.6 dB       -4.2 dB ✓
Worst peak:         +12 dB @45Hz → +3 dB @45Hz  -9 dB ✓
Worst null:         -14 dB @62Hz → -13 dB @62Hz -1 dB  (unfixable)
Sub alignment:      16.8ms apart → 0.0ms         aligned ✓
Polarity:           mismatched  → matched         ✓
PEQ slots used:     0/8 per sub → 4/8 per sub
Input PEQ slots:    0/8         → 5/8
```

### 4.3 Unfixable problems — room improvement recommendations

Review `analyze_phase` results across all measurements. For every band where
`fixable=False` or where EQ couldn't converge:

**Sub placement opportunities:**
- Identify which sub(s) contribute to cancellation nulls using `compare_sub_phase`
- Recommend specific repositioning: "Sub 2 cancels Sub 1 at 55Hz. Moving Sub 2
  to an adjacent wall would shift this null."
- Corner placement increases coupling — recommend for subs that are weak below 40Hz
- If one sub has significantly more nulls than the other, it's the better candidate to move

**Room treatment candidates:**
- Review `analyze_decay` for modes with T60 > 500ms — these ring audibly
- "50Hz mode rings for 800ms. A corner bass trap tuned to 50Hz would reduce this.
  Budget option: OC 703 panel (4" thick) in the nearest corner."
- Prioritize modes by audibility: higher SPL + longer T60 = more audible

**Rattle and resonance detection:**
- Check coherence data from measurements — sudden narrow coherence drops
  at specific frequencies can indicate mechanical resonances (cabinet rattles,
  HVAC ducts, window panes, shelving)
- "Low coherence at 73Hz — possible rattle or mechanical resonance. Check for
  loose objects, ductwork, or thin panels that vibrate at this frequency."
- If coherence is consistently low across a broad band, it may be high
  ambient noise rather than a rattle

### 4.4 EQ improvement opportunities

**FIR candidates:**
- Any mode where `analyze_decay` showed T60 > 500ms but PEQ only reduced the peak
  (not the ringing duration) is a candidate for FIR correction
- "The 50Hz mode still rings for 600ms after PEQ. A minimum-phase FIR filter
  (256 taps at 96kHz) could shorten the decay. Run a FIR-capable recipe next."

**Slot efficiency:**
- If all PEQ slots are used but deviation is still > 2dB, suggest:
  - Combining closely-spaced filters
  - Dropping the least-effective filter (smallest impact on RMS)
  - Switching to FIR for broadband correction

### 4.5 Next steps — prioritized action list

Present a numbered list, ordered by expected impact:

```
## What to do next (highest impact first)

1. 🔧 Move Sub 2 ~30cm away from the side wall
   WHY: 55Hz cancellation null (-14dB) between subs. Unfixable with EQ.
   IMPACT: Would eliminate the deepest null in the response.

2. 🧱 Add bass trap to front-left corner
   WHY: 50Hz mode rings for 800ms. PEQ reduced the peak but not the ringing.
   IMPACT: Shorter decay → cleaner bass, less mud.

3. 🔍 Check for rattle near 73Hz
   WHY: Low coherence (0.6) at 73Hz in all measurements. Suggests something
   is physically vibrating.
   IMPACT: Removing rattles improves measurement accuracy AND listening quality.

4. 🔄 Re-run calibration after changes
   WHY: Moving a sub changes the room transfer function. Current EQ is
   optimized for current placement.
```

Use plain language. The user may not be an acoustics expert — explain WHY each
recommendation matters and WHAT to do about it, not just what the data shows.

## MCP tools used

### Hardware I/O
- `check_system` — pre-flight hardware verification
- `measure` — take a sweep measurement
- `apply_eq` — write per-sub correction filters (with `output_index` for single-output targeting)
- `apply_input_eq` — write shared Harman target curve to DSP input
- `mute_output` / `unmute_output` — isolate subs for solo measurement
- `set_delay` / `set_polarity` / `set_output_gain` — sub alignment
- `set_volume` — set AVR volume for sweep playback
- `calibrate_level` — find optimal sweep volume
- `configure_matrix` — route active input to all outputs

### Analytics (data for LLM judgment)
- `analyze_phase` — per-band fixability: minimum-phase (EQ-correctable) vs excess-phase (reposition sub)
- `compare_sub_phase` — per-frequency phase relationship between solo sub measurements
- `analyze_ir` — IR peak time/sign/SPL for computing delay and polarity corrections
- `analyze_decay` — T60 ringing analysis for EQ Q selection
- `compute_deviation` — RMS deviation with automatic null/rolloff exclusion

### Simulation (verify before applying)
- `simulate_eq` — predict FR after proposed PEQ filters (iterate in simulation, apply once)
- `optimize_q` — find best Q for a filter at a chosen frequency and gain

### State and config
- `get_config` — discover `eq_capabilities` (input/output PEQ slots, labels, current state)
- `get_output_state` — verify current per-output state mid-calibration
- `get_measurement_history` — FR data with coherence for filter design
- `compare_sessions` — per-band delta between two sessions (verify EQ changes)
- `read_eq` / `read_input_eq` — current PEQ state (for iterative filter merging)
