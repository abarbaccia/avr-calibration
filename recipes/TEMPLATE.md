# Recipe Template

Every calibration recipe MUST follow this structure. The template encodes hard
rules that ensure safety, consistency, and full use of the analytics pipeline.

Recipes are read and executed by an LLM (Claude), not by a Python parser. Write
in plain English. Be specific about which MCP tools to call and in what order.

Save custom recipes to `recipes/custom/{name}.md` (never modify `recipes/core/`).

---

## Required Sections

Every recipe MUST include ALL of the following sections in this order.
Sections marked [CONDITIONAL] may be omitted with justification.

```markdown
# Recipe: {Name}

## Goal
{1-3 sentences. What does this recipe achieve? What is the target outcome?}

## Filter Strategy
{State which filter types are used: PEQ only, FIR only, or both.
 List which layers are used (output PEQ, input PEQ, FIR) and which tools
 write to each. State which layers are NOT used — this prevents the LLM
 from accidentally writing to the wrong target.}

| Layer | Tool | Slots | Purpose |
|-------|------|-------|---------|
| Output PEQ | `apply_eq` with `output_index` | N per output | ... |
| Input PEQ | `apply_input_eq` | N shared | ... |
| FIR | `apply_fir` | N taps/output | ... |

## Pre-flight
{MUST call `check_system` to verify hardware.
 MUST call `get_config` to discover output slots and EQ capabilities.
 MUST mute non-sub outputs (shakers) if config has them.}

## Phase 0 — Setup
{MUST clear existing EQ to known state before any measurement.
 MUST set volume and call `calibrate_level` for sweep SNR.
 For multi-sub: MUST call `configure_matrix` for input routing.
 For multi-sub: MUST measure each sub solo, level-match, apply trims.}

## Phase N — {Calibration Phases}
{The recipe's core calibration logic. See HARD RULES below for what
 every calibration phase must include.}

## Convergence
{MUST define explicit, measurable criteria.
 MUST use `compute_deviation` for RMS checks (it handles null exclusion).
 MUST specify frequency range (use 25-80Hz for sub bass).
 MUST specify max iterations.}

## When convergence fails
{MUST explain what to do if max iterations are reached.
 MUST distinguish between EQ-fixable and placement-fixable problems.}

## Phase N+1 — Retrospective
{MUST always run, even if calibration converged.
 See RETROSPECTIVE REQUIREMENTS below.}

## MCP tools used
{MUST list every tool the recipe calls, grouped by category:
 Hardware I/O, Analytics, Simulation, State and config.
 MUST only reference tools that actually exist (see AVAILABLE TOOLS below).}
```

---

## Hard Rules for Calibration Phases

These rules are non-negotiable. Every calibration phase that designs EQ must
follow this workflow:

### 1. Measure first, always fresh
Call `measure` to take a new sweep. Never use `get_measurement_history` as a
substitute for a fresh baseline — stale data may reflect a different EQ state,
volume, or sub configuration.

### 2. Analyze before designing
Before designing any correction filter:
- Call `analyze_phase(session_id)` to determine fixability per band
- Check coherence in measurement data (low coherence = unreliable data)
- For multi-sub: call `compare_sub_phase` to understand reinforcement/cancellation
- For ringing modes: call `analyze_decay(session_id)` for T60 and suggested_q

### 3. Only correct fixable problems
- `fixable=True` bands: safe to design PEQ/FIR corrections
- `fixable=False` bands: skip — recommend repositioning in the retrospective
- Low coherence (<0.8): don't design precise corrections based on noisy data

### 4. Simulate before applying
Call `simulate_eq(session_id, filters)` to predict the result before any
hardware write. Iterate on filter design in simulation until satisfied.
This is free — no hardware writes, no new measurements needed.

### 5. Use optimize_q for Q selection
Don't guess Q values. Call `optimize_q(session_id, freq_hz, target_gain_db)`
to numerically find the best Q. For ringing modes, prefer `suggested_q` from
`analyze_decay`.

### 6. Mandatory 18Hz HPF
Every `apply_eq` and `apply_input_eq` call MUST include:
`{"freq": 18.0, "gain_db": 0.0, "q": 0.707, "type": "hpf"}`

### 7. Iterative merge pattern
When iterating on EQ:
- Call `read_eq` (or `read_input_eq`) to get the currently applied filter set
- Design only the ADDITIONAL corrections needed
- Merge into the existing set
- Call `apply_eq` with the FULL merged set — never with just the delta
  (`apply_eq` replaces all slots; a delta-only write discards prior corrections)

### 8. Anchor target curves with null exclusion
When computing a reference level for a target curve:
- Exclude frequencies with SPL > 15 dB below band average (cancellation nulls)
- Exclude frequencies below 28 Hz (below port tuning rolloff)
- Do NOT re-anchor between iterations

### 9. Use compute_deviation for convergence
Call `compute_deviation(session_id, target_curve)` — it handles null zone
exclusion and below-port rolloff automatically. Don't compute RMS manually.

### 10. Prefer cuts over boosts
Cuts are always safe. Boosts are limited by SafetyValidator (+6 dB max per
band, +9 dB cumulative per 1/3 octave, +3 dB change per iteration).

---

## Retrospective Requirements

Every recipe MUST end with a retrospective phase that includes:

### Before/after scorecard
Use `compare_sessions` between baseline and final measurement. Present:
- RMS deviation before and after
- Worst peak/null before and after
- PEQ/FIR slots used
- Whether convergence was reached

### Unfixable problems — room improvement recommendations
Review `analyze_phase` results for `fixable=False` bands:
- **Sub placement**: identify nulls, recommend repositioning strategies
- **Room treatment**: `analyze_decay` modes with T60 > 500ms are bass trap candidates
- **Rattle detection**: narrow coherence drops = mechanical resonance

### FIR opportunities
Modes where PEQ reduced the peak but ringing persists (T60 still long) are
candidates for FIR correction in a future run.

### Next steps — prioritized action list
Numbered list ordered by expected impact, in plain language the user can act on.

---

## Available MCP Tools

Only reference tools from this list. Using a tool that doesn't exist will cause
the LLM to fail at runtime.

### Hardware I/O
| Tool | Purpose |
|------|---------|
| `check_system` | Pre-flight hardware verification |
| `measure` | Take a sweep measurement |
| `apply_eq` | Write PEQ filters to a DSP output (SafetyValidator enforced) |
| `apply_input_eq` | Write shared input PEQ filters |
| `apply_fir` | Write FIR coefficients to a DSP output |
| `clear_fir` | Clear FIR and reset to passthrough |
| `mute_output` / `unmute_output` | Mute/unmute specific DSP outputs |
| `set_delay` | Set output delay in ms |
| `set_polarity` | Set output polarity (normal/inverted) |
| `set_output_gain` | Set gain for a single DSP output |
| `set_volume` | Set AVR volume |
| `calibrate_level` | Find optimal sweep volume with good SNR |
| `configure_matrix` | Configure miniDSP routing matrix |
| `set_master_gain` | Set miniDSP master gain |
| `end_sweep_session` | Restore miniDSP source after calibration (call when done) |

### Analytics (data for LLM judgment)
| Tool | Purpose |
|------|---------|
| `analyze_phase` | Per-band fixability (minimum-phase vs excess-phase) |
| `compare_sub_phase` | Phase relationship between two solo sub measurements |
| `analyze_ir` | IR peak time, polarity sign, SPL |
| `analyze_decay` | T60 ringing analysis, suggested_q per mode |
| `compute_deviation` | RMS deviation from target with null/rolloff exclusion |

### Simulation (verify before applying)
| Tool | Purpose |
|------|---------|
| `simulate_eq` | Predict FR after proposed PEQ filters |
| `optimize_q` | Find best Q for a filter at a given frequency and gain |
| `design_fir` | Design FIR correction coefficients (minimum/linear/mixed phase) |

### State and Config
| Tool | Purpose |
|------|---------|
| `get_config` / `set_config` | Read/write system configuration |
| `get_device_state` | Current AVR + DSP hardware status |
| `get_output_state` | Per-output gain, delay, polarity, FIR taps |
| `get_measurement_history` | FR data with coherence (use format="compact" for bass) |
| `get_fr_summary` | 1/3-octave downsampled FR (coarse, for quick checks only) |
| `compare_sessions` | Per-band delta between two measurements |
| `read_eq` / `read_input_eq` | Current PEQ state |
| `fetch_recipe` | Load a recipe by name |

---

## Common Mistakes to Avoid

These mistakes were found in real recipes during audits. Don't repeat them.

1. **Retrieving stale data instead of measuring.** `get_measurement_history`
   returns whatever the last measurement was. Always call `measure` for a
   fresh baseline.

2. **Missing anchor step.** If your recipe uses a target curve with relative
   dB values (like Harman), you MUST compute an absolute reference level.
   Without anchoring, the LLM has no defined SPL to aim for.

3. **Analyzing phase AFTER applying corrections.** Always analyze first, then
   design. `analyze_phase` and `compare_sub_phase` inform the corrections —
   running them after wastes the data.

4. **Convergence range too wide.** Subs typically cross over at 80Hz. Don't
   evaluate convergence at 200Hz — the sub isn't responsible for that range.
   Use 25-80Hz for sub bass recipes.

5. **No null exclusion in anchor.** A single -20dB cancellation null drags
   the entire target down. Exclude nulls >15dB below band average.

6. **Implicit tool names.** Say "call `set_delay`", not "delay it to match."
   The LLM is more reliable with explicit tool references.

7. **Missing configure_matrix.** The miniDSP 2x4 HD default routing splits
   inputs across outputs. Multi-sub recipes must call `configure_matrix`.

8. **Forgetting to unmute.** Every `mute_output` must have a corresponding
   `unmute_output`. Account for error paths too.
