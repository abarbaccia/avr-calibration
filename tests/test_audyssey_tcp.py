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
