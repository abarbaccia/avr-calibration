# TODOS

## Deferred from /autoplan (2026-04-01) — Pi 5 Headless Readiness

### TODO-P5-1: PyTTa measurement quality validation vs. REW
**What:** Run a measurement quality comparison: same room, same mic (UMIK-1), REW vs. PyTTa. Compare frequency response curves in 20-200 Hz band.
**Why:** Before building the autonomous calibration loop (next feature), confirm PyTTa is within 1-2 dB of REW in the target band. If materially worse, the autonomous loop will converge on a bad result.
**How:** Run REW with UMIK-1 (laptop), run `calibrate measure` on Pi 5, export both to CSV, compare. Look for systematic bias or high variance in PyTTa measurements.
**Effort:** S — measurement comparison, no code.
**Priority:** P1 — prerequisite for autonomous loop feature. Do before starting that feature.

### TODO-P5-2: arm64 sounddevice wheel validation
**What:** Verify `sounddevice` on arm64 resolves to manylinux wheel (fast) vs. source build (slow).
**Why:** If sounddevice falls back to source compilation on arm64, CI arm64 build time increases significantly.
**How:** Inspect arm64 CI build logs after first push; look for "Building sounddevice from source" vs. wheel install.
**Effort:** S — observation task.
**Priority:** P2 — validate during first arm64 CI run.

## Deferred from /autoplan (2026-03-31) — Signal Path Builder

### TODO-SP1: Atomic YAML write in update_config()
**What:** Replace non-atomic `open(path, "w") + yaml.safe_dump()` with write-to-temp + `os.replace()`. `os.replace` is atomic on Linux (same filesystem).
**Why:** Pi Zero 2W on SD card + Docker = routine unclean shutdowns. Current pattern can produce zero-byte config on power loss, silently resetting all user config to defaults.
**Effort:** S (4-line change in `calibrate/config.py:update_config()`)
**Priority:** P2 — data loss risk, but predates this feature.

### TODO-SP2: Keyboard navigation for chain builder
**What:** Add `tabindex=0`, `role=button`, `onkeydown` (Enter/Space) to all interactive chain elements (node cards, "+ Add" slots, Remove buttons). The entire codebase uses `onclick` on divs with zero keyboard nav.
**Why:** Inaccessible for keyboard-only users. Lowest-friction setup device (no mouse needed on Pi-attached screen).
**Effort:** M — needs systematic pass over all onclick handlers in chain builder JS.
**Priority:** P3 — accessibility, no functional impact.

### TODO-SP3: Atomic multi-store write for POST /api/signal-chain
**What:** SQLite speaker writes and YAML config writes are not transactional across stores. A failed YAML write after successful SQLite write leaves split state. Fix: write SQLite first (in a transaction with rollback), then YAML. If YAML fails, re-run SQLite rollback manually.
**Why:** Edge case, but split state is hard to debug and self-heal.
**Effort:** M — needs wrapper in the chain POST handler.
**Priority:** P2 — data integrity.

### TODO-SP4: Safe SQL in SessionStore.update_equipment()
**What:** `update_equipment()` builds SQL with f-string over hardcoded key names. Not injectable today (keys are hardcoded `label`, `data`, `updated_at`) but pattern is dangerous if future contributors add user-controlled keys to the fields dict.
**Why:** Pattern will fail a security scan and will encourage copy-paste of the unsafe pattern.
**Effort:** S (replace with explicit column list in UPDATE).
**Priority:** P3 — no active risk, preventive.

### TODO-SP5: Speaker preset library (additional models)
**What:** Add more speaker presets beyond PB12-NSD: SVS SB-16 Ultra, REL T/9x, SVS PC-4000, etc. Data model is ready (preset field exists).
**Why:** Most users have a different sub. PB12-NSD is the owner's sub; preset library makes the system usable by others.
**Effort:** S (add preset definitions to a presets.py or presets.json, wire into the UI dropdown)
**Priority:** P2 — usability for anyone other than the owner.

## Deferred from /autoplan (2026-03-30) — Full-Room Measurement

### TODO-R1: Resolve sweep delivery for multi-channel measurement
**Status:** GATE 1 FAILED (2026-03-30). Pi Zero 2 W vc4-hdmi only exposes `IEC958_SUBFRAME_LE`
(S/PDIF stereo container). 8-channel PCM is not available via this driver.
**What:** Pick one of three fallback paths and implement:
- **Path A — Denon test tone API:** trigger per-channel pink noise via denonavr/telnet,
  record from UMIK, FFT for RTA-based FR. Check `plughw:vc4hdmi` first:
  `aplay -D plughw:vc4hdmi --dump-hw-params /dev/null` and
  `python3 -c "import denonavr; print([m for m in dir(denonavr.DenonAVR) if 'tone' in m.lower() or 'speaker' in m.lower()])"`
- **Path B — Pi 5:** HDMI audio driver supports multi-channel PCM. Swap hardware, original
  plan works as designed. Also solves FIR convolution headroom.
- **Path C — Mono sweep + Denon channel routing:** play mono sweep via existing HDMI path
  (already works), use denonavr to route each speaker in sequence. One sweep per channel,
  same UMIK capture. Uses current hardware only.
**Why:** Blocked the entire full-room measurement feature.
**Priority:** P1 — unblocks feature. Pick a path before writing any measurement code.

### TODO-R2: Satellite channel correction path
**What:** When correction path for satellite speakers exists (CamillaDSP full-chain or
future Denon feature), connect the room measurement dashboard data to drive EQ recommendations.
**Why:** Currently visibility only — no correction path for non-sub channels.
**Priority:** P3 — depends on hardware decisions.

### TODO-R3: Waterfall / decay plots
**What:** Add time-domain waterfall (spectrogram) to `/room` dashboard.
**Why:** Room mode decay (ringing) is as important as steady-state frequency response.
Deferred from this PR — adds significant rendering complexity.
**Priority:** P2 — natural follow-on once FR measurement is working.

### TODO-R4: Room health monitor (recurring automated measurement)
**What:** Schedule `calibrate measure-room` on a cron/interval, store time-series,
alert when room response shifts significantly (furniture moved, season change, etc.).
**Why:** This is the genuinely novel value vs. REW — automation, not just visualization.
**Priority:** P2 — after dashboard is working.

## Deferred from /autoplan (2026-03-30) — Measurement Curve Viewer

### TODO-CV1: Harman target overlay on FR chart
**What:** Add the Harman target curve as a reference line on the frequency response chart.
**Completed:** v0.1.9.0 (2026-03-30) — implemented as dashed `#94a3b8` dataset using median SPL as reference. Flat above 80 Hz, +3 dB/octave below.

### TODO-CV2: Delta-from-Harman column in history table
**What:** Add a computed column to the history table showing the RMS deviation from the Harman target curve for each session. A single number that answers "how close am I to target?"
**Why:** Without this, 10 sessions of data still can't answer "is my calibration improving?" The column makes the history table a progress dashboard.
**Effort:** M — requires computing the Harman error metric against stored FR data. Needs the Harman target data from TODO-CV1 first.
**Priority:** P2 — depends on TODO-CV1.

### TODO-CV3: URL deep linking for sessions
**What:** `?session=3` query param on page load restores the selected session. `history.pushState()` when a session is loaded.
**Completed:** v0.1.9.0 (2026-03-30) — `?session={id}` on load, `history.pushState` on select, `popstate` handler for back/forward.

## Deferred from /plan-eng-review (2026-03-19)

### TODO-1: Dry-run mode
**What:** `--dry-run` CLI flag — full measurement + AI analysis cycle runs, but no writes sent to miniDSP or Denon.
**Why:** Essential for first-run confidence before trusting the system to touch hardware. Also makes the system demonstrable without physical setup.
**Pros:** Zero-risk validation of AI judgment; useful for development and demos.
**Cons:** Small extra mode/flag to maintain in the CLI layer.
**Context:** SafetyValidator still runs in dry-run (so rejections are visible). Hardware adapter calls become no-ops that log "would write: {params}" instead of executing. Implement as an injected flag on the Adapter protocol — each adapter checks `dry_run` before writing.
**Depends on:** CLI scaffolding, hardware adapters (miniDSP + Denon).

---

### TODO-3: Content-tagged subjective feedback
**What:** Optional `content_tag` field on feedback log entries ("Fury Road chapter 3", "music: Daft Punk", "gaming: FPS"). AI analysis core groups by tag to identify content-specific patterns.
**Why:** This is the core differentiator from every other calibration tool. "Bass too heavy on action movies but not music" is a different problem than "bass too heavy" — it maps to different EQ presets.
**Pros:** Unlocks content-aware EQ profiles; maps naturally to miniDSP 2x4 HD's 4 preset slots (action / music / gaming / default).
**Cons:** Adds logging friction; content-aware profiles require multiple optimization runs (one per content type).
**Context:** Build the `content_tag` field into the feedback log schema from day one (nullable, optional). Even if unused initially, having it in the schema makes adding content-aware logic trivial later. miniDSP 2x4 HD has exactly 4 preset slots — this maps perfectly to 4 content profiles.
**Depends on:** Feedback log schema, AI analysis prompt engineering.
**Status (v0.1.1):** ✓ `content_tag` column is live in the `feedback` table; `add_feedback()` accepts and stores it. AI analysis grouping still to implement.

---

## Measurement & Calibration

### TODO-6: Multi-channel sweep support (fl, fr, c, sl, sr)
**What:** Extend `sweep_channel` beyond `lfe` to support satellite speaker channels for full-system calibration.
**Why:** Stage 2 (HDMI full-chain) measures what the listener actually hears. Limiting to LFE means only the sub can be characterized and EQ'd — you can't calibrate the satellite speakers or optimize the crossover.
**Pros:** Unlocks full-system room correction across all channels; maps to denonavr channel selection API.
**Cons:** Requires determining how to play a mono sweep on each ALSA channel (LFE vs FL/FR HDMI channel mapping varies by Pi audio driver).
**Context:** Current PR scopes to `lfe` only. Future PR: add `sweep_channel` → ALSA channel index mapping for HDMI route. The config field is already in place; only the dispatch logic needs extension.
**Depends on:** TODO-4 (HDMI route, this PR), physical Pi HDMI → Denon connection.
**Priority:** P2 — bass calibration is the primary use case; satellites are a future expansion.

---

### TODO-7: Measurement quality threshold calibration
**What:** Tune the three validation thresholds (-40 dBFS floor noise, 0.05 cross-correlation peak, 20 dB SNR) against real room measurements.
**Why:** Initial values are engineering estimates, not empirically validated for a living room with HVAC, sub output levels, and USB mic sensitivity. Wrong thresholds → false quality errors or missed garbage measurements.
**Pros:** Once calibrated, thresholds give reliable quality gates for all future measurements.
**Cons:** Requires at least a few real sweep sessions to observe trigger behavior (not a code task, a practical validation task).
**Context:** All three thresholds are already config-overridable (`noise_floor_window_ms`, `correlation_threshold`, `min_snr_db` params with config defaults). This is a tuning exercise after first real Stage 1 (USB) measurements. Update config.yaml defaults once empirical values are known.
**Depends on:** TODO-4 (this PR) — need a working measurement session first.
**Priority:** P2 — thresholds are conservative defaults; the system works before this is done.

---

### TODO-8: Rule-of-two sweep validation (repeat sweeps for non-stationary noise)
**What:** Run two sweeps back-to-back and compare their frequency responses. If they differ significantly (> X dB in any 1/3 octave), warn "inconsistent measurement — room noise may be non-stationary."
**Why:** Single-sweep validation catches static noise floors and missing signals, but can't detect intermittent noise (traffic, HVAC bursts, footsteps). Two sweeps that agree are much more trustworthy than one.
**Pros:** Catches measurement unreliability that the current three-check validation misses.
**Cons:** Doubles sweep time (2× 3s + 2× countdown). Adds complexity to the browser measurement flow (two sequential sweep+record cycles).
**Context:** Explicitly deferred as "overkill for now" in the office-hours session (2026-03-22). Revisit if Stage 1 measurements show high variance or if you start measuring in noisier conditions.
**Depends on:** TODO-4, TODO-5 (both this PR).
**Priority:** P3 — nice to have, not needed for reliable single-room calibration.

---

## Hardware & Deployment

### TODO-HW2: Equipment labeling — miniDSP I/O and Denon connections
**What:** Add a `connections` config section in `config.yaml` (and display it in the signal path card) so each physical I/O port has a human label:

```yaml
connections:
  minidsp:
    inputs:
      0: "Denon bass mgmt L (XLR)"
      1: "Denon bass mgmt R (XLR)"
    outputs:
      0: "SVS PB12-NSD (LFE, XLR)"
      1: "Spare / not used"
      2: "Spare / not used"
      3: "Spare / not used"
  denon:
    sweep_input: "AUX1"   # which Denon input the Pi HDMI is connected to
    outputs:
      fl: "Klipsch RP-8000F L"
      fr: "Klipsch RP-8000F R"
      c: "Klipsch RP-504C"
      sl: "Klipsch RP-502S L"
      sr: "Klipsch RP-502S R"
      sub: "miniDSP 2x4 HD"
```

Render labels in `#spInputs` / `#spOutputs` in the signal path card instead of generic "Input 1 / Output 2" names. Also surface `denon.sweep_input` in the Denon preflight card so it's clear which input will be selected for sweep playback.

**Why:** Without labels, the block diagram shows "Input L / Input R / Out 1…4" with no physical meaning. The user can't tell which output goes to the sub vs. a satellite. Also needed for sweep routing — we have to know which Denon input the Pi HDMI cable is plugged into before we can auto-select it.
**Priority:** P2 — not blocking calibration, but the signal path card is confusing without it.

### TODO-HW1: EMI-style USB disconnect detection in preflight
**What:** Scan `dmesg` for `disabled by hub (EMI?)` messages in `check_hidraw()`. If found, add a targeted hint to the error: "USB voltage instability detected (EMI protection tripped). Check your Pi power supply — Pi Zero 2 W needs 2.5A+ under WiFi + USB load."
**Why:** The generic OTG adapter hint is misleading for this failure mode. Root cause is a 1A power supply causing voltage dips when WiFi and USB OTG fire simultaneously, triggering the Pi's EMI protection circuitry. Symptom: `usb usb1-port1: disabled by hub (EMI?), re-enabling...` followed by `error -71` on re-enumerate. Rebooting recovers the port; replacing the power supply prevents recurrence.
**How to detect:** `subprocess.run(["dmesg"], capture_output=True, text=True)`, grep for `EMI` in the last 100 lines. Only add this hint when hidraw is missing AND EMI lines are present — don't fire on clean boots.
**Note:** `check_hidraw()` is only called when minidspd daemon fails (see `check_minidsp_combined`). The EMI detection is for "USB device disappeared and daemon can't reconnect" post-failure scenarios.
**Priority:** P3 — power supply is a one-time physical fix; hint is nice-to-have.

### TODO-4: Sweep playback routing — miniDSP USB vs. Denon HDMI
**What:** Design and implement how the Pi plays the log sweep. Two viable approaches with different trade-offs:

**Option A — miniDSP USB DAC (direct)**
Pi → USB → miniDSP 2x4 HD → Subwoofer
- Tests the miniDSP EQ chain in isolation
- Only works for subwoofer/bass calibration
- Requires `/dev/snd` Docker passthrough + ALSA device config
- Simple but incomplete — doesn't test Denon crossover or the full signal path

**Option B — Denon HDMI (full chain)**
Pi HDMI → Denon input → Denon crossover → miniDSP → Sub (for bass)
Pi HDMI → Denon input → Denon amp → Speakers (for other channels)
- Tests the exact signal chain the listener actually hears, including Denon crossover, bass management, and room correction
- Works for all speakers, not just the sub
- Requires Pi HDMI connected to a Denon input, and Denon controlled via denonavr to select that input + set volume before sweep
- Preferred for full-system calibration

**Decision needed:** For sub-only bass calibration (current scope), both work. For future full-system calibration, Option B is required. The question is whether to build Option A now (simpler, working MVP) and extend later, or build Option B from the start.

**Recommendation:** Build Option B from the start. The marginal complexity is low, it tests the real signal chain (critical for accurate Denon crossover interaction with the miniDSP EQ), and avoids having to re-architect playback later. The config key `playback_route` with values `usb` | `hdmi` | `auto` keeps it flexible.

**Config design:**
```yaml
measurement:
  playback_route: hdmi       # usb | hdmi | auto (auto tries hdmi, falls back to usb)
  playback_device: "miniDSP" # substring match for ALSA device name (usb route only)
  sweep_channel: lfe         # lfe | fl | fr | c | sl | sr — which Denon channel to sweep
  denon_sweep_input: "AUX1"  # Denon input to select for sweep playback (hdmi route) — user selectable
  denon_sweep_volume: -25.0  # dB — set Denon volume before sweep, restore after
```

**Implementation:**
- `play_signal()` checks `playback_route` and dispatches to `_play_via_usb()` or `_play_via_hdmi()`
- `_play_via_hdmi()` uses denonavr to: select input → set volume → play sweep → restore input/volume
- `_play_via_usb()` uses sounddevice with ALSA device name matching (like mic does)
- Docker: `--device=/dev/snd` needed for USB route; HDMI route needs no extra Docker config
- `calibrate check` verifies the configured playback route is reachable

**Why:** The miniDSP EQ is in the subwoofer signal path *after* the Denon crossover. If we bypass the Denon (USB direct), we skip its bass management entirely — the sweep hits the sub at a different level and frequency rolloff than what the listener hears. HDMI gives us ground truth.

**Hardware prerequisite (not yet done):** Pi HDMI → Denon AUX1 input. This is the blocking physical dependency before HDMI route can be tested.

**Depends on:** Pi HDMI cable to Denon AUX1, Denon adapter (denonavr already integrated), `/dev/snd` Docker passthrough for USB fallback only.
**Priority:** P0 — measurement is silent without this. Wire Pi HDMI to Denon first, then implement.

---

### TODO-5: Measurement quality validation
**What:** Before accepting a recording, validate it wasn't just floor noise. Three checks:
1. **Floor noise gate** — measure RMS of first 500ms of recording (before sweep arrives) as ambient noise floor. If RMS > -40 dBFS, warn "room is too noisy for reliable measurement."
2. **Sweep capture check** — compute cross-correlation between sent sweep and recording. Peak correlation should exceed a threshold (e.g. 0.05 normalized) — if not, the sweep wasn't captured and the result is meaningless (amp off, wrong input, mic muted).
3. **SNR check** — compare RMS of recording peak window vs. floor noise. Require at least 20 dB SNR. Below that, warn "signal too weak — check amp volume and miniDSP routing."
**Why:** Right now a "measurement" with the amp off produces a valid-looking FR of pure noise. This silently produces garbage data that will mislead the AI analysis. These checks catch the most common real-world failure modes: amp off, wrong input selected, mic muted, cable unplugged.
**Context:** Validation runs in `compute_fr()` before deconvolution — raise `MeasurementQualityError` (subclass of `RuntimeError`) with a user-friendly message. The web API surfaces it as a 422 with a structured body `{error, check, detail}` so the browser can show a specific actionable message ("Turn on your amp and select the right input") rather than a generic failure. Add `test_measurement_quality.py` with tests for all three checks including boundary conditions (exactly at threshold, just below, just above).
**Depends on:** TODO-4 (need real signal to tune thresholds).
**Priority:** P1 — important for reliability but needs hardware to validate thresholds.

---

## Multi-Sub Calibration

### TODO-9: OCA + Audyssey integration guide
**What:** Document the 3-step calibration sequence: (1) `avr-calibration align-subs` first to align miniDSP sub outputs relative to each other, (2) Run Audyssey MultEQ XT32 on the Denon AVR (which now sees a phase-coherent dual-sub source), (3) optional OCA A1 EVO AcoustiX post-processing (Windows + Audyssey MultEQ Editor required) to further optimize Audyssey's result.
**Why:** avr-calibration and Audyssey are not mutually exclusive — they operate on different layers of the signal chain. Without this guide, users run Audyssey before aligning subs (wrong order), causing Audyssey to try to calibrate two out-of-phase subs and produce a confused calibration. avr-calibration is the prerequisite for accurate Audyssey/OCA results.
**Pros:** Zero implementation cost — this is documentation only. Captures the system architecture before the insight is lost.
**Cons:** OCA step requires Windows machine + Audyssey MultEQ Editor license — not automated by avr-calibration.
**Context:** OCA's *software* (Audyssey One, A1 EVO AcoustiX) is Audyssey-locked and Windows-only. OCA's *methodology* (phase convergence via IR analysis) is what `align-subs` implements natively. The full calibration stack is: avr-calibration miniDSP alignment → Audyssey full-system calibration → OCA post-processing of Audyssey result. Add a `docs/calibration-workflow.md` that explains this layering with ASCII signal-chain diagram.
**Depends on:** align-subs Phase 1-4 (shipped v0.1.7.0) — dependency now met; APF (TODO-10) extends the guide further when implemented.
**Priority:** P3 — useful guide, not blocking anything.

---

### TODO-10: APF all-pass filter phase correction (Phase 3.5)
**What:** After the Phase 1-5 alignment algorithm runs, detect whether a persistent notch remains in 60-120 Hz that PEQ amplitude correction couldn't fix. If yes, implement Phase 3.5: design 1-2 all-pass filters (APFs) per sub to correct frequency-dependent phase differences. APF biquad coefficients written to miniDSP PEQ slots 1-2 (reserved; slots 3-10 are used for amplitude EQ).
**Why:** A single delay corrects travel-time differences (physical distance between subs, ~90% of typical 2-sub setups). APFs correct frequency-dependent phase differences caused by each sub's proximity to different room boundaries, port resonances, and room modes — these can't be fixed by a single delay. MSO solves this; we should too for challenging sub placements.
**Pros:** Closes the gap between our Approach B and full MSO for rooms where subs are placed in acoustically different positions (front vs. side wall, corner vs. free-standing).
**Cons:** Minimum-phase extraction is mathematically involved; APF design requires scipy signal processing. Must respect the 2-APF-per-sub limit (more causes excessive group delay). Only needed when the standard Phase 1-5 produces a persistent notch — most 2-sub setups won't need it.
**Context:** Trigger condition: after Phase 5 combined EQ pass, if the residual FR shows a notch deeper than -6dB in 60-120Hz that persisted after EQ (i.e., PEQ couldn't fill it without >+6dB boost), the system prompts: "Run `calibrate align-subs --apf` for all-pass filter correction." Detection function: `detect_apf_needed(combined_fr, pEQ_applied) -> bool`. PEQ slot budget: slots 1-2 (0-indexed) reserved for APFs; Phase 5 EQ implementation must only use slots 2-9 (=slots 3-10 in 1-indexed miniDSP UI). Source: MSO methodology, Harman research on multi-sub room interaction.
**Depends on:** TODO-10 alias for Phase 1-5 (align-subs v1) working and validated on real hardware.
**Priority:** P2 — most 2-sub setups won't need it; implement after validating v1 and observing whether the notch pattern appears in practice.

---

## Architecture Convergence (added 2026-04-05)

### TODO-ARCH1: Extract Denon lifecycle from measurement.py
**Completed:** v0.1.9.1 (2026-04-05) — `DenonSweepContext` async context manager in `drivers/denon.py`. `measurement.py` has zero denonavr imports. All callers (CLI, web, MCP) use the same pattern: `DenonSweepContext.from_config(cfg)` returns context or None, caller wraps `engine.measure()` accordingly.

### TODO-ARCH2: Extract HDMI int16 conversion from measurement.py
**Completed:** v0.1.9.1 (2026-04-05) — `PlaybackStrategy` protocol in `calibrate/drivers/playback.py` with `USBPlayback` (PyTTa duplex) and `HDMIPlayback` (split sd.rec+sd.play, int16 output). `measurement.py` calls `playback_for_route(route).play_and_record()`. Zero format-specific logic in the engine.

### TODO-ARCH3: MCP trigger_measurement should call engine directly
**Completed:** v0.1.9.1 (2026-04-05) — `_tool_trigger_measurement()` now creates `MeasurementEngine`, calls `engine.measure()` directly, saves to `SessionStore`. No httpx, no HTTP hop. Uses `DenonSweepContext` wrapper for HDMI route.

### TODO-ARCH4: Consolidate Denon access through DenonDriver
**Completed:** v0.1.9.1 (2026-04-05) — Zero raw denonavr imports outside `drivers/denon.py`. `measurement.py`, `web.py`, `mcp_server.py`, and `preflight.py` all go through `DenonDriver` or `DenonSweepContext`. `preflight.check_denon()` uses `DenonDriver.get_state()` and `DenonDriver.discover()` for SSDP.

---

## Completed

### TODO-4: Sweep playback routing — miniDSP USB vs. Denon HDMI
**Completed:** v0.1.6.0 (2026-03-22) — original: `_play_via_usb()` + `_play_via_hdmi()` split API.
**Updated:** v0.1.9.1 (2026-04-05) — dead code deleted. `measure()` is the single entry point with route-aware playback (USB=PyTTa duplex, HDMI=split sd.rec+sd.play with int16 conversion). Denon lifecycle extracted to `DenonSweepContext` in `drivers/denon.py` (ARCH1). All callers use the same context manager pattern.

### TODO-5: Measurement quality validation
**Completed:** v0.1.6.0 (2026-03-22) — implemented `validate_recording()` with three checks (floor noise gate → warn, cross-correlation sweep capture → raise, SNR → raise). HTTP 422 response with structured `{error, check, detail, suggestion}` body. `FrequencyResponse.warnings` field for non-fatal warnings.

### TODO-2: Measurement history browser
**What:** `calibrate history` CLI command — shows past sessions with date, starting FR vs. final FR, filters applied, and subjective feedback logged during that session.
**Why:** The long-term value of this tool is the accumulated room model. Without visibility, you can't tell if the system is improving over time or debug why a session diverged.
**Context:** Read-only queries against the same SQLite store the pipeline writes to. Output as a formatted table or JSON. Also `calibrate show <id>` for detail view with --csv and --json export.
**Completed:** v0.1.2 (2026-03-20)

## Deferred from /autoplan (2026-04-01) — Equipment Driver Abstraction

### TODO-DA1: MicDriver abstraction
**What:** A `MicDriver` ABC in `calibrate/drivers/mic_driver.py` for measurement microphone abstraction. Follows the same pattern as `AVRDriver`/`DSPDriver`. `UMIKDriver(MicDriver)` handles: device discovery by name substring, UMIK .cal file loading and correction, audio sweep recording.
**Config:** `mic_driver: umik` with `UMIKDriver` registered in registry.py.
**Why:** The mic is hardware with variation (UMIK-1, UMIK-2, Dayton iMM-6, miniDSP EARS). Cal file format differs per model. Abstracting enables other mic types without touching measurement.py.
**Deferred because:** `trigger_measurement` is a Pi Zero degraded stub (USB port occupied by miniDSP). Abstracting a mic that can't be triggered from MCP yet is premature. Revisit when measurement loop is unblocked (see TODO-R1).
**Effort:** M — `measurement.py` is the hottest file in repo; requires careful blast radius management.
**Priority:** P2 — natural follow-on after AVR+DSP drivers; block by TODO-R1.

### TODO-DA2: discover_avr MCP tool
**What:** `discover_avr()` MCP tool that calls `_avr.discover()` (SSDP scan). Returns list of found AVR hosts on the network. Claude calls this during setup conversation to auto-populate Denon IP.
**Why:** Core of the "AI-first configuration" conversation — user says "set up my system" and Claude discovers the AVR automatically.
**Effort:** S — `DenonDriver.discover()` base returns []; `DenonDriver.discover()` implementation uses denonavr SSDP; expose as MCP tool.
**Priority:** P2 — enables conversational setup flow.

### TODO-DA3: MCP get_config / set_config tools
**What:** Two new MCP tools: `get_config()` returns current `config.yaml` as dict, `set_config(updates)` deep-merges updates and writes back. Enables Claude to configure hardware via conversation.
**Why:** The "AI-first configuration" design goal requires Claude to be able to read/write config without SSH or YAML editing.
**Effort:** S — thin wrappers over `Config.load()` and `update_config()` already in config.py.
**Priority:** P2 — completes the AI-first config vision.
