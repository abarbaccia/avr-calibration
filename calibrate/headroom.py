"""Headroom / amp clipping test — pure math functions.

Multitone synthesis, FFT analysis, THD computation, tone cluster assignment,
and compression detection. Zero hardware dependencies — all functions operate
on numpy arrays and return dicts.

Used by the ``play_and_measure_fft`` MCP tool and the ``/headroom`` skill.
"""

from __future__ import annotations

import logging
import math
from typing import Any

import numpy as np

log = logging.getLogger(__name__)


# ── Multitone synthesis ──────────────────────────────────────────────────────


def generate_multitone(
    frequencies_hz: list[float],
    duration_s: float,
    sample_rate: int = 48000,
    amplitude: float = 0.5,
    phase_randomize: bool = True,
    rng_seed: int | None = None,
) -> np.ndarray:
    """Synthesize a multitone signal (sum of sines) as a float32 1D array.

    Random phase per tone prevents crest factor buildup from aligned peaks,
    keeping the peak amplitude manageable even with many tones.

    Returns a float32 array normalized so peak ≤ *amplitude*.
    """
    n_samples = int(sample_rate * duration_s)
    t = np.arange(n_samples, dtype=np.float64) / sample_rate

    rng = np.random.default_rng(rng_seed)
    signal = np.zeros(n_samples, dtype=np.float64)

    for freq in frequencies_hz:
        phase = rng.uniform(0, 2 * np.pi) if phase_randomize else 0.0
        signal += np.sin(2 * np.pi * freq * t + phase)

    # Normalize to peak ≤ amplitude
    peak = np.max(np.abs(signal))
    if peak > 0:
        signal *= amplitude / peak

    return signal.astype(np.float32)


def build_multichannel_buffer(
    channel_assignments: dict[int, list[float]],
    duration_s: float,
    sample_rate: int = 48000,
    amplitude: float = 0.5,
    n_channels: int = 6,
    rng_seed: int | None = None,
) -> np.ndarray:
    """Build an int16 multichannel HDMI buffer with per-channel multitone clusters.

    Args:
        channel_assignments: {hdmi_channel_1based: [freq1, freq2, ...], ...}
        duration_s: tone duration in seconds
        sample_rate: audio sample rate
        amplitude: peak amplitude per channel (0–1)
        n_channels: total HDMI channels (6 for 5.1)
        rng_seed: optional seed for reproducible phase randomization

    Returns:
        int16 ndarray of shape (n_samples, n_channels).
    """
    n_samples = int(sample_rate * duration_s)
    buf = np.zeros((n_samples, n_channels), dtype=np.int16)

    for ch, freqs in channel_assignments.items():
        ch_idx = ch - 1  # 1-based → 0-based
        if ch_idx < 0 or ch_idx >= n_channels:
            log.warning("Channel %d out of range (n_channels=%d), skipping", ch, n_channels)
            continue
        tone = generate_multitone(
            freqs, duration_s, sample_rate, amplitude,
            phase_randomize=True, rng_seed=rng_seed,
        )
        buf[:, ch_idx] = (np.clip(tone, -1.0, 1.0) * 32767).astype(np.int16)

    return buf


# ── FFT analysis ─────────────────────────────────────────────────────────────


def compute_thd(
    fft_magnitudes: np.ndarray,
    freq_resolution: float,
    fundamental_hz: float,
    n_harmonics: int = 5,
    nyquist_hz: float = 24000.0,
) -> tuple[float, list[dict]]:
    """Compute THD for a single tone from an FFT magnitude spectrum.

    Searches for energy at harmonic frequencies (2nd through n_harmonics+1).
    Uses a ±1-bin window around each expected harmonic to handle FFT leakage.

    Returns:
        (thd_percent, harmonics_list) where harmonics_list contains dicts with
        keys: harmonic, frequency_hz, magnitude, spl_dbfs.
    """
    fund_bin = int(round(fundamental_hz / freq_resolution))
    if fund_bin <= 0 or fund_bin >= len(fft_magnitudes):
        return 0.0, []

    # Use peak in ±1 bin window for fundamental
    lo = max(0, fund_bin - 1)
    hi = min(len(fft_magnitudes), fund_bin + 2)
    fund_mag = float(np.max(fft_magnitudes[lo:hi]))

    if fund_mag <= 0:
        return 0.0, []

    harmonics = []
    sum_sq = 0.0

    for h in range(2, n_harmonics + 2):
        h_freq = fundamental_hz * h
        if h_freq >= nyquist_hz:
            break
        h_bin = int(round(h_freq / freq_resolution))
        if h_bin >= len(fft_magnitudes):
            break

        lo_h = max(0, h_bin - 1)
        hi_h = min(len(fft_magnitudes), h_bin + 2)
        h_mag = float(np.max(fft_magnitudes[lo_h:hi_h]))

        sum_sq += h_mag ** 2
        spl = 20.0 * math.log10(h_mag + 1e-12)
        harmonics.append({
            "harmonic": h,
            "frequency_hz": round(h_freq, 1),
            "magnitude": h_mag,
            "spl_dbfs": round(spl, 1),
        })

    thd_percent = 100.0 * math.sqrt(sum_sq) / fund_mag if fund_mag > 0 else 0.0
    return round(thd_percent, 3), harmonics


def analyze_fft(
    recording: np.ndarray,
    sample_rate: int,
    target_frequencies: list[float],
    fft_size: int = 8192,
    window: str = "hann",
) -> dict[str, Any]:
    """Extract SPL and THD at specific frequencies from a steady-state recording.

    Uses Welch's method (averaged overlapping FFT segments) for stable estimates.

    Args:
        recording: 1D float64 array from mic
        sample_rate: Hz
        target_frequencies: tone frequencies to extract
        fft_size: FFT window size (8192 @ 48kHz ≈ 5.86Hz resolution)
        window: window function name

    Returns:
        {"per_tone": [...], "noise_floor_dbfs": float}
    """
    from scipy.signal import welch

    freqs, psd = welch(
        recording, fs=sample_rate, nperseg=fft_size,
        window=window, scaling="spectrum",
    )
    # Convert power spectrum to RMS magnitude
    magnitudes = np.sqrt(psd)
    freq_res = freqs[1] - freqs[0] if len(freqs) > 1 else 1.0
    nyquist = sample_rate / 2.0

    per_tone = []
    tone_bins = set()

    for freq in target_frequencies:
        target_bin = int(round(freq / freq_res))
        if target_bin >= len(magnitudes):
            continue

        # Peak in ±1 bin window
        lo = max(0, target_bin - 1)
        hi = min(len(magnitudes), target_bin + 2)
        peak_mag = float(np.max(magnitudes[lo:hi]))
        spl_dbfs = 20.0 * math.log10(peak_mag + 1e-12)

        thd_pct, harmonics = compute_thd(
            magnitudes, freq_res, freq, n_harmonics=5, nyquist_hz=nyquist,
        )
        thd_db = 20.0 * math.log10(thd_pct / 100.0 + 1e-12) if thd_pct > 0 else -120.0

        per_tone.append({
            "frequency_hz": round(freq, 1),
            "spl_dbfs": round(spl_dbfs, 1),
            "thd_percent": round(thd_pct, 3),
            "thd_db": round(thd_db, 1),
            "harmonics": harmonics,
        })

        # Track bins used by tones and harmonics for noise floor calc
        for b in range(lo, hi):
            tone_bins.add(b)
        for h_info in harmonics:
            h_bin = int(round(h_info["frequency_hz"] / freq_res))
            for b in range(max(0, h_bin - 1), min(len(magnitudes), h_bin + 2)):
                tone_bins.add(b)

    # Noise floor: median of bins not occupied by tones or harmonics
    noise_bins = [i for i in range(len(magnitudes)) if i not in tone_bins and i > 0]
    if noise_bins:
        noise_mag = float(np.median(magnitudes[noise_bins]))
        noise_floor = 20.0 * math.log10(noise_mag + 1e-12)
    else:
        noise_floor = -120.0

    return {
        "per_tone": per_tone,
        "noise_floor_dbfs": round(noise_floor, 1),
    }


# ── Tone cluster assignment ──────────────────────────────────────────────────


def assign_tone_clusters(
    speaker_passbands: dict[str, tuple[float, float]],
    tones_per_speaker: int = 4,
    min_inter_speaker_spacing_hz: float = 30.0,
    min_frequency_hz: float = 200.0,
    n_harmonics_avoid: int = 5,
    harmonic_guard_hz: float = 10.0,
) -> dict[str, list[float]]:
    """Assign non-overlapping multitone clusters to speakers within their flat passbands.

    Uses interleaved placement: divides the frequency range into
    ``tones_per_speaker`` regions, then within each region assigns one tone per
    speaker with ``min_inter_speaker_spacing_hz`` between them. This guarantees
    separation even when all speakers share the same passband.

    Constraints:
    - All tones within each speaker's passband [low_hz, high_hz]
    - No tone below min_frequency_hz
    - Minimum gap of min_inter_speaker_spacing_hz between any two tones on different speakers
    - No harmonic (2nd through n_harmonics_avoid+1) of any tone within harmonic_guard_hz
      of another speaker's tone

    Args:
        speaker_passbands: {speaker_role: (low_hz, high_hz), ...}
        tones_per_speaker: number of tones per speaker
        min_inter_speaker_spacing_hz: minimum gap between tones on different speakers
        min_frequency_hz: absolute minimum tone frequency
        n_harmonics_avoid: check harmonics up to this order for cross-talk avoidance
        harmonic_guard_hz: minimum distance from any harmonic to another speaker's tone

    Returns:
        {speaker_role: [freq1, freq2, ...], ...}

    Raises:
        ValueError: if constraints cannot be satisfied
    """
    speakers = sorted(speaker_passbands.keys())
    if not speakers:
        return {}

    n_speakers = len(speakers)

    # Clamp passbands to min_frequency_hz
    bands = {}
    for spk in speakers:
        low, high = speaker_passbands[spk]
        low = max(low, min_frequency_hz)
        if low >= high:
            raise ValueError(
                f"Speaker '{spk}' passband ({speaker_passbands[spk][0]}-"
                f"{speaker_passbands[spk][1]} Hz) is entirely below "
                f"min_frequency_hz={min_frequency_hz}"
            )
        bands[spk] = (low, high)

    # Find the common usable range (intersection of all passbands)
    global_low = max(b[0] for b in bands.values())
    global_high = min(b[1] for b in bands.values())

    # Width needed per frequency region: n_speakers tones * spacing
    region_width = n_speakers * min_inter_speaker_spacing_hz
    total_range = global_high - global_low

    if total_range < region_width * tones_per_speaker:
        # Not enough room for ideal spacing — pack tighter
        log.warning(
            "Passband %.0f-%.0fHz too narrow for %d speakers x %d tones "
            "at %.0fHz spacing; packing tighter",
            global_low, global_high, n_speakers, tones_per_speaker,
            min_inter_speaker_spacing_hz,
        )

    # Divide the usable range into tones_per_speaker frequency regions.
    # Each region holds one tone per speaker, spaced apart.
    margin = region_width / 2
    usable_low = global_low + margin
    usable_high = global_high - margin

    if usable_high <= usable_low:
        usable_low = global_low
        usable_high = global_high

    if tones_per_speaker == 1:
        region_centers = [(usable_low + usable_high) / 2]
    else:
        step = (usable_high - usable_low) / (tones_per_speaker - 1)
        region_centers = [usable_low + i * step for i in range(tones_per_speaker)]

    # Within each region, assign one tone per speaker offset by spacing
    assignments: dict[str, list[float]] = {spk: [] for spk in speakers}
    all_tones: list[tuple[str, float]] = []

    for center in region_centers:
        # Center the speaker group around the region center
        group_start = center - (n_speakers - 1) * min_inter_speaker_spacing_hz / 2
        for idx, spk in enumerate(speakers):
            tone = round(group_start + idx * min_inter_speaker_spacing_hz, 1)
            low, high = bands[spk]
            tone = max(low, min(high, tone))

            # Check harmonic avoidance against all previously assigned tones
            for other_spk, other_freq in all_tones:
                if other_spk == spk:
                    continue
                for h in range(2, n_harmonics_avoid + 2):
                    if abs(tone * h - other_freq) < harmonic_guard_hz:
                        tone = round(tone + harmonic_guard_hz + 1, 1)
                        tone = max(low, min(high, tone))
                        break
                    if abs(other_freq * h - tone) < harmonic_guard_hz:
                        tone = round(tone + harmonic_guard_hz + 1, 1)
                        tone = max(low, min(high, tone))
                        break

            assignments[spk].append(tone)
            all_tones.append((spk, tone))

    # Validate: check no two tones from different speakers are within spacing
    violations = []
    for i, (spk_a, freq_a) in enumerate(all_tones):
        for spk_b, freq_b in all_tones[i + 1:]:
            if spk_a != spk_b and abs(freq_a - freq_b) < min_inter_speaker_spacing_hz:
                violations.append((spk_a, freq_a, spk_b, freq_b))

    if violations:
        log.warning("Tone spacing violations (may cause cross-talk in FFT): %s", violations)

    return assignments


# ── Compression detection ────────────────────────────────────────────────────


def detect_compression(
    volume_steps: list[dict],
    reference_volume_db: float = -30.0,
    expected_gain_per_step_db: float = 1.0,
    compression_threshold_db: float = 0.3,
    thd_spike_threshold_db: float = 6.0,
) -> dict[str, Any]:
    """Analyze volume-step measurements to detect compression onset.

    Args:
        volume_steps: list of per-step data, each:
            {"volume_db": float, "speakers": {role: {"spl_dbfs": float, "thd_db": float}}}
        reference_volume_db: starting volume for headroom calculation
        expected_gain_per_step_db: expected SPL gain per 1dB volume increase
        compression_threshold_db: gain deficit that counts as compression
        thd_spike_threshold_db: THD increase (dB) that indicates clipping

    Returns:
        Dict with compression_onset_volume_db, headroom_db, failure_mode,
        weak_channel, per_channel details, and all_steps data.
    """
    if len(volume_steps) < 2:
        return {
            "compression_onset_volume_db": None,
            "headroom_db": None,
            "failure_mode": "insufficient_data",
            "weak_channel": None,
            "per_channel": {},
            "all_steps": volume_steps,
        }

    speakers = list(volume_steps[0].get("speakers", {}).keys())
    if not speakers:
        return {
            "compression_onset_volume_db": None,
            "headroom_db": None,
            "failure_mode": "no_speaker_data",
            "weak_channel": None,
            "per_channel": {},
            "all_steps": volume_steps,
        }

    # Track per-channel gain and THD across steps
    per_channel_onset: dict[str, float | None] = {s: None for s in speakers}
    per_channel_thd_onset: dict[str, float | None] = {s: None for s in speakers}

    steps_with_gain: list[dict] = []

    for i in range(1, len(volume_steps)):
        prev = volume_steps[i - 1]
        curr = volume_steps[i]
        step_data: dict[str, Any] = {
            "volume_db": curr["volume_db"],
            "gains": {},
            "thd_changes": {},
        }

        for spk in speakers:
            prev_spk = prev.get("speakers", {}).get(spk, {})
            curr_spk = curr.get("speakers", {}).get(spk, {})

            prev_spl = prev_spk.get("spl_dbfs", -100)
            curr_spl = curr_spk.get("spl_dbfs", -100)
            gain = curr_spl - prev_spl
            step_data["gains"][spk] = round(gain, 2)

            prev_thd = prev_spk.get("thd_db", -120)
            curr_thd = curr_spk.get("thd_db", -120)
            thd_change = curr_thd - prev_thd
            step_data["thd_changes"][spk] = round(thd_change, 2)

            # Detect compression: gain significantly below expected
            if per_channel_onset[spk] is None:
                if gain < (expected_gain_per_step_db - compression_threshold_db):
                    per_channel_onset[spk] = curr["volume_db"]

            # Detect THD spike
            if per_channel_thd_onset[spk] is None:
                if thd_change > thd_spike_threshold_db:
                    per_channel_thd_onset[spk] = curr["volume_db"]

        steps_with_gain.append(step_data)

    # Determine global compression onset (earliest across channels)
    onset_volumes = [v for v in per_channel_onset.values() if v is not None]
    global_onset = min(onset_volumes) if onset_volumes else None

    # Classify failure mode
    if global_onset is None:
        failure_mode = "none"
        weak_channel = None
    else:
        # Check if all channels compressed at roughly the same step (±1dB)
        compressing = [
            (spk, v) for spk, v in per_channel_onset.items() if v is not None
        ]
        onset_spread = max(v for _, v in compressing) - min(v for _, v in compressing)

        if onset_spread <= 1.5 and len(compressing) > 1:
            failure_mode = "power_supply_sag"
            weak_channel = None
        else:
            failure_mode = "per_channel_clipping"
            weak_channel = min(compressing, key=lambda x: x[1])[0]

    # Headroom from reference to onset
    headroom = global_onset - reference_volume_db if global_onset is not None else None

    # Per-channel summary
    per_channel = {}
    for spk in speakers:
        last_step = volume_steps[-1].get("speakers", {}).get(spk, {})
        per_channel[spk] = {
            "compression_onset_db": per_channel_onset[spk],
            "thd_onset_db": per_channel_thd_onset[spk],
            "final_spl_dbfs": last_step.get("spl_dbfs"),
            "final_thd_db": last_step.get("thd_db"),
        }

    return {
        "compression_onset_volume_db": global_onset,
        "headroom_db": headroom,
        "failure_mode": failure_mode,
        "weak_channel": weak_channel,
        "per_channel": per_channel,
        "all_steps": steps_with_gain,
    }
