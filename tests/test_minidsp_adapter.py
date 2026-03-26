"""Unit tests for MinidspClient — HTTP adapter for minidspd.

All HTTP calls are intercepted with respx (same pattern as test_preflight.py).
No hardware or real network access is required.
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


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def client() -> MinidspClient:
    return MinidspClient("localhost", 5380)


# ── Happy-path tests ───────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_set_output_gain_happy_path(client: MinidspClient) -> None:
    respx.put(f"{BASE}/output/0/gain").mock(return_value=httpx.Response(200))
    await client.set_output_gain(0, -6.0)  # no error raised


@respx.mock
@pytest.mark.asyncio
async def test_set_output_delay_happy_path(client: MinidspClient) -> None:
    respx.put(f"{BASE}/output/0/delay").mock(return_value=httpx.Response(200))
    await client.set_output_delay(0, 4.5)  # no error raised


@respx.mock
@pytest.mark.asyncio
async def test_set_output_polarity_happy_path(client: MinidspClient) -> None:
    respx.put(f"{BASE}/output/0/polarity").mock(return_value=httpx.Response(200))
    await client.set_output_polarity(0, inverted=True)  # no error raised


@respx.mock
@pytest.mark.asyncio
async def test_restore_all_gains_writes_zero(client: MinidspClient) -> None:
    """restore_all_gains must call set_output_gain(i, 0.0) for each index."""
    route0 = respx.put(f"{BASE}/output/0/gain").mock(return_value=httpx.Response(200))
    route1 = respx.put(f"{BASE}/output/1/gain").mock(return_value=httpx.Response(200))

    await client.restore_all_gains([0, 1])

    assert route0.called
    assert route1.called
    # Verify the payload sent was 0.0 for both (httpx serialises without spaces)
    assert b'"gain"' in route0.calls[0].request.content
    assert b'0.0' in route0.calls[0].request.content
    assert b'"gain"' in route1.calls[0].request.content


# ── Error handling tests ───────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_set_output_polarity_not_supported(client: MinidspClient) -> None:
    """A 404 from minidspd (hardware doesn't support polarity) → MinidspApiError."""
    respx.put(f"{BASE}/output/0/polarity").mock(return_value=httpx.Response(404))

    with pytest.raises(MinidspApiError) as exc_info:
        await client.set_output_polarity(0, inverted=True)

    assert exc_info.value.status_code == 404
    assert "/output/0/polarity" in exc_info.value.path


@pytest.mark.asyncio
async def test_set_output_delay_out_of_range(client: MinidspClient) -> None:
    """delay_ms > MAX_DELAY_MS (30 ms) → ValueError before any HTTP call."""
    with pytest.raises(ValueError, match="exceeds hardware maximum"):
        await client.set_output_delay(0, MAX_DELAY_MS + 1.0)


@pytest.mark.asyncio
async def test_set_output_peq_invalid_slot(client: MinidspClient) -> None:
    """Slot 0 is reserved for APF → ValueError before any HTTP call."""
    reserved_slot = next(iter(APF_RESERVED_SLOTS))  # 0
    with pytest.raises(ValueError, match="reserved for APF"):
        await client.set_output_peq(0, reserved_slot, {"bypass": False})


@pytest.mark.asyncio
async def test_set_output_delay_at_max_boundary(client: MinidspClient) -> None:
    """delay_ms == MAX_DELAY_MS is allowed (boundary value)."""
    with respx.mock:
        respx.put(f"{BASE}/output/1/delay").mock(return_value=httpx.Response(200))
        await client.set_output_delay(1, MAX_DELAY_MS)  # no error


@respx.mock
@pytest.mark.asyncio
async def test_connection_refused_propagates(client: MinidspClient) -> None:
    """httpx.ConnectError propagates without wrapping."""
    respx.put(f"{BASE}/output/0/gain").mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(httpx.ConnectError):
        await client.set_output_gain(0, -6.0)
