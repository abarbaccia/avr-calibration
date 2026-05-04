"""Tests for the SET_COEFDT packet builder."""
from __future__ import annotations

import struct

import numpy as np
import pytest

from calibrate.drivers.denon.audyssey_coef_transfer import (
    MARKER,
    SET_COEFDT_BYTES,
    TARGET_CURVE_FLAT,
    TARGET_CURVE_REFERENCE,
    XT32_SAMPLE_RATES_HZ,
    all_streams_for_channel,
    build_coef_packets,
    packet_config_for,
)


# ── packet_config_for math ──────────────────────────────────────────────


def test_packet_config_speaker_1024_floats() -> None:
    """1024 floats → 9 packets: 1×127 + 7×128 + 1×1."""
    cfg = packet_config_for(1024)
    assert cfg["packet_count"] == 9
    assert cfg["first_packet_floats"] == 127
    assert cfg["mid_packet_floats"] == 128
    assert cfg["last_packet_floats"] == 1
    assert cfg["last_seq_num"] == 8
    # Sanity: floats sum to 1024.
    total = (
        cfg["first_packet_floats"]
        + (cfg["packet_count"] - 2) * cfg["mid_packet_floats"]
        + cfg["last_packet_floats"]
    )
    assert total == 1024


def test_packet_config_sub_704_floats() -> None:
    """704 floats → 6 packets: 1×127 + 4×128 + 1×65."""
    cfg = packet_config_for(704)
    assert cfg["packet_count"] == 6
    assert cfg["first_packet_floats"] == 127
    assert cfg["last_packet_floats"] == 65
    assert cfg["last_seq_num"] == 5
    total = (
        cfg["first_packet_floats"]
        + (cfg["packet_count"] - 2) * cfg["mid_packet_floats"]
        + cfg["last_packet_floats"]
    )
    assert total == 704


def test_packet_config_zero_floats() -> None:
    cfg = packet_config_for(0)
    assert cfg["packet_count"] == 0
    assert cfg["last_seq_num"] == 0


def test_packet_config_single_packet() -> None:
    """≤ 127 floats fit in one packet, marked as both first and last."""
    cfg = packet_config_for(50)
    assert cfg["packet_count"] == 1
    assert cfg["first_packet_floats"] == 50
    assert cfg["last_packet_floats"] == 50


def test_packet_config_exact_127_one_packet() -> None:
    cfg = packet_config_for(127)
    assert cfg["packet_count"] == 1
    assert cfg["last_packet_floats"] == 127


def test_packet_config_exactly_255_floats() -> None:
    """127 + 128 = 255 → 2 packets, last = 128."""
    cfg = packet_config_for(255)
    assert cfg["packet_count"] == 2
    assert cfg["last_packet_floats"] == 128


# ── build_coef_packets framing ─────────────────────────────────────────


def _parse_packet_header(pkt: bytes) -> dict:
    """Helper: pull the framing fields out of a SET_COEFDT packet for tests."""
    assert pkt[0] == MARKER
    total_len = struct.unpack(">H", pkt[1:3])[0]
    assert len(pkt) == total_len, f"length mismatch: header says {total_len}, frame is {len(pkt)}"
    seq = pkt[3]
    last_seq = pkt[4]
    cmd = pkt[5:15]
    sep = pkt[15]
    param_len = struct.unpack(">H", pkt[16:18])[0]
    param_data = pkt[18:18 + param_len]
    checksum = pkt[-1]
    expected = sum(pkt[:-1]) & 0xFF
    return {
        "marker_ok": pkt[0] == MARKER,
        "total_len": total_len,
        "seq": seq,
        "last_seq": last_seq,
        "cmd": cmd,
        "separator_ok": sep == 0,
        "param_len": param_len,
        "param_data": param_data,
        "checksum_ok": checksum == expected,
    }


def test_build_packets_speaker_count_and_command_bytes() -> None:
    coefs = [0.0] * 1024  # speaker IR length post-decimation
    pkts = build_coef_packets(
        coefs,
        channel_id="FL",
        target_curve=TARGET_CURVE_FLAT,
        samplerate_hz=48000,
    )
    assert len(pkts) == 9
    for i, pkt in enumerate(pkts):
        info = _parse_packet_header(pkt)
        assert info["marker_ok"]
        assert info["cmd"] == SET_COEFDT_BYTES
        assert info["separator_ok"]
        assert info["seq"] == i
        assert info["last_seq"] == 8
        assert info["checksum_ok"]


def test_build_packets_sub_count() -> None:
    coefs = [0.0] * 704
    pkts = build_coef_packets(
        coefs,
        channel_id="SW1",
        target_curve=TARGET_CURVE_REFERENCE,
        samplerate_hz=48000,
    )
    assert len(pkts) == 6


def test_first_packet_carries_tc_sr_channel_header() -> None:
    """First packet's param_data = tc(1) + sr(1) + channel(1) + 0x00 +
    coefficient bytes. Subsequent packets are just coefficient bytes."""
    coefs = [1.0, 2.0, 3.0, 4.0, 5.0]
    pkts = build_coef_packets(
        coefs,
        channel_id="FL",
        target_curve=TARGET_CURVE_FLAT,
        samplerate_hz=48000,
    )
    first = _parse_packet_header(pkts[0])
    # First 4 bytes of param: target_curve, sample_rate, channel_byte, 0x00
    header = first["param_data"][:4]
    assert header == bytes.fromhex("00" + "02" + "00" + "00")
    # FL = eq2 byte 0x00, samplerate 48000 = code 02, target_curve = 00 (Flat)


def test_first_packet_header_for_sw1_at_44100() -> None:
    coefs = [1.0]
    pkts = build_coef_packets(
        coefs,
        channel_id="SW1",
        target_curve=TARGET_CURVE_REFERENCE,
        samplerate_hz=44100,
    )
    first = _parse_packet_header(pkts[0])
    # SW1 channel_byte = 0x0d, samplerate 44100 = code 01, tc = 01 (Reference)
    assert first["param_data"][:4] == bytes.fromhex("01" + "01" + "0d" + "00")


def test_coefficients_serialize_as_be_float32() -> None:
    """Wire format is big-endian float32. Confirmed by pcap-decode of real
    AVR traffic (scripts/audyssey_pcap_decode.py uses '>f') and by
    ratbuddyssey's FloatInt32 union. The prior LE encoding caused ~1-2%
    packet NACK + FINZ_COEFS-never-ACKs on multi-channel uploads."""
    coefs = [0.25, -0.5, 0.125]
    pkts = build_coef_packets(
        coefs,
        channel_id="FL",
        target_curve=TARGET_CURVE_FLAT,
        samplerate_hz=48000,
    )
    assert len(pkts) == 1
    info = _parse_packet_header(pkts[0])
    # Skip the 4-byte first-packet header.
    coef_bytes = info["param_data"][4:]
    assert len(coef_bytes) == len(coefs) * 4
    decoded = [
        struct.unpack(">f", coef_bytes[i * 4: (i + 1) * 4])[0]
        for i in range(len(coefs))
    ]
    np.testing.assert_allclose(decoded, coefs, atol=1e-7)


def test_coefficient_wire_bytes_match_pcap_format() -> None:
    """Pin the exact wire bytes for a known float so the encoding can't
    silently regress to little-endian. 0.25 in IEEE 754 BE = 0x3E800000."""
    pkts = build_coef_packets(
        [0.25],
        channel_id="FL",
        target_curve=TARGET_CURVE_FLAT,
        samplerate_hz=48000,
    )
    info = _parse_packet_header(pkts[0])
    coef_bytes = info["param_data"][4:]  # skip stream header
    assert coef_bytes == bytes.fromhex("3E800000"), (
        f"expected BE bytes 3E800000 for 0.25, got {coef_bytes.hex().upper()}"
    )


def test_invalid_target_curve_raises() -> None:
    with pytest.raises(ValueError, match="target_curve"):
        build_coef_packets(
            [0.0],
            channel_id="FL",
            target_curve="99",
            samplerate_hz=48000,
        )


def test_invalid_samplerate_raises() -> None:
    with pytest.raises(ValueError, match="samplerate"):
        build_coef_packets(
            [0.0],
            channel_id="FL",
            target_curve=TARGET_CURVE_FLAT,
            samplerate_hz=22050,  # not an Audyssey-supported rate
        )


def test_unknown_channel_raises() -> None:
    with pytest.raises(ValueError, match="Unknown Audyssey channel"):
        build_coef_packets(
            [0.0],
            channel_id="BOGUS",
            target_curve=TARGET_CURVE_FLAT,
            samplerate_hz=48000,
        )


# ── all_streams_for_channel ────────────────────────────────────────────


def test_all_streams_speaker_count() -> None:
    """Speaker (1024 floats × 9 packets) × 2 curves × 3 rates = 54 packets."""
    coefs = [0.0] * 1024
    streams = all_streams_for_channel(coefs, channel_id="FL")
    assert len(streams) == 2 * 3 * 9


def test_all_streams_sub_count() -> None:
    """Sub (704 floats × 6 packets) × 2 curves × 3 rates = 36 packets."""
    coefs = [0.0] * 704
    streams = all_streams_for_channel(coefs, channel_id="SW1")
    assert len(streams) == 2 * 3 * 6


def test_all_streams_first_packet_per_stream_carries_header() -> None:
    """Within each stream, only the seq=0 packet gets the 4-byte header.
    For 6 streams × 9 packets/stream the first packet of each stream is
    at indices 0, 9, 18, 27, 36, 45."""
    coefs = [0.0] * 1024
    streams = all_streams_for_channel(coefs, channel_id="FL")
    expected_first_indices = {0, 9, 18, 27, 36, 45}
    for i, pkt in enumerate(streams):
        info = _parse_packet_header(pkt)
        if i in expected_first_indices:
            assert info["seq"] == 0, f"packet {i} should be seq=0"
        else:
            assert info["seq"] != 0, f"packet {i} should not be seq=0"


def test_all_streams_uses_default_xt32_rates() -> None:
    """Default rates are 32k, 44.1k, 48k for XT32."""
    assert XT32_SAMPLE_RATES_HZ == (32000, 44100, 48000)
