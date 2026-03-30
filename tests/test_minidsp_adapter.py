"""Unit tests for MinidspClient — HTTP adapter for minidspd.

All HTTP calls are intercepted with respx (same pattern as test_preflight.py).
No hardware or real network access is required.

The minidspd REST API uses a single POST /devices/{idx}/config endpoint for all
mutating operations. Each method builds a partial Config payload.
"""

import pytest
import httpx
import respx

from calibrate.adapters.minidsp import (
    MinidspClient,
    MinidspApiError,
    MAX_DELAY_MS,
    APF_RESERVED_SLOTS,
    ALIGNMENT_PEQ_SLOTS,
)

BASE = "http://localhost:5380"
CONFIG_PATH = f"{BASE}/devices/0/config"


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def client() -> MinidspClient:
    return MinidspClient("localhost", 5380)


# ── set_output_gain ────────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_set_output_gain_happy_path(client: MinidspClient) -> None:
    route = respx.post(CONFIG_PATH).mock(return_value=httpx.Response(200))
    await client.set_output_gain(0, -6.0)
    assert route.called
    body = route.calls[0].request.content
    assert b'"outputs"' in body
    assert b'"gain"' in body
    assert b'-6.0' in body


@respx.mock
@pytest.mark.asyncio
async def test_set_output_gain_sends_correct_index(client: MinidspClient) -> None:
    route = respx.post(CONFIG_PATH).mock(return_value=httpx.Response(200))
    await client.set_output_gain(2, -3.0)
    import json
    payload = json.loads(route.calls[0].request.content)
    assert payload["outputs"][0]["index"] == 2
    assert payload["outputs"][0]["gain"] == -3.0


# ── set_output_delay ──────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_set_output_delay_happy_path(client: MinidspClient) -> None:
    route = respx.post(CONFIG_PATH).mock(return_value=httpx.Response(200))
    await client.set_output_delay(0, 4.5)
    import json
    payload = json.loads(route.calls[0].request.content)
    delay = payload["outputs"][0]["delay"]
    # 4.5ms = 4_500_000 nanos
    assert delay["secs"] == 0
    assert delay["nanos"] == 4_500_000


@respx.mock
@pytest.mark.asyncio
async def test_set_output_delay_at_max_boundary(client: MinidspClient) -> None:
    """delay_ms == MAX_DELAY_MS is allowed (boundary value)."""
    respx.post(CONFIG_PATH).mock(return_value=httpx.Response(200))
    await client.set_output_delay(1, MAX_DELAY_MS)  # no error


@pytest.mark.asyncio
async def test_set_output_delay_out_of_range(client: MinidspClient) -> None:
    """delay_ms > MAX_DELAY_MS (30 ms) → ValueError before any HTTP call."""
    with pytest.raises(ValueError, match="exceeds hardware maximum"):
        await client.set_output_delay(0, MAX_DELAY_MS + 1.0)


# ── set_output_polarity ───────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_set_output_polarity_happy_path(client: MinidspClient) -> None:
    route = respx.post(CONFIG_PATH).mock(return_value=httpx.Response(200))
    await client.set_output_polarity(0, inverted=True)
    import json
    payload = json.loads(route.calls[0].request.content)
    assert payload["outputs"][0]["invert"] is True


@respx.mock
@pytest.mark.asyncio
async def test_set_output_polarity_not_supported(client: MinidspClient) -> None:
    """A 4xx from minidspd → MinidspApiError."""
    respx.post(CONFIG_PATH).mock(return_value=httpx.Response(400))

    with pytest.raises(MinidspApiError) as exc_info:
        await client.set_output_polarity(0, inverted=True)

    assert exc_info.value.status_code == 400
    assert "/devices/0/config" in exc_info.value.path


# ── set_output_peq ────────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_set_output_peq_happy_path(client: MinidspClient) -> None:
    route = respx.post(CONFIG_PATH).mock(return_value=httpx.Response(200))
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
async def test_set_output_peq_with_bypass(client: MinidspClient) -> None:
    """bypass field is included in PEQ entry, not in coeff."""
    route = respx.post(CONFIG_PATH).mock(return_value=httpx.Response(200))
    biquad = {"b0": 1.0, "b1": 0.0, "b2": 0.0, "a1": 0.0, "a2": 0.0, "bypass": True}
    await client.set_output_peq(0, 3, biquad)
    import json
    payload = json.loads(route.calls[0].request.content)
    peq = payload["outputs"][0]["peq"][0]
    assert peq["bypass"] is True
    assert "bypass" not in peq["coeff"]


# ── set_input_routing ─────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_set_input_routing_enabled(client: MinidspClient) -> None:
    """Route input 1 to outputs 0, 2, 3; disable output 1."""
    route = respx.post(CONFIG_PATH).mock(return_value=httpx.Response(200))
    await client.set_input_routing(1, {0: True, 1: False, 2: True, 3: True})
    import json
    payload = json.loads(route.calls[0].request.content)
    inp = payload["inputs"][0]
    assert inp["index"] == 1
    routing = {r["index"]: r["mute"] for r in inp["routing"]}
    assert routing[0] is False  # enabled → mute=False
    assert routing[1] is True   # disabled → mute=True
    assert routing[2] is False
    assert routing[3] is False


@respx.mock
@pytest.mark.asyncio
async def test_set_input_routing_partial(client: MinidspClient) -> None:
    """Partial routing only sends the specified outputs."""
    route = respx.post(CONFIG_PATH).mock(return_value=httpx.Response(200))
    await client.set_input_routing(0, {0: True})
    import json
    payload = json.loads(route.calls[0].request.content)
    assert len(payload["inputs"][0]["routing"]) == 1


# ── restore_all_gains ─────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_restore_all_gains_writes_zero(client: MinidspClient) -> None:
    """restore_all_gains must POST gain=0.0 for each output index."""
    route = respx.post(CONFIG_PATH).mock(return_value=httpx.Response(200))

    await client.restore_all_gains([0, 1])

    assert route.call_count == 2
    import json
    gains = [json.loads(c.request.content)["outputs"][0]["gain"] for c in route.calls]
    assert gains == [0.0, 0.0]


@respx.mock
@pytest.mark.asyncio
async def test_restore_all_gains_continues_on_partial_failure(client: MinidspClient) -> None:
    """If one gain restore fails, the rest still run."""
    call_count = 0

    def side_effect(req):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.ConnectError("refused")
        return httpx.Response(200)

    respx.post(CONFIG_PATH).mock(side_effect=side_effect)

    # Should not raise even though output 0 fails
    await client.restore_all_gains([0, 1])
    assert call_count == 2


# ── Error handling ────────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_api_error_on_5xx(client: MinidspClient) -> None:
    """5xx from minidspd → MinidspApiError."""
    respx.post(CONFIG_PATH).mock(return_value=httpx.Response(500))

    with pytest.raises(MinidspApiError) as exc_info:
        await client.set_output_gain(0, -6.0)

    assert exc_info.value.status_code == 500


@respx.mock
@pytest.mark.asyncio
async def test_connection_refused_propagates(client: MinidspClient) -> None:
    """httpx.ConnectError propagates without wrapping."""
    respx.post(CONFIG_PATH).mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(httpx.ConnectError):
        await client.set_output_gain(0, -6.0)


# ── get_devices ───────────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_get_devices_happy_path(client: MinidspClient) -> None:
    respx.get(f"{BASE}/devices").mock(
        return_value=httpx.Response(200, json=[{"url": "hid://001:006"}])
    )
    devices = await client.get_devices()
    assert len(devices) == 1
    assert devices[0]["url"] == "hid://001:006"
