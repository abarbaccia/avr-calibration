"""Tests for the SET_SETDAT envelope builder + chunker.

These cover everything that doesn't need a live AVR — frame size math,
field-order preservation, type-correctness for the calibration_settings
fields the firmware is picky about, and the chunker's ability to split
a 1.5 kB envelope across multiple sub-510-byte packets.

The actual TCP push (push_avr_filters) is exercised by the
``smoke_test_filter_upload.py`` script, which talks to the real AVR.
"""
from __future__ import annotations

import json

import pytest

from calibrate.drivers.denon.audyssey_filter_upload import (
    DEFAULT_CALIBRATION_SETTINGS,
    SET_SETDAT_CHUNK_THRESHOLD_BYTES,
    SETDAT_PARAM_ORDER,
    build_set_dat_envelope,
    channels_in_ady,
    chunk_setdat_payload,
    envelope_packet_size,
    parse_frames,
)


# ── Envelope builder ───────────────────────────────────────────────────


@pytest.fixture()
def minimal_ady() -> dict:
    """Tiny .ady stand-in with FL/FR/SW1 — enough for envelope tests."""
    return {
        "ampAssignInfo": "00" * 48,
        "enAmpAssignType": 0,
        "subwooferNum": 1,
        "detectedChannels": [
            {
                "commandId": "FL",
                "customSpeakerType": "S",
                "customDistance": 4.05,
                "customCrossover": 80,
                "trimAdjustment": -1.5,
                "responseData": {"0": [0.0], "1": [0.0], "2": [0.0]},
            },
            {
                "commandId": "FR",
                "customSpeakerType": "S",
                "customDistance": 4.20,
                "customCrossover": 80,
                "trimAdjustment": 0.0,
                "responseData": {"0": [0.0], "1": [0.0], "2": [0.0]},
            },
            {
                "commandId": "SW1",
                "customSpeakerType": "E",
                "customDistance": 6.0,
                "customCrossover": 0,
                "trimAdjustment": 2.0,
                "responseData": {"0": [0.0], "1": [0.0], "2": [0.0]},
            },
        ],
    }


@pytest.fixture()
def minimal_avr_status() -> dict:
    return {
        "AmpAssign": "Normal",
        "AssignBin": "deadbeef" + "00" * 44,
        "ChSetup": [{"FL": "S"}, {"FR": "S"}, {"SW1": "E"}],
        "SWSetup": {"SWNum": "1", "SWMode": "N/A", "SWLayout": "N/A"},
        "DType": "Float",
        "EQType": "MultEQXT32",
    }


def test_build_envelope_preserves_required_field_order(
    minimal_ady: dict, minimal_avr_status: dict
) -> None:
    """The AVR firmware requires a specific field order or it NACKs."""
    ordered = build_set_dat_envelope(minimal_ady, minimal_avr_status)
    keys_in_order = [k for k, _ in ordered]
    # All emitted keys must appear in SETDAT_PARAM_ORDER, AND must keep
    # the same relative order as that tuple.
    canonical_indices = [SETDAT_PARAM_ORDER.index(k) for k in keys_in_order]
    assert canonical_indices == sorted(canonical_indices), (
        f"envelope key order is not canonical: {keys_in_order}"
    )


def test_build_envelope_includes_core_fields(
    minimal_ady: dict, minimal_avr_status: dict
) -> None:
    keys = [k for k, _ in build_set_dat_envelope(minimal_ady, minimal_avr_status)]
    for required in (
        "AmpAssign", "AssignBin", "SpConfig", "Distance", "ChLevel",
        "Crossover", "AudyFinFlg", "AudyDynEq", "AudyEqRef", "AudyMultEQ",
        "SWSetup",
    ):
        assert required in keys, f"missing required field {required!r}"


def test_audyfinflg_defaults_to_notfin(
    minimal_ady: dict, minimal_avr_status: dict
) -> None:
    ordered = dict(build_set_dat_envelope(minimal_ady, minimal_avr_status))
    assert ordered["AudyFinFlg"] == "NotFin"


def test_audymulteq_uses_capital_q(
    minimal_ady: dict, minimal_avr_status: dict
) -> None:
    """The field name has a capital Q at the end. Lowercase q is a NACK."""
    ordered = dict(build_set_dat_envelope(minimal_ady, minimal_avr_status))
    assert "AudyMultEQ" in ordered
    assert "AudyMultEq" not in ordered


def test_calibration_settings_use_correct_types(
    minimal_ady: dict, minimal_avr_status: dict
) -> None:
    """AudyDynEq/AudyDynVol/AudyMultEQ/AudyLfc are bools, AudyEqRef is int.
    Wrong types trigger a NACK."""
    ordered = dict(build_set_dat_envelope(minimal_ady, minimal_avr_status))
    assert isinstance(ordered["AudyDynEq"], bool)
    assert isinstance(ordered["AudyDynVol"], bool)
    assert isinstance(ordered["AudyMultEQ"], bool)
    assert isinstance(ordered["AudyLfc"], bool)
    assert isinstance(ordered["AudyEqRef"], int) and not isinstance(ordered["AudyEqRef"], bool)
    assert isinstance(ordered["AudyLfcLev"], int) and not isinstance(ordered["AudyLfcLev"], bool)


def test_distance_override_replaces_ady_value(
    minimal_ady: dict, minimal_avr_status: dict
) -> None:
    ordered = dict(
        build_set_dat_envelope(
            minimal_ady,
            minimal_avr_status,
            distances_override_m={"SW1": 20.0},
        )
    )
    distance_arrays = ordered["Distance"]
    # 3 measurement positions per minimal_ady fixture
    assert len(distance_arrays) == 3
    for pos in distance_arrays:
        assert pos["SW1"] == 2000  # 20.0 m × 100 cm/m
        assert pos["FL"] == 405    # untouched (.ady customDistance × 100)


def test_distance_override_only_affects_named_channels(
    minimal_ady: dict, minimal_avr_status: dict
) -> None:
    ordered = dict(
        build_set_dat_envelope(
            minimal_ady,
            minimal_avr_status,
            distances_override_m={"FL": 10.0},
        )
    )
    pos = ordered["Distance"][0]
    assert pos["FL"] == 1000
    assert pos["FR"] == 420  # unaffected


def test_subwoofer_crossover_set_to_F_string(
    minimal_ady: dict, minimal_avr_status: dict
) -> None:
    """Speaker type "E" (subwoofer) gets crossover "F" (full-range)."""
    ordered = dict(build_set_dat_envelope(minimal_ady, minimal_avr_status))
    crossover_arr = ordered["Crossover"][0]
    assert crossover_arr["SW1"] == "F"
    assert crossover_arr["FL"] == 80


def test_channel_level_uses_dbx10_int(
    minimal_ady: dict, minimal_avr_status: dict
) -> None:
    """AVR encodes ChLevel as integer dB × 10."""
    ordered = dict(build_set_dat_envelope(minimal_ady, minimal_avr_status))
    chlevel = ordered["ChLevel"][0]
    assert chlevel["FL"] == -15   # -1.5 dB × 10
    assert chlevel["SW1"] == 20   # +2.0 dB × 10


def test_calibration_settings_override(
    minimal_ady: dict, minimal_avr_status: dict
) -> None:
    """Caller-provided settings replace defaults."""
    ordered = dict(
        build_set_dat_envelope(
            minimal_ady,
            minimal_avr_status,
            calibration_settings={"AudyDynEq": True, "AudyEqRef": 1},
        )
    )
    assert ordered["AudyDynEq"] is True
    assert ordered["AudyEqRef"] == 1
    # Untouched fields keep defaults.
    assert ordered["AudyDynVol"] is False


def test_build_envelope_raises_on_empty_ady() -> None:
    with pytest.raises(ValueError, match="no detectedChannels"):
        build_set_dat_envelope({"detectedChannels": []}, {})


def test_default_calibration_settings_match_oca_reference() -> None:
    """Pin the defaults — these match A1Evo Acoustica's post-cal values."""
    assert DEFAULT_CALIBRATION_SETTINGS == {
        "AudyFinFlg": "NotFin",
        "AudyDynEq": False,
        "AudyEqRef": 0,
        "AudyDynVol": False,
        "AudyDynSet": "L",
        "AudyMultEQ": True,
        "AudyEqSet": "Flat",
        "AudyLfc": False,
        "AudyLfcLev": 3,
    }


# ── Chunker ────────────────────────────────────────────────────────────


def test_chunk_keeps_small_payload_in_one_packet() -> None:
    chunks = chunk_setdat_payload([("AudyFinFlg", "NotFin")])
    assert chunks == [{"AudyFinFlg": "NotFin"}]


def test_chunk_drops_none_values() -> None:
    chunks = chunk_setdat_payload(
        [("A", 1), ("B", None), ("C", 2)]
    )
    assert chunks == [{"A": 1, "C": 2}]


def test_chunk_splits_when_envelope_exceeds_threshold(
    minimal_ady: dict, minimal_avr_status: dict
) -> None:
    """Real envelopes are 1-2 kB. Must split across multiple packets."""
    # Bulk up the .ady to push the envelope past one chunk's worth.
    bulked = dict(minimal_ady)
    extra = []
    for i in range(20):
        extra.append({
            "commandId": "C",  # not technically valid to repeat, but the
                                # envelope builder doesn't dedupe — we
                                # just need to make the JSON heavy
            "customSpeakerType": "S",
            "customDistance": 4.0 + i * 0.01,
            "customCrossover": 80,
            "trimAdjustment": 0.0,
            "responseData": {str(j): [0.0] for j in range(3)},
        })
    bulked["detectedChannels"] = list(minimal_ady["detectedChannels"]) + extra
    ordered = build_set_dat_envelope(bulked, minimal_avr_status)
    chunks = chunk_setdat_payload(ordered)
    assert len(chunks) >= 2, f"expected splitting; got {len(chunks)} chunks"
    for chunk in chunks:
        assert envelope_packet_size(chunk) <= SET_SETDAT_CHUNK_THRESHOLD_BYTES


def test_chunk_preserves_field_order_across_chunks() -> None:
    """When chunked, fields keep their canonical order globally."""
    # Synthetic but representative ordered list.
    ordered = [
        ("AmpAssign", "Normal"),
        ("AssignBin", "00" * 48),
        ("Distance", [{"FL": 405, "FR": 420, "C": 386, "SW1": 2000}] * 3),
        ("ChLevel", [{"FL": 0, "FR": 0, "C": 0, "SW1": 20}] * 3),
        ("AudyFinFlg", "NotFin"),
    ]
    chunks = chunk_setdat_payload(ordered, max_packet_bytes=200)
    flat_keys: list[str] = []
    for chunk in chunks:
        flat_keys.extend(chunk.keys())
    assert flat_keys == [k for k, _ in ordered if _ is not None]


def test_chunk_raises_when_single_param_exceeds_threshold() -> None:
    huge = "x" * 10_000
    with pytest.raises(ValueError, match="exceeds"):
        chunk_setdat_payload([("AssignBin", huge)], max_packet_bytes=510)


# ── Helpers ────────────────────────────────────────────────────────────


def test_channels_in_ady_returns_command_ids(minimal_ady: dict) -> None:
    assert channels_in_ady(minimal_ady) == ["FL", "FR", "SW1"]


def test_envelope_packet_size_includes_full_frame() -> None:
    """envelope_packet_size accounts for header + checksum, not just body."""
    payload = {"AudyFinFlg": "NotFin"}
    body_len = len(json.dumps(payload, separators=(",", ":")).encode("ascii"))
    full_len = envelope_packet_size(payload)
    # Frame overhead = marker(1) + length(2) + reserved(2) + cmd(10) +
    #                   sep(1) + data_len(2) + checksum(1) = 19 bytes.
    assert full_len == body_len + 19


# ── parse_frames ───────────────────────────────────────────────────────


def test_parse_frames_round_trip() -> None:
    """A frame we construct via build_frame parses back correctly."""
    from calibrate.drivers.denon.audyssey_tcp import build_frame

    payload = b'{"Comm":"ACK"}'
    frame = build_frame("SET_SETDAT", payload)
    # Mimic an incoming response — TX uses 'T', RX uses 'R'. parse_frames
    # accepts both. Build a response-shape frame manually to test:
    rx_frame = bytearray(frame)
    rx_frame[0] = ord("R")
    rx_frame[-1] = sum(rx_frame[:-1]) & 0xFF  # recompute checksum

    stream = bytearray(rx_frame)
    parsed = parse_frames(stream)
    assert len(parsed) == 1
    assert parsed[0]["cmd"].strip() == "SET_SETDAT"
    assert parsed[0]["data"] == payload
    # Stream should be fully consumed.
    assert len(stream) == 0


def test_parse_frames_skips_invalid_checksum() -> None:
    """A frame with a corrupt checksum is dropped one byte at a time."""
    from calibrate.drivers.denon.audyssey_tcp import build_frame

    good = bytearray(build_frame("ENTER_AUDY"))
    good[-1] ^= 0xFF  # corrupt checksum
    parsed = parse_frames(good)
    assert parsed == []


def test_parse_frames_empty_stream() -> None:
    assert parse_frames(bytearray()) == []
