"""Unit tests for MinidspClient — HTTP adapter for minidspd.

HTTP-based methods use respx mocking. CLI-based methods (set_output_gain,
set_output_delay, set_output_polarity, set_input_routing, restore_all_gains,
mute_outputs, unmute_outputs) mock _run_minidsp_cli with AsyncMock since they
no longer touch the HTTP transport.

The minidspd REST API uses POST /devices/{idx}/config for EQ/routing mutations
and POST /devices/{idx} with a MasterStatus body for preset/source switching.
The /preset/:n and /source/:s path endpoints do not exist in minidspd 0.1.x.
"""

import pytest
import httpx
import respx
from unittest.mock import AsyncMock, patch

from calibrate.adapters.minidsp import (
    MinidspClient,
    MinidspApiError,
    MAX_DELAY_MS,
    MAX_OUTPUT_INDEX,
    MAX_PRESET_INDEX,
    VALID_SOURCES,
    APF_RESERVED_SLOTS,
    ALIGNMENT_PEQ_SLOTS,
)

_CLI_PATH = "calibrate.adapters.minidsp._run_minidsp_cli"

BASE = "http://localhost:5380"
CONFIG_URL = f"{BASE}/devices/0/config"
DEVICE_URL = f"{BASE}/devices/0"


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


# ── set_output_peq ────────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_set_output_peq_happy_path(client: MinidspClient) -> None:
    route = respx.post(CONFIG_URL).mock(return_value=httpx.Response(200))
    biquad = {"b0": 1.0, "b1": 0.0, "b2": 0.0, "a1": 0.0, "a2": 0.0}
    await client.set_output_peq(0, 2, biquad)
    import json
    payload = json.loads(route.calls[0].request.content)
    peq = payload["outputs"][0]["peq"][0]
    assert peq["index"] == 2
    assert peq["coeff"]["b0"] == 1.0


@pytest.mark.asyncio
async def test_set_output_peq_invalid_slot(client: MinidspClient) -> None:
    """Slot 0 is reserved for APF → ValueError before any HTTP call."""
    reserved_slot = next(iter(APF_RESERVED_SLOTS))  # 0
    with pytest.raises(ValueError, match="reserved for APF"):
        await client.set_output_peq(0, reserved_slot, {"b0": 1.0})


@respx.mock
@pytest.mark.asyncio
async def test_set_output_peq_valid_slot(client: MinidspClient) -> None:
    import json
    route = respx.post(CONFIG_URL).mock(return_value=httpx.Response(200))
    slot = next(iter(ALIGNMENT_PEQ_SLOTS))
    biquad = {"b0": 1.0, "b1": 0.0, "b2": 0.0, "a1": 0.0, "a2": 0.0}
    await client.set_output_peq(0, slot, biquad)
    payload = json.loads(route.calls[0].request.content)
    assert payload["outputs"][0]["peq"][0]["index"] == slot


@respx.mock
@pytest.mark.asyncio
async def test_set_output_peq_with_bypass(client: MinidspClient) -> None:
    """bypass field is included in PEQ entry, not in coeff."""
    route = respx.post(CONFIG_URL).mock(return_value=httpx.Response(200))
    biquad = {"b0": 1.0, "b1": 0.0, "b2": 0.0, "a1": 0.0, "a2": 0.0, "bypass": True}
    await client.set_output_peq(0, 3, biquad)
    import json
    payload = json.loads(route.calls[0].request.content)
    peq = payload["outputs"][0]["peq"][0]
    assert peq["bypass"] is True
    assert "bypass" not in peq["coeff"]


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


@respx.mock
@pytest.mark.asyncio
async def test_switch_preset_happy_path(client: MinidspClient) -> None:
    route = respx.post(DEVICE_URL).mock(return_value=httpx.Response(200))
    await client.switch_preset(2)
    assert route.called
    import json
    assert json.loads(route.calls[0].request.content) == {"preset": 2}


@respx.mock
@pytest.mark.asyncio
async def test_switch_preset_api_error(client: MinidspClient) -> None:
    respx.post(DEVICE_URL).mock(return_value=httpx.Response(500))
    with pytest.raises(MinidspApiError) as exc_info:
        await client.switch_preset(1)
    assert exc_info.value.status_code == 500


# ── switch_source ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_switch_source_invalid(client: MinidspClient) -> None:
    with pytest.raises(ValueError, match="invalid"):
        await client.switch_source("HDMI")


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["Analog", "Toslink", "USB"])
async def test_switch_source_happy_path(client: MinidspClient, source: str) -> None:
    route = respx.post(DEVICE_URL).mock(return_value=httpx.Response(200))
    await client.switch_source(source)
    assert route.called
    import json
    assert json.loads(route.calls[0].request.content) == {"source": source}


@respx.mock
@pytest.mark.asyncio
async def test_switch_source_api_error(client: MinidspClient) -> None:
    respx.post(DEVICE_URL).mock(return_value=httpx.Response(502))
    with pytest.raises(MinidspApiError) as exc_info:
        await client.switch_source("Toslink")
    assert exc_info.value.status_code == 502


# ── get_device_status ──────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_get_device_status_happy_path(client: MinidspClient) -> None:
    payload = {
        "master": {"preset": 0, "source": "Analog", "volume": -30.0, "mute": False},
        "input_levels": [],
        "output_levels": [],
    }
    respx.get(DEVICE_URL).mock(return_value=httpx.Response(200, json=payload))
    status = await client.get_device_status()
    assert status["master"]["preset"] == 0
    assert status["master"]["source"] == "Analog"


@respx.mock
@pytest.mark.asyncio
async def test_get_device_status_api_error(client: MinidspClient) -> None:
    respx.get(DEVICE_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(MinidspApiError) as exc_info:
        await client.get_device_status()
    assert exc_info.value.status_code == 503


# ── get_devices ────────────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_get_devices_happy_path(client: MinidspClient) -> None:
    devices = [{"product_name": "miniDSP 2x4 HD", "version": {"serial": "12345"}}]
    respx.get(f"{BASE}/devices").mock(return_value=httpx.Response(200, json=devices))
    result = await client.get_devices()
    assert result[0]["product_name"] == "miniDSP 2x4 HD"


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
    assert "USB" in VALID_SOURCES


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
async def test_peq_invalid_output_rejected(client: MinidspClient) -> None:
    with pytest.raises(ValueError, match="out of range"):
        await client.set_output_peq(5, 2, {"b0": 1.0})


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
