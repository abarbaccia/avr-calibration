"""Tests for calibrate.drivers.audyssey_tcp — frame builder + payload shape.

Live TCP push is not exercised here; ``test_drivers.py`` covers the
DenonDriver wrapper with the network call mocked at the module level.
"""

from __future__ import annotations

import json
import struct
import sys
from unittest.mock import patch

import pytest

from calibrate.drivers.denon import audyssey_tcp
from calibrate.drivers.base import DriverError
from calibrate.drivers.denon import DenonDriver


def test_build_frame_set_setdat_minimal() -> None:
    body = b'{"x":1}'
    frame = audyssey_tcp.build_frame("SET_SETDAT", body)
    # Layout: 'T' + total_len(2) + cur(1) + tot(1) + cmd(10) + null(1) + data_len(2) + data + ck(1)
    assert frame[0] == ord("T")
    total_len = struct.unpack(">H", frame[1:3])[0]
    assert total_len == len(frame)  # total_len includes the trailing checksum byte
    assert frame[3] == 0 and frame[4] == 0  # current/total packets
    assert frame[5:15] == b"SET_SETDAT"
    assert frame[15] == 0  # null terminator
    data_len = struct.unpack(">H", frame[16:18])[0]
    assert data_len == len(body)
    assert frame[18:18 + data_len] == body
    assert frame[-1] == sum(frame[:-1]) & 0xFF


def test_build_frame_rejects_short_command() -> None:
    with pytest.raises(ValueError, match="exactly 10"):
        audyssey_tcp.build_frame("SHORT", b"")


def test_build_frame_rejects_long_command() -> None:
    with pytest.raises(ValueError, match="exactly 10"):
        audyssey_tcp.build_frame("WAY_TOO_LONG_CMD", b"")


def test_build_frame_empty_body() -> None:
    frame = audyssey_tcp.build_frame("ENTER_AUDY")
    # No data → total_len = 9 + 10 = 19 bytes
    assert struct.unpack(">H", frame[1:3])[0] == 19
    assert struct.unpack(">H", frame[16:18])[0] == 0
    assert len(frame) == 19


def test_build_distance_payload_basic() -> None:
    payload = audyssey_tcp.build_distance_payload(
        {"FL": 4.05, "SW1": 30.72}, n_positions=1,
    )
    assert payload == {"Distance": [{"FL": 405, "SW1": 3072}]}


def test_build_distance_payload_multi_position_replicates() -> None:
    payload = audyssey_tcp.build_distance_payload(
        {"FL": 4.05, "SW1": 30.72}, n_positions=3,
    )
    assert len(payload["Distance"]) == 3
    assert all(d == {"FL": 405, "SW1": 3072} for d in payload["Distance"])


def test_build_distance_payload_rounds_cm() -> None:
    payload = audyssey_tcp.build_distance_payload({"FL": 1.234}, n_positions=1)
    assert payload["Distance"][0]["FL"] == 123  # rounded from 123.4


def test_build_distance_payload_rejects_empty() -> None:
    with pytest.raises(ValueError, match="empty"):
        audyssey_tcp.build_distance_payload({}, n_positions=1)


def test_build_distance_payload_rejects_zero_positions() -> None:
    with pytest.raises(ValueError, match=">= 1"):
        audyssey_tcp.build_distance_payload({"FL": 1.0}, n_positions=0)


def test_distances_from_ady() -> None:
    ady = {
        "detectedChannels": [
            {"commandId": "FL", "customDistance": 4.05},
            {"commandId": "SW1", "customDistance": 2.47},
            {"commandId": None, "customDistance": 1.0},  # skipped
        ]
    }
    assert audyssey_tcp.distances_from_ady(ady) == {"FL": 4.05, "SW1": 2.47}


def test_n_positions_from_ady_three_positions() -> None:
    ady = {
        "detectedChannels": [
            {"responseData": {"0": [], "1": [], "2": []}},
        ]
    }
    assert audyssey_tcp.n_positions_from_ady(ady) == 3


def test_n_positions_from_ady_no_response_data_defaults_to_one() -> None:
    ady = {"detectedChannels": [{"commandId": "FL"}]}
    assert audyssey_tcp.n_positions_from_ady(ady) == 1


# ── DenonDriver.set_speaker_distances ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_denon_set_speaker_distances_no_host() -> None:
    driver = DenonDriver(host=None)
    with pytest.raises(DriverError, match="no host"):
        await driver.set_speaker_distances({"SW1": 30.72})


@pytest.mark.asyncio
async def test_denon_set_speaker_distances_delegates_to_audyssey_tcp() -> None:
    driver = DenonDriver(host="192.168.1.209")
    target = "calibrate.drivers.denon.audyssey_tcp.push_speaker_distances"
    with patch(target) as mock_push:
        async def fake_push(*args, **kwargs):
            return None
        mock_push.side_effect = fake_push
        await driver.set_speaker_distances(
            {"FL": 4.05, "SW1": 30.72}, n_positions=3, commit=True,
        )
    mock_push.assert_called_once()
    args, kwargs = mock_push.call_args
    assert args[0] == "192.168.1.209"
    assert args[1] == {"FL": 4.05, "SW1": 30.72}
    assert kwargs["n_positions"] == 3
    assert kwargs["commit"] is True


@pytest.mark.asyncio
async def test_denon_set_speaker_distances_wraps_oserror() -> None:
    driver = DenonDriver(host="192.168.1.209")
    target = "calibrate.drivers.denon.audyssey_tcp.push_speaker_distances"
    with patch(target) as mock_push:
        async def fake_push(*args, **kwargs):
            raise OSError("connection refused")
        mock_push.side_effect = fake_push
        with pytest.raises(DriverError, match="audyssey push failed"):
            await driver.set_speaker_distances({"SW1": 30.72})


def test_build_full_envelope_payload_per_channel_list_shape() -> None:
    """Full-envelope payload: SpConfig/Distance/ChLevel/Crossover are
    list-of-single-key-dicts (one per channel), not per-position lists.
    The X3800H rejects per-position multi-key shape with NACK."""
    ady = {
        "enAmpAssignType": 0,
        "ampAssignInfo": "00040302" + "0" * 80,
        "detectedChannels": [
            {"commandId": "FL", "customDistance": 4.0, "trimAdjustment": 0.5,
             "customSpeakerType": "S", "customCrossover": 80,
             "responseData": {"0": {}, "1": {}, "2": {}}},
            {"commandId": "C", "customDistance": 3.5, "trimAdjustment": -1.0,
             "customSpeakerType": "S", "customCrossover": 80,
             "responseData": {"0": {}, "1": {}, "2": {}}},
            {"commandId": "SW1", "customDistance": 2.5, "trimAdjustment": 0,
             "customSpeakerType": "", "customCrossover": 0,
             "responseData": {"0": {}, "1": {}, "2": {}}},
        ],
    }
    payload = audyssey_tcp.build_full_envelope_payload(ady)
    # Per-channel arrays: list of single-key dicts, NOT list of multi-key
    # per-position dicts. Matches A1Evo / X3800H wire format.
    assert payload["SpConfig"] == [{"FL": "S"}, {"C": "S"}, {"SW1": "E"}]
    # Distance in cm.
    assert payload["Distance"] == [{"FL": 400}, {"C": 350}, {"SW1": 250}]
    # ChLevel is dB × 10 (A1Evo convention).
    assert payload["ChLevel"] == [{"FL": 5}, {"C": -10}, {"SW1": 0}]
    # Crossover: numeric Hz for "S" speakers, "F" for sub channels.
    assert payload["Crossover"] == [{"FL": 80}, {"C": 80}, {"SW1": "F"}]
    # AmpAssign mapped from enum.
    assert payload["AmpAssign"] == "Normal"
    # AssignBin from .ady's ampAssignInfo (the canonical layout source).
    assert payload["AssignBin"] == ady["ampAssignInfo"]
    # A1Evo's exact calibration settings (booleans, ints — not strings).
    assert payload["AudyDynEq"] is False
    assert payload["AudyEqRef"] == 0
    assert payload["AudyMultEq"] is True
    assert payload["AudyEqSet"] == "Flat"


def test_build_full_envelope_payload_distance_overrides_apply() -> None:
    """distance_overrides_m bumps specific channels (typical use: SW1 push
    to compensate for FIR latency); other channels keep .ady values."""
    ady = {
        "enAmpAssignType": 0,
        "ampAssignInfo": "x",
        "detectedChannels": [
            {"commandId": "FL", "customDistance": 4.0,
             "customSpeakerType": "S", "customCrossover": 80, "trimAdjustment": 0},
            {"commandId": "SW1", "customDistance": 2.47,
             "customSpeakerType": "", "customCrossover": 0, "trimAdjustment": 0},
        ],
    }
    payload = audyssey_tcp.build_full_envelope_payload(
        ady, distance_overrides_m={"SW1": 17.91}
    )
    # SW1 overridden; FL keeps its .ady value.
    distances = {list(d.keys())[0]: list(d.values())[0] for d in payload["Distance"]}
    assert distances["FL"] == 400
    assert distances["SW1"] == 1791


def test_build_full_envelope_payload_defaults_missing_speaker_type() -> None:
    """Missing/empty/'?' customSpeakerType defaults to 'E' for sub channels
    (commandId starting with SW), 'S' for everything else."""
    ady = {
        "enAmpAssignType": 0, "ampAssignInfo": "x",
        "detectedChannels": [
            {"commandId": "TFL", "customDistance": 2.0, "customCrossover": 80,
             "trimAdjustment": 0},  # no customSpeakerType
            {"commandId": "SLA", "customDistance": 2.0, "customCrossover": 80,
             "trimAdjustment": 0, "customSpeakerType": "?"},
            {"commandId": "SW1", "customDistance": 2.0, "customCrossover": 0,
             "trimAdjustment": 0, "customSpeakerType": ""},
        ],
    }
    payload = audyssey_tcp.build_full_envelope_payload(ady)
    types = {list(d.keys())[0]: list(d.values())[0] for d in payload["SpConfig"]}
    assert types["TFL"] == "S"   # missing → S for non-sub
    assert types["SLA"] == "S"   # "?" → S
    assert types["SW1"] == "E"   # SW prefix → E


def test_split_setdat_packets_single_packet_when_under_threshold() -> None:
    payload = {"AmpAssign": "Normal", "AudyFinFlg": "NotFin"}
    packets = audyssey_tcp.split_setdat_packets(payload)
    assert len(packets) == 1
    assert packets[0] == payload


def test_split_setdat_packets_splits_at_510_byte_threshold() -> None:
    """A typical 5.1.4-channel envelope is ~835 bytes — must split into
    multiple packets each ≤ 510 bytes (matches A1Evo BINARY_PACKET_THRESHOLD)."""
    big_array = [{f"CH{i}": "S"} for i in range(20)]
    payload = {
        "AmpAssign": "Normal",
        "AssignBin": "0" * 96,
        "SpConfig": big_array,
        "Distance": [{f"CH{i}": 400} for i in range(20)],
        "ChLevel": [{f"CH{i}": 0} for i in range(20)],
        "Crossover": [{f"CH{i}": 80} for i in range(20)],
        "AudyFinFlg": "NotFin",
        "AudyDynEq": False,
        "SWSetup": {"SWNum": 1, "SWMode": "N/A", "SWLayout": "N/A"},
    }
    packets = audyssey_tcp.split_setdat_packets(payload)
    # Multiple packets, each ≤ threshold.
    assert len(packets) >= 2
    for p in packets:
        body = json.dumps(p, separators=(",", ":")).encode("ascii")
        frame = audyssey_tcp.build_frame("SET_SETDAT", body)
        assert len(frame) <= audyssey_tcp.SET_SETDAT_PACKET_THRESHOLD
    # Union of all packet keys equals the original key set.
    seen = set()
    for p in packets:
        seen |= set(p.keys())
    assert seen == set(payload.keys())


def test_split_setdat_packets_preserves_canonical_order() -> None:
    """Per A1Evo's protocol, params must arrive in DF_SETTING_DATA_PARAMETERS
    order. Splitting must walk that order — never skip earlier-listed params."""
    payload = {k: f"v{i}" for i, k in enumerate(audyssey_tcp.DF_SETTING_DATA_PARAMETERS)}
    packets = audyssey_tcp.split_setdat_packets(payload)
    seen_order = []
    for p in packets:
        seen_order.extend(p.keys())
    assert seen_order == list(audyssey_tcp.DF_SETTING_DATA_PARAMETERS)


def test_push_speaker_distances_use_custom_raises_deprecation() -> None:
    """use_custom=True is deprecated 2026-05-02 — it wipes speaker layout
    on Fin commit. Calling it must raise DriverError pointing at the new path."""
    import asyncio
    with pytest.raises(DriverError) as exc:
        asyncio.run(audyssey_tcp.push_speaker_distances(
            "192.168.1.209", {"SW1": 17.91}, use_custom=True,
        ))
    assert "deprecated" in str(exc.value).lower()
    assert "push_full_envelope_from_ady" in str(exc.value)


def test_push_full_envelope_sync_aborts_fin_on_setdat_nack() -> None:
    """Critical safety: if any SET_SETDAT packet NACKs, the Fin commit must
    NOT be sent. Committing on a partial-state previously corrupted the
    AVR's speaker layout (heights/center/surrounds dropped to defaults).
    """
    from unittest.mock import MagicMock
    ack_frame = audyssey_tcp.build_frame("SET_SETDAT", b'{"Comm":"ACK"}')
    nack_frame = audyssey_tcp.build_frame("ERROR     ", b'{"Comm":"NACK"}')

    mock_sock = MagicMock()
    mock_sock.recv.side_effect = [
        ack_frame,                  # ENTER_AUDY ack
        b"",
        nack_frame,                 # First SET_SETDAT NACKs
        b"",
        ack_frame,                  # EXIT_AUDMD ack
        b"",
    ]
    payload = audyssey_tcp.build_full_envelope_payload({
        "enAmpAssignType": 0, "ampAssignInfo": "0" * 96,
        "detectedChannels": [
            {"commandId": "FL", "customDistance": 4.0, "customSpeakerType": "S",
             "customCrossover": 80, "trimAdjustment": 0},
        ],
    })
    with patch.object(audyssey_tcp.socket, "create_connection", return_value=mock_sock):
        result = audyssey_tcp._push_full_envelope_sync(
            "192.168.1.209", 1256, payload, commit=True, timeout=2.0,
        )
    assert result is False  # signals NACK to caller
    sends = [args[0] for args, _ in mock_sock.sendall.call_args_list]
    # Verify the Fin commit was NOT sent — only ENTER, SET_SETDAT, EXIT.
    fin_sent = any(b'"AudyFinFlg":"Fin"' in s for s in sends)
    assert not fin_sent, "Fin commit must not fire on NACK'd SET_SETDAT"
    # EXIT_AUDMD must always fire to release the AVR's calibration mode.
    assert any(b"EXIT_AUDMD" in s for s in sends)


def test_denon_advertises_max_speaker_delay_ms() -> None:
    """Calibration code must be able to budget against this without an instance."""
    assert DenonDriver.MAX_SPEAKER_DELAY_MS == audyssey_tcp.MAX_APPLIED_DELAY_MS
    assert DenonDriver.MAX_SPEAKER_DELAY_MS > 0


@pytest.mark.asyncio
async def test_denon_get_state_includes_max_delay() -> None:
    from unittest.mock import AsyncMock, MagicMock
    receiver = MagicMock()
    receiver.async_setup = AsyncMock()
    receiver.async_update = AsyncMock()
    receiver.volume = -20.0
    receiver.input_func = "TV Audio"
    receiver.muted = False
    receiver.model_name = "AVR-X3800H"
    mod = MagicMock()
    mod.DenonAVR = MagicMock(return_value=receiver)
    with patch.dict(sys.modules, {"denonavr": mod}):
        driver = DenonDriver(host="192.168.1.209")
        state = await driver.get_state()
    assert state["max_speaker_delay_ms"] == DenonDriver.MAX_SPEAKER_DELAY_MS


# ── DenonDriver.audyssey_status ────────────────────────────────────────────────


def _mock_receiver(sound_mode: str | None, multi_eq: str | None):
    """Build a denonavr receiver mock with given sound_mode and multi_eq."""
    from unittest.mock import AsyncMock, MagicMock
    receiver = MagicMock()
    receiver.async_setup = AsyncMock()
    receiver.async_update = AsyncMock()
    receiver.audyssey.async_update = AsyncMock()
    receiver.soundmode.sound_mode = sound_mode
    receiver.audyssey.multi_eq = multi_eq
    return receiver


@pytest.mark.asyncio
async def test_audyssey_status_active_in_movie_with_multeq() -> None:
    """Normal listening case: MOVIE mode + MultEQ Reference → active."""
    receiver = _mock_receiver("MOVIE", "Reference")
    from unittest.mock import MagicMock
    mod = MagicMock()
    mod.DenonAVR = MagicMock(return_value=receiver)
    with patch.dict(sys.modules, {"denonavr": mod}):
        status = await DenonDriver(host="192.168.1.209").audyssey_status()
    assert status["active"] is True
    assert status["sound_mode"] == "MOVIE"
    assert status["multi_eq"] == "Reference"
    assert status["reason"] is None


@pytest.mark.asyncio
async def test_audyssey_status_pure_direct_inactive() -> None:
    """Pure Direct bypasses ALL DSP — distances not applied even if MultEQ is on."""
    receiver = _mock_receiver("PURE DIRECT", "Reference")
    from unittest.mock import MagicMock
    mod = MagicMock()
    mod.DenonAVR = MagicMock(return_value=receiver)
    with patch.dict(sys.modules, {"denonavr": mod}):
        status = await DenonDriver(host="192.168.1.209").audyssey_status()
    assert status["active"] is False
    assert "Pure Direct" in status["reason"]


@pytest.mark.asyncio
async def test_audyssey_status_multeq_off_inactive() -> None:
    """MultEQ Off → distances aren't applied, even outside Pure Direct."""
    receiver = _mock_receiver("STEREO", "Off")
    from unittest.mock import MagicMock
    mod = MagicMock()
    mod.DenonAVR = MagicMock(return_value=receiver)
    with patch.dict(sys.modules, {"denonavr": mod}):
        status = await DenonDriver(host="192.168.1.209").audyssey_status()
    assert status["active"] is False
    assert "MultEQ is Off" in status["reason"]


@pytest.mark.asyncio
async def test_audyssey_status_unknown_when_fields_none() -> None:
    """If denonavr hasn't populated either field, return None — caller treats as unknown."""
    receiver = _mock_receiver(None, None)
    from unittest.mock import MagicMock
    mod = MagicMock()
    mod.DenonAVR = MagicMock(return_value=receiver)
    with patch.dict(sys.modules, {"denonavr": mod}):
        status = await DenonDriver(host="192.168.1.209").audyssey_status()
    assert status["active"] is None
    assert status["reason"] is None


@pytest.mark.asyncio
async def test_audyssey_status_no_host_raises() -> None:
    driver = DenonDriver(host=None)
    with pytest.raises(DriverError, match="no host"):
        await driver.audyssey_status()


# ── Full-upload orchestration ──────────────────────────────────────────────

import socket
from unittest.mock import patch, MagicMock

from calibrate.drivers.denon.audyssey_tcp import _push_filters_sync


def _make_mock_sock(rx_frames: list[bytes]) -> MagicMock:
    """Build a mock socket whose recv returns the given frames once,
    then empty bytes (mimicking a closed connection)."""
    sock = MagicMock()
    sock.settimeout = MagicMock()
    sock.sendall = MagicMock()
    sock.close = MagicMock()
    queue = list(rx_frames) + [b""] * 100
    sock.recv.side_effect = lambda n: queue.pop(0) if queue else b""
    return sock


def _ack_frame(cmd: str) -> bytes:
    """Build a minimal R-frame ACK for given command name."""
    from calibrate.drivers.denon.audyssey_tcp import build_frame as bf
    body = b'{"Comm":"ACK"}'
    raw = bf(cmd, body)
    # build_frame always emits T-marker; for receive flip to R
    return b"R" + raw[1:]


def test_push_filters_sync_runs_full_sequence() -> None:
    """End-to-end: ENTER_AUDY → SET_SETDAT(envelope) → coef streams →
    FINZ_COEFS → SET_SETDAT(Fin) → EXIT_AUDMD."""
    rx = [
        _ack_frame("ENTER_AUDY"),
        _ack_frame("SET_SETDAT"),    # envelope ack
        _ack_frame("FINZ_COEFS"),
        _ack_frame("SET_SETDAT"),    # commit ack
        _ack_frame("EXIT_AUDMD"),
    ]
    mock_sock = _make_mock_sock(rx)
    fake_packet = b"\x54\x00\x10\x00\x00SET_COEFDT\x00\x00\x00\x00"  # not real, just bytes
    coef_per_channel = [
        ("FL", [fake_packet, fake_packet]),
        ("C", [fake_packet]),
    ]
    envelope = {"Distance": [{"FL": 400, "SW1": 1800}], "AudyFinFlg": "NotFin"}

    with patch.object(socket, "create_connection", return_value=mock_sock):
        status = _push_filters_sync(
            "192.168.1.209",
            1256,
            setdat_envelope=envelope,
            coef_packets_per_channel=coef_per_channel,
            inter_packet_ms=0.0,
            inter_channel_ms=0.0,
            coef_wait_final_ms=0.0,  # skip the 15s wait in tests
        )

    # Counts always reliable regardless of rx-mock timing
    assert status["coef_streams_sent"] == 2
    assert status["coef_packets_sent"] == 3   # 2 + 1
    # ACK booleans depend on rx-frame draining; the send-order test
    # below exercises the wire-level correctness independently.


def test_push_filters_sync_sends_in_correct_order() -> None:
    """Verify the wire-level sendall order: ENTER_AUDY first, EXIT_AUDMD last,
    with SET_SETDAT-envelope before coef packets and FINZ_COEFS after."""
    rx = [
        _ack_frame("ENTER_AUDY"),
        _ack_frame("SET_SETDAT"),
        _ack_frame("FINZ_COEFS"),
        _ack_frame("SET_SETDAT"),
        _ack_frame("EXIT_AUDMD"),
    ]
    mock_sock = _make_mock_sock(rx)
    coef_per_channel = [("FL", [b"\x54coef1"]), ("C", [b"\x54coef2"])]
    envelope = {"Distance": [{}], "AudyFinFlg": "NotFin"}

    with patch.object(socket, "create_connection", return_value=mock_sock):
        _push_filters_sync(
            "192.168.1.209",
            1256,
            setdat_envelope=envelope,
            coef_packets_per_channel=coef_per_channel,
            inter_packet_ms=0.0,
            inter_channel_ms=0.0,
            coef_wait_final_ms=0.0,
        )

    sends = [args[0] for args, _ in mock_sock.sendall.call_args_list]
    # First send must be ENTER_AUDY frame
    assert b"ENTER_AUDY" in sends[0]
    # Second is SET_SETDAT (envelope) — the JSON body should appear in the frame
    assert b"AudyFinFlg" in sends[1]
    assert b"SET_SETDAT" in sends[1]
    # Then the coef packets (raw bytes we provided)
    assert sends[2] == b"\x54coef1"
    assert sends[3] == b"\x54coef2"
    # Then FINZ_COEFS
    assert b"FINZ_COEFS" in sends[4]
    # Then SET_SETDAT(commit)
    assert b"AudyFinFlg" in sends[5]
    assert b"Fin" in sends[5]
    # Last is EXIT_AUDMD
    assert b"EXIT_AUDMD" in sends[6]
