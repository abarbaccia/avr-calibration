# Measurement chain architecture

Hardware-agnostic measurement: callers say `measure(target='X')`; the system
derives every chain parameter (route, cal_mode, sound_mode, sweep_channel,
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

## The four physical routes

```
Sub-via-cal (sub measurements):
  Pi sweep → snd-aloop → CamillaDSP (cap=Loopback) → Focusrite OUT → subs

Sub-via-live (normal listening; not a measurement path):
  AVR LFE → Focusrite IN → CamillaDSP (cap=USB) → Focusrite OUT → subs

Mains-via-HDMI-direct (mains-only measurement):
  Pi sweep → vc4-hdmi → AVR (PURE DIRECT) → speaker outs → mains

LFE-via-AVR-bass-mgmt (test bass-mgmt path):
  Pi sweep → vc4-hdmi → AVR (DIRECT) → AVR sub-pre-out → Focusrite IN
    → CamillaDSP (cap=USB, must be live) → Focusrite OUT → subs
```

The first goes via cal_mode; the second is listening-only; the third
ignores the DSP entirely; the fourth requires DSP in live mode.

## Profile schema

```yaml
measurement_profiles:
  sub:
    route: usb                              # 'usb' or 'hdmi'
    cal_mode:                               # nullable
      enabled: required                     # 'required' | 'forbidden' | 'optional'
      capture_device: hw:Loopback,1,0       # DSP captures from here in cal mode
      playback_device: hw:Loopback,0,0      # measure() injects sweep here
      channels: 2
    sound_mode: null                        # AVR mode; null = don't touch
    sweep_channel: null                     # HDMI channel; null = N/A for USB
    sweep_freq_min_hz: 15
    sweep_freq_max_hz: 200
    master_gain_db: -10                     # CamillaDSP master during sweep
    sweep_volume_db: null                   # AVR vol; null = use config default
    pre_sweep_validation:                   # ordered checks before sweep
      - check: dsp_capture_device_matches
        expected: hw:Loopback,1,0
      - check: audibility_ping
        freq_hz: 50
        min_spl_dbfs: -65
    reference_loopback:                     # optional output-side loopback
      device: hw:Loopback,1,1               # for IR-alignment precision
      pick_channel: 8
      enabled: when_available

  main:
    route: hdmi
    cal_mode: null                          # DSP not in chain; cal_mode irrelevant
    sound_mode: PURE DIRECT
    sweep_channel: from_position            # FL→1, C→3, etc.
    sweep_freq_min_hz: 60
    sweep_freq_max_hz: 20000

  lfe_pre:                                  # bass-mgmt-via-AVR path
    route: hdmi
    cal_mode: { enabled: forbidden }        # DSP must be in LIVE mode
    sound_mode: DIRECT
    sweep_channel: LFE
    sweep_freq_min_hz: 15
    sweep_freq_max_hz: 200
```

## cal_mode semantics

- **`null`**: cal_mode irrelevant. The chain doesn't go through the DSP at
  all (e.g. mains in PURE DIRECT). Measurement engine doesn't check.
- **`enabled: required`**: cal_mode must be ON before the sweep. The DSP
  must capture from snd-aloop so the Pi sweep reaches it. (Sub measurements.)
- **`enabled: forbidden`**: cal_mode must be OFF before the sweep. The DSP
  must capture from the live audio interface for the chain to work
  (lfe_pre — AVR's sub-pre-out goes to Focusrite IN3, which the DSP must
  be capturing).
- **`enabled: optional`**: either is acceptable.

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
    route: usb
    cal_mode: { enabled: required, ... }
    by_processor:
      minidsp_aux:
        cal_mode: { enabled: optional }    # this DSP doesn't need cal_mode
        playback_device: hw:miniDSP_aux
```

## Validation gates (queued — task #57 phase 2)

The `pre_sweep_validation` list will run named checks before each sweep:

- `dsp_capture_device_matches` — read-back DSP config, refuse if mismatch
- `audibility_ping` — brief tone before sweep, abort if SPL < threshold
- `dsp_held_by_only_intended_process` — `lsof` check, refuse if multiple
  processes hold audio devices
- `avr_responsive` — check AVR is on and responsive

These are not yet implemented in the engine; the cal_mode enforcement gate
in `_tool_trigger_measurement` is the only check today.

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
- `feedback_cal_mode_required_for_sub_measure.md` (memory rule): the
  earlier symptom-level rule, now structural
