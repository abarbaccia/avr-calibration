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
