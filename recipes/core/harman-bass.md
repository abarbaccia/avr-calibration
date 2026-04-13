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

## Pre-flight

Call `check_system` to verify all hardware is connected and reachable.
Call `get_config` to discover output slots, EQ capabilities, and mic configuration.

## Step 0 — Reset ALL DSP state

Before taking the baseline measurement, reset the entire DSP to a known zero state.
`read_eq` and `get_output_state` only track in-memory changes since the MCP server
started — hardware flash retains settings from prior sessions. Always write explicitly.

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

This ensures the baseline measurement reflects the true room response,
not the room plus prior EQ, delays, or gain trims.

## Step 1 — Set volume and calibrate level

Call `set_volume(-10)` as a known-good starting point.
Call `calibrate_level` to find the optimal sweep volume with good SNR.

## Step 2 — Baseline measurement

Call `measure` to take a fresh sweep measurement. Note the session_id — this
is the baseline that all corrections will be designed against.

## Step 3 — Anchor the target curve

Compute the optimal reference level for the Harman target. Find the highest
reference SPL where no frequency band requires more than +6 dB of boost:

  ref = min(measured(f) - harman_offset(f) + 6) across all frequencies in 25-80Hz

Exclude from this calculation:
- Frequencies where measured SPL is > 15 dB below the band average (cancellation nulls)
- Frequencies below 28 Hz (below port tuning rolloff, unfixable)

This maximizes bass extension while staying within the +6 dB safety limit.
Report the chosen reference level and the resulting max boost needed.

### Harman bass target (relative to 80Hz)

| Hz  | Target |
|-----|--------|
| 25  | +5 dB  |
| 31  | +4 dB  |
| 40  | +3 dB  |
| 50  | +2 dB  |
| 63  | +1 dB  |
| 80  | 0 dB   |

## Step 4 — Analyze fixability

Call `analyze_phase(session_id)` on the baseline measurement to determine which
deviations from target are fixable with EQ:
- `fixable=True`: minimum-phase error — PEQ can correct it
- `fixable=False`: excess-phase (cancellation) — repositioning the sub is more effective
- Check coherence in measurement data — low coherence (<0.8) means unreliable data

This avoids wasting PEQ slots on unfixable problems.

## Step 5 — Analyze the current response

Compare the measured SPL at each 1/3-octave band (25–80Hz) against the Harman
bass target anchored at the reference from Step 3.

## Step 6 — Design corrections

For each band deviating from target, calculate:
  correction_db = target_db - measured_db

Only design corrections for bands where `analyze_phase` reported `fixable=True`.
Skip unfixable bands — they are cancellation nulls that EQ cannot help.

Call `analyze_decay(session_id)` to identify ringing modes. For modes with
T60 > 500ms, use the `suggested_q` for a narrower, more surgical correction.

For each filter, call `optimize_q(session_id, freq_hz, target_gain_db)` to find
the best Q value.

Apply corrections as peaking EQ bands. Prefer cuts over boosts where possible
— cuts are always safe; boosts are limited by SafetyValidator.

## Step 7 — Verify in simulation

Call `simulate_eq(session_id, filters)` with the proposed filter set. Check the
predicted FR against the Harman target. If the prediction shows remaining issues,
adjust filters and re-simulate. Iterate in simulation until satisfied — this is
free (no hardware writes, no new measurements needed).

## Step 8 — Apply corrections

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

## Step 9 — Re-measure and iterate

After applying filters, call `measure` to take a new measurement.
Call `compute_deviation(session_id, target_curve)` to get RMS deviation with
automatic null zone and below-port rolloff exclusion.

Do NOT re-anchor the target between iterations (it was set in Step 3).

On each subsequent iteration:
- Call `read_eq` to get the **currently applied** filter set
- Compute the residual deviation from the anchored target based on the new measurement
- Merge the additional correction into the existing filters (adjust gains at the
  same frequency, add new bands for new frequencies)
- Call `apply_eq` with the **full merged set** — never with just the delta.
  `apply_eq` replaces all PEQ slots: a delta-only write discards all prior corrections.

Repeat until convergence or the maximum iteration count is reached.

## Convergence

Stop when: RMS deviation from Harman target < 2.0 dB across 25–80Hz
(computed via `compute_deviation` with automatic null exclusion)

Maximum iterations: 5

## Phase 2 — Performance Optimization (Optional)

After convergence, inventory remaining DSP resources and suggest ways to push
RMS lower. Present options and let the user choose.

### 2.1 Inventory available resources

Check what's still unused:
- **Output PEQ slots**: `read_eq` — how many of the 8 slots are free?
- **FIR taps**: `get_config` → `eq_capabilities.fir_capable`, `fir_max_taps_per_output`.
  FIR can reduce ringing that PEQ can only attenuate.
- **Between-band peaks**: Pull `get_measurement_history(format="compact", min_hz=20, max_hz=120)`
  and scan for narrow peaks between the 1/3-octave centers that `compute_deviation`
  summary doesn't report.

Report the inventory to the user with current RMS and remaining resources.

### 2.2 Optimization options

Present available optimizations with expected impact:

**FIR for ringing:** If `analyze_decay` showed modes with T60 > 500ms, FIR can
shorten the decay where PEQ only reduced the peak amplitude. Call `design_fir`
with minimum-phase mode using the baseline solo measurement. Apply to the sub's
output, re-measure, and adjust PEQ to compensate for FIR magnitude changes.

**Between-band peak hunting:** If max_error > 2 dB but 1/3-octave bands look good,
narrow peaks exist between reporting frequencies. Pull full-res FR, find the worst
peak, design a narrow cut (Q=4-8), simulate, and apply. Use narrow Q to avoid
disrupting adjacent well-tuned bands.

**Slot optimization:** If all slots are used but some filters have < 0.5 dB impact,
consider dropping the least effective filter to free a slot for a higher-impact
correction elsewhere.

Let the user choose which to pursue, or skip to the retrospective.

## Phase 3 — Retrospective

After calibration completes, analyze the run and give the user a roadmap for
further improvement. Always run this, even if EQ converged.

### Before/after scorecard

Use `compare_sessions` between the baseline (Step 2) and final measurement. Present:
- RMS deviation before → after
- Worst peak and null before → after
- PEQ slots used
- Whether convergence was reached

### Unfixable problems — room improvement recommendations

Review `analyze_phase` results. For every band where `fixable=False`:

**Sub placement:**
- Identify nulls that EQ couldn't address — these are room mode cancellations
- Corner placement increases bass coupling below 40Hz
- Moving the sub away from a wall midpoint reduces the deepest standing wave mode
- Recommend trying 2-3 candidate positions and re-measuring to compare

**Room treatment:**
- Review `analyze_decay` for modes with T60 > 500ms
- Recommend bass traps for ringing modes, prioritized by audibility (SPL x T60)

**Rattles:**
- Check coherence — narrow drops at specific frequencies suggest mechanical resonances
- Recommend the user check for loose objects, ductwork, thin panels

### Next steps — prioritized action list

Present a numbered list ordered by expected impact, in plain language:
1. Physical changes (sub placement, room treatment, rattle fixes)
2. EQ improvements (FIR for ringing modes, slot optimization)
3. Re-run calibration after changes

## Notes

**Ported sub below port resonance:** The SVS PB12-NSD has a port tuned to ~22 Hz.
Below this frequency, output rolls off steeply. Do not boost below 25 Hz —
SafetyValidator enforces this. Deep bass at 20 Hz should be handled by the
mandatory HPF, not boost.

**Room modes:** Large peaks in the 40–80 Hz range are often room modes, not
sub response. Cut them rather than boosting the surroundings. Cuts are always
safe and are not limited by SafetyValidator.

**Multiple measurements:** For more reliable results, take 3 measurements at
slightly different mic positions (within 0.5 m) and average them by calling
`get_measurement_history(limit=3)`. Average the SPL values across measurements
at each frequency before calculating corrections.

**Harman target calibration:** The relative target values above assume the
listening position is in a room with typical bass gain (rooms tend to add
3–6 dB at low frequencies relative to open space). If the room is unusually
well-damped at low frequencies, reduce the bass lift values by 2–3 dB.

## MCP tools used

### Hardware I/O
- `check_system` — pre-flight hardware verification
- `measure` — take a sweep measurement
- `apply_eq` — write correction filters
- `set_volume` — set AVR volume for sweep playback
- `calibrate_level` — find optimal sweep volume

### Analytics (data for LLM judgment)
- `analyze_phase` — per-band fixability: minimum-phase vs excess-phase
- `analyze_decay` — T60 ringing analysis for EQ Q selection
- `compute_deviation` — RMS deviation with automatic null/rolloff exclusion

### Simulation (verify before applying)
- `simulate_eq` — predict FR after proposed PEQ filters
- `optimize_q` — find best Q for a filter at a chosen frequency and gain

### State and config
- `get_config` — discover output slots, EQ capabilities, mic config
- `get_measurement_history` — FR data with coherence for filter design
- `compare_sessions` — per-band delta between two sessions
- `read_eq` — current PEQ state (for iterative filter merging)
