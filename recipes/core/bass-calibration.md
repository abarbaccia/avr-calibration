# Recipe: Bass Calibration
version: 2.0

## Goal

Calibrate one or more subwoofers to a user-chosen target curve across a configurable
frequency range. For multi-sub setups: align subs in time and correct each sub's room
response independently. For all setups: reduce ringing with FIR (if available), then
shape the combined response to the target curve.

Adapts automatically to the hardware:
- **1 sub**: Skips alignment (Phase 1), simplifies level matching
- **2+ subs**: Full alignment, per-sub room correction, combined verification
- **No FIR**: Skips Phase 3, proceeds directly to target curve PEQ
- **FIR available**: Applies ringing reduction before target curve for a stable foundation

## Measurement Signal Path

Show the active signal path for this recipe based on `get_config`. The path depends
on `config.measurement.playback_route`:

- **`"usb"`**: Pi → USB audio → miniDSP → sub outputs → room → mic → Pi.
  The AVR is not in the measurement loop.
- **`"hdmi"`**: Pi → HDMI → AVR → miniDSP (via LFE) → sub outputs → room → mic → Pi.
  The AVR and its processing are in the loop.

Build the diagram dynamically — show only the boxes and connections that are active
for the configured playback route. Populate labels from config (sub names, mic model,
AVR model, DSP output slots). Mark shaker outputs as muted, unused as skipped.

## Configuration

Before calibration begins, walk the user through these choices. Suggest defaults
based on the hardware config. Most users accept defaults; technical users can adjust.

### Pick a target curve

List all `.json` files in `recipes/curves/`. For each, show the `name` and
`description` from the file. Mark the one with `"default": true`.

```
Available target curves:
  1. Harman Bass Target (recommended) — gentle bass rise backed by listener research
  2. Flat — ruler-flat, best for treated rooms
  3. House Curve (+3 dB) — gentle shelf, popular audiophile compromise
  4. Custom — provide your own frequency/offset pairs
```

If the user provides custom points, validate:
- At least 3 frequency/offset pairs
- Frequencies monotonically increasing
- A reference frequency specified (default: highest frequency in the curve)

Load the chosen curve from `recipes/curves/{name}.json`. The `points` array has
`freq_hz` and `offset_db` (relative to `reference_freq_hz`). The absolute target at
each frequency = `reference_spl + offset_db` (where `reference_spl` is computed
during anchoring in Phase 4).

### Pick the bass range

The frequency range determines what this recipe calibrates. Outside this range,
other speakers (mains, surrounds) are responsible.

Suggest a default based on the user's setup:
- **Lower bound**: port tuning frequency + 3 Hz. For SVS PB12-NSD (port ~22 Hz),
  default is **25 Hz**. Sealed subs can go lower (20 Hz).
- **Upper bound**: LCR crossover frequency from the AVR.
  - Ask: "What is your LCR crossover set to?"
  - 60 Hz → large mains handling most midbass
  - **80 Hz** → THX standard, most common (default if unknown)
  - 100–120 Hz → small bookshelf speakers, subs handling more of the spectrum

If the crossover is above the target curve's highest defined point, extend the
curve flat from its reference frequency to the crossover. For example, if using
Harman (reference 80 Hz) with a 120 Hz crossover, the target is 0 dB offset
from 80–120 Hz.

Report: `"Calibrating 25–80 Hz"` (or whatever the user chose).

### Set convergence goals

| Parameter | Default | Range | What it means |
|-----------|---------|-------|---------------|
| RMS threshold | **1.5 dB** | 0.5–3.0 | Stop when RMS deviation from target is below this |
| Max iterations/phase | **5** | 3–10 | Hard stop per calibration phase |

Explain the tradeoffs:
- **1.5 dB** (default): good accuracy, achievable in most rooms with 2+ subs
- **1.0 dB**: ambitious — needs many PEQ slots, good room acoustics, may not converge
- **2.0 dB**: relaxed — fewer iterations, acceptable for casual listening
- Below 1.0 dB: diminishing returns; measurement noise floor limits accuracy

### Safety limits (informational)

These are enforced in `SafetyValidator` code — not recipe parameters. Show them
so the user knows the constraints:

| Limit | Value | Why |
|-------|-------|-----|
| Max boost per band | +6 dB | Protects ported subs from unloading below port tuning |
| Max cumulative boost (1/3 oct) | +9 dB | Prevents thermal overload from stacked boosts |
| Max change per iteration | +3 dB | Prevents runaway correction loops |
| Mandatory HPF | 18 Hz, 4th-order | Protects driver from infrasonic excursion |
| Cuts | Unlimited | Cuts are always safe — no restriction |

Note to user: "If SafetyValidator blocks a filter, the recipe reduces the gain
and retries. The limits are conservative — if you find them too restrictive after
running the recipe, the thresholds can be adjusted in the system config."

**Recommended code changes** (see end of recipe):
- Increase max change per iteration to +6 dB when preceded by `simulate_eq`
  verification — the current +3 dB limit forces unnecessary measurement cycles
  after structural changes like FIR application.
- Make max boost frequency-dependent: +6 dB below 30 Hz (port protection),
  +8 dB above 30 Hz (thermal limit only).

## Filter Strategy

This recipe uses up to three filter layers, depending on hardware:

| Layer | Tool | Slots | Purpose | Required? |
|-------|------|-------|---------|-----------|
| Output PEQ (per sub) | `apply_eq` with `output_index` | 8 per output | Room mode cuts, per-sub flattening | Always |
| FIR (per sub) | `apply_fir` with `output_index` | taps from config | Ringing reduction (time domain) | If `fir_capable` |
| Input PEQ (shared) | `apply_input_eq` | 8 shared | Target curve shaping | Always |

If `fir_capable` is false, Phase 3 is skipped and the recipe proceeds from
per-sub PEQ directly to target curve PEQ.

**Phase ordering matters.** The DSP signal chain is:

```
Input → Input PEQ → Routing → Output PEQ → FIR → DAC
```

But the RECIPE applies corrections in this order:

```
Output PEQ (flatten each sub) → FIR (fix ringing) → Input PEQ (shape to target)
```

This ensures Input PEQ is designed against the FIR-corrected response. If FIR
were applied after Input PEQ, the magnitude changes would invalidate the target
curve work and require full re-iteration.

## Pre-flight

Call `check_system` to verify all hardware is connected and reachable.
Call `get_config` to discover output slots, EQ capabilities, and mic configuration.
Mute any non-sub outputs (e.g. shakers) during calibration.

## Phase 0 — Setup

### 0.0 Reset ALL DSP state

Before any measurements, reset the entire DSP to a known zero state.
The miniDSP is write-only — there is no way to read current hardware state. Prior
sessions leave settings in flash, and `get_output_state` only tracks what this MCP
server has written since it started. Always write explicitly.

For **every output** (0-3, including unused/shaker):
1. `set_delay(output_index, 0)` — clear any leftover alignment delays
2. `set_polarity(output_index, inverted=false)` — clear polarity flips
3. `set_output_gain(output_index, 0)` — clear level trims

For **each sub output** in the config:
4. `apply_eq(output_index, [HPF only])` — bypasses all PEQ slots

For **inputs**:
5. `apply_input_eq([HPF only])` — clears input PEQ on both inputs
6. `set_master_gain(0)` — reset master gain

For **FIR** (if previously used):
7. `clear_fir(output_index)` for each output

### 0.1 Configure input routing

Call `configure_matrix` with the `active_input` from config. This routes the active
analog input to ALL four outputs and mutes the unused input. Without this, the
miniDSP 2x4 HD default matrix splits inputs across outputs.

### 0.2 Set initial volume

Set AVR to -10 dB as a known-good starting point.
(If USB mode: use `set_master_gain` instead — AVR is not in the signal chain.)

### 0.3–0.6 Level matching

**Single sub:** Skip to 0.7 — no level matching needed.

**Multiple subs:**

0.3 Measure each sub solo:
  For each subwoofer output in the config:
  1. Mute all other sub outputs
  2. Take a measurement
  3. Record the peak SPL from the frequency response
  4. Unmute

0.4 Compare levels: The loudest sub is the reference (trim = 0 dB).
  For each quieter sub: trim = reference_spl - measured_spl.

0.5 Check for large gaps: If any sub needs more than **10 dB** of digital trim,
  **STOP and tell the user.** Suggest turning up the volume knob on the quieter sub.
  Wait for user confirmation.

0.6 Apply trims via `set_output_gain`. Loudest sub stays at 0 dB.

### 0.7 Calibrate sweep level

Call `calibrate_level` to find the optimal sweep volume with good SNR.

## Phase 1 — Alignment

**Single sub:** Skip this entire phase — proceed to Phase 2.

### 1.1 Measure each sub solo

For each subwoofer:
1. Mute all other subs
2. Take a measurement — note the session_id
3. Call `analyze_ir(session_id)` — get `peak_time_s`, `peak_sign`, `spl_db`
4. Unmute

### 1.2 Analyze phase relationship

Call `compare_sub_phase(session_a, session_b)` using the solo session IDs.
This shows per-band phase difference, predicted coherent sum, and whether subs
reinforce or cancel. Understand the interaction before correcting.

### 1.3 Apply delay correction

The sub with the latest `peak_time_s` is the reference (delay = 0).
Each earlier-arriving sub gets:
  delay_ms = (reference_peak_time_s - its_peak_time_s) * 1000
Apply via `set_delay`.

### 1.4 Polarity test (measurement-based)

Do NOT rely on `peak_sign` alone — room reflections can mislead.
1. Unmute all subs. Measure combined (label "combined-pol-normal").
2. Flip polarity on non-reference sub(s) via `set_polarity(inverted=True)`.
3. Measure combined again (label "combined-pol-flipped").
4. Compare via `compare_sessions`. Keep whichever polarity produces higher
   combined SPL. If difference < 1 dB, keep normal (simpler).
5. Restore the losing setting.

### 1.5 Verify alignment

Measure all subs together. Combined should be louder than any individual sub
(reinforcement). If combined is quieter at some frequencies, subs still cancel —
revisit using `compare_sub_phase` to guide adjustments.

Maximum 3 alignment iterations.

## Phase 2 — Room Correction (Output PEQ)

Each sub gets its own EQ to flatten its individual room response.
**Single sub:** Same workflow, just one sub — skip combined verification (2.6).

### 2.1 Measure each sub solo (post-alignment)

For each sub:
1. Mute all other subs
2. Take a measurement — note the session_id
3. Unmute

### 2.2 Full-resolution analysis

For each sub's solo measurement, pull full-resolution FR data:
`get_measurement_history(format="compact", min_hz={low_hz-5}, max_hz={high_hz+40})`

Parse the compact string (split on `,` then `:`) to get (freq_hz, spl_db) pairs
at ~0.18 Hz resolution. Then:

1. Compute the average SPL across the configured frequency range
2. Find all **peaks** > 3 dB above average:
   - Record the **exact** frequency (to 0.5 Hz), amplitude, and approximate width
   - Width = the frequency span where SPL stays within 3 dB of the peak
3. Find all **dips** > 3 dB below average — note for fixability check
4. Rank features by amplitude x width (broad peaks = highest priority)

**This is the key improvement over 1/3-octave analysis.** A 5 dB peak at 47.3 Hz
that sits between the 40 Hz and 50 Hz band centers would be invisible to 1/3-octave
reporting but is clearly visible at full resolution.

### 2.3 Analyze fixability and decay

For each sub's solo measurement:
- Call `analyze_phase(session_id)` — which deviations are EQ-fixable?
- Call `analyze_decay(session_id)` — which modes ring? Note T60 and `suggested_q`
- Check coherence: low coherence (<0.8) = unreliable data, don't design filters there

### 2.4 Design per-sub correction filters

For each sub, design PEQ filters targeting the features found in 2.2:

- **Peaks above average** (`fixable=True`): Cut with peaking filter at the **exact
  peak frequency** from full-res analysis (not a rounded 1/3-octave center).
- **Ringing modes** (T60 > 500ms from 2.3): Use the `suggested_q` from `analyze_decay`
  for a narrower, more surgical cut. Call `optimize_q` to refine.
- **Dips below average**: Leave narrow dips alone (likely cancellation nulls).
  Confirm with `analyze_phase` before considering any boost.
- **Adjacent features**: When a peak is within 1/3 octave of a dip, use Q >= 4
  to prevent the cut from bleeding into the dip.

For each proposed filter, call `optimize_q(session_id, freq_hz, target_gain_db)`
to find the best Q.

**Simulate before applying:** Call `simulate_eq(session_id, filters)` to predict
the corrected FR. Iterate in simulation until satisfied. This is free.

**Prefer cuts heavily.** Goal is flattening, not boosting. Nulls can't be filled.

Always include the mandatory 18 Hz HPF.

### 2.5 Apply and verify per-sub

For each sub:
1. Call `apply_eq(output_index, filters)` with the simulated filter set
2. Mute other subs, measure solo
3. Check if response is flatter (variance < 3 dB across the frequency range)
4. If not, iterate: read current EQ, design additional corrections, merge,
   simulate, apply. Maximum 5 iterations per sub.

**Keep iterating while PEQ slots remain and peaks exceed ~1.5 dB above local average.**
Check `config.eq_capabilities.peq_slots_per_output` and compare to the number of
filters applied so far. If slots are free and there are still peaks > 1.5 dB above
the band average:
- Re-measure each sub solo (post-PEQ) to see residual peaks
- Design additional narrow cuts at those peaks (use `optimize_q` with narrow band
  constraints to avoid bleed into adjacent dips)
- Simulate the delta filters only (not the full chain) against the post-PEQ session,
  since the existing filters are already baked into that measurement
- Apply the full chain (existing + new) with `simulation_verified=true`

Stop when any of these is true:
- Remaining peaks are all within 1.5 dB of local average (diminishing returns)
- PEQ slots are exhausted
- Additional filters are creating notches by overlapping with adjacent cuts
- You've hit 5 iterations

**Rationale:** most miniDSP-class hardware has 10+ PEQ slots per output. Stopping
at 2-3 filters when 7 more are available leaves flatness on the table. But every
narrow cut also narrows the passband between adjacent dips — watch for that.

### 2.6 Combined verification

After all subs have per-sub PEQ applied:
1. Unmute all subs
2. Measure combined response (label "combined-persub-peq")
3. Compare to the pre-PEQ combined measurement from Phase 1.5
4. Check: did any per-sub cut create an unwanted interaction in the combined
   response? (A cut on one sub can unmask a peak from the other sub.)
5. If combined response has new peaks > 3 dB that weren't in the pre-PEQ
   combined, note them for Phase 3/4 — don't re-do per-sub PEQ.

## Phase 3 — FIR Ringing Reduction

FIR addresses time-domain problems that PEQ cannot: long modal ringing (high T60)
where PEQ reduces the peak amplitude but the mode still decays slowly.

### 3.1 Check FIR availability

Call `get_config` and check `eq_capabilities`:
- If `fir_capable` is **false**: **skip this entire phase** — proceed to Phase 4.
  Report: "FIR not available on this hardware. Skipping ringing reduction."
- If `fir_capable` is true: note `fir_max_taps_per_output` and `fir_sample_rate_hz`

### 3.2 Identify FIR candidates

Review `analyze_decay` results from Phase 2.3. FIR is worthwhile if:
- Any mode has **T60 > 500 ms** AND
- The mode's frequency falls within the configured bass range

If no modes qualify, **skip this phase entirely** — proceed to Phase 4.
Report: "No significant ringing detected (all modes T60 < 500ms). Skipping FIR."

### 3.3 Design FIR per sub

For each sub with ringing modes:
1. Use the Phase 2 solo measurement (with output PEQ already active)
2. Call `design_fir(session_id, output_index, ...)` with:
   - `mode="minimum_phase"` (preserves transient alignment from Phase 1)
   - `freq_focus_hz=[low_hz, high_hz+10]` matching the configured bass range
3. Note the predicted magnitude change at each frequency

### 3.4 Apply FIR

For each sub, call `apply_fir(output_index, ...)` with the designed coefficients.

### 3.5 Verify decay improvement

For each sub:
1. Mute other subs
2. Measure solo (label "sub{N}-post-fir")
3. Call `analyze_decay` on the new measurement
4. Compare T60 at each targeted mode vs the Phase 2 measurement:

```
Mode    Pre-FIR T60    Post-FIR T60    Change
47 Hz   1270 ms        800 ms          -37%
70 Hz   1263 ms        900 ms          -29%
```

If a targeted mode's T60 didn't decrease by at least **15%**, the FIR didn't help
at that frequency. Consider removing FIR and relying on PEQ alone.

5. Unmute

### 3.6 Adjust output PEQ for FIR magnitude changes

FIR changes magnitude, not just decay. Compare each sub's post-FIR solo FR
to its pre-FIR solo FR (Phase 2 measurement):

1. Call `compare_sessions(pre_fir_session, post_fir_session)`
2. For each band where FIR changed magnitude by > 2 dB:
   - **Audit existing output PEQ**: Is the filter at this frequency still needed?
     The FIR may have already addressed what the PEQ was targeting.
   - Remove PEQ filters that are now redundant (their target problem is gone)
   - Adjust gains on filters where FIR partially addressed the problem
   - Add new filters if FIR created new deviations
3. Simulate the adjusted PEQ against the post-FIR measurement before applying

### 3.7 Clean combined baseline

This measurement becomes the **reference for all Phase 4 work**:

1. Unmute all subs (output PEQ + FIR active on all subs)
2. Clear input PEQ to HPF-only: `apply_input_eq([HPF only])`
3. Measure combined response (label "combined-pre-target")
4. **Save this session_id** — ALL Phase 4 simulations use this session

This ensures `simulate_eq` can accurately predict input PEQ effects on the
FIR-corrected combined response. Without this baseline, simulation would
reference a stale pre-FIR state and produce inaccurate predictions.

## Phase 4 — Target Curve (Input PEQ)

Apply the user's chosen target curve to the combined, FIR-corrected response
using the shared input PEQ.

### 4.1 Anchor the target curve

Compute the optimal reference level against the **Phase 3 baseline** (session
from step 3.7, or Phase 2 combined if FIR was skipped).

Algorithm:
1. Pull full-res FR for the baseline measurement
2. For each frequency in the configured range:
   - Interpolate the target curve offset at this frequency
   - required_boost = offset + ref - measured_spl
3. Constraint: max(required_boost) <= 6 dB (safety limit)
4. So: ref = min(measured_spl(f) - offset(f)) + 6 across all f in the range
5. Exclude from this calculation:
   - Frequencies where measured SPL > 15 dB below the band average (nulls)
   - Frequencies below port_tuning_hz + 3 Hz (rolloff, unfixable)

Report the chosen reference level and the resulting max boost needed.

### 4.2 Full-resolution filter design

Pull the full-res FR for the baseline measurement. At each measurement point,
compute: `error = (reference_spl + offset_at_this_freq) - measured_spl`.

Analyze the error curve:
1. Find the largest errors — these drive the first filters
2. Group nearby errors (within 1/3 octave) — one filter can address a group
3. For each filter: use `optimize_q` to find the best Q for the error shape
4. Simulate the full filter set against the baseline: `simulate_eq(baseline_session, filters)`
5. Check the simulated error at full resolution — iterate until satisfied

### 4.3 Apply and iterate (with filter audit)

Each iteration follows this workflow:

**Step A — Filter audit.** Before designing new corrections, evaluate every
existing input PEQ filter:
1. For each filter: simulate the set with this filter removed
2. Compare RMS-with vs RMS-without. If the difference < 0.3 dB, the filter
   is not pulling its weight — remove it and free the slot
3. For remaining filters: has the measured response shifted at this filter's
   frequency since it was designed? If so, re-optimize the gain and Q against
   the current measurement using `optimize_q`
4. This prevents stale filters from accumulating across iterations

**Step B — Design new corrections.** For remaining deviations:
1. Pull full-res FR from the latest measurement
2. Compute error against the anchored target (do NOT re-anchor)
3. Find the largest remaining errors not addressed by existing filters
4. Design new filters at exact error frequencies
5. Merge new filters with audited existing set

**Step C — Simulate the full merged set** against the Phase 3/4 baseline.
Verify the predicted response meets the target. Adjust if needed.

**Step D — Apply and measure.**
1. Call `apply_input_eq` with the full merged filter set (always include 18 Hz HPF)
2. Measure combined response
3. Call `compute_deviation(session_id, target_curve)` to check convergence

Stop when RMS < configured threshold or max iterations reached.

### 4.4 Between-band peak hunting

After the main iteration loop converges (or stalls), check for narrow peaks
hiding between the standard reporting frequencies:

1. Pull full-res FR from the latest measurement
2. Interpolate the target at every measurement point
3. Find the single worst peak (highest positive error) not already addressed
4. If the peak is > 1.5 dB above target and an input PEQ slot is available:
   - Design a narrow cut (Q=4-8)
   - Simulate first — narrow filters close together interact
   - Apply and re-measure
5. Repeat until no peak > 1.5 dB above target or no slots remain

## Phase 5 — Retrospective

Always run this phase, even if calibration converged perfectly.

### 5.1 Before/after scorecard

Use `compare_sessions` between the earliest combined measurement (Phase 0 or 1)
and the final measurement. Also use `compute_deviation` on both.

```
                      Before          After           Delta
RMS deviation:        X.X dB    ->    X.X dB         -X.X dB
Worst peak:           +X dB @XXHz ->  +X dB @XXHz    -X dB
Worst null:           -X dB @XXHz ->  -X dB @XXHz    (unfixable)
Sub alignment:        X.Xms apart ->  0.0ms          aligned
PEQ slots/sub:        0/8        ->   X/8
Input PEQ slots:      0/8        ->   X/8
FIR taps/sub:         0          ->   XXXX
Convergence:          N/A        ->   YES/NO (X.X dB RMS)
```

### 5.2 Unfixable problems — room improvement recommendations

Review `analyze_phase` results. For every band where `fixable=False` or where
EQ couldn't converge:

**Sub placement:**
- Identify nulls that EQ couldn't address — these are cancellation from room modes
- Corner placement increases coupling below 40 Hz
- Moving a sub away from a wall midpoint reduces the deepest standing wave
- Use `compare_sub_phase` to identify which sub contributes more to each null
- Recommend 2-3 candidate positions and re-measuring

**Room treatment:**
- Review `analyze_decay` for modes with T60 > 500ms (even after FIR)
- Recommend bass traps prioritized by audibility (SPL x T60)
- Corner placement for membrane traps, wall placement for porous absorbers

**Rattle detection:**
- Narrow coherence drops at specific frequencies = mechanical resonance
- Broad low coherence = ambient noise
- Recommend checking loose objects, ductwork, thin panels

### 5.3 Next steps — prioritized action list

Numbered list ordered by expected impact, in plain language:
1. Physical changes (sub placement, room treatment, rattle fixes)
2. EQ improvements (slot optimization, different curve, tighter convergence)
3. Re-run calibration after changes

## Convergence

| Criterion | Threshold | Tool |
|-----------|-----------|------|
| Level match | All subs within 3 dB before digital trim | Phase 0 solo measurements |
| Alignment | Combined SPL > any solo SPL (reinforcement) | `compare_sessions` |
| Per-sub flatness | Solo FR variance < 3 dB across range | Phase 2 measurement |
| Target curve | RMS deviation < configured threshold (default 1.5 dB) | `compute_deviation` |

`compute_deviation` automatically excludes:
- Null zones (> 15 dB below band average)
- Below-port rolloff (< port_tuning_hz + 3 Hz)

## When convergence fails

If max iterations reached:
- Report final state and remaining deviations per phase
- Identify the top 3 frequency bands preventing convergence
- For each: is it fixable (min-phase) or not (excess-phase)?
- Fixable but stuck: may need more PEQ slots, wider Q, or different filter topology
- Not fixable: recommend sub repositioning in the retrospective
- If FIR was skipped and ringing is high: suggest running Phase 3

## Recommended code changes

These changes would improve the recipe's effectiveness. They are not required
to run the recipe, but the recipe notes where it hits current limitations.

### compute_deviation: higher resolution

Current: returns ~6 bands (1/3-octave centers) in 25-80 Hz.
Proposed: configurable resolution via a `resolution` parameter:
- `"third_octave"` (current default): ~6 bands
- `"sixth_octave"`: ~12 bands — good balance of detail and readability
- `"twelfth_octave"`: ~24 bands — full detail for filter design
Default to `"sixth_octave"` (~20 points across 20-120 Hz).

This gives the LLM enough resolution to see narrow peaks between the current
reporting frequencies. A 4 dB peak at 54 Hz is invisible in 1/3-octave
(sits between 50 and 63) but obvious in 1/6-octave.

### SafetyValidator: simulation-verified iteration step

Current: max +3 dB change per band per iteration (always).
Proposed: +6 dB per iteration when the filter set has been verified by
`simulate_eq` immediately before application.

The +3 dB limit exists to prevent runaway correction loops. When the LLM
simulates the filter set and confirms the predicted response is reasonable,
the risk of runaway is already mitigated. +6 dB halves the number of
measurement cycles needed after structural changes (like FIR application).

### SafetyValidator: frequency-dependent boost limit

Current: +6 dB max boost at all frequencies.
Proposed:
- Below 30 Hz: +6 dB (port protection for ported subs)
- 30-120 Hz: +8 dB (thermal limit only — SHARC DSP has no saturation concern)

The +6 dB limit below port tuning protects against unloading the port. Above
port tuning, the driver is mechanically loaded and the risk is thermal only.
+8 dB is still conservative for thermal protection.

## MCP tools used

### Hardware I/O
- `check_system` — pre-flight hardware verification
- `measure` — take a sweep measurement
- `apply_eq` — write per-sub PEQ filters (with `output_index`)
- `apply_input_eq` — write shared target curve to DSP input
- `apply_fir` — write FIR coefficients to a DSP output
- `clear_fir` — clear FIR and reset to passthrough
- `mute_output` / `unmute_output` — isolate subs for solo measurement
- `set_delay` / `set_polarity` / `set_output_gain` — sub alignment
- `set_volume` — set AVR volume for sweep playback
- `set_master_gain` — set miniDSP master gain (USB mode volume)
- `calibrate_level` — find optimal sweep volume
- `configure_matrix` — route active input to all outputs

### Analytics (data for LLM judgment)
- `analyze_phase` — per-band fixability: min-phase (correctable) vs excess-phase (reposition)
- `compare_sub_phase` — phase relationship between solo sub measurements
- `analyze_ir` — IR peak time, polarity sign, SPL for alignment
- `analyze_decay` — T60 ringing analysis, suggested_q per mode
- `compute_deviation` — RMS deviation from target with null/rolloff exclusion

### Simulation (verify before applying)
- `simulate_eq` — predict FR after proposed PEQ filters
- `optimize_q` — find best Q for a filter at a given frequency and gain
- `design_fir` — compute FIR coefficients (minimum/linear/mixed phase)

### State and config
- `get_config` — discover output slots, EQ capabilities, mic config
- `get_output_state` — per-output gain, delay, polarity, FIR taps
- `get_measurement_history` — FR data (use `format="compact"` for bass)
- `compare_sessions` — per-band delta between two measurements

> **PEQ is write-only.** The miniDSP cannot be queried for its current filter set.
> Track the filters YOU applied in this conversation — that's the source of truth.
> When iterating, carry the full merged filter set in context and pass it to
> `apply_eq` each time (never call with just a delta).
