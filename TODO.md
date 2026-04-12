# TODO — Architecture Migration: Claude-Driven Calibration

## Core Principle

**Claude Code is the orchestrator, not Python.** Recipes are human-readable English
instructions that Claude reads and executes by calling MCP tools. Python provides
primitives (measure, apply EQ, mute/unmute, set delay) — never orchestration logic.

Do NOT build Python orchestrators, phase runners, or loop state machines.
Claude drives the loop, reads the recipe, decides what to do next.

---

## MCP Server Architecture — Plugin Facade

One MCP server exposed to Claude with high-level tools. Hardware drivers are
plugins behind the facade — Claude never sees Denon vs miniDSP details.

### Principle
Claude calls `measure`, not "set Denon input, set volume, play HDMI sweep on
channel 3, record via UMIK, restore Denon." The plugin layer handles hardware
protocol, ordering, and cleanup. Recipes stay hardware-agnostic.

### MCP tools Claude sees

| Tool | What Claude thinks | Plugin layer handles | Status |
|------|-------------------|---------------------|--------|
| `measure` | "take a measurement" | Denon context, HDMI routing, UMIK, PyTTa | Done |
| `apply_eq` | "write these filters" | Safety, biquad math, miniDSP HTTP | Done |
| `mute_output` | "silence these outputs" | miniDSP gain → -127 dB | Done |
| `unmute_output` | "restore these outputs" | miniDSP gain → 0 dB | Done |
| `set_delay` | "delay this output" | miniDSP delay API | Done |
| `set_polarity` | "flip this output" | miniDSP polarity API | Done |
| `set_volume` | "set AVR volume" | Denon volume control | Done |
| `get_device_state` | "what's the system state" | Queries all hardware, combines | Done |
| `check_system` | "is everything working" | PreflightChecker.run_all() | Done |
| `read_eq` | "current EQ state" | In-memory EQ tracking | Done |
| `fetch_recipe` | "load a recipe" | Filesystem | Done |
| `get_config` / `set_config` | "read/write config" | Filesystem | Done |
| `get_measurement_history` | "past measurements" | SQLite | Done |
| `get_calibration_runs` | "past calibrations" | SQLite | Done |
| `discover_avr` | "find my AVR" | SSDP scan | Done |

### Plugin drivers (Python, behind the facade)
- `drivers/denon.py` — AVR control (volume, input, sweep context)
- `drivers/minidsp.py` — DSP control (EQ, mute, delay, polarity)
- `drivers/playback.py` — HDMI/USB sweep playback
- `measurement.py` — UMIK recording + PyTTa deconvolution
- `safety.py` — SafetyValidator (always in the code path, never prompt-only)

### Cleanup tasks
- [ ] Simplify `mcp_server.py` to thin facade — each tool handler is ~5 lines delegating to a plugin
- [x] Remove `set_denon_volume` alias (renamed to `set_volume`, legacy aliases kept in dispatch)
- [x] Add `set_delay(output_index, delay_ms)` MCP tool
- [x] Add `set_polarity(output_index, inverted)` MCP tool
- [x] Add `check_system` MCP tool (preflight checks as a single tool)
- [x] Rename `trigger_measurement` → `measure` (legacy alias kept in dispatch)
- [x] Rename `mute_sub_outputs` → `mute_output` / `unmute_output` (legacy alias kept in dispatch)

---

## Skills to Build

### `/setup` — Equipment Configuration (SKILL CREATED)
- [x] SKILL.md created at `.claude/skills/setup/SKILL.md`
- [x] Ask user about hardware: AVR model, DSP, sub count/models, mic, bass shakers
- [x] Generate `config.yaml` with output_slots (sub/shaker/unused types), IP addresses, mic name
- [x] Configure MCP server connection (`.claude/mcp.json`)
- [x] Verify connectivity via `check_system` MCP tool

### `/recipe` — Interactive Recipe Builder (SKILL CREATED)
- [x] SKILL.md created at `.claude/skills/recipe/SKILL.md`
- [x] Ask user about calibration goals (target curve, frequency range, sub alignment needs)
- [x] Ask about room issues (known modes, placement constraints)
- [x] Write a recipe `.md` file in `recipes/` following the established format
- [x] Validate recipe references valid MCP tools and safe parameters

### `/calibrate` — Run Calibration (PRIORITY)
- [x] Default recipe: align subs → Harman curve fit 20-80Hz
- [ ] Skill reads recipe file, drives measure→analyze→adjust loop via MCP tools
- [ ] Handles sub alignment (measure each solo, compute delays/polarity/levels, verify)
- [ ] Handles EQ loop (measure combined, compare to target, propose filters, apply, re-measure)
- [ ] Reports progress conversationally
- [ ] Stops on convergence or max iterations

### `/subcrawl` — Sub Placement Optimization (SKILL CREATED)
- [x] SKILL.md created at `.claude/skills/subcrawl/SKILL.md`
- [x] Guide user through sub crawl procedure (sub at listening position, mic at candidate positions)
- [x] Measure at each candidate position
- [x] Compare FR smoothness, modal distribution, bass extension at each position
- [x] Recommend optimal placement with reasoning
- [x] Support multiple subs (crawl each independently, then measure combined)

### `/measure` — Single Measurement + Analysis (SKILL CREATED)
- [x] SKILL.md created at `.claude/skills/measure/SKILL.md`
- [x] Take one measurement via MCP
- [x] Analyze FR, compare to target curve if specified
- [x] Report: RMS deviation, room modes, rolloff points, problem frequencies

### `/check` — Pre-flight System Check (SKILL CREATED)
- [x] SKILL.md created at `.claude/skills/check/SKILL.md`
- [x] Wraps `check_system` MCP tool
- [x] Report status summary with troubleshooting guidance

### `/status` — Current State Review (SKILL CREATED)
- [x] SKILL.md created at `.claude/skills/status/SKILL.md`
- [x] Current EQ filters on miniDSP
- [x] Last measurement and distance from target
- [x] Calibration history summary

### `/investigate` — Debug Issues
- [ ] Already exists in gstack skills
- [ ] Route hardware/calibration issues here

---

## Recipe Format Migration

### Current (structured YAML — DEPRECATED for orchestration)
```yaml
phases:
  - type: align-subs
  - type: eq-loop
    target: harman
    band: [20, 80]
```

### New (human-readable English — Claude reads and executes)
```markdown
# Recipe: Harman Bass Aligned

## Goal
Calibrate two ported subwoofers to the Harman bass target curve (20-80Hz).

## Steps
1. Align subs individually — measure each solo, apply delay/polarity/level corrections, verify
2. EQ the combined response to the Harman target curve
3. Converge to <2dB RMS deviation, max 5 iterations

## Alignment details
Measure each sub with all others muted. Compute travel-time delays from IR peaks.
Correct polarity if needed. Level-match to loudest sub. Re-measure to verify
alignment within 0.5ms delay spread and 1dB level spread.

## Convergence
RMS deviation from Harman target < 2 dB across 20-80Hz.
Max 5 EQ iterations. Prefer cuts over boosts.
```

### Migration Tasks
- [x] Write default recipe: `recipes/core/harman-bass-aligned.md`
- [ ] Keep `recipes/core/harman-bass.md` (EQ only, no alignment)
- [x] YAML recipes can stay for backwards compat but are not the primary format
- [x] PhaseRunner (`calibrate/phase_runner.py`) is removed
- [x] LoopOrchestrator (`calibrate/loop.py`) is removed

---

## System Characterization & Amp Headroom Test

### The problem this solves

"Do I need a better amp?" is one of the most common questions in home theater — and
almost everyone answers it wrong. They upgrade on spec sheets, vibes, or forum posts.

This tool gives objective data: at what volume does your amp actually start to fail,
where does it fail first (which channel, which driver section), and is the failure mode
per-channel clipping or shared power supply sag? That determines whether the fix is a
better amp, more efficient speakers, or a crossover adjustment.

Someone with 95dB/W/m speakers sees a completely different headroom curve than someone
with 87dB/W/m speakers at the same Denon volume. This tool shows that, automatically,
for your specific hardware.

### Architecture

New MCP tool: `analyze_system` (orchestrates all phases)
New skill: `/headroom` (drives the test, reports results)

Four phases:

**Phase 1: Speaker characterization**
- Solo sweep per speaker (FR measurement)
- Extract: flat passband start/end, efficiency (dBSPL @ reference level), -3dB rolloff
- Identifies what frequency range each speaker can actually reproduce

**Phase 2: Multitone cluster assignment**
- Auto-assign 3-5 test tones per speaker, spanning its flat passband
- Tone spacing: woofer region (low passband), crossover region (impedance dip = max amp load), tweeter region (high passband)
- Between-speaker minimum spacing: 30Hz (prevents UMIK from mixing channels in FFT)
- Avoid below 200Hz (room modes corrupt readings), stay within measured flat passband only
- Result: a unique frequency "fingerprint" per speaker — FFT isolates each channel in the mix

**Phase 3: Simultaneous multi-channel load test**
- Play all channels at once with assigned multitone clusters (HDMI multichannel from Pi 5)
- Step Denon master volume 1dB at a time, hold 2 seconds per step
- UMIK records the room mix at each step
- FFT extracts per-speaker SPL and THD for each cluster at each volume step

**Phase 4: Analysis and recommendation**
- Track SPL gain per dB of volume increase — should be 1:1
- Detect **power supply sag**: all channels compress simultaneously = shared supply failing
  (signature: THD rises in all channels at once, SPL gain falls below 1dB/step everywhere)
- Detect **per-channel clipping**: single channel THD spikes while others stay clean
  (signature: one speaker's cluster distorts, others stay linear)
- Report:
  - Headroom at reference listening level (dB before compression onset)
  - Weakest channel (first to show distortion)
  - Failure mode: sag vs clipping
  - Recommended action: upgrade amp / reduce crossover / raise speaker sensitivity / add external amp

### Why multitone clusters

Single-frequency tones give a misleading amp load picture. Speaker impedance varies
with frequency: peaks at resonance (easy on the amp), dips at the crossover region
(maximum amp stress). A single tone at an impedance peak underestimates load by 2-4×.

Multitone clusters drive both the woofer and tweeter simultaneously — the same load
profile as real music/film content. THD products from multitone are also easily
distinguished from room reflections in the FFT, unlike noise floor in swept tests.

### Power supply sag vs per-channel clipping

These have different signatures and different fixes:

- **Sag** (shared supply): at threshold volume, *all* channels compress together. SPL
  gain drops to 0 across all speakers simultaneously. Fix: replace receiver with separates,
  or use external amps for most demanding channels.
- **Clipping** (per-channel): one channel THD spikes while others stay clean. Fix:
  more efficient speakers on that channel, or external amp for just that channel.

Sag is the failure mode most Denon X-series owners hit at loud movie levels — the
internal power supply isn't large enough to sustain all channels at high current simultaneously.
This test quantifies exactly where that happens for your specific setup.

### Implementation

**New MCP tools needed**
- [ ] `analyze_system` — orchestrate characterization + load test, return JSON report
- [ ] `play_multitone(channel_assignments)` — play simultaneous multitone clusters
  via HDMI multichannel (Pi 5 ALSA multichannel output, discrete per-channel tone sets)
- [ ] `measure_fft(duration_sec)` — record + FFT, return per-frequency SPL array
  (replaces swept IR for this use case — steady-state FFT, not impulse response)

**Driver-level hooks (no new MCP tools needed)**
- `DenonDriver.set_master_gain(db)` — already exists (wraps `set_volume`)
- `MinidspDriver` — no changes, EQ stays as-is during test
- Future: `CamillaDSPDriver.set_channel_volume(channel, db)` via WebSocket API
  (for per-channel volume stepping once CamillaDSP is installed on Pi 5)

**Multichannel HDMI requirement**
Pi 5 supports multichannel HDMI output via ALSA (`plughw:0,3` or similar device).
Test tones must be rendered to discrete HDMI channels so Denon routes each to the
correct speaker. Mono sum playback cannot isolate per-channel load.

**Implementation order**
1. Denon + miniDSP path (current hardware) — sweep per speaker, HDMI multichannel playback
2. FFT-based measure_fft tool (UMIK steady-state recording + numpy FFT)
3. Volume stepping + compression detection (Denon only for now)
4. CamillaDSP integration when installed — adds per-channel level control

**Expected output**
```json
{
  "reference_level_db": -20,
  "headroom_db": 8.5,
  "compression_onset_volume": -11.5,
  "failure_mode": "power_supply_sag",
  "weak_channel": null,
  "per_channel": {
    "FL": {"headroom_db": 8.5, "efficiency_dbspl": 87.2, "flat_band": [80, 18000]},
    "FR": {"headroom_db": 8.5, "efficiency_dbspl": 87.1, "flat_band": [80, 18000]},
    "C":  {"headroom_db": 8.5, "efficiency_dbspl": 86.8, "flat_band": [100, 18000]}
  },
  "recommendation": "Power supply sag detected at -11.5dB volume. All channels compress simultaneously. External amplification for FL/FR/C recommended."
}
```

### Tasks
- [ ] `measure_fft` MCP tool — UMIK steady-state recording + numpy FFT, return per-Hz SPL
- [ ] `play_multitone` driver — ALSA multichannel tone synthesis, one cluster per HDMI channel
- [ ] Phase 1: `analyze_system` characterization pass — solo sweep per speaker, extract passband
- [ ] Phase 2: tone assignment algorithm — auto-assign clusters within measured flat bands
- [ ] Phase 3: volume stepping loop — 1dB steps, record FFT, track per-cluster SPL/THD
- [ ] Phase 4: compression detection — sag vs clipping classifier, headroom computation
- [ ] `/headroom` skill — drives the full test, reports results conversationally
- [ ] Tests for FFT extraction, tone assignment, and compression detection logic

---

## Full-Room Integration & Audyssey Enhancement

### Phase 1 — Post-Audyssey Verification (DONE)

Measure combined system after sub calibration + Audyssey, check crossover integration,
recommend Denon setting adjustments.

- [x] Recipe: `recipes/core/full-room-verify.md`
- [x] Skill: `.claude/skills/verify/SKILL.md` — `/verify` drives the verification recipe
- [x] Enhanced `/recipe` skill to support full-room calibration scope (sub cal + Audyssey + verification)
- [ ] `measure` full_range mode — preserve Denon sound mode (no Pure Direct), full-range sweep (20Hz–20kHz)
- [ ] DenonSweepContext option to skip Pure Direct and preserve Audyssey processing
- [ ] Config option for full-range sweep frequency limits (measurement.full_range_freq_min/max)

### Phase 2 — Guided Corrections via Denon API (TODO)

After verification identifies issues, automate the Denon-side adjustments instead of
telling the user to navigate menus manually.

**Why:** The Denon exposes channel levels, crossover, and sub trim via the denonavr API.
We already control volume and input — extending to these settings closes the loop between
verification findings and corrective action.

**Tasks:**
- [ ] DenonDriver: `set_channel_level(channel, trim_db)` — per-channel level trim (FL, FR, C, SW, etc.)
- [ ] DenonDriver: `set_crossover(channel, freq_hz)` — per-channel crossover frequency
- [ ] DenonDriver: `set_sub_trim(trim_db)` — subwoofer level trim on AVR
- [ ] DenonDriver: `get_speaker_config()` — read current speaker sizes, distances, levels, crossovers
- [ ] DenonDriver: `set_speaker_distance(channel, distance)` — per-channel distance/delay
- [ ] DenonDriver: `set_audyssey_mode(mode)` — switch Audyssey curve (Reference/Flat/Off)
- [ ] DenonDriver: `set_dynamic_eq(enabled)` — toggle Dynamic EQ
- [ ] MCP tool: `adjust_denon(setting, value)` — high-level Denon adjustment tool
- [ ] Updated `/verify` skill: after reporting, offer to apply recommended Denon adjustments automatically
- [ ] Safety: max ±6dB on any channel trim, confirm before applying

### Phase 3 — Custom Target Curves via MultEQ Protocol (TODO)

Push custom target curves to Audyssey via the reverse-engineered TCP protocol (port 1256).
This is the big unlock — it lets our system control what Audyssey optimizes toward,
eliminating the fight between miniDSP sub calibration and Audyssey's own bass management.

**Why:** Currently Audyssey and miniDSP are calibrated independently. Audyssey may
undo our sub work in the crossover region, or apply its own bass curve that conflicts
with our Harman target. Custom target curves let us tell Audyssey exactly what to do
in the crossover region so both systems cooperate.

**References:** See `reference_audyssey_tcp_protocol.md` in project memory.
ratbuddyssey (C# open source) proved the read/write path works. OCA (Python) demonstrated
full calibration automation.

**Tasks:**
- [ ] Implement MultEQ TCP client (Python, based on ratbuddyssey protocol documentation)
- [ ] Read current Audyssey calibration state (speaker config, distances, levels, target curves)
- [ ] Write custom target curves per channel (the key capability)
- [ ] MCP tool: `get_audyssey_state()` — read current MultEQ calibration
- [ ] MCP tool: `set_audyssey_target(channel, curve_points)` — push custom target curve
- [ ] Recipe: `recipes/core/audyssey-custom-target.md` — calibrate subs, then push a custom
  Audyssey target that complements the sub calibration in the crossover region
- [ ] Skill: `/audyssey` — drives the custom target workflow
- [ ] Safety: never modify Audyssey calibration without explicit user confirmation
  (Audyssey re-runs are time-consuming; changes must be reversible)
- [ ] Store original Audyssey state before modifications for rollback

### Phase 4 — Full Claude-Driven Room Correction (TODO, aspirational)

Replace Audyssey entirely with Claude-driven room correction for users who want
full control. This is the long-term vision — Claude measures every speaker, designs
per-channel correction, and applies it.

**Why:** Audyssey is a black box. Users with specific preferences (e.g. no treble
rolloff, custom house curves, per-seat optimization) can't control what it does.
Claude-driven correction lets them specify exactly what they want.

**Prerequisites:**
- Per-channel measurement capability (TODO-R1: Denon test tones or Pi 5 multichannel HDMI)
- Either Denon manual PEQ slots (limited, ~9 bands) or external full-range DSP
  (e.g. miniDSP SHD, CamillaDSP on Pi 5)

**Tasks:**
- [ ] Per-channel measurement via Denon test tones or Pi 5 multichannel HDMI (TODO-R1)
- [ ] Full-range target curves (Harman preference for all channels, not just bass)
- [ ] Per-channel PEQ design (same analysis engine as sub calibration, wider frequency range)
- [ ] FIR filter design for linear-phase room correction (if DSP supports it)
- [ ] Multi-position optimization (measure at multiple seats, optimize for zone)
- [ ] Recipe: `recipes/core/full-room-correction.md`
- [ ] Skill: `/room-correct` — full room correction workflow
- [ ] CamillaDSP driver (if Pi 5 becomes the DSP for mains as well as subs)

---

## Python Code: What to Keep vs Remove

### Keep (MCP primitives)
- `calibrate/mcp_server.py` — MCP tool definitions (measure, apply_eq, mute, set_delay, etc.)
- `calibrate/alignment.py` — IR extraction, delay computation, polarity detection (library functions)
- `calibrate/measurement.py` — MeasurementEngine (PyTTa sweep + deconvolution)
- `calibrate/drivers/` — Denon, miniDSP, playback drivers
- `calibrate/safety.py` — SafetyValidator (NEVER bypass, NEVER move to prompt-only)
- `calibrate/config.py` — Hardware config with output_slots types
- `calibrate/storage.py` — SessionStore (SQLite history)
- `calibrate/adapters/` — HTTP clients for miniDSP, Denon

### Deprecate / Remove (Python orchestration replaced by Claude)
- `calibrate/phase_runner.py` — Claude is the phase runner now
- `calibrate/loop.py` — LoopOrchestrator replaced by Claude driving MCP tools in a loop
- `recipes/*.yaml` — replaced by `.md` recipes (keep for backwards compat temporarily)

### New MCP Tools Needed
- [ ] `measure_sub_solo(sub_index)` — mute others, measure, unmute (atomic operation)
- [ ] `get_alignment_state()` — current delay/polarity/level for each sub
- [x] Enrich measurement sessions with full IR-derived metadata at capture time (peak time, polarity, SPL, phase, T60, group delay) to avoid redundant sweeps
- [x] `set_delay(output_index, delay_ms)` — set delay for one output (was apply_sub_delay)
- [x] `set_polarity(output_index, inverted)` — set polarity for one output (was apply_sub_polarity)
- [x] `measure` — trigger a sweep and return session ID (was run_sweep/trigger_measurement)

---

## CLAUDE.md Updates Needed
- [x] Architecture section updated to reflect Claude-as-orchestrator
- [x] Add calibration knowledge section (signal chain, how to interpret FR, loop pattern)
- [x] Add MCP tool reference
- [x] Add safety rules (non-negotiable, code-enforced)
- [x] Add skill routing for calibration skills
