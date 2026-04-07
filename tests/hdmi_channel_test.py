#!/usr/bin/env python3
"""Test which HDMI channel routes to the Denon sub pre-out (LFE).

Plays a 60Hz tone on each HDMI channel (1-6) for 2 seconds while recording
from the UMIK mic. Reports the RMS level for each channel.

Uses the same device selection as production measurement code:
- Output: ALSA "hdmi" plugin device (not raw hw:0,0)
- Input: UMIK mic
"""

import sys
import time

import numpy as np
import sounddevice as sd


FREQ_HZ = 60
DURATION_S = 2.0
SAMPLE_RATE = 48000
AMPLITUDE = 0.5


def find_device(keyword: str, kind: str) -> int | None:
    devices = sd.query_devices()
    for i, dev in enumerate(devices):
        if keyword.lower() in dev["name"].lower():
            if kind == "output" and dev["max_output_channels"] > 0:
                return i
            if kind == "input" and dev["max_input_channels"] > 0:
                return i
    return None


def generate_tone(freq: float, duration: float, sr: int) -> np.ndarray:
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    return (AMPLITUDE * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def test_channel(ch_num: int, n_channels: int, in_dev: int, out_dev: int) -> float:
    """Play tone on one HDMI channel, record from UMIK, return RMS in dB."""
    tone = generate_tone(FREQ_HZ, DURATION_S, SAMPLE_RATE)
    n_samples = len(tone)

    hdmi_buf = np.zeros((n_samples, n_channels), dtype=np.int16)
    hdmi_buf[:, ch_num - 1] = (np.clip(tone, -1.0, 1.0) * 32767).astype(np.int16)

    rec_data = np.zeros((n_samples, 1), dtype=np.float32)
    rec_pos = [0]

    def _rec_callback(indata, frames, time_info, status):
        end = min(rec_pos[0] + frames, n_samples)
        count = end - rec_pos[0]
        rec_data[rec_pos[0]:end] = indata[:count]
        rec_pos[0] = end

    in_stream = sd.InputStream(
        device=in_dev, samplerate=SAMPLE_RATE,
        channels=1, dtype="float32", callback=_rec_callback,
    )
    out_stream = sd.OutputStream(
        device=out_dev, samplerate=SAMPLE_RATE,
        channels=n_channels, dtype="int16",
    )

    in_stream.start()
    out_stream.start()
    out_stream.write(hdmi_buf)
    out_stream.stop()
    time.sleep(0.5)
    in_stream.stop()
    in_stream.close()
    out_stream.close()

    skip = int(0.3 * SAMPLE_RATE)
    signal = rec_data[skip:rec_pos[0], 0]
    if len(signal) == 0:
        return -100.0
    rms = np.sqrt(np.mean(signal ** 2))
    db = 20 * np.log10(rms + 1e-10)
    return db


def main():
    print("=== HDMI Channel LFE Test ===")
    print(f"Tone: {FREQ_HZ} Hz, {DURATION_S}s, amplitude {AMPLITUDE}")
    print()

    # Find devices matching production code
    # Input: UMIK mic
    umik_dev = find_device("umik", "input")
    if umik_dev is None:
        print("ERROR: No UMIK input device found")
        sys.exit(1)

    # Output: ALSA "hdmi" plugin (not raw vc4-hdmi hardware)
    # Production code prefers exact name match "hdmi", shorter names first
    hdmi_dev = None
    devices = sd.query_devices()
    candidates = [
        (i, d) for i, d in enumerate(devices)
        if d["max_output_channels"] > 0 and "hdmi" in d["name"].lower()
    ]
    candidates.sort(key=lambda x: (x[1]["name"].lower() != "hdmi", len(x[1]["name"])))
    if candidates:
        hdmi_dev = candidates[0][0]

    if hdmi_dev is None:
        print("ERROR: No HDMI output device found")
        sys.exit(1)

    hdmi_info = sd.query_devices(hdmi_dev)
    umik_info = sd.query_devices(umik_dev)
    n_channels = min(int(hdmi_info["max_output_channels"]), 6)

    print(f"UMIK input:  [{umik_dev}] {umik_info['name']}")
    print(f"HDMI output: [{hdmi_dev}] {hdmi_info['name']} ({n_channels} ch)")
    print()

    # Labels — ALSA channel order for 5.1 may vary
    labels = {1: "FL", 2: "FR", 3: "FC/LFE?", 4: "LFE/FC?", 5: "RL", 6: "RR"}

    results = {}
    for ch in range(1, n_channels + 1):
        label = labels.get(ch, f"Ch{ch}")
        print(f"Testing channel {ch} ({label})...", end=" ", flush=True)
        try:
            db = test_channel(ch, n_channels, umik_dev, hdmi_dev)
            results[ch] = db
            print(f"{db:.1f} dB RMS")
        except Exception as e:
            print(f"ERROR: {e}")
            results[ch] = None
        time.sleep(1.0)

    print()
    print("=== Results ===")
    for ch in sorted(results):
        label = labels.get(ch, f"Ch{ch}")
        db = results[ch]
        if db is not None:
            marker = " <<< SIGNAL" if db > -40 else ""
            print(f"  Channel {ch} ({label:>8s}): {db:7.1f} dB{marker}")
        else:
            print(f"  Channel {ch} ({label:>8s}): ERROR")

    print()
    loud = [ch for ch, db in results.items() if db is not None and db > -40]
    quiet = [ch for ch, db in results.items() if db is not None and db <= -40]
    if loud:
        print(f"Channels with signal (> -40 dB): {loud}")
    if quiet:
        print(f"Channels silent (<= -40 dB): {quiet}")


if __name__ == "__main__":
    main()
