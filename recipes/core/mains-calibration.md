# Recipe: Mains + Subs Unified Calibration
version: 0.1.0

## Goal

Calibrate **every channel** in the active signal path (mains, surrounds, atmos,
subs) at the primary listening position so the full system converges on a single
unified target curve (default: `harman-plus-4`). The recipe enumerates channels
from `config.signal_graph.transducers` + `config.speakers` — it adapts to whatever
hardware is present, never hardcoded.

The recipe is **single-seat (MLP only)**. Multi-position averaging is a future
extension and explicitly out of scope here.

This recipe writes to the AVR (distances, levels, crossovers, optional FIR via
Audyssey) and to the sub DSP. Every write is gated by user confirmation in
safe mode; SafetyValidator still enforces the per-band limits in either mode.

## Filter Strategy

| Layer | Tool | Slots | Purpose | Required? |
|-------|------|-------|---------|-----------|
| Sub-DSP shelf | `apply_input_eq` (CamillaDSP) | 8 | Anchor the bass region of the unified target on the sub bus | Required |
| Sub-DSP per-output PEQ | `apply_eq` per sub | 8 / sub | Per-sub modal correction (existing bass-calibration output, kept) | Carried-over |
| AVR per-channel FIR | `design_avr_fir` + `apply_avr_fir` | 1024 / 704 coeff | Per-channel correction toward target on the mains bus | Capability-dependent |
| AVR distances/levels/crossovers | `push_avr_speaker_layout` | n/a | Time + level alignment, atomic write | Required |

Phase ordering — measure-first, then time/level alignment, then filter design,
then verify. FIR design comes BEFORE writing distances + levels because the
target-curve fit treats the corrected response, not the raw measurement.

> **TODO (filter-design refactor — see `_tool_design_avr_fir` docstring):** the
> filter-design tool surface is currently coupled to AVR Audyssey output format
> (`design_avr_fir`) and CamillaDSP (`design_fir`). A future refactor should
> split these into a generic `design_fir(target_curve, samplerate, taps, phase)`
> + hardware-specific apply adapters. Until that lands, Phase 6 below MUST
> branch on `config.eq_capabilities` and the active processors in
> `config.signal_graph.processors` to decide which design tool to call. If the
> active hardware has no FIR-capable mains DSP, Phase 6 (mains FIR) is skipped
> and the report says so.

## Configuration

Ask the user (or accept defaults):

- **Target curve** (default: `harman-plus-4`). Load
  `recipes/curves/{name}.json`. The curve's `points` are interpolated across
  20 Hz – 20 kHz for the full system.
- **Crossover frequency** (default: read from current `.ady` per channel; the
  recipe does NOT propose a global crossover unless the user asks).
- **Convergence threshold** (default: 1.5 dB RMS deviation in-band).
- **Max iterations per phase** (default: 3).
- **Reference channel for level matching** (default: `C` if present, else `FL`).
- **Bass region for sub-shelf fit** (default: 20 – 200 Hz).

## Measurement Signal Path

This recipe requires **every measurement to share the same AVR-routed path** so
IR peak times are commensurable. Distance computation is from measured IR peak
times — no calculation, no static offsets.

Build the diagram from `config`:

- Pi → HDMI → AVR → speakers → room → mic → Pi (mains)
- Pi → HDMI → AVR → sub-pre-out → Focusrite → CamillaDSP → subs → room → mic → Pi (subs)

Both paths share the same HDMI trigger so peak times are referenced to each other.

## Pre-flight

### 0.1 System check
Call `check_system`. STOP if any component is unreachable.

### 0.2 Read config
Call `get_config`. Discover:
- All transducers from `signal_graph.transducers` (role, processor, output_index, profile)
- All speaker positions from `speakers[].positions` (model, sweep_range_hz, crossover_hz, freq_response, sensitivity_db, impedance_ohms)
- DSP capabilities from `eq_capabilities` (FIR-capable? max taps? PEQ slots?)
- Mic from `mic.name`

### 0.3 HDMI sweep gate (open blocker as of 2026-05-03)
Take a sanity sweep on the reference channel (e.g. C). If the result has
SNR < `measurement.min_snr_db`, STOP with a diagnostic — see project memory
`project_2026-05-03_hdmi_mains_blocked.md`.

### 0.4 Mute everything that shouldn't play during cal
- Shakers: `mute_output` on every shaker output (per `feedback_shakers_never_during_cal.md`)
- Confirm AVR is in a sound mode that engages Audyssey (NOT Pure Direct, MultEQ on)
- Set `master_gain_db` per `config.measurement.master_gain_hdmi_db`

### 0.5 Back up current AVR state
Confirm a recent `.ady` file is on disk (look in `backups/` and `1_BACKUP_*.ady`).
The AVR-write tools depend on this as the layout source of truth. STOP if no
`.ady` is found — the user must export one from MultEQ Editor first.

### 0.6 Save run record
Call `save_calibration_run(recipe_name="mains-calibration", target=<curve>,
goal=..., hypothesis=..., device_state=get_device_state())`. Save the run_id.

## Phase 1 — Baseline measurement of every channel

For **each transducer** present in the signal graph (mains, surrounds, atmos,
subs), measure solo at MLP through the AVR path:

1. Solo-route the channel: mute every other output at the appropriate layer
   (AVR speaker mute for mains, DSP `mute_output` for subs).
2. Use the speaker spec's `sweep_range_hz` from `config.speakers` for that
   position (e.g. mains: 60 Hz–20 kHz; surrounds: 80 Hz–20 kHz). For subs,
   use 20 Hz–200 Hz from `sub.sweep_range_hz`.
3. Call `measure(label="mains-cal-baseline-{position}", target_position="MLP")`.
4. After all sweeps, call `analyze_ir(session_id)` per channel to get IR peak
   time and SPL. Save the per-channel peak time `t_peak_ms` and band-limited
   SPL.
5. Save iteration: `save_calibration_iteration(run_id, iteration=1, ...)`.

Unmute after each solo measurement before moving to the next channel.

## Phase 2 — Distance alignment (measured, not calculated)

Distance is derived from IR peak time difference between the slowest channel
and each other channel. NEVER infer from physical measurements or assume
sub-chain latency — measure it.

1. Find the channel with the **largest** `t_peak_ms` from Phase 1
   (slowest-arriving). Call this `t_max`.
2. For each other channel `c`, the required additional delay is
   `Δt_c = t_max - t_peak_c (ms)`. Convert to a distance increment:
   `Δd_c = Δt_c × 343 / 1000  (meters at 343 m/s)`.
3. Read each channel's current `customDistance` from the `.ady`. The new
   distance for channel `c` is `current_distance_c + Δd_c`.
4. Build `distance_overrides_m = {channel_id: new_distance_m, ...}`.
5. Sub channels often need to push past the MultEQ Editor 18 m UI cap because
   of accumulated FIR latency on the sub chain — `push_avr_speaker_layout`
   handles values past the cap. Document any sub channel that lands above 18 m.

Do NOT push yet — accumulate `distance_overrides_m` for the atomic Phase 7
write.

## Phase 3 — Level alignment

For each channel, compute band-limited average SPL from the Phase 1 measurement:
- Mains/surrounds/atmos: 500 Hz – 2 kHz (mid-band, free of room mode and
  treble tilt)
- Subs: 30 Hz – 60 Hz (sub midband, above port and below crossover)

Compare against the chosen reference channel's SPL.

`level_override_db_c = SPL_reference - SPL_c`

Build `level_overrides_db = {channel_id: trim_db, ...}`. Cap to ±10 dB to stay
inside Audyssey's clamp.

For sub-vs-mains level (cross-band), use a separate reference: aim for
`SPL_subs_30-60Hz` to be `target_curve.offset_at(50_Hz) - target_curve.offset_at(1_kHz)`
relative to `SPL_mains_500_Hz-2_kHz`. With `harman-plus-4` at 50 Hz the offset
is +4 dB.

## Phase 4 — Crossover alignment (subs ↔ mains phase at XO)

After distances are computed (Phase 2) but before pushing, refine sub-bus delay
to phase-align subs and mains in the crossover region.

1. With Phase 2 distance overrides applied volatile-only (`commit=False`),
   measure the L+R+C mains group (no subs) and the subs group (no mains).
2. Restrict the analysis to ±1 octave around the user's crossover frequency.
3. Call `optimize_sub_alignment(session_ids=[mains_id, subs_id],
   priority_band=[xo*0.5, xo*2.0])` — search delay/gain/polarity in the XO
   region.
4. Apply per-sub recommendations via `set_delay` / `set_polarity` /
   `set_output_gain` on the sub DSP. Do NOT change AVR sub distance here —
   that was set in Phase 2 from the IR peak measurement and reflects total
   acoustic delay.
5. Re-measure subs solo to confirm peak time hasn't drifted; if it did,
   recompute Phase 2 sub distance and update `distance_overrides_m`.

## Phase 5 — Anchor the unified target

1. Load the target curve JSON. Resample its `points` across 20 Hz – 20 kHz
   for full-range fit, and across the bass region for sub-shelf fit.
2. For the **bass region**: take a fresh combined-bass measurement (all subs
   playing, mains playing as bass-managed) and call
   `anchor_target(session_id, target_offsets=<bass-region points>,
   direction="cuts_only")`. This sets the absolute SPL anchor that minimizes
   required boosts.
3. The anchor's `reference_spl` is used in Phase 6 to set the input-EQ shelf
   gain on the sub bus.

## Phase 6 — Design correction filters

> **TODO — DSP-capability branching.** The exact tool calls below assume an
> Audyssey-FIR-capable AVR with a CamillaDSP sub bus. Generalize once the
> filter-design refactor lands (see top-of-file TODO). Until then, branch on:
>
> - `eq_capabilities.fir_capable` for the sub DSP path
> - `signal_graph.processors[].kind == "avr"` AND the AVR driver advertising
>   `audyssey_fir_supported` for the mains FIR path
>
> If mains FIR is unsupported, skip Phase 6.2 and report what was skipped.

### 6.1 Sub bus shelf (CamillaDSP input PEQ)

For the bass-region target shape, fit a low shelf:
`fit_shelf_for_target(session_id=<combined-bass>, target_curve=<harman-plus-4 bass region>,
min_hz=20, max_hz=120)`.

`apply_input_eq(filters=[18Hz HPF, fitted shelf, ...existing per-sub corrections preserved])`
on the sub bus. Always include the 18 Hz HPF.

### 6.2 Mains FIR (AVR Audyssey path)

For each main/surround/atmos channel `c`:
1. Compute the per-channel correction target: `target(f) = curve(f) - measured_c(f)`
   over `speaker.sweep_range_hz` for that position.
2. Cap correction to ±6 dB per band (Audyssey hard limit) and 0 dB outside the
   speaker's freq_response.
3. Call `design_avr_fir(channel_id=c, target_curve_db=<correction>,
   cache_key="mains-cal-<run_id>")`.

Repeat for every mains channel present.

### 6.3 Simulate before writing

Before any apply, call `simulate_eq` (sub bus filters) and inspect the FIR
target-curve fit error per channel. Iterate in simulation until the predicted
in-band RMS deviation is below the convergence threshold. No hardware writes
until simulation is satisfied.

## Phase 7 — Write to hardware (gated by user confirmation)

Atomic AVR write — distances + levels + crossovers in one envelope:

```
push_avr_speaker_layout(
    ady_path=<latest .ady>,
    distance_overrides_m=<from Phase 2/4>,
    level_overrides_db=<from Phase 3>,
    crossover_overrides_hz=<from Phase 5 if changed, else omit>,
    commit=True,  # only after explicit user OK
)
```

If FIR designs exist (Phase 6.2):

```
apply_avr_fir(
    host=<config.denon.host>,
    ady_path=<latest .ady>,
    cache_key="mains-cal-<run_id>",
    distances_override_m=<distance_overrides_m>,  # keep distances aligned in this write too
)
```

Sub-DSP writes (Phase 6.1) via `apply_input_eq` — same iteration.

In safe mode, describe each write in plain English before calling — channels,
values, why — and wait for explicit confirmation.

## Phase 8 — Verify

1. Re-measure each main solo + each sub solo + combined.
2. `compute_deviation(session_id=<combined>, target=<harman-plus-4>,
   resolution="sixth_octave", convergence_threshold=1.5)`.
3. `compare_sessions(baseline_id, final_id)` for the before/after scorecard.
4. Re-run `analyze_ir` per channel. Confirm peak times now align within
   ±1 ms (single-seat target).

## Convergence

- In-band RMS deviation from target ≤ 1.5 dB (default), configurable.
- Per-channel IR peak times within ±1 ms of slowest channel.
- No safety violations from `apply_eq` / `apply_input_eq`.
- All Phase 7 writes ACK'd; `applied=True`, `committed=True`.

If max iterations reached without convergence: STOP, do NOT keep iterating.
Report the residual deviations and proceed to the retrospective.

## When convergence fails

Distinguish EQ-fixable vs placement-fixable:
- `analyze_phase` says `fixable=False` at problematic frequencies → recommend
  speaker repositioning, room treatment.
- Coherence < 0.8 in the deviation band → measurement noise; re-measure with
  higher SNR before designing more filters.
- Sub+mains XO null persists after Phase 4 → physical sub placement issue,
  not a delay problem.

## Cleanup (NON-NEGOTIABLE)

1. `update_calibration_run(run_id, converged=..., final_rms=..., iterations_run=...)` — ALWAYS, before any cleanup.
2. `unmute_output` for every output muted in Phase 0 (shakers etc.).
3. `restore_listening_mode()` — re-establishes the cal_matrix and master gain.
4. `set_master_gain(0)` and verify (`end_sweep_session` does NOT do this).
5. Confirm AVR is back on the user's normal sound mode.

## Retrospective

Required, even on convergence:

1. **Before/after scorecard** via `compare_sessions`.
2. **Per-channel IR peak time table** — slowest channel, all deltas, all final residuals.
3. **Per-channel level table** — measured, target, applied trim.
4. **Unfixable problems** from `analyze_phase` `fixable=False` bands — placement / treatment recommendations.
5. **Lessons** — `record_lesson` for any non-obvious finding, capped at 2 per run, scope `room` (with invalidators) or `general` (with promotion plan).

## MCP tools used

### Hardware I/O
- `check_system`, `measure`, `mute_output`, `unmute_output`, `set_delay`, `set_polarity`, `set_output_gain`
- `apply_input_eq` (sub bus shelf)
- `apply_eq` (per-sub PEQ, if carried over)
- `push_avr_speaker_layout` (atomic distance + level + crossover write)
- `design_avr_fir` + `apply_avr_fir` (mains FIR — capability-gated; see TODO)
- `set_master_gain`, `end_sweep_session`, `restore_listening_mode`

### Analytics
- `analyze_ir` (peak times, SPL — backbone of Phase 2 + 3)
- `analyze_phase` (fixability for filter targeting)
- `compute_deviation`, `compare_sessions`
- `optimize_sub_alignment` (XO-region phase alignment, Phase 4)

### Simulation
- `simulate_eq` (sub bus shelf preview)
- `fit_shelf_for_target` (Phase 6.1)
- `anchor_target` (Phase 5)

### State and config
- `get_config`, `get_device_state`, `get_output_state`, `get_measurement_history`
- `save_calibration_run`, `save_calibration_iteration`, `update_calibration_run`
- `record_lesson`, `get_relevant_lessons`
