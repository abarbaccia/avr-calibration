---
name: recipe
version: 1.0.0
description: |
  Interactive recipe builder. Asks about calibration goals, room issues,
  and writes a hardware-agnostic recipe .md file in recipes/.
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

# /recipe

Build a custom calibration recipe interactively.

## Arguments

- `$ARGUMENTS` — optional recipe name. If omitted, asks the user.

## Workflow

### Step 1 — Review existing recipes

```
List recipes in recipes/core/ and recipes/custom/.
Show the user what's available. Ask if they want to create a new one or modify an existing one.
```

### Step 2 — Ask about calibration scope

Ask first:

> What do you want to calibrate?
> 1. **Subs only** — calibrate subwoofers via miniDSP (EQ, alignment, target curve)
> 2. **Full room** — sub calibration + Audyssey guidance + post-Audyssey verification
> 3. **Verification only** — check integration after sub cal + Audyssey (no EQ changes)

If "verification only", point the user to `/verify` instead — that skill handles it directly.

If "full room", the recipe will include three stages:
1. Sub calibration (via miniDSP, same as subs-only recipes)
2. Audyssey guidance (manual steps the user performs on the Denon)
3. Post-Audyssey verification (measure combined system, recommend adjustments)

### Step 3 — Ask about sub calibration goals

Ask one topic at a time:

1. **Target curve**: Harman (default), flat, custom? If custom, ask for frequency/dB pairs.
2. **Frequency range**: Full bass (20-80Hz default)? Extended (20-120Hz)? Sub-only (20-50Hz)?
3. **Sub alignment**: Do they have multiple subs that need alignment first?
4. **Convergence**: How tight? RMS < 2dB (default) or tighter/looser?
5. **Max iterations**: How many EQ passes? (default: 5)
6. **Room issues**: Known room modes? Placement constraints?

### Step 4 — Ask about full-room integration (if scope is "full room")

If the user chose "full room" in Step 2, also ask:

1. **Room correction system**: What does the AVR use? (e.g. Audyssey, Dirac, YPAO, MCACC, manual, none)
2. **Crossover frequency**: What's set on the AVR? (default: 80Hz)
3. **Room correction curve**: Which curve/mode? (e.g. flat, reference/rolled-off, off for some channels)
4. **Dynamic volume/EQ**: Any dynamic processing enabled? (affects bass level at lower volumes)
5. **Listening level**: Typical volume? (affects dynamic processing interaction)
6. **Known integration issues**: Any problems noticed after room correction? (e.g. thin bass, boomy crossover region, level mismatch)

### Step 5 — Write the recipe

Write a markdown recipe file following this format:

```markdown
# Recipe: {Name}

## Goal
{1-2 sentences describing what this recipe does}

## Prerequisites
{What must be done before this recipe — e.g. sub calibration completed, Audyssey run}

## Pre-flight
{What to check before starting}

## Phase 1 — {First phase name}
### 1.1 {Step}
{Instructions in plain English, hardware-agnostic}

## Phase 2 — {Second phase name}
### Target curve
| Hz | Target |
|...|...|

### 2.1 {Step}
{Instructions}

## Convergence
{When to stop}

## When convergence fails
{What to suggest if it doesn't converge}
```

**For "full room" scope**, the recipe should chain three stages.
Remember: recipes are **hardware-agnostic** — use generic terms (AVR, room correction,
DSP) not brand names (Denon, Audyssey, miniDSP). The skill layer handles hardware specifics.

```markdown
# Recipe: {Name} — Full Room Calibration

## Stage 1 — Sub Calibration
{Sub alignment, per-sub EQ, target curve — same structure as sub-only recipes}

## Stage 2 — Room Correction Guidance
{Manual steps for running the AVR's room correction}
### Preparation
- Set all speakers to "Small" with crossover at {X}Hz on the AVR
- Run the AVR's automatic room correction with mic at multiple positions
### Post-Correction Settings
- Select room correction curve (flatter vs more rolled off)
- Enable/disable dynamic volume compensation — note interaction with sub calibration
- Verify sub distance and level trim haven't been changed dramatically by room correction

## Stage 3 — Integration Verification
{References recipes/core/full-room-verify.md procedure}
- Measure subs-only baseline
- Measure full system with room correction active
- Check crossover integration
- Recommend AVR setting adjustments
```

Save to `recipes/custom/{name}.md` (user recipes go in custom/, not core/).

### Step 6 — Validate

Read the recipe back and verify:
- References only valid MCP tool actions (measure, apply EQ, mute, delay, polarity)
- Contains no hardware-specific details (no model names, IPs, protocols)
- Has clear convergence criteria
- Has a "when convergence fails" section
- For full-room recipes: includes all three stages (sub cal, room correction guidance, verification)
- For verification stages: references the post-audyssey-verify recipe structure

## Important rules

1. **Hardware-agnostic.** Recipes must never mention specific hardware models, IPs, or protocols.
2. **Plain English.** Write for Claude to read and execute, not for a Python parser.
3. **Include convergence criteria.** Every recipe must define when it's "done."
4. **Prefer cuts over boosts.** Mention this in EQ sections.
5. **Always include the mandatory 18Hz HPF.** Mention this in EQ sections.
