---
name: calibrate
version: 1.0.0
description: |
  Run a calibration recipe end-to-end. Reads a human-readable recipe file,
  drives the calibration loop by calling MCP tools (measure, apply EQ, mute/unmute,
  set delay, etc.), and converges to the target curve. Default recipe: harman-bass-persub.
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

# /calibrate

Run a calibration recipe by reading it and executing each step via MCP tools.

## Arguments

- `$ARGUMENTS` — recipe name (default: none — show picker). Looks in `recipes/core/{name}.md`.

## Workflow

### Step 1 — List recipes and recommend based on hardware

```
1. Call `get_config` to read the user's hardware setup (number of subs, shakers, etc.)
2. List all .md files in recipes/core/ using Glob.
3. Read the first few lines of each recipe to get the Goal/Overview.
4. Display them as a numbered list with a brief description.
5. Recommend a recipe based on the hardware config:

   Recommendation logic:
   - Count sub outputs (type: "sub") in config.minidsp.output_slots
   - If 2+ subs → recommend "harman-bass-persub" (per-sub EQ gives best multi-sub results)
   - If 1 sub → recommend "harman-bass" (simpler, no alignment needed)
   - If user has alignment issues (subs at different distances) → mention "harman-bass-aligned"

   Show the recommendation with a brief reason, e.g.:
   "Recommended: **harman-bass-persub** — you have 2 subs, per-sub EQ will flatten each
    sub's room response independently before applying the shared Harman target."

6. If $ARGUMENTS names a valid recipe, pre-select it but still confirm.
7. Let the user pick or accept the recommendation.
```

### Step 2 — Pre-flight check

Before starting calibration, verify the system is ready:

1. Call the `check_system` MCP tool (avr-calibration server)
2. If any component is unreachable, report the issue and STOP
3. Confirm with the user: "System is ready. Starting calibration with recipe: {name}. Proceed?"

### Step 3 — Execute the recipe

Read the recipe step by step and execute each instruction by calling the appropriate MCP tools.

**You are the orchestrator.** The recipe tells you WHAT to do. You decide HOW by calling MCP tools.

Key MCP tools available on the `avr-calibration` server:
- `measure` — take a sweep measurement, returns frequency response data
- `apply_eq` — write PEQ filters to miniDSP (SafetyValidator enforced)
- `read_eq` — read current EQ state from miniDSP
- `mute_output` — mute specific DSP outputs for solo measurement
- `unmute_output` — unmute DSP outputs (always unmute when done)
- `set_delay` — set output delay in ms (for sub alignment)
- `set_polarity` — set output polarity (for sub alignment)
- `get_device_state` — current AVR + DSP state
- `set_volume` — set AVR volume for sweep playback
- `check_system` — verify all hardware is reachable
- `fetch_recipe` — load a recipe by name
- `get_config` / `set_config` — read/write config
- `get_output_state` — per-output gain_db, delay_ms, polarity_inverted (in-memory tracking for this session)
- `analyze_ir` — IR peak time, polarity sign, SPL from a stored session (key input for computing alignment corrections)
- `analyze_decay` — T60 decay analysis on the IR from a measurement; returns ringing modes with priority and suggested_q

### Step 4 — Report progress

After each measurement or adjustment:
- Report what you measured (key frequencies, RMS deviation)
- Report what you changed (filters applied, delays set)
- Report convergence status (how far from target)

Keep the user informed but be concise — they don't need to see raw data unless they ask.

### Step 5 — Convergence or max iterations

When the recipe's convergence criteria are met:
- Report final state: RMS deviation, filters applied, alignment corrections
- Congratulate the user

If max iterations reached without convergence:
- Report final state and remaining deviations
- Suggest next steps (room treatment, sub repositioning, different recipe)

## Important rules

1. **SafetyValidator is in the code.** You do not need to enforce safety limits yourself — `apply_eq` will reject unsafe filters. If rejected, reduce the offending values and retry.
2. **Prefer cuts over boosts.** Cuts are always safe. Boosts are limited.
3. **Always include the 18Hz HPF** in every `apply_eq` call.
4. **Mute bass shakers** before starting calibration if the config has shaker outputs.
5. **Unmute everything** when done, even if calibration fails.
6. **Do not hardcode frequencies or gains.** Read them from the measurement data and recipe.
