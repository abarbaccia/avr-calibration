# Level & Gain Architecture (authoritative)

This document is the single source of truth for **every gain stage** in the
calibration signal path, **which knob controls what**, and the **operating
levels**. It exists because "subs too quiet / which gain controls the subs"
has repeatedly cost multi-hour debugging sessions. Read this before touching
any level, and before diagnosing a quiet/failed sweep.

Verified empirically 2026-06-15 (master −20 dB → 50 Hz tone at MLP −8.8 dBFS;
master −40 dB → −28.7 dBFS — i.e. CamillaDSP master gain scales the subs ~1:1).

## TL;DR — the rules

1. **`cal_master_gain` (CamillaDSP) IS the sub level control.** It scales the
   sub acoustic output ~1:1. It is NOT a mains control. Set it via
   `set_master_gain(db)`.
2. **The loopback reference is tapped PRE-master** (at `avr_cal_sweep:monitor`,
   before CamillaDSP). So the reference level is independent of master gain;
   only the mic/acoustic level moves with master. Never reason about sub level
   from the reference — it is always near full scale.
3. **Master gain is a single persistent value; USB sweeps run at whatever it
   is — no per-sweep override.** (As of 2026-06-15.) Set it explicitly with
   `set_master_gain` or `calibrate_level`; it is saved to `active_dsp_state` and
   restored on restart/reboot. *Historical:* before this, `_USBSweepContext`
   forced `config.measurement.master_gain_db` (−50 dB) for the sweep duration and
   restored it after — a hidden override that silently defeated `set_master_gain`
   and ran sweeps too quiet. That class was removed.
4. **`set_input_gain(input_index=2,…)` does NOT affect the subs.** The
   `lfe_source` mixer fans physical capture channel 2 into logical input 0
   *before* the per-input gain filters, and the subs are routed from logical
   input 0. Per-input gain only helps on input 0.
5. **`set_output_gain` is alignment-only** (per-sub trim, ±dB), applied
   post-routing, and capped at +6 dB. It is not the level knob.
6. **Master gain does NOT persist across a reboot/restart.** It comes up at the
   `__init__` default (0 dB), because `active_dsp_state` has no master-gain
   field and rehydrate does not restore it. Per-output gain/delay/polarity/FIR
   and routing DO persist; master gain does not. (See "Footguns".)

## Signal flow (CamillaDSP pipeline) with every gain stage

```
pw-cat sweep ──► avr_cal_sweep (PW null sink)
                   │ monitor_FL
                   ├──► loopback_ref  ............ REFERENCE X  (PRE-master, ~full scale)
                   └──► camilladsp_capture:input_3  (capture channel index 2 = the LFE/sub feed)
                                │
   CamillaDSP pipeline (drivers/camilladsp.py _build_* , GetConfigJson order):
     0. lfe_source mixer      capture ch2 ──► logical inputs 0 AND 1   (fan-out)
     1. cal_in0_peq (ch0)     input PEQ on the sub feed (HPF 18 + 40/−6/Q1.4 + 57/−3/Q3)
     2. cal_in1_peq (ch1)     duplicate on ch1 (ch1 is unused downstream)
     3. cal_master_gain (ch0) ◄── THE SUB LEVEL KNOB (set_master_gain / USB sweep ctx)
     4. output_router mixer   ch0 ──► outputs 5 (sub_front_right) and 6 (sub_nearfield)
                              ch0 ──► output 7 (shaker, MUTED for cal)
     5..  per-output (channels 5,6):
            cal_outN_fir       (optional Conv — room-correction FIR)
            cal_outN_peq       (protective port PEQ: HPF 18 + 22/−10/Q3 + 28/−8/Q3)
            cal_outN_delay     (MSO alignment, e.g. out5 = 7.444 ms)
            cal_outN_gain      (alignment trim + polarity + mute; out6 polarity inverted)
                                │
                          Scarlett 18i20 line outs 5/6 ──► SVS PB12-NSD subs ──► room
                                │
   UMIK-1 (own USB device) ◄── MIC Y  (acoustic; scales with master gain)
   Deconvolution: H = Y(mic) / X(loopback_ref)
```

Key consequence of (2)+(3): the reference is hot (≈ −0.4 dBFS) and unaffected by
master; the mic is `referenceLevel − masterGain − acousticLoss`. At the −50 dB
measurement default the mic sweep lands ~−27 dBFS sig / SNR ~14 dB, which fails
the deconvolution cross-correlation gate (`validate_recording`, threshold 0.05)
and *sounds* too quiet in the room.

## Gain knobs — what each controls

| Knob (MCP tool) | Writes | Affects | Cap / notes |
|---|---|---|---|
| `set_master_gain(db)` | `cal_master_gain` filter, ch0, pre-`output_router` | **All subs ~1:1** | Overridden during a USB sweep by the sweep context (see below). Does not persist across reboot. |
| `set_input_gain(idx,db)` | `cal_in{idx}_gain`, pre-PEQ pre-mixer | input channel `idx` only | **idx=2 has NO effect on subs** — sub feed is logical input 0 after `lfe_source`. Use for Scarlett-trim make-up only. |
| `set_output_gain(idx,db)` | `cal_out{idx}_gain`, post-routing | one output | **Alignment trim only**, capped +6 dB. Not the level knob. |
| `calibrate_level(target_spl)` | iterates `set_master_gain`, writes `config.measurement.master_gain_db` | sub measurement level | The intended way to set the measurement level. Does NOT persist to `active_dsp_state`. |
| `restore_listening_mode()` | `set_master_gain(−20)` + routing | listening level | Hardcodes −20 dB. The de-facto "operating level". |

## Operating levels (current, and the incoherence)

| Concept | Where | Value | Used by |
|---|---|---|---|
| Measurement sweep level | `config.measurement.master_gain_db` | **−50.0** | `_USBSweepContext` forces this for every USB sweep |
| HDMI sweep level | `config.measurement.master_gain_hdmi_db` | −20 | HDMI route |
| Listening level | `restore_listening_mode` default | −20 | post-cal restore |
| Driver init default | `CamillaDSPDriver.__init__` | **0.0** | what comes up after a reboot |

**These four do not agree, and nothing reconciles them.** A reboot lands the
subs at 0 dB (loud); a USB sweep forces −50 dB (too quiet to correlate);
listening wants −20 dB. There is no single "operating level" source of truth and
no validation that the measurement level yields adequate SNR.

## Footguns (and current status)

1. **[FIXED 2026-06-15] Sweep no longer overrides master gain.** Sweeps run at
   the current persistent master. Set the level with `set_master_gain` /
   `calibrate_level`. (Was: forced to −50 dB → "cross-correlation peak too low".)
2. **The loopback reference is pre-master and always hot (~−0.4 dBFS).** It does
   NOT indicate sub level. Judge sub level from the mic (`sig`/`SNR` in the
   `HDMIPwCatPlayback` log line), never from the reference.
3. **`set_input_gain(2)` is a no-op for subs** (lfe_source reads ch2 before it).
4. **[FIXED 2026-06-15] Master gain now persists across reboot** in
   `active_dsp_state` (key `processor:<name>:master:gain`) and is restored on
   startup. (Was: reset to the 0 dB init default on every reboot.)
5. **`set_output_gain` caps at +6 dB** — it is alignment trim, not the level knob.
6. **A 50 Hz sustained tone ≠ a sweep** for level intuition: the tone sits on a
   room mode (+resonance), the sweep passes through. Compare sweeps to sweeps.

## Coherence work — status

1. **[DONE]** Persist master gain in `active_dsp_state` + restore on startup
   (`storage.dsp_master_key`, `CamillaDSPDriver.rehydrate_from_active_state`,
   `_tool_set_master_gain`/`calibrate_level` persist). No more 0 dB reboot landing.
2. **[DONE]** Removed the per-sweep master-gain juggling (`_USBSweepContext`);
   USB `sweep_context` is a no-op. One explicit, persistent level.
3. **Measurement-level validation is a RECIPE/orchestrator concern, not Python.**
   Per the architecture rule (no measure→adjust loops in Python), the recipe sets
   the level (via `calibrate_level`) and checks the reported `SNR`/xcorr, raising
   and retrying if low. `measure()` stays transparent — it runs at the current
   master and reports SNR; it does not silently force a level.
4. **TODO (evaluate later):** the HDMI/Denon path still saves/restores AVR volume
   per sweep (`DSPHDMISweepContext`/`DenonSweepContext`). Decide whether to fold
   that into the audio-mode switch so no per-sweep volume juggling remains.
   Tracked at `drivers/camilladsp.py: sweep_context()` TODO.
5. **set_input_gain idx semantics** documented at the tool; subs are logical
   input 0 after `lfe_source`.

## See also
- `docs/pipewire-architecture.md` — the PW graph (null sinks, input_3, loopback_ref)
- `CLAUDE.md` — signal chain, safety limits, `CamillaDSP −20 dB master is the level match`
- Memory: `feedback_master_gain_is_sub_level`, `feedback_usb_sweep_forces_measurement_gain`
