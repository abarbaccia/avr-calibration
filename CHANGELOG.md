# Changelog

All notable changes to this project will be documented in this file.

## [0.2.5] - 2026-05-25

### Tests / Hardening
- **PEQ simulate-vs-apply coefficient match pinned.** Investigation of the 2026-05-25 report that designed input-PEQ cut depths landed 2-3× deeper at the listener than `simulate_eq` predicted found no coefficient mismatch — the simulator (`mcp_server._biquad_response`), the SafetyValidator magnitude path (`safety._filter_magnitude_db`), and the CamillaDSP driver (`CamillaDSPDriver._filter_block`) all use identical RBJ-cookbook biquad math and agree to floating-point precision. The observed 2-3× over-cut is consistent with measurement-to-measurement variance (4 dB IR-peak drift between supposedly identical "HPF-only" baselines 262 vs 267, polarity flip across the session). To prevent a future regression that would manifest the same symptom, added `tests/test_peq_simulate_vs_apply.py` with 17 tests covering: (a) `_biquad_response` matches analytical z-domain RBJ within 1e-9 dB across the bug-repro filter set, (b) `_filter_magnitude_db` matches the simulator within 0.01 dB at 20–200 Hz, (c) the driver forwards {freq, q, gain} byte-for-byte to CamillaDSP's `Biquad::Peaking` (which is RBJ cookbook in the daemon), and (d) the routing-mixer single-application invariant — exactly one input may route to any given output, otherwise input PEQ stacks and cut depth lands 2-3× deeper. Documented the invariant in `CamillaDSPDriver.apply_input_eq`.

## [0.2.4] - 2026-05-25

### Fixed
- **`analyze_decay` T60 over-estimation by 4–15× resolved.** The bandpass-mode estimator was reporting T60 values longer than the impulse response itself (e.g. 1905 ms for a 47 Hz mode in a 500 ms IR). Root cause: Schroeder backward integration treated the IR's noise tail as remaining modal energy and inflated the integral. Validated 2026-05-25 against session 262 — manual T20×3 gave 117–308 ms vs algorithm's 1905 ms. Replaced with a direct envelope-based T20 estimator (`_estimate_t60_envelope`): Hilbert envelope → time-to -5 dB and -25 dB crossings post-peak → T60 = (t₂₅ − t₅) × 3. Includes a noise-floor sanity gate: if the median envelope after the -25 dB crossing sits above -25 dB, the crossing was noise modulation and the estimate is rejected (returns None — never extrapolates past the IR window). Six new unit tests cover synthetic recovery, truncated IRs, high/low noise floors, and the session 262 regression.

## [0.2.3] - 2026-05-25

### Fixed
- **USB-route cal coherence is now reproducible — engine deconvolves the mic recording against the captured loopback reference, cancelling PipeWire scheduling jitter as common-mode noise.** Before the fix, `MeasurementEngine` deconvolved the mic recording against the *analytical* sweep template (a sample-perfect digital reference with zero jitter). Under the PipeWire stack, sweep playback and mic capture are two separate client streams whose relative timing varies per run, so per-bin phase smear collapsed coherence. The `LoopbackRefPlayback` driver was already capturing an electrical reference in parallel; this change actually substitutes it in `_compute_fr_arrays`. Empirically: coherence improved from 0.05–0.6 (non-reproducible) to 0.94–1.00 across 20–200 Hz, with IR peak SPL from ~ -75 dBFS to ~ -22 dBFS (sessions 248 vs 255/256).

## [0.2.2] - 2026-05-25

### Fixed
- **USB-route sub calibration now works on the PipeWire audio stack.** After the v0.2.0 PipeWire migration, the container could no longer deliver sweeps to the subs via the USB route: PortAudio inside Docker only sees raw ALSA, the snd-aloop subdev used for the loopback ref is owned exclusively by PipeWire, and there was no PipeWire client in the image. The cascade kept the audio router as PipeWire end-to-end and restored the path:
  - Dockerfile installs PipeWire **client** libs (`pipewire`, `libpipewire-0.3-0`, `libspa-0.2-modules`, `pipewire-alsa`, `pipewire-pulse`, `libasound2-plugins`). No daemon, no WirePlumber — the container is a PipeWire client of the host session.
  - Systemd unit (`deploy/avr-calibration.service` + `deploy/install.sh`) bind-mounts `/run/user/1000` and exports `XDG_RUNTIME_DIR` / `PIPEWIRE_RUNTIME_DIR` so the container speaks to the host PipeWire socket; `--ipc=host` retained.
  - New host service `avr-cal-sweep-link.service` (`deploy/avr-cal-sweep-link.sh`) creates a persistent PipeWire null sink `avr_cal_sweep` and links its monitor ports to `camilladsp_capture:input_1/2`, and also fans the same monitor into the `snd-aloop` sink so the existing loopback ref bridge captures the sweep electrically as a timing reference. Idempotent — re-running is safe; existing links and pre-loaded sink are detected and skipped.
  - `MeasurementEngine.measure(route="usb")` resolves `playback_device` against PortAudio first (legacy direct-miniDSP setups still work). When no direct match is found, it pins `sd.default.device` to the ALSA `default` PCM (which pipewire-alsa hooks) and sets `PIPEWIRE_NODE=<playback_device>` so the OutputStream routes to the configured PipeWire node.
  - `install.sh` default `config.yaml` now writes `playback_device: "avr_cal_sweep"`.
  - CamillaDSP capture device is **unchanged** — still PipeWire-shaped from the v0.2.1 fix.

## [0.2.1] - 2026-05-25

### Fixed
- **CamillaDSP driver defaults are PipeWire-shaped**, matching the v0.2.0 audio-stack migration. The driver's `_DEFAULT_CAPTURE_DEVICE` / `_DEFAULT_PLAYBACK_DEVICE` were still ALSA (`type: Alsa`, `hw:USB,0,0` / `hw:Loopback,1,0`, explicit `S32_LE`). When `start_calibration` reset DSP state, `_build_config` → `SetConfig` pushed those ALSA-shaped devices into the running PipeWire daemon; the audio thread died silently, CamillaDSP went to Inactive, `camilladsp-watchdog.sh` issued `systemctl restart camilladsp`, the restart hung in `deactivating`, and every subsequent SetConfig hit `RateLimitExceededError` — killing the in-progress calibration session. Defaults now use `type: PipeWire` with `node_name` + `autoconnect_to` pointing at the Scarlett 18i20 multichannel PipeWire nodes (format is negotiated via WirePlumber, no explicit `format` key).

## [0.6.10.2] - 2026-05-23

### Added
- **Loopback reference timing for sub sweep path.** When `loopback_ref_device` is configured in `config.yaml`, each sweep now records two derived timing floats alongside the UMIK measurement: `avr_processing_ms` (AVR DSP delay, from xcorr of sweep template against Scarlett IN3) and `loopback_xcorr_peak_ms` (CamillaDSP latency + acoustic travel, from xcorr of IN3 against UMIK). No new hardware required — Scarlett IN3 is already wired to the AVR LFE pre-out. A sanity check logs whether the sum of both values matches the existing full round-trip `xcorr_peak_ms`.

### Changed
- **`get_fr_summary` MCP tool** now exposes `loopback_xcorr_peak_ms` and `avr_processing_ms` when present in a stored measurement.

### Removed
- **`cal_mode` concept fully deleted.** The routing concept was already absent from `measurement_profiles.py`; this removes all remaining references in MCP server comments, the `preflight.py` docstring, `docs/measurement-chain.md`, and the core bass/mains calibration recipes. The `test_default_profiles_no_cal_mode` regression guard is retained.
- **Measurement-chain docs** rewritten to reflect the HDMI-only signal path; the "four physical routes" section and `cal_mode` schema block are gone.

## [0.6.10.0] - 2026-04-30

### Added
- **Per-speaker `sweep_range_hz` in config.yaml** drives sweep band selection. Each entry under `speakers:` and `sub:` can now declare `sweep_range_hz: [lo, hi]`. The `measure` MCP tool resolves the right band when given a `target` parameter (e.g. `target="mains"` → 60-20000 Hz, `target="subs"` → 15-150 Hz, `target="FL"` → looks up the main speaker's range). Avoids the recipe author having to remember which range to use per speaker class.
- **`measure` tool gained `target`, `freq_min`, `freq_max` parameters.** Resolution order: explicit `freq_min`/`freq_max` (any one of them, partial overrides supported) → `target` → speaker config → global `measurement.freq_min`/`freq_max` defaults. The default behaviour (no params) is unchanged: 20-200 Hz sub-only sweep.
- **`MeasurementEngine.measure()` accepts `freq_min`/`freq_max` overrides.** Engine falls back to config defaults when None is passed.
- **New helper `_resolve_sweep_range()`** with 16 unit tests covering explicit overrides, partial overrides (only `freq_min`), target-based lookups (subs / mains / atmos / position codes / aliases), fallback when no target / no speaker has the range configured, and the diagnostic `source` string returned alongside.

### Why
Mains play 48 Hz - 32 kHz. The default 20-200 Hz sweep was sub-only and produced essentially no useful data above the crossover. Mains calibration via the AVR's MultEQ filter banks (the SET_COEFDT path landed in v0.6.9.7-8) needs full-range FR data to design filters across the audible band. With this change a recipe can do `measure(target="FL")` and Just Get Right.

### Notes for callers
- The 1/3-octave summary returned by `get_fr_summary` is currently still pinned to 20-200 Hz bins. Wider sweeps capture full data into the session store; the summary will be extended to 20-20000 Hz in a follow-up.

## [0.6.9.9] - 2026-04-30

### Fixed
- **Preflight `check_denon` now fails loudly when AVR is in standby.** The Denon HTTP service responds in standby — `state["connected"] == True` does not mean "ready to play audio." If `power != "ON"`, Telnet replies vanish silently and sweep measurements come back at SNR = 0. Surfaces a clear "AVR is in standby" failure instead of passing the check. Caught after wasting 30 minutes assuming the AVR was on.
- **`DenonDriver.get_state` now reports `power`** (was missing). Callers can guard before sending Telnet/sweep commands.

### Added
- **`DenonSweepContext` auto-powers-on the AVR.** If the AVR is in standby when a sweep context is entered, the context now calls `async_power_on()` and waits up to 10 s for `power == "ON"` before yielding. Sweeps that previously came back silent now either succeed or fail with a clear "AVR did not report power=ON within 10 s" error.
- **`DenonDriver.async_power_on()`** — public helper to power on the AVR + wait for ON.
- **`DenonDriver.telnet_query()`** — loud-fail wrapper for Telnet command sequences. Verifies `power == "ON"` first (configurable), sends commands, raises `DriverError` with a diagnostic message if all replies are empty (which happens silently in standby or with an exhausted Telnet pool). Replaces the silent-empty-string pattern that hid the standby bug.

## [0.6.9.8] - 2026-04-30

### Added
- **`calibrate/drivers/denon/audyssey_filter_upload.py`** — full Audyssey TCP upload orchestration. `query_avr_status()` runs ENTER_AUDY + GET_AVRINF + GET_AVRSTS introspection. `build_set_dat_envelope()` constructs the 16-field ordered SET_SETDAT payload from a .ady file + GET_AVRSTS response, with correct types (booleans not strings, integers not strings, capital-Q in `AudyMultEQ`). `chunk_setdat_payload()` splits envelopes under the 510-byte AVR threshold preserving canonical field order. `push_avr_filters()` runs the full upload sequence: ENTER_AUDY → chunked SET_SETDAT → SET_COEFDT streams per channel × tc × sr → FINZ_COEFS → AudyFinFlg=Fin commit → EXIT_AUDMD. INIT_COEFS is auto-detected from `DType` (only sent when fixed-point — X3800H is float, skip).

- **MCP tools `design_avr_fir` + `apply_avr_fir`** — design + push AVR-format FIR coefficients via the Audyssey TCP path, fully scriptable from a recipe.
  - `design_avr_fir(channel_id, target_curve_db, cache_key)` — takes a target FR curve (list of `{freq_hz, gain_db}` points), runs `design_correction_ir → convert_xt32`, caches the 1024 (speaker) / 704 (sub) AVR coefficients keyed by `(cache_key, channel_id)`.
  - `apply_avr_fir(host, ady_path, cache_key, ...)` — loads cached coefficients per channel, builds the full envelope from .ady + AVR introspection, pushes via `push_avr_filters`. Supports `distances_override_m` for the variance-cap bypass and `target_curves` / `samplerates_hz` for limiting the upload to specific banks.

- **`scripts/smoke_test_filter_upload.py`** — end-to-end pipeline smoke test against a live AVR with no audio sweeps. Default `--dry-run` mode introspects + builds envelopes + counts packets without writing. `--transmit` mode actually pushes (with a typed-confirmation prompt) — useful for verifying the full protocol works before the first measurement-driven calibration.

- 22 new tests in `test_audyssey_filter_upload.py` covering field-order preservation, bool/int type-correctness for picky firmware fields, distance overrides, chunker behaviour, and `parse_frames` round-trip. 12 new tests in `test_mcp_avr_fir_tools.py` for the MCP tool layer (caching, validation, mocked TCP push).

### Notes for callers
- The full SET_SETDAT envelope is now sent (16 fields) — `apply_avr_fir` does not have the FR-drift side effect that `set_speaker_distances(use_custom=True)` exhibited. EQ-related fields (AudyDynEq, AudyEqRef, AudyMultEQ, etc.) are sent explicitly to the values that match A1Evo Acoustica's post-cal defaults.
- The `AudyFinFlg=Fin` commit at the end of `apply_avr_fir` writes to the AVR's NVRAM. There is no volatile mode for filter coefficients.
- Caller MUST NOT enter Manual Setup > Distances on the AVR after a successful push — that triggers re-validation that snaps Distance back to the variance cap.
- Recovery from a botched push: re-upload the original `.ady` via the MultEQ Editor app's "Send to AV receiver" button.

### Verified end-to-end (2026-04-30)
- `query_avr_status` against X3800H returns AmpAssign="Normal", EQType="MultEQXT32", DType="Float", CoefWaitTime.Final=15000.
- `build_set_dat_envelope` from a real .ady + GET_AVRSTS produces a 1619-byte payload that chunks cleanly into 5 sub-510-byte SET_SETDAT packets.
- `all_streams_for_channel` produces 522 SET_COEFDT packets for a 9-speaker + 1-sub setup × 2 target curves × 3 sample rates.
- The actual `--transmit` path against the AVR is gated on a typed confirmation in the smoke-test script — not exercised in this commit.

## [0.6.9.7] - 2026-04-30

### Added
- **`calibrate/audyssey_fir.py`** — XT32 polyphase FIR designer. Generates a 16,321-tap (speaker) or 16,055-tap (sub) impulse response from a target FR curve, then polyphase-decimates 4-band to the AVR's expected 1024 / 704 coefficient vector. Math ported with attribution from `srinivas486/audyssey-rew-tuner` (MIT-licensed). Includes channel-byte mapping table for SET_COEFDT routing, sample-rate codes (32k/44.1k/48k/96k), and `design_correction_ir` / `design_passthrough_ir` helpers.
- **`calibrate/drivers/denon/audyssey_coef_transfer.py`** — SET_COEFDT packet builder. Frames 1024/704-float vectors into the AVR's variable-length packet stream (1×127 floats first, ~7×128 mid, last partial — total 9 packets per stream for speakers, 6 for subs). Builds one stream per (target_curve × sample_rate) tuple — XT32 expects 6 streams per channel (Reference + Flat × 3 sample rates), so 54 packets for a speaker / 36 for a sub.
- **`audyssey_tcp.push_filter_set`** — full upload orchestrator. Sequences ENTER_AUDY → SET_SETDAT(envelope, AudyFinFlg=NotFin) → coefficient streams (no ACK) → FINZ_COEFS → SET_SETDAT(AudyFinFlg=Fin) → EXIT_AUDMD. Sync core (`_push_filters_sync`) + async wrapper. Configurable inter-packet / inter-channel pacing and final-coef wait (default 15 s for X3800H). Returns per-stage status. **Wire-tested via mocked socket only — not yet hardware-validated.** No MCP tool exposes it yet.
- 50 tests across `tests/test_audyssey_fir.py`, `tests/test_audyssey_coef_transfer.py`, `tests/test_audyssey_tcp.py` — polyphase decomposition round-trip, multi-rate output-length checks, packet framing + checksum + LE float32 round-trip, all-streams-per-channel counts, full-upload sequence ordering with mock socket.

These are the building blocks for direct-uploading custom FIR coefficients to the AVR — the protocol layers needed for an LLM-driven mains calibration loop. **Still not in this version:** MCP tools (`design_avr_fir` / `apply_avr_fir`) that wire these into Claude's calibration loop, plus the AVR-state introspection (GET_AVRINF + GET_AVRSTS) that builds the full 16-field SET_SETDAT envelope. Hardware validation of the upload path is also pending.

## [0.6.9.6] - 2026-04-30

### Added / Changed
- **Audyssey distance variance-cap bypass (OCA-style envelope), verified on X3800H:** the firmware re-validates a `Distance`-only SET_SETDAT on `EXIT_AUDMD` and clamps the applied delay variance to ~38 ms (matches UI 18 m / 60 ft cap). Including `AudyFinFlg: "NotFin"` in the same packet, then a separate `AudyFinFlg=Fin` commit before `EXIT_AUDMD`, tells the firmware "this is a complete write, not a partial poke" — and the larger Distance values stick. Empirically the envelope extends the applied-delay ceiling to ~55 ms (still capped, just higher). Confirmed end-to-end via SW1=20m sweep with sub-vs-mains phase-slope going from +10.6 ms (subs trail) to -3.93 ms (subs slightly lead) and per-band cancelling bands dropping from 2 → 0.

  Implementation: `audyssey_tcp.build_envelope_distance_payload` returns `{"Distance":[...], "AudyFinFlg":"NotFin"}`; `push_speaker_distances(use_custom=True)` now sends that payload and forces `commit=True`. Old `build_custom_distance_payload` (which targeted a non-existent `customDistance` wire field) is now an alias for the working envelope builder.

- **`set_speaker_distances` MCP tool docstring updated** to describe the envelope bypass behaviour, the 38 ms vs 55 ms cap distinction, and the FR-drift side effect from the minimal envelope (mids may shift ±5-10 dB because AudyMultEq/AudyEqRef/AudyEqSet aren't carried). For full-state preservation, callers should switch to `scripts/audyssey_push_full_envelope.py` once it gains AVR-state introspection.

### Removed
- `build_custom_distance_payload` no longer constructs the broken `Distance=0 + CustomDistance` payload (that field doesn't exist on the wire — it's a .ady-file-only artifact). The name remains as a back-compat alias for `build_envelope_distance_payload`.

## [0.6.9.5] - 2026-04-29

### Added
- **`samplerate` param on `design_modal_fir`:** previously the tool hardcoded `sample_rate=8000` regardless of the CamillaDSP processing rate. When the daemon runs at 48 kHz native (no internal resampler), the 8 kHz coefficients applied 1:6 — modal frequencies shifted 6× higher, anti-pulse positions garbled. The new `samplerate` param (default 8000 to preserve backward behavior) plumbs through to `ModalAwareFIRDesigner(sample_rate=...)`. Use `samplerate=48000, num_taps=24576` to match the `8000 + 4096` 512 ms filter window at 48 kHz native. Function signature, tool inputSchema, and dispatcher all updated.

## [0.6.9.4] - 2026-04-29

### Changed
- **`analyze_ir` description + docstring tightened to flag cross-path misuse:** the tool's `peak_time_s` only equals acoustic travel time when the compared measurements share an identical processing chain. With a long FIR (e.g. 4096-tap modal-FIR @ 48 kHz ≈ 85 ms window) on the sub chain, the detected peak sits inside the FIR's non-causal region — its absolute value reflects FIR shape and buffer latency, not arrival time. Every cross-path use (sub-vs-mains, FIR-chain vs no-FIR-chain, cal-mode vs HDMI) was returning misleading numbers without warning. Docstring + tool description now name the valid (solo-sub) and invalid (cross-path) use cases explicitly and route cross-path callers to `compare_sub_phase` or the loopback alignment rig.

### Added
- **`cross_path_warning` field in `analyze_ir` response:** when `peak_time_ms` exceeds 80 ms (well beyond any realistic solo-sub acoustic range), the response includes a string warning that the value reflects FIR/buffer latency rather than acoustic arrival, and that cross-path comparisons against this number are invalid. Solo-sub callers (peak in normal 0–30 ms range) see `cross_path_warning: null` and behave unchanged.

## [0.6.8.4] - 2026-04-28

### Fixed
- **IR onset detection no longer locks onto room-mode resonance:** when a sub sits in a room null at the listening position, the direct arrival can be heavily attenuated and the resonance build-up that follows becomes the largest |ir| feature. The previous primary path used the cross-correlation envelope's `argmax` for `peak_time_ms`, which then reported the resonance time (e.g. 167 ms) instead of the actual time-of-flight (e.g. ~5 ms). Two solo subs measured under that geometry could appear to be 115 ms apart when the real delta was a few ms — entirely a peak-detector artifact. The fix runs IR-domain onset detection (skip 1 ms of DC region, find first sample crossing −20 dB from the in-window peak) on every measurement instead of trusting the xcorr envelope's argmax. Polarity still reads from the dominant impulse so the existing alignment heuristic doesn't change.

### Added
- **Preflight `Audio stack` check:** detects PipeWire/wireplumber/PulseAudio holders on `/dev/snd` controls. cal-mode routing is unreliable when these userspace audio managers are active — they hold ALSA control handles and may auto-route audio between sinks unpredictably. Reports a failure with the disable command when offenders are found.

## [0.6.8.3] - 2026-04-28

### Fixed
- **cal_mode silently routed sweep through the AVR instead of CamillaDSP loopback:** the cal-mode override looked up the ALSA device string ``hw:Loopback,0,0`` against PortAudio device names via literal substring match, but PortAudio renames ALSA hw devices using the numeric card index — the actual name is ``"Loopback: PCM (hw:2,0)"`` (where 2 is the ALSA card index for snd-aloop). Match returned zero, the code logged a "no match — falling back" warning, and ``sd.default.device[1]`` (system default = HDMI/AVR or whatever) received the sweep. Every cal-mode measurement was bypassing CamillaDSP entirely, producing FR/IR data that reflected the AVR-driven mains rather than the DSP path. Symptoms that flagged the bug: "out5 solo" and "out6 solo" gave byte-identical FR at 11/11 bands, combined ≈ solo (no constructive sum), both subs muted still produced a strong signal. Fixed by adding ``_resolve_alsa_device_in_portaudio`` which reads ``/proc/asound/cards`` to map card names to indices and matches PortAudio's ``"(hw:N,M)"`` suffix. Cal-mode now raises ``RuntimeError`` instead of silent fallback when the resolver fails — broken routing must be loud.

## [0.6.8.2] - 2026-04-28

### Fixed
- **Coherence metric was meaningless for swept-sine measurements:** the previous implementation called `scipy.signal.coherence` (Welch's method) on the sweep stimulus and recording. Welch averages cross-power across time segments and assumes the signal is stationary. A swept sine is the opposite — at any given frequency bin only one segment of the sweep contains real signal; the other 28 contain only noise. The averaged cross-power gets diluted while the recording's autopower stays inflated by ambient noise, pinning reported coherence near zero independently of measurement quality. The effect was worst at low frequency (where each band is visited briefly during a log sweep) — exactly where reliability mattered most for sub work. Replaced with a per-bin SNR derived from the early IR window (signal) vs the late IR tail (noise floor), mapped to a [0, 1] reliability via the standard γ² = SNR/(1+SNR). This is what REW and Smaart report as "coherence" for swept-sine measurements.

## [0.6.8.1] - 2026-04-28

### Fixed
- **`set_cal_mode` no longer clobbers externally-applied FIR coefficients:** Toggling cal-mode used to rebuild the entire CamillaDSP pipeline from the driver's shadow state. FIRs written to the daemon by another path (e.g. an external `SetConfigJson` call to dodge the token cost of passing 4096-tap arrays through MCP) were not in shadow state and got silently reverted to the driver's last-known coefficients. The driver now syncs Conv `cal_out{N}_fir` filters from `GetConfigJson` into shadow state before the push, so externally-applied FIRs survive the toggle. Defensive: failed `GetConfigJson` calls log a warning and the swap still completes.

## [0.6.8.0] - 2026-04-24

### Fixed
- **`fit_correction_filter` preserve_mean + max_boost_db=0 conflict:** When both constraints are active, the optimizer was degenerate — `preserve_mean` penalises net-downward corrections while `max_boost_db=0` prevents compensating boosts, producing poor filter selection (only 3 filters, minimal improvement). The tool now auto-suppresses `preserve_mean` in this case and reports `preserve_mean_suppressed: true` in the response so the caller is aware.
- **`fit_correction_filter` auto-anchor divergence causing forced boosts:** When `target_offsets` is used, `anchor_target` can set `reference_spl` well above the measured SPL at the reference frequency, forcing the optimizer to place boost filters just to reach the anchor, producing doublets. The tool now clamps the anchor to measured SPL when `max_boost_db=0`, and emits `anchor_warning` with the divergence amount whenever the anchor is more than 3 dB above measured at the reference frequency.

## [0.6.7.0] - 2026-04-23

### Fixed
- **IR peak jitter on ALSA/CamillaDSP pipelines:** `measure()` now strips the pre-sweep portion of the recording using the cross-correlation-derived sweep-start sample rather than a wall-clock `PRE_DELAY_S × sample_rate` offset. ALSA/PortAudio stream startup is non-deterministic (20–50 ms of variable latency on Loopback + CamillaDSP), so wall-clock stripping removed a variable amount of real sweep and produced matching IR-peak-time jitter. Cross-correlation locates the sweep itself and is immune to the stream-startup race. Diagnostic log line reports expected-vs-actual offset so you can see stream-start drift per run.

### Changed
- **`validate_recording` return signature:** now returns `tuple[list[dict], int]` — the second element is the sweep-start sample index derived from the existing FFT cross-correlation pass (no extra work). Callers must unpack; previously returned just the warnings list.

## [0.6.6.0] - 2026-04-12

### Changed
- **IR onset detection extracted to shared function:** `detect_ir_onset()` replaces duplicated 18-line blocks in `measurement.py` and `mcp_server.py`. Both `compute_session_metadata` and `_tool_analyze_ir` now call the same function, eliminating drift risk.
- **Named constants for onset detection:** `IR_ONSET_SKIP_S` (2 ms DC artifact blanking) and `IR_ONSET_THRESHOLD_DB` (-20 dB onset threshold) replace inline magic numbers.
- **IR storage length respects sample rate:** Previously hardcoded to 24,000 samples (assumes 48 kHz). Now uses `IR_GATE_S * sample_rate`, correct at any rate.
- **IR taper duration is a named constant:** `IR_TAPER_S` replaces the magic `0.05` in the deconvolution gate window.

### Removed
- **Dead `post_delay` parameter on `_run_minidsp_cli`:** Never passed by any caller. CLI delay now always uses `CLI_COMMAND_DELAY_S` directly.

## [0.6.5.0] - 2026-04-11

### Added
- **`calibrate_level` controls master gain in USB mode:** Previously the USB path only checked SNR and returned — it never touched the miniDSP master gain, so calibration could start at 0 dB (factory default) and measure at full blast. Now sets master gain to `start_db` (default −10 dB) before measuring, then steps it down if `peak_spl` exceeds `max_spl_dbfs` (default 0 dBFS). Saves the calibrated master gain to config on success. HDMI mode behavior unchanged.

### Fixed
- **Recipes measured through invisible prior EQ:** All three calibration recipes now start with an explicit EQ-clear step (HPF-only on each sub output) before any measurements begin. `read_eq` only tracks in-memory state since server start — after a restart it returns `[]` while old filters remain on the hardware. Clearing explicitly guarantees the baseline measurement reflects the true room response.
- **Recipe EQ iterations discarded prior corrections:** The "re-measure and iterate" sections in all three recipes were ambiguous — an AI following them would compute a delta correction and call `apply_eq` with just the delta, silently overwriting previous iterations. All iteration sections now explicitly say: call `read_eq` to get the current filter set, merge the new correction in, and call `apply_eq` with the full merged set.

## [0.6.4.0] - 2026-04-10

### Added
- **Target curve stored with each measurement session:** Measurements now capture the active optimization target (Harman, flat, etc.) at the time of the sweep. The dashboard shows dB RMS deviation against the actual calibration reference — not a post-hoc best-fit. Sessions taken outside of a calibration run show no delta.
- **FR filtering/decimation for history and summary MCP tools:** `get_measurement_history` and `get_fr_summary` now accept `freq_min`/`freq_max` band limits and a `max_points` cap, reducing data volume in LLM context for bass-focused calibration workflows.

### Fixed
- **Dashboard charts never displayed after clicking a session:** `renderChart()` referenced an undeclared `targetLine` variable inside the port tune marker path. Since port tune is always set (22 Hz), every chart render threw a silent `ReferenceError` swallowed by `loadSession()`.
- **Comparison curves aligned 14 dB below the measurement:** The comparison reference used `storedTarget.reference_spl` (the DSP calibration reference, around −7 dBFS) instead of the measurement's own SPL at 80 Hz. Now anchored to the session's actual level.
- **No remove button for overlaid comparison curves:** Added × chips for comparison lines (Harman, flat) alongside the existing overlay chips.
- **dB RMS context lost when switching sessions:** The dashboard now sets/clears the target reference from each session's stored data rather than the global DSP state, so each historical session shows the target it was actually measured against.

## [0.6.3.0] - 2026-04-10

### Fixed
- **Input PEQ had zero effect on sweep measurements:** Two bugs: (1) `apply_input_eq` was writing to `active_input=1` (the analog Denon LFE path) but calibration sweeps use USB source routed through `usb_input=0` — the PEQ was on the wrong input and never touched the sweep signal. (2) `reapply_volatile_output_state` explicitly skipped input PEQ with a "not volatile" comment, so even if the filter had been on the right input, any source switch (analog → USB at sweep start) would wipe it and it would never be restored.
- **Fix:** `apply_input_eq` now writes to all active signal paths (union of `usb_input` and `active_input` — both inputs 0 and 1 in this setup). `reapply_volatile_output_state` now restores input PEQ alongside output PEQ after source switches. `MinidspDriver` constructor now accepts `usb_input` (populated from `output_channel` in config via `registry.py`).
- **FR resolution guidance added to calibrate skill:** Added explicit rule that `get_measurement_history` (983 pts, ~0.18 Hz spacing) must be used for filter design and verification. `get_fr_summary` (11 1/3-octave bands) is too coarse to resolve narrow peaks (Q > 2) and is for quick convergence checks only.

## [0.6.2.0] - 2026-04-09

### Fixed
- **miniDSP HTTP writes removed:** All writes to the miniDSP 2x4 HD now go through the `minidsp` CLI instead of the HTTP REST API. The HTTP API resets routing and PEQ biquad state when a CLI session opens, and has a sign bug in `a1`/`a2` coefficients that causes DSP hangs requiring a physical power-cycle. CLI is the only safe write path.
- **Three USB measurement fragility bugs in MinidspSweepContext:** (1) Source switch is now skipped when the device is already on USB, saving ~2 s per measurement. (2) Source-switch restores (mutes, gains, PEQ) now only run when the source actually changed — prevents unnecessary state churn. (3) `__aenter__`/`__aexit__` log messages now distinguish "source skipped" from "source switched" for clearer debugging.
- **Volatile output state lost after USB source switch:** The miniDSP 2x4 HD resets output mutes, gains, and PEQ biquads when the source is switched. `MinidspSweepContext` now calls `reapply_volatile_output_state()` after each source switch, restoring all calibration EQ so sweeps include the correct filter state.
- **Measurement event-loop blocking:** `play_and_record()` (blocking PortAudio I/O) now runs in a thread-pool executor via `loop.run_in_executor()` so the asyncio event loop stays responsive during sweeps. A 60-second `asyncio.wait_for` timeout raises `RuntimeError` if the audio device hangs.
- **Concurrent measurement race:** `MeasurementEngine.measure()` is now serialized by an `asyncio.Lock` to prevent concurrent calls from clobbering `sd.default.device` (a sounddevice global state).

## [0.6.1.0] - 2026-04-07

### Fixed
- **USB sweep buffer truncation:** Recording buffer was sized to `n_sweep_samples` only. With a 1-second pre-delay, the buffer filled before the sweep finished, truncating the cross-correlation and collapsing SNR. Buffer is now sized to `pre_samples + n_sweep_samples + post_samples`.
- **SNR check argmax bug:** `validate_recording` used `np.argmax(abs(recording))` to locate the sweep, which breaks when the UMIK clips or a room transient occurs at recording start. The first max-value sample lands in the floor window, making `signal_rms ≈ floor_rms → SNR ≈ 0 dB`. Fix: use the cross-correlation lag already computed in the sweep-capture check.
- **MinidspSweepContext silent failures:** `__aenter__` wrapped setup in a try/except that swallowed CLI errors (routing, source switch, mute restore). Failed setup silently passed a garbage signal path to `measure()`. Fix: remove try/except — setup failures now propagate loudly.
- **USBPlayback error handling:** Stream operations now catch any audio device error and re-raise as `RuntimeError("Audio device error: ...")` for consistent caller interface.
- **mcp_server MinidspSweepContext wiring:** Pass `driver=_dsp` so the context shares the driver's in-memory mute state.

### Added
- **`docs/audio-debugging-lessons.md`:** Hard-won USB measurement debugging lessons (10, 11, 12) covering buffer sizing, xcorr SNR, and context manager fail-loud rules.
- **MinidspDriver mute tracking:** `mute_outputs`/`unmute_outputs` now update `_output_muted` dict; `get_mute_state()` exposes it.
- **MinidspSweepContext:** Full USB sweep lifecycle — source switch, routing, mute restore — via `_get_source_via_cli`, `_configure_routing_via_cli`, and `_restore_driver_mutes`.

## [0.6.0.0] - 2026-04-07

### Added
- **FIR filter support:** `apply_fir` and `clear_fir` MCP tools write FIR coefficients to the miniDSP 2x4 HD (2048 taps/output, 96kHz). Claude can now design time-domain filters that shorten room mode decay, not just cut peak magnitude.
- **Decay analysis:** `analyze_decay` MCP tool runs Schroeder integration on stored impulse responses to identify ringing room modes (T60 > threshold), prioritize them, and recommend EQ Q values. Exposes `freq_hz`, `t60_ms`, `peak_db`, `suggested_q`, and `priority` per mode.
- **IR analysis:** `analyze_ir` extracts `peak_time_s`, `peak_sign`, and `peak_spl_db` from stored sessions so Claude can compute inter-sub delay offsets and polarity inversions without custom Python.
- **Output state:** `get_output_state` returns in-memory per-output tracking (gain, delay, polarity, fir_taps) accumulated since server startup — necessary because minidspd has no GET endpoint for these parameters.
- **Output gain:** `set_output_gain` sets per-output gain in dB directly from Claude, completing the sub level-match workflow.
- **`get_config` FIR capabilities:** `eq_capabilities` now includes `fir_capable`, `fir_max_taps_per_output`, `fir_shared_tap_pool`, and `fir_sample_rate_hz` so Claude can gate FIR vs PEQ recommendations on device capability.

### Changed
- **`configure_matrix` safety label:** Tool descriptor now carries the signal-path write warning, consistent with other routing tools.
- **Per-sub recipe phase 1 and 2:** `harman-bass-persub.md` updated to call `analyze_ir` after each solo measurement for delay/polarity computation and `analyze_decay` after per-sub EQ for ringing mode detection.
- **`/calibrate` skill:** Recipe picker now recommends hardware-aware recipes and lists all new MCP tools (`get_output_state`, `analyze_ir`, `analyze_decay`).

### Fixed
- **FIR exit code:** `minidsp fir import` and `fir clear` return exit code 1 even on success (minidsp-rs#766). CLI wrapper now ignores exit code 1 for these commands — previously every `apply_fir` call raised a `DriverError` even when the hardware loaded the coefficients correctly.
- **FIR lock:** `apply_fir` and `clear_fir` now acquire `self._lock`, consistent with `apply_eq`. Concurrent `apply_eq + apply_fir` calls would otherwise interleave CLI writes and corrupt device state.
- **FIR state rollback:** `_fir_state` is now updated only after all hardware writes succeed, matching the P0 rollback pattern in `apply_eq`.
- **`clear_fir` passthrough:** `clear_output_fir` now explicitly sets `fir bypass off` after clearing, ensuring deterministic passthrough state regardless of firmware behaviour post-clear.
- **Decay analysis broadband reference:** `broadband_energy` in `analyze_decay` now excludes near-zero bins. Previously, measurements with sparse energy could produce reference levels near zero, generating spurious +300 dB peak values that would misdirect EQ decisions.
- **Python orchestrators removed:** `run_alignment_phases()` and `run_full_alignment()` — incomplete Python orchestrators with hardcoded placeholder values — deleted from `alignment.py`. Claude drives the calibration loop via MCP tools + recipes.

## [0.5.1.0] - 2026-04-05

### Added
- **New MCP tools:** `set_delay`, `set_polarity`, and `check_system` for sub alignment and pre-flight hardware checks.
- **Split mute/unmute:** `mute_output` and `unmute_output` replace the combined `mute_sub_outputs` tool for cleaner per-sub isolation during calibration.
- **6 new Claude skills:** `/setup` (equipment config), `/recipe` (interactive recipe builder), `/subcrawl` (sub placement optimization), `/measure` (single measurement + analysis), `/check` (pre-flight system check), `/status` (current state review).

### Changed
- **Tool renames:** `trigger_measurement` → `measure`, `avr_set_volume` → `set_volume`, `mute_sub_outputs` → `mute_output`/`unmute_output`. Legacy names still dispatch correctly for backwards compatibility.
- **Removed deprecated `set_denon_volume` from tool list** (legacy alias kept in dispatch only).
- **`MinidspDriver`:** Added `set_output_delay` and `set_output_polarity` methods delegating to the adapter layer.

### Fixed
- **Defensive bool coercion:** `set_polarity` dispatch uses `is True` instead of `bool()` to prevent string "false" from coercing to True.

## [0.5.0.0] - 2026-04-01

### Added
- **Driver abstraction layer (`calibrate/drivers/`):** `AVRDriver` and `DSPDriver` abstract base classes decouple MCP tools from specific hardware brands. Adding a new AVR or DSP requires only a new subclass + one registry entry.
- **`DenonDriver`:** Wraps the `denonavr` library behind the `AVRDriver` protocol. Lazy setup (no network calls in constructor), asyncio.TimeoutError wrapped as `DriverError`.
- **`MinidspDriver`:** Wraps `MinidspClient` behind the `DSPDriver` protocol. Owns in-memory EQ state with an `asyncio.Lock` covering the full read→SafetyValidator→write→update sequence.
- **`avr_set_volume` MCP tool:** Brand-agnostic replacement for `set_denon_volume`. The old name is kept as a deprecated alias for backward compatibility with cached Claude Code sessions.
- **`DriverError` exception hierarchy:** All driver methods raise `DriverError`; MCP tools catch it and return `{ok: false}`. No raw hardware exceptions escape to the MCP protocol layer.
- **Starlette lifespan handler:** `setup()` and `close()` called on server start/stop for proper resource lifecycle.
- **`tests/test_drivers.py`:** 28 new unit tests covering `DenonDriver`, `MinidspDriver`, and the driver registry.

### Changed
- **`calibrate/mcp_server.py`:** Zero direct references to `denonavr` or `MinidspClient` — all hardware logic moved to driver layer. `_eq_state` dict moved from module-level into `MinidspDriver` instance.
- **`calibrate/config.py`:** Added `avr_driver` and `dsp_driver` config keys (defaults: `denon`, `minidsp`). Added `avr_driver_name` and `dsp_driver_name` typed properties. Atomic config write via `os.replace()` (prevents zero-byte config on Pi power loss).
- **`tests/test_mcp_server.py`:** Refactored to mock at driver level (`patch("calibrate.mcp_server._avr", ...)`) instead of patching `sys.modules["denonavr"]` and `_eq_state` directly.
- **`get_device_state` response:** Keys renamed from `denon`/`minidsp` to `avr`/`dsp` for brand-agnostic output.

### Fixed
- **P0: Partial EQ write rollback:** `MinidspDriver.apply_eq` only updates `_eq_state` after ALL hardware writes succeed. Previously a mid-loop `MinidspApiError` left state diverged from hardware, causing SafetyValidator to diff against a wrong baseline.
- **P0: Path traversal via symlinks:** `fetch_recipe` now calls `recipe_path.resolve().is_relative_to(RECIPES_DIR.resolve())` after the `".."` check, blocking symlinks inside `recipes/` that point outside it.

## [0.4.1.0] - 2026-03-31

### Added
- **Signal chain builder (Phase 1 redesign):** Replaces the three independent CRUD cards (Denon, miniDSP, Speakers) with a vertical signal-chain builder that traces the audio path from the Pi outward, node by node. The Pi root is shown as a fixed header; Denon and miniDSP appear as full cards below it.
- **Output slot speaker picker:** Each of the four miniDSP output slots has a "+ Add speaker" affordance that expands inline with label, room location (front-left, front-right, rear-left, rear-right, center, other), and speaker preset (SVS PB-12 NSD). Clicking × on a configured slot clears it.
- **SVS PB-12 NSD speaker preset:** First preset in the library. Extensible to additional models without schema changes.
- **Live connectivity badges with 5s timeout:** The Denon badge now uses `asyncio.wait_for(..., timeout=5.0)` so an unreachable AVR shows FAIL within 5 seconds instead of hanging. The frontend AbortController mirrors this with a 5.5s client-side limit.
- **Offline recovery UX:** When the Denon is unreachable, an amber warning row appears inline with a Retry button. The "Change host" affordance remains available.
- **Setup gate:** "Continue to Baseline" is disabled until Denon host is saved AND at least one output slot has a speaker configured.
- **`GET /api/signal-chain`:** Synthesizes the full chain from flat config + speakers. Includes migration tombstone: reads old `connections.minidsp.outputs` labels into `output_slots` on first load.
- **`POST /api/signal-chain`:** Writes `denon.*`, `minidsp.input_labels`, `minidsp.output_slots`, and **derives `measurement.sub_outputs`** from non-empty slots — keeping the calibration loop in sync with the new UI. Tombstones `connections.minidsp` to prevent dual-read.
- **`minidsp.output_slots` config defaults:** `DEFAULT_CONFIG` now includes four empty output slots so new users get a valid structure without a config migration.
- **13 new tests:** Cover GET/POST /api/signal-chain, migration tombstone, sub_outputs derivation, preset/location round-trip, setup gate logic, and config defaults.

### Changed
- `DEFAULT_CONFIG.minidsp` now includes `input_labels: {}` and `output_slots: [4 empty slots]`.
- Equipment Setup phase no longer stores speaker room location in `equipment.data` — `output_slots[].location` in config.yaml is the single source of truth.

### Fixed
- `equipment_denon_state()` now wraps Denon AVR calls in `asyncio.wait_for(..., timeout=5.0)`, preventing the phase from hanging when the AVR is offline.

## [0.4.0.0] - 2026-03-31

### Added
- **Equipment Setup page (Phase 1):** Replaces the old Room Setup + Equipment Verification two-step flow with a single Equipment Setup phase. Three cards — Denon AVR, miniDSP 2x4 HD, and Speakers & Subs — let you configure and save your full signal chain before calibration.
- **Denon AVR card:** SSDP auto-discovery finds your AVR on the LAN with one click. After discovery, the live input list is pulled directly from the AVR so you can select which input the Pi HDMI cable is connected to. A test tone (440 Hz via Pi HDMI) lets you confirm the input by ear, then saves `denon.host` and `measurement.denon_sweep_input` to `config.yaml`.
- **miniDSP 2x4 HD card:** Label each of the four inputs and four outputs (e.g. "Denon LFE L", "SVS PB12-NSD"). Labels are saved under `connections.minidsp.inputs/outputs` in `config.yaml` for use in the signal path block diagram.
- **Speakers & Subs card:** Add any speaker or subwoofer with a flexible JSON data blob — type, label, room location, port tune frequency, or any future field you want. Stored in a new `equipment` SQLite table with full CRUD via `/api/equipment/speakers`.
- **Signal path block diagram:** The miniDSP card now renders a live input/output block diagram with per-channel level meters (dBFS bars), updated every 2 seconds. Polling stops automatically when you navigate away from the phase.
- **`connections` config property:** `Config.connections` exposes the new `connections` section in `config.yaml` for miniDSP I/O labels and Denon connection info.
- **`update_config()` helper:** Deep-merges a partial dict into `config.yaml` preserving all unrelated keys. Used by all new equipment save endpoints.
- **New API endpoints:** `GET/POST /api/equipment/denon/state`, `/api/equipment/denon/discover`, `/api/equipment/denon/save`, `/api/equipment/denon/test-input`, `/api/equipment/minidsp/save-labels`, `GET/POST/PUT/DELETE /api/equipment/speakers`.

### Fixed
- `update_equipment()` with no fields now returns the existing row instead of `None`, preventing a spurious 404 from the PUT endpoint when the request body contains only `type` (no label/data to update).
- `denonDiscover()` now checks `r2.ok` before parsing the state re-fetch response, so a 500 on the state endpoint shows a proper error instead of silently leaving the input dropdown empty.
- Level meter polling timer (`_spLevelTimer`) is now cleared when navigating away from Phase 2, eliminating continuous `/api/signal-path/device-state` requests while on other phases.
- `deleteSpeaker()` now checks the HTTP response status and surfaces errors to the user instead of silently reloading after a failed delete.
- `equipment_minidsp_save_labels` now merges input/output labels into the existing `connections.minidsp` object instead of replacing it, preserving any other fields in that config section.
- Corrupt `data` JSON in an equipment row now logs a warning instead of silently returning `{}`.

## [0.3.1.3] - 2026-03-30

### Fixed
- **miniDSP preflight passes with minidspd running:** `check_minidsp_combined()` now treats the minidspd HTTP daemon as the authoritative check. Previously it required `/dev/hidraw0` to exist, but minidspd claims the device via libusb/usbfs which intentionally detaches `hid-generic` — so `hidraw0` is absent while the daemon is healthy. The hidraw check is now only used as a diagnostic fallback when the daemon itself fails, to distinguish "device not plugged in" from "daemon not running".
- **miniDSP version field null crash:** `device.get("version", {})` returned `None` when minidspd reports `"version": null` (key present but null). Fixed with `(device.get("version") or {})` so the default applies regardless.

## [0.3.1.2] - 2026-03-30

### Added
- **Version chip (top-right corner):** Every page now shows a small pill badge in the top-right corner with the running semantic version (`v0.3.1.2`). The chip is green when up-to-date, amber with a ▲ indicator when an update is available, and grey when the version check cannot reach GHCR. Hovering shows the full version + git SHA.
- **`semantic_version` in `/api/version`:** The version endpoint now returns the semantic version string from the `VERSION` file, in addition to the existing git SHA fields. Version is read once at startup and cached for the process lifetime — no per-request SD card I/O.
- **Docker: `VERSION` copied into image:** `Dockerfile` now includes `COPY VERSION /app/VERSION` so the semantic version is available inside the container at the expected path.

### Fixed
- Version chip JS uses `classList.add/remove` instead of `className=` assignment, preserving any other classes applied to the chip element.
- Version chip null-guarded so it silently no-ops when the element is absent from the DOM rather than throwing a TypeError.
- Version chip `else` branch (GHCR unreachable) now clears stale CSS classes from a previous `loadVersion()` call.
- `_read_semantic_version()` now tries `/app/VERSION` (Docker WORKDIR) before the repo-root path, fixing silent `"unknown"` fallback in production containers where the file was not previously present.

## [0.3.1.1] - 2026-03-30

### Changed
- **Auto-update polls every minute** instead of once daily. The update timer fires 2 minutes after boot and every minute thereafter. No-op if the GHCR SHA hasn't changed — the update service exits immediately after the manifest check.

## [0.3.1.0] - 2026-03-30

### Changed
- **Preflight checks consolidated to 4:** Phase 2 equipment check row count drops from 7 to 4. Microphone check moved to signal sweep phase (Phase 3). miniDSP USB + daemon checks run concurrently and report as a single "miniDSP" row. Denon AVR + playback route checks merge into a single "Denon AVR" row.
- **Denon auto-discovery via SSDP:** `check_denon()` now performs a 10-second SSDP scan when no `denon.host` is configured, eliminating the need to hardcode the AVR's IP address in most home network setups.
- **Config check always passes:** `check_config()` no longer fails when `denon.host` is absent (SSDP covers it). Shows an informational note when auto-discovery will be used.

### Fixed
- SSDP discovery now has a 10-second `asyncio.wait_for()` timeout guard. Previously, a busy network with many UPnP devices could cause preflight to stall indefinitely during the HTTP SCPD-fetch phase.
- `check_minidsp_combined()` now includes the daemon error message (not just the USB error) when both checks fail simultaneously, giving users the full picture on first failure.
- SSDP device with a malformed or missing host address now fails with a clear diagnostic instead of passing `None` to `DenonAVR()` and producing an opaque exception.

## [0.3.0.0] - 2026-03-30

### Added
- **Auto-update (F7):** New host-side `avr-calibration-update.service` (oneshot) + `avr-calibration-update.timer` (daily 3am, 30min jitter). The update service watches `~/.avr-calibration/upgrade-trigger` via `inotifywait -m`, pulls the latest GHCR image, restarts the container, polls `/health` for 30s, and rolls back to the previous digest on failure. Trigger file written by `POST /api/upgrade` inside the container — no Docker socket needed.
- **Version badge + upgrade button (F8):** Fixed footer shows current git SHA and update status (up-to-date / update-available / upgrading / unknown). "Upgrade Now" button writes the trigger file, then polls `/health` every 3s with a 180s timeout until the new container is healthy. GHCR latest SHA is fetched from the OCI manifest index annotation (2 API calls: anon bearer token + manifest), cached 1hr in-process.
- **`GET /api/version`:** Returns `current_sha`, `latest_sha`, `up_to_date`, `latest_checked_at`. Cached 1hr, reset on upgrade trigger.
- **`POST /api/upgrade`:** Writes the upgrade trigger file. Returns 202 on success, 409 if upgrade already in progress, 503 if the data directory is not writable.
- **Update audit log:** All upgrade events (auto or manual) are recorded in a new `update_events` SQLite table via `SessionStore.log_update_event()`. `GET /api/update-history` returns recent events.
- **`inotify-tools` system dependency** added to `install.sh` and `avr-calibration-update.service` pre-check.
- **Equipment Verification Workflow:** The flat single-page UI is replaced by a 5-step guided workflow navigator. Users start at Room Setup (Phase 1, placeholder form), move to Equipment Verification (Phase 2), then into the existing calibration tools (Phase 3). Phases 4-5 are visible but locked.
- **Test tone:** Phase 2 includes an 80 Hz Web Audio API test tone with a "I Can Hear It" confirmation button. Handles tab-focus `AudioContext` suspension via `resume()` before playback.
- **Hardware check rows:** Phase 2 shows pass/fail badges for all 7 checks: Config, Microphone, miniDSP USB, miniDSP, Denon AVR, Playback Route, Signal Path. "Run All" button fires all checks concurrently via `/api/preflight`.
- **`/api/preflight` endpoints:** `GET /api/preflight` runs all hardware checks and returns a JSON list. `GET /api/preflight/{name}` runs a single named check. Both wire directly to the existing `PreflightChecker` class.
- **`PreflightChecker.check_config()`:** New check that validates required config fields are present (`denon.host`). Included in `run_all()` as a 7th check.

### Changed
- `avr-calibration.service` restart policy changed from `on-failure` to `always` (survives clean exits from container restarts during upgrade).
- `Dockerfile`: `ARG BUILD_SHA` + `ENV BUILD_SHA=${BUILD_SHA:-unknown}` in runtime stage.
- `.github/workflows/docker.yml`: passes `BUILD_SHA=${{ github.sha }}` as build-arg and writes `index:org.opencontainers.image.revision` OCI annotation.
- **`check_playback_route(hdmi)` deduplication:** HDMI route now delegates to `self.check_denon()` instead of creating its own `DenonAVR` instance, preventing a double-connection when `run_all()` fires both checks in parallel.

## [0.2.0.0] - 2026-03-30

### Added
- **Extended target curves (F1):** HT-Aggressive and Musicality target curves join Harman and Flat in the curve selector. HT-Aggressive adds +4 dB/octave below 100 Hz for home theater content. Musicality adds a Gaussian peak at 30 Hz for music listening. Selection persists via `localStorage`.
- **Sub Trim Advisor (F2):** Card below the plot accepts an Audyssey sub trim reading (dB) and renders a color-coded badge with guidance. Optimal: −12 to −10 dB (green). Acceptable: −10 to −5 dB (amber). Too hot: above −5 dB (red). Too low: below −12 dB (blue).
- **Seat-to-Seat Variance (F3):** `POST /api/sessions/average` now returns `spl_variance` (per-bin standard deviation across sessions). The chart renders a transparent teal variance band (±1σ) after averaging multiple seats.
- **Phase/Time Alignment (F4):** Each measurement now stores its impulse response. `POST /api/sessions/time-align` cross-correlates two sessions at 60–100 Hz to estimate sub/mains time offset in milliseconds and feet — and tells you which way to move the delay. Sessions captured before this version show a "re-measure to enable phase check" advisory.
- **Dynamic EQ Advisor (F5):** Dismissable callout card (amber border, top priority) reminding users to disable Audyssey Dynamic EQ before calibration. Persists dismissal via `localStorage`.
- **Cardioid Sub Helper (F6):** `POST /api/signal-path/cardioid` enables cardioid array mode by inverting polarity and applying computed delay (`sub_separation_m / 343 × 1000` ms) to miniDSP output 1. Gracefully falls back to advisory-only when hardware does not support polarity inversion.
- **`has_ir` in session list:** `GET /api/sessions` now includes `has_ir: bool` per session so the Phase Check card can show re-measure warnings inline.

### For contributors
- `FrequencyResponse` carries an optional `impulse_response` field (list of float, first 24,000 samples). `_compute_fr_arrays` returns a 3-tuple `(frequencies, spl, ir_samples)`.
- `SessionStore.save_measurement()` persists the IR blob if the `FrequencyResponse` includes it.
- `sessions` table gets a new `impulse_response` column (TEXT, nullable) via idempotent `_migrate_schema()`. Existing rows get NULL.

## [0.1.9.0] - 2026-03-30

### Added
- **Measurement curve viewer:** Click any row in the History table to load and display its frequency response curve. The chart shows the before-EQ FR alongside the Harman subwoofer target curve (dashed reference line: flat above 80 Hz, +3 dB/octave below).
- **Before/after EQ overlay:** When a session has both `start_fr` and `end_fr`, the chart renders both as labeled datasets ("Before EQ" and "After EQ") for direct comparison.
- **PNG export:** Export button on the FR chart card. Downloads `fr-session-{id}.png` via canvas.toDataURL.
- **URL deep linking:** Selecting a session pushes `?session={id}` to the URL. Reloading the page restores the selected session. Browser back/forward navigates between sessions.
- **`GET /api/sessions/{session_id}` endpoint:** Returns full frequency response data (`frequencies`, `spl`) for a single session. Returns `null` for `start_fr`/`end_fr` when data is absent or corrupt.

### Fixed
- **Crash on corrupt session data:** `_row_to_session()` now defensively handles malformed JSON in `start_fr`, `end_fr`, and `filters_applied` columns. Returns a sentinel FrequencyResponse with empty arrays instead of crashing, so `list_sessions()` and `get_session()` remain functional even if old or aborted measurements have corrupt data.
- **Empty spl crash:** `FrequencyResponse.peak_spl` and `freq_at_peak` guard against empty `spl` lists, returning `0.0` instead of raising `ValueError`.

## [0.1.8.1] - 2026-03-29

### Fixed
- **arm/v7 Docker image missing packages:** `uv sync` was installing into `/build/.venv` instead of `/opt/venv` because the venv path wasn't explicitly passed. Added `UV_PROJECT_ENVIRONMENT=/opt/venv` so packages land in the venv that the runtime stage copies. Fixes `No module named uvicorn` crash on Pi Zero 2 W.

## [0.1.8.0] - 2026-03-29

### Added
- **miniDSP USB preflight check:** `calibrate check` now verifies `/dev/hidraw0` exists before attempting to contact minidspd. Missing device shows a targeted OTG adapter hint — Pi Zero 2 W requires a micro-USB OTG adapter, not a plain USB-A cable. Uses `asyncio.to_thread` to avoid blocking the event loop during the filesystem stat.

### Changed
- **Drop arm/v6 build target:** Docker image CI now builds only `linux/arm/v7,linux/amd64`. Pi Zero W (arm/v6) is no longer supported; Pi Zero 2 W (arm/v7) is the target platform.
- **QEMU setup scoped to arm:** CI QEMU step explicitly sets `platforms: arm` to avoid installing all emulators on every build (+30-60s CI waste removed).
- Updated deployment docs to reflect Pi Zero 2 W, arm/v7, and the OTG adapter requirement.

## [0.1.7.1] - 2026-03-23

### Changed
- **minidsp runs inside the Docker container** (not on the Pi host): `entrypoint.sh` now starts `minidsp server` in the background when `/dev/hidraw0` is present; container launched with `--device=/dev/hidraw0` instead of the full USB bus
- `minidsp.host` default changed from `172.17.0.1` (Docker bridge gateway) to `localhost`; config template updated to match
- `install.sh`: removed separate `minidspd.service` on the Pi host; replaced with udev rules that auto-bind `usbhid` to the HID interface on hotplug, so `/dev/hidraw0` is created automatically on every miniDSP replug
- `Dockerfile`: minidsp binary now bundled in the runtime image (`MINIDSP_VERSION=0.1.12`; ARM and x86_64 variants resolved via `TARGETARCH`/`TARGETVARIANT`)

### Fixed
- `install.sh` udev rule now also sets `MODE="0666"` on `/dev/hidraw*` (not just the raw USB device) so Docker can access the HID interface without running as root

## [0.1.7.0] - 2026-03-22

### Added
- **Sub-alignment algorithm (TODO-6, Phases 1-4):** MSO-inspired IR phase alignment for multiple subs — independent per-sub sweep+record, FFT deconvolution to extract impulse responses, travel-time delay offsets (Phase 2), polarity detection and correction (Phase 3), level matching (Phase 4)
- **`calibrate/adapters/minidsp.py`:** Async HTTP client (`MinidspClient`) wrapping the minidspd REST API — `set_output_gain()`, `set_output_delay()`, `set_output_polarity()`, `set_output_peq()`, `restore_all_gains()`; `MinidspApiError` on 4xx; `ValueError` guards for delay > 30 ms and reserved APF PEQ slots
- **`calibrate/alignment.py`:** Core signal-processing module — `extract_ir()` (FFT deconvolution, adaptive peak detection in configurable search window), `compute_delay_offsets()`, `measure_sub_ir()`, `detect_and_correct_polarity()`, `level_match_subs()`, `apply_delays()`, `run_alignment_phases()`; `SubIRResult` and `AlignmentSummary` dataclasses
- **Web API:** Three new endpoints — `POST /api/align-subs/start` (mute others, schedule sweep, return token), `POST /api/align-subs/record` (extract IR per step, run phases 2-4 on final step, restore gains), `POST /api/align-subs/cancel` (abort + restore gains)
- **TTL cleanup:** Background daemon thread evicts stale alignment sessions (>10 min) and restores sub gains automatically
- New config keys: `measurement.sub_outputs` (list of miniDSP output indices) and `measurement.ir_search_window_ms` (default 50 ms = 17.5 m max travel time)

### Fixed
- `_restore_sub_gains()` — moved `asyncio.new_event_loop()` before `try` block to prevent `NameError` in `finally` if loop creation fails

## [0.1.6.1] - 2026-03-22

### Fixed
- `_play_via_hdmi()` now calls `await receiver.async_update()` after `async_setup()` — without this, `_input_func_map` is empty and `async_set_input_func()` raises `AvrCommandError: No mapping for input source` even when the input name is correct
- `DEFAULT_CONFIG` `denon_sweep_input` changed from `"AUX1"` to `None` — AUX1 is not a valid input on all Denon models; `None` forces explicit configuration with a clear error message and discovery command if unset
- `_play_via_hdmi()` raises `ValueError` with actionable message when `denon_sweep_input` is not configured, rather than silently failing or raising a cryptic `KeyError`

## [0.1.6.0] - 2026-03-22

### Added
- **Playback routing (TODO-4):** `play_signal()` now dispatches to `_play_via_usb()` (Pi → miniDSP direct, Stage 1 sub alignment) or `_play_via_hdmi()` (Pi → Denon → full chain, Stage 2 integration) based on `config.measurement.playback_route`; `_play_via_hdmi()` connects to Denon via denonavr, switches to `denon_sweep_input`, sets `denon_sweep_volume` (safety guard: ≤ −25.0 dB), plays sweep over HDMI, and always restores original input + volume in `finally` block
- **Measurement quality validation (TODO-5):** `validate_recording()` runs three checks before deconvolution — floor noise gate (warn if > −40 dBFS), FFT cross-correlation sweep capture (raise `MeasurementQualityError` if peak < 0.05; O(N log N) — avoids O(N²) `np.correlate()` which would take ~100s on Pi Zero W), and SNR check (raise if < 20 dB)
- `MeasurementQualityError(RuntimeError)` — structured error with `check`, `detail`, `suggestion` fields; maps to HTTP 422 in web.py
- `FrequencyResponse.warnings` — new `list[dict]` field (backward-compatible via `setdefault` in `from_json()`)
- `check_playback_route()` on `PreflightChecker` — USB verifies output device is visible, HDMI verifies Denon is reachable
- New config keys: `playback_route`, `denon_sweep_input`, `denon_sweep_volume`, `denon_settle_ms`, `sweep_channel`, `playback_device`, `hdmi_playback_device`

### Changed
- `run_all()` on `PreflightChecker` refactored to paired `(name, coroutine)` structure — adding new checks no longer requires updating a parallel names list
- `web.py` `_play()` background thread now logs `RuntimeError` via `logger.warning()` instead of silently swallowing it
- `web.py` `/api/measure/record` response now includes `warnings` array from `FrequencyResponse`

### Fixed
- `web.py` `measure_record()` catches `MeasurementQualityError` before `RuntimeError` and returns structured 422 instead of 500

## [0.1.5.3] - 2026-03-22

### Fixed
- `deploy/entrypoint.sh` — hard-code `CERT_DIR=/data/.avr-calibration` instead of relying on `$HOME` to prevent silent breakage if the Dockerfile `ENV HOME` ever changes; openssl errors no longer silenced by `2>/dev/null` (now show output + fail fast with a clear error message if cert generation fails)

### Changed
- `TODOS.md` — marked TODO-4 (sweep playback routing) and TODO-5 (measurement quality validation) complete; added TODO-6 (multi-channel sweep), TODO-7 (measurement quality threshold calibration), and TODO-8 (rule-of-two sweep validation) as deferred items with full context

## [0.1.5.2] - 2026-03-22

### Fixed
- `generate_sweep()` no longer requires pytta — log sweep is now generated with pure numpy (exponential sine sweep formula), removing the pytta dependency from the browser-based measurement path; armv6/Pi Zero W can now run measurements without pytta installed
- Updated `TestGenerateSweep` tests to verify numpy implementation directly (sample count, value range, param overrides) instead of asserting against pytta mock calls

## [0.1.5.1] - 2026-03-22

### Fixed
- HTTPS (self-signed TLS) — `getUserMedia` requires a secure context; server now generates a self-signed cert on first boot (stored in the data volume) and runs uvicorn over HTTPS; browser shows a one-time "proceed anyway" warning
- `deploy/entrypoint.sh` — new Docker entrypoint that generates the cert and starts uvicorn with `--ssl-keyfile` / `--ssl-certfile`
- `Dockerfile` — added `openssl` to runtime stage; CMD now runs entrypoint.sh
- `deploy/install.sh` + docs — URLs updated from `http://` to `https://`

## [0.1.5.0] - 2026-03-22

### Fixed
- `deploy/install.sh` — upgraded minidsp-rs from v0.1.5 to v0.1.12 with corrected asset filename (`minidsp.arm-linux-gnueabihf-rpi.tar.gz`); added `-f` flag to `curl` so HTTP errors fail fast with a clear message instead of silently downloading a 404 HTML page
- `deploy/install.sh` — removed erroneous `--device=/dev/snd` passthrough from Docker run command; UMIK-1 is on the laptop, not the Pi

### Changed
- `CLAUDE.md` — architecture diagram updated to reflect browser-based audio capture (UMIK-1 on laptop → Web Audio API → Pi server); was incorrectly showing PyTTa running on the Pi
- `docs/deployment/pi-zero-w.md` — hardware diagram corrected: UMIK-1 now shown on the laptop, not the Pi USB hub; `calibrate check` expected output updated to remove UMIK mic check (mic is client-side)

## [0.1.4.0] - 2026-03-21

### Added
- `Dockerfile` — multi-stage build (builder + runtime); ARMv6 (Pi Zero W) path compiles numpy 1.24.x from source and skips pytta (no LLVM); all other arches build full deps including pytta via `uv sync`
- `.github/workflows/docker.yml` — GitHub Actions CI: cross-compiles `linux/arm/v6` and `linux/amd64` images via QEMU + Buildx, pushes to GHCR on every main push and version tag
- `[measurement]` optional extra in `pyproject.toml` — isolates pytta (and its numba/llvmlite/LLVM chain) so it can be skipped on ARMv6

### Changed
- `deploy/install.sh` — rewritten: Pi now just installs Docker, pulls the pre-built GHCR image, and runs it as a systemd service; no more source builds on the Pi
- `deploy/avr-calibration.service` — updated to run `docker run` with USB device passthrough and `/data/.avr-calibration` volume mount
- `pyproject.toml` — relaxed numpy to `>=1.24.4` (was `>=1.26`; ARMv6 cannot build wheels for 1.26+)
- `Dockerfile` runtime stage sets `ENV HOME=/data` so `config.py` finds `~/.avr-calibration` at the mounted volume path

## [0.1.3] - 2026-03-20

### Added
- `calibrate web` CLI command — starts a FastAPI web server (`--host`, `--port` options)
- `calibrate/web.py` — FastAPI app with placeholder index page and `/health` endpoint; full web UI ships in next release
- `deploy/install.sh` — Pi Zero W bootstrap script: system packages, uv, numpy ARMv6 pin, minidsp-rs ARM binary, udev rule, config template, systemd service
- `deploy/avr-calibration.service` — systemd unit file for the web server (auto-start on boot)
- `docs/deployment/pi-zero-w.md` — step-by-step Pi Zero W deployment guide
- `fastapi>=0.110` and `uvicorn>=0.29` added as core dependencies
- 7 new tests covering web app endpoints and CLI command at 100% coverage

## [0.1.2] - 2026-03-20

### Added
- `calibrate history` CLI command — lists all past measurement sessions (id, timestamp, label, peak SPL, point count; checkmark for sessions with a final post-EQ measurement)
- `calibrate show <id>` CLI command — human-readable session detail with ASCII frequency response plot, plus `--csv` and `--json` export modes
- `_ascii_plot()` helper — 10-bar log-spaced ASCII bar chart of frequency response
- `show` command displays feedback notes (text + optional `content_tag`) when present
- `show` command shows final peak SPL and delta (Δ dB) when `end_fr` is recorded
- 15 new tests covering all `history`, `show`, and `_ascii_plot` code paths at 100% coverage
- `update_end_fr()` on `SessionStore` — records post-EQ measurement for a session

## [0.1.1] - 2026-03-20

### Added
- `calibrate measure` CLI command — runs a log-sweep measurement and saves to SQLite history
- `MeasurementEngine` — PyTTa-based acoustic measurement with lazy import (no PortAudio required at import time)
- `FrequencyResponse` dataclass — serializable result with `to_json`/`from_json`, `peak_spl`, `freq_at_peak`
- Deconvolution via numpy FFT: `H(f) = FFT(recording) / FFT(sweep)`, zero-division guarded, trimmed to calibration band
- `SessionStore` — SQLite session persistence (`~/.avr-calibration/history.db`) with schema designed for `calibrate history` (TODO-2) and content-tagged feedback (TODO-3)
- `add_feedback()` / `get_feedback()` on `SessionStore` — optional `content_tag` field baked in from day one
- `measurement` config section with defaults for freq range, sweep duration, sample rate, and channel routing
- 40 new tests covering all new code paths, data flow branches, and CLI commands at 100% coverage
- `fake_pytta_module` session fixture in conftest — same lazy-import mock pattern as sounddevice

## [0.1.0] - 2026-03-19

### Added
- `calibrate check` pre-flight hardware verification command
- `PreflightChecker` with async checks for UMIK microphone, miniDSP 2x4 HD (via minidspd), and Denon AVR
- `Config` class with YAML loading, deep-merge defaults, and template creation
- Color CLI output: green ✓ / red ✗ with actionable error hints
- 22 unit tests with 100% coverage of all happy paths and error branches
- GitHub Actions CI pipeline (Python 3.12, uv, pytest)
- CLAUDE.md with architecture overview, safety limits, and development setup
- TESTING.md documenting mock strategy for PortAudio, httpx, and denonavr
