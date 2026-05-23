# Measurement chain architecture

Hardware-agnostic measurement: callers say `measure(target='X')`; the system
derives every chain parameter (route, sound_mode, sweep_channel,
frequency range, validation gates) from `signal_graph` + `measurement_profiles`
in `config.yaml`.

This eliminates the 2026-05-06 class of bug where a global `playback_route`
config sent sub measurements through HDMI to mains, silently producing
"successful" measurements of the wrong device.

## Principle

**`target` is authoritative.** It resolves to:
1. A transducer (e.g. `sub_front_right`) → that transducer's role + any
   per-transducer overrides
2. A group (e.g. `subs`) → the common role of its members
3. A role name (`sub`, `main`, `atmos`, `surround`, `lfe_pre`, `shaker`) →
   the role's profile directly

The role's `measurement_profile` provides the full chain spec. Hardware
details live in config; measurement code is hardware-agnostic.

## The physical routes

```
Sub measurements:
  Pi sweep → vc4-hdmi → AVR (DIRECT) → AVR sub-pre-out → Focusrite IN
    → CamillaDSP → Focusrite OUT → subs → room → mic → Pi

Mains-via-HDMI-direct (mains-only measurement):
  Pi sweep → vc4-hdmi → AVR (PURE DIRECT) → speaker outs → mains → room → mic → Pi

LFE-via-AVR-bass-mgmt (test bass-mgmt path):
  Pi sweep → vc4-hdmi → AVR (DIRECT) → AVR sub-pre-out → Focusrite IN
    → CamillaDSP → Focusrite OUT → subs → room → mic → Pi
```

## Profile schema

```yaml
measurement_profiles:
  sub:
    route: hdmi                             # sweep goes via HDMI → AVR → sub-pre-out → CamillaDSP
    sound_mode: DIRECT
    sweep_channel: LFE
    sweep_freq_min_hz: 15
    sweep_freq_max_hz: 200
    master_gain_db: -10                     # CamillaDSP master during sweep
    sweep_volume_db: null                   # AVR vol; null = use config default
    pre_sweep_validation:                   # ordered checks before sweep
      - check: audibility_ping
        freq_hz: 50
        min_spl_dbfs: -65

  main:
    route: hdmi
    sound_mode: PURE DIRECT
    sweep_channel: from_position            # FL→1, C→3, etc.
    sweep_freq_min_hz: 60
    sweep_freq_max_hz: 20000

  lfe_pre:                                  # bass-mgmt-via-AVR path (same as sub)
    route: hdmi
    sound_mode: DIRECT
    sweep_channel: LFE
    sweep_freq_min_hz: 15
    sweep_freq_max_hz: 200
```

## Per-target overrides

A specific transducer can override role defaults via
`measurement_overrides:`:

```yaml
signal_graph:
  transducers:
    - name: sub_nearfield
      role: sub
      processor: camilla
      output_index: 6
      measurement_overrides:
        master_gain_db: -5     # this one needs hotter sweeps
```

Resolution order (highest priority first):
1. Per-transducer `measurement_overrides`
2. Per-processor `by_processor` sub-profile (multi-DSP setups)
3. Role default profile
4. Built-in default in `DEFAULT_MEASUREMENT_PROFILES`

## Multi-processor setups

A single role can have different chain specs depending on which DSP drives
the transducer:

```yaml
measurement_profiles:
  sub:
    route: hdmi
    by_processor:
      minidsp_aux:
        sound_mode: DIRECT
        sweep_channel: LFE
```

## Validation gates (queued — task #57 phase 2)

The `pre_sweep_validation` list will run named checks before each sweep:

- `audibility_ping` — brief tone before sweep, abort if SPL < threshold
- `dsp_held_by_only_intended_process` — `lsof` check, refuse if multiple
  processes hold audio devices
- `avr_responsive` — check AVR is on and responsive

These are not yet implemented in the engine.

## Backwards compatibility

Existing recipes that don't use `target` and rely on
`measurement.playback_route` config fall through to the **legacy path**.
The resolver returns `legacy_path=True` and `_tool_trigger_measurement`
logs a deprecation warning. Recipes should be migrated to use `target`.

## Reference

- Task #57 (architectural refactor): scope and design
- `feedback_validate_chain_matches_target.md` (memory rule): the bug class
  this prevents
- `feedback_no_audio_bridge.md` (memory rule): no userland bridges; the
  DSP is the sole owner of the audio interface
