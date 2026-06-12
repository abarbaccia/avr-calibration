# Recipe: Trinnov-Style Coherent Multi-Sub Calibration
version: 1.0

## What this is and when to use it

This recipe designs FIRs using the **complex Wiener inverse** — the same
principle used by Trinnov Altitude room correction. Unlike per-sub independent
FIR design (`bass-calibration-fir.md`), the Wiener inverse uses the phase
relationship between subs to design filters that, when summed acoustically at
MLP, produce a combined response matching the target in both magnitude AND phase.

**Use this when:**
- You have 2+ subs that cancel in some bands at MLP despite physical alignment
- `bass-calibration-fir.md` converged on magnitude but destructive interference
  persists (combined < max(solo) in some bands even after FIR)
- You want the best possible coherent summation at MLP

**Do NOT use when:**
- One sub is significantly weaker than the other (>6 dB in any band) — the
  Wiener boost to compensate will hit safety limits or correct into noise.
  Fix the hardware imbalance first. Sub 6 on this rig is known weak above
  40 Hz; resolve before running Trinnov.
- You only have 1 sub — use `bass-calibration-fir.md` instead.

**Core algorithm:**

```
H_i(f) = measured complex response of sub i (mag×exp(j×phase))
T(f)   = complex target (min-phase from |target_curve|)

Per-sub correction:
  K_i = (T/N) × conj(H_i) / (|H_i|² + λ²)

Properties:
  • When |H_i| >> λ:  K_i·H_i ≈ T/N  (full correction)
  • When |H_i| ≈ λ:   K_i·H_i ≈ T/(2N) (regularization limits boost)
  • When |H_i| << λ:  K_i·H_i ≈ 0  (deep null — don't fight it)

All K_i phase-land at the same target phase → coherent summation.
```

**What it CANNOT do:** Trinnov FIRs reduce both magnitude AND decay of modes
that appear in H_i. They do NOT add a separate anti-pulse/time-reversal
correction — that approach is a matched filter that re-excites modes (+tens dB,
no T60 benefit). The Wiener's decay benefit comes from the complex inverse
itself; longer FIR taps help by giving the correction a longer impulse window.

## Compliance

Execute every step in order. The phase data path is brittle — a single
measurement at a different `loopback_xcorr_peak_ms` contaminates the design.

1. `check_system` must pass ALL checks before measuring. "Loopback timing
   stability" is specifically relevant here — if it warns, re-examine.
2. Measure ALL subs **in the same session** (same FIR load, same PipeWire
   quantum, same day). Phase data is only cross-comparable when
   `loopback_xcorr_peak_ms` is consistent.
3. `measure_impulse_ir` is required before `design_fir_trinnov` — it gives
   the T60 report needed to understand which modes the FIR is correcting.
4. After applying FIRs, run `verify_trinnov_coherence` — this is the ONLY
   check that validates the inter-sub phase property. `verify_fir_effect`
   checks magnitude only and cannot detect phase failures.

## Phase 0 — Preflight (fully automated — execute without prompting)

Run in order, no user confirmation needed for any step:

```
1. check_system()
   Abort if: loopback ref fails, CamillaDSP not Running, loopback timing std > 0.5 ms.

2. mute_output(target='shaker')
   Shaker is ALWAYS muted during all calibration measurements. No exceptions.

3. clear_fir(output_index=5)
   clear_fir(output_index=6)
   Wipe any prior FIR state so Phase 2 measures the physical room, not a
   pre-shaped response. If the user explicitly says "keep existing FIRs as
   the baseline", skip this step — but that is the exception, not the default.

4. calibrate_level()
   Establishes the working sweep level. REQUIRED at every session start —
   do not skip even if a level was found last session. The Gain block state
   does not survive container restarts. calibrate_level sets master_gain_db
   in config; record the returned calibrated_master_gain_db and
   suggested_solo_gain_db for use in Phase 0.5.

5. apply_eq(output_index=5, protective_peq)
   apply_eq(output_index=6, protective_peq)
   Apply protective PEQ to BOTH subs before any measurement:
     HPF 18 Hz (4th-order Butterworth)
     Peaking  22 Hz, −10 dB, Q=3
     Peaking  28 Hz, −8 dB, Q=3
   Port resonance without these cuts saturates the chain and collapses
   coherence above 60 Hz. This is NOT optional.
```

If "DSP persisted state" shows active FIRs from a prior session (beyond the
protective PEQ just applied), stop and confirm with the user before proceeding.

## Phase 0.5 — Sub hardware balance check (automated)

Run without prompting. Use suggested_solo_gain_db from calibrate_level above.

```
1. mute_output(output_index=6); unmute_output(output_index=5)
   set_config(measurement.master_gain_db = suggested_solo_gain_db)
   measure(label='sub5-solo-balance', position='MLP', target='subs')
   session_5 = last session_id

2. mute_output(output_index=5); unmute_output(output_index=6)
   measure(label='sub6-solo-balance', position='MLP', target='subs')
   session_6 = last session_id

3. Unmute both: unmute_output([5, 6])
   set_config(measurement.master_gain_db = calibrated_master_gain_db)
```

**Polarity check (auto-fix):**
Compare `ir.peak_sign` from session_5 and session_6.
- If signs differ → call `set_polarity(output_index=X, inverted=True)` on
  whichever sub has peak_sign=−1 to match the positive sub. Do NOT ask the
  user — just fix it and report what you did.
- Verify: run a combined sweep and confirm coherence ≥ 0.85 at 25–80 Hz.
  If combined coherence is low after the polarity fix, report this as an
  anomaly (unexpected acoustic interference, may be placement/room issue).

**Balance check:**
Get FR for both solo sessions: `get_measurement_history(limit=2, min_hz=25, max_hz=80, smooth='third_octave')`

- If any 1/3-octave band 25–80 Hz shows >6 dB difference → hardware imbalance.
  Do NOT proceed. Report: which sub is weak, at which frequencies, by how much.
  Likely causes: amp gain knob, cable polarity (but polarity was just fixed),
  standby mode. User must investigate physically before Trinnov can run.
- If all bands within 6 dB → proceed to Phase 1.

## Phase 1 — Alignment (same as bass-calibration-fir.md)

Run Phases 0–1.5 of `bass-calibration-fir.md` to align the subs. Trinnov
corrects phase BUT it cannot fix >10 ms misalignment — the Wiener has a
limited pre-ring budget (`preringing_ms`, default 20 ms). Gross misalignment
burns the entire pre-ring budget on delay compensation, leaving nothing for
mode correction.

If subs are already aligned from a prior session and you've confirmed
`loopback_xcorr_peak_ms` stability, you may skip to Phase 2.

## Phase 2 — Measure each sub solo (the Trinnov measurement)

**Clear FIR preflight** (REQUIRED — always clear before Trinnov measurement):

```
clear_fir(output_index=5)
clear_fir(output_index=6)
```

This ensures H_i captures the physical room only — a correction FIR bakes
that correction into K_i, making the design circular. Use `clear_fir` to
reset to identity passthrough; this preserves the Conv block topology so
the PipeWire quantum stays stable between sessions.

**Per-sub measurement:**

For each sub (output_index 5 and 6):
1. `mute_output` all other subs and shakers
2. `measure(label="sub_{N}-solo-trinnov", position="MLP")` — note `session_id`
3. Verify `loopback_xcorr_peak_ms` is within 0.3 ms of the other sub's session
4. `unmute_output`

**If xcorr_peak_ms differs by >0.5 ms between the two sessions:**
Stop. Something shifted the PipeWire pipeline quantum between measurements.
Restart the avr-calibration container, re-run identity FIRs, and re-measure
both subs in immediate succession.

## Phase 3 — Baseline IR for T60 report

```
measure_impulse_ir(n_averages=64)
```

This takes ~3 minutes. The impulse IR is used ONLY for the ringing-mode
report in `design_fir_trinnov` — not to design the correction. It can be
taken with either set of FIRs loaded; identity FIRs are cleanest.

```
analyze_decay(ir_session_id=<from above>, bands_per_octave=6)
```

Record the modes and T60s. This is your pre-correction baseline.

## Phase 4 — Design Trinnov FIRs

```
design_fir_trinnov(
    ir_session_id=<from Phase 3>,
    measurements=[
        {session_id: <sub5_solo>, output_index: 5, label: "sub_front_left"},
        {session_id: <sub6_solo>, output_index: 6, label: "sub_front_right"},
    ],
    target_curve={
        "points": [
            {"freq": 25, "spl": 5},
            {"freq": 31.5, "spl": 4},
            {"freq": 40, "spl": 3},
            {"freq": 50, "spl": 2},
            {"freq": 63, "spl": 1},
            {"freq": 80, "spl": 0},
        ]
    },
    num_taps=24576,
    phase_mode="mixed",
    regularization_lambda=0.01,
    freq_focus_hz=[25, 100],
    preringing_ms=20,
)
```

**Inspect the response:**
- `fir_sample_rate_hz` — must match CamillaDSP's live processing rate (48000
  on this rig — verified via GetConfigJson 2026-06-12; the graph and DSP both
  run 48 kHz). The tool reads the rate from the active driver; a mismatch
  means the DSP was unreachable when the tool ran. Stop and fix.
- `per_sub_peak_boost_db` — if any sub exceeds +8 dB, the regularization is
  too small for that sub's signal level. Increase `regularization_lambda` to
  0.05 and redesign.
- `balance_warnings` — if present, a sub is too weak. Do not proceed.
- `latency_ms` — the pre-ring latency added to the sub chain. Mains must be
  delayed by this amount via SPEAKER DISTANCE (not the global Audio-Delay).
  At 20 ms pre-ring, subs are 20 ms early relative to mains; mains need
  +20 ms distance. Conversion: 1 ms = 0.343 m = 1.125 ft.

**Tap count rationale:**
- 24576 taps @ 48 kHz = 512 ms impulse window
- T60s in this room are typically 400–800 ms at 47–70 Hz
- 512 ms covers ~65–100% of the T60. (2× T60 coverage would need up to
  ~77000 taps at 48 kHz — beyond practical limits.) The partial coverage
  still reduces both magnitude and early decay.

## Phase 5 — Apply FIRs

```
apply_fir(output_index=5, design_session_id=<cache_id for sub5>)
apply_fir(output_index=6, design_session_id=<cache_id for sub6>)
```

`design_fir_trinnov` returns `cache_ids: [{output_index, design_session_id}]`.
Use the `design_session_id` values from there — do not guess.

## Phase 6 — Verify coherent summation

**This is the critical Trinnov-specific check.** If you skip it, you have no
evidence that the FIRs are summing coherently rather than cancelling.

6.1 Measure each sub solo (FIRs active):
  - `measure(label="sub5-solo-postfir")` → `solo_session_5`
  - `measure(label="sub6-solo-postfir")` → `solo_session_6`

6.2 Measure combined (both subs, FIRs active):
  - `measure(label="combined-postfir")` → `combined_session`

6.3 Verify coherence:
```
verify_trinnov_coherence(
    combined_session_id=<combined_session>,
    solo_session_ids=[<solo_session_5>, <solo_session_6>],
    min_hz=20, max_hz=100
)
```

**Decision rule on `verdict`:**
- `pass`: combined ≥ max(solo) in all bands. Trinnov is working. Proceed to Phase 7.
- `marginal` (1–2 destructive bands): likely room geometry at those specific
  frequencies. If the null is in 20–40 Hz and both subs have coherence > 0.7
  there, try `sweep_inter_sub_delay` with `priority_band=[20, 50]` to move the
  null. Accept and document if it stays.
- `fail` (3+ destructive bands): the FIRs are not summing coherently. Diagnose:
  1. Was `loopback_xcorr_peak_ms` stable across the Phase 2 sessions?
  2. Were both subs measured with the same FIR tap count active (identity FIRs)?
  3. Try `phase_mode="minimum"` to check if magnitude-only correction helps —
     if it does, the problem is in the mixed-phase target-phase calculation.
  4. Re-measure both subs immediately back-to-back and redesign.

**Also verify T60 improvement:**

```
measure_impulse_ir(n_averages=64)   # with Trinnov FIRs active
analyze_decay(ir_session_id=<new>, bands_per_octave=6)
```

Compare T60 at each mode against Phase 3 baseline. Expect 20–40% reduction at
modes within the `freq_focus_hz` band for 24576-tap FIRs covering ~30% of T60.

## Phase 7 — Sub-mains time alignment

Trinnov FIRs add `latency_ms` pre-ring to the sub chain. Mains must be
delayed to match.

1. Compute mains distance delta: `distance_m = latency_ms × 0.343`
2. Add to current mains SPEAKER DISTANCE in MultEQ (NOT the global Audio-Delay)
3. Verify via `measure_impulse_ir` on one main + one sub combined IR peak comparison

## Phase 8 — Cleanup

1. `unmute_output` all outputs that were muted
2. `end_sweep_session`
3. `set_master_gain(0)` — calibrate_level may have lowered this
4. `get_device_state` — confirm master_gain=0, no muted outputs

## MCP tools used

| Tool | When |
|------|------|
| `check_system` | Phase 0 preflight |
| `measure` | Phase 2 per-sub solo |
| `measure_impulse_ir` | Phase 3 (baseline T60) and Phase 6 (post-FIR T60) |
| `analyze_decay` | Phase 3 and Phase 6 T60 reports |
| `design_fir_trinnov` | Phase 4 FIR design |
| `apply_fir` | Phase 5 FIR application |
| `clear_fir` | Phase 2 preflight (reset to physical room baseline) |
| `verify_trinnov_coherence` | Phase 6 — coherent summation check |
| `verify_fir_effect` | Phase 6 — magnitude check (complementary) |
| `compare_sessions` | Phase 0.5 sub balance check |
| `sweep_inter_sub_delay` | Phase 6 if marginal null remains |
| `mute_output` / `unmute_output` | Solo measurements |
| `end_sweep_session` / `set_master_gain` | Phase 8 cleanup |

## Safety notes

- FIR safety validation uses `intent="correction"` (strict thermal cap, not
  modal_cancel). `per_sub_peak_boost_db` values above +8 dB will be rejected
  by `apply_fir`. Increase `regularization_lambda` to cap boosts.
- Shakers are NEVER active during measurement. Hard rule.
- The minimum-phase FIR below-port-tune spillover fix applies: ensure the
  target_curve includes `spl=-8 at 20 Hz` and `spl=-2 at 28 Hz` to force
  attenuation below the SVS PB12-NSD port tune. The default Harman curve
  starts at 25 Hz; extend it down if needed.
