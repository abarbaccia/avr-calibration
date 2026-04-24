# Recipe: Bass Calibration (FIR-Native)
version: 1.1

## Compliance — READ FIRST (NON-NEGOTIABLE)

**You MUST execute every step in this recipe in order. You MUST NOT skip
checkpoints, even when a later phase looks ready.** The recipe encodes
learnings from prior runs; skipping a step has caused measurable audible
regressions (e.g. run 14 skipped Phase 2.5 and left a major mode at 1900 ms
T60 instead of the ~700 ms mixed-phase would have delivered).

Rules:
1. **Do not skip a numbered step**, even if you believe it's a no-op. If a
   step is genuinely N/A (single-sub skip in Phase 1), say so explicitly in
   your run log. "I assumed it wasn't needed" is not acceptable.
2. **Do not proceed past a decision point without executing it.** Every
   "Decision point:" heading is a required branch — evaluate the condition,
   log the evaluation, then take the branch the data dictates.
3. **When you take a shortcut, stop and ask the user first.** Do not rewrite
   the recipe under fatigue, pressure to finish, or "this looks close enough".
4. **Every `analyze_decay` / `analyze_phase` call that flags a mode over
   threshold REQUIRES a follow-up action** — either a filter redesign, a
   documented "unfixable — geometry" classification, or an explicit user
   override. Seeing the flag and moving on is a recipe violation.
5. **Record compliance in each `save_calibration_iteration` call.** The
   `filters_proposed` field must include which recipe step produced the
   decision, so post-hoc audits can detect skips.

If at any point the recipe feels wrong for the hardware in front of you,
surface the disagreement to the user and amend the recipe — do not silently
deviate mid-run.

## Goal

Calibrate one or more subwoofers on **FIR-capable hardware** (CamillaDSP) using a
two-layer strategy that decouples per-sub room correction from target-curve shape:

- **Per-sub correction:** minimum-phase FIR flattens each sub's individual room
  response across the target band. High-resolution, catches narrow modes that
  PEQ slot budgets would force you to skip.
- **Target curve:** shared input PEQ shapes the combined response to the chosen
  curve (Harman, flat, custom). Low-order biquads are a natural fit for smooth
  curves and keep the target layer cheap to redesign.

The two layers are independent. To swap target curves (Harman → flat, etc.),
re-run Phase 3 only — the per-sub FIRs stay in place.

Adapts to the hardware:
- **1 sub:** skips alignment (Phase 1), skips combined-verification.
- **2+ subs:** full alignment, per-sub FIR, combined target shaping.

## Hardware requirements

This recipe **requires** FIR-capable DSP with enough taps per output to design
a useful bass-band correction. Check `eq_capabilities` from `get_config`:
- `fir_capable` must be `true`
- `fir_max_taps_per_output` should be `>= 4096`
- If not met, use `bass-calibration.md` (PEQ-based) instead.

## Addressing scopes — `target` vs `output_index`

Every EQ tool accepts a scope by name. Read the graph once with
`get_signal_graph` (or `get_config` — it's embedded), then use transducer and
group names in preference to raw output indices:

- `apply_eq(target="bass", filters=[...])` — broadcast to every sub in the
  `bass` group.
- `apply_fir(output_index=N, coefficients=[...])` — FIR is per-output only;
  resolve `target` → `output_index` via the signal graph first.
- `resolve_target("bass")` — get `{transducer, output_index, profile}` entries
  when you need raw indices for tools that still take `output_index` only
  (`set_delay`, `set_polarity`, `set_output_gain`, `mute_output`, `apply_fir`).

## Measurement signal path

Show the active signal path based on `get_config.measurement.playback_route`:
- **`"usb"`:** Pi → USB audio → DSP → sub outputs → room → mic → Pi. AVR is
  not in the loop.
- **`"hdmi"`:** Pi → HDMI → AVR → DSP (via LFE) → sub outputs → room → mic → Pi.

Build the diagram dynamically from config — sub labels from `output_slots`,
mic from `config.mic.name`, AVR from `config.denon`, DSP name from `config.dsp_driver`.

## Filter strategy

Three layers, all of them simultaneously active in the DSP pipeline:

| Layer | Tool | Purpose | Required? |
|-------|------|---------|-----------|
| Per-sub FIR | `apply_fir` with `output_index` | Room-mode flattening (magnitude + some decay reduction) | Always |
| Per-sub PEQ | `apply_eq` with `output_index` | **HPF only** (18 Hz 4th-order infrasonic protection) | Always |
| Input PEQ | `apply_input_eq` | Target curve shape across the combined response | Always |

**DSP signal chain:**
```
Input → Input PEQ (target curve) → Routing → Output PEQ (HPF) → Output FIR (per-sub correction) → DAC
```

**Recipe ordering** (correction layers applied in this order so each one is
designed against the state produced by the previous layer):

```
1. Per-sub HPF only (Phase 0 reset)
2. Alignment via delay + polarity (Phase 1)
3. Per-sub FIR flattening (Phase 2)
4. Input PEQ target curve (Phase 3)
```

## Configuration

Before calibration begins, walk the user through these choices. Suggest defaults
based on hardware config. Most users accept defaults.

### Pick a target curve

List all `.json` files in `recipes/curves/`. For each, show the `name` and
`description`. Mark the one with `"default": true`.

If custom, validate:
- At least 3 frequency/offset pairs
- Frequencies monotonically increasing
- A reference frequency specified (default: highest frequency in the curve)

Load the chosen curve from `recipes/curves/{name}.json`. The `points` array has
`freq_hz` and `offset_db` (relative to `reference_freq_hz`). Absolute target at
each frequency = `reference_spl + offset_db` (reference_spl computed during
anchoring in Phase 3).

### Pick the bass range

- **Lower bound:** `sub.port_tune_hz + 3` (25 Hz for SVS PB12-NSD).
- **Upper bound:** LCR crossover frequency (80 Hz THX standard, 60/100/120
  depending on mains). Ask the user if unknown.

If the target curve's highest defined point is below the crossover, extend the
curve flat from its reference frequency to the crossover.

Report: `"Calibrating 25–80 Hz with Harman target"` (or whatever the user chose).

### FIR parameters

| Parameter | Default | Range | What it means |
|-----------|---------|-------|---------------|
| FIR taps per sub | **8192** | 4096–32768 | More taps = better low-frequency resolution, higher DSP cost |
| FIR phase mode | **minimum** | minimum / mixed | Minimum = no pre-ring (safe). Mixed = some pre-ring for extra decay reduction below 80 Hz (advanced) |
| Frequency focus | **`[port_tune+3, crossover+10]`** | — | Correct within this band, taper to passthrough outside so the FIR doesn't alter the rest of the spectrum |

Explain tradeoffs:
- **8192 taps @ 48 kHz** = ~170 ms impulse. Enough to correct most bass modes.
- **16384 taps** = ~340 ms. Needed if the room has very long decay (T60 > 600 ms).
- **Mixed-phase** is useful only if minimum-phase leaves T60 > 500 ms on a
  major mode after Phase 2 iteration. Don't use it by default.

### Convergence goals

| Parameter | Default | Range | What it means |
|-----------|---------|-------|---------------|
| Per-sub flatness (post-FIR) | **± 2 dB** | 1–4 | Max deviation from flat across target band per sub |
| RMS deviation from target | **1.5 dB** | 0.5–3.0 | Phase 3 stop criterion |
| Max iterations/phase | **3** | 2–5 | FIR converges faster than PEQ; usually one pass |

### Safety limits (code-enforced)

| Limit | Value | Why |
|-------|-------|-----|
| Min boost frequency | 25 Hz | Port protection (SVS PB12-NSD) |
| Max boost (PEQ) | +6 dB per band | Prevents unloading |
| Max cumulative boost | +9 dB per 1/3 oct | Thermal |
| Mandatory HPF | 18 Hz, 4th-order | Infrasonic protection |
| **FIR magnitude check** | Design-time, in recipe | See note below |

`SafetyValidator` code enforces PEQ limits at `apply_eq` / `apply_input_eq`
time. **It does NOT currently inspect FIR coefficient magnitude responses.**
The recipe must check the predicted FIR magnitude before calling `apply_fir`:
- If the predicted FIR boost exceeds +6 dB anywhere between 20–25 Hz → narrow
  the `freq_focus_hz` lower bound until the boost tapers below that threshold.
- If the predicted FIR boost exceeds +8 dB anywhere 25–120 Hz → reduce tap
  count or accept a softer correction.

## Pre-flight

1. Call `check_system`. Abort if any hardware is unreachable.
2. Call `get_config`. Verify `eq_capabilities.fir_capable == true` and
   `fir_max_taps_per_output >= 4096`. If not, STOP and suggest the user switch
   to `bass-calibration.md`.
3. Mute shakers (any output where `type == "shaker"` in `output_slots`).
   **Shakers are NEVER active during measurement.** Hard rule.

## Phase 0 — Setup

### 0.0 Reset ALL DSP state

**⚠️ NEVER SKIP THIS PHASE — IT DIRECTLY AFFECTS FIR DESIGN ACCURACY**

The MCP driver carries in-memory DSP state across calibration runs and pushes the
full config to CamillaDSP on every write. This means stale FIR coefficients, PEQ
filters, gain trims, and delays from a prior run are silently active when the new
run starts. Any baseline measurement taken without resetting first is contaminated —
the FIR design will target a pre-shaped response, and the final calibration will be
wrong. This has caused real regressions (e.g., run 17 FIRs designed against run 16's
input PEQ, producing a mismatched FIR-PEQ combination).

For **every output** (including unused/shaker):
1. `set_delay(output_index, 0)` — clear alignment delays
2. `set_polarity(output_index, inverted=false)` — clear polarity flips
3. `set_output_gain(output_index, 0)` — clear level trims
4. `clear_fir(output_index)` — reset to passthrough before designing new FIRs

For **each sub output**:
5. `apply_eq(output_index, [HPF only])` — HPF-only baseline, clears all PEQ slots

For **inputs**:
6. `apply_input_eq([HPF only])` — **clears any target curve from prior runs**
7. `set_master_gain(0)` — reset master gain

**Verification (mandatory):** After completing the above, call `get_output_state` for
each sub output and confirm: `fir_taps == 0`, `gain_db == 0`, `delay_ms == 0`.
If any value is non-zero, the reset did not complete — do NOT proceed to measurements.

### 0.1 Configure input routing

`configure_matrix(active_input=config.minidsp.active_input)` — route active
input to all enabled outputs, mute unused inputs.

### 0.2 Set initial volume

- **USB mode:** `set_master_gain(-30)` as a safe starting point. `calibrate_level`
  in 0.7 will tune this.
- **HDMI mode:** `set_volume(-30)` on the AVR.

### 0.3 Mute shakers

For each output where `type == "shaker"`, call `mute_output(output_index)`.
Hard rule: shakers are NEVER active during any `measure` call.

### 0.4–0.6 Level matching

**Single sub:** skip to 0.7.

**Multiple subs:**

0.4 Measure each sub solo:
  For each sub output:
  1. Mute all other sub outputs
  2. `measure(label="sub_{N}-solo-level", position="MLP")` — note `session_id`
  3. Record peak SPL from FR data
  4. Unmute

0.5 Compare levels. Loudest sub is the reference (trim = 0 dB). For each
  quieter sub: `trim_db = reference_spl - measured_spl`.

  If any trim > 10 dB: STOP. Suggest turning up the quieter sub's volume knob.
  Wait for user confirmation.

0.6 Apply trims via `set_output_gain`. Loudest sub stays at 0.

### 0.7 Calibrate sweep level

`calibrate_level()` — finds optimal sweep volume with good SNR. Two sweeps:
probe + verify.

## Phase 1 — Alignment

**Single sub:** skip this phase, proceed to Phase 2.

FIR does not fix gross time alignment — alignment is pure geometry (speed of
sound × path difference). Do this first so per-sub FIR design in Phase 2 sees
an already-aligned sub.

### 1.1 Measure each sub solo

For each sub:
1. Mute all other subs
2. `measure(label="sub_{N}-solo-align", position="MLP")` — note `session_id`
3. `analyze_ir(session_id)` → `peak_time_s`, `peak_sign`, `spl_db`
4. Unmute

`peak_time_s` is the sub→mic travel time recovered from a bandlimited
(30–150 Hz) Hilbert-envelope cross-correlation peak. Differential
`peak_time_s` between subs is a valid first-pass delay estimate but
carries ~0.5–1 ms residual bias from the bandpass group delay. Use
`compare_sub_phase.delay_estimate` (Phase 1.2) as the primary source —
its phase-slope fit is unbiased.

### 1.2 Optimize alignment (primary tool)

`optimize_sub_alignment(session_ids=[solo_session_ids], min_hz, max_hz)` —
MSO-style numerical search. Pass every solo-sub session ID; the tool
returns per-sub recommended `{delay_ms, gain_db, polarity_inverted}`
minimizing predicted combined-FR error against the per-frequency
ceiling (= what the subs would produce if they summed perfectly).

Trust when `improvement_db > 1` AND `converged: true`. If `improvement_db`
is small, the subs are already aligned — accept the recommendation
anyway (it's the best achievable at this geometry) or move on.

Pass a `target_curve` to optimize against e.g. a cinema-bass curve
instead of the flatness-ceiling default. Scales to N subs.

`compare_sub_phase` is still useful as a DIAGNOSTIC to understand
*where* subs fight (the per-band `classification`: reinforcing /
cancelling), but it is NOT the alignment primary — the phase-slope fit
embedded there fails in strongly modal rooms where the room's modal
structure dominates phase at low frequencies.

### 1.3 Apply the recommendations

For each per-sub record from Phase 1.2:

```
set_delay(target=<transducer-name>, delay_ms=<rec.delay_ms>)
set_output_gain(target=<transducer-name>, gain_db=<rec.gain_db>)  # if nonzero
set_polarity(target=<transducer-name>, inverted=<rec.polarity_inverted>)  # if changed
```

**Describe every hardware action explicitly.** Example:
"optimize_sub_alignment recommends sub_nearfield delay=9.3 ms,
gain=+0.5 dB, polarity=normal; sub_front_right delay=0 ms, gain=0 dB,
polarity=normal. Applying these to align the two subs at MLP."

### 1.4 Polarity test (measurement-based)

Do NOT trust `peak_sign` alone — room reflections can flip it.

1. Unmute all subs. `measure(label="combined-pol-normal")`.
2. Flip polarity on the non-reference sub: `set_polarity(output_index=N, inverted=true)`.
3. `measure(label="combined-pol-flipped")`.
4. `compare_sessions(normal_id, flipped_id)` — keep whichever has higher
   combined SPL in the bass range.
5. If difference < 1 dB: keep normal (simpler). Restore the losing setting.

### 1.5 Verify alignment

Measure combined. Should be louder than any individual sub across most of the
target band (reinforcement). If combined is quieter than solo at some
frequencies → subs still cancel. Revisit using `compare_sub_phase`.

Max 3 alignment iterations.

## Phase 2 — Per-sub correction FIR

For each sub, design a minimum-phase FIR that flattens that sub's solo response
across the target band. The goal is "each sub measures flat at the MLP ± 2 dB
in its bass range."

### 2.1 Measure each sub solo (post-alignment)

For each sub:
1. Mute all other subs
2. `measure(label="sub_{N}-solo-prefir", position="MLP")` — note `session_id`
3. `analyze_phase(session_id)` — per-band fixability. Which bands are
   minimum-phase (FIR can correct) vs excess-phase (can't)?
4. `analyze_decay(session_id)` — T60 per mode
5. Unmute

### 2.2 Design the FIR

For each sub:

1. Call `design_fir` with:
   - `session_id` = the sub's solo measurement from 2.1
   - `phase_mode="minimum"` (default — no pre-ring)
   - `num_taps=8192` (default; bump to 16384 if T60 > 600 ms on a major mode)
   - `freq_focus_hz=[port_tune+3, crossover+10]` — e.g. `[25, 90]`
   - **Do NOT pass `target_curve`** — omitting defaults to flat correction,
     which is what we want for Phase 2

2. Inspect the returned predicted magnitude response:
   - Max boost below `port_tune+3` must be ≤ +6 dB. If exceeded, raise the
     lower bound of `freq_focus_hz` by 1 Hz and retry.
   - Max boost in target band must be ≤ +8 dB. If exceeded, reduce `num_taps`
     or accept a softer correction.
   - Check pre-ringing estimate (returned by `design_fir`). For minimum-phase
     it should be effectively zero.

3. For bands marked `fixable=False` by `analyze_phase` (cancellation nulls):
   the FIR will try and fail to correct them. This is unavoidable in one pass,
   but the recipe must note these bands in the retrospective as unfixable.

### 2.3 Apply the FIR

1. `apply_fir(output_index=N, coefficients=[...])` with the designed FIR
2. Describe the action: "Applying 8192-tap minimum-phase FIR to output {N}
   ({sub_name}). Predicted effect: flatten modal peak at 47 Hz (-5 dB), cut
   mode at 76 Hz (-3 dB), no boost below 25 Hz. Tapers to passthrough outside
   25–90 Hz."

### 2.4 Verify per-sub

For each sub:
1. Mute other subs
2. `measure(label="sub_{N}-solo-postfir")` — note `session_id`
3. Pull full-res FR: `get_measurement_history(format="compact", min_hz=20, max_hz=120, limit=1)` for this session
4. Compute variance across target band. Target: **flat within ± 2 dB**
5. `analyze_decay(session_id)` — compare T60 vs the pre-FIR measurement.
   Min-phase FIR reduces modal peak magnitude, which indirectly shortens T60
   somewhat. If a targeted mode's T60 is still > 500 ms after this pass,
   consider mixed-phase FIR for that sub (iteration — see 2.5).
6. Unmute

### 2.5 Iterate if needed

If a sub's post-FIR response has residual peaks > 2 dB:
1. Re-design FIR against the **post-FIR** solo measurement (the new filter
   corrects what's left, composing additively in the room)
2. BUT: the `apply_fir` call REPLACES the previous FIR entirely — so the
   new design must target the ORIGINAL pre-FIR measurement with tighter
   focus, or design against post-FIR and then convolve with the prior FIR
3. Simplest path: redesign against ORIGINAL pre-FIR measurement with updated
   parameters (e.g. bump to 16384 taps), reapply
4. Max 3 iterations per sub. After 3, document residual peaks in the
   retrospective.

### 2.5a Mixed-phase decision point — REQUIRED, do not skip

For **each sub**, after applying its min-phase FIR (Phase 2.3) and taking its
post-FIR solo measurement (Phase 2.4):

1. **MANDATORY tool call:** `recommend_fir_phase(session_id=<sub_solo_postfir>)`.
   Evaluates T60 and peak prominence against the recipe's thresholds and
   returns `recommendation: "minimum" | "mixed"`, `offending_modes`, and
   `suggested_num_taps`. Do not eyeball `analyze_decay` and decide yourself;
   call the tool. If the tool is unavailable (older container), abort and
   surface the version mismatch to the user.
2. **If `recommendation == "mixed"`:** DO NOT proceed to Phase 2.6 or Phase 3.
   - Re-run `design_fir(session_id=<pre_FIR_solo>, phase_mode="mixed",
     num_taps=<suggested_num_taps>,
     preringing_ms=<suggested_preringing_ms>,
     return_coefficients=false)` for this sub. The tool's
     `suggested_preringing_ms` (default 25 ms) bounds the filter's added
     audio latency AND the psychoacoustic pre-ringing window — below
     ~100 Hz the ear integrates over 20–30 ms so the pre-ringing is
     inaudible. The `fits_in_budget` flag tells you whether the pre-ringing
     stays within the AVR's Audio-Delay compensation range.
   - Apply via `apply_fir(output_index=N, design_session_id=<pre_FIR_solo>)`.
   - **Set the AVR's Audio-Delay / lip-sync** to match the design's reported
     `latency_ms` so video stays in sync with the bass. On Denon X-series
     this is under Menu → Audio → Audio Delay.
   - Re-measure that sub solo.
   - Re-run `recommend_fir_phase` on the new post-mixed-FIR measurement. If
     it still returns "mixed" on the second pass, document the residual,
     recommend bass-trap placement for the offending frequency, and proceed.
     Two mixed-phase attempts is the max per sub.
3. **If `recommendation == "minimum"`:** log the tool's `note` field in your
   iteration record and proceed to Phase 2.6.
4. Every `save_calibration_iteration` call for Phase 2 MUST include the
   tool's `recommendation` and `offending_modes` in the `filters_proposed`
   payload, so post-hoc audits can verify the check was executed.

**Why this is a tool, not prose:** min-phase FIR flattens magnitude but does
not cancel decay; mixed-phase FIR actively cancels decay in exchange for a
small amount of pre-ringing, inaudible below ~100 Hz. In run 14 the driving
LLM skipped this step, left a 46.9 Hz mode at 2037 ms T60, and moved to
Phase 3. Turning the decision into a single structured tool result is
harder to silently omit than a free-form paragraph.

### 2.6 Combined verification

After all subs have FIR applied:
1. Unmute all subs
2. `measure(label="combined-postfir", position="MLP")` — note `session_id`.
   **This is the reference measurement for Phase 3.**
3. Compare to the pre-alignment combined (Phase 1.5) — combined RMS should
   be flatter across the target band.

## Phase 3 — Target curve (Input PEQ)

Shape the combined, FIR-corrected response to the user's target curve using
the shared input PEQ.

Why PEQ instead of FIR here: target curves (Harman, flat, house) are smooth
low-order shapes. Biquad PEQ handles them with 3-5 filters. FIR would use
thousands of taps for the same result. Using PEQ also makes target-curve
swap cheap: redesign a few biquads, leave the per-sub FIRs untouched.

### 3.1 Baseline: HPF-only input

1. Clear input PEQ to HPF only: `apply_input_eq([{type: "hpf", freq: 18, gain_db: 0, q: 0.707}])`
2. `measure(label="combined-baseline-hpfonly", position="MLP")` — note `session_id`
3. **This is the session all Phase 3 simulations target.**

### 3.2 Anchor the target curve

Compute the optimal reference SPL for the target curve against the baseline.

Algorithm:
1. Pull full-res FR: `get_measurement_history(format="compact", min_hz=20, max_hz=120)` for the baseline session
2. For each frequency in the target range:
   - Interpolate the target offset at this frequency
   - `required_boost(f) = offset(f) + ref - measured_spl(f)`
3. Constraint: `max(required_boost) <= 6 dB` (SafetyValidator max boost)
4. So: `ref = min(measured_spl(f) - offset(f)) + 6` across all f in the range
5. Exclude from this calculation:
   - Frequencies where measured SPL > 15 dB below band average (nulls — unfixable)
   - Frequencies below `port_tune + 3` Hz (rolloff — unfixable)

Report the chosen reference level and the resulting max boost needed.

### 3.3 Design input PEQ

Input PEQ slots from `eq_capabilities.input_peq.available_slots` — typically 8.
Always reserve slot 1 for the HPF. Target curves usually need 3-5 filters for
the curve shape + 0-2 for residual room-mode cleanup.

Design the filter set:

1. Always include 18 Hz 4th-order HPF.
2. Compute the error curve: `error(f) = (ref + offset(f)) - measured(f)` at
   full resolution across the target band.
3. Decompose the error into a small set of biquad shapes:
   - **Low shelf** around the target's bass-rise knee (e.g. Harman's shelf
     peaks near 40–50 Hz)
   - **Peaking filters** for any residual room modes still protruding through
     the per-sub FIR (these should be small — FIR already handled most)
   - For each filter, call `optimize_q(session, freq_hz, target_gain_db)` to
     find the Q that minimizes residual error in the filter's band
4. **Simulate before applying:** `simulate_eq(baseline_session, filters)`.
   Inspect predicted response. Iterate design in simulation until satisfied.

Use `compute_deviation(baseline_session, target_curve)` to see RMS error
before vs after simulation.

### 3.4 Apply and iterate

For each iteration:

**Step A — Apply:**
1. `apply_input_eq(filters, simulation_verified=true, target_curve={type, reference_spl, band, points})`
2. Describe the action: "Applying input PEQ: 18 Hz HPF + low shelf at 45 Hz
   (+3 dB, Q 0.8) for Harman bass rise + cut at 62 Hz (-2 dB, Q 3) for
   residual combined peak. Predicted RMS: 0.9 dB."

**Step B — Measure:**
1. `measure(label="combined-iter{N}", position="MLP", target_curve=...)` — pass
   target_curve so the dashboard shows the delta

**Step C — Check convergence:**
1. `compute_deviation(session, target_curve, resolution="sixth_octave", convergence_threshold=1.5)`
2. If converged → proceed to Phase 4
3. If not: audit filters (see Step D), redesign, iterate. Max 3 iterations.

**Step D — Filter audit (before designing new corrections):**
1. For each existing filter: simulate the set with this filter removed. If
   RMS improvement < 0.3 dB without it, the filter isn't pulling its weight —
   drop it and free the slot.
2. For remaining filters: did the measured response shift at this filter's
   frequency? If so, re-optimize gain and Q against the latest measurement.
3. Then design new filters for remaining error peaks.

## Phase 4 — Retrospective

Always run this phase, even if calibration converged perfectly.

### 4.1 Before/after scorecard

Compare the earliest combined measurement (Phase 1.5 or pre-reset) and the
final measurement. Use `compare_sessions` and `compute_deviation` on both.

```
                         Before           After            Delta
RMS deviation:           X.X dB     →     X.X dB           -X.X dB
Worst peak:              +X dB @YYHz →    +X dB @YYHz      -X dB
Worst null:              -X dB @YYHz →    -X dB @YYHz      (unfixable)
Sub alignment:           X.Xms apart →    0.0ms            aligned
FIR taps/sub:            0          →     X,XXX (min-phase)
Input PEQ slots:         0/8        →     X/8
Convergence:             N/A        →     YES/NO (X.X dB RMS)
T60 on major modes:      before →   after (per mode)
```

### 4.2 Unfixable problems

For each band where `analyze_phase` flagged `fixable=False` or where the
recipe couldn't converge:

**Sub placement:**
- Identify nulls that FIR couldn't address — these are cancellation
- Use `compare_sub_phase` to identify which sub contributes more to each null
- Recommend specific moves: corner coupling, wall-midpoint avoidance
- Suggest measuring at 2-3 candidate positions before permanent placement

**Room treatment:**
- Review `analyze_decay` for modes with T60 > 500 ms (even after FIR)
- Recommend bass traps prioritized by audibility (SPL × T60)
- Corner placement for membrane traps, wall placement for porous absorbers

**Rattle detection:**
- Narrow coherence drops at specific frequencies = mechanical resonance
- Broad low coherence = ambient noise
- Recommend checking loose objects, ductwork, thin panels

### 4.3 Next steps

Numbered list, ordered by expected impact:
1. Physical changes (sub placement, room treatment, rattle fixes)
2. EQ refinements (different target curve, tighter convergence threshold,
   try mixed-phase FIR on stubborn modes)
3. Re-run calibration after changes

### 4.4 Save FIR coefficients for target swap

For later use (target curve swap):
1. For each sub, record the applied FIR in the calibration run metadata via
   `update_calibration_run` (coefficients, num_taps, phase_mode, freq_focus_hz,
   solo_session_id used for design)
2. Tell the user: "To swap target curves later, run `bass-calibration-fir-retarget`
   (or Phase 3 of this recipe only) using the stored per-sub FIRs. The FIRs
   stay in place; only the input PEQ changes."

## Phase 5 — Cleanup

Always run ALL of these, even on failure or abort.

1. `unmute_output` every output that was muted during calibration (especially
   shakers — hard rule: restore normal listening state).
2. `end_sweep_session` — restores DSP source to pre-calibration state.
3. `set_master_gain(0)` — `calibrate_level` may have dropped this to -30 dB
   or lower. If you skip this, the user sits down to silence.
4. If `playback_route` or `active_input` was switched mid-run, restore via
   `set_config` + `configure_matrix`.
5. `get_device_state` — verify master_gain=0, source=Analog (or user preference),
   no outputs muted. Report the final state to the user.

## Convergence

| Criterion | Threshold | Tool |
|-----------|-----------|------|
| Level match | All subs within 3 dB before digital trim | Phase 0 solo measurements |
| Alignment | Combined SPL > any solo SPL (reinforcement) | `compare_sessions` |
| Per-sub flatness | Solo FR variance < ± 2 dB in target band (post-FIR) | Phase 2 measurement |
| Target curve | RMS deviation < configured threshold (default 1.5 dB) | `compute_deviation` |

`compute_deviation` automatically excludes:
- Null zones (> 15 dB below band average)
- Below-port rolloff (< `port_tune + 3` Hz)

## When convergence fails

- **Per-sub FIR won't flatten a sub within ± 2 dB:** check `analyze_phase` for
  `fixable=False` bands. If the residual peaks are in cancellation zones,
  FIR cannot fix them — document in retrospective and recommend sub moves.
- **Input PEQ won't converge to target within 1.5 dB RMS:** the curve shape
  may require more slots than available, or the combined response still has
  strong room-mode interaction that per-sub FIR didn't fully flatten. Options:
  1. Return to Phase 2, try mixed-phase FIR on the stubborn sub
  2. Loosen convergence threshold to 2.0 dB
  3. Recommend room treatment + re-cal

## Target curve swap (sub-recipe)

A user who has completed a full run of this recipe and wants to try a different
target curve can run Phase 3 only:

1. Retrieve the most recent combined-baseline-hpfonly measurement from the
   stored run (or re-measure if stale — combined with all per-sub FIRs active
   and input PEQ cleared to HPF only)
2. Re-anchor against the new target curve
3. Design new input PEQ
4. Apply, measure, verify

Per-sub FIRs stay in place. Cleanup phase still required.

A future recipe file `bass-calibration-fir-retarget.md` can wrap this as its
own workflow for convenience.

## Safety notes

**FIR magnitude IS now code-enforced by `SafetyValidator.validate_fir`.**
Every `apply_fir` call runs the FIR's magnitude response through the FFT
and rejects coefficients that exceed:
- +`max_boost_per_band_db` below `min_boost_freq_hz` (port protection;
  +6 dB below 25 Hz for the SVS PB12-NSD profile)
- +`max_boost_above_threshold_db` above the port-tune floor up through
  200 Hz (+8 dB thermal ceiling for the SVS profile)

The recipe's Phase 2.2 design-time inspection is therefore belt-and-braces:
the validator is the sole non-bypassable guardrail, but checking the
designed FIR's magnitude before `apply_fir` still helps the LLM iterate
without bouncing off the driver-level rejection. Keep checking:
- No boost > +6 dB below 25 Hz (port protection)
- No boost > +8 dB above 25 Hz (thermal)
- Minimum-phase default (no pre-ring)

**Shakers muted for every `measure` call.** This is a project-wide hard rule,
not specific to this recipe.

**Cuts are always safe.** Boost is the only thing constrained. If in doubt,
accept a less perfect flattening rather than pushing boost close to limits.

## MCP tools used

### Hardware I/O
- `check_system` — pre-flight hardware verification
- `measure` — take a sweep measurement
- `apply_fir` — write FIR coefficients to a DSP output
- `clear_fir` — reset an output's FIR to passthrough
- `apply_eq` — write per-sub PEQ (used only for HPF in this recipe)
- `apply_input_eq` — write shared target-curve PEQ
- `mute_output` / `unmute_output` — isolate subs for solo measurement
- `set_delay` / `set_polarity` / `set_output_gain` — sub alignment and trim
- `set_volume` / `set_master_gain` — sweep-level control
- `calibrate_level` — auto-find sweep volume
- `configure_matrix` — route input to outputs

### Analytics (data for LLM judgment)
- `analyze_phase` — per-band fixability (minimum vs excess phase)
- `compare_sub_phase` — phase relationship between solo subs
- `analyze_ir` — IR peak time, polarity sign, SPL for alignment
- `analyze_decay` — T60 per mode
- `compute_deviation` — RMS deviation from target with null/rolloff exclusion

### Simulation (verify before applying)
- `design_fir` — compute FIR coefficients (minimum / mixed phase)
- `simulate_eq` — predict FR after proposed PEQ filters
- `optimize_q` — find best Q for a biquad at a given frequency and gain

### State and run tracking
- `get_config` — discover output slots, EQ capabilities, mic config
- `get_signal_graph` — named scope resolution (transducer / group / role)
- `get_output_state` — per-output gain, delay, polarity, FIR tap count
- `get_measurement_history` — FR data (always `format="compact"` with `min_hz`/`max_hz`)
- `compare_sessions` — per-band delta between two measurements
- `save_calibration_run` / `save_calibration_iteration` / `update_calibration_run`

## Recommended code changes

These would improve the recipe's effectiveness. Not required to run.

### SafetyValidator for FIR — **DONE**

`SafetyValidator.validate_fir(coefficients, sample_rate, profile)` now runs
before every `apply_fir` (both CamillaDSP and miniDSP drivers). Unsafe FIRs
raise `SafetyValidationError` → surfaced as `DriverError` → returned as
`{ok: false, error: "SafetyValidator: ..."}` by the MCP tool. The Phase 2.2
magnitude check in this recipe is now belt-and-braces, not the sole guardrail.

### FIR composition tool

For future recipes that want to truly decouple per-sub flattening from
target shape (without moving target to PEQ), add:

`compose_fir(fir_a: list[float], fir_b: list[float]) -> list[float]`

Simple numpy convolution. Would let a recipe keep per-sub flatten FIRs
cached separately and convolve with a fresh target FIR at apply time,
instead of the current approach where target lives in input PEQ.
