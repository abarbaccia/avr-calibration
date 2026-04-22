# Signal graph

The **composed prism** — a stack of layers signal passes through, each with
settable state — is how the system models what's between source and mic.
This doc describes what the graph is, what it owns, what the user plumbs,
and what reasoning it enables. The specifics of any particular rig are
something to reason through at use-time, not to enumerate here.

## What the graph is

A set of named nodes with implicit edges:

- **Sources** — places signal enters the chain (an AVR input, a direct USB
  sweep, analog RCA, a file generator).
- **Processors** — nodes with filterable state. AVR (volume, trim, distance,
  sound mode) and DSP (routing, PEQ, FIR, delay, gain) are both processors.
  A cable is not.
- **Transducers** — physical drivers in the room (sub, main, centre,
  surround, height, shaker). Each references a processor + output index and
  a safety profile.
- **Profiles** — per-model hardware limits (boost ceilings, mandatory HPF,
  per-iteration change caps). Shared across transducers of the same model.
- **Groups** — named scopes (`"bass"`, `"front_soundstage"`) so recipes
  address a set of transducers with one identifier.

Edges are implicit: a `Source.avr_input_ref` connects to a `Processor.inputs`
entry; a `Processor.outputs` entry connects to a `Transducer` whose
`processor_ref` and `output_index` identify that output.

## What the graph owns

**Names.** It's how the LLM and recipes address calibration scope — `target="bass"`
beats `output_index=0`. Names are stable across driver swaps; indices are not.

**Safety dispatch.** Each transducer carries a profile reference; `apply_eq`
with a `target` validates filters against the transducer's profile (for
per-output EQ) or the strictest profile in the scope (for input EQ, which
affects all downstream transducers).

**Sweep-context composition.** For a sweep from a given source to a set of
transducers, the graph walks the processors in path order and stacks each
driver's `sweep_context`. AVR-side neutralisation (Pure Direct, input,
volume save) and DSP-side neutralisation (source switch, routing) compose
uniformly without the caller knowing which driver does what.

## What the user plumbs via `config.yaml`

A `signal_graph:` block lists sources, processors, transducer profiles,
transducers, and groups. Every field the LLM would need to reason about
scope lives here. When the block is absent, the legacy shim synthesises a
graph from `dsp_driver`, `minidsp.output_slots`, and `measurement.sub_outputs`
so existing installs keep working unchanged.

An explicit graph is worth writing when any of the following is true: more
than one DSP instance, transducers that aren't SVS PB12-NSDs (safety
profiles matter), multiple named sources the LLM can reason about, or
named groups for scoped calibration.

## What the graph is loose about on purpose

- **Role is an open string.** The LLM can reason about `"height"` or
  `"top_middle"` without an enum change. Types enforce structure; strings
  enable reasoning.
- **No cycle detection.** A graph with a loop is nonsensical, but the LLM
  catches that during calibration; a static validator would reject edge
  cases that turn out to be legitimate (two DSPs cross-feeding for room
  correction + bass management, for instance).
- **No runtime reconfiguration.** Config is load-time only. Hot-swapping
  processors mid-calibration is the kind of capability that exists mostly
  to add bugs.

## Where this interacts with the rest of the system

- **`DSPDriver` / `AVRDriver`** — unchanged in shape. The graph holds
  references to processor names; a driver registry (`DriverRegistry`) maps
  processor name → driver instance. Legacy single-X dispatch (`_default_dsp`,
  `load_dsp_driver`) resolves through the registry, so existing callers keep
  working.
- **`SafetyValidator`** — takes a `TransducerProfile` at construction. The
  built-in `SVS_PB12_NSD_PROFILE` is the default, which means every legacy
  caller keeps its historical behaviour.
- **Storage keys** — `active_dsp_state` keys are namespaced
  `processor:<name>:output:<idx>:<field>` so multi-DSP installs don't
  collide on flat keys. Legacy flat keys are migrated lazily on first open.
- **MCP surface** — `apply_eq` and `apply_input_eq` gain an optional
  `target` parameter (group / transducer / role name). Legacy
  `output_index` still works. New tools: `get_signal_graph` (compact
  summary for LLM reasoning) and `resolve_target` (name → concrete
  transducer list).

## Known gaps (follow-ups)

- **AVR-side writes** — trims / distances / bass management still live on
  the Denon; the graph describes them but the `DenonDriver` doesn't yet
  read or write them. Tracked as `TODO-CAMILLA-PREFLIGHT`. Deferred until
  the user can validate writes against live hardware.

## Recently closed

- ~~Per-tool `target` on EQ only~~ — `set_delay`, `set_polarity`,
  `set_output_gain`, `mute_output`, and `unmute_output` now accept `target`
  and dispatch across multiple processors.
- ~~Manual Denon+DSP nest in `_tool_trigger_measurement`~~ — HDMI route
  now goes through `graph.sweep_context_for_route`. The DSP's HDMI-mode
  neutralisation (source=Analog, `master_gain_hdmi_db`) lives in a
  driver-agnostic `DSPHDMISweepContext`.
- ~~Multi-DSP target dispatch rejects cleanly~~ — `apply_eq` and
  `apply_input_eq` now route each transducer to the driver that owns its
  output, bucketing by processor where it matters (input EQ applies
  per-processor against that bucket's strictest profile).
