"""Async HTTP client for the bare-metal avr-measurement service.

The Docker MCP server uses this client instead of importing MeasurementEngine
directly, so no PipeWire socket mount is needed inside the container.

The service runs on the Pi host at localhost:8767. Because the container uses
--network=host, ``localhost`` inside the container is the Pi's loopback.

Usage::

    client = MeasurementServiceClient()
    fr = await client.measure(freq_min=20, freq_max=200, route="usb")
    devices = await client.list_devices()
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)


class MeasurementServiceError(RuntimeError):
    """Raised when the avr-measurement service is unreachable or returns an error."""


class MeasurementServiceClient:
    """Async HTTP client for the bare-metal measurement service."""

    def __init__(self, base_url: str = "http://localhost:8767"):
        self.base_url = base_url.rstrip("/")

    # ── Internal helper ────────────────────────────────────────────────────────

    async def _post(self, path: str, body: dict) -> dict:
        """POST to the service, raise MeasurementServiceError on failure."""
        try:
            import httpx
        except ImportError as exc:
            raise MeasurementServiceError(
                "httpx is required for MeasurementServiceClient — pip install httpx"
            ) from exc

        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                resp = await client.post(url, json=body)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise MeasurementServiceError(
                f"avr-measurement service unreachable at {self.base_url} — "
                "is avr-measurement.service running on the Pi?"
            ) from exc

        if resp.status_code != 200:
            try:
                detail = resp.json().get("error", resp.text)
            except Exception:
                detail = resp.text
            raise MeasurementServiceError(
                f"avr-measurement service error ({resp.status_code}): {detail}"
            )

        data = resp.json()
        if not data.get("ok"):
            raise MeasurementServiceError(
                f"avr-measurement service returned error: {data.get('error', data)}"
            )
        return data

    async def _get(self, path: str) -> dict | list:
        """GET from the service, raise MeasurementServiceError on failure."""
        try:
            import httpx
        except ImportError as exc:
            raise MeasurementServiceError(
                "httpx is required for MeasurementServiceClient — pip install httpx"
            ) from exc

        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(url)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise MeasurementServiceError(
                f"avr-measurement service unreachable at {self.base_url} — "
                "is avr-measurement.service running on the Pi?"
            ) from exc

        if resp.status_code != 200:
            try:
                detail = resp.json().get("error", resp.text)
            except Exception:
                detail = resp.text
            raise MeasurementServiceError(
                f"avr-measurement service error ({resp.status_code}): {detail}"
            )

        return resp.json()

    # ── Public API ─────────────────────────────────────────────────────────────

    async def health(self) -> dict:
        """Return health status dict from the service."""
        result = await self._get("/health")
        return result  # type: ignore[return-value]

    async def list_devices(self) -> list[dict]:
        """Return list of sounddevice device dicts from the Pi host."""
        result = await self._get("/devices")
        return result  # type: ignore[return-value]

    async def find_umik_device(
        self, name_substring: str = "UMIK"
    ) -> tuple[int | None, dict | None]:
        """Find the UMIK microphone in the Pi host's device list.

        Returns (index, device_dict) or (None, None) if not found.
        """
        from .measurement import _find_umik_device
        devices = await self.list_devices()
        idx = _find_umik_device(devices, name_substring=name_substring)
        if idx is None:
            return None, None
        return idx, devices[idx]

    async def measure(
        self,
        freq_min: int | None = None,
        freq_max: int | None = None,
        route: str | None = None,
        out_channel_override: int | None = None,
        direct_path_window_ms: float | None = None,
    ):
        """Run a log-sweep measurement. Returns FrequencyResponse."""
        from .measurement import FrequencyResponse
        data = await self._post("/measure", {
            "freq_min": freq_min,
            "freq_max": freq_max,
            "route": route,
            "out_channel_override": out_channel_override,
            "direct_path_window_ms": direct_path_window_ms,
        })
        return FrequencyResponse.from_json(data["result"])

    async def measure_spl_pink(
        self,
        channel: int,
        duration_s: float = 10.0,
        level_dbfs: float = -20.0,
        weighting: str = "C",
        n_output_channels: int = 6,
        integration_time_s: float = 1.0,
        cal_path: str | None = None,
    ) -> dict:
        """Play pink noise on one HDMI channel and return SPL measurement."""
        data = await self._post("/measure_spl_pink", {
            "channel": channel,
            "duration_s": duration_s,
            "level_dbfs": level_dbfs,
            "weighting": weighting,
            "n_output_channels": n_output_channels,
            "integration_time_s": integration_time_s,
            "cal_path": cal_path,
        })
        return data["result"]

    async def measure_impulse_ir(
        self,
        n_averages: int = 64,
        record_duration_s: float = 2.5,
        impulse_amplitude: float = 0.9,
    ) -> tuple[list[float], int]:
        """Measure room impulse response. Returns (ir_samples, sample_rate)."""
        data = await self._post("/measure_impulse_ir", {
            "n_averages": n_averages,
            "record_duration_s": record_duration_s,
            "impulse_amplitude": impulse_amplitude,
        })
        result = data["result"]
        return result["ir_samples"], result["sample_rate"]

    async def play_and_measure_fft(
        self,
        channel_assignments: dict,
        duration_s: float = 2.0,
        amplitude: float = 0.5,
        fft_size: int = 8192,
        n_channels: int = 6,
        sample_rate: int = 48000,
    ) -> dict:
        """Play multitone signal and return FFT analysis."""
        data = await self._post("/play_and_measure_fft", {
            "channel_assignments": channel_assignments,
            "duration_s": duration_s,
            "amplitude": amplitude,
            "fft_size": fft_size,
            "n_channels": n_channels,
            "sample_rate": sample_rate,
        })
        return data["result"]
