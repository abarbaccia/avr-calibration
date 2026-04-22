---
name: avr:recipe
version: 2.0.0
description: |
  Interactive recipe builder. Guides the user through creating a calibration
  recipe, validates it against the template, and saves to recipes/custom/.
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

# /avr:recipe

Build or validate a custom calibration recipe.

## Arguments

- `$ARGUMENTS` — recipe name to create, or "validate {path}" to validate an existing recipe.

## Step 1 — Read the templates

Read `recipes/TEMPLATE.md` (universal rules) and `recipes/CALIBRATION.md` (calibration-specific
patterns) in full. These are the source of truth for recipe structure, hard rules, and available
tools. Every decision in this skill flows from these templates.

## Step 2 — Review existing recipes

```
List recipes in recipes/core/ and recipes/custom/.
Read the first few lines (Goal + Filter Strategy) of each.
Show the user what exists. Ask if they want to:
  a) Create a new recipe
  b) Validate an existing custom recipe
  c) Fork a core recipe as a starting point
```

If validating, skip to the Validation step.

## Step 3 — Understand the user's goal

Ask one question at a time. Don't overwhelm with a questionnaire.

**Question 1: What are you trying to achieve?**
> Examples: "flatten my sub response", "match Harman curve with FIR",
> "calibrate for music (less bass lift)", "align 3 subs then EQ"

**Question 2: Hardware setup**
> - How many subs?
> - PEQ only, FIR only, or both?
> - Any constraints? (e.g. limited PEQ slots, no FIR capability)

**Question 3: What's different from existing recipes?**
> Show which existing recipe is closest and ask what they want to change.
> If the answer is "nothing" — point them to `/avr:calibrate` instead.

## Step 4 — Choose a starting point

Based on the answers, either:
- **Fork an existing recipe**: copy `recipes/core/{closest}.md` to
  `recipes/custom/{name}.md` and modify
- **Start from scratch**: use the template structure from `recipes/TEMPLATE.md`
  plus `recipes/CALIBRATION.md` if it's a calibration recipe

If forking, tell the user which recipe you're starting from and why.

## Step 5 — Build the recipe section by section

Walk through each required section from the template. For each section:

1. **Explain what it needs** (from the template's requirements)
2. **Propose content** based on the user's goals
3. **Ask for confirmation** before moving on

Sections to walk through:
1. Goal
2. Configuration (target curve, frequency range, convergence thresholds)
3. Filter Strategy (which layers, which tools, which NOT used)
4. Pre-flight (always: check_system, get_config, mute shakers)
5. Phase 0 — Setup (clear EQ, volume, matrix, level matching)
6. Calibration phases (the core logic — this is where recipes differ)
7. Convergence criteria (frequency range, RMS threshold, max iterations)
8. When convergence fails
9. Retrospective (always required — scorecard, recommendations, next steps)
10. MCP tools list

For the **calibration phases** (step 6), enforce these hard rules from CALIBRATION.md:
- Analyze fixability before designing corrections
- Full-resolution FR for filter design (not 1/3-octave)
- Simulate before applying
- Use optimize_q, not guessed Q values
- Include mandatory 18Hz HPF in every apply_eq call
- Filter audit on every iteration (remove stale filters)
- Iterative merge pattern for subsequent iterations
- Anchor with null exclusion if using a target curve
- Clean baselines after structural changes (FIR)
- Prefer cuts over boosts

## Step 6 — Write the recipe

Save to `recipes/custom/{name}.md`.

## Step 7 — Validate

Run the full validation checklist (same as validate mode). Report results
as a pass/fail checklist. Fix any failures before finishing.

---

## Validation Mode

When invoked with "validate {path}" or after writing a new recipe, run every
check below. Report as a checklist with pass/fail for each item.

### Structure checks
- [ ] Has `## Goal` section
- [ ] Has `## Configuration` section (target curve, frequency range, convergence)
- [ ] Has `## Filter Strategy` section with layer table
- [ ] Has `## Pre-flight` section
- [ ] Pre-flight calls `check_system`
- [ ] Pre-flight calls `get_config`
- [ ] Has setup phase that clears EQ before baseline
- [ ] Has setup phase with volume setup (`set_volume` and/or `calibrate_level`)
- [ ] Has `## Convergence` section with measurable criteria
- [ ] Convergence uses `compute_deviation` (not manual RMS)
- [ ] Has max iteration count
- [ ] Has `## When convergence fails` section
- [ ] Has retrospective phase
- [ ] Retrospective includes before/after scorecard
- [ ] Retrospective includes room improvement recommendations
- [ ] Retrospective includes prioritized next steps
- [ ] Has `## MCP tools used` section

### Analytics-first workflow checks
- [ ] Calls `analyze_phase` before designing corrections
- [ ] Checks coherence before designing corrections
- [ ] Uses full-resolution FR for filter design (not 1/3-octave)
- [ ] Calls `simulate_eq` before `apply_eq` (verify before apply)
- [ ] Uses `optimize_q` for Q selection (not hardcoded Q values)
- [ ] Calls `analyze_decay` for ringing mode Q selection

### Safety checks
- [ ] Every `apply_eq` / `apply_input_eq` mention includes mandatory 18Hz HPF
- [ ] Iterative steps use merge pattern (track applied filters in context -> audit -> merge -> apply full set)
- [ ] Target curve anchor excludes nulls (>15dB below average)
- [ ] Target curve anchor excludes below-port frequencies
- [ ] Does NOT re-anchor between iterations

### Multi-sub checks (if applicable)
- [ ] Calls `configure_matrix` for input routing
- [ ] Calls `compare_sub_phase` before applying alignment corrections
- [ ] Specifies delay reference sub (latest-arriving = 0)
- [ ] Every `mute_output` has a corresponding `unmute_output`
- [ ] Mutes shakers during calibration

### Tool reference checks
- [ ] Every tool name referenced exists in the Available MCP Tools list
- [ ] No tools referenced that don't exist (e.g. made-up tool names)
- [ ] Uses `measure` for fresh data (not `get_measurement_history` as baseline)

## Important rules

1. **Recipes go in `recipes/custom/`, never `recipes/core/`.** Core recipes are
   maintained by the project. Custom recipes are user-contributed.
2. **The templates are the source of truth.** Always read `recipes/TEMPLATE.md`
   and `recipes/CALIBRATION.md` before building or validating.
3. **Don't skip validation.** Every recipe must pass all applicable checks.
4. **Be opinionated about quality.** If the user's proposed recipe would skip
   analytics (e.g. "just measure and apply EQ"), explain why the analytics-first
   workflow produces better results and guide them to include it.
