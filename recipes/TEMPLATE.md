# Recipe Template

Recipes are instructions read and executed by an LLM (Claude), not by a Python
parser. Write in plain English. Be specific about which MCP tools to call.

Save custom recipes to `recipes/custom/{name}.md` (never modify `recipes/core/`).

---

## Required Sections

Every recipe MUST include these sections:

```markdown
# Recipe: {Name}
version: {semantic version}

## Goal
{1-3 sentences. What does this recipe achieve?
 State which hardware configurations it supports.
 If it adapts to hardware, say so.}

## Measurement Signal Path
{Show the signal path DURING THIS RECIPE, not the production listening path.
 Build dynamically from `get_config` — do not hardcode.
 Show: signal source → DSP → outputs → room → mic → recording}

## Pre-flight
{MUST call `check_system` to verify hardware.
 MUST call `get_config` to discover capabilities.
 MUST mute outputs not relevant to this recipe.}

## Phase 0 — Setup
{MUST reset ALL DSP state to known defaults before any measurement.
 The miniDSP is write-only — there is no readback. `get_output_state`
 only reflects what this MCP server has written since it started.
 Hardware flash retains prior settings. Always write explicitly.}

## Phase N — {Recipe-specific phases}
{The recipe's core logic.}

## MCP tools used
{MUST list every tool the recipe calls, grouped by category.
 MUST only reference tools that actually exist (see AVAILABLE TOOLS below).}
```

Calibration recipes (those that design and apply EQ/FIR corrections) MUST also
follow the patterns in `CALIBRATION.md`.

---

## General Rules

These apply to ALL recipes, not just calibration.

### 1. Measure fresh
Call `measure` for a new sweep. Never use `get_measurement_history` as a
substitute — stale data may reflect a different EQ state or volume.

### 2. Explicit tool names
Say "call `set_delay`", not "delay it to match." The LLM is more reliable
with explicit tool references.

### 3. Always unmute when done
Every `mute_output` must have a corresponding `unmute_output`. Account for
error paths — if the recipe fails mid-run, outputs must still be restored.

### 4. Adapt to hardware, don't fork
Don't create separate recipes for hardware variants (PEQ-only vs FIR+PEQ,
single vs multi-driver). One recipe should check capabilities and skip
phases that don't apply. Report what was skipped and why.

### 5. Parameterize user choices
Don't hardcode values the user should pick (target curves, frequency ranges,
thresholds). Load curves from `recipes/curves/{name}.json`. Present options
interactively, suggest defaults based on config.

### 6. Configure input routing
The miniDSP 2x4 HD default matrix splits inputs across outputs. Any recipe
that uses multiple outputs MUST call `configure_matrix`.

---

## Available MCP Tools

Only reference tools from this list.

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
| `get_measurement_history` | FR data with coherence (use format="compact") |
| `get_fr_summary` | 1/3-octave downsampled FR (coarse, quick checks only) |
| `compare_sessions` | Per-band delta between two measurements |
| `fetch_recipe` | Load a recipe by name |

> PEQ is write-only — track filters you applied in conversation context; pass the
> full merged set to `apply_eq` each iteration (never just a delta).
