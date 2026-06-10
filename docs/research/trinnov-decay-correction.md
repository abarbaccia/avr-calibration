# Trinnov-Style Decay Correction — Research Notes

> **RESOLVED 2026-06-10.** The pre-causal time-reversed-ringing approach
> described below was removed. It is mathematically unsound (a matched filter
> that re-excites the very modes it targets). `design_fir_trinnov` is now a thin
> wrapper over the regularised complex Wiener inverse (`design_multi_input_fir`)
> in **mixed phase** — i.e. "Path to a correct implementation" option #1 below.
> See **Resolution** at the bottom. The sections in between are kept as the
> historical record of why the original approach failed.

## What was implemented (HISTORICAL — removed)

`design_fir_trinnov` in `calibrate/multi_fir.py` — a wideband pre-causal decay
correction that derives the anti-ringing signal directly from the measured room IR
rather than from per-mode Gabor anti-pulses.

**Motivation:** Gabor anti-pulses (used in `design_fir_multi_modal`) require modes to
be >0.5 octave apart. The 46.9/57.1 Hz and 64/72/80 Hz clusters are 0.25–0.3 octaves
apart — adding a third Gabor caused the composite impulse peak to jump 3× (0.11→0.37),
revealing latent room modes via increased excitation SNR and creating artefacts at the
combined intermodulation frequencies.

**Algorithm (as implemented):**

1. Measure room IR with `measure_impulse_ir(n_averages=64)` → baseline impulse response
2. Bandpass-filter IR at 1/6-octave bands from `freq_min` to `freq_max`
3. For each band where Schroeder T60 > `target_t60_ms`:
   - Extract ringing portion (after IR peak)
   - Time-reverse and negate: `anti = -ringing[:pre_delay_samples][::-1]`
   - Scale by `cancel_strength × (excess_T60 / total_T60)`
4. Sum all band corrections → single continuous pre-causal waveform
5. Place Wiener main impulse at `pre_delay_samples` (50 ms)
6. Combine into one FIR buffer

**Why it was expected to work:** Time-reversal of the ringing should create a
cancelling pre-signal that arrives at the listening position in anti-phase with the
room's modal excitation — the same principle as the Gabor anti-pulse but generalised
across all frequencies simultaneously.

## What actually happened

| Freq | Baseline | Post-Trinnov |
|------|---------|--------------|
| 20.2 Hz | 1604 ms | gone |
| 22.7 Hz | 257 ms | gone |
| 32.1 Hz | 178 ms | gone |
| **57.1 Hz** | **~500 ms** | **3457 ms** ← 7× worse |

Low-frequency modes (20–35 Hz) were eliminated. The 57.1 Hz mode became dramatically
worse.

## Root cause

**Time-reversal does not guarantee phase cancellation for non-isolated modes.**

For cancellation to work, the pre-signal must arrive at the listening position with
exactly −π phase relative to the room's modal response at each frequency. The Gabor
anti-pulse achieves this by placing the pulse exactly T/2 = 1/(2f) before the main
impulse — a precise, analytically derived phase relationship.

Time-reversing the ringing does NOT produce this phase relationship in general:

- The room IR is a sum of multiple overlapping exponentially-decaying sinusoids
- Time-reversing the combined ringing mixes the phase contributions of ALL modes
- At some frequencies the result is constructive (→ T60 increases), at others
  destructive (→ T60 decreases)
- Which outcome occurs at a given frequency depends on the room's complex transfer
  function at that frequency — not predictable from the ringing amplitude alone

The 57.1 Hz mode is a room mode with non-trivial phase response. The time-reversed
correction at 57 Hz happened to arrive in constructive phase, amplifying rather than
cancelling the mode (500ms → 3457ms).

## What the real Trinnov Optimizer likely does

The Trinnov Altitude's Optimizer is not a time-reversal algorithm. Based on published
descriptions and the perceptual results users report:

1. **Multi-point measurement** — 3D mic captures at 4+ positions simultaneously,
   producing a spatially-averaged room response. This reduces position-dependent modal
   variation before any correction is attempted.

2. **Joint magnitude + decay optimization** — The correction filter is computed as a
   regularized inverse of the full complex room response, with an additional penalty
   term on excess decay energy. Solving:

   ```
   min ||W(x * h - target)||² + λ_mag ||x||² + λ_decay ||decay(x*h) - target_decay||²
   ```

   This jointly minimises magnitude error and excess decay across all frequencies
   simultaneously, which is fundamentally different from per-mode treatment.

3. **Pre-causal taps from inverse filter** — The resulting filter naturally includes
   anti-causal taps (pre-ring) because the inverse of a minimum-phase room response is
   anti-causal. The Trinnov doesn't explicitly "design" the pre-causal section — it
   emerges from the inversion.

## Path to a correct implementation

To implement this properly in our system:

1. **Complex inverse filter with decay penalty.** Design a filter that minimises
   both magnitude error and excess decay simultaneously using weighted least-squares
   in the frequency domain. The penalty should weight the imaginary (phase) component
   of the room's modal response, not just the magnitude.

2. **Multi-position IR.** Average IRs from 3–5 mic positions at the MLP cluster
   before designing the correction. This suppresses position-dependent modes and
   produces a more robust correction.

3. **Modal decomposition before inversion.** Identify individual mode parameters
   (frequency, Q, amplitude, phase) via ESPRIT or Prony's method on the bandpass-
   filtered IR. Design per-mode cancellation with correct phase using the complex
   mode parameters, not just the ringing amplitude.

Option 3 is closest to a correct extension of the Gabor anti-pulse approach: instead
of estimating mode phase from frequency alone (Gabor assumes T/2 relationship), use
the measured complex mode response to set the correct cancellation phase for each mode.
This would handle closely-spaced modes without the dense-mode interference problem.

## Resolution (2026-06-10)

The pre-causal section was removed. `_compute_trinnov_precausal()` is deleted and
`design_fir_trinnov()` now delegates to `design_multi_input_fir()` (the regularised
complex Wiener inverse) in **mixed** phase, plus a best-effort baseline ringing-mode
report from the IR via `analyze_decay`.

**Why the pre-causal approach is unsalvageable (sharper than the original root cause).**
The corrected response at the mic for an impulse input is exactly `C * h`, where `C`
is the correction FIR and `h` is the sub→mic path. Adding time-reversed mode ringing
to `C` makes `C` a *matched filter* for that mode; convolving a matched filter back
through `h` is an autocorrelation, which **maximally reinforces** the mode. Measured in
simulation against a single isolated 47 Hz mode (so there is no "dense-mode interference"
excuse):

| cancel_strength | T60 @ 47 Hz | steady-state level @ 47 Hz |
|---|---|---|
| 0.0 (pure Wiener) | 578 → **407 ms** | corrected toward target |
| 0.5 | 578 → 585 ms | **+67 dB boost** |
| 1.0 | 585 ms | +73 dB boost |

The boost is sign-independent (matched filter), so the field result (57 Hz: 500→3457 ms)
was not bad luck on phase — it is the systematic behaviour. The only filter that reduces
a mode in both magnitude and decay is the regularised inverse itself.

**Why `phase_mode` matters for multi-sub.** `K_i = T_i·conj(H_i)/(|H_i|²+λ²)` carries the
phase needed to land every sub at a common target phase so they sum coherently. Realising
it in `minimum` phase discards that — two subs at different arrival times then cancel at
the listener (a −23 dB null at 80 Hz in simulation; ~+1.5 dB measured vs +6 dB predicted
at 40 Hz on run 25). `mixed` preserves it and lands within ~1.5 dB of target. Default is
now `mixed`; `minimum` remains available for single-sub magnitude-only work.

**Safety.** The old path auto-tagged the FIR `modal_cancel` whenever the pre-causal peak
exceeded 0.01, which raised the FIR boost cap from the strict 10 dB thermal limit to
60 dB — exactly what let the +35 dB matched-filter boost through. The Wiener-only design
is tagged `correction` (strict cap) and is safe to sweep through.

What option #3 (per-mode complex cancellation via ESPRIT/Prony) was hoping to achieve is
subsumed by the complex inverse: a sufficiently long mixed-phase Wiener filter inverts the
mode's pole directly, no per-mode decomposition needed. Multi-position IR averaging
(option #2) remains a worthwhile future robustness improvement, independent of this fix.

## Current state

`design_fir_trinnov` = coherent mixed-phase Wiener inverse + informational T60 report.
No anti-ringing/pre-causal section. Tagged `correction` (strict safety cap), sweep-safe.

The legacy 2-pulse Gabor design (`design_fir_multi_modal`) is unchanged. Note that the
`C * h` matched-filter argument applies to Gabor anti-pulses too; treat their measured
T60 benefit with the same skepticism and verify per-mode before trusting it.

## References

- `calibrate/multi_fir.py`: `design_fir_trinnov()` (wrapper), `design_multi_input_fir()`
- `calibrate/mcp_server.py`: `_tool_design_fir_trinnov()`
- Related: `design_fir_multi_modal()` (legacy Gabor approach)
- Trinnov Altitude documentation (proprietary; community descriptions only)
