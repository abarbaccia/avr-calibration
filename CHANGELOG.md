# Changelog

All notable changes to this project will be documented in this file.

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
