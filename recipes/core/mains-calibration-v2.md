# Recipe: Mains + Subs Cinema Calibration (v2)
version: 2.0.0

## Goal

Calibrate every channel for a **cinema-ready listening experience** at MLP, not
a flat anechoic frequency response. The system should:

- Hit reference loudness at AVR volume −10 to −5 dB (not require near-max)
- Have **felt** sub bass on action content (chest pressure at 25–40 Hz)
- Keep modal warmth (don't cut every measured peak)
- Reproduce voices with chest-resonance body intact (no aggressive cuts in
  100–200 Hz on the Center channel)

This is a deliberate course-correction from v1 (`mains-calibration.md`), which
cut every modal peak above ~+5 dB and produced a technically flat but
perceptually flat-sounding room. The lesson note
`feedback_calibration_v2_avoid_flat.md` captures the failure mode and the
corrected philosophy this recipe encodes.

## Filter philosophy — be surgical, not greedy

The v1 failure was treating "minimize RMS deviation from target" as the
objective. v2 treats it as one input among several:

1. **Cut only narrow modes**: Q > 3 AND peak > +8 dB above the local target.
   Broader humps (Q < 3, peak +3 to +6 dB) are "warmth" — leave them.
2. **Cap any single cut at −5 dB.** Deeper cuts on a mode-rich curve produce
   the flat/dull sound. If a peak is +12 dB, don't try to fully erase it; cut
   it to +7 dB and accept the residue.
3. **Never cut 100–200 Hz on the Center channel** more than −3 dB. That band
   carries male voice fundamentals and chest resonance; aggressive cuts
   muffle dialog.
4. **Target curve = `recipes/curves/harman-plus-4.json`** — anchored at
   **1 kHz** (not 80 Hz), with a **+4 dB bass shelf 20–50 Hz** tapering
   to 0 dB at ~200 Hz, then a ~1 dB/octave downward HF tilt. Read the
   curve file at runtime — don't hand-write target values into the
   recipe.
5. **Level-match POST-FIR**, not before. FIR insertion changes per-channel
   gain. Setting AVR trims to the post-FIR pink-noise level fixes the
   "doesn't feel like reference" symptom.
6. **Modal-robustness check, not full multi-position averaging.** We don't
   yet have an `average_sessions` MCP tool, and ≤80 Hz wavelengths (4+ m)
   barely shift across a 20 cm head movement so spatial averaging there
   is low-value anyway. Instead: take the primary sweep at MLP, then a
   single head-shift sweep ~20 cm to one side. Use the second only as a
   **modal robustness discriminator** — only cut a peak if it shows up
   in both. A peak at one head position but not the other is positional
   comb-filtering, not a room mode; cutting it makes the other position
   worse.

## Addressing scopes — `target` vs `output_index` vs `channel_ids`

Three different addressing namespaces are in play:

- **AVR speaker channels** — `["FL", "C", "FR", "SLA", "SRA", "TFL",
  "TFR", "TRL", "TRR", "SW1"]`. Used by `apply_avr_fir`,
  `push_avr_speaker_layout`, and `design_avr_fir`. These are Audyssey
  `commandId` values from `signal_graph` / `.ady`. Discover via
  `get_signal_graph` + `.ady` parse.
- **CamillaDSP outputs** — integer indices `0`–`9`. Used by `apply_eq`,
  `apply_fir`, `set_delay`, `set_polarity`, `clear_fir`. Index 5/6 =
  subs, 7 = shaker on this rig. Discover via `get_signal_graph` and
  prefer `target=...` (transducer name like `"sub_front_right"`,
  group like `"bass"`, or role `"sub"`) over raw indices.
- **Run-tracking labels** — strings in measurement labels and
  iteration filter dicts. Use the AVR commandId for consistency with
  the run record across phases.

Use `resolve_target("bass")` when you need raw indices for legacy
tools. Otherwise prefer named targets so per-transducer safety
profiles auto-apply.

## Measurement Signal Path

This recipe requires the AVR-routed path for every measurement so IR
peak times are commensurable across channels. Distance computation
(Phase 2) is from measured IR peaks — never from physical room
measurements or static offsets.

```
Pi → HDMI → AVR (Audyssey + bass-mgmt + our FIRs)
              ├─→ speakers → room → mic → Pi (FL/FR/C/SLA/SRA/atmos)
              └─→ sub pre-out → Scarlett input 3 → CamillaDSP
                                                  → (input_eq + per-output)
                                                  → Scarlett analog out
                                                  → subs → room → mic → Pi
```

Sub-only `cal_mode` loopback is WRONG for this recipe — distances would
be unreferenced. If `config.measurement.playback_route != "hdmi"`, the
recipe MUST switch it (or refuse with a clear message).

## Configuration

Ask the user (or accept defaults):

- **Target curve** (default: `harman-plus-4` — load `recipes/curves/{name}.json`)
- **Reference SPL** (default: `75 dB` C-weighted at MLP for −20 dBFS pink noise
  per channel — Audyssey/THX standard)
- **AVR Audyssey EQ Set** (default: `Reference` for cinema — warmer HF tilt;
  use `Flat` only for music monitoring)
- **AVR Dynamic EQ** (default: `ON` for cinema — compensates for low-volume
  listening per Fletcher-Munson; off for reference-level listening)
- **Head-shift offset** (default: `20 cm` to one side — sole purpose is
  modal-vs-positional discrimination; not used for averaging)
- **Modal cut threshold** (default: `+8 dB above local target` — peaks below
  this are left as warmth)
- **Modal cut Q threshold** (default: `3` — only cut narrow peaks)
- **Max single cut depth** (default: `−5 dB` — beyond this is over-correction)
- **Max FIR refinement iterations** (default: `3` — exit early on listening
  test pass; never loop indefinitely)
- **Sub-bus master gain** (default: `−15 dB` — subs hotter than mains-parity by
  5 dB; closer to LFE-channel +10 dB cinema spec without overshoot)
- **Sub trim bump** (default: `+3 dB` on AVR `PSSWL` — cinematic bass headroom)
- **Center trim bump** (default: `+2 dB` on AVR `SSLEVC` — dialog clarity)

## Compliance — Run Instrumentation (NON-NEGOTIABLE)

Run-instrumentation is mandatory — every measurement-bearing phase below
calls these. Don't reference v1 by pointer; this list is the spec.

1. **At START** — `save_calibration_run(recipe_name, target, goal,
   hypothesis, device_state)` produces the `run_id` used by every other
   call.
2. **After every iteration of every measurement-bearing phase**
   (Phase 1, 1.5, 3 design, 4 sub-bus shaping, 5 per-sub modal, 6
   atomic push verification, 7 level matching, 8.5 each refinement
   pass) — `save_calibration_iteration(run_id, iteration, rms_before,
   rms_after, filters_proposed, filters_applied, safety_ok)`.
   `filters_proposed` should include enough text to identify which
   recipe phase produced the decision, so post-hoc audits can detect
   skipped phases.
3. **On every exit path before cleanup** (convergence, max iterations,
   listening-test pass, listening-test fail, hardware error, user
   abort) — `update_calibration_run(run_id, converged, final_rms,
   iterations_run, outcome, error)` BEFORE Phase 11 cleanup.
4. **Lessons** — at run end, `record_lesson` for ≤2 falsifiable claims,
   tagged `room` (with invalidators) or `general` (followed by
   `promote_lesson`).

SafetyValidator enforces per-band limits server-side regardless of safe
vs autonomous mode. The recipe never disables it; if a proposed filter
is rejected, retry with a milder version.

## Pre-flight

### 0.1 System check + speaker presence
- `check_system` — STOP if any component unreachable.
- Verify SSSPC bits via Telnet: `SSSPC ?` should report all expected positions
  YES (FRO, CEN, SUA at minimum; SBK if 7.x). After ANY Fin commit during
  this recipe, re-issue `SSSPCCEN YES` / `SSSPCSUA YES` (the commit resets
  them — see `feedback_avr_layout_push_does_not_engage_sspc.md`).

### 0.2 Backup + stale-state check
- Verify a recent `.ady` is on disk (will be Phase 6's layout-push baseline
  AND the rollback target if Phase 9 fails catastrophically).
- `get_device_state()` — this snapshot goes into the run record.
- Inspect active DSP state for stale per-output writes (polarity, gain,
  delay) from prior runs — `feedback_check_stale_dsp_state.md`. Anything
  not in this run's plan should be cleared or explicitly preserved.

### 0.3 AVR menu state — set ON THE AVR before continuing

The flags below live in the .ady envelope and on the AVR's settings
state. We do NOT push them programmatically (no `flag_overrides` parameter
on `push_avr_speaker_layout` yet). User must set these manually on the
AVR before starting Phase 1.

| Setting (Denon menu path) | Recipe default | Why |
|---|---|---|
| Audio → Audyssey → MultEQ | `Reference` | Cinema warmth (use `Flat` only for music monitoring) |
| Audio → Audyssey → Dynamic EQ | `ON` | Fletcher-Munson at low volume — required for movie watching |
| Audio → Audyssey → Dynamic Volume | `OFF` | Compresses dynamic range — leave off unless night-mode |
| Audio → Audyssey → MultEQ | `ON` (not Off) | Engages our pushed FIRs |

Verify from a Telnet probe: `PSDYNEQ ?` / `PSAUDY ?`. If a Fin commit
during Phase 6 resets any of them (some firmware versions toggle MultEQ
off after a coef push), re-set on the AVR remote/menu and document it
in the iteration record. **Re-verify before Phase 9** — listening-test
on a non-EQ system invalidates the gate.

### 0.4 HDMI route sanity gate

Mains pass-through this recipe must use `playback_route="hdmi"` with the
HDMI plugin pinned to `default:CARD=vc4hdmi0` (not the `hdmi:` plugin —
see `feedback_alsa_hdmi_plugin_downmixes.md`).

1. Take a sanity sweep on FL via HDMI at the configured `master_gain_hdmi_db`.
2. STOP if SNR < `measurement.min_snr_db` — see
   `project_2026-05-03_hdmi_mains_blocked.md` for diagnostics.

### 0.5 Save run + mute shakers
- `save_calibration_run(recipe_name="mains-calibration-v2",
  target="harman-plus-4", goal=..., hypothesis=...,
  device_state=<from 0.2>)`. Save the returned `run_id`.
- `mute_output(target="tactile")` for every shaker output. Tactile content
  is never a calibration target — see
  `feedback_shakers_never_during_cal.md`.

## Phase 1 — Baseline measurement (per channel, MLP + 1 head-shift)

Solo each channel through the AVR path. For each transducer in the signal
graph + speakers config:

1. Mute everything else (AVR speaker mute for mains, DSP `mute_output`
   for subs).
2. **MLP-exact sweep** at the speaker's `sweep_range_hz` (mains
   60 Hz–20 kHz, surrounds 80 Hz–20 kHz, subs 20–200 Hz).
   `measure(label="v2-baseline-{channel}-mlp", position="MLP")`
3. **Head-shift sweep** at MLP + 20 cm (one side; pick consistent side
   across all channels for the run). Same sweep range.
   `measure(label="v2-baseline-{channel}-shift", position="MLP+20cm")`
4. `analyze_ir(mlp_session_id)` — capture `t_peak_ms` and band-limited
   SPL. Time alignment uses the MLP-exact peak only; the head-shift is
   for modal-robustness only.

After the full pass: `save_calibration_iteration(run_id, iteration=1,
rms_before=0, rms_after=0, filters_proposed=<per-channel session ids>,
filters_applied=[], safety_ok=True)`. RMS is 0/0 because this is
measure-only; both session_ids per channel + analyze_ir summaries go
into `filters_proposed` for the audit trail.

### 1.5 Sub-mains crossover phase verification (LLM-driven, no AVR write)

Before designing FIRs, verify sub + mains are not phase-cancelling at the
crossover region. This is a major contributor to "dull" perceived bass
that no FIR shaping will fix.

**Important — LLM-first**: this phase used to call `optimize_sub_alignment`,
which sweeps a parameter space and picks the local minimum by a fixed
cost function. That's a solver doing the LLM's job. Replaced with a
data + analytics + judgment workflow.

**Important — no AVR write here**. This phase only *proposes* a candidate
SW1 distance. Phase 2 rolls candidates into `distance_overrides_m` for
Phase 6's atomic push. We never write to the AVR mid-recipe.

1. **Measure** three sessions at MLP:
   - FL-solo (mains route only, sub muted)
   - sub-solo (sub-bus only, mains muted)
   - FL+sub combined (FL active + bass-mgmt routing to sub)
2. **Numerical comparison** (no `sum_of` primitive exists — Claude
   reasons across two pairwise diffs):
   - `compare_sessions(combined_id, FL_solo_id)` — shows what the sub
     adds (in-phase = strong addition; out-of-phase = cancellation).
   - `compare_sessions(combined_id, sub_solo_id)` — shows what the
     mains add at the crossover.
   - Inspect the 60–100 Hz region of each. **Suspect cancellation if**
     combined is *not* greater than the louder of the two solos by at
     least 2 dB across 60–100 Hz, or if there's a sharp dip in
     `combined - FL_solo` localized in that band.
3. **Classify** the same band: `analyze_phase(combined_id)` returns
   per-1/3-octave `geometry` / `partial` / `minimum_phase`
   classifications.
4. **Claude reasons** over the data:
   - If combined is louder than each solo by ≥2 dB across 60–100 Hz →
     in-phase, OK. Record SW1 distance unchanged for Phase 2.
   - If a dip is in a `geometry`-classified band → leave it. "Geometric
     null at NN Hz, accepted" is the right answer; chasing geometric
     nulls with delay creates new nulls elsewhere.
   - If a dip is in `minimum_phase` or `partial` → fixable.
     Starting Δd from the freq of the deepest dip:
     `Δd_candidate ≈ 343 / (2 × null_freq_hz)` meters (a half-wavelength
     shift inverts the cancelling phase). This is a starting point only;
     iterate via Phase 8.5 if the first candidate doesn't help.
5. **Record** for Phase 2: candidate `SW1_new = SW1_current + Δd_candidate`,
   the residual diff plots, and the rationale. NO AVR WRITE HERE.
6. `save_calibration_iteration(run_id, iteration=N, rms_before=...,
   rms_after=..., filters_proposed=[{"phase": "1.5",
   "sw1_candidate_m": ..., "rationale": ...}], filters_applied=[],
   safety_ok=True)`. `filters_applied` is empty because we deferred
   the write to Phase 6.

**Why no solver**: the actual judgment is "is this null fixable, or is it
geometry that we should stop fighting?" — that depends on room context
Claude has more access to than a delay-sweep cost function.

## Phase 2 — Time alignment (measured, deferred-write)

Pick the slowest-arriving channel from Phase 1 IR peak times, compute Δt
per other channel, convert to `Δd_m = Δt_ms × 0.343`, build the
`distance_overrides_m` dict. **Don't push yet** — write happens
atomically in Phase 6.

Roll Phase 1.5's SW1 candidate (if proposed) into the same dict here:
if both Phase 2 and 1.5 propose a SW1 distance, prefer Phase 1.5's
(it's tuned to the actual sub-mains coherence, not just IR peak time).
Document the divergence in the iteration record.

Sub channels typically need values past the 18 m UI cap; that's expected
because of FIR-induced latency on the sub chain. Document any channel
above 18 m.

`save_calibration_iteration(run_id, iteration=N, rms_before=0,
rms_after=0, filters_proposed=<distance_overrides_m + per-channel
Δt rationale>, filters_applied=[], safety_ok=True)`. Applied is empty
because we deferred the AVR write to Phase 6.

## Phase 3 — Surgical modal cuts (per-channel FIR target curves, LLM-driven)

**No `anchor_target` calls.** That tool conflates analytics with judgment
(it picks the reference-SPL anchor by a fixed heuristic, which is exactly
the decision the LLM should be making with full room/run context). The
workflow below uses `analyze_phase` + `compute_deviation` + Claude's
judgment instead.

**Known limitation — AVR FIR is unreliable below ~80 Hz** (per
`project_avr_fir_decimation_broken.md`). The 117 Hz region delivers as
designed; 70 Hz delivered 7.7 dB short of design. Treat mains FIR as a
≥80 Hz tool only. For sub-bass shaping (≤80 Hz), rely on Phase 4
input_eq on the sub bus, where filters are not affected by this AVR
bug. Mains-FIR cuts in 60–80 Hz may underdeliver and require iterative
re-design — note this in the run record.

For each main / surround / atmos channel:

1. **Run analytics on the MLP-exact session**:
   - `analyze_phase(mlp_session_id)` — per-1/3-octave classification of
     `geometry` / `partial` / `minimum_phase` bands. Geometry bands
     (cancellation nulls) are **never cut and never boosted**.
   - `analyze_decay(mlp_session_id)` — returns `modes` list (note: tool
     returns `modes`, not `decay_modes`). Each mode has `freq_hz`,
     `t60_ms`, `peak_db`, `suggested_q`. Q comes from this tool, not
     from FR magnitude derivation.
2. **Identify candidate cuts** — modes that satisfy ALL:
   - `peak_db > +8 dB` above local 1/3-octave target
   - `suggested_q > 3` (narrow, isolated)
   - `t60_ms > 200 ms` (narrow modal character, not a transient)
   - **AND** a peak within ±1 1/3-octave band shows up in the head-
     shift session (modal-robustness; rules out positional comb-
     filtering — exact-Hz matching is too strict, ±band is right)
   - **AND** band classification is `minimum_phase` or `partial`
     (geometry bands stay untouched)
   - **AND** for mains channels, `freq_hz ≥ 80 Hz` (LF AVR-FIR bug —
     leave the deeper modes for Phase 4 input_eq)
3. **Propose cut depth**: `depth = −min(peak_excess − 3, 5) dB` (leave
   3 dB of the peak, cap at 5 dB cut). Q matches `suggested_q`.
4. **Center channel exception**: any cut in 100–200 Hz capped at −3 dB
   regardless of peak height. Voice clarity overrides modal flatness.
5. **Cumulative-boost sanity check**: after building the per-channel
   filter list, sum any boosts within each 1/3-octave band. If the
   cumulative exceeds the speaker profile's
   `max_cumulative_boost_in_third_octave_db`, drop the smallest
   contributor and re-check. SafetyValidator will reject the push
   otherwise.
6. **Pick a reference SPL** — Claude's judgment, not a solver:
   - Read each channel's `band_avg_spl` from the FR data
   - Read the speaker profile's `max_boost_per_band_db` headroom limit
   - Pick a candidate `reference_spl` at a frequency where measured ≈
     local target — i.e. anchor at the *natural band*, not the curve's
     0 dB point (per `feedback_anchor_target_at_natural_band.md`).
     Boosts at sub-bass don't deliver well in modal-rich rooms; cuts do.
   - Validate via `compute_deviation` — note the parameter shape:
     ```
     target_curve = {
       "type": "harman-plus-4",
       "reference_spl": <candidate>,
       "band": [25, 500],
       "points": [{"freq_hz": ..., "spl": <candidate> + harman_offset}, ...]
     }
     compute_deviation(session_id=..., target_curve=target_curve,
                       resolution="sixth_octave")
     ```
     `reference_spl` is encoded into each `points[*].spl` (NOT a
     top-level arg).
   - Inspect the residual map. If any band needs boost > headroom,
     lower `reference_spl` and re-run. If the map is "mostly cuts above
     the anchor with low residuals", that's the anchor.
   - Don't overthink; an iter1 anchor that's 1-2 dB off is fine — Phase
     8.5 iteration tightens it.
7. **Build per-channel `target_curve_db`** (the input to
   `design_avr_fir`, distinct from the `target_curve` dict for
   `compute_deviation`): list of `{freq_hz, gain_db}` points combining
   the harman-plus-4 offsets (scaled to `reference_spl`) with the
   surgical cuts from steps 2–3.
8. `design_avr_fir(channel_id=..., target_curve_db=..., cache_key=...)`
   — verify peak_amplitude < 0.95 in the response (means we have
   FIR-coefficient headroom).
9. **Passthrough channels**: atmos (TFL/TFR/TRL/TRR) and SW1 get
   explicit passthrough designs (target_curve_db with two 0 dB points)
   so they're in the cache for the Phase 6 atomic push.

`save_calibration_iteration(run_id, iteration=N, rms_before=..., rms_after=...,
filters_proposed=<per-channel target_curve_db dicts>, filters_applied=[],
safety_ok=True)` — applied is empty because we defer the AVR write to
Phase 6. Keep `filters_proposed` human-readable so post-hoc audits can
see Claude's reasoning.

## Phase 4 — Sub-bus shaping (CamillaDSP input_eq, LLM-driven)

Modest, not aggressive. **Derive parameters from the measured sub
response — never hardcode shelf gains.**

**LLM-first**: this phase used to call `fit_shelf_for_target` to pick
shelf params. That's the same solver pattern we removed elsewhere — a
tool deciding "what shelf shape minimizes residual error". Replaced
with a candidate-and-validate loop: Claude proposes shelf params,
`simulate_eq` reports the predicted residual, Claude adjusts.
`fit_shelf_for_target` may still be called as a *suggestion oracle*
(read its output as a starting point), but the recipe uses
`simulate_eq` as the source-of-truth verification.

The complete filter list passed to `apply_input_eq` MUST include every
filter the chain needs — the call replaces the existing input_eq
wholesale, so any omitted filter (including the mandatory HPF) is
lost. Be explicit.

1. **Inputs**:
   - The sub-solo session from Phase 1 (per-sub) plus the FL+sub
     combined session from Phase 1.5 (for the punch-zone validation).
   - Speaker profile: `svs_pb12_nsd` — 18 Hz mandatory HPF, max boost
     +6 dB/band, max cumulative +9 dB/octave, min boost freq 25 Hz.
2. **Mandatory filters** (always present in the output list):
   - `{"freq": 18, "gain_db": 0, "q": 0.7, "type": "hpf"}` — driver
     protection, never omit.
3. **Propose a low-shelf** for the Harman+4 bass tilt:
   - Read the sub-solo measurement's natural response (port roll-off
     freq, modal hot-spots).
   - Candidate starting point: `low_shelf` at 40–60 Hz, +2 to +5 dB,
     q≈1.0. Pick from this range based on where the sub's natural
     response drops below the target.
4. **Don't cut 50–80 Hz on input_eq.** That's the kick/punch zone shared
   by sub + bass-managed mains; cuts here register as "no impact".
5. **Optional modal cut**: only if a specific sub-bus mode clearly
   dominates the combined response (>+8 dB Q>3 in a band classified
   `minimum_phase`), add ONE peaking cut for it. Otherwise leave it.
6. **Validate via simulation**:
   ```
   simulate_eq(session_id=<sub-solo session>, filters=<full list>,
               band=[20, 200])
   ```
   Inspect predicted FR vs the harman-plus-4 target. Acceptance:
   predicted ≤ ±3 dB from target across 25–80 Hz; ±2 dB across
   80–200 Hz. If wider, adjust shelf gain/freq and re-simulate. Don't
   apply until simulation passes.
7. `apply_input_eq(filters=<full list with HPF>,
   simulation_verified=True)`. The full list always includes HPF;
   never call apply_input_eq with a list missing the HPF.
8. `save_calibration_iteration(run_id, iteration=N, rms_before=...,
   rms_after=..., filters_proposed=<full list>,
   filters_applied=<full list>, safety_ok=True)`.

## Phase 5 — Per-sub modal correction (only if obvious)

For each sub solo response from Phase 1:

1. Identify modes that meet **all three**: peak > +10 dB above local
   target, T60 > 400 ms, Q > 3 (Q from `analyze_decay`'s `suggested_q`).
2. **If ZERO modes qualify**, skip per-sub FIR — use only input_eq from
   Phase 4.
3. **If 1–2 modes qualify**, design **same treatment type on every
   sub** (per `feedback_per_sub_phase_coherence_trap.md` — mixed
   treatments collapse 20–50 Hz coherence at MLP). Different
   `cancel_strength` per sub is fine; mixing `anti_pulse` on one sub
   with `linear_notch` on another is not.
4. Use `design_modal_fir` at samplerate=48000 (NOT 8000 — see
   `project_sub_cal_signal_chain_TODO.md`; 8 kHz design at 48 kHz
   playback lands anti-pulses 6× too early).
5. **Check the FIR-history cache first** — `active_dsp_state_history`
   (PR #159) preserves prior-run FIRs. If a recent good-result FIR
   exists for this sub at the same modal frequency, restore it via
   `restore_active_dsp_history(history_id)` before designing fresh
   (saves measurement work + iteration time). Note: no MCP tool exposes
   this yet — for now, designing fresh is the only path. Listed here
   for the future tool wiring.
6. `save_calibration_iteration(run_id, iteration=N, rms_before=...,
   rms_after=..., filters_proposed=<modal FIR specs>,
   filters_applied=<applied via apply_fir>, safety_ok=...)`.

## Phase 6 — Atomic 10-channel push (FIR + envelope)

Single TCP/1256 session writes everything. Required because per-channel
pushes wipe other channels' FIRs (see `feedback_avr_5ch_push_unreliable.md`,
now resolved by retry-on-NACK in `audyssey_filter_upload.py`).

1. **Build call args**:
   - `host` = AVR IP from config
   - `ady_path` = path to backup .ady on the container filesystem
   - `cache_key` = the same `run_iter` used in Phase 3's design calls
   - `channel_ids` = `["FL", "C", "FR", "SLA", "SRA", "TFL", "TFR",
      "TRL", "TRR", "SW1"]` — full 10-channel set, including atmos +
      sub passthrough designs
   - `distances_override_m` = the dict accumulated from Phase 2 + 1.5
   - `inter_packet_delay_ms` = `100`
2. `apply_avr_fir(...)` — call returns when EXIT_AUDMD ACKs.
3. **Verify response**:
   - `coef_nack_count == 0` (retry-on-NACK auto-recovers transient
     drops; final 0 means everything got through after retries)
   - `coef_packets_unrecovered == 0` (any unrecovered means a
     permanent NACK on a packet — abort, do not commit Fin)
   - `fin_commit_ack == True` (NVRAM write succeeded)
   - Surface `coef_packet_retries` in the run record (count of
     transient NACKs auto-recovered)
4. **Settle 30 seconds** before any subsequent TCP/1256 or Telnet
   operation. The AVR's NVRAM flush continues after EXIT_AUDMD ACKs,
   and Telnet writes during this window have caused crashes (see
   `feedback_avr_settle_after_commit.md`).
5. **After the settle**, re-enable speaker presence (Fin commit
   resets these — see
   `feedback_avr_layout_push_does_not_engage_sspc.md`):
   ```
   echo "SSSPCCEN YES" | nc <avr> 23
   sleep 1
   echo "SSSPCSUA YES" | nc <avr> 23
   ```
   Verify with `SSSPC ?` that all expected positions read YES. Re-issue
   if any didn't stick.
6. **Re-verify Audyssey state** (per Phase 0.3 — Fin commits sometimes
   toggle MultEQ off):
   ```
   echo "PSDYNEQ ?" | nc <avr> 23
   echo "PSAUDY ?" | nc <avr> 23
   ```
   If MultEQ is OFF, set on AVR menu (we don't push this flag
   programmatically).
7. `save_calibration_iteration(run_id, iteration=N, rms_before=...,
   rms_after=..., filters_proposed=<full Phase 3+4+5 plan>,
   filters_applied=<channel_ids actually committed>,
   safety_ok=(coef_nack_count==0 and fin_commit_ack))`.

If verification fails (NACKs unrecovered, Fin gate aborted, AVR drops
TCP) — go to Phase 9's catastrophic-failure rollback, do NOT iterate.

## Phase 7 — Level alignment (POST-FIR pink noise)

This is what fixes the "doesn't feel like reference" symptom in v1.

### 7.1 Reference-volume sanity check

Before adjusting per-channel trims, verify what AVR volume corresponds to
reference SPL on this system:

1. Play −20 dBFS pink noise on FL solo at AVR volume = **−10 dB**.
2. Measure C-weighted SPL at MLP. Audyssey/THX reference says this should
   land around **75 dB**. Note the actual value as `avr_minus10_spl`.
3. The "AVR volume that delivers 75 dB" = `−10 + (75 − avr_minus10_spl)` dB.
   Document this in the run record so the user knows what knob position
   reads as reference.
4. If `avr_minus10_spl < 70 dB` even with our calibration: trims are
   too low; bump per-channel `SSLEV<chan>` uniformly +3 dB before per-
   channel level matching.

### 7.2 Per-channel trim landing

1. For each main / surround / atmos channel, play −20 dBFS pink noise via
   the channel solo path at the AVR volume from 7.1.
2. Measure C-weighted SPL at MLP with the UMIK.
3. Adjust AVR per-channel trim (`SSLEV<chan>`) until SPL = `reference_spl`
   (default 75 dB) ±0.5 dB.
4. For sub: target = `reference_spl + 10 dB` (+10 dB LFE spec). Set
   `PSSWL` accordingly. Apply the additional `+3 dB` cinema bump on top
   if the user's preference includes it.
5. Center: apply the `+2 dB` clarity bump on top of the level-matched
   value.
6. `save_calibration_iteration(run_id, iteration=N, rms_before=...,
   rms_after=..., filters_proposed=<per-channel target SPL>,
   filters_applied=<per-channel trim landing values>, safety_ok=True)`.

### 7.3 Distortion / clip check before high-SPL listening

Brief safety check. We **don't** programmatically toggle clip-warning
yet, so this is a guarded listen rather than an automated test:

1. Pick a known content track with sustained low-bass (e.g. *Tron Legacy*
   opening 30 s).
2. Start at the reference AVR volume from 7.1; listen for distortion,
   port chuffing, or audible compression on bass transients.
3. While playing, watch CamillaDSP `cpu_load` via `get_device_state`. If
   `> 80 %`, the DSP is starved and may drop samples — pull master gain
   back 3 dB and retry.
4. If audible distortion at reference volume: pull sub-bus master gain
   3 dB OR pull `PSSWL` 3 dB before listening tests.
5. **Future enhancement**: wire `assign_headroom_tones` (which exists
   but is unverified for this use) into a programmatic clip check.

### 7.4 Re-verify AVR Audyssey state

The flag set was originally configured on the AVR menu in Phase 0.3.
Phase 6's Fin commit can drift them. Re-verify before listening tests:

```
echo "PSAUDY ?" | nc <avr> 23     # should match the menu choice (Reference)
echo "PSDYNEQ ?" | nc <avr> 23    # should be ON for cinema
echo "PSDYNVOL ?" | nc <avr> 23   # should be OFF for cinema
```

If any drift, re-set on the AVR remote/menu and document in the
iteration record. We do NOT push these flags programmatically yet.

## Phase 8 — DSP master + sub-bus level

1. `set_master_gain(-15)` — sub bus operates at −15 dB (5 dB hotter than
   v1's −20 mains-parity).
2. Verify with a quick listen: bass should feel fuller without overpowering
   mains.

## Phase 8.5 — Iteration loop (max 3 passes, RMS is a signal not a gate)

This is the place to refine, before calling listening verification. Each
pass is a fresh measurement → identify residual issues → narrow re-design
→ atomic re-push. **The convergence gate is Phase 9 (listening test).**
RMS-deviation values here are *signals to escalate* the listening test,
not pass/fail gates.

1. Take a fresh exact-MLP sweep on FL, C, FR.
2. For each, build the same `target_curve` dict used in Phase 3.6 and
   compute residual deviation:
   ```
   compute_deviation(session_id=..., target_curve=<harman+4 with absolute
                     SPL>, resolution="sixth_octave")
   ```
   Note: `target_curve.band` is set inside the dict; there's no
   top-level `band` arg. Record `rms_after` per channel.
3. **Exit signals** (any one means "go listen"):
   - `rms_after ≤ 4 dB` AND no single residual peak > +6 dB above target
   - Iteration count == `max_FIR_refinement_iterations` (default 3)
   - User says "good enough"
4. **Always go listen first** before another iteration — even if RMS
   says we could improve, the listening test may already be acceptable.
   Phase 9 has the actual gate.
5. If Phase 9 says iterate: tighten ONE parameter only per pass — don't
   change everything at once. Common single-knob tightenings:
   - "Boomy at 50 Hz" → drop modal cut threshold to +6 dB above target
     (still cap depth at −5 dB); re-design + atomic push.
   - "Voices still muffled" → reduce Center 100–200 Hz cuts toward −2 dB
     ceiling; re-design Center only (still 10-ch atomic push with the
     others' designs unchanged from this iteration's cache).
   - "Too quiet overall" → re-run Phase 7 trim landing; possibly bump
     Phase 7.1's `avr_minus10_spl` target 2 dB hotter.
   - "Sub muddy / not deep" → re-run Phase 1.5 crossover phase check
     and Phase 4 bass-shelf simulation; one of them is wrong.
6. **Re-push uses the same `inter_packet_delay_ms=100` and a fresh 30 s
   settle** before any subsequent TCP/Telnet (per
   `feedback_avr_settle_after_commit.md`).
7. `save_calibration_iteration(...)` after every pass.

## Phase 9 — Listening verification (the actual convergence gate)

RMS deviation from target is a **secondary** metric for cinema. The
convergence gate is subjective listening with reference content.

Reference test material (each evaluated for ~30–60 s). Ask the user to
**also pick 2–3 tracks they know intimately**; user-specific material
catches issues generic test scenes miss.

| Track / Scene | What to listen for |
|---|---|
| *Tron Legacy* — opening Daft Punk synth | Sub depth at 25–40 Hz; chest pressure |
| *Edge of Tomorrow* — beach landing | Mid-bass impact + dialog clarity |
| *La La Land* — "Another Day of Sun" | Voice intelligibility, no muffling |
| *Mad Max: Fury Road* — chase scenes | Modal warmth, no boom or boxiness |
| Reference pink noise at −20 dBFS | All channels equal, no channel sticks out |
| **User-picked: 2–3 known tracks** | Naturalness, "sounds like the recording" |

Decision tree:

- **Sounds great** → record outcome + lesson, exit recipe (Phase 10).
- **Bass shallow / no impact** → bump `PSSWL` +3 dB; retry listening.
- **Voices muffled** → reduce Center FIR cuts in 100–200 Hz toward −2 dB
  ceiling; re-design Center only + 10-ch atomic re-push (Phase 8.5).
- **Overall too quiet** → bump per-channel `SSLEV<chan>` +2 dB (every
  main + center); verify volume sits at AVR −10 to −5 dB.
- **Boomy / boxy** → modal cut threshold was too lenient; tighten to
  +6 dB above local target (still cap at −5 dB depth); re-design + push.
- **Brittle / fatiguing** → cuts are too aggressive in upper-bass; reduce
  cut depth or drop a borderline cut; re-design + push.
- **One channel sticks out** (subjectively louder/quieter than the rest)
  → re-run Phase 7.2 for that channel only; verify pink-noise SPL is
  within 0.5 dB of `reference_spl`.
- **Phantom-center collapse** (vocals pull to one side instead of
  anchoring center) → FL/FR are louder than C; re-run Phase 7.2 for all
  three.
- **Atmos channels too quiet/loud** → re-run Phase 7.2 for atmos
  channels; their solo trim landing was likely skipped.
- **Sub-mains incoherence** (bass note hits but no chest pressure, or
  bass note + sub thump arrive separated in time) → re-run Phase 1.5
  crossover phase check; SW1 distance candidate may need a different
  Δd.
- **Iteration count == max_iterations and listening still fails** →
  exit with `converged=False`, record outcome describing the residual
  problem, recommend follow-ups (bass traps, sub repositioning, sub
  crawl). Don't loop indefinitely.
- **Catastrophically worse than baseline** (audibly broken — no
  dialog, distortion, channels missing) → ROLLBACK:
  1. `list_dsp_snapshots(run_id=<this run>)` — newest-first list of
     every state mutation in this run, each with operation name +
     iteration tag. Find the last-good-iteration snapshot id (or the
     `start_calibration` / `save_calibration_run` snapshot if you want
     to revert to pre-recipe state).
  2. `restore_dsp_snapshot(<id>)` — one call returns the DSP shadow
     (input EQ, per-output FIRs, delays, polarities, gains) to that
     exact state. The current state is itself snapshotted first so the
     rollback is reversible.
  3. AVR-side restore (only if Phase 6's Fin commit corrupted ChSetup):
     `push_avr_speaker_layout(ady_path=<backup .ady from 0.2>,
     commit=True)` to re-establish speaker layout + distances. If FIRs
     also need to be wiped on the AVR side, push 10 passthrough FIRs
     via `apply_avr_fir` — but in most cases the snapshot restore
     covers what you need.
  4. `update_calibration_run(converged=False, error="rollback after
     catastrophic listening-test failure")` then exit.

## Phase 10 — Run instrumentation close-out

`update_calibration_run(run_id, converged=<bool>, final_rms=<float>,
iterations_run=<N>, outcome=<prose>)`. Outcome should compare actual
listening result to hypothesis.

`record_lesson(...)` for ≤2 falsifiable claims. Examples:

- **room scope**: "Center −3 dB cut at 117 Hz delivered legible dialog
  improvement vs −7 dB; deeper cut muffled male voices at this room/MLP."
- **general scope** (with `promote_lesson` after action): "Cinema
  calibration converges on listening tests, not RMS deviation; v2 recipe
  encodes this gate."

## Phase 11 — Cleanup

1. `unmute_output(target="tactile")` for every shaker output (per
   `feedback_calibration_cleanup.md` — but note that bass-recipe
   convention says "set master to 0 dB"; this recipe deliberately
   diverges because **−15 dB IS our cinema operating level**, not a
   transient cal-time setting).
2. **`set_master_gain(-15)` (NOT 0)** — confirm with the operator that
   the recipe ends at −15 dB. Ignore the bass-recipe cleanup convention
   here; verify the operating level matches Configuration's
   `sub_bus_master_gain`.
3. Verify SSSPC presence still YES; re-enable via Telnet if any drifted
   to NO during Phase 9 iterations.
4. Re-verify Audyssey state per Phase 7.4 (PSAUDY, PSDYNEQ, PSDYNVOL).
5. Final `get_device_state` snapshot — record AVR volume, DSP master,
   per-channel trim landing values, MultEQ state for the run report.

## Notes / Future work

### LLM-first deprecations baked into this recipe

- **`anchor_target`** — conflated analytics (per-band error) with
  judgment (where to anchor) and the judgment piece belongs to the LLM.
  Phase 3 uses `analyze_phase` + `compute_deviation` directly with
  Claude's reasoning. Task #11 is now scoped as "delete the tool and
  callers", not "fix the crash."
- **`optimize_sub_alignment`** — same pattern (delay-sweep solver
  picking local minimum). Phase 1.5 replaces it with `compare_sessions`
  + `analyze_phase` + Claude's "fixable phase or geometric null?"
  judgment.
- **`fit_shelf_for_target`** — same pattern (shelf params chosen by
  fit). Phase 4 uses `simulate_eq` as the source-of-truth verification;
  Claude proposes shelf candidates; `fit_shelf_for_target` may be
  consulted as a starting-point oracle but is not the deciding tool.

The line: tools that compute *one* derived value (like `suggested_q`
from `analyze_decay`) are data tools, fine. Tools that pick *which*
peak to correct or *what* parameter value to apply are solver-shaped
and don't belong in an LLM-first recipe.

### Capabilities not yet wired

- **No `average_sessions` MCP tool** — multi-position averaging is
  approximated in Phase 1 by a single head-shift session used as a
  modal-robustness discriminator (peak must appear in both, not strict
  mean). True spatial averaging would need a tool to interpolate to a
  common bin grid and combine.
- **No `flag_overrides` on `push_avr_speaker_layout`** — AudyEqSet,
  AudyDynEq, AudyDynVol, AudyMultEQ are set on the AVR menu manually
  in Phase 0.3 and re-verified in Phase 7.4. Open improvement.
- **`active_dsp_state_history`** (per-key fine-grained, PR #159) and
  **`dsp_snapshots`** (per-operation coarse-grained, this PR) both auto-
  archive on every state mutation. No recipe-side instrumentation
  needed — every `apply_avr_fir`, `apply_eq`, `apply_input_eq`,
  `apply_fir`, `clear_fir`, `push_avr_speaker_layout`, `set_delay`,
  `set_polarity`, `set_output_gain`, `set_master_gain`, and
  `reset_dsp_defaults` call records its pre-mutation state. Snapshots
  taken inside a `save_calibration_run` / `update_calibration_run`
  boundary are tagged with `run_id` + `iteration` so they're trivially
  queryable per run via `list_dsp_snapshots(run_id=...)`. Phase 9's
  catastrophic-rollback should use
  `restore_dsp_snapshot(<last-good-snapshot-id>)` — one call returns
  the DSP shadow to that exact state. AVR-side envelope restore (if
  Phase 6 corrupted ChSetup) is still a separate
  `push_avr_speaker_layout` call from the backup .ady.
- **Computational compute split (task #10)** — recommended before
  iter3+ to keep MCP responsive during long runs. v2 doesn't depend on
  it but heavy iteration sessions are stress-prone without it.
- **No programmatic clip detection** — Phase 7.3's distortion check
  is a guarded listening test, not an automated clip probe. Wiring
  `assign_headroom_tones` (which exists but doesn't currently report
  clip state) would close this gap.
