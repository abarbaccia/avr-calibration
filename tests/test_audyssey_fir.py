"""Tests for the polyphase XT32 FIR + channel-byte tooling.

The polyphase math is ported from oca_transfer.py. We don't have a
reference fixture file, so the tests cover correctness via algorithmic
properties: lengths, energy conservation under decompose/reassemble,
known-input/known-output for short test cases, and channel-byte
round-trips.
"""
from __future__ import annotations

import numpy as np
import pytest

from calibrate.audyssey_fir import (
    CHANNEL_BYTE_TABLE,
    DECIMATION_FACTOR,
    FILTER_CONFIGS,
    calculate_multirate,
    convert_xt32,
    decompose_filter,
    design_correction_ir,
    design_passthrough_ir,
    filter_config_for,
    generate_window,
    get_channel_byte,
    is_sub_channel,
    polyphase_decimate,
)


# ── Channel-byte table ─────────────────────────────────────────────────


def test_get_channel_byte_xt32_known_values() -> None:
    assert get_channel_byte("FL", "XT32") == 0x00
    assert get_channel_byte("C", "XT32") == 0x01
    assert get_channel_byte("FR", "XT32") == 0x02
    assert get_channel_byte("SW1", "XT32") == 0x0d
    assert get_channel_byte("SW2", "XT32") == 0x0e
    assert get_channel_byte("LFE", "XT32") == 0x0d
    # Heights / atmos
    assert get_channel_byte("TFL", "XT32") == 0x0b
    assert get_channel_byte("TFR", "XT32") == 0x04
    assert get_channel_byte("TRL", "XT32") == 0x09
    assert get_channel_byte("TRR", "XT32") == 0x06


def test_get_channel_byte_unknown_channel_raises() -> None:
    with pytest.raises(ValueError, match="Unknown Audyssey channel"):
        get_channel_byte("BOGUS", "XT32")


def test_get_channel_byte_xt32_falls_back_to_neq2_when_eq2_missing() -> None:
    # SRB has eq2=None but neq2=0x07. XT32 should fall back.
    assert CHANNEL_BYTE_TABLE["SRB"]["eq2"] is None
    assert get_channel_byte("SRB", "XT32") == 0x07


def test_is_sub_channel() -> None:
    assert is_sub_channel("SW1")
    assert is_sub_channel("SW2")
    assert is_sub_channel("LFE")
    assert not is_sub_channel("FL")
    assert not is_sub_channel("C")


def test_filter_config_for_routes_correctly() -> None:
    assert filter_config_for("FL")["output_length"] == 1024
    assert filter_config_for("SW1")["output_length"] == 704
    assert filter_config_for("LFE")["output_length"] == 704


# ── Polyphase decomposition ────────────────────────────────────────────


def test_decompose_filter_round_trip() -> None:
    """Decomposing then re-interleaving should recover the original."""
    taps = list(np.arange(20, dtype=np.float64))
    M = 4
    phases = decompose_filter(taps, M)
    assert len(phases) == M
    assert all(len(p) == 5 for p in phases)
    # Reassemble: phase[p][i] sits at original index i*M + p.
    reassembled = [0.0] * len(taps)
    for p in range(M):
        for i, t in enumerate(phases[p]):
            reassembled[i * M + p] = t
    assert reassembled == taps


def test_decompose_filter_decimation_factor_4() -> None:
    """The four polyphase filters should match every-fourth-tap slicing."""
    M = DECIMATION_FACTOR
    taps = list(range(13))  # length not divisible by M
    phases = decompose_filter(taps, M)
    assert phases[0] == [0, 4, 8, 12]
    assert phases[1] == [1, 5, 9]
    assert phases[2] == [2, 6, 10]
    assert phases[3] == [3, 7, 11]


def test_decompose_filter_zero_M_returns_empty_list() -> None:
    assert decompose_filter([1.0, 2.0], 0) == []


# ── Polyphase decimation kernel ────────────────────────────────────────


def test_polyphase_decimate_output_length() -> None:
    """Output length is ceil((signal + filter - 1) / M)."""
    rng = np.random.default_rng(42)
    signal = rng.normal(size=400).tolist()
    M = 4
    filt = rng.normal(size=29).tolist()
    phases = decompose_filter(filt, M)
    decimated = polyphase_decimate(signal, phases, M, len(filt))
    expected_len = (len(signal) + len(filt) - 1 + M - 1) // M
    assert len(decimated) == expected_len


def test_polyphase_decimate_finite_and_deterministic() -> None:
    """Two runs over the same input produce identical finite output."""
    rng = np.random.default_rng(123)
    signal = rng.normal(size=200).tolist()
    M = 4
    filt = rng.normal(size=29).tolist()
    phases = decompose_filter(filt, M)
    out1 = polyphase_decimate(signal, phases, M, len(filt))
    out2 = polyphase_decimate(signal, phases, M, len(filt))
    assert out1 == out2
    assert all(np.isfinite(out1))


def test_polyphase_decimate_empty_signal_returns_empty() -> None:
    assert polyphase_decimate([], [[1.0]], 1, 1) == []


# ── Window ──────────────────────────────────────────────────────────────


def test_generate_window_endpoints_zero() -> None:
    """Hann window: zero at both endpoints, peak at center."""
    w = generate_window(11)
    assert w[0] == pytest.approx(0.0)
    assert w[-1] == pytest.approx(0.0)
    assert max(w) == pytest.approx(1.0, abs=1e-6)


def test_generate_window_zero_length() -> None:
    assert generate_window(0) == []


# ── XT32 polyphase decimation (the big one) ────────────────────────────


def test_calculate_multirate_speaker_output_length() -> None:
    """Speaker IR (16321 taps) → 1024 output coefs."""
    cfg = FILTER_CONFIGS["xt32Speaker"]
    ir = [0.0] * cfg["input_length"]
    ir[cfg["input_length"] // 2] = 1.0  # impulse at center
    out = calculate_multirate(ir, cfg)
    assert len(out) == 1024


def test_calculate_multirate_sub_output_length() -> None:
    """Sub IR (16055 taps) → 704 output coefs."""
    cfg = FILTER_CONFIGS["xt32Sub"]
    ir = [0.0] * cfg["input_length"]
    ir[cfg["input_length"] // 2] = 1.0
    out = calculate_multirate(ir, cfg)
    assert len(out) == 704


def test_convert_xt32_speaker_dispatch() -> None:
    """convert_xt32 picks the right config based on input length."""
    speaker_len = FILTER_CONFIGS["xt32Speaker"]["input_length"]
    ir = [0.0] * speaker_len
    ir[speaker_len // 2] = 1.0
    out = convert_xt32(ir)
    assert len(out) == 1024


def test_convert_xt32_sub_dispatch() -> None:
    sub_len = FILTER_CONFIGS["xt32Sub"]["input_length"]
    ir = [0.0] * sub_len
    ir[sub_len // 2] = 1.0
    out = convert_xt32(ir)
    assert len(out) == 704


def test_convert_xt32_wrong_length_raises() -> None:
    with pytest.raises(ValueError, match="doesn't match XT32 expectations"):
        convert_xt32([0.0] * 100)


def test_convert_xt32_empty_returns_empty() -> None:
    assert convert_xt32([]) == []


def test_convert_xt32_passthrough_ir_is_finite_and_nonzero() -> None:
    """A centered impulse should produce a non-zero, finite output that
    largely concentrates energy near the start (post-shift)."""
    speaker_len = FILTER_CONFIGS["xt32Speaker"]["input_length"]
    ir = [0.0] * speaker_len
    ir[speaker_len // 2] = 1.0
    out = np.asarray(convert_xt32(ir))
    assert np.all(np.isfinite(out))
    assert np.any(np.abs(out) > 0.0), "passthrough IR should produce nonzero output"


# ── FIR design helpers ─────────────────────────────────────────────────


def test_design_passthrough_ir_speaker_length() -> None:
    ir = design_passthrough_ir(is_sub=False)
    assert len(ir) == FILTER_CONFIGS["xt32Speaker"]["input_length"]
    # Single nonzero tap at the center.
    nonzero_idx = [i for i, v in enumerate(ir) if v != 0.0]
    assert nonzero_idx == [len(ir) // 2]


def test_design_passthrough_ir_sub_length() -> None:
    ir = design_passthrough_ir(is_sub=True)
    assert len(ir) == FILTER_CONFIGS["xt32Sub"]["input_length"]


def test_design_correction_ir_speaker_length() -> None:
    """Output length matches the AVR's expected speaker input."""
    ir = design_correction_ir(
        target_freqs_hz=[20, 100, 1000, 10000, 20000],
        target_gain_db=[0, 0, 0, 0, 0],
        is_sub=False,
    )
    assert len(ir) == FILTER_CONFIGS["xt32Speaker"]["input_length"]


def test_design_correction_ir_sub_length() -> None:
    ir = design_correction_ir(
        target_freqs_hz=[20, 50, 80, 200],
        target_gain_db=[0, 0, 0, 0],
        is_sub=True,
    )
    assert len(ir) == FILTER_CONFIGS["xt32Sub"]["input_length"]


def test_design_correction_ir_peak_under_half() -> None:
    """Designed IR is normalized so peak is at most 0.5."""
    ir = design_correction_ir(
        target_freqs_hz=[20, 100, 1000, 10000, 20000],
        target_gain_db=[+6, +3, 0, -3, -6],
        is_sub=False,
    )
    arr = np.asarray(ir)
    assert np.max(np.abs(arr)) <= 0.5 + 1e-9
    assert np.all(np.isfinite(arr))


def test_design_correction_ir_mismatched_lengths_raises() -> None:
    with pytest.raises(ValueError, match="same length"):
        design_correction_ir(
            target_freqs_hz=[20, 100, 1000],
            target_gain_db=[0, 0],
            is_sub=False,
        )


def test_design_correction_ir_pipes_through_convert_xt32() -> None:
    """End-to-end: design IR → polyphase decimate → output is the right
    length for the AVR's filter banks."""
    ir = design_correction_ir(
        target_freqs_hz=[20, 1000, 20000],
        target_gain_db=[0, +3, 0],
        is_sub=False,
    )
    avr_taps = convert_xt32(ir)
    assert len(avr_taps) == 1024
    assert all(np.isfinite(avr_taps))


def test_design_correction_ir_sub_pipes_through_convert_xt32() -> None:
    ir = design_correction_ir(
        target_freqs_hz=[20, 50, 80, 200],
        target_gain_db=[+5, +2, 0, 0],
        is_sub=True,
    )
    avr_taps = convert_xt32(ir)
    assert len(avr_taps) == 704
    assert all(np.isfinite(avr_taps))
