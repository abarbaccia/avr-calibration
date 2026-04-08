# Audio Debugging Lessons Learned

Hard-won debugging findings from calibration sessions. Read this before debugging
any measurement failure. Most SNR/sweep-detection issues come from one of these.

---

## 1. Never use `sd.playrec()` or `sd.rec()` + `sd.play()` for measurements

**Symptom:** SNR collapses to ~0 dB regardless of volume. `sd.playrec()` with two
different USB devices (UMIK on hw:3, miniDSP on hw:2) uses non-synchronized
PortAudio streams. The recording may start *after* playback is already underway,
putting sweep energy into the floor window and making floor_rms ≈ signal_rms.

**Rule:** Always use explicit `sd.InputStream` + `sd.OutputStream`. Start the
InputStream FIRST. See `HDMIPlayback` and `USBPlayback` in `calibrate/drivers/playback.py`.

**Related:** `sd.rec(float32)` + `sd.play(int16_buffer)` also corrupts the recording
buffer (dtype mismatch). Fixed in PR #48 for HDMI route, then rediscovered for USB.

---

## 2. Start recording BEFORE playback — and add a pre-delay

**Symptom:** SNR ~1 dB even though the sub is clearly playing (output_levels confirm
signal at the DSP output). Cross-correlation detects the sweep but SNR is too low.

**Root cause:** USB audio playback starts within ~50-200 ms of `out_stream.start()`.
The SNR validator uses the first 500 ms of recording as the noise floor. If the sweep
arrives within that window, floor_rms ≈ signal_rms → SNR ≈ 0 dB.

**Fix:** Sleep 700 ms between `in_stream.start()` and `out_stream.start()`:

```python
in_stream.start()
time.sleep(0.7)   # ensure floor window is clean pre-sweep silence
out_stream.start()
out_stream.write(sweep_buf)   # blocking write — waits for all audio to play
```

This is analogous to the Denon settle delay (`denon_settle_ms`) on the HDMI route.

---

## 3. miniDSP source switch resets routing AND hardware mutes

**Symptom:** Measurement works on first call. On subsequent calls (after source
switches Analog → USB → Analog → USB), output mutes are reset — all outputs play.

**Root cause:** `minidsp source <name>` resets the routing matrix AND clears all
per-output hardware mutes, regardless of whether the source is actually changing.

**Rules:**
- After every source switch, always reconfigure routing via `_configure_routing_via_cli()`
- After routing, always re-apply all 4 output mutes via individual CLI calls
- Batch mode (`minidsp -f -`) works for routing but NOT for output mute commands
- Individual sequential `minidsp output N mute on/off` calls are required for mutes

**Exception tested:** `minidsp source usb` when already on USB does NOT reset
mutes or routing (confirmed). Only actual source changes reset state.

---

## 4. miniDSP output mute state is not readable from hardware

**Symptom:** After a container restart or source switch, in-memory mute tracking
is lost. Code tries to "read" mute state from output_levels but this is unreliable.

**Root cause:** Neither `minidsp -o json status` nor the HTTP API (`GET /devices/0`)
expose per-output mute as a boolean. `output_levels` show -120 dBFS for muted outputs
but ALSO show -120 dBFS for unmuted outputs with no signal in Analog mode (noise floor
at the same value as the mute floor).

**Fix:** Track mute state in driver memory (`MinidspDriver._output_muted`). After any
source switch, iterate ALL 4 outputs and set mutes explicitly — do not skip outputs
that haven't been explicitly tracked (default those to unmuted).

---

## 5. ALL miniDSP writes must use CLI — never HTTP

**Symptom:** After an HTTP write (POST /devices/0/config), subsequent CLI PEQ writes
cause DSP state corruption. HTTP routing is reset by the next CLI WebSocket connection.

**Rule:** ALL writes go through `_run_minidsp_cli()` or `_run_minidsp_batch()`.
The HTTP client (`MinidspClient`) is for READS only (GET /devices/0 for status/levels).
PEQ, routing, gain, delay, polarity, mute — all CLI.

**Related:** The minidspd HTTP config API has a sign bug in the `a1`/`a2` feedback
coefficients. Using HTTP for PEQ writes sends wrong signs and causes DSP hang
(output frozen at 0.0 dBFS, requires physical power-cycle to recover).

---

## 6. miniDSP PEQ biquad sign convention: negate a1/a2 before sending

**Symptom:** DSP freezes at 0.0 dBFS on all outputs after applying PEQ. Requires
physical power-cycle to recover. Bypassing slots or switching presets does not help.

**Root cause:** The miniDSP 2x4 HD hardware uses a positive-sign feedback recurrence:
`y[n] = b0*x + b1*x[-1] + b2*x[-2] + a1_hw*y[-1] + a2_hw*y[-2]`
scipy/standard DSP uses negative signs for feedback (`-a1`, `-a2` in the denominator).
Sending scipy's a1/a2 directly causes poles at |z|≈2.4 (unstable) → DSP overflow.

**Fix:** In `set_output_peq_cli()` and `set_input_peq_cli()`: negate a1 and a2 before
sending: `str(-c["a1"]), str(-c["a2"])`. Fixed in `calibrate/adapters/minidsp.py`.

---

## 7. Never run concurrent miniDSP CLI calls

**Symptom:** Random state corruption — wrong EQ applied, routing partially set,
mutes inconsistently applied.

**Root cause:** Each `minidsp` CLI invocation opens a new WebSocket to minidspd.
Concurrent WebSocket sessions corrupt the device's command stream.

**Rule:** All CLI calls are sequential. `_run_minidsp_batch()` (single session via
`minidsp -f -`) is preferred for multiple routing commands. Individual calls
for mute commands (batch mode doesn't reliably apply output mutes).

---

## 8. USB playback bypass the AVR — volume knob does not affect sweep level

**Symptom:** Changing AVR volume has no effect on measurement SNR in USB mode.
`calibrate_level` says to turn up the knob but nothing changes.

**Root cause:** USB sweep route is Pi → miniDSP USB directly. The Denon AVR is
not in the signal path. AVR volume only affects HDMI/Analog inputs.

**Fix:** To adjust sweep level in USB mode, change miniDSP output gain or the
sub's physical volume knob. `calibrate_level` in USB mode correctly reports this.

---

## 9. PyTTa sweep amplitude is full-scale (0 dBFS)

PyTTa `generate.sweep()` produces a signal normalized to ±1.0 (0 dBFS peak).
`PlayRecMeasure` applies `outputLinearGain = 10^(outputAmplification_dB/20)`,
defaulting to 1.0 (0 dB gain → full scale output).

For a nearfield sub positioned close to MLP, full-scale sweep can produce very
high SPL at the mic, approaching UMIK ADC clipping (>130 dBSPL). If needed,
reduce miniDSP output gain before measuring (Phase 0 level check), or use
`set_output_gain()` to attenuate temporarily.

---

## 10. Recording buffer must account for pre-delay

**Symptom:** SNR inverts — MORE gain → WORSE SNR. At -15 dB SNR is 7.6 dB, at -7 dB
SNR collapses to 1.4 dB.

**Root cause:** Recording buffer sized to `n_samples` (sweep length = 3s). With a
700ms pre-delay before playback, recording fills up at 3s from start — only 2.3s of
the 3s sweep is captured. Cross-correlation runs on a truncated signal. At higher
gains, truncation plus potential sub distortion causes the cross-correlation to land
at the wrong lag → peak_window lands in silence → signal_rms ≈ floor_rms → SNR ≈ 0 dB.

**Fix:** Size recording buffer to `pre_samples + n_samples + post_samples`:
```python
pre_samples = int(PRE_DELAY_S * sample_rate)   # 1.0s → 48000
post_samples = int(POST_DELAY_S * sample_rate)  # 0.5s → 24000
rec_n = pre_samples + n_samples + post_samples  # floor + full sweep + tail
rec_buf = np.zeros((rec_n, 1), dtype=np.float32)
```
Also append post-silence to the output buffer so the full sweep response
(including room reverb tail) is captured before the input stream stops.

**Rule:** Recording buffer capacity must always be > n_sweep_samples when a pre-delay
is used. `rec_n = pre + n_sweep + post` is the correct sizing.

---

## 11. SNR check must use cross-correlation lag, not np.argmax(abs(recording))

**Symptom:** SNR reported as ~0 dB even though the direct audio path test shows 40+ dB
SNR. Sweep is clearly being detected (cross-correlation passes). Measurement reports
"SNR 0.5 dB < 15.0 dB threshold."

**Root cause:** `validate_recording` used `peak_idx = np.argmax(np.abs(rec_array))` to
find where the sweep is in the recording. When the UMIK clips (many samples exactly at
1.0 dBFS) or a room transient fires at the start of the recording, `argmax` returns the
first sample at maximum value — which may be in the floor window (first 500ms). This
puts the signal window in the floor region: `signal_rms ≈ floor_rms` → SNR ≈ 0 dB.

**Fix:** Use the cross-correlation lag from Check 2 (already computed) to locate the
sweep in the recording. The lag `corr[:n].argmax()` is the circular delay index where
the sweep starts in the recording — always correct even when the signal is clipped.

```python
lag_idx = int(np.argmax(np.abs(corr[:n])))  # from FFT cross-correlation
sig_start = max(0, lag_idx)
sig_end = min(len(rec_array), lag_idx + n)
peak_window = rec_array[sig_start:sig_end]
signal_rms = np.sqrt(np.mean(peak_window**2))
```

**Rule:** Never use `argmax(abs(rec))` to locate a sweep in a noisy or potentially
clipping recording. Always use the cross-correlation lag.

---

## 12. MinidspSweepContext must NOT swallow setup exceptions

**Symptom:** Measurement reports SNR ~0 dB despite hardware path being functional.
Direct audio tests pass but `measure()` fails. Issue is non-deterministic — sometimes
works, sometimes fails.

**Root cause:** `MinidspSweepContext.__aenter__` wrapped setup in `try/except` that
swallowed exceptions and logged only a warning. If any CLI call in setup fails
(routing, source, mutes), the sweep plays with whatever routing happened to be in
hardware, giving garbage SNR data.

**Fix:** Remove the try/except from `__aenter__`. Let exceptions propagate — a failed
measurement setup should raise and be visible, not silently produce a bad measurement.

**Rule:** Context manager `__aenter__` must fail loudly. Silent failures in setup are
worse than explicit errors because they produce data that looks valid but is wrong.

---

## Diagnostic checklist for "SNR too low" failures

1. **Check routing:** Is input 0 (USB left) routed to the target output?
   Run `_configure_routing_via_cli(0, {1,2,3})` manually and check output_levels
   during playback. If output stays at -131 dBFS, routing is wrong.

2. **Check mutes:** Is the target output unmuted? `output_levels` shows -120 dBFS
   for muted outputs (with no signal), -130 to -150 dBFS for unmuted+silent.

3. **Check pre-delay:** Is there 700ms of silence at the start of the recording?
   Look at the USBPlayback debug log: `floor=X dBFS`. If floor is elevated
   (e.g., -10 dBFS), the sweep is contaminating the floor window.

4. **Check level:** Is the sub clipping the UMIK? `peak=0.0 dBFS` in USBPlayback
   log indicates clipping. Reduce DSP output gain by 10-15 dB and retry.

5. **Check source:** Is miniDSP source set to USB? If source is Analog during
   a USB sweep, no signal reaches the sub.

6. **Check if sub is playing:** Monitor `output_levels` during sweep. If output
   stays at noise floor (-130 to -150 dBFS) during playback, signal isn't reaching it.
