# Recipe: Per-Sub EQ + Harman Bass Target

## Goal

Calibrate multiple subwoofers using a two-layer EQ strategy:
- **Output PEQ** (per-sub): flatten each sub's individual room response
- **Input PEQ** (shared): apply the Harman bass target curve to all subs equally

This separation gives the combined Harman pass a much smoother starting point
and keeps per-sub corrections independent from the target curve.

## EQ Architecture

```
Input PEQ (shared Harman target)
    │
    ├── Output 0 PEQ (Sub 0 room correction)  → Left Front Sub
    ├── Output 2 PEQ (Sub 2 room correction)  → Nearfield Sub
    └── Output 3 (shakers, muted during cal)
```

- **Input PEQ**: Harman target curve + mandatory 18Hz HPF. Applied once,
  affects all outputs equally. Lives on the active input channel.
- **Output PEQ**: Per-sub room correction. Each output gets its own filters
  to flatten that sub's unique room interaction before the shared target
  curve is applied.

## Pre-flight

Verify all hardware is connected and reachable before starting.
Mute any non-sub outputs (e.g. bass shakers) during calibration.

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
2. Take a measurement
3. Record the impulse response (IR peak time and polarity)
4. Unmute

### 1.2 Apply corrections

Compare the per-sub impulse responses:
- **Delay**: Delay earlier-arriving subs to match the latest
- **Polarity**: Flip polarity if IR peak is inverted relative to others

### 1.3 Verify alignment

Measure all subs together. Combined response should be louder than any
individual sub (reinforcement). If combined is quieter at some frequencies,
subs are still cancelling — revisit delay and polarity.

Maximum 3 alignment iterations.

## Phase 2 — Per-Sub Room Correction (Output PEQ)

This is the new phase. Each sub gets its own EQ to flatten its individual
room response before the shared Harman target is applied.

### 2.1 Measure each sub solo (post-alignment)

For each subwoofer:
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

Always include the mandatory 18Hz HPF on each output.

### 2.3 Apply per-sub EQ to output PEQ slots

For each sub output, call `apply_eq` with that sub's correction filters.
Each output gets its own independent filter set.

**Important:** This requires per-output EQ writes — the `apply_eq` tool
must support targeting a specific output, not all sub outputs at once.

### 2.4 Re-measure each sub solo and iterate

After applying per-sub EQ:
1. Measure each sub solo again
2. Check if its response is flatter (lower variance across 25-80Hz)
3. If variance > 3 dB: adjust and re-measure
4. Maximum 3 iterations per sub

## Phase 3 — Harman Target (Input PEQ)

Now all subs are individually flattened. Apply the shared Harman target
curve to the DSP input, which affects all subs equally.

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
Calculate RMS deviation from the Harman target.

### 3.2 Apply Harman EQ to input PEQ

Design filters to match the Harman target:
- Above target: cut (always safe)
- Below target: boost (limited by safety)

Apply these filters to the **input PEQ** (not outputs). This ensures all
subs receive the same target curve correction.

Always include the mandatory 18Hz HPF.

### 3.3 Re-measure and iterate

After applying input EQ, re-measure combined response:
- RMS deviation < 2.0 dB: done
- Otherwise: adjust input PEQ and re-measure
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

## Driver requirements

This recipe requires two capabilities not yet in the MCP tools:

1. **Input PEQ writes** — `apply_input_eq` tool to write PEQ filters to
   the active input channel (not outputs). Uses the same minidspd partial
   config API: `{"inputs": [{"index": N, "peq": [...]}]}`

2. **Per-output EQ writes** — `apply_eq` must support targeting a single
   output index, not broadcasting to all sub outputs. This allows each
   sub to have independent room correction filters.
