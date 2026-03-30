# Changelog

All notable changes to this project will be documented in this file.

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
