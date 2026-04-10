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
1. Call `get_config` to read the user's hardware setup.
2. List all .md files in recipes/core/ using Glob.
3. Read the first few lines of each recipe to get the Goal/Filter Strategy/Overview.
4. Display them as a numbered list with a brief description.
5. Before the recipe list, show a visual signal chain derived from get_config.
   Build the diagram dynamically from the config — do not hardcode labels or counts.

   Signal chain diagram format (populate from config):

   ```
   Denon X3800H ──HDMI LFE──▶ miniDSP 2x4 HD
                                │
                      ┌─────────┴─────────┐
                      ▼                   ▼
                 Input PEQ           (shared to all)
                 [N] slots
                      │
          ┌───────────┼───────────┐───────────┐
          ▼           ▼           ▼           ▼
      Output 0    Output 1    Output 2    Output 3
      [label]     [label]     [label]     [label]
      [type]      [type]      [type]      [type]
      [N] PEQ     [N] PEQ     [N] PEQ     [N] PEQ
      [N] FIR     [N] FIR     [N] FIR     [N] FIR
          │           │           │           │
          ▼           ▼           ▼           ▼
       [driver]    [driver]    [driver]    [driver]
                                          
                      ◀─── UMIK mic ◀─── room
   ```

   Read labels, types, PEQ slot counts from:
   - config.minidsp.output_slots → per-output label and type (sub/shaker/unused)
   - config.eq_capabilities.output_peq → PEQ slots per sub output
   - config.eq_capabilities.input_peq → shared input PEQ slots
   - config.eq_capabilities.fir_capable, fir_max_taps_per_output,
     fir_shared_tap_pool, fir_sample_rate_hz → FIR capability

   Mark shaker outputs as "MUTED during cal". Mark unused outputs as dimmed/skipped.
   For sub outputs, show the PEQ slot count and FIR tap count.
   Show the input PEQ slot count on the shared input stage.

6. Recommend a recipe based on the hardware config:

   Recommendation logic:
   - Count sub outputs (type: "sub") in config.minidsp.output_slots
   - If 2+ subs → recommend "harman-bass-persub" (time alignment + per-sub EQ + shared Harman target)
   - If 1 sub → recommend "harman-bass" (simpler, no alignment needed)
   - If user has alignment issues (subs at different distances) → mention "harman-bass-aligned"

   Show the recommendation with a brief reason, e.g.:
   "Recommended: **harman-bass-persub** — you have 2 subs; aligns them in time, flattens each
    sub's room response independently, then applies the shared Harman target."

   Also note whether the selected recipe uses FIR or PEQ only (from the recipe's
   ## Filter Strategy section), so the user knows what hardware will be touched.

7. If $ARGUMENTS names a valid recipe, pre-select it but still confirm.
8. Let the user pick or accept the recommendation.
```

### Step 2 — Pre-flight check

Before starting calibration, verify the system is ready:

1. Call the `check_system` MCP tool (avr-calibration server)
2. If any component is unreachable, report the issue and STOP
3. Confirm with the user: "System is ready. Starting calibration with recipe: {name}. Proceed?"

### Step 3 — Choose execution mode

Ask the user:

> Run in **safe mode** (confirm each DSP write) or **autonomous mode** (proceed without confirmation, SafetyValidator still enforces limits)?

- **Safe mode:** Before each signal-path write (`set_polarity`, `set_delay`, `set_output_gain`, `apply_eq`, `apply_input_eq`), describe the intended change and wait for the user to explicitly confirm before calling the tool.
- **Autonomous mode:** Call tools without asking for confirmation. SafetyValidator in code still enforces all safety limits.

### Step 4 — Execute the recipe

Read the recipe step by step and execute each instruction by calling the appropriate MCP tools.

**You are the orchestrator.** The recipe tells you WHAT to do. You decide HOW by calling MCP tools.

Key MCP tools available on the `avr-calibration` server:
- `measure` — take a sweep measurement, returns frequency response data
- `apply_eq` — write PEQ filters to miniDSP (SafetyValidator enforced)
- `apply_input_eq` — write shared input PEQ filters (Harman target, affects all outputs)
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
- `get_measurement_history` — raw FR data (983 pts, ~0.18Hz spacing): **use this for filter design and verification**
- `get_fr_summary` — 11-band 1/3-octave summary: use only for quick coarse convergence checks
- `analyze_ir` — IR peak time, polarity sign, SPL from a stored session (key input for computing alignment corrections)
- `analyze_decay` — T60 decay analysis on the IR from a measurement; returns ringing modes with priority and suggested_q

**FR data resolution rule:** Always use `get_measurement_history` when designing or verifying filters. `get_fr_summary` returns only 11 1/3-octave bands (~2.8Hz–17Hz wide each) — too coarse to resolve narrow peaks (Q > 2) or verify filter notch depth. `get_measurement_history` gives ~0.18Hz spacing across the full range, which is what you need to place center frequencies accurately and confirm attenuation at the notch.

### Step 5 — Report progress

After each measurement or adjustment:
- Report what you measured (key frequencies, RMS deviation)
- Report what you changed (filters applied, delays set)
- Report convergence status (how far from target)

Keep the user informed but be concise — they don't need to see raw data unless they ask.

### Step 6 — Convergence or max iterations

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
7. **Describe every hardware action explicitly.** Before each DSP write, say in plain language what you are doing and why — which inputs/outputs are involved, what values are being set, and what problem it solves. Do not just name the tool call. Examples:
   - `configure_matrix`: "Routing input 1 (the Denon HDMI analog input) to outputs 1, 2, and 3. This ensures all subs receive signal — the 2x4 HD default matrix can split inputs across outputs unexpectedly."
   - `set_delay`: "Sub 2 (Nearfield, output 2) arrives 16.8ms earlier than Sub 1 at the mic. Adding 16.8ms delay to output 2 so both subs arrive simultaneously."
   - `set_polarity`: "Sub 2 IR peak sign is +1 while Sub 1 is −1. Inverting polarity on output 2 to match Sub 1's phase."
   - `set_output_gain`: "Sub 1 measured 3.4 dB quieter than Sub 2. Applying +3.4 dB gain to output 1 to level-match."
