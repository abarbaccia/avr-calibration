# Recipe: Per-Sub EQ + Harman Bass Target

## Goal

Calibrate multiple subwoofers using a two-layer EQ strategy:
- **Output PEQ** (per-sub): flatten each sub's individual room response
- **Input PEQ** (shared): apply the Harman bass target curve to all subs equally

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

Verify all hardware is connected and reachable before starting.
Mute any non-sub outputs (e.g. shakers) during calibration.

## Phase 0 — Level Setup

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
- Suggest turning up the volume knob on the quieter sub.
- Wait for user confirmation before continuing.

### 0.5 Apply level trims

Apply the computed gain trims to miniDSP output gains.
The loudest sub stays at 0 dB gain. Quieter subs get positive trim.

### 0.6 Calibrate sweep level

Call `calibrate_level` to find the optimal sweep volume with good SNR.

## Phase 1 — Sub Alignment

### 1.1 Measure each sub solo

For each subwoofer:
1. Mute all other subs
2. Take a measurement — note the session_id
3. Call `analyze_ir(session_id)` — get `peak_time_s`, `peak_sign`, `spl_db`
4. Unmute

### 1.2 Apply corrections

Compare the per-sub `analyze_ir` results:
- **Delay**: The sub with the latest `peak_time_s` is the reference (delay = 0).
  Each other sub gets delay = (reference_peak_time_s − its_peak_time_s) × 1000 ms.
  Apply via `set_delay`.
- **Polarity**: Sub 0 is the polarity reference. Any sub with opposite `peak_sign`
  gets `set_polarity(inverted=True)`.

### 1.3 Verify alignment

Measure all subs together. Combined response should be louder than any
individual sub (reinforcement). If combined is quieter at some frequencies,
subs are still cancelling — revisit delay and polarity.

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

### 2.2 Design per-sub correction filters

For each sub, compare its solo FR to flat (its own average level):
- Peaks above the average: cut with peaking filter (always safe)
- Dips below the average: leave alone if narrow (likely a null — unfixable)
- Broad dips: gentle boost if > 3 dB below average (limited by safety)

**Prefer cuts heavily.** The goal is to flatten each sub's response,
not to boost it to match a target. Nulls cannot be filled with EQ.

Always include the mandatory 18Hz HPF.

### 2.3 Apply per-sub EQ

For each sub, call `apply_eq` with `output_index` set to that sub's
output index. Each sub gets its own independent filter set.

### 2.4 Optional — Decay analysis after per-sub EQ

After applying per-sub corrections, call `analyze_decay(session_id)` on the most
recent solo measurement to check if any modes exhibit T60 > 500ms. If so, use
`suggested_q` from that mode when designing the PEQ cut — a narrower Q targets
the ringing frequency more surgically without over-cutting broadband energy.

### 2.6 Re-measure each sub solo and iterate

After applying per-sub EQ:
1. Measure each sub solo again
2. Check if its response is flatter (lower variance across 25-80Hz)
3. If variance > 3 dB: adjust and re-measure
4. Maximum 3 iterations per sub

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

After applying input EQ, re-measure combined response (label "iter-N @ MLP"):
- RMS deviation < 2.0 dB from the anchored target: done
- Otherwise: adjust input PEQ and re-measure
- Do NOT re-anchor the target between iterations (it was set in 3.2)
- Maximum 5 EQ iterations

## Convergence

- **Level match**: All subs within 3 dB before digital trim
- **Alignment**: Combined response reinforces vs individual subs
- **Per-sub EQ**: Each sub's solo FR variance < 3 dB across 25-80Hz
- **Harman target**: Combined RMS deviation < 2.0 dB across 20-80Hz

## When convergence fails

If max iterations reached:
- Report final state and remaining deviations per phase
- Deep nulls in per-sub solo measurements indicate placement problems
- Frequencies below the sub's capability cannot be boosted — expected
- If per-sub EQ can't flatten a sub, suggest repositioning that sub
- If the combined Harman pass can't converge, the per-sub corrections
  may need revisiting — check for cancellation between subs

## MCP tools used

- `get_config` — discover `eq_capabilities` (input/output PEQ slots, labels, current state)
- `measure` — take a sweep measurement
- `apply_eq` — write per-sub correction filters (with `output_index` for single-output targeting)
- `apply_input_eq` — write shared Harman target curve to DSP input
- `mute_output` / `unmute_output` — isolate subs for solo measurement
- `set_delay` / `set_polarity` / `set_output_gain` — sub alignment
- `get_output_state` — verify current per-output state mid-calibration
- `analyze_ir` — IR peak time/sign/SPL for computing delay and polarity corrections
- `analyze_decay` — T60 ringing analysis for EQ Q selection (optional, Phase 2)
- `calibrate_level` — find optimal sweep volume
