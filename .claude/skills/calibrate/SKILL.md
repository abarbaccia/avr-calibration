---
name: calibrate
version: 1.0.0
description: |
  Run a calibration recipe end-to-end. Reads a human-readable recipe file,
  drives the calibration loop by calling MCP tools (measure, apply EQ, mute/unmute,
  set delay, etc.), and converges to the target curve. Default recipe: harman-bass-aligned.
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

- `$ARGUMENTS` — recipe name (default: `harman-bass-aligned`). Looks in `recipes/core/{name}.md`.

## Workflow

### Step 1 — Load the recipe

```
Read the recipe file from recipes/core/{recipe_name}.md
If not found, list available recipes and ask the user to pick one.
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
- `trigger_measurement` — take a sweep measurement, returns frequency response data
- `apply_eq` — write PEQ filters to miniDSP (SafetyValidator enforced)
- `read_eq` — read current EQ state from miniDSP
- `mute_sub_outputs` — mute/unmute specific outputs for solo measurement
- `get_device_state` — current miniDSP state (gains, delays, mutes)
- `avr_set_volume` — set Denon volume for sweep playback
- `check_system` — verify all hardware is reachable

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
