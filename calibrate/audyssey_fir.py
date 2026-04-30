"""Audyssey FIR design + polyphase decimation for direct-upload to Denon AVRs.

Generates the multi-rate FIR filter packets that the AVR's MultEQ engine
expects, bypassing the official MultEQ Editor app. Uploaded via the
SET_COEFDT TCP command path (see calibrate/drivers/denon/audyssey_tcp.py).

Filter sizes (XT32 family):
    speaker:  input 16,321 taps → output 1,024 taps (4-band polyphase)
    sub:      input 16,055 taps → output 704 taps   (4-band polyphase)

The polyphase math is ported with attribution from
``srinivas486/audyssey-rew-tuner`` (MIT-licensed, oca_transfer.py).
That project is itself a clean-room reimplementation of the
A1EvoAcoustica/transfer.js upload pipeline. Reverse-engineered protocol
details: github.com/srinivas486/audyssey-rew-tuner/SPEC.md.

This module only handles the math. The wire-protocol packetisation +
TCP upload sequence live in ``calibrate.drivers.denon.audyssey_tcp``.
"""
from __future__ import annotations

import math
from typing import Sequence

import numpy as np

# ── Channel byte table — maps Audyssey channel commandIds to the byte
#    value the AVR uses to identify the channel in SET_COEFDT packets.
#    XT32 uses the ``eq2`` column; XT and MultEQ use ``neq2``.
#    Source: oca_transfer.py:48-85 (MIT-licensed).
CHANNEL_BYTE_TABLE: dict[str, dict[str, int | None]] = {
    "FL":  {"eq2": 0x00, "neq2": 0x00},
    "C":   {"eq2": 0x01, "neq2": 0x01},
    "FR":  {"eq2": 0x02, "neq2": 0x02},
    "FWR": {"eq2": 0x15, "neq2": 0x15},
    "SRA": {"eq2": 0x03, "neq2": 0x03},
    "SRB": {"eq2": None, "neq2": 0x07},
    "SBR": {"eq2": 0x07, "neq2": 0x07},
    "SBL": {"eq2": 0x08, "neq2": 0x08},
    "SLB": {"eq2": None, "neq2": 0x0d},
    "SLA": {"eq2": 0x0c, "neq2": 0x0c},
    "FWL": {"eq2": 0x1c, "neq2": 0x1c},
    "FHL": {"eq2": 0x10, "neq2": 0x10},
    "CH":  {"eq2": 0x12, "neq2": 0x12},
    "FHR": {"eq2": 0x14, "neq2": 0x14},
    "TFR": {"eq2": 0x04, "neq2": 0x04},
    "TMR": {"eq2": 0x05, "neq2": 0x05},
    "TRR": {"eq2": 0x06, "neq2": 0x06},
    "SHR": {"eq2": 0x16, "neq2": 0x16},
    "RHR": {"eq2": 0x13, "neq2": 0x17},
    "TS":  {"eq2": 0x1d, "neq2": 0x1d},
    "RHL": {"eq2": 0x11, "neq2": 0x1a},
    "SHL": {"eq2": 0x1b, "neq2": 0x1b},
    "TRL": {"eq2": 0x09, "neq2": 0x09},
    "TML": {"eq2": 0x0a, "neq2": 0x0a},
    "TFL": {"eq2": 0x0b, "neq2": 0x0b},
    "FDL": {"eq2": 0x1a, "neq2": 0x1a},
    "FDR": {"eq2": 0x17, "neq2": 0x17},
    "SDR": {"eq2": 0x18, "neq2": 0x18},
    "SDL": {"eq2": 0x19, "neq2": 0x19},
    "SW1": {"eq2": 0x0d, "neq2": 0x0d},
    "SW2": {"eq2": 0x0e, "neq2": 0x0e},
    "SW3": {"eq2": 0x21, "neq2": 0x21},
    "SW4": {"eq2": 0x22, "neq2": 0x22},
    "LFE": {"eq2": 0x0d, "neq2": 0x0d},  # LFE maps to SW1
}


def get_channel_byte(command_id: str, mult_eq_type: str = "XT32") -> int:
    """Return the AVR's channel-byte for a given Audyssey channel commandId.

    Args:
        command_id: e.g. "FL", "SW1", "TRL".
        mult_eq_type: "XT32" (uses eq2 column) or "XT" / "MultEQ" (uses neq2).
    """
    entry = CHANNEL_BYTE_TABLE.get(command_id)
    if entry is None:
        raise ValueError(f"Unknown Audyssey channel commandId: {command_id!r}")
    column = "eq2" if mult_eq_type == "XT32" else "neq2"
    val = entry.get(column)
    if val is None:
        # Fall back to the other column if XT32 entry is missing.
        val = entry.get("neq2" if column == "eq2" else "eq2")
    if val is None:
        raise ValueError(
            f"No channel byte mapping for {command_id!r} under {mult_eq_type}"
        )
    return val


# ── Multi-sample-rate codes used in the SET_COEFDT header.
#    XT32 ships the same FIR three times, once per processing rate; the AVR
#    routes audio through whichever bank matches the current source rate.
SAMPLE_RATE_CODES: dict[int, str] = {
    32000: "00",
    44100: "01",
    48000: "02",
    96000: "b8",
}


# ── Polyphase decimation filters (4-band, decimation factor 4) ───────────
DECIMATION_FACTOR = 4

# 29-tap filter for sub band 1.
DEC_FILTER_XT32_SUB29_TAPS: tuple[float, ...] = (
    -0.0000068090826, -4.5359936e-8, 0.00010496614, 0.0005359394, 0.0017366897,
    0.0043950975, 0.00936928, 0.017480986, 0.029199528, 0.04430621,
    0.061674833, 0.07929655, 0.094606727, 0.1050576, 0.10877161,
    0.1050576, 0.094606727, 0.07929655, 0.061674833, 0.04430621,
    0.029199528, 0.017480986, 0.00936928, 0.0043950975, 0.0017366897,
    0.0005359394, 0.00010496614, -4.5359936e-8, -0.0000068090826,
)

# 37-tap filter for sub band 2.
DEC_FILTER_XT32_SUB37_TAPS: tuple[float, ...] = (
    -0.000026230078, -0.00013839548, -0.00045447858, -0.0011429883,
    -0.0023770225, -0.0042346125, -0.0065577077, -0.0088115167,
    -0.010010772, -0.008782894, -0.0036095164, 0.0067711435,
    0.02289046, 0.04414973, 0.06865209, 0.093375608, 0.11469775,
    0.12916237, 0.1342851, 0.12916237, 0.11469775, 0.093375608,
    0.06865209, 0.04414973, 0.02289046, 0.0067711435, -0.0036095164,
    -0.008782894, -0.010010772, -0.0088115167, -0.0065577077, -0.0042346125,
    -0.0023770225, -0.0011429883, -0.00045447858, -0.00013839548, -0.000026230078,
)

# 93-tap filter for sub band 3.
DEC_FILTER_XT32_SUB93_TAPS: tuple[float, ...] = (
    0.000004904671, 0.000016451735, 0.000035466823, 0.000054780343,
    0.000057436635, 0.000019883537, -0.00007663135, -0.00022867938,
    -0.0003953652, -0.0004970615, -0.00043803814, -0.00015296187,
    0.00033801072, 0.00089421676, 0.0012704487, 0.0011992522,
    0.0005233042, -0.00067407207, -0.0020127299, -0.0028939669,
    -0.0027228948, -0.0012104996, 0.0013740772, 0.004148222, 0.005850492,
    0.005338624, 0.0021824592, -0.0029139882, -0.0081179589, -0.011018342,
    -0.0096052159, -0.0033266835, 0.0062539442, 0.015607043, 0.020322932,
    0.016872915, 0.0044270838, -0.014038938, -0.031958703, -0.040876575,
    -0.033219177, -0.0052278917, 0.04104016, 0.097502038, 0.15189469,
    0.19119503, 0.20552149, 0.19119503, 0.15189469, 0.097502038,
    0.04104016, -0.0052278917, -0.033219177, -0.040876575, -0.031958703,
    -0.014038938, 0.0044270838, 0.016872915, 0.020322932, 0.015607043,
    0.0062539442, -0.0033266835, -0.0096052159, -0.011018342, -0.0081179589,
    -0.0029139882, 0.0021824592, 0.005338624, 0.005850492, 0.004148222,
    0.0013740772, -0.0012104996, -0.0027228948, -0.0028939669, -0.0020127299,
    -0.00067407207, 0.0005233042, 0.0011992522, 0.0012704487, 0.00089421676,
    0.00033801072, -0.00015296187, -0.00043803814, -0.0004970615, -0.0003953652,
    -0.00022867938, -0.00007663135, 0.000019883537, 0.000057436635,
    0.000054780343, 0.000035466823, 0.000016451735, 0.000004904671,
)

# 129-tap filter — used for all 3 speaker bands.
DEC_FILTER_XT32_SAT129_TAPS: tuple[float, ...] = (
    0.0000043782347, 0.000014723354, 0.000032770109, 0.000054528296,
    0.000068608439, 0.00005722275, 0.0000025561833, -0.0001022896,
    -0.00024198946, -0.0003741896, -0.0004376953, -0.00037544663,
    -0.00016613922, 0.00014951751, 0.00046477153, 0.000636138,
    0.0005427991, 0.00015503204, -0.0004217047, -0.00095836946,
    -0.0011810855, -0.00089615857, -0.00010969268, 0.0009218459,
    0.0017551293, 0.0019349628, 0.0012194271, -0.00024770317,
    -0.0019181528, -0.0030198381, -0.0028912309, -0.0013345525,
    0.0011865027, 0.0036375371, 0.0048077558, 0.0038727189,
    0.00087827817, -0.0031111876, -0.0063393954, -0.0070888256,
    -0.0045305756, 0.00070328976, 0.006557314, 0.010292898,
    0.009696761, 0.0042538098, -0.0042899773, -0.012354134,
    -0.01590999, -0.012335026, -0.0019397299, 0.0116079, 0.022352377,
    0.024387382, 0.014624386, -0.0051601734, -0.028005365, -0.043577183,
    -0.04166761, -0.016186262, 0.031879943, 0.09379751, 0.15517053,
    0.20020825, 0.21674114, 0.20020825, 0.15517053, 0.09379751,
    0.031879943, -0.016186262, -0.04166761, -0.043577183, -0.028005365,
    -0.0051601734, 0.014624386, 0.024387382, 0.022352377, 0.0116079,
    -0.0019397299, -0.012335026, -0.01590999, -0.012354134, -0.0042899773,
    0.0042538098, 0.009696761, 0.010292898, 0.006557314, 0.00070328976,
    -0.0045305756, -0.0070888256, -0.0063393954, -0.0031111876,
    0.00087827817, 0.0038727189, 0.0048077558, 0.0036375371, 0.0011865027,
    -0.0013345525, -0.0028912309, -0.0030198381, -0.0019181528,
    -0.00024770317, 0.0012194271, 0.0019349628, 0.0017551293, 0.0009218459,
    -0.00010969268, -0.00089615857, -0.0011810855, -0.00095836946,
    -0.0004217047, 0.00015503204, 0.0005427991, 0.000636138, 0.00046477153,
    0.00014951751, -0.00016613922, -0.00037544663, -0.0004376953,
    -0.0003741896, -0.00024198946, -0.0001022896, 0.0000025561833,
    0.00005722275, 0.000068608439, 0.000054528296, 0.000032770109,
    0.000014723354, 0.0000043782347,
)


def decompose_filter(filter_taps: Sequence[float], M: int) -> list[list[float]]:
    """Split ``filter_taps`` into ``M`` polyphase components.

    Component p contains taps at indices [p, p+M, p+2M, ...].
    """
    L = len(filter_taps)
    if M <= 0 or L == 0:
        return [[] for _ in range(M or 0)]
    return [list(filter_taps[p::M]) for p in range(M)]


# ── Pre-computed polyphase splits for each decimation filter — done once
#    at import so callers don't pay the cost on every FIR design.
_SUB29_PHASES = decompose_filter(DEC_FILTER_XT32_SUB29_TAPS, DECIMATION_FACTOR)
_SUB37_PHASES = decompose_filter(DEC_FILTER_XT32_SUB37_TAPS, DECIMATION_FACTOR)
_SUB93_PHASES = decompose_filter(DEC_FILTER_XT32_SUB93_TAPS, DECIMATION_FACTOR)
_SAT129_PHASES = decompose_filter(DEC_FILTER_XT32_SAT129_TAPS, DECIMATION_FACTOR)


FILTER_CONFIGS: dict[str, dict] = {
    "xt32Sub": {
        "description": "MultEQ XT32 Subwoofer",
        "input_length": 0x3EB7,    # 16,055
        "output_length": 0x2C0,    # 704
        "band_lengths": [0x60, 0x60, 0x100, 0xEF],   # [96, 96, 256, 239]
        "dec_filters_info": [
            {"phases": _SUB29_PHASES, "original_length": 29},
            {"phases": _SUB37_PHASES, "original_length": 37},
            {"phases": _SUB93_PHASES, "original_length": 93},
        ],
        "delay_comp": [True, True, True],
    },
    "xt32Speaker": {
        "description": "MultEQ XT32 Speaker",
        "input_length": 0x3FC1,    # 16,321
        "output_length": 0x400,    # 1024
        "band_lengths": [0x100, 0x100, 0x100, 0xEB],  # [256, 256, 256, 235]
        "dec_filters_info": [
            {"phases": _SAT129_PHASES, "original_length": 129},
            {"phases": _SAT129_PHASES, "original_length": 129},
            {"phases": _SAT129_PHASES, "original_length": 129},
        ],
        "delay_comp": [True, True, True],
    },
}


def is_sub_channel(command_id: str) -> bool:
    """True for any sub channel (SW1-4 or LFE)."""
    return command_id.startswith("SW") or command_id == "LFE"


def filter_config_for(command_id: str) -> dict:
    """Pick the right FILTER_CONFIGS entry for an XT32 channel."""
    return FILTER_CONFIGS["xt32Sub" if is_sub_channel(command_id) else "xt32Speaker"]


# ── Polyphase decimation kernel ──────────────────────────────────────────


def polyphase_decimate(
    signal: Sequence[float],
    phases: Sequence[Sequence[float]],
    M: int,
    original_filter_length: int,
) -> list[float]:
    """Decimate ``signal`` by factor ``M`` using its precomputed polyphase
    components ``phases``.

    Equivalent to convolving with the original filter then taking every
    Mth output sample, but does the work at the lower output rate.

    Ported from oca_transfer.py:polyphase_decimate.
    """
    signal_len = len(signal)
    L = original_filter_length
    if signal_len == 0 or L == 0 or M <= 0 or len(phases) != M:
        return []

    sig = np.asarray(signal, dtype=np.float64)
    convolved_length = signal_len + L - 1
    output_len = (convolved_length + M - 1) // M
    if output_len <= 0:
        return []

    output = np.zeros(output_len, dtype=np.float64)
    for k in range(output_len):
        y_k = 0.0
        for p in range(M):
            phase_p = phases[p]
            for i, tap in enumerate(phase_p):
                in_index = k * M + p - i * M
                if 0 <= in_index < signal_len:
                    y_k += tap * sig[in_index]
        output[k] = y_k
    return output.tolist()


def generate_window(length: int) -> list[float]:
    """Generate a Hann-style window for band processing.

    Ported from oca_transfer.py:generate_window (window_type=1).
    """
    if length <= 0:
        return []
    a, b, c = 0.5, 0.5, 0.0
    factor = 1.0 / (length - 1 if length > 1 else 1)
    pi2 = 2.0 * math.pi
    pi4 = 4.0 * math.pi
    window = [0.0] * length
    for i in range(length):
        t = i * factor
        window[i] = a - b * math.cos(pi2 * t) + c * math.cos(pi4 * t)
    return window


def _calculate_band(
    current_residual: Sequence[float],
    band_idx: int,
    config: dict,
) -> tuple[list[float], list[float]]:
    """Process one frequency band: window the kept portion, compute the
    residual that flows into the next band.

    Returns (processed_band, updated_residual). Ported from
    oca_transfer.py:calculate_multi_sample_rate_filter.
    """
    band_lengths = config["band_lengths"]
    dec_filters_info = config["dec_filters_info"]
    delay_comp = config["delay_comp"]

    band_len = band_lengths[band_idx]
    filter_info = dec_filters_info[band_idx]
    use_delay_comp = delay_comp[band_idx]

    dec_filter_phases = filter_info["phases"]
    dec_filter_original_len = filter_info["original_length"]

    if band_len <= 0:
        return [], list(current_residual)

    processed_band = [0.0] * band_len
    delay = (
        (dec_filter_original_len * 3 - 3) // 2 if use_delay_comp else 0
    )
    win_len = band_len - delay
    if win_len < 0:
        return [], list(current_residual)

    win_alloc = win_len * 2 + 3
    full_window = generate_window(win_alloc)

    # 1. Pass the delay portion straight through.
    for i in range(delay):
        if i < len(current_residual):
            processed_band[i] = current_residual[i]

    # 2. Window the next ``win_len`` samples.
    window_offset = win_alloc // 2 + 1
    for i in range(win_len):
        residual_idx = delay + i
        if residual_idx < len(current_residual) and (window_offset + i) < len(full_window):
            processed_band[residual_idx] = (
                current_residual[residual_idx] * full_window[window_offset + i]
            )
        elif residual_idx >= len(current_residual):
            break

    # 3. Compute residual for the next band: input minus what we kept.
    residual_for_decimation: list[float] = []
    for i in range(win_len):
        residual_idx = delay + i
        if residual_idx < len(current_residual):
            residual_for_decimation.append(
                current_residual[residual_idx] - processed_band[residual_idx]
            )
        else:
            residual_for_decimation.append(0.0)
    for i in range(delay + win_len, len(current_residual)):
        residual_for_decimation.append(current_residual[i])

    # 4. Decimate by 4× via polyphase, and amplify by M to compensate.
    decimated = polyphase_decimate(
        residual_for_decimation,
        dec_filter_phases,
        DECIMATION_FACTOR,
        dec_filter_original_len,
    )
    updated_residual = [v * DECIMATION_FACTOR for v in decimated]
    return processed_band, updated_residual


def calculate_multirate(
    impulse_response: Sequence[float],
    config: dict,
) -> list[float]:
    """Run the multi-band polyphase decomposition end-to-end.

    Returns the AVR-format coefficient vector (1024 floats for speakers,
    704 for subs). Ported from oca_transfer.py:calculate_multirate.
    """
    if not impulse_response:
        return []

    output_length = config["output_length"]
    final_output = [0.0] * output_length
    current_residual = list(impulse_response)
    output_write_offset = 0

    num_bands = len(config["band_lengths"])
    for band_idx in range(num_bands - 1):
        processed_band, current_residual = _calculate_band(
            current_residual, band_idx, config,
        )
        current_band_len = config["band_lengths"][band_idx]
        for i in range(current_band_len):
            output_idx = output_write_offset + i
            if output_idx < output_length and i < len(processed_band):
                final_output[output_idx] = processed_band[i]
        output_write_offset += current_band_len

    # Last band: copy whatever residual is left, no decimation.
    last_band_len = config["band_lengths"][num_bands - 1]
    for i in range(last_band_len):
        output_idx = output_write_offset + i
        if output_idx < output_length and i < len(current_residual):
            final_output[output_idx] = current_residual[i]
    return final_output


def convert_xt32(impulse_response: Sequence[float]) -> list[float]:
    """Apply XT32 polyphase decimation to a 16,321 (speaker) or 16,055 (sub)
    tap impulse response.

    Returns the AVR-format coefficient vector — 1024 (speaker) or
    704 (sub) floats — ready to wrap in SET_COEFDT packets.

    The input length determines speaker vs sub configuration.
    """
    n = len(impulse_response)
    if n == 0:
        return []
    speaker_cfg = FILTER_CONFIGS["xt32Speaker"]
    sub_cfg = FILTER_CONFIGS["xt32Sub"]
    if n == speaker_cfg["input_length"]:
        return calculate_multirate(impulse_response, speaker_cfg)
    if n == sub_cfg["input_length"]:
        return calculate_multirate(impulse_response, sub_cfg)
    raise ValueError(
        f"impulse response length {n} doesn't match XT32 expectations "
        f"({speaker_cfg['input_length']} for speakers or "
        f"{sub_cfg['input_length']} for subs)"
    )


# ── FIR design helpers ──────────────────────────────────────────────────


def design_correction_ir(
    target_freqs_hz: Sequence[float],
    target_gain_db: Sequence[float],
    *,
    is_sub: bool,
    samplerate_hz: float = 48000.0,
) -> list[float]:
    """Design a minimum-phase FIR impulse response to apply ``target_gain_db``
    at ``target_freqs_hz``, of the right length for XT32 polyphase
    decimation (16,321 taps for speakers, 16,055 for subs).

    Approach: linear-magnitude inverse FFT of an interpolated target
    response, with a Hann window and zero-padding to the AVR-expected
    length. Outside the supplied frequency range gain tapers to 0 dB.

    Returns a Python list of float taps. Pass to ``convert_xt32`` to
    polyphase-decimate to the AVR-format 1024/704 coefficients.
    """
    if len(target_freqs_hz) != len(target_gain_db):
        raise ValueError("target_freqs_hz and target_gain_db must be same length")

    target_taps = (
        FILTER_CONFIGS["xt32Sub"]["input_length"] if is_sub
        else FILTER_CONFIGS["xt32Speaker"]["input_length"]
    )
    n_fft = 1
    while n_fft < target_taps:
        n_fft *= 2

    # Frequency grid for the FFT bins (one-sided).
    bins = np.fft.rfftfreq(n_fft, d=1.0 / samplerate_hz)
    # Interpolate target curve on a log axis (much more natural for audio).
    freqs = np.asarray(target_freqs_hz, dtype=np.float64)
    gains = np.asarray(target_gain_db, dtype=np.float64)
    log_bins = np.log10(np.maximum(bins, 1e-3))
    log_freqs = np.log10(np.maximum(freqs, 1e-3))
    gain_db = np.interp(log_bins, log_freqs, gains, left=gains[0], right=gains[-1])
    # Outside the supplied range, taper toward 0 dB to avoid wild boost
    # at DC and Nyquist.
    below = bins < freqs[0]
    above = bins > freqs[-1]
    gain_db = np.where(below, 0.0, gain_db)
    gain_db = np.where(above, 0.0, gain_db)
    magnitude = 10.0 ** (gain_db / 20.0)

    # Inverse FFT → linear-phase IR centred at n_fft/2.
    spectrum = magnitude.astype(np.complex128)
    ir_full = np.fft.irfft(spectrum, n=n_fft)
    # Centre the response and trim/pad to the AVR-expected length.
    centred = np.fft.fftshift(ir_full)
    mid = n_fft // 2
    half = target_taps // 2
    start = mid - half
    end = start + target_taps
    truncated = centred[start:end]

    # Hann-window the truncated IR to suppress edge ringing.
    window = np.hanning(target_taps)
    windowed = truncated * window

    # Normalise peak |coefficient| to ≤ 0.5 so subsequent polyphase math
    # has headroom (the AVR's filter engine clips on overflow).
    peak = float(np.max(np.abs(windowed)))
    if peak > 0:
        windowed = windowed * (0.5 / peak)
    return windowed.tolist()


def design_passthrough_ir(*, is_sub: bool) -> list[float]:
    """Build a zero-gain passthrough impulse response of the AVR-expected
    length (16,321 speaker / 16,055 sub).

    The "impulse" goes at the centre tap. Useful for restoring channels
    to a flat-EQ state.
    """
    target_taps = (
        FILTER_CONFIGS["xt32Sub"]["input_length"] if is_sub
        else FILTER_CONFIGS["xt32Speaker"]["input_length"]
    )
    ir = [0.0] * target_taps
    ir[target_taps // 2] = 1.0
    return ir
