# Calibration Recipe Patterns

Recipes that design and apply EQ/FIR corrections MUST follow these patterns
in addition to the universal `TEMPLATE.md` rules.

Measurement-only recipes (sub crawl, room verify, diagnostics) do NOT need
to follow these — they don't write to DSP.

---

## Additional Required Sections

Calibration recipes MUST include these sections beyond what TEMPLATE.md requires:

```markdown
## Configuration
{Parameterize user choices:
 - Target curve: load from `recipes/curves/{name}.json`, suggest default
 - Frequency range: suggest based on driver capability and crossover
 - Convergence threshold: default 1.5 dB, user-adjustable
 - Max iterations: default 5 per phase
 Present interactively. Most users accept defaults.}

## Filter Strategy
{State which filter layers are used, mark optional ones:

| Layer | Tool | Slots | Purpose | Required? |
|-------|------|-------|---------|-----------|

 State the RECIPE phase ordering (may differ from DSP signal chain).
 FIR should come BEFORE target-curve PEQ so PEQ is designed against
 the corrected response.}

## Convergence
{MUST define explicit, measurable criteria.
 MUST use `compute_deviation` for RMS checks.
 MUST specify frequency range from the configured range.
 MUST specify max iterations.
 Default convergence RMS: 1.5 dB.}

## When convergence fails
{MUST explain what to do if max iterations reached.
 MUST distinguish EQ-fixable vs placement-fixable problems.}

## Retrospective
{MUST always run, even if calibration converged perfectly.
 See RETROSPECTIVE section below.}
```

---

## Hard Rules for Filter Design

Non-negotiable. Every phase that designs EQ must follow these.

### 1. Analyze before designing
Before designing any correction filter:
- Call `analyze_phase(session_id)` — fixability per band
- Check coherence (low < 0.8 = unreliable, don't design there)
- For multi-driver: `compare_sub_phase` for reinforcement/cancellation
- For ringing: `analyze_decay(session_id)` for T60 and `suggested_q`

### 2. Only correct fixable problems
- `fixable=True`: safe to design PEQ/FIR corrections
- `fixable=False`: skip — recommend repositioning in retrospective
- Low coherence: don't design precise corrections on noisy data

### 3. Full-resolution FR for filter design
Always pull `get_measurement_history(format="compact", min_hz=..., max_hz=...)`
when designing filters. ~0.18 Hz resolution finds exact peak/dip centers.

Do NOT design filters from 1/3-octave summaries (`get_fr_summary` or
`compute_deviation` band centers). Those are for convergence checks only.

A 5 dB peak at 47.3 Hz is invisible in 1/3-octave (between 40 and 50 Hz)
but obvious at full resolution.

### 4. Simulate before applying
Call `simulate_eq(session_id, filters)` to predict the result before any
hardware write. Iterate in simulation until satisfied. Free — no hardware
writes, no new measurements.

### 5. Use optimize_q for Q selection
Don't guess Q values. Call `optimize_q(session_id, freq_hz, target_gain_db)`.
For ringing modes, prefer `suggested_q` from `analyze_decay`.

### 6. Mandatory 18 Hz HPF
Every `apply_eq` and `apply_input_eq` call MUST include:
`{"freq": 18.0, "gain_db": 0.0, "q": 0.707, "type": "hpf"}`

### 7. Filter audit on every iteration
Before designing new corrections, audit every existing filter:
- Simulate the set with each filter removed. Impact < 0.3 dB? Remove it.
- Has the measured response shifted at this filter's frequency? Re-optimize.
- Only THEN design new filters for remaining deviations.
Don't just add filters — also prune.

### 8. Iterative merge pattern
When iterating on EQ:
- Track the filter set YOU applied in conversation context — the miniDSP is
  write-only, so there is no tool to read the current filters back
- Run filter audit (rule 7)
- Design only additional corrections needed
- Merge into the audited set
- Apply the FULL merged set — never just the delta
  (`apply_eq` replaces all slots; delta-only discards prior corrections)

### 9. Anchor target curves with null exclusion
When computing a reference level for a target curve:
- Exclude frequencies > 15 dB below band average (cancellation nulls)
- Exclude frequencies below driver's usable range (port rolloff, etc.)
- Do NOT re-anchor between iterations

### 10. Clean baselines after structural changes
After any major DSP change (FIR application, output PEQ redesign), take a
fresh measurement with downstream processing cleared. This gives `simulate_eq`
an accurate reference for the new state.

### 11. Prefer cuts over boosts
Cuts are always safe. Boosts are limited by SafetyValidator (+6 dB/band,
+9 dB cumulative per 1/3 octave, +3 dB change per iteration).

---

## Retrospective Requirements

Every calibration recipe MUST end with:

### Before/after scorecard
`compare_sessions` between baseline and final measurement:
- RMS deviation before → after
- Worst peak/null before → after
- DSP resources used (PEQ slots, FIR taps)
- Whether convergence was reached

### Unfixable problems — room improvement recommendations
Review `analyze_phase` for `fixable=False` bands:
- **Driver placement**: nulls from room mode cancellation, repositioning strategies
- **Room treatment**: `analyze_decay` modes with T60 > 500ms = trap candidates
- **Rattle detection**: narrow coherence drops = mechanical resonance

### Next steps — prioritized action list
Numbered, ordered by expected impact, in plain language the user can act on.

---

## Common Calibration Mistakes

1. **Applying FIR after target PEQ.** FIR changes magnitude 3-6 dB, invalidating
   PEQ work. Apply FIR before target PEQ.

2. **Additive-only iterations.** Only adding filters without auditing leads to
   stale filters wasting slots. Audit every iteration.

3. **No baseline after FIR.** `simulate_eq` against a pre-FIR session produces
   inaccurate predictions. Take a clean post-FIR baseline.

4. **Designing from 1/3-octave data.** 6 points across 25-80 Hz. Peaks between
   band centers are invisible. Use full-res compact FR.

5. **Hardcoding target curves.** Load from `recipes/curves/{name}.json`.
   Users pick curves, recipes implement the algorithm.

6. **Convergence range mismatch.** Don't evaluate above the crossover — the
   recipe's drivers aren't responsible for that range.
