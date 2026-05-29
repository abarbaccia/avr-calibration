# Trinnov-Style Decay Correction — Research Notes

## What was implemented

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

## Current state

`design_fir_trinnov` is implemented and registered as an MCP tool. The algorithm
produces partial results (eliminates low-frequency modes) but can worsen
closely-spaced modes in the 50–90 Hz range. Do not use on live hardware without
verifying T60 at all modes post-application.

The production listening FIR remains the 2-pulse Gabor design (23.4 + 46.9 Hz) from
`design_fir_multi_modal`, which is validated and stable.

## References

- `calibrate/multi_fir.py`: `_compute_trinnov_precausal()`, `design_fir_trinnov()`
- `calibrate/mcp_server.py`: `_tool_design_fir_trinnov()`
- Related: `design_fir_multi_modal()` (production Gabor approach)
- Trinnov Altitude documentation (proprietary; community descriptions only)
