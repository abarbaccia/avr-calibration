# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# avr-calibration

AI-first home theater calibration — closed-loop bass optimization for Denon X3800H + Focusrite Scarlett 18i20 + CamillaDSP + SVS PB12-NSD subs.

## Operating mode — be aggressive

There is a real deadline. Push forward on real problems; do not wrap up sessions with "let's pick this up tomorrow" language. When you hit a wall:

1. Propose the next concrete diagnostic / fix, with specifics.
2. Rank by likelihood and blast radius.
3. Pick the highest-leverage one and execute.

Recovery paths exist (push_avr_speaker_layout, MultEQ Editor, .ady backups, PSMULTEQ:OFF). Don't stop at the first AVR crash — recover and push on. The user prefers honest failures + retries over premature wrap-ups.

When in doubt: keep going, take action, course-correct on user feedback.

## Architecture — Claude is the Orchestrator

**Core principle: Claude Code drives calibration, not Python.**

Python provides MCP tool primitives (measure, apply EQ, mute/unmute, set delay). Claude reads human-readable recipe files and calls those tools in a loop.

**Do NOT build Python orchestrators, phase runners, or loop state machines.** If you're writing a `for` loop in Python that calls measure→analyze→apply→repeat, STOP — that logic belongs in a Claude skill or recipe, not in Python code.

```
[ Claude Code ]  ←── reads recipe, drives calibration loop
       |
       | MCP tool calls (high-level actions)
       ▼
[ Pi 5 @ 192.168.1.117 — Docker (arm64) ]
  ├── MCP server (thin facade) — calibrate/mcp_server.py (~900 tool handlers)
  │     Plugin drivers (Claude never sees these directly)
  │     ├── DenonDriver         AVR volume, input, sweep context
  │     ├── CamillaDSP driver   DSP EQ, mute, delay, polarity, FIR (primary sub path)
  │     ├── MinidspDriver       Legacy: miniDSP 2x4 HD (kept for regression)
  │     ├── MeasurementEngine   UMIK + PyTTa sweep/deconvolution
  │     └── SafetyValidator     hard limits, NEVER bypassed
  ├── Web UI            ← browser dashboard (read-only)
  └── SessionStore      ← SQLite history
```

### What belongs where

| Concern | Where |
|---------|-------|
| Loop logic (measure→analyze→adjust→repeat) | Claude skill / recipe |
| Decision-making (what to adjust next) | Claude |
| Filter design (which freqs, PEQ vs FIR) | Claude |
| Numerical computation (FFT, biquad math) | MCP tools |
| Analytics (phase, coherence, fixability) | MCP tools |
| Hardware protocol (ordering, cleanup) | Plugin drivers |
| Safety enforcement | SafetyValidator (code) — `calibrate/safety.py` |
| Recipes | Markdown files in `recipes/core/` |
| Hardware config | `config.yaml` |

### MCP tool design principle
Claude sees **actions** ("measure", "apply EQ"), not **hardware** ("set Denon input", "POST to minidsp-rs"). The MCP server is a thin facade. Each tool handler is ~5 lines delegating to a plugin driver.

### LLM-first tool design — HARD RULE

**Tools provide DATA and SIMULATION. The LLM provides JUDGMENT.**

Never build deterministic solvers for decisions the LLM should make. Tools provide analytics (phase, coherence, fixability) and simulation (predicted FR). The LLM reasons about the full context and decides what to correct, where, and how.

```
WRONG:  measure → suggest_filters() → apply     (Python decides)
RIGHT:  measure → analyze_phase() → LLM reasons → simulate_eq() → LLM adjusts → apply
```

## Development

```bash
# Set up environment
uv venv .venv && source .venv/bin/activate
uv sync --extra dev

# Run all tests
uv run python -m pytest tests/ -v

# Run a single test file
uv run python -m pytest tests/test_modal_fir.py -v

# Run with coverage
uv run python -m pytest tests/ --cov --cov-report=term-missing
```

## Testing conventions (from TESTING.md)

- `tests/test_{module}.py` mirrors `calibrate/{module}.py`
- `sounddevice` and `pytta` are injected into `sys.modules` via session-scoped fixtures in `conftest.py` — never need real audio hardware in tests
- Async tests: `pytest-asyncio` mode=auto (set in `pyproject.toml`)
- miniDSP CLI: patch `calibrate.adapters.minidsp._run_minidsp_cli` (AsyncMock)
- Denon: patch `denonavr.DenonAVR` with AsyncMock
- When a new function is added, cover happy path + each error branch + edge cases

## Deployment

- Docker image built by GitHub Actions on every push to `main` → `:latest`
- arm64 cross-compiled in CI; no compilation on the Pi
- Source installed at `/opt/venv/lib/python3.11/site-packages/calibrate/` inside container
- Pi 5 at `192.168.1.117` (user `pi`)

**Primary workflow: hotfix first, pipeline second.**

```bash
./deploy/hotfix.sh                    # auto-detects modified calibrate/ files
./deploy/hotfix.sh calibrate/web.py   # specific file

# After CI build completes:
ssh pi@192.168.1.117 "sudo docker pull ghcr.io/abarbaccia/avr-calibration:latest && sudo systemctl restart avr-calibration"
```

**PipeWire state after Pi restart:** The Docker container shares the host's PipeWire socket via `/run/user/1000`. After reboots or multiple rapid container restarts, the PW session inside the container can become stale — `pw-cat` hangs with exit 124. The fix is a full Pi reboot (not just container restart) so the host PipeWire daemon reinitializes cleanly.

**Audio mode:** Run `/usr/local/sbin/audio-mode set cal` before measurements. Modes: `listening`, `cal`, `karaoke`. CamillaDSP owns the Scarlett directly in cal mode.

## Key modules

| Module | Purpose |
|--------|---------|
| `calibrate/mcp_server.py` | All MCP tool handlers (~900 tools). Each handler is ~5 lines; business logic is in the modules below. |
| `calibrate/measurement.py` | PyTTa sweep + deconvolution. `MeasurementEngine._compute_fr_arrays()` is the core. IR gate: 500ms, with optional `direct_path_window_ms` for time-windowed analysis above Schroeder frequency. |
| `calibrate/decay.py` | T60 analysis via spectrogram + Schroeder integration. scipy.signal pre-imported at module level for fast first call. |
| `calibrate/modal_fir.py` | `ModalAwareFIRDesigner` + `design_anti_pulse()`. Anti-pulses use Gabor wavelets; default `n_cycles=1` (CRITICAL: higher values truncate trailing half and flip phase from −π to 0, amplifying modes instead of cancelling). |
| `calibrate/multi_fir.py` | `design_multi_input_fir()` — regularized Wiener inverse for N-sub coherent FIR design. `design_fir_multi_modal()` — combines Wiener magnitude correction with anti-pulse T60 correction in a single FIR buffer. |
| `calibrate/safety.py` | `SafetyValidator` — hard limits enforced before every EQ/FIR write. Profile-based. |
| `calibrate/drivers/camilladsp.py` | CamillaDSP driver. Owns the PipeWire routing, FIR application, per-output state. Config rebuilt and pushed on every state change. |
| `calibrate/drivers/playback.py` | `HDMIPwCatPlayback` for USB/PipeWire sub sweeps. Uses `pw-cat --target avr_cal_sweep` piped PCM stdin. |
| `calibrate/graph.py` | Signal graph: maps transducer names → output indices → DSP processor. Used by restore_listening_mode and routing tools. |
| `calibrate/storage.py` | SQLite: sessions, FR data, calibration runs, lessons. |

## Hardware (current production setup)

- **AVR:** Denon X3800H — denonavr library, TCP port 1256 for Audyssey
- **DSP:** CamillaDSP via PipeWire → Focusrite Scarlett 18i20
  - Sub outputs: Scarlett lines 5/6/7 (direct-PCM, not via monitor bus)
  - Sub cal signal path: `pw-cat → avr_cal_sweep PW null sink → camilladsp_capture:input_3 (LFE feed → CamillaDSP → Scarlett → subs)`. **⚠️ The `input_3` link is LOAD-BEARING — it is the feed that drives the subs. Do NOT remove it; tearing it down silences the subs (coherence ~0.5, mic SNR ~0; verified 2026-06-10). `input_2` alone does NOT drive the subs.** The deconvolution reference is a SEPARATE tap: `avr_cal_sweep:monitor_FL → loopback_ref` (its own PW null-sink node), NOT a camilladsp_capture port.
- **Mic:** UMIK-1 or UMIK-2 (UMIK .cal correction applied)
- **Subs:** SVS PB12-NSD (ported, ~22Hz tuning), output indices 5 and 6
- **Shaker:** Earthquake MQB-1 on Behringer NX3000, output index 7 — **ALWAYS muted during calibration measurements**

## Signal chain (USB sub calibration)

```
pw-cat → avr_cal_sweep (PW null sink)
   ├─ monitor_FL/FR → camilladsp_capture:input_3   [LFE feed → CamillaDSP → subs]  ◀ REQUIRED, do not remove
   │                    → CamillaDSP lfe_source mixer → outputs 5, 6 (subs) / 7 (shaker, muted)
   │                    → Scarlett 18i20 line outputs → subs → room
   └─ monitor_FL → loopback_ref:playback_1          [pre-DSP deconvolution reference X]

UMIK-1 (its own USB device) → captured directly by the measurement engine   [mic Y]
Deconvolution:  H = Y(mic) / X(loopback_ref)
```

The `input_3` link is the **LFE feed that drives the subs** (CamillaDSP capture channel — the script's `lfe_input_channel=2`). It must always be present: tearing it down silences the subs (coherence collapses to ~0.5, mic SNR ~0; verified the hard way 2026-06-10). The **deconvolution reference is the SEPARATE `loopback_ref` null-sink node** (`avr_cal_sweep:monitor_FL → loopback_ref:playback_1`), captured pre-CamillaDSP. The mic is the UMIK captured directly (its own USB device), NOT via the Scarlett. Without the loopback ref, coherence collapses. (`input_2` is also linked but is vestigial — it does not drive the subs on its own.)

**Sub-only measurements bypass the Denon** — inject sweep via Pi → CamillaDSP → Scarlett → subs directly. Never route sub cal sweeps through the Denon LFE pre-out (Audyssey/MultEQ corrupt the stimulus).

## Safety limits (SVS PB12-NSD) — code-enforced

Enforced in `SafetyValidator` before every write. Never bypass.

- Minimum boost frequency: **25 Hz**
- Max boost per EQ band: **+6 dB**
- Max cumulative boost in any 1/3 octave: **+9 dB**
- Max change per iteration: **+3 dB/band**
- Mandatory infrasonic HPF: **18 Hz, 4th-order Butterworth** (always on)
- Cuts: no floor

## FIR design — critical invariants

**`design_fir` normalization:** Only normalize FIR taps when `peak > 1.0`. Unconditionally dividing by peak when `peak < 1.0` amplifies attenuating filters and inverts the correction direction (cuts become boosts).

**`design_modal_fir` Gabor n_cycles:** Default is `n_cycles=1`. For `n_cycles ≥ 2`, the Gabor trailing half extends past `pre_samples` and gets hard-clipped, breaking the −π cancellation phase and amplifying modes by tens of dB instead of cancelling them. The floor formula `(0.5 + 0.5 * n_cycles) * T` ensures leading-edge safety; n_cycles=1 also ensures trailing-edge safety.

**`design_fir_multi` regularization_lambda:** Signal levels in this setup are typically −28 to −16 dBFS (linear 0.04–0.16). The default λ=0.1 exceeds the signal level and suppresses everything. Use λ=0.01 for this hardware.

**`design_fir_multi_modal` anti-pulse + Wiener:** Anti-pulses must be placed BEFORE the Wiener main impulse in the same FIR time-domain buffer (using `ModalAwareFIRDesigner` with the Wiener FIR as `base_correction`). Convolving anti-pulse and Wiener as separate FIRs fails — the Gabor's Fourier spectrum (+52 dB at mode frequency) is normalized by ModalAwareFIRDesigner's adjacent-band cap, making the combined FIR effectively mute the sub.

**`design_fir` prediction with pre-ringing (mixed phase):** The mixed-phase FIR adds pre-ringing (e.g., 40 ms). The loopback reference is pre-CamillaDSP; the measurement deconvolution sees the pre-ring as apparent early energy. Use `phase_mode='minimum'` for sub measurements where the loopback timing is inconsistent.

## Measurement reliability

**Baseline requires PEQ pre-conditioning:** The SVS port resonance (+15 dBFS at 20 Hz without cuts) saturates the measurement chain and collapses coherence at 63–80 Hz. Always apply HPF + cuts at 20–36 Hz (e.g., 22 Hz/−10 dB, 28 Hz/−8 dB) before taking a FIR baseline.

**Loopback timing consistency:** `loopback_xcorr_peak_ms` must be stable across compared sessions. When it varies significantly (e.g., 3.0 vs 4.979 ms), the measurements cannot be directly compared — the deconvolution reference has shifted.

**Time-windowed IR (`direct_path_window_ms`):** For verifying FIR effects above the Schroeder frequency (~150 Hz in this room), use `measure(direct_path_window_ms='100')`. This applies a 100 ms Hanning window around the IR peak, isolating the direct path. **Do NOT use for sub-bass calibration** (20–80 Hz) — room modes need the full 500 ms gate to establish; the short gate shows 10–15 dB lower than listening level.

**Minimum-phase FIR at <50 Hz:** FIR group delay (100–200 ms at 31–40 Hz) shifts room mode phases and can increase measured level even while attenuating. Verify FIR effects at sub-bass via tap frequency-response analysis, not 1/3-octave room comparison.

## Lessons system

Each calibration run carries three short prose fields and produces ≤2 lessons.

| When | Tool | What |
|---|---|---|
| Run start | `save_calibration_run(goal, hypothesis)` | concrete measurable target + why this run should achieve it |
| Phase start | `get_relevant_lessons(category, tags)` | pull prior lessons before designing filters |
| Run end | `update_calibration_run(outcome)` | prose comparing actual to hypothesis |
| Run end | `record_lesson(claim, scope, invalidators)` | one or two falsifiable claims |

- `scope='general'` = universal acoustics/tooling rule — must be promoted (fix codebase or write memory file), don't let accumulate in DB
- `scope='room'` = this room/hardware — provide `invalidators` so it stales automatically
- Cap at ~2 lessons per run with confidence ≥ 0.5

## Harman bass target (relative to 80 Hz reference)

| Hz | Target |
|----|--------|
| 25 | +5 dB  |
| 31 | +4 dB  |
| 40 | +3 dB  |
| 50 | +2 dB  |
| 63 | +1 dB  |
| 80 | 0 dB   |

## Skill routing

gstack skills are disabled for this repo. Do NOT auto-invoke ship, review, investigate, or other gstack skills. Handle all requests directly without routing to skills.
