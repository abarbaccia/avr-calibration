# CamillaDSP driver notes

What the CamillaDSPDriver expects, what it owns, and what the user is
responsible for. For step-by-step setup of any particular rig, describe your
hardware to Claude and reason through the specifics together — this doc
covers the invariants, not a recipe.

## What the driver expects

A running CamillaDSP daemon reachable over websocket. The daemon can live
wherever (bare metal, another container, another host) as long as the MCP
server can open a TCP connection to its control port. The driver does not
start, supervise, or restart the daemon.

## What the driver owns

The entire active pipeline. Every state-mutating call (`apply_eq`,
`set_output_*`, `mute_outputs`, `set_routing`, `apply_fir`) rebuilds the full
config from shadow state and pushes it via `SetConfig`. The daemon is a
reflection of the driver's shadow, not an independent authority. Hand-authored
pipelines, user customisations, extra filter blocks — all get overwritten
the first time the driver mutates state.

## What the user plumbs via `config.yaml`

Everything the driver can't infer and has no opinion about:

- Where the daemon lives (host, port)
- Audio hardware topology — capture device, playback device, sample rate,
  chunk size, channel counts
- Which output channels are subs (`sub_outputs`) — used for the default
  routing and as the broadcast target for `apply_eq` calls without an
  explicit `output_index`

The driver accepts the `capture` and `playback` blocks verbatim into the
CamillaDSP `devices` section. Anything CamillaDSP accepts there, the
driver accepts.

## Default routing is minimum-safe

Input 0 → `sub_outputs` only, every other output silent. Broadcasting the
LFE sweep to every channel at full gain is a real way to blow a tweeter;
the driver starts narrow and the caller expands via `set_routing` once the
rest of the topology is known.

## What the driver does not have

- Preset slots. `set_preset(N)` is a no-op; `current_preset()` returns 0.
  Recall-by-preset is the MCP server's job, not the DSP's.
- Source switching. `valid_sources` is an empty frozenset; the HDMI sweep
  path in the MCP server already skips `set_source` when the driver reports
  no sources.
- Flash / non-volatile state. A daemon restart loses the pipeline unless
  the driver re-pushes. The MCP server's shadow rehydration path does not
  yet apply to CamillaDSP (follow-up).
- Tap-count ceilings beyond chunk size. FIR goes inline via Conv with
  `type: Values`; no temp files, no shared pool.

## What has to be true outside the driver

- If a Denon sits in the chain, it has to be transparent at the calibration
  input: Audyssey / DynamicEQ / DynamicVolume / Restorer off, Pure or Source
  Direct on, per-channel trims zero, tone defeat on, distances at reference,
  bass management assigned to exactly one of {Denon, CamillaDSP}. Any signal
  path DSP on the receiver silently invalidates the calibration.
- The ALSA capture and playback devices named in `config.yaml` actually
  exist at daemon start and aren't held open by another process.
- The MCP server's container or host can reach the daemon's websocket port.
  Host networking, an SSH tunnel, or a routable IP — driver doesn't care.

## Where this interacts with the rest of the system

- `DSPDriver.capabilities` — CamillaDSP reports `max_preset_index=-1`,
  `valid_sources=∅`, `max_delay_ms=1000`, `fir_shared_tap_pool=None`.
  Callers read through `driver.capabilities` instead of importing
  hardware-specific constants.
- `DSPDriver.sweep_context(config)` — returns `None`. CamillaDSP is always
  passing audio; no pre-sweep setup needed.
- Safety layer — `SafetyValidator` runs on the same code path as
  `MinidspDriver`. The mandatory 18 Hz HPF is required on every `apply_eq`
  call regardless of driver.
