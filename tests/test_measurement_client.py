"""Tests for calibrate.measurement_client.MeasurementServiceClient."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from calibrate.measurement_client import MeasurementServiceClient


def _mock_httpx(captured: dict):
    """Build a patched httpx.AsyncClient that records the timeout it was
    constructed with and returns a successful 200 response."""

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "ok": True,
        "result": {"ir_samples": [0.0, 1.0, 0.0], "sample_rate": 48000},
    }

    httpx_instance = AsyncMock()
    httpx_instance.post.return_value = mock_response

    def _ctor(*args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=httpx_instance)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    return _ctor


@pytest.mark.asyncio
async def test_post_default_timeout_is_300():
    """_post with no explicit timeout uses the 300 s default."""
    captured: dict = {}
    with patch("httpx.AsyncClient", side_effect=_mock_httpx(captured)):
        client = MeasurementServiceClient()
        await client._post("/measure", {})
    assert captured["timeout"] == 300.0


@pytest.mark.asyncio
async def test_post_honors_explicit_timeout():
    """_post passes an explicit timeout through to httpx.AsyncClient."""
    captured: dict = {}
    with patch("httpx.AsyncClient", side_effect=_mock_httpx(captured)):
        client = MeasurementServiceClient()
        await client._post("/measure", {}, timeout=1234.0)
    assert captured["timeout"] == 1234.0


@pytest.mark.asyncio
async def test_measure_impulse_ir_uses_long_timeout_for_large_n():
    """A 64-shot impulse run must compute a timeout well above 300 s so the
    HTTP client does not ReadTimeout mid-measurement."""
    captured: dict = {}
    with patch("httpx.AsyncClient", side_effect=_mock_httpx(captured)):
        client = MeasurementServiceClient()
        await client.measure_impulse_ir(n_averages=64, record_duration_s=2.5)
    # 64 * 2.5 * 5.0 + 60 = 860 s
    expected = max(300.0, 64 * 2.5 * 5.0 + 60.0)
    assert captured["timeout"] == expected
    assert captured["timeout"] > 300.0


@pytest.mark.asyncio
async def test_measure_impulse_ir_small_n_keeps_floor_timeout():
    """Small runs still clamp to the 300 s floor."""
    captured: dict = {}
    with patch("httpx.AsyncClient", side_effect=_mock_httpx(captured)):
        client = MeasurementServiceClient()
        await client.measure_impulse_ir(n_averages=1, record_duration_s=2.5)
    assert captured["timeout"] == 300.0
