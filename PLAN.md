<!-- /autoplan restore point: /home/andrew/.gstack/projects/abarbaccia-avr-calibration/feat-pi5-headless-readiness-autoplan-restore-20260401-210330.md -->

# Plan: Pi 5 Headless Readiness

**Branch:** feat/pi5-headless-readiness
**Base:** main
**Date:** 2026-04-01

## Feature Brief

**One-liner:** The Docker image builds for arm64, the UMIK-1 is auto-detected on Pi 5, and a measurement sweep can be triggered headlessly via CLI or MCP tool without a browser.

**Why now:** Pi 5 arrives this week. It has 4 USB ports meaning miniDSP and UMIK-1 can coexist for the first time. This unlocks the autonomous calibration loop the Zero 2 W could never run.

**In scope:**
- arm64 Docker CI build target (GitHub Actions — add `linux/arm64` to platforms)
- UMIK-1 auto-detection on Pi 5 (sounddevice device enumeration, select UMIK by name)
- `/api/measure` headless endpoint (Pi-side record via UMIK, not browser getUserMedia)
- `calibrate measure` CLI works headlessly on Pi 5 (already uses `MeasurementEngine.measure()`)
- `trigger_measurement` MCP tool returns real result on Pi 5, not degraded mode

**Out of scope:**
- Full autonomous loop (sweep → analyze → apply EQ → re-measure) — next feature
- Pi Zero 2 W changes — arm/v7 path stays as-is (browser audio remains)
- UMIK calibration file (.cal) application — already wired; just needs UMIK detected

## Problem Decomposition

### What exists today

| Sub-problem | File | Status |
|-------------|------|--------|
| CI arm/v7 + amd64 build | `.github/workflows/docker.yml` | Working — platforms: `linux/arm/v7,linux/amd64` |
| PyTTa included for amd64 | `Dockerfile` else-branch | Working — `--extra measurement` |
| PyTTa excluded for arm/v7 | `Dockerfile` arm-branch | Correct — browser captures audio on Pi Zero |
| `calibrate measure` CLI | `calibrate/cli.py:83` + `MeasurementEngine.measure()` | Works on amd64; uses PyTTa PlayRecMeasure |
| `MeasurementEngine.measure()` | `calibrate/measurement.py:313` | Exists; uses `pytta.PlayRecMeasure`; no UMIK device selection |
| UMIK detection | `calibrate/mcp_server.py:196-203` | Inline in `_tool_trigger_measurement`; not reusable |
| MCP `trigger_measurement` | `calibrate/mcp_server.py:189-227` | Detects UMIK, then POSTs to `/api/measure` |
| `/api/measure` endpoint | `calibrate/web.py` | DOES NOT EXIST — only `/api/measure/start` + `/api/measure/record` |

### The critical gap

The MCP tool (`trigger_measurement`) calls `POST /api/measure` which returns 404 today. The whole headless path is blocked on this missing endpoint.

`/api/measure/start` and `/api/measure/record` are the browser-split path. We need a unified headless endpoint that calls `MeasurementEngine.measure()` directly on the Pi.

### Arm64 Docker gap

The Dockerfile's conditional is:
```bash
if [ "$TARGETARCH" = "arm" ] && [ "$TARGETVARIANT" = "v7" ]
```
Pi 5 has `TARGETARCH=arm64`, `TARGETVARIANT=` (empty). This falls to the else-branch which already runs `uv sync --extra measurement` — PyTTa IS included for arm64. The Dockerfile already handles arm64 correctly. Only CI platforms needs adding.

## Implementation Steps

### Step 1: CI — Add arm64 platform
**File:** `.github/workflows/docker.yml`
**Change:** `platforms: linux/arm/v7,linux/amd64` → `platforms: linux/arm/v7,linux/arm64,linux/amd64`
**QEMU:** `docker/setup-qemu-action@v3` with `platforms: arm` needs to add `arm64` (or `all`)
**Build time concern (CEO Finding 5):** aarch64 manylinux wheels exist on PyPI for numpy and scipy. sounddevice C extension needs PortAudio; builder stage already has `portaudio19-dev`. PyTTa is pure Python. Build time for arm64 should be similar to arm/v7 (~30-60 min in QEMU). Consider making arm64 a separate non-blocking workflow job if it proves flaky during initial bringup.

### Step 2.5: Gate — Validate PyTTa device selection (BLOCKING, CEO Finding 2)
**Before writing Step 3 or Step 4 code, verify this:**
```python
import sounddevice as sd
import pytta
sd.default.device = (UMIK_INDEX, sd.default.device[1])
# Does pytta.PlayRecMeasure respect this? Check by logging which device index is opened.
```
If PyTTa ignores `sd.default.device`, use sounddevice directly for recording:
```python
# Alternative: play via sd.play(), record via sd.rec() with device=UMIK_INDEX
recording = sd.rec(frames, samplerate=sr, channels=1, device=UMIK_INDEX)
sd.play(sweep, samplerate=sr, device=output_idx)
sd.wait()
```
This is the sounddevice fallback. Implement this path if PyTTa doesn't expose device selection.

### Step 2: Add UMIK device detection utility
**File:** `calibrate/measurement.py`
**Change:** Add `_find_umik_device(devices) -> int | None` function that searches sounddevice device list for UMIK by name substring.

### Step 3: Wire UMIK device into MeasurementEngine.measure()
**File:** `calibrate/measurement.py`
**Change:** Add `input_device_name: str | None = None` parameter to `measure()`. Before creating `pytta.PlayRecMeasure`, if `input_device_name` is set, use sounddevice to find the device index and call `sd.default.device = (input_idx, output_idx)`. PyTTa respects sounddevice defaults.

### Step 4: Add /api/measure headless endpoint
**File:** `calibrate/web.py`
**New endpoint:** `POST /api/measure`
```python
class HeadlessMeasureRequest(BaseModel):
    label: str | None = None

@app.post("/api/measure")
async def measure_headless(body: HeadlessMeasureRequest) -> dict:
    """Headless measurement for Pi 5. Requires UMIK-1 connected and PyTTa installed."""
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        umik_devices = [(i, d) for i, d in enumerate(devices)
                        if "UMIK" in str(d.get("name", "")) and d.get("max_input_channels", 0) > 0]
        if not umik_devices:
            raise HTTPException(status_code=503, detail="No UMIK microphone found")
    except ImportError:
        raise HTTPException(status_code=503, detail="sounddevice not available on this platform")

    cfg = Config.load()
    engine = MeasurementEngine(cfg)
    device_name = umik_devices[0][1]["name"]
    try:
        fr = await asyncio.to_thread(engine.measure, input_device_name=device_name)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    store = SessionStore(cfg.db_path)
    session_id = store.save_measurement(fr, label=body.label or "headless")
    return {"session_id": session_id, "status": "ok"}
```

### Step 5: Update MCP trigger_measurement
**File:** `calibrate/mcp_server.py`
**Changes (CEO Finding 3 — all 4 locations):**
- Line 191: docstring "Requires Pi 4" → "Requires Pi 5"
- Line 201-203: "trigger_measurement requires Pi 4 — no UMIK" → "trigger_measurement requires Pi 5"
- Line 206-209: "trigger_measurement requires Pi 4 — audio device" → "trigger_measurement requires Pi 5"
- `_TOOLS` tool description line 388-390: "Requires Pi 4 4GB" → "Requires Pi 5 (4 USB ports)"
- Pass `{"label": "mcp-triggered"}` — already correct
- No structural changes needed (already POSTs to `/api/measure`)

### Step 6: Tests
- `tests/test_measurement_headless.py`: test `/api/measure` endpoint
  - UMIK detected: returns session_id
  - No UMIK: 503
  - sounddevice unavailable: 503
  - sounddevice device detection mocked
- `tests/test_measurement.py`: test `MeasurementEngine.measure(input_device_name=...)` sets sd.default.device
- CI arm64 build: validated by GitHub Actions (no unit test)

## Risk Assessment

### R1: PyTTa/sounddevice arm64 build — MEDIUM
PyTTa requires sounddevice which requires PortAudio. PortAudio (`portaudio19-dev`) is already in the builder stage. sounddevice on arm64 should build from source via PyPI (manylinux wheels exist for aarch64). PyTTa itself is a pure-Python wrapper around sounddevice + numpy. Verdict: likely works, but untested. If it fails, fallback is to skip PyTTa `PlayRecMeasure` and implement play+record via sounddevice directly.

### R2: pytta.PlayRecMeasure ignores sd.default.device — HIGH (must verify)
If PyTTa opens its own PortAudio stream without respecting `sd.default.device`, UMIK selection won't work. Mitigation: test on amd64 first; if PyTTa ignores defaults, implement UMIK recording via `sounddevice.rec()` directly (bypass PyTTa for the record step; still use PyTTa for sweep generation).

### R3: /api/measure called from browser accidentally — LOW
New endpoint should only be called from MCP or CLI. No browser JS calls it. Already isolated by endpoint name.

### R4: Pi 5 USB enumeration order — LOW
On Pi 5, USB devices enumerate in plug-in order. UMIK device index could be 0, 1, or 2 depending on what else is plugged in. The name-based search (`_find_umik_device`) handles this correctly regardless of index.

## Eng Review Additions

### P0: Measurement lock required (F1 + F2)
`sd.default.device` is a global shared across threads. Two concurrent `/api/measure` calls will race on PortAudio. Fix: add module-level `asyncio.Lock` in web.py, held for the full duration of `measure()`. Return HTTP 409 if lock is already held.

```python
_measurement_lock = asyncio.Lock()

@app.post("/api/measure")
async def measure_headless(body: HeadlessMeasureRequest) -> dict:
    if _measurement_lock.locked():
        raise HTTPException(status_code=409, detail="measurement already in progress")
    async with _measurement_lock:
        ...
```

The lock also prevents the `sd.default.device` race (F1) since no two calls can set the device concurrently.

### P1: Dockerfile — arm64 gets wrong minidsp binary (F6)
Current: `if [ "$ARCH" = "amd64" ]; then DEB_ARCH="amd64"; else DEB_ARCH="armhf"; fi`
For TARGETARCH=arm64, ARCH="arm64" → falls to else → DEB_ARCH="armhf" → 32-bit binary on 64-bit kernel → Exec format error.

Fix:
```bash
if [ "$ARCH" = "amd64" ]; then DEB_ARCH="amd64"; \
elif [ "$ARCH" = "arm64" ]; then DEB_ARCH="arm64"; \
else DEB_ARCH="armhf"; fi
```
(Verify `minidsp_0.1.12-1_arm64.deb` exists in mrene/minidsp-rs releases.)

### P1: QEMU needs arm64 (F7)
Fix: `platforms: arm,arm64` (or `platforms: all`) in `docker/setup-qemu-action@v3`.

### P1: measure() missing input_device_name param (F4)
Must implement BEFORE Step 4 endpoint. Current signature has no params. Add:
```python
def measure(self, input_device_name: str | None = None) -> FrequencyResponse:
    ...
    if input_device_name:
        import sounddevice as sd
        devs = sd.query_devices()
        for i, d in enumerate(devs):
            if input_device_name in str(d.get("name", "")) and d.get("max_input_channels", 0) > 0:
                sd.default.device = (i, sd.default.device[1] if isinstance(sd.default.device, tuple) else sd.default.device)
                break
```

### P1: SessionStore() call pattern (F5)
All existing calls use `SessionStore()` with no args. Plan draft incorrectly used `SessionStore(cfg.db_path)`. Fix: use `store = SessionStore()`.

### P1: UMIK disconnect / PortAudioError not caught (F3 + F11)
`measure()` calls `_compute_fr()` directly (not `compute_fr()`), bypassing quality checks. Two fixes:
1. Wrap `meas.run()` in try/except for all exceptions, re-raise as RuntimeError
2. Call `validate_recording()` inside `measure()` before deconvolution
3. In the endpoint, catch both `RuntimeError` and `sounddevice.PortAudioError` → 503

### P2: Add mic_device_name config key (F9)
UMIK may present as "C-Media USB Audio Device" on Linux. Add to config defaults:
```yaml
measurement:
  mic_device_name: "UMIK"   # substring match for input device name
```
`_find_umik_device()` uses this config key instead of hardcoded "UMIK".

### P2: MCP timeout too short (F10)
`httpx.AsyncClient(timeout=30.0)` in `_tool_trigger_measurement()` — a 3s sweep + Pi 5 startup can exceed 30s under load. Increase to `timeout=60.0`. Document expected latency in tool description.

### P2: sounddevice aarch64 wheel validation (F8)
Before declaring arm64 ready: verify PyPI `sounddevice` has `manylinux_2_17_aarch64` wheel. If falls back to source build, builder stage has `portaudio19-dev` — will succeed but be slower.

### P3: measure() headless path skips quality gate (F12)
Add `validate_recording()` call inside `measure()` before `_compute_fr()`. Already in plan (F3 fix — same change).

## Done Criteria
- `docker build --platform linux/arm64` succeeds in CI and image pushes to ghcr.io
- On Pi 5: `calibrate measure` completes, saves a measurement file with real data (UMIK used as input)
- On Pi 5: MCP `trigger_measurement` call returns `{"status": "ok", "session_id": N}` not `{"error": "..."}`
- All existing tests pass (arm/v7 + amd64 paths unchanged)

## Eng Review — Test Plan
Test plan artifact: `~/.gstack/projects/abarbaccia-avr-calibration/andrew-feat-pi5-headless-readiness-test-plan-20260401-211515.md`

New test files:
- `tests/test_measurement_headless.py` — 9 new tests for `measure()` device param + `_find_umik_device()`
- `tests/test_api_measure_headless.py` — 5 new integration tests for `POST /api/measure`
- Update `tests/test_mcp_server.py` — update "Pi 4" → "Pi 5" assertions, verify 60s timeout

## Eng Review — NOT In Scope (confirmed deferred)
- CamillaDSP or other DSP driver (zero relevance to Pi 5)
- sounddevice aarch64 wheel caching in CI (optimization, not correctness)
- Full PyTTa device selection rewrite (try `sd.default.device` first; fallback only if broken)
- arm64 native runner (too expensive for hobby project; QEMU acceptable)

## Eng Review — What Already Exists
| Sub-problem | Existing code |
|---|---|
| Headless sweep measurement | `measurement.py:313` `measure()` — exists; needs device param + quality gate |
| UMIK detection (inline) | `mcp_server.py:196-203` — extract into utility |
| Session persistence | `web.py SessionStore()` pattern — established; use no-arg form |
| Measurement quality validation | `measurement.py validate_recording()` — exists; wire into `measure()` |
| PortAudio runtime | `libportaudio2` in Dockerfile runtime stage — already installed |
| PyTTa deps for arm64 | Dockerfile else-branch — already includes `--extra measurement` |

## Not In Scope (confirmed deferred)
- Full autonomous loop (analyze → apply EQ → re-measure)
- MicDriver abstraction (TODO-DA1) — design doc exists; implement after headless path is validated
- Multi-channel sweep (TODO-6) — Pi 5 HDMI audio driver opens that door, but deferred
- Pi Zero 2 W: zero changes to arm/v7 Dockerfile path

## CEO Review — NOT In Scope (deferred)
- PyTTa measurement quality validation vs. REW (TODO-MQ1 — prerequisite before autonomous loop feature)
- Making arm64 a non-blocking CI job — do if arm64 build proves flaky
- MicDriver abstraction (TODO-DA1) — implement after this feature validates the Pi 5 path

## CEO Review — What Already Exists
- `calibrate measure` CLI uses `MeasurementEngine.measure()` — headless path already validated on amd64
- UMIK detection logic in `mcp_server.py:196-203` — inline, extract into utility
- Dockerfile else-branch already handles arm64 with PyTTa included
- CI QEMU setup already configured; just needs `arm64` added

## CEO Review — Failure Modes Registry

| Mode | Trigger | Impact | Mitigation |
|------|---------|--------|------------|
| PyTTa ignores sd.default.device | PyTTa opens own PortAudio stream | Wrong device records; garbage FR | Step 2.5 gate; sounddevice fallback |
| sounddevice C extension fails arm64 | Missing PortAudio headers | PyTTa import fails; CLI 503 | portaudio19-dev already in builder stage |
| UMIK not found by name | Device shows as "C-Media USB" not "UMIK" | 503 on all measurements | Add `mic_device_name` config key for substring |
| /api/measure called during browser session | Concurrent measurement | Double-sweep, race condition | Add `_measurement_lock: asyncio.Lock()` in web.py |
| arm64 CI QEMU timeout | Large C extension builds | CI failure | Separate non-blocking arm64 job |

## CEO Review — Dream State Delta
```
CURRENT STATE (today, Pi Zero 2 W):
  miniDSP connected via USB OTG (single port)
  UMIK-1 on laptop → browser getUserMedia → Float32 sent to Pi
  Manual: human opens browser, clicks measure, reads results

THIS PLAN (Pi 5, this branch):
  miniDSP + UMIK-1 both connected (4 ports)
  MCP trigger_measurement → /api/measure → MeasurementEngine → FR saved
  calibrate measure CLI also works on Pi 5
  Browser path unchanged (still works for manual use)

12-MONTH IDEAL:
  Claude agent calls trigger_measurement → gets FR → calls apply_eq → calls trigger_measurement again
  Closed loop runs until FR matches Harman target within tolerance
  Human reviews at end; each iteration takes <60s
```

## Decision Audit Trail

| # | Phase | Decision | Classification | Principle | Rationale | Rejected |
|---|-------|----------|----------------|-----------|-----------|----------|
| 1 | CEO | Add Step 2.5 gate — validate PyTTa device selection before endpoint ships | Mechanical | P1 completeness | R2 is Critical; endpoint with wrong device records garbage FR | Ship without validation |
| 2 | CEO | Fix all 4 "Pi 4" references in mcp_server.py (docstring + 2 errors + tool desc) | Mechanical | P1 completeness | All 4 are wrong; plan Step 5 originally missed docstring | Fix only error messages |
| 3 | CEO | Reject SSH-to-CLI alternative (Finding 4) | Mechanical | P3 pragmatic | MCP server is the designed integration point; SSH is a workaround | Replace MCP with SSH |
| 4 | CEO | Note arm64 CI build time concern; check aarch64 wheels | Mechanical | P1 completeness | QEMU arm64 could be slow; document the risk | Ignore |
| 5 | CEO | Add measurement quality validation to TODOS.md | Mechanical | P2 boil lakes | Valid concern; prerequisite for autonomous loop; zero code in this PR | Include in this PR |
| 6 | CEO | TASTE: Ship infra-only vs. include loop iteration | TASTE | User chose scope | User explicitly picked "headless trigger + infra" scope in /feature | See gate |
| 7 | CEO | TASTE: PyTTa quality validation as prerequisite for next feature | TASTE | P3 pragmatic | Important long-term concern; zero relevance to arm64 infra work | See gate |
| 8 | Eng | P0: Add _measurement_lock asyncio.Lock to prevent concurrent sweep + sd.default.device race | Mechanical | P1 completeness | Two concurrent calls race on PortAudio global state | Accept race |
| 9 | Eng | P1: Fix Dockerfile arm64 minidsp binary selection (armhf → arm64) | Mechanical | P1 completeness | armhf binary on arm64 kernel = Exec format error; minidspd never starts | Leave armhf |
| 10 | Eng | P1: Fix QEMU platforms to include arm64 | Mechanical | P1 completeness | Without aarch64 QEMU, arm64 build fails at first RUN | Leave arm only |
| 11 | Eng | P1: Implement measure(input_device_name) before Step 4 | Mechanical | P5 explicit | Plan's endpoint calls this param; current signature has none → TypeError | Hardcode device |
| 12 | Eng | P1: Use SessionStore() not SessionStore(cfg.db_path) | Mechanical | P3 pragmatic | All existing calls use no-arg pattern; inconsistency causes TypeError | Use db_path |
| 13 | Eng | P1: Catch PortAudioError + add validate_recording in measure() | Mechanical | P1 completeness | Disconnect mid-measure returns garbage FR silently saved to DB | Accept garbage |
| 14 | Eng | P2: Add mic_device_name config key for Linux device name mismatch | Mechanical | P1 completeness | UMIK presents as "C-Media USB" on Linux; hardcoded "UMIK" fails | Hardcode "UMIK" |
| 15 | Eng | P2: Increase MCP trigger_measurement timeout to 60s | Mechanical | P1 completeness | 30s too short for 3s sweep + deconvolution on Pi 5 under load | Leave 30s |
| 16 | Eng | P3: validate_recording in measure() headless path | Mechanical | P1 completeness | Same fix as decision 13; no separate work | Separate task |

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/autoplan` | Scope & strategy | 1 | issues_open | 7 findings; 2 taste decisions at gate; 5 auto-decided; QEMU + PyTTa gate + "Pi 4" coverage added |
| CEO Voices | `/autoplan` | Dual independent review | 1 | subagent-only | Codex unavailable; Claude subagent: 6 findings (1 critical, 2 high, 3 medium/low) |
| Design Review | skipped | No UI scope | 0 | — | — |
| Eng Review | `/autoplan` | Architecture & tests | 1 | issues_open | 13 findings (2 P0, 5 P1, 4 P2, 2 P3); all auto-decided; test plan written |
| Eng Voices | `/autoplan` | Dual independent review | 1 | subagent-only | Codex unavailable; Claude subagent: 13 findings |

**VERDICT:** REVIEWED [subagent-only, Codex unavailable]. Critical bugs caught: armhf minidsp binary on arm64 (P1), QEMU missing arm64 (P1), no measurement lock (P0). Plan is implementation-ready.
