---
name: avr:calibrate
version: 2.0.0
description: |
  Run a calibration recipe end-to-end. Lists available recipes, recommends one
  based on hardware, then reads and executes each step via MCP tools. Saves
  equipment state and run history for later review.
allowed-tools:
  - Bash
  - Read
  - Write
  - Edit
  - Glob
  - Grep
  - Agent
  - AskUserQuestion
---

# /avr:calibrate

Run a calibration recipe by reading it and executing each step via MCP tools.

## Arguments

- `$ARGUMENTS` — recipe name (default: none — show picker). Looks in `recipes/core/{name}.md`.

## Workflow

### Step 1 — Discover hardware and show recipes

```
1. Call `get_config` to read the user's hardware setup.

2. Show a three-layer system diagram. Build ENTIRELY from config — do not
   hardcode ANY labels, counts, models, or connection types.

   **Data sources:**
   - `config.connections` → physical wiring between devices (from/to/via arrays)
   - `config.minidsp.output_slots` → per-output label and type (sub/shaker/unused)
   - `config.eq_capabilities` → PEQ slot counts, FIR tap counts per output
   - `config.speakers` → speaker groups with positions, models, and specs
   - `config.mic.name` → mic model
   - `config.denon` → AVR info

   **Three layers, same heading style for each:**

   Each layer uses a `═══ LABEL ═══` banner, with boxes below it connected by
   labeled lines. Lines between layers show connection type from `config.connections`.

   **a. INTELLIGENCE layer** (top)
   Banner: `═══ INTELLIGENCE ═══`
   One box: "avr-calibration (Pi 5) / Claude orchestrator"
   Three lines descend from this box to the hardware layer, labeled with their
   connection types from `config.connections` (e.g. "HDMI, Network", "USB", "USB").

   **b. HARDWARE layer** (middle)
   Banner: `═══ HARDWARE ═══`
   One separate box per MCP-controlled device — read from `config.connections`:
   - **AVR box** — model from `config.denon`. Connections to other hardware devices
     (e.g. Analog arrow to miniDSP) shown as horizontal labeled arrows between boxes.
   - **DSP box** — "miniDSP 2x4 HD". Inside, list each output from
     `config.minidsp.output_slots` with label, type, and EQ capability from
     `config.eq_capabilities`. Mark shaker outputs "MUTED during cal", unused "(unused)".
   - **Mic box** — model from `config.mic.name`, plus cal file info.
   Lines descend from AVR and DSP boxes into the physical layer, labeled with
   connection types (speaker_wire, analog).

   **c. PHYSICAL layer** (bottom)
   Banner: `═══ PHYSICAL ═══`
   One room box containing ALL sound-producing transducers (these are NOT
   separate device boxes — they live in the room):
   - Speakers from `config.speakers` with positions, models, and specs (sensitivity, impedance)
   - Subs from `config.minidsp.output_slots` where type=sub, with labels
   - Shakers from `config.minidsp.output_slots` where type=shaker, with labels
   Group by which hardware device drives them and label the connection type.

3. List all .md files in recipes/core/ using Glob.
4. Read the first few lines of each recipe to get the Goal section.
5. Display them as a numbered list with a brief description.
6. Recommend based on the user's hardware:
   - bass-calibration is the primary recipe for sub/bass work
   - Note whether the recipe uses FIR or PEQ only (from the Filter Strategy section),
     so the user knows what hardware will be touched

7. If $ARGUMENTS names a valid recipe, pre-select it but still confirm.
8. Let the user pick or accept the recommendation.

9. After recipe selection, read the recipe's "## Measurement Signal Path" section
   and build the ACTIVE SIGNAL PATH diagram from config. The recipe tells you which
   config fields determine the path (e.g. `playback_route`, `speakers`, `output_slots`).
   Show only the boxes and connections that are active for this recipe's measurement mode.
```

### Step 2 — Pre-flight check

1. Call the `check_system` MCP tool (avr-calibration server)
2. If any component is unreachable, report the issue and STOP
3. Confirm with the user: "System is ready. Starting calibration with recipe: {name}. Proceed?"

### Step 3 — Record equipment state

Capture the starting equipment state for the run record:

1. Call `get_device_state` to capture AVR volume, input, DSP preset, routing state
2. Call `save_calibration_run` with the recipe name, target curve, and device_state
3. Save the returned `run_id` — use it to record iterations and final results

### Step 4 — Choose execution mode

Ask the user:

> Run in **safe mode** (confirm each DSP write) or **autonomous mode** (proceed without confirmation, SafetyValidator still enforces limits)?

- **Safe mode:** Before each signal-path write (`set_polarity`, `set_delay`, `set_output_gain`, `apply_eq`, `apply_input_eq`), describe the intended change and wait for the user to explicitly confirm before calling the tool.
- **Autonomous mode:** Call tools without asking for confirmation. SafetyValidator in code still enforces all safety limits.

### Step 5 — Execute the recipe

Read the recipe step by step and execute each instruction by calling the appropriate MCP tools.

**You are the orchestrator.** The recipe tells you WHAT to do. You decide HOW by calling MCP tools.

Key MCP tools available on the `avr-calibration` server:
- `measure` — take a sweep measurement, returns frequency response data
- `apply_eq` — write PEQ filters to miniDSP (SafetyValidator enforced). Pass `simulation_verified=true` when the filter set was just confirmed by `simulate_eq` (relaxes per-iteration limit to +6 dB).
- `apply_input_eq` — write shared input PEQ filters (same `simulation_verified` support)
- `mute_output` / `unmute_output` — mute/unmute specific DSP outputs
- `set_delay` / `set_polarity` / `set_output_gain` — per-output DSP settings
- `set_volume` / `set_master_gain` — AVR/DSP volume
- `check_system` — verify all hardware
- `get_config` / `set_config` — system configuration
- `get_output_state` — per-output gain_db, delay_ms, polarity_inverted
- `get_measurement_history` — raw FR data. **ALWAYS** pass `min_hz=20, max_hz=120, format="compact"` for sub bass work.
- `get_fr_summary` — 1/3-octave summary (coarse convergence checks only)
- `compute_deviation` — RMS deviation from target. Supports configurable `resolution` ("sixth_octave" default) and `convergence_threshold` (recipe-defined).
- `analyze_ir` / `analyze_phase` / `analyze_decay` / `compare_sub_phase` — analytics
- `simulate_eq` / `optimize_q` / `design_fir` — simulation and optimization
- `calibrate_level` / `configure_matrix` / `end_sweep_session` — setup tools
- `save_calibration_run` / `update_calibration_run` / `save_calibration_iteration` — run tracking

**FR data resolution rule:** Always use `get_measurement_history(min_hz=20, max_hz=120, format="compact")` when designing or verifying sub bass filters. The compact format gives 0.18Hz resolution, ~550 points per session, ~8KB. `get_fr_summary` returns only 11 bands — too coarse to resolve narrow peaks.

### Step 6 — Record each iteration

After each EQ iteration:
1. Call `save_calibration_iteration` with the run_id, iteration number, RMS before/after, and filters proposed/applied
2. Report what you measured, what you changed, and convergence status

### Step 7 — Convergence or max iterations

When the recipe's convergence criteria are met:
1. Call `update_calibration_run` with converged=true, final_rms, iterations_run
2. Report final state: RMS deviation, filters applied, alignment corrections

If max iterations reached without convergence:
1. Call `update_calibration_run` with converged=false, final_rms, iterations_run
2. Report final state and remaining deviations
3. Suggest next steps (room treatment, sub repositioning, different recipe)

### Step 8 — Cleanup

1. Call `end_sweep_session` to restore miniDSP source
2. **Unmute everything** — even if calibration failed

## Important rules

1. **SafetyValidator is in the code.** You do not need to enforce safety limits yourself — `apply_eq` will reject unsafe filters. If rejected, reduce the offending values and retry.
2. **Prefer cuts over boosts.** Cuts are always safe. Boosts are limited (frequency-dependent: +6 dB below 30 Hz, +8 dB above 30 Hz).
3. **Always include the 18Hz HPF** in every `apply_eq` call.
4. **Mute bass shakers** before starting calibration if the config has shaker outputs.
5. **Unmute everything** when done, even if calibration fails.
6. **Do not hardcode frequencies or gains.** Read them from the measurement data and recipe.
7. **Pass simulation_verified=true** to `apply_eq`/`apply_input_eq` when you have just confirmed the predicted response via `simulate_eq`. This allows +6 dB per iteration instead of +3 dB.
8. **Record every run.** Call `save_calibration_run` at start, `save_calibration_iteration` after each iteration, `update_calibration_run` at end. Pass `device_state` from `get_device_state` to capture equipment state.
9. **Describe every hardware action explicitly.** Before each DSP write, say in plain language what you are doing and why — which inputs/outputs are involved, what values are being set, and what problem it solves. Examples:
   - `configure_matrix`: "Routing input 1 (the Denon HDMI analog input) to outputs 1, 2, and 3. This ensures all subs receive signal — the 2x4 HD default matrix can split inputs across outputs unexpectedly."
   - `set_delay`: "Sub 2 (Nearfield, output 2) arrives 16.8ms earlier than Sub 1 at the mic. Adding 16.8ms delay to output 2 so both subs arrive simultaneously."
   - `set_polarity`: "Sub 2 IR peak sign is +1 while Sub 1 is -1. Inverting polarity on output 2 to match Sub 1's phase."
   - `set_output_gain`: "Sub 1 measured 3.4 dB quieter than Sub 2. Applying +3.4 dB gain to output 1 to level-match."
