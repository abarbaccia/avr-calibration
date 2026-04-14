# /headroom

Test amplifier headroom and detect compression onset. Plays multitone clusters
through all HDMI speakers simultaneously, steps Denon volume 1dB at a time, and
uses FFT analysis to distinguish power supply sag from per-channel clipping.

## Arguments

None required. Uses config headroom defaults, overridable via conversation.

## Workflow

### Step 0 — Pre-flight and safety briefing

1. Call `check_system` to verify AVR and DSP are reachable.
2. Call `get_config` to read headroom defaults and hardware layout.
3. Call `get_device_state` to capture starting state (volume, input, source).
4. Warn the user:
   > This test will step the Denon volume from {start_volume_db} to {max_volume_db}
   > in 1dB steps while playing tones on all speakers. It gets progressively louder.
   > Consider ear protection at higher levels. The test will auto-abort if compression
   > is detected for 2 consecutive steps.
5. Confirm with user before proceeding.

### Step 1 — Isolate subs from the test

The headroom test measures the Denon's amp channels, not the miniDSP/sub path.
Disconnect the subs by switching miniDSP to USB input:

1. Call `get_device_state` to check current miniDSP source.
2. If source is "Analog", tell the user: "Switching miniDSP to USB input to
   disconnect subs from the Denon pre-out during the test. Subs will be silent."
3. **Ask user to confirm** before switching (signal path write requires approval).
4. Remember the original source to restore later.

### Step 2 — Speaker characterization (Phase 1)

For each speaker channel in the HDMI channel map (left, right, center,
surround_left, surround_right — skip LFE):

1. Set Denon to a safe reference level (e.g. -25dB).
2. Use `set_config` to set `measurement.sweep_channel` to the speaker role.
3. Call `measure` with a descriptive label (e.g. "headroom-characterize-FL").
4. From the returned FR data, determine the flat passband:
   - Find the frequency range where SPL is within +/-3dB of the median
   - The passband is (low_3db_hz, high_3db_hz)
   - Stay above 200Hz to avoid room modes
5. Record each speaker's passband and efficiency (SPL at reference volume).

### Step 3 — Tone cluster assignment (Phase 2)

1. Call `assign_headroom_tones` with the measured passbands from Step 2.
2. Inspect the returned assignments — verify tones are reasonable.
3. Report to user: "Assigned tone clusters: FL=[500, 800, 1200, 1600], FR=[530, 830, 1230, 1630], ..."
4. The tool returns `channel_assignments` in the exact format needed by `play_and_measure_fft`.

### Step 4 — Volume stepping load test (Phase 3)

Set Denon to `start_volume_db` (default: -30dB). Then loop:

1. Call `set_volume(volume_db)` — set Denon to current step.
2. Wait 2 seconds (Denon volume settle time).
3. Call `play_and_measure_fft(channel_assignments, duration_s=hold_duration_s)`.
4. Parse the returned per_tone data. Group tones by speaker (you know which
   frequencies belong to which speaker from Step 3).
5. For each speaker, compute:
   - Average SPL across its tones
   - Average THD across its tones
6. Record: `{volume_db, speakers: {role: {spl_dbfs, thd_db}}}`.
7. Check abort conditions:
   - **SPL stall**: If ALL speakers gained <0.3dB for 2 consecutive steps, STOP.
     This is power supply sag — going further risks protection trip.
   - **THD spike**: If any speaker's THD increased >6dB in one step, STOP.
     This is clipping.
   - **Max volume**: If current volume >= max_volume_db, STOP.
   - **Protection trip**: If recording is silent (peak < -80dBFS), the Denon
     likely tripped protection. STOP immediately.
8. If no abort, increment volume by step_db and repeat.

Report progress every 5 steps: "Step {n}: volume={vol}dB, FL={spl}dBFS, all linear so far."

### Step 5 — Analysis (Phase 4)

After the loop ends (compression detected or max volume reached):

1. Compute per-step gain for each speaker: gain = SPL[i] - SPL[i-1].
2. Find compression onset: first step where gain < 0.7 dB (for 1dB steps).
3. Classify failure mode:
   - **Power supply sag**: All speakers compress within 1.5dB of each other
   - **Per-channel clipping**: One speaker compresses or THD spikes while others stay clean
   - **None**: Reached max volume with no compression
4. Report headroom = onset_volume - reference_volume.
5. Present results clearly:

   ```
   === Headroom Test Results ===

   Reference level: -20 dB (your normal listening volume)
   Compression onset: -11.5 dB
   Headroom: 8.5 dB above reference

   Failure mode: Power supply sag
   All channels compressed simultaneously at -11.5 dB.

   Per-channel summary:
     FL: linear to -11.5 dB, final THD 1.2%
     FR: linear to -11.5 dB, final THD 1.1%
     C:  linear to -12.0 dB, final THD 1.4%
     SL: linear to -11.5 dB, final THD 0.9%
     SR: linear to -11.5 dB, final THD 0.8%

   Recommendation: External amplification for FL/FR/C would extend
   headroom. The surround channels have adequate headroom for most content.
   ```

### Step 6 — Cleanup

**Always run this, even if the test fails or is aborted:**

1. Set Denon volume back to the saved starting volume.
2. If miniDSP source was changed, **ask user to confirm** restoring it to Analog.
3. Report: "Test complete. Volume and miniDSP source restored."

## Safety rules

1. **Max volume ceiling**: Never exceed `max_volume_db` from config (default: -10dB).
   The user can lower this but the skill should warn before going above -5dB.
2. **Auto-abort on stall**: 2 consecutive no-gain steps → stop immediately.
3. **Auto-abort on silence**: Recording peak < -80dBFS → protection tripped, stop.
4. **Always restore**: Volume and miniDSP source MUST be restored in cleanup.
5. **Signal path writes need confirmation**: Switching miniDSP source requires
   explicit user approval per CLAUDE.md rules.
6. **Start quiet**: Always begin at start_volume_db (-30 default), never skip ahead.
