---
name: subcrawl
version: 1.0.0
description: |
  Sub crawl placement optimization. Guides user through sub crawl procedure,
  measures at candidate positions, compares FR, and recommends optimal placement.
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

# /subcrawl

Guide the user through a sub crawl to find the optimal subwoofer placement.

## Arguments

- `$ARGUMENTS` — optional: sub index (default: crawl for all subs sequentially).

## Background

A sub crawl reverses the normal measurement setup: place the sub at the listening position
and the mic at candidate sub positions. Due to reciprocity, the frequency response measured
this way is equivalent to sub-at-candidate, mic-at-listening-position.

This is faster than moving a heavy subwoofer to each candidate position.

## Workflow

### Step 1 — Pre-flight

1. Call `check_system` to verify hardware is ready.
2. Ask the user how many candidate positions they want to test (recommend 4-6).
3. Explain the sub crawl procedure.

### Step 2 — Setup

Ask the user to:
1. Place the subwoofer at the **listening position** (where they normally sit).
2. Connect the mic on a stand at the **first candidate position**.
3. Confirm when ready.

### Step 3 — Measure each position

For each candidate position:
1. Ask the user to move the mic to position N and confirm.
2. Call `set_volume` to set a safe measurement level.
3. Call `measure` to take a sweep.
4. Record the measurement and report key metrics:
   - Bass extension (-3dB point)
   - Smoothness (standard deviation of FR in 20-80Hz band)
   - Room mode severity (peaks/dips > 6dB from mean)
5. Ask the user to move to the next position.

### Step 4 — Compare and recommend

After all positions are measured, compare:
- **Smoothest response** (lowest std dev in 20-80Hz)
- **Best extension** (lowest -3dB point)
- **Fewest severe modes** (peaks/dips > 6dB)

Rank positions and recommend the best one with reasoning.

If doing multiple subs:
- After placing sub 1 at its optimal position, repeat for sub 2.
- For sub 2, also check combined response with sub 1 at each candidate position.
- The goal is minimizing combined-response nulls, not just individual smoothness.

### Step 5 — Final verification

After the user places the sub(s) at the recommended position(s):
1. Take a final combined measurement.
2. Compare to the individual candidate measurements.
3. Report the improvement.

## Important rules

1. **Safe volume.** Use a moderate measurement level (-30 to -25 dB).
2. **Label measurements.** Use descriptive labels like "subcrawl-pos1", "subcrawl-pos2-combined".
3. **Be patient.** The user is physically moving equipment. Wait for confirmation at each step.
4. **Explain trade-offs.** A position might have better extension but worse smoothness.
5. **Don't over-promise.** Room acoustics are physics — some rooms have no great positions.
