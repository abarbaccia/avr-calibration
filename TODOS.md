# TODOS

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

## Completed

### TODO-4: Sweep playback routing — miniDSP USB vs. Denon HDMI
**Completed:** docker-pipeline PR (2026-03-22) — implemented `_play_via_usb()` + `_play_via_hdmi()` with `playback_route: usb | hdmi` config dispatch. Two-stage calibration model (Stage 1 USB for sub alignment, Stage 2 HDMI for full-chain integration). `calibrate check` reports playback route status.

### TODO-5: Measurement quality validation
**Completed:** docker-pipeline PR (2026-03-22) — implemented `validate_recording()` with three checks (floor noise gate → warn, cross-correlation sweep capture → raise, SNR → raise). HTTP 422 response with structured `{error, check, detail, suggestion}` body. `FrequencyResponse.warnings` field for non-fatal warnings.

### TODO-2: Measurement history browser
**What:** `calibrate history` CLI command — shows past sessions with date, starting FR vs. final FR, filters applied, and subjective feedback logged during that session.
**Why:** The long-term value of this tool is the accumulated room model. Without visibility, you can't tell if the system is improving over time or debug why a session diverged.
**Context:** Read-only queries against the same SQLite store the pipeline writes to. Output as a formatted table or JSON. Also `calibrate show <id>` for detail view with --csv and --json export.
**Completed:** v0.1.2 (2026-03-20)
