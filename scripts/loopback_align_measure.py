"""Sub-vs-mains acoustic alignment measurement via dual loopback.

Plays a log sweep through HDMI to the Denon, simultaneously captures:
  - mic on UMIK (device 4)
  - FL pre-out loopback on Focusrite input 9  (channel index 8)
  - SW1 pre-out loopback on Focusrite input 3 (channel index 2)

Cross-correlates mic against each loopback to find acoustic time-of-arrival.
The difference is the sub-vs-mains alignment gap, with clock-offset between
UMIK and Focusrite USB devices algebraically cancelled.
"""
from __future__ import annotations
import sys, time
import numpy as np
import sounddevice as sd

SR = 48000
SWEEP_DUR = 5.0
CAPTURE_PRE = 0.5
CAPTURE_POST = 1.5
F0 = 30.0
F1 = 2000.0

HDMI_DEV = 5         # 'hdmi' alias with plug conversion
UMIK_DEV = 3         # UMIK shifted up since Focusrite no longer enumerated directly
LOOPBACK_DEV = 2     # Loopback hw:2,1 — read side; ALSA picks next free subdevice
FL_LOOPBACK_CH = 8   # input 9 (0-indexed) on Focusrite → ch8 of bridge
SW_LOOPBACK_CH = 2   # input 3 (0-indexed) on Focusrite → ch2 of bridge


def make_log_sweep(dur_s, f0, f1, sr):
    n = int(dur_s * sr)
    t = np.arange(n) / sr
    K = dur_s * f0 / np.log(f1 / f0)
    L = dur_s / np.log(f1 / f0)
    sweep = np.sin(2 * np.pi * K * (np.exp(t / L) - 1))
    fade_n = int(0.05 * sr)
    fade = np.hanning(2 * fade_n)
    sweep[:fade_n] *= fade[:fade_n]
    sweep[-fade_n:] *= fade[-fade_n:]
    return sweep.astype(np.float32)


def xcorr_peak_ms(ref, sig, sr=SR, max_lag_ms=300):
    n_ref = len(ref); n_sig = len(sig)
    max_lag = int(max_lag_ms * sr / 1000)
    ref_n = (ref - np.mean(ref)) / (np.std(ref) + 1e-12)
    sig_n = (sig - np.mean(sig)) / (np.std(sig) + 1e-12)
    n_fft = 1 << int(np.ceil(np.log2(n_ref + n_sig - 1)))
    R = np.fft.rfft(ref_n, n=n_fft)
    S = np.fft.rfft(sig_n, n=n_fft)
    xc = np.fft.irfft(np.conj(R) * S, n=n_fft)
    xc_window = xc[:max_lag]
    peak_idx = int(np.argmax(np.abs(xc_window)))
    peak_ms = peak_idx * 1000.0 / sr
    peak_val = float(xc_window[peak_idx])
    return peak_ms, peak_val


def main():
    sweep = make_log_sweep(SWEEP_DUR, F0, F1, SR)
    capture_dur = CAPTURE_PRE + SWEEP_DUR + CAPTURE_POST
    capture_n = int(capture_dur * SR)
    play_buf = np.column_stack([sweep, sweep])

    mic_buf = np.zeros(capture_n, dtype=np.float32)
    fr_buf = np.zeros((capture_n, 20), dtype=np.float32)
    mic_idx = [0]; fr_idx = [0]

    def mic_cb(indata, frames, time_info, status):
        if status: print(f"  mic: {status}", file=sys.stderr)
        n = min(frames, capture_n - mic_idx[0])
        if n <= 0: return
        mic_buf[mic_idx[0]:mic_idx[0]+n] = indata[:n, 0]
        mic_idx[0] += n

    def fr_cb(indata, frames, time_info, status):
        if status: print(f"  fr: {status}", file=sys.stderr)
        n = min(frames, capture_n - fr_idx[0])
        if n <= 0: return
        fr_buf[fr_idx[0]:fr_idx[0]+n, :] = indata[:n, :]
        fr_idx[0] += n

    print(f"opening streams (sweep {SWEEP_DUR}s, capture {capture_dur}s)...")
    mic_stream = sd.InputStream(device=UMIK_DEV, channels=1, samplerate=SR,
                                 dtype='float32', blocksize=1024, callback=mic_cb)
    fr_stream = sd.InputStream(device=LOOPBACK_DEV, channels=20, samplerate=SR,
                                dtype='float32', blocksize=1024, callback=fr_cb)
    mic_stream.start(); fr_stream.start()
    time.sleep(CAPTURE_PRE)
    print(f"playing sweep on HDMI device {HDMI_DEV}...")
    sd.play(play_buf, samplerate=SR, device=HDMI_DEV, blocking=True)
    time.sleep(CAPTURE_POST)
    mic_stream.stop(); fr_stream.stop()
    mic_stream.close(); fr_stream.close()

    fl_loopback = fr_buf[:fr_idx[0], FL_LOOPBACK_CH]
    sw_loopback = fr_buf[:fr_idx[0], SW_LOOPBACK_CH]
    mic = mic_buf[:mic_idx[0]]

    print(f"  RMS: mic={np.sqrt(np.mean(mic**2)):.4f}  "
          f"FL_loop={np.sqrt(np.mean(fl_loopback**2)):.4f}  "
          f"SW_loop={np.sqrt(np.mean(sw_loopback**2)):.4f}")
    print("  All Focusrite channels RMS (input # = ch+1):")
    for ch in range(20):
        rms = np.sqrt(np.mean(fr_buf[:fr_idx[0], ch]**2))
        if rms > 0.00001:
            print(f"    ch{ch} (input {ch+1}): {rms:.5f}")

    print("\n=== cross-correlations ===")
    t_fl_ms, fl_v = xcorr_peak_ms(fl_loopback, mic)
    print(f"FL_loopback → mic: {t_fl_ms:.3f} ms (peak {fl_v:.3g})")
    t_sw_ms, sw_v = xcorr_peak_ms(sw_loopback, mic)
    print(f"SW_loopback → mic: {t_sw_ms:.3f} ms (peak {sw_v:.3g})")
    gap_ms = t_sw_ms - t_fl_ms
    print(f"\n=== sub-vs-mains gap: {gap_ms:+.3f} ms ===")
    print(f"    (positive = subs arrive LATER than mains at MLP)")


if __name__ == "__main__":
    main()
