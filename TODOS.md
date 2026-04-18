# TODOS

## Backlog — Deep Research (not scheduled)

### TODO-FLASH-1: Atomic preset persist via native-app protocol
**What:** Reverse-engineer the opcode sequence the native miniDSP Windows/Mac app uses to commit a full preset (matrix, output gains, master gain, source, PEQ, delays, polarity) to flash in one atomic write. Expose as MCP tool `persist_preset(slot)`.
**Why:** `minidsp-rs` CLI writes are incremental; some land in flash-backed memory (PEQ/delays/polarity) and some in volatile DSP registers (matrix, output gains, master, input gain, source). After a calibration run, the "fine trim" state is lost on power cycle. Native app avoids this because it ships a complete preset blob.
**How:** USB wire capture of the native app → isolate the commit-to-flash command → implement in minidsp-rs (ideally upstream PR) or a separate Python helper. Test on non-critical slot first. Bricking risk if wrong.
**Effort:** L — 1-2 days if protocol is partially public, more if not.
**Priority:** P3 — low, manual save-in-app workflow is acceptable for now.

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
**Note:** `analyze_decay` MCP tool (v0.6.0.0) provides the CLI/MCP primitive — Claude can now identify and prioritize ringing modes. The web waterfall visualization (spectrogram chart) remains this item.
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
**Completed:** v0.6.4.0 (2026-04-10) — target curve stored per measurement session at capture time; dB RMS computed against the actual calibration reference (null for sessions taken outside of a calibration run).

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

----

### TODO-RATTLE: Harmonic distortion measurement for rattle detection
**What:** Extend `MeasurementEngine` to extract 2nd and 3rd harmonic impulse responses from the PyTTa log sweep deconvolution. Return `thd_db` (THD by frequency) alongside `spl_db` in the session store and `measure` tool response. Add a `measure_distortion` MCP tool (or flag on `measure`) that sweeps at higher level to provoke rattles, then reports elevated harmonic content with per-frequency THD.
**Why:** Rattles and port noise are a common source of listening fatigue that users can't diagnose by ear at low levels. Showing the user where harmonic distortion peaks are (e.g. "elevated 2nd harmonic at 35 Hz — likely port resonance or loose panel") is actionable feedback. The log sweep deconvolution already separates harmonics — PyTTa computes these; they're just not being returned.
**How it works:**
- A log sweep naturally separates harmonics in the time domain after deconvolution. The 2nd harmonic IR arrives at a predictable earlier time offset; the 3rd harmonic even earlier.
- `ImpulsiveResponse.py` in PyTTa already handles harmonic extraction when `method='linear'` is used with the right window.
- Signal classification:
  - Narrow harmonic peak (elevated 2nd/3rd harmonic) → speaker/port/panel rattle
  - Broadband THD spike + coherence dip at a single frequency → room object rattle (picture frame, shelf, vent cover, lamp)
  - Broad coherence dip across a frequency range → port wind noise / turbulence at high excursion
- Solo each sub (mute others) to localize which sub is driving a rattle; if it persists across all solos, it's room-only.
- PyTTa can compute **coherence** (correlation of recorded vs reference at each frequency). Room rattles cause sharp coherence dips — often a cleaner detector than THD for high-Q object resonances.
**User-facing feedback:** Report top frequencies with elevated THD or low coherence, classify the type, and suggest mitigations:
- Port rattle: add port plug to convert to sealed, tighten HPF below port tuning
- Driver rattle: reduce boost below port tuning, check spider/surround
- Panel rattle: brace the enclosure, check binding posts, tighten screws
- Room object rattle: identify the object at the reported frequency (blu-tack, foam pad under the object, move it)
**Effort:** M — `measurement.py` changes to extract harmonic IRs and coherence from PyTTa; new `thd_db` + `coherence` fields in `FrequencyResponse`/session store; new or extended MCP tool; Claude skill guidance.
**Priority:** P2 — valuable diagnostic for multi-sub calibration where rattles are hard to localize. Natural follow-on after basic calibration loop is stable.

----

### TODO-MODES: Room mode identification and classification
**What:** Analyze the measured FR to distinguish room modes (standing waves) from boundary gain (wall/corner loading). Report identified modes with their frequencies, estimated Q, and whether they are fixable with EQ or not.
**Why:** Users and even calibration tools routinely try to EQ room modes that can't be fixed — deep nulls from cancellation are unfixable, and boosting them wastes headroom and risks driver damage. Knowing which peaks are modes vs. boundary gain changes the EQ strategy entirely.
**How it works:**
- Room modes in a rectangular room occur at predictable frequencies: f = c/2 × sqrt((l/Lx)² + (m/Ly)² + (n/Lz)²). If the user provides room dimensions, we can calculate expected mode frequencies and compare to measured peaks.
- Without room dimensions: identify peaks with high Q (narrow bandwidth) as likely modes; broad peaks are likely boundary gain. Nulls (deep dips) are cancellation — flag as unfixable.
- Classify each feature: "broad peak (boundary gain, EQ-able)", "narrow peak (room mode, cut only)", "null (cancellation, leave alone)"
**User-facing guidance:**
- Boundary gain: "safe to cut, reduces overall bass level but won't create new problems"
- Room mode peak: "cut only — boosting the dip on the other side won't fill it in"
- Null: "this is acoustic cancellation — EQ cannot fix it. Consider repositioning the sub or moving the listening seat."
**Effort:** M — FR analysis + optional room dimension input in config; Claude skill integration.
**Priority:** P2 — prevents users from chasing unfixable problems; improves EQ strategy.

----

### TODO-CLIP: Clipping and gain staging detection
**What:** During measurement, inspect the recorded waveform for clipping (samples at ±full scale) and check gain staging across the chain (AVR volume, DSP input level, DSP output gain). Report if any stage is clipping or if headroom is unbalanced.
**Why:** Clipping upstream of the DSP means EQ is irrelevant — you're measuring a distorted signal. Gain staging problems are silent: the system appears to work but measurements are unreliable and audio quality is degraded.
**How it works:**
- Check `max(abs(recording))` after each sweep — if > 0.95 FS, flag as likely clipping.
- Check DSP input levels from `get_device_state()` during the sweep — if either input hits 0 dBFS, the DSP is clipping internally.
- Suggest gain adjustments: lower AVR sweep volume, adjust DSP input gain, or reduce sub amplifier gain.
**Effort:** S — recording is already available in `MeasurementEngine`; add a post-capture check. DSP input levels already returned by `get_device_state`.
**Priority:** P1 — silent correctness issue; cheap to detect.

----

### TODO-BOUNDARY: Sub boundary distance estimation with placement suggestions
**What:** From the measured frequency response shape, estimate how far the sub is from the nearest wall and corner. Compare to the optimal boundary distance for smooth bass response, and suggest whether moving the sub closer or further from boundaries would improve the FR.
**Why:** Boundary proximity is the single biggest factor in a sub's in-room response after room modes. A sub 30cm from a corner gets ~9 dB of boundary gain at low frequencies; a sub in free space gets none. Understanding placement explains why the FR looks the way it does — and gives actionable improvement advice before any EQ is applied.
**How it works:**
- Boundary gain produces a predictable shelving boost at frequencies where the sub-to-wall distance is less than a quarter wavelength. The shelf frequency and boost level encode the approximate distance.
- Corner loading (two boundaries): ~6 dB more gain than a single boundary; the FR tilt at low frequencies is steeper.
- Estimate: fit the measured low-frequency shelf to a boundary gain model, extract approximate distance(s).
- Compare to "golden ratio" placement (room length × 0.276 from front wall is a common starting point for minimizing modal excitation).
**User-facing guidance:**
- "Your sub appears to be ~40cm from the rear wall with no side wall loading. Moving it into the corner would add ~6 dB of low bass but may excite the room mode at 32 Hz more strongly."
- "Your sub appears to be corner-loaded. Moving it 60–90cm from the corner may reduce the 28 Hz peak without losing much output."
**Effort:** L — requires a curve-fitting model for boundary gain; multiple sub positions make it more complex. Start with single-sub solo measurements.
**Priority:** P2 — high-value guidance, especially for users with placement flexibility.

----

### TODO-SEATS: Seat-to-seat consistency measurement
**What:** Measure the frequency response at multiple listening positions (primary seat, left seat, right seat, rear seats) and report how consistent the bass response is across seats. Flag frequencies with high seat-to-seat variance.
**Why:** A calibration that sounds great in one seat may sound completely different in another. Multi-sub arrays specifically aim to improve seat-to-seat consistency. Knowing the variance before and after calibration is a key success metric.
**How it works:**
- Claude guides the user to move the mic to each seat position and take a measurement.
- Compute mean FR and standard deviation at each frequency across all seats.
- Report: overall consistency score (mean std dev across 20–80 Hz), worst frequencies, best/worst seats.
- Compare pre- and post-calibration consistency to show whether the calibration helped.
**User-facing guidance:**
- "Seat variance is ±4 dB at 42 Hz — this is a room mode that affects different seats differently. EQ at one seat will make other seats worse. Consider sub repositioning."
- "After calibration, seat-to-seat variance improved from ±6 dB to ±3 dB across 20–80 Hz."
**Effort:** M — multiple measurement sessions with positional labels; variance computation; Claude skill to guide the user through each seat.
**Priority:** P2 — essential metric for multi-sub calibration quality assessment.

----

### TODO-BEFOREAFTER: Calibration before/after comparison
**What:** Automatically take a "before" measurement at the start of every calibration run and compare it to the "after" measurement at the end. Report the improvement as: RMS deviation reduction from target, peak reduction at identified problem frequencies, and a plain-language summary.
**Why:** Calibration is abstract — users can't hear a ±2 dB RMS improvement easily. Showing "your 38 Hz peak was +8 dB, now it's +1 dB" makes the value of calibration concrete and builds trust in the system.
**How it works:**
- Store the pre-calibration baseline measurement at the start of the recipe execution.
- After convergence, measure again and diff against the baseline.
- Compute: RMS deviation (pre vs post vs target), peak count above threshold (pre vs post), worst-case deviation (pre vs post).
- Generate a summary: "3 peaks reduced by average 5.2 dB. RMS deviation from Harman target: 6.1 dB → 2.3 dB."
**Effort:** S — measurement infrastructure already exists; just needs a before/after diff step in the recipe execution flow and a summary formatter.
**Priority:** P1 — low effort, high user satisfaction impact. Should be part of every calibration run.

----

### TODO-PHASE: AVR phase sweep optimization
**What:** For AVRs with a continuously variable subwoofer phase control (Denon X3800H supports 0°–180°), sweep the phase in increments and measure SPL at each setting. Report the optimal phase for the primary listening position.
**Why:** Phase alignment between the sub and satellite speakers at the crossover frequency (typically 80 Hz) significantly affects bass punch and clarity. The optimal phase depends on sub placement relative to the main speakers — it cannot be calculated from first principles without measurement.
**How it works:**
- Set AVR to a mode where both satellites and sub are playing (or play a sine wave at the crossover frequency, e.g. 80 Hz).
- Sweep sub phase 0°→180° in 15° increments via denonavr API (if supported) or prompt the user to adjust manually.
- Measure SPL at the crossover frequency at each phase setting.
- The phase with the highest SPL at 80 Hz gives the best sub-satellite integration.
**Note:** Denon X3800H phase control via denonavr library — needs verification that the API supports programmatic phase adjustment. If not, guide the user to adjust manually and press a key at each step.
**Effort:** M — denonavr phase control verification; sweep loop; crossover-frequency SPL measurement (may need to extend measurement frequency range or use a single-tone measurement).
**Priority:** P2 — meaningful improvement in perceived bass quality; often overlooked in calibration guides.

----

### TODO-REPORT: Calibration report generation
**What:** After a calibration run completes, generate a human-readable HTML or PDF report summarizing: hardware configuration, measurements taken, problems found (room modes, rattles, clipping), corrections applied (EQ filters, delay, polarity), before/after FR comparison chart, and recommendations for further improvement.
**Why:** Users want to share results with home theater forums, keep a record for future reference, or understand what the system did. A report also builds trust — it shows the user exactly what was changed and why.
**What it includes:**
- System config (subs, DSP, AVR, mic)
- Alignment results (delay corrections, polarity flips, level trims)
- Per-sub FR charts (before/after)
- Combined FR chart (before/after vs Harman target)
- Filters applied (listed as human-readable EQ bands, not raw biquad coefficients)
- Diagnostics: room modes identified, rattles detected, clipping warnings
- Remaining issues and suggestions (placement, room treatment)
- Calibration timestamp and session IDs for reproducibility
**Effort:** M — data is all available in the session store and EQ state; needs a report template (HTML is simplest, PDF via weasyprint or similar). Claude generates the narrative sections.
**Priority:** P2 — polish feature; implement after core calibration loop is stable and reliable.

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

## Room Optimization

### TODO-11: Sub crawl — guided placement optimization
**What:** A guided workflow where the user places the UMIK mic at the listening position and moves the sub (or places the sub at the listening position and walks the mic around the room — "reverse sub crawl"). At each candidate position, the system takes a measurement and logs the FR. After all positions are measured, the system recommends the best placement based on smoothest bass response, fewest nulls, and best extension.
**Why:** Sub placement is the single highest-impact variable in bass calibration. No amount of EQ can fix a sub in a null. The classic "sub crawl" technique works but is tedious and imprecise without measurement data at each position. An AI-guided version with real FR data at each spot turns a 45-minute guessing game into a 15-minute measured decision.
**How:** TBD. Core pieces: (1) take N measurements at labeled positions, (2) store all FRs, (3) score/rank by some metric (flatness, extension, null depth), (4) recommend best position. Could be MCP-driven ("measure position A... now move to B... measuring...") or web-driven with a room map UI.
**Effort:** M-L — measurement infra exists, scoring algorithm and UX are the new work.
**Priority:** P2 — high user value, but requires the measurement loop to be solid first.

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

### TODO-FIR1: FIR inverse pre-filtering recipe for T60 ringing reduction
**What:** A new recipe `recipes/core/harman-bass-fir.md` that implements minimum-phase inverse filtering via FIR to reduce room mode T60 (decay time), not just amplitude. Workflow: measure full IR per sub → compute regularized inverse filter (avoid boosting nulls) → load FIR coefficients per output via `apply_fir` → compensate AVR sub delay for FIR latency (~10ms at 96kHz/2048 taps).
**Why:** PEQ cuts reduce ringing amplitude but not T60 — the mode decays for the same duration, just quieter. True T60 reduction requires inverse pre-filtering (what Dirac Live Bass Control does). The miniDSP 2x4 HD has the FIR capability (2048 taps/output, 4096 shared @ 96kHz) to implement this.
**Constraints:** Regularized inversion required to avoid dangerous boosts at null frequencies. Latency (~10.7ms) must be compensated in AVR delay settings. FIR writes via CLI only (not HTTP). Sequential, never parallel.
**Deferred because:** Current PEQ workflow (analyze_decay → narrow-Q cuts) is the practical equivalent for amplitude reduction. FIR inverse pre-filtering is Dirac-level complexity and warrants its own tooling before a recipe can be written.
**Effort:** L — requires IR inversion tooling (likely scipy), regularization logic, FIR coefficient pipeline, and latency compensation in config/MCP.
**Priority:** P3 — nice-to-have; only meaningfully better than PEQ for T60 reduction, not amplitude reduction.
