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

### Step 2 — Ask about calibration goals

Ask one topic at a time:

1. **Target curve**: Harman (default), flat, custom? If custom, ask for frequency/dB pairs.
2. **Frequency range**: Full bass (20-80Hz default)? Extended (20-120Hz)? Sub-only (20-50Hz)?
3. **Sub alignment**: Do they have multiple subs that need alignment first?
4. **Convergence**: How tight? RMS < 2dB (default) or tighter/looser?
5. **Max iterations**: How many EQ passes? (default: 5)
6. **Room issues**: Known room modes? Placement constraints?

### Step 3 — Write the recipe

Write a markdown recipe file following this format:

```markdown
# Recipe: {Name}

## Goal
{1-2 sentences describing what this recipe does}

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

Save to `recipes/custom/{name}.md` (user recipes go in custom/, not core/).

### Step 4 — Validate

Read the recipe back and verify:
- References only valid MCP tool actions (measure, apply EQ, mute, delay, polarity)
- Contains no hardware-specific details (no model names, IPs, protocols)
- Has clear convergence criteria
- Has a "when convergence fails" section

## Important rules

1. **Hardware-agnostic.** Recipes must never mention specific hardware models, IPs, or protocols.
2. **Plain English.** Write for Claude to read and execute, not for a Python parser.
3. **Include convergence criteria.** Every recipe must define when it's "done."
4. **Prefer cuts over boosts.** Mention this in EQ sections.
5. **Always include the mandatory 18Hz HPF.** Mention this in EQ sections.
