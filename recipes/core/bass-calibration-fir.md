# Recipe: Bass Calibration (FIR-Native)
version: 1.2

## Compliance — READ FIRST

**Execute every step in order. Do not skip checkpoints.** Past LLM runs
that skipped steps left audible regressions (e.g. run 14 skipped Phase 2.5
and left a 46.9 Hz mode at 2037 ms T60).

1. **No silent skips.** If a step is N/A (single-sub: Phase 1 alignment),
   log it explicitly. "I assumed it wasn't needed" is not acceptable.
2. **Execute every decision point.** Every "Decision point:" heading is
   a required branch — evaluate, log, take the branch the data dictates.
3. **Ask before shortcutting.** Don't rewrite the recipe under fatigue
   or pressure to finish.
4. **Every `analyze_decay`/`analyze_phase` flag requires follow-up** —
   either filter redesign, documented "unfixable" classification, or
   user override. Don't see the flag and move on.
5. **Bookkeeping is mandatory:**
   - At start, `run_id` must be in scope (set by `/avr:calibrate`).
   - After EVERY iteration (every phase, every loop pass), call
     `save_calibration_iteration(run_id, iteration=N, rms_before=...,
     rms_after=..., filters_proposed=..., filters_applied=...,
     safety_ok=...)`. `filters_proposed` MUST include the recipe step.
   - On early exit FOR ANY REASON, call
     `update_calibration_run(run_id, converged=..., final_rms=...,
     iterations_run=N, error=...)` BEFORE Phase 5 cleanup or telling
     the user "done."

If the recipe feels wrong for your hardware, surface the disagreement
to the user and amend — don't silently deviate.

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

## Sanity preflight — DO NOT SKIP

Three failure modes silently corrupted months of calibration work before
v0.6.8.x. Each one produces measurements that look plausible but are based
on a broken signal chain. If any of these checks fail, **stop and fix
before measuring anything** — you'll be optimizing noise.

### 1. Confirm the sweep is going where you think

`set_cal_mode(true)` then `mute_output` both subs and run `measure(label="silence-test")`.
Expected: the measurement fails with `"Sweep not detected in recording (cross-correlation peak too low)"`. That failure means the DSP path is silenced
correctly; the mic captured no signal correlated with the sweep.

If the measurement succeeds with normal-looking data when both subs are
muted, the sweep is reaching the mic by some other path (HDMI to AVR,
PipeWire mirroring, etc.). Stop. Diagnose:
- `playback_route` MUST be `"usb"` for cal-mode bypass to engage. `hdmi`
  routes through the AVR regardless of `cal_mode_active`.
- `check_system` includes an `Audio stack` check that flags PipeWire/
  wireplumber/PulseAudio holding `/dev/snd` handles. Disable them on the
  host: `systemctl --user mask pipewire wireplumber pipewire-pulse`.
- Verify the cal-mode loopback resolves to the right PortAudio device —
  `_resolve_alsa_device_in_portaudio` reads `/proc/asound/cards` to map
  `hw:Loopback,0,0` → the actual ALSA card index. If this fails, cal-mode
  silently falls back to system default (HDMI on a Pi).

### 2. Trust coherence as the data quality signal

The post-v0.6.8.2 coherence metric is per-bin SNR (not Welch — Welch is
invalid on non-stationary sweeps). Reading rules:
- ≥ **0.95**: gold standard. Optimize against this freely.
- **0.7 – 0.95**: usable, but expect ±1-2 dB jitter run-to-run. Don't fit
  precise filters here.
- **0.3 – 0.7**: marginal. Use group delay rather than IR peak for timing
  in this band; don't trust SPL deltas under ~3 dB as meaningful.
- **< 0.3**: noise. **Do not optimize against this band.** A 10 dB SPL
  swing here is invisible to the ear and reflects ambient noise, not
  acoustic cancellation. Most common at 20-25 Hz when the sub is below
  port tuning or sitting in a deep null.

A 3-second sweep is enough for ≥0.95 coherence at 31.5 Hz and up on the
USB-direct cal-mode path. Bump to 5-10s only when chasing 20 Hz
specifically. Longer sweeps don't help when the bottleneck is geometry,
not SNR.

### 3. IR peak time can lie when a room mode dominates

When the listening position sits in a destructive-cancellation null for
one sub, the direct arrival is heavily attenuated and the room-mode
resonance that follows is the largest |IR| feature. Naive `argmax(|ir|)`
locks onto the resonance time (e.g. 167 ms) instead of the
time-of-flight (e.g. ~5 ms). The post-v0.6.8.4 onset detector handles
the typical case (find first sample ≥ −20 dB from peak) but **it
cannot recover the direct arrival when it's buried 30+ dB below the
resonance**. If two solo subs in the same room appear ≥30 ms apart at
MLP, the geometry of one is masking direct arrival — physically move
the sub or use group delay at coherent frequencies (50-100 Hz) for
alignment, not IR peak time.

### 4. Sub placement beats every software lever

If the combined response at MLP shows a 20+ dB null at 25-31 Hz with
high coherence, that's not an EQ problem — it's destructive
interference between the two subs at the listening seat. EQ cannot
fill a null; it just dumps power into the room without changing the
cancellation at MLP. The fixes are physical:
- Sub crawl (move one sub by 1-2 ft and re-measure)
- Move MLP by 6-12 inches
- Add a third sub (distributed bass array)

Note this in the run log if the geometry is the bottleneck. Don't waste
filter slots trying to compensate.

## Filter strategy — REASON, DON'T PRESCRIBE

The recipe deliberately does NOT prescribe a fixed pipeline of "FIR for X, PEQ
for Y." After the alignment phase, you have rich data (per-sub solo FRs,
modal decay, phase fixability, coherence, combined response). Use it to
**design a filter plan tailored to this room**, considering all the tools
available and what we are optimizing for.

**The DSP pipeline:**
```
Input → Input PEQ → Routing → Per-output PEQ → Per-output FIR → DAC
```

Every layer above is available. Pick where each correction lives based on:

**Tool capabilities:**

| Tool | Strengths | Weaknesses |
|------|-----------|------------|
| Per-output FIR (`design_fir`, `apply_fir`) | High-resolution magnitude shape; cuts and boosts at exact frequencies; per-sub differentiation | Auto-anchors target_curve at band mean — aggressive shapes need explicit absolute target points |
| Per-output modal FIR (`design_modal_fir`) | Active anti-pulse T60 cancellation | Adds pre-ring latency; transient-aware safety still bounded |
| Per-output PEQ (`apply_eq`) | Cheap modal cuts, HPF | Limited slot count; can't shorten T60 |
| Input PEQ (`apply_input_eq`) | Single shared shape stage; biquad shelves at low slot cost | All-output broadcast — can't shape per-sub |
| Master gain | Restores level lost to cuts | Single number |

**Constants you must factor in (not optional):**

1. **Cuts work; boosts don't.** PEQ cuts ≈ 100 % at the listener; PEQ boosts
   25-50 %. FIR magnitude cuts ≈ 100 %. FIR magnitude boosts hit the same
   thermal/excursion ceiling as PEQ (`+6 dB below port_tune+3`, `+8 dB above`,
   code-enforced).
2. **Modal T60 cannot be cut down by magnitude EQ.** Cutting at the source
   reduces the energy that excites the mode but the room rings on. Only
   anti-pulse FIR or physical bass traps shorten T60.
3. **Anchoring drives the entire shape.** For sub calibration, anchor at the
   DEEP-BASS end of the band (e.g. 25-40 Hz) so the work is cuts above; never
   at the curve's nominal 0 dB at the crossover (that direction makes deep
   bass a boost target and runs into safety limits). See Phase 3.2.
4. **Tool auto-anchoring fights aggressive shapes.** `design_fir`'s
   `target_curve` parameter is interpreted relative to band-mean SPL — passing
   the raw harman offsets gives a *modest* slope. To get the FIR to actually
   carry a steep shape, pass absolute SPL target points anchored deep-bass
   (i.e. set the upper-bass target points well below current measured SPL so
   the only solution is a CUT).
5. **Per-sub differs from combined.** Per-sub FIR can flatten each sub
   individually, then a smaller shared layer adds the target shape — OR a
   per-sub FIR can deliver the per-sub share of the target shape directly.
   Different subs can be allowed to do different work (e.g. only the
   stronger-deep-bass sub carries deep bass; the other rolls off below 40 Hz).

**Build a plan, then execute. Phase 2 and Phase 3 below are sequence
guides, not filter recipes.** For your room:

1. Read the post-alignment combined and per-sub FRs (`get_measurement_history`,
   `analyze_phase`, `analyze_decay`, `compare_sub_phase`).
2. Determine the **anchor frequency** (Phase 3.2 algorithm). The anchor
   determines whether the work is mostly cuts (anchor low) or mostly boosts
   (anchor high — usually wrong for sub cal).
3. Decide where the **target-curve shape** lives:
   - Per-sub FIR (high resolution, per-sub differentiation, but aggressive
     shapes need explicit absolute SPL target points to override auto-anchor)
   - Input PEQ (cheap, easy target swaps)
   - Hybrid (FIR carries some shape + flattens per-sub; input PEQ adds
     residual shape and modal cuts)
4. Decide where **modal correction** lives:
   - `design_modal_fir` per sub: anti-pulse for ringy modes outside the
     priority deep-bass band; linear_notch for modes inside it.
   - PEQ cuts: cheap when the budget fits, but only reduce peak — don't
     shorten T60.
5. **Tap-count sizing.** Read `eq_capabilities.fir_max_taps_per_output` and
   `fir_sample_rate_hz`. Pick taps so the impulse window covers
   `2 × max(T60_ms)` of the modes you care about (bass T60s in this room
   are typically 400-800 ms → 800-1600 ms window → 38400-77000 taps at
   48 kHz). Smaller windows under-resolve deep modes; larger windows cost
   DSP CPU. State the choice and why.
6. **Simulate before applying.** `simulate_eq` for PEQ; design_fir's
   `predicted_effect` for FIR magnitude; cross-check against the
   1/3-octave coverage limits (predicted_effect smooths over narrow boosts
   the safety validator catches in finer bins).
7. **Verify after applying.** `verify_input_eq_effect`, `verify_fir_effect`,
   and a fresh combined measurement.

State the plan in the iteration log (`save_calibration_iteration`)
with `filters_proposed` containing your reasoning, not just the numbers.

**Why no fixed default.** Past recipe versions hard-coded "Phase 2 =
flat per-sub, Phase 3 = target via input PEQ" — this works for some
rooms and fails for others (aggressive target curves where input PEQ
boosts run out of budget, or modal-rich rooms where FIR's better
resolution is needed for the shape). Reason about which split fits
the room you actually measured.

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

**Apply the delays as returned — do not manually subtract a baseline.**
``optimize_sub_alignment`` already returns delays in MINIMUM-LATENCY form:
the trailing sub is anchored at 0 ms; leading subs receive only the
inter-sub delta. Adding a uniform offset would just push the entire sub
chain back in time, burning latency on the AVR's distance-push budget
without changing how the subs sum at the listening position. Inter-sub
alignment depends only on the delta between subs, not the absolute
offset; the tool enforces that contract.

**Apply the delays to the subs the tool indicates — do not swap based on
your own IR-peak interpretation.** The optimizer uses the full IR data
(not just the dominant-peak time) to predict combined response. In
geometries where one sub sits in a deep null at MLP, the IR's "peak" is
the room-mode resonance ringing AFTER the direct arrival, not the direct
arrival itself — so naive `argmax(|ir|)` reading gives a misleading sense
of which sub is "leading" vs "trailing". Empirical A/B (recal session
755 vs 751, 2026-04-28): swapping the optimizer's delay assignment
between subs gave essentially equivalent FR but cost 11 ms additional
sub-chain latency. **Trust the optimizer's literal output. Apply
`per_sub[i].delay_ms` to the sub identified by `per_sub[i].session_id`,
nothing else.**

**Polarity is canonical-form**: `optimize_sub_alignment` anchors sub_0
(the first session_id passed) to `polarity_inverted=false` because
absolute polarity is unobservable in sub-only optimization. Any sub
returned with `polarity_inverted=true` is flipped RELATIVE to sub_0
— that's the only acoustically meaningful signal the optimizer can
produce. Just apply what the tool returns.

**Describe every hardware action explicitly.** Example:
"optimize_sub_alignment recommends sub_nearfield delay=9.3 ms,
gain=+0.5 dB, polarity=normal; sub_front_right delay=0 ms, gain=0 dB,
polarity=normal. Applying these to align the two subs at MLP."

### 1.4 Polarity verification (measurement-based, per-band)

The optimizer's prediction uses linear summation of solo measurements,
which can miss real-room cancellation patterns at the listening
position. Verify by measurement, comparing **per band** — aggregate
SPL averages can hide narrow-band cancellation that's audible.

1. Apply optimizer's recommendation. `measure(label="combined-as-recommended")`.
2. Flip polarity on the non-reference sub. `measure(label="combined-pol-flipped")`.
3. **Per-band comparison** across at least three bands:
   - **20-40 Hz** (deep bass)
   - **40-80 Hz** (mid bass / punch)
   - **80-120 Hz** (upper bass / crossover region)
4. Decision rule:
   - If one polarity wins ALL bands → keep it.
   - If polarities split per band → **keep the one that wins 20-40 Hz**.
     Cancellation in the deep bass is unrecoverable; mid-bass loss is
     recoverable via PEQ/FIR.
   - If difference < 1 dB in every band → keep the optimizer's recommendation.

**Why per-band**: aggregate SPL can show "polarity A is better" while
hiding a 15+ dB cancellation in a narrow band (20-40 Hz). Real
example: a session with polarity-A winning aggregate SPL by 2 dB
while losing 16 dB at 31 Hz because of position-induced cancellation.
Aggregate test passed; user couldn't feel any deep bass.

### 1.5 Verify alignment — deep-bass priority + no major nulls

Two priorities, in this order:

1. **Maximize deep bass strength (20-40 Hz).** This is the band the user
   *feels*, where room cancellations are typically deepest, and where
   recovery via EQ is least possible. Optimization should prioritize
   keeping this band high before any other concern.

2. **No major narrow nulls anywhere in 20-100 Hz.** A "major null" is a
   1/3-octave band where the combined response drops more than 6 dB
   below the trend in adjacent bands, OR more than 3 dB below
   `max(solo_per_sub)`. Either signature indicates destructive
   interference at MLP. Mid-bass nulls (50-100 Hz) are partially
   recoverable via EQ; deep-bass nulls (20-40 Hz) are not — moving the
   sub or the listener is the only reliable fix.

Concretely, after applying the optimizer's recommendation:

1. Take a combined measurement.
2. **First check deep bass (20-40 Hz):** what's the minimum SPL in the
   1/3-octave bands at 20, 25, 31.5, 40 Hz? If any is more than 6 dB
   below the 50-80 Hz mid-bass trend, that's a deep-bass null. Try the
   polarity flip (Phase 1.4); if neither polarity recovers it, escalate
   to physical placement.
3. **Then check 50-100 Hz for narrow nulls.** If a single band drops
   more than 3 dB below the trend, flag as a candidate for FIR
   correction in Phase 2.

Bands above 100 Hz are informational only at this stage — the mains
will mask any sub imperfection there at the typical 80 Hz crossover.
Bands below 20 Hz are below port tuning for most subs and reflect
noise more than signal.

**Why deep-bass-priority instead of full-band RMS:** wideband flatness
optimization can leave a 10+ dB null at 30 Hz while reading "flat" on
RMS because the rest of the band offsets it. The null is the audible
problem ("subs not digging deep") even when RMS reads fine — see also
Phase 1.6.

If combined < max(solo) in any band:
- **First**: try the polarity verification (Phase 1.4) again — may
  reveal a sub-vs-sub polarity mismatch the optimizer missed.
- **Then**: consider sub repositioning (changes which frequencies
  cancel at MLP).
- **Last**: high-pass filter on the cancelling sub's chain at the
  band where it cancels — e.g. roll off the rear sub below 40 Hz so
  only the front sub carries the deep bass. Loses spatial averaging
  in that band, but eliminates the cancellation.

**Required deep-bass re-pass for 2+ subs.** Default `optimize_sub_alignment`
minimizes flatness across the full target band — that can leave a narrow
cancellation null in the boost zone (e.g. 30-40 Hz) because flatness
elsewhere offsets it on RMS. Single-call workflow:

```
optimize_sub_alignment(session_ids=[...], priority_band=[20, 50])
```

The `priority_band` argument weights that range 3× in the objective so
the optimizer attacks deep-bass nulls in one call. Inspect the
`per_band_polarity` field in the response — bands listed there indicate
per-band cancellations that would benefit from a polarity flip on a
specific sub.

**Then refine inter-sub delay with the automated sweep:**

```
sweep_inter_sub_delay(
    session_ids=[<sub_a>, <sub_b>],
    base_delays_ms=[<current>, <current>],
    priority_band=[28, 50],
    sweep_range_ms=2.0,
    step_ms=0.25,
)
```

The tool predicts deepest-null depth for each delay step and reports the
delay that shallowest the deepest 1/3-octave null in the priority band.
Apply via `set_delay(output_index=<trailing_sub>, delay_ms=<recommended>)`.

**After Phase 2** designs FIRs, re-run `sweep_inter_sub_delay` with
`step_ms=0.1` — the FIRs change phase response, so the optimal inter-sub
delay shifts slightly (typically ≤0.5 ms).

Max 3 alignment iterations total (wideband + deep-bass + post-FIR).

## Phase 1.95 — Build the calibration plan (NON-NEGOTIABLE)

Before touching `design_fir` / `design_modal_fir` / `apply_eq` / `apply_input_eq`
in Phase 2 or 3, **the LLM MUST emit a structured plan document and submit it
for adversarial review**. This phase exists because: in real runs the LLM has
made anchor-direction mistakes, mis-classified modal treatments, picked
tap counts without justification, and committed boost budgets that ended up
exceeding safety caps. A plan-then-review handoff catches these before any
hardware writes.

**The plan is not a free-form essay.** It's a structured document that
forces every decision to have a measurable justification.

### 1.95.1 Gather all available signals

Before drafting the plan, collect:

- Per-sub post-alignment solo FRs (from Phase 1)
- Combined post-alignment FR
- `analyze_phase` per band (fixability classification)
- `analyze_decay` per sub and combined (T60 + peak per mode)
- `compare_sub_phase` solo-A vs solo-B (per-band reinforcement classification)
- Coherence per band (already in measurement metadata)
- `eq_capabilities` (FIR taps, sample rate, PEQ slot counts)
- Target curve points + `anchor_target` analysis under `cuts_only` and
  `balanced` directions
- Safety profile from `get_signal_graph` (boost caps, port_tune, modal_cancel cap)

### 1.95.2 Emit the structured plan

Output a plan in this shape (YAML-flavored, embed in a code block):

```yaml
calibration_plan:
  target:
    curve: harman-in-room
    anchor:
      freq_hz: 25
      method: deep_bass_priority
      reasoning: |
        Lowest band where every adjacent has measured-relative ≥ target-relative
        gap (i.e., implementable via cuts). At anchor=25 Hz: 31 has +0.3 gap,
        40 has +3.9 gap, 50 has +6.7 gap. Anchor=31 also works but yields
        smaller cuts above; anchor=25 maximizes cut headroom.

  per_sub_strategy:
    sub_front_right:
      filter_type: design_fir   # or design_modal_fir
      target_curve_in_fir: true
      num_taps: 24576
      taps_reasoning: |
        max(T60) = 1163 ms at 47 Hz → impulse window ≥ 2× = 2326 ms.
        24576 taps @ 48 kHz = 512 ms window. SHORT of 2× requirement.
        Acceptable trade-off: 24576 matches 8 kHz processing rate alignment
        and DSP CPU budget. Document residual ringing as known limit.
      freq_focus_hz: [25, 100]
      anchor_mode: deep_bass_priority
      modal_treatment:           # only if using design_modal_fir
        47Hz: linear_notch
        70Hz: anti_pulse cancel_strength=0.6 bp_q=auto
        94Hz: anti_pulse cancel_strength=0.5 bp_q=auto
      modal_treatment_reasoning: |
        47 Hz IS in priority band 25-50 → linear_notch to avoid anti-pulse
        leakage into 25-31 (deep bass we want to PRESERVE). 70 Hz and 94 Hz
        are above the protected band → anti_pulse OK. cancel_strength
        will be auto-reduced if adjacent-band cap trips (commit 2d18420).
    sub_nearfield:
      ...

  combined_strategy:
    input_peq: hpf_only
    reasoning: |
      Per-sub FIRs carry both target shape AND modal correction. Combined
      response should land within achievable accuracy of the curve.
      Input PEQ would only stack with FIR cuts — no value-add unless a
      residual modal peak survives both per-sub FIRs.

  expected_residual:
    rms_db: 3.0
    threshold_chosen: 3.0
    reasoning: |
      Modal dilution feedback memory documents PEQ/FIR cuts deliver only
      20-30% at the listener in this room (T60 471-1163 ms at 47-94 Hz).
      Default 1.5 dB threshold not achievable without bass traps. 3.0 dB
      is realistic for DSP-only path.

  unfixable:
    - description: "20 Hz null"
      reason: "below SVS PB12-NSD port tune (22 Hz) + position-induced cancellation"
      action: "skip — document for retrospective"

  safety_budget_check:
    max_predicted_boost_per_band_db: 4.5    # below profile cap of +6 below 25
    max_predicted_boost_above_25_db: 7.2    # below profile cap of +8
    modal_cancel_predicted_boost_db: 18     # below profile cap of +20
    headroom_assessment: "all bands within budget; no expected safety rejections"

  next_steps:
    - "bass traps at 47/70/94 Hz to reduce T60 below 400ms (largest impact)"
    - "sub repositioning: try corner placement for FR sub to break mode coupling"
    - "re-cal after physical changes"
```

The plan MUST be specific. "Try anti-pulse maybe" is not a plan. "anti_pulse
on 70 Hz at cancel_strength=0.6 bp_q=auto, expected adjacent-band boost
+18 dB at 50 Hz (within +20 cap)" is a plan.

### 1.96 Adversarial review (NON-NEGOTIABLE)

Invoke the `Agent` tool with `subagent_type: general-purpose` to red-team
the plan. The reviewer prompt MUST include:

1. The full structured plan from 1.95.2
2. The relevant measurement session IDs (the agent can pull data via MCP
   `get_measurement_history`, `analyze_phase`, `analyze_decay`)
3. The `eq_capabilities` and safety profile
4. Explicit red-team checklist (below)

**Reviewer red-team checklist (all 8 must be addressed):**

1. **Anchor direction**: is anchor at the deep-bass end? If not, justify.
   For Harman-style curves in modal-rich rooms, anchoring high makes deep
   bass a boost target. Has the LLM checked both `anchor_target
   direction=cuts_only` and `direction=balanced`?

2. **Tap count**: is `num_taps` justified by `2 × max(T60_ms)` of the
   modes being treated? If not, what's the trade-off rationale?

3. **Modal treatment classification**: did the plan correctly identify
   modes in the priority band [25, 50] as `linear_notch` (not
   `anti_pulse` — leakage risk) and modes outside as `anti_pulse`? Did
   it consider mode density (47-70 are half-octave apart — Q affects
   leakage)?

4. **Boost budget**: are any predicted boosts within 1 dB of the safety
   cap (+6 below 25 Hz, +8 above, +20 modal_cancel)? If yes, what's
   the contingency if the FIR design overshoots predicted?

5. **Convergence threshold**: is the chosen RMS threshold realistic
   given the room's T60 and modal density? Or is it wishful thinking
   that will drive the LLM to iterate past convergence?

6. **Geometry-bound problems**: are there nulls (combined < max(solo) at
   some band) that EQ can't fix that the plan still allocates filter
   budget to?

7. **Tool sequencing**: does the plan respect `apply_fir` REPLACING (not
   stacking) the previous FIR? Does it leave a viable iteration path if
   first apply doesn't converge?

8. **Hidden assumptions**: does the plan rely on any room characteristic
   that wasn't actually measured (e.g., assumed sub T60 from a different
   session)?

The reviewer agent returns:

```yaml
review:
  overall: pass|fail
  concerns:
    - severity: high|medium|low
      checklist_item: 1-8
      issue: <specific concern>
      suggested_revision: <concrete change>
  recommended_action: proceed|revise_plan|escalate_to_user
```

### 1.97 Plan revision

If `overall == fail` OR any concern is `severity: high`:

1. Apply the suggested revisions to the plan.
2. Re-run 1.96 adversarial review against the revised plan.
3. Maximum 2 revision cycles. If still flagged after 2 revisions,
   escalate to the user with both the original plan, all reviewer
   feedback, and the revisions tried.

If `overall == pass` (or only `low/medium` concerns the LLM accepts with
documented rationale): proceed to Phase 2 executing THIS plan.

### Why plan-then-review

This phase looks like overhead. It catches the exact mistakes that cost
hours in prior runs:

- Anchored at 80 Hz when target needed deep-bass anchor → ran into safety
  cap on every iteration
- Picked num_taps=8192 without checking T60 → modal correction didn't
  resolve the modes
- Applied input PEQ -10 dB cuts without realizing modal dilution would
  cap effect at -2 dB → spent iterations chasing what physics wouldn't
  give
- Mis-classified 47 Hz as anti_pulse → leakage into 25 Hz hit safety

The reviewer agent is cheap (~30 sec). The mistakes it catches cost
hours each. Always run.

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

### 2.2 Design the FIR — flat per-sub + target shape via input PEQ (default)

Default architecture: per-sub FIRs flatten each sub's solo response
across the target band; input PEQ (Phase 3) shapes the combined
response to the chosen target. Pros: easy target-curve swaps (re-run
Phase 3 only), clean separation of "what the room does to each sub"
(FIR) from "what shape I want" (PEQ). The risk — per-sub FIR cuts at
modal peaks fighting input-PEQ shape boosts at adjacent frequencies
— is mitigated by Phase 3.3's "cuts first, boosts last" rule.

**Call `design_fir` for each sub:**

- `session_id` = the sub's solo measurement from 2.1
- `phase_mode="minimum"` (default for this architecture — flattens
  magnitude only, zero pre-ring). Use Phase 2.5a to upgrade to
  modal-aware (`design_modal_fir`) if T60 > 400 ms after this pass.
- `num_taps=8192` (bump to 16384 if T60 > 600 ms on a major mode)
- `freq_focus_hz=[port_tune+18, crossover+5]` — e.g. `[40, 85]`.
  Lower bound starts above the deepest-bass null because the FIR
  has limited boost headroom there; tighten +2 Hz on retry until
  max boost lands within safety profile.
- **No `target_curve`** — defaults to flat per-sub.

**Inspect the predicted magnitude response:**

- Max boost below `port_tune + 3 Hz` must be ≤ +6 dB. If exceeded,
  raise lower bound of `freq_focus_hz` by 1 Hz and retry.
- Max boost in target band must be ≤ +8 dB. If exceeded, reduce
  `num_taps` or accept softer correction.
- Pre-ring estimate should be near zero for `phase_mode="minimum"`.

**Bands marked `fixable=False` by `analyze_phase`** (cancellation
nulls): the FIR can't correct them. Note in the retrospective as
unfixable; do not waste FIR taps on them.

**Fallback architecture — FIR=target_shape + Input PEQ=narrow modal cuts.**
For asymmetric setups where per-sub flat correction conflicts with
target shape (rare), pass `target_curve` to `design_fir` so each sub
plays its share of the target shape directly. Input PEQ then becomes
HPF + narrow Q4-5 cuts at residual modes only. Used when subs differ
strongly in deep-bass capability and the target curve calls for an
aggressive bass shelf — the layered approach (FIR=flat + PEQ=shape)
asks the weak sub to boost beyond its safety limit. Most rooms don't
need this fallback; default to flat per-sub.

### 2.2a Modal-aware FIR (`design_modal_fir`) — when ringing dominates magnitude

When `analyze_decay` reports modes with `T60 > 500 ms` AND `peak > +6 dB`,
plain `design_fir` (magnitude correction only) cuts the peak but does **not**
shorten T60 — the mode still rings at reduced level. `design_modal_fir`
adds **active anti-pulse cancellation**: a band-limited pulse placed one
half-wavelength before the main impulse, opposite-signed, so the modal
ringing is destructively cancelled in the time domain.

**Use it when:**
- Combined or solo measurement has multiple modes with T60 > 500 ms
- Peak reduction alone (PEQ or `design_fir`) isn't enough — you want
  T60 reduction
- You have ≥10 ms of pre-ring latency budget (Audyssey distance push
  on mains works; UI-clamp at ~38 ms)

**Don't use it when:**
- Room T60s are already < 400 ms (anti-pulse adds latency for negligible
  gain — fall back to `design_fir` minimum-phase)
- Tight latency budget (< 5 ms — anti-pulse needs half-wavelength of
  pre-ring per mode, e.g. 7 ms at 70 Hz, 11 ms at 47 Hz)
- Modal frequency is in or near a band you cannot afford to lose
  magnitude in (the protected deep-bass band — see lesson below)

**Anti-pulse leakage and the 47 Hz lesson** (validated 2026-04-28):

Anti-pulses produce some spectral content outside their nominal modal
band. With `envelope="butterworth"` (legacy), a 47 Hz anti-pulse leaked
~12 dB into the 25 Hz band and forced sacrificing deep-bass priority.
With `envelope="gabor"` (default since v0.6.8.6, Gaussian-windowed
sinusoid) the same anti-pulse leaks **15 dB less** at 25 Hz. Gabor
should be the default; only use `butterworth` for regression A-B tests.

Even with Gabor, anti-pulse fundamentally adds energy at the mode
frequency itself. If the protected priority band overlaps the mode's
1/3-octave bin, prefer `linear_notch` (precise magnitude cut, no
spectral leakage) over `anti_pulse` for that mode.

**Per-mode treatments** (pass via `intents` for verbatim control, or
omit to auto-classify by T60+peak):

| treatment | what it does | when to pick |
|---|---|---|
| `anti_pulse` | half-wavelength inverted band-limited pulse → cancels T60 in time domain | T60 > target × 2, peak > 6 dB, mode is OUTSIDE protected priority band |
| `linear_notch` | symmetric magnitude cut at the mode | T60 < target/2 but loud peak; OR mode overlaps protected band |
| `min_phase` | gentle minimum-phase magnitude EQ | mild peaks/dips not strongly ringy |
| `skip` | no treatment | below port tune (< 25 Hz on PB12-NSD); above sub crossover; mains-driven modes |

**Per-mode parameters** (each intent dict accepts):
- `cancel_strength` (0–1, default 0.6): aggressiveness of the anti-pulse.
  Lower if SafetyValidator trips on adjacent-band boost.
- `bp_q` (default 1.5): bandpass Q on the anti-pulse envelope. Raise to
  3–5 if adjacent bands trip the thermal cap. Diminishing returns above
  Q ≈ 5 with Gabor envelope.
- `envelope` (default `"gabor"`): keep `"gabor"` unless A-B testing.

**Worked example (combined sub, two ringy modes, room with ~500 ms T60):**

```python
design_modal_fir(
    session_id=<combined_session>,
    intents=[
        # Below port tune — never anti_pulse.
        {"freq_hz": 23.4, "treatment": "skip"},
        # 47 Hz overlaps protected deep-bass priority — magnitude only.
        {"freq_hz": 46.9, "t60_ms": 443, "peak_db": 13.1,
         "treatment": "linear_notch"},
        # 70 Hz — well above protected band, ideal anti_pulse target.
        {"freq_hz": 70.3, "t60_ms": 482, "peak_db": 11.0,
         "treatment": "anti_pulse", "cancel_strength": 0.6, "bp_q": 3},
        # 94 Hz — also above protected band.
        {"freq_hz": 93.8, "t60_ms": 524, "peak_db": 6.6,
         "treatment": "anti_pulse", "cancel_strength": 0.5, "bp_q": 3},
        # Above sub crossover — mains handle.
        {"freq_hz": 117, "treatment": "skip"},
    ],
    max_pre_ring_ms=25,
    num_taps=4096,
)
```

Apply via `apply_fir(output_index=N, design_session_id=<session>)`
to each sub. Verify with a fresh combined measurement: T60 should
drop ~25–35 % at the targeted modes.

**Unified target + modal FIR (v0.6.8.7+).** ``design_modal_fir`` accepts a
``target_curve`` argument that adds a min-phase magnitude correction layer
in the same FIR. Use this when you want a single FIR to deliver both modal
T60 reduction AND target-curve shaping (e.g. Harman+4) without stacking
input PEQ on top — input PEQ shape can fight the anti-pulses, since the
PEQ is blind to the FIR's spectrum.

```python
design_modal_fir(
    session_id=<combined>,
    intents=[...],
    target_curve={
        "points": [{"freq": 25, "spl": 5}, {"freq": 31.5, "spl": 4},
                   {"freq": 40, "spl": 3}, {"freq": 50, "spl": 2},
                   {"freq": 63, "spl": 1}, {"freq": 80, "spl": 0}],
        "band": [20, 100],   # taper magnitude correction outside this band
    },
)
```

The session's third-octave SPL is the source FR; target − source − modal_fir
becomes the magnitude correction. Anchored to the 60–100 Hz midband so
absolute SPL drops out. Outside ``band`` the correction tapers to 0 dB.

**Modal-cancellation safety cap (v0.6.8.8+).** Anti-pulse modal cancellation
intentionally drives the driver hot at the mode frequency (the FIR's FFT
magnitude at the mode is meant to cancel the room mode at the listener,
so net SPL at MLP is unchanged but the driver receives the boosted level).
``SafetyValidator.validate_fir(intent="modal_cancel")`` admits up to
``profile.modal_cancel_max_boost_db`` (+20 dB on PB12-NSD). At typical
calibrated levels (master gain ~-25 to -15 dB) this is well within driver
xmax/thermal capacity. Generic FIRs and PEQ writes still respect the
strict ``max_boost_above_threshold_db`` cap. ``apply_fir`` reads the
cached design intent so coefficients from ``design_modal_fir`` use the
relaxed cap automatically.

### 2.2b Anti-pulse caveats — phase interference is real (2026-04-29 lessons)

Anti-pulse modal cancellation is **the only mechanism in this recipe that
shortens T60** — magnitude-only EQ (PEQ, ``design_fir``) reduces the peak
but the mode still rings at reduced level. The trade-off is that the
anti-pulse's time-domain phase rotation has consequences at adjacent
frequencies you can't predict from the FIR alone.

**Phase-interference is bidirectional and per-room:**

- **Helpful direction:** at frequencies where the room has a *natural
  cancellation null* (forward + reflected paths arriving 180° out of phase),
  the anti-pulse's phase rotation can shift one path toward in-phase →
  **null fills**. Empirically observed 2026-04-29: 80 Hz null at -10 dB
  → +3 dB (+13 dB lift) from a single 70 Hz anti-pulse, with no FIR
  magnitude boost at 80 Hz.
- **Harmful direction:** at frequencies where the room has natural modal
  reinforcement (forward + reflected paths arriving in-phase), the
  anti-pulse's phase rotation can shift toward 180° → **constructive
  reinforcement turns destructive**. Bug observed 2026-04-28 (since fixed):
  the 47 Hz anti-pulse rotated phase at 25 Hz, destroying 8 dB of deep
  bass at MLP without any visible FIR-magnitude cut at 25 Hz.

**You can't predict which way it goes without measuring.** The room's
arrival-path geometry is unknown. The recipe's right path is the
**empirical 2-step**:

1. Apply modal FIR.
2. Measure listener result.
3. If a null filled (lucky) — keep going.
4. If deep bass collapsed at adjacent frequencies — back off
   ``cancel_strength``, switch the offending mode to ``linear_notch`` or
   ``skip``, OR layer a magnitude-correction FIR on top to compensate.

**Concrete starting parameters for typical sub rooms** (validated
2026-04-29 on a +13 dB at 47 Hz, +11 dB at 70 Hz, +7 dB at 94 Hz room):

- Start with **a single anti_pulse on the most distant-from-deep-bass mode**
  (e.g. 70 Hz first, not 47 Hz). Measure.
- If 25-40 Hz dropped >2 dB at MLP, the phase interaction was harmful —
  reduce ``cancel_strength`` or use ``linear_notch``.
- If 25-40 Hz held, add a second anti_pulse (e.g. 94 Hz). Measure again.
- Avoid ≥3 simultaneous anti-pulses — the cumulative pre-ring energy
  saturates the IR onset detector and breaks T60 measurement (observed
  2026-04-29 session 768: IR peak detected at 1 ms instead of 130 ms;
  T60 numbers became unusable).

**Worked example, 2026-04-29 final state:**

- 1× anti-pulse on 70 Hz: ``cancel_strength=0.3, bp_q=3, envelope=gabor``
- 47 Hz, 94 Hz: ``treatment=skip`` (47 too close to deep bass; 94 destabilizes
  IR detection in combination)
- Result vs baseline: T60 at 70 Hz **-22 %** (467 → 362 ms), 80 Hz null
  **+13 dB filled** (-10 → +3), deep bass preserved (25 Hz: +0.3 → -0.1).

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

### 2.5a Decay-vs-magnitude decision

After Phase 2.4's post-FIR solo measurement, call
`recommend_fir_phase(session_id=<sub_solo_postfir>)`. It returns
`recommendation: "minimum" | "mixed"` based on T60 and peak prominence.

- **`"minimum"`**: proceed to Phase 2.6.
- **`"mixed"`**: the sub still has ringy modes the magnitude-only FIR can't
  fix. Switch to **`design_modal_fir`** (Phase 2.2a) instead of `design_fir`
  with `phase_mode="mixed"`. The modal-aware tool's anti-pulse cancellation
  is the right path; the mixed-phase magnitude FIR is the legacy approach
  that this recipe used pre-v0.6.8.5 and is left in for hardware that
  doesn't support modal_fir's Gabor pulses.

When the sub-chain FIR adds pre-ring latency, compensate by increasing the
**mains' per-channel SPEAKER DISTANCE** in the AVR (NOT the global Audio
Delay / lip-sync slider). For Denon X-series, the UI clamps distance at
~60 ft; if you need more, write directly via the Audyssey TCP protocol
(MultEQ-X / ratbuddyssey on port 1256 — the firmware accepts values past
the UI clamp).

Two passes max — if `recommend_fir_phase` keeps returning `"mixed"` after
two iterations, document the unfixable mode and recommend bass-trap
placement at that frequency.

### 2.6 Combined verification

After all subs have FIR applied:
1. Unmute all subs
2. `measure(label="combined-postfir", position="MLP")` — note `session_id`.
   **This is the reference measurement for Phase 3.**
3. Compare to the pre-alignment combined (Phase 1.5) — combined RMS should
   be flatter across the target band.

### 2.7 Diagnose room limits before Phase 3

Before designing target-curve EQ, identify which features in the combined
FR are EQ-fixable vs physical-only. Three categories of feature, and what
the LLM should do with each:

| Feature | EQ-fixable? | Action |
|---------|-------------|--------|
| **Modal peak with T60 < 300 ms** | Yes via PEQ cut | Cut at the mode in Phase 3 with high Q (4-6) |
| **Modal peak with T60 > 400 ms** | Partially via anti-pulse FIR | Re-run `design_modal_fir` (Phase 2.2a) with stronger `cancel_strength`. PEQ cuts at the source only kill new energy; the room rings on regardless and the 1/3-octave RMS captures the tail. |
| **Cancellation null (>5 dB dip vs adjacent bands)** | NOT fixable with PEQ | Skip it. PEQ boost at a phase null delivers <30 % of asked dB at the listener. Recommend sub repositioning instead. Document in retrospective. |
| **Below-port rolloff (< port_tune + 3 Hz)** | NOT fixable | Hard physical limit. Don't fight. |
| **Modal ringing dominating a 1/3-octave band** | Only modal FIR or bass traps | PEQ cuts at the source reduce input energy, but the room mode keeps ringing for T60 ms. Band-RMS measurement integrates the tail; cuts deliver only 1-2 dB at the listener even when the source-side cut is 6-8 dB. |

**Rule of thumb for modal-rich rooms:** if combined T60 across the modal
band averages > 400 ms, PEQ alone won't get you flat. Either push
modal-FIR cancellation harder (Phase 2.2a, stronger `cancel_strength`),
add physical bass traps, or pick a target curve that accepts the room's
character (Phase 3 curve selection).

## Phase 3 — Target curve (Input PEQ)

In the default architecture, Phase 2 left each sub flat per-solo. Phase
3 shapes the COMBINED response to the target curve via input PEQ —
biquads handle smooth low-order shapes (Harman, flat, house) with 3-5
filters; FIR would use thousands of taps for the same result.

**Skip Phase 3.2-3.4 if you used the fallback architecture** (FIR
carries target shape via `target_curve` in `design_fir`). In that case
Phase 3 is truncated to: clear input PEQ to HPF only, measure combined,
add narrow Q4-5 peaking cuts (typically -2 to -4 dB) at any modes still
visibly ringing, skip anchor/design/iterate. The target shape is
already in the FIRs.

### 3.1 Baseline: HPF-only input

1. Clear input PEQ to HPF only: `apply_input_eq([{type: "hpf", freq: 18, gain_db: 0, q: 0.707}])`
2. `measure(label="combined-baseline-hpfonly", position="MLP")` — note `session_id`
3. **This is the session all Phase 3 simulations target.**

### 3.2 Anchor the target curve — pick the right reference

**For sub calibration, ALWAYS anchor at the DEEP-BASS end of the band
(e.g. 25-40 Hz), NOT near the curve's 0 dB point at the crossover (80
Hz).** A sub cal exists to maximize bass; anchoring near the crossover
makes deep bass a boost target — and PEQ boosts deliver only 25-50% at
the listener in modal-rich rooms while running into safety caps. The
goal is for the MEASURED deep bass to BE the curve's high point. The
mid/upper bass then comes DOWN to the curve's falloff via cuts. Cuts
deliver ~100% at the listener and are unconstrained by safety. Master
gain (Phase 5 cleanup) restores the absolute level the cuts removed.

Three rules:

1. **Anchor low, not high.** Deep bass is the priority and the place
   the room can't physically be boosted. Anchor where the measured
   response is at-or-above the curve's local value, so the rest is cuts.
2. **Cuts work; boosts don't (in modal-rich rooms).** PEQ cuts deliver
   ~100% of their dB at the listener; boosts deliver 25-50%. Anchor
   such that ALL the work is cuts when possible.
3. **Pick the anchor empirically from the baseline measurement.** For
   each candidate anchor frequency f_anchor in the deep-bass band
   (typically 25-40 Hz), compute per-band gap = (measured[f] −
   measured[f_anchor]) − (target[f] − target[f_anchor]). The right
   anchor is the LOWEST-frequency candidate where every band above it
   has a non-negative gap (i.e. measured ≥ target relative to the
   anchor — implementable via cut). If no such candidate exists,
   accept the lowest-frequency anchor with the smallest single-band
   shortfall and accept some residual boost at that one band.

Concrete worked example, validated 2026-04-29 (Harman+4 in a modal
room with mid-bass hump):

| Anchor freq | Worst gap | Avg \|gap\| |
|-------------|-----------|-------------|
| 25 Hz (curve's max value) | +7.6 dB | 3.2 dB |
| **31 Hz (slightly off the max)** | **+4.1 dB** | **2.4 dB** |
| 80 Hz (curve's 0 dB / canonical) | similar to 25 Hz | ~3 dB |

The 25 Hz anchor was wrong because measured 25 Hz was 8 dB BELOW where
the curve says it should be (deep-bass rolloff + null at adjacent
bands). 31 Hz was naturally close to its target value, so anchoring
there gave most other bands smaller errors.

**Algorithm:**

1. Pull baseline FR (HPF-only state).
2. For each f_anchor in {25, 31, 40, 50, 63, 80}:
   - Compute the relative target shape: `target[f] − target[f_anchor]`
   - Compute the relative measured shape: `measured[f] − measured[f_anchor]`
   - For each band f, gap = relative_meas[f] − relative_target[f]
   - Score = max(|gap|) across the target band
3. Pick the f_anchor with the lowest score.
4. The reference SPL for the target_curve = measured[f_anchor] − target[f_anchor].

This makes cuts dominate the correction, side-stepping the boost-
inefficiency problem. Master gain (Phase 5 cleanup) compensates for
the level drop the cuts create — so the absolute SPL anchor is
decoupled from the curve fit.

### 3.3 Design input PEQ — cuts first, boosts last

Input PEQ slots from `eq_capabilities.input_peq.available_slots` —
typically 8. Always reserve slot 1 for the HPF. Most target curves
need 3-5 filters for shape + 1-2 for residual cleanup.

**Filter ordering by effectiveness in modal-rich rooms:**

| Type | Effectiveness at MLP | Use for |
|------|---------------------|---------|
| Peaking cut at modal peak (high Q 4-6) | Near 100% of dB cut | Trimming +X dB modal hot spots |
| Low/high shelf in non-modal range | 80-100% | Broad tilt toward curve shape |
| Peaking cut, broad Q (1-2) | Limited by modal ringing in band | Avoid — cut SOURCE energy but room rings on |
| **Peaking BOOST at any band** | **25-50%** | **Avoid wherever possible** |
| Boost at a phase null | <30% | Never (waste of headroom) |

**Design steps:**

1. Always include 18 Hz 4th-order HPF.
2. Compute residual = target_anchored − measured. Sort by sign:
   negative deltas → CUTS (cheap), positive deltas → BOOSTS (expensive).
3. Place high-Q (4-6) peaking cuts AT the modal peaks where measured is
   hot. Cut depth = the residual error there. These deliver near-100%.
4. Place broad shelves only when the curve genuinely calls for tilt
   (don't shelf-boost just to lift a band the room can't deliver).
5. **For boost candidates, ask: is this a phase null?** Check FR for
   a sharp dip vs surrounding bands. A null > 5 dB below average is
   NOT fillable with PEQ; skip it and accept the dip.
6. Simulate via `simulate_eq` before applying — its prediction will
   over-estimate boost effects. Cross-check with `verify_input_eq_effect`
   after applying.

Use `compute_deviation(baseline_session, target_curve)` for RMS error
delta in simulation.

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
3. If not: audit filters (Step D), redesign, iterate. Max 3 iterations.

**Step C.1 — Diagnose dominating residual (REQUIRED before iterating):**

Before designing more filters, classify the residual error:

- **Geometry-dominated** (`excluded_geometry_points` ≫ `included_points`,
  e.g. >80% of band): EQ cannot fix the residual. Most of the band's error
  is cancellation at the listener position. Stop adding input PEQ; instead,
  evaluate **per-sub band-limit** remediation:
  - Identify the deepest geometry null in the boost band from `null_zones`
    or the `summary` `max_error_db` entry. If it's in the deep-bass region
    (<50 Hz) and the room has 2+ subs, run the Phase 1.5 per-band cancellation
    re-check on the latest combined: `combined_SPL` vs `max(solo_SPL)` per
    1/3-octave. If `combined < max(solo) − 6 dB` at the null frequency, the
    null is sub-vs-sub destructive (band-limit one sub), not pure geometry.
    Apply `apply_eq(target=cancelling_sub, filters=[{type:hpf, freq:18, ...},
    {type:low_shelf, freq:<null_band_top>, gain_db:-10, q:0.7}])` to remove
    that sub from the cancellation band, then re-measure.
  - If `combined ≈ max(solo)` at the null, the null is room geometry at MLP
    and EQ cannot fix it. Document and proceed to Phase 4 — do not chase it
    with more filters; you will only add unwanted activity in correctable
    bands without changing the null depth.
- **Modal residual** (`excluded_geometry_points` < 50% of band, summary shows
  positive `error_db` peaks above target): proceed to Step D — narrow PEQ
  cuts at the peak frequencies are appropriate.
- **Level offset** (mean_error_db magnitude > 4 dB, distributed across band):
  the target curve is anchored at a different SPL than the measurement.
  Re-anchor the target_curve reference_spl to the measurement's mean SPL in
  the upper-bass band (60-80 Hz) and re-run `compute_deviation`. Don't add
  filters to chase a mis-anchored target.

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
