"""Unit tests for MinidspClient — adapter for minidspd.

All I/O goes through the minidsp CLI (WebSocket transport). No HTTP.
CLI writes are mocked via AsyncMock on _run_minidsp_cli.
CLI status reads are mocked via AsyncMock on _get_status_via_cli.
"""

import pytest
from unittest.mock import AsyncMock, patch

from calibrate.adapters.minidsp import (
    MinidspClient,
    MinidspApiError,
    MAX_DELAY_MS,
    MAX_OUTPUT_INDEX,
    MAX_PRESET_INDEX,
    VALID_SOURCES,
)

_CLI_PATH = "calibrate.adapters.minidsp._run_minidsp_cli"
_STATUS_CLI = "calibrate.adapters.minidsp._get_status_via_cli"


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def client() -> MinidspClient:
    return MinidspClient("localhost", 5380)


# ── set_output_gain ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_set_output_gain_happy_path(client: MinidspClient) -> None:
    with patch(_CLI_PATH, new_callable=AsyncMock) as mock_cli:
        await client.set_output_gain(0, -6.0)
    mock_cli.assert_called_once_with("output", "0", "gain", "--", "-6.0")


@pytest.mark.asyncio
async def test_set_output_gain_sends_correct_index(client: MinidspClient) -> None:
    with patch(_CLI_PATH, new_callable=AsyncMock) as mock_cli:
        await client.set_output_gain(2, -3.0)
    mock_cli.assert_called_once_with("output", "2", "gain", "--", "-3.0")


@pytest.mark.asyncio
async def test_set_output_gain_error(client: MinidspClient) -> None:
    with patch(_CLI_PATH, new_callable=AsyncMock) as mock_cli:
        mock_cli.side_effect = MinidspApiError(1, "minidsp output 0 gain -- -6.0: error")
        with pytest.raises(MinidspApiError):
            await client.set_output_gain(0, -6.0)


# ── set_output_delay ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_set_output_delay_happy_path(client: MinidspClient) -> None:
    with patch(_CLI_PATH, new_callable=AsyncMock) as mock_cli:
        await client.set_output_delay(0, 4.5)
    mock_cli.assert_called_once_with("output", "0", "delay", "4.5")


@pytest.mark.asyncio
async def test_set_output_delay_out_of_range(client: MinidspClient) -> None:
    with pytest.raises(ValueError, match="exceeds hardware maximum"):
        await client.set_output_delay(0, MAX_DELAY_MS + 1.0)


@pytest.mark.asyncio
async def test_set_output_delay_at_max_boundary(client: MinidspClient) -> None:
    """delay_ms == MAX_DELAY_MS is allowed (boundary value)."""
    with patch(_CLI_PATH, new_callable=AsyncMock):
        await client.set_output_delay(1, MAX_DELAY_MS)  # no error


# ── set_output_polarity ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_set_output_polarity_happy_path(client: MinidspClient) -> None:
    with patch(_CLI_PATH, new_callable=AsyncMock) as mock_cli:
        await client.set_output_polarity(0, inverted=True)
    mock_cli.assert_called_once_with("output", "0", "invert", "on")


@pytest.mark.asyncio
async def test_set_output_polarity_not_inverted(client: MinidspClient) -> None:
    """inverted=False sends 'off'."""
    with patch(_CLI_PATH, new_callable=AsyncMock) as mock_cli:
        await client.set_output_polarity(0, inverted=False)
    mock_cli.assert_called_once_with("output", "0", "invert", "off")


# ── set_input_routing ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_set_input_routing_enabled(client: MinidspClient) -> None:
    """Route input 1 to outputs 0, 2, 3; disable output 1 via CLI."""
    with patch(_CLI_PATH, new_callable=AsyncMock) as mock_cli:
        await client.set_input_routing(1, {0: True, 1: False, 2: True, 3: True})
    calls = [c.args for c in mock_cli.call_args_list]
    assert ("input", "1", "routing", "0", "enable", "true") in calls
    assert ("input", "1", "routing", "1", "enable", "false") in calls
    assert ("input", "1", "routing", "2", "enable", "true") in calls
    assert ("input", "1", "routing", "3", "enable", "true") in calls


@pytest.mark.asyncio
async def test_set_input_routing_mute_semantics(client: MinidspClient) -> None:
    """enabled=False → 'enable false'; enabled=True → 'enable true'."""
    with patch(_CLI_PATH, new_callable=AsyncMock) as mock_cli:
        await client.set_input_routing(0, {0: False, 1: True})
    calls = {c.args[3]: c.args[5] for c in mock_cli.call_args_list}
    assert calls["0"] == "false"
    assert calls["1"] == "true"


@pytest.mark.asyncio
async def test_set_input_routing_partial(client: MinidspClient) -> None:
    """Partial routing only calls CLI for the specified outputs."""
    with patch(_CLI_PATH, new_callable=AsyncMock) as mock_cli:
        await client.set_input_routing(0, {0: True})
    assert mock_cli.call_count == 1


# ── restore_all_gains ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_restore_all_gains_unmutes_outputs(client: MinidspClient) -> None:
    """restore_all_gains sends 'mute off' for each output index via CLI."""
    with patch(_CLI_PATH, new_callable=AsyncMock) as mock_cli:
        await client.restore_all_gains([0, 1])
    assert mock_cli.call_count == 2
    calls = [c.args for c in mock_cli.call_args_list]
    assert ("output", "0", "mute", "off") in calls
    assert ("output", "1", "mute", "off") in calls


@pytest.mark.asyncio
async def test_restore_all_gains_continues_on_partial_failure(client: MinidspClient) -> None:
    """If one unmute fails, the rest still run (errors are swallowed)."""
    call_count = 0

    async def side_effect(*args):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise MinidspApiError(1, "minidsp output 0 mute off: error")

    with patch(_CLI_PATH, side_effect=side_effect):
        # Should not raise even though output 0 fails
        await client.restore_all_gains([0, 1])
    assert call_count == 2


@pytest.mark.asyncio
async def test_restore_all_gains_partial_failure(client: MinidspClient) -> None:
    """Errors from individual outputs are swallowed; all outputs attempted."""
    attempted = []

    async def side_effect(*args):
        attempted.append(args[1])  # output index
        if args[1] == "0":
            raise MinidspApiError(1, "minidsp output 0 mute off: error")

    with patch(_CLI_PATH, side_effect=side_effect):
        await client.restore_all_gains([0, 1])
    assert "0" in attempted
    assert "1" in attempted


# ── switch_preset ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_switch_preset_out_of_range(client: MinidspClient) -> None:
    with pytest.raises(ValueError, match="out of range"):
        await client.switch_preset(MAX_PRESET_INDEX + 1)


@pytest.mark.asyncio
async def test_switch_preset_negative(client: MinidspClient) -> None:
    with pytest.raises(ValueError, match="out of range"):
        await client.switch_preset(-1)


@pytest.mark.asyncio
async def test_switch_preset_happy_path(client: MinidspClient) -> None:
    with patch(_CLI_PATH, new_callable=AsyncMock) as mock_cli:
        await client.switch_preset(2)
    mock_cli.assert_called_once_with("preset", "2")


@pytest.mark.asyncio
async def test_switch_preset_api_error(client: MinidspClient) -> None:
    with patch(_CLI_PATH, new_callable=AsyncMock, side_effect=MinidspApiError(1, "preset 1")):
        with pytest.raises(MinidspApiError):
            await client.switch_preset(1)


# ── switch_source ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_switch_source_invalid(client: MinidspClient) -> None:
    with pytest.raises(ValueError, match="invalid"):
        await client.switch_source("HDMI")


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["Analog", "Toslink", "Usb"])
async def test_switch_source_happy_path(client: MinidspClient, source: str) -> None:
    with patch(_CLI_PATH, new_callable=AsyncMock) as mock_cli:
        await client.switch_source(source)
    mock_cli.assert_called_once_with("source", source.lower())


@pytest.mark.asyncio
async def test_switch_source_api_error(client: MinidspClient) -> None:
    with patch(_CLI_PATH, new_callable=AsyncMock, side_effect=MinidspApiError(1, "source toslink")):
        with pytest.raises(MinidspApiError):
            await client.switch_source("Toslink")


# ── get_device_status ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_device_status_happy_path(client: MinidspClient) -> None:
    payload = {
        "master": {"preset": 0, "source": "Analog", "volume": -30.0, "mute": False},
        "input_levels": [],
        "output_levels": [],
    }
    with patch(_STATUS_CLI, new_callable=AsyncMock, return_value=payload):
        status = await client.get_device_status()
    assert status["master"]["preset"] == 0
    assert status["master"]["source"] == "Analog"


@pytest.mark.asyncio
async def test_get_device_status_api_error(client: MinidspClient) -> None:
    with patch(_STATUS_CLI, new_callable=AsyncMock,
               side_effect=MinidspApiError(1, "minidsp status: connection refused")):
        with pytest.raises(MinidspApiError) as exc_info:
            await client.get_device_status()
    assert exc_info.value.status_code == 1


# ── get_devices ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_devices_happy_path(client: MinidspClient) -> None:
    status = {"master": {}, "input_levels": [], "output_levels": []}
    with patch(_STATUS_CLI, new_callable=AsyncMock, return_value=status):
        result = await client.get_devices()
    assert result[0]["product_name"] == "miniDSP 2x4 HD"


@pytest.mark.asyncio
async def test_get_devices_raises_on_cli_failure(client: MinidspClient) -> None:
    """CLI failure propagates as MinidspApiError (not silently returns [])."""
    with patch(_STATUS_CLI, new_callable=AsyncMock,
               side_effect=MinidspApiError(1, "minidsp status: no device")):
        with pytest.raises(MinidspApiError):
            await client.get_devices()


# ── Error handling ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_api_error_on_cli_failure(client: MinidspClient) -> None:
    """Non-zero CLI exit code → MinidspApiError."""
    with patch(_CLI_PATH, new_callable=AsyncMock) as mock_cli:
        mock_cli.side_effect = MinidspApiError(1, "minidsp output 0 gain -- -6.0: device error")
        with pytest.raises(MinidspApiError) as exc_info:
            await client.set_output_gain(0, -6.0)
    assert exc_info.value.status_code == 1


@pytest.mark.asyncio
async def test_cli_exception_propagates(client: MinidspClient) -> None:
    """Unexpected CLI exceptions propagate without wrapping."""
    with patch(_CLI_PATH, new_callable=AsyncMock) as mock_cli:
        mock_cli.side_effect = RuntimeError("subprocess crash")
        with pytest.raises(RuntimeError):
            await client.set_output_gain(0, -6.0)


# ── constants ──────────────────────────────────────────────────────────────────

def test_valid_sources_contains_expected() -> None:
    assert "Analog" in VALID_SOURCES
    assert "Toslink" in VALID_SOURCES
    assert "Usb" in VALID_SOURCES


def test_max_preset_index_is_three() -> None:
    assert MAX_PRESET_INDEX == 3


# ── Output index validation ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_output_index_negative_rejected(client: MinidspClient) -> None:
    with pytest.raises(ValueError, match="out of range"):
        await client.set_output_gain(-1, 0.0)


@pytest.mark.asyncio
async def test_output_index_too_high_rejected(client: MinidspClient) -> None:
    with pytest.raises(ValueError, match="out of range"):
        await client.set_output_gain(MAX_OUTPUT_INDEX + 1, 0.0)


@pytest.mark.asyncio
async def test_delay_negative_rejected(client: MinidspClient) -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        await client.set_output_delay(0, -1.0)


@pytest.mark.asyncio
async def test_polarity_invalid_output_rejected(client: MinidspClient) -> None:
    with pytest.raises(ValueError, match="out of range"):
        await client.set_output_polarity(5, inverted=True)


@pytest.mark.asyncio
async def test_delay_invalid_output_rejected(client: MinidspClient) -> None:
    with pytest.raises(ValueError, match="out of range"):
        await client.set_output_delay(10, 1.0)


@pytest.mark.asyncio
async def test_peq_cli_invalid_output_rejected(client: MinidspClient) -> None:
    """set_output_peq_cli rejects output index out of range."""
    with pytest.raises(ValueError, match="out of range"):
        await client.set_output_peq_cli(5, [])


# ── mute_outputs error propagation ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_mute_outputs_raises_on_cli_failure(client: MinidspClient) -> None:
    """If the first mute CLI call fails, MinidspApiError propagates immediately."""
    async def side_effect(*args):
        if args[1] == "0":
            raise MinidspApiError(1, "minidsp output 0 mute on: error")

    with patch(_CLI_PATH, side_effect=side_effect):
        with pytest.raises(MinidspApiError):
            await client.mute_outputs([0, 1])


# ── CLI serialisation lock ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cli_lock_serialises_concurrent_calls(client: MinidspClient) -> None:
    """Concurrent CLI calls must be serialised — no two minidsp processes at once.

    The miniDSP 2x4 HD firmware silently drops commands when they arrive
    faster than its internal commit cycle.  The _cli_lock in _run_minidsp_cli
    prevents this.

    We mock asyncio.create_subprocess_exec (not _run_minidsp_cli) so the
    real lock and delay logic runs.
    """
    import asyncio
    from unittest.mock import MagicMock

    call_order: list[str] = []
    call_count = 0

    async def fake_subprocess(*args, **kwargs):
        nonlocal call_count
        idx = call_count
        call_count += 1
        call_order.append(f"start:{idx}")
        await asyncio.sleep(0.01)
        call_order.append(f"end:{idx}")
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.kill = MagicMock()
        proc.wait = AsyncMock()
        return proc

    with patch("calibrate.adapters.minidsp.asyncio.create_subprocess_exec",
               side_effect=fake_subprocess):
        with patch("calibrate.adapters.minidsp.CLI_COMMAND_DELAY_S", 0.0):
            await asyncio.gather(
                client.set_output_gain(0, 0.0),
                client.set_output_gain(1, 0.0),
                client.set_output_gain(2, 0.0),
            )

    # Verify serialisation: each "end" must come before the next "start"
    starts = [i for i, v in enumerate(call_order) if v.startswith("start")]
    ends = [i for i, v in enumerate(call_order) if v.startswith("end")]
    assert len(starts) == 3, f"Expected 3 calls, got {len(starts)}: {call_order}"
    for s, e in zip(starts[1:], ends[:-1]):
        assert e < s, (
            f"Commands overlapped: {call_order}"
        )
