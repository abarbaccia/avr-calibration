"""HTTP client for minidspd — per-output gain, delay, polarity, PEQ, and routing.

minidspd exposes a local REST API.  MinidspClient wraps the config endpoint
used by the sub-alignment algorithm and signal routing setup.

API (relative to http://{host}:{port}):
  GET  /devices                         → list connected devices
  GET  /devices/{idx}                   → master status (preset, source, volume, mute)
  POST /devices/{idx}                   → patch master status
  POST /devices/{idx}/config            → apply partial Config (outputs/inputs/master_status)

Config payload shape (all fields optional, only include what you want to change):
  {
    "outputs": [{"index": 0, "gain": -6.0}],
    "inputs":  [{"index": 1, "routing": [{"index": 0, "mute": false}]}]
  }

Safety:
  - delay_ms > MAX_DELAY_MS  → ValueError (hardware limit is 30 ms)
  - slot in APF_RESERVED_SLOTS → ValueError (slots 0-1 reserved for APF)
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

# ── Constants ──────────────────────────────────────────────────────────────────

MAX_DELAY_MS: float = 30.0
"""Hardware maximum output delay for miniDSP 2x4 HD."""

APF_RESERVED_SLOTS: frozenset[int] = frozenset({0, 1})
"""PEQ slot indices reserved for APF all-pass filters (TODO-10).

Slots 2-9 are available for amplitude EQ (ALIGNMENT_PEQ_SLOTS).
"""

ALIGNMENT_PEQ_SLOTS: range = range(2, 10)
"""PEQ slots used by the alignment amplitude-EQ pass."""


# ── Exceptions ─────────────────────────────────────────────────────────────────

class MinidspApiError(RuntimeError):
    """Raised when minidspd returns an unexpected HTTP error.

    Attributes:
        status_code  -- HTTP status returned by minidspd
        path         -- the request path that failed
    """

    def __init__(self, status_code: int, path: str) -> None:
        self.status_code = status_code
        self.path = path
        super().__init__(f"minidspd {status_code} on {path}")


# ── Client ─────────────────────────────────────────────────────────────────────

class MinidspClient:
    """Thin async HTTP client wrapping the minidspd REST API.

    All mutating operations use POST /devices/{device_index}/config with
    a partial Config payload — only the fields you want to change are sent.

    Usage (synchronous callers use asyncio.run / loop.run_until_complete):

        client = MinidspClient("localhost", 5380)
        await client.set_output_gain(0, -6.0)
        await client.set_output_delay(0, 4.5)
        await client.set_output_polarity(0, inverted=True)
        await client.set_input_routing(1, {0: True, 1: False, 2: True, 3: True})
        await client.restore_all_gains([0, 1])
    """

    def __init__(self, host: str, port: int, device_index: int = 0) -> None:
        self._base = f"http://{host}:{port}"
        self._device_index = device_index

    # ── Internal helpers ───────────────────────────────────────────────────────

    async def _post_config(self, config: dict[str, Any]) -> None:
        """POST a partial Config to the device, raising MinidspApiError on 4xx/5xx."""
        path = f"/devices/{self._device_index}/config"
        url = f"{self._base}{path}"
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=config)
        if response.status_code >= 400:
            raise MinidspApiError(response.status_code, path)

    # ── Public API ─────────────────────────────────────────────────────────────

    async def get_devices(self) -> list[dict]:
        """Return the list of connected miniDSP devices from minidspd."""
        url = f"{self._base}/devices"
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url)
        response.raise_for_status()
        return response.json()  # type: ignore[no-any-return]

    async def set_output_gain(self, output: int, gain_db: float) -> None:
        """Set output *output* gain to *gain_db* dB.

        Typical use: mute with MUTE_GAIN_DB (-127) or restore to 0.0.
        """
        await self._post_config({"outputs": [{"index": output, "gain": gain_db}]})

    async def set_output_delay(self, output: int, delay_ms: float) -> None:
        """Set output *output* delay to *delay_ms* milliseconds.

        Raises ValueError if delay_ms > MAX_DELAY_MS (hardware limit).
        """
        if delay_ms > MAX_DELAY_MS:
            raise ValueError(
                f"delay_ms={delay_ms} exceeds hardware maximum {MAX_DELAY_MS} ms"
            )
        total_nanos = int(round(delay_ms * 1_000_000))
        secs, nanos = divmod(total_nanos, 1_000_000_000)
        await self._post_config({
            "outputs": [{"index": output, "delay": {"secs": secs, "nanos": nanos}}]
        })

    async def set_output_polarity(self, output: int, inverted: bool) -> None:
        """Set output *output* phase inversion.

        Raises MinidspApiError on hardware or daemon error.
        """
        await self._post_config({"outputs": [{"index": output, "invert": inverted}]})

    async def set_output_peq(
        self,
        output: int,
        slot: int,
        biquad: dict[str, Any],
    ) -> None:
        """Write a biquad filter to output *output* PEQ slot *slot*.

        *biquad* must contain at least the biquad coefficients (b0, b1, b2, a1, a2).
        Optionally include "bypass": bool to set the bypass state.

        Raises ValueError if *slot* is in APF_RESERVED_SLOTS (0 or 1).
        """
        if slot in APF_RESERVED_SLOTS:
            raise ValueError(
                f"PEQ slot {slot} is reserved for APF filters; "
                f"use slots {list(ALIGNMENT_PEQ_SLOTS)}"
            )
        bypass = biquad.pop("bypass", None)
        peq_entry: dict[str, Any] = {"index": slot, "coeff": biquad}
        if bypass is not None:
            peq_entry["bypass"] = bypass
        await self._post_config({
            "outputs": [{"index": output, "peq": [peq_entry]}]
        })

    async def set_input_routing(
        self,
        input_index: int,
        output_enabled: dict[int, bool],
    ) -> None:
        """Set the routing matrix for *input_index*.

        *output_enabled* maps each output index to whether it should receive
        signal from this input.  Example to route input 1 (input 2, 0-based)
        to outputs 0, 2, 3 only:

            await client.set_input_routing(1, {0: True, 1: False, 2: True, 3: True})

        Outputs not listed in *output_enabled* are left unchanged.
        """
        routing = [
            {"index": out_idx, "mute": not enabled}
            for out_idx, enabled in output_enabled.items()
        ]
        await self._post_config({
            "inputs": [{"index": input_index, "routing": routing}]
        })

    async def restore_all_gains(self, output_indices: list[int]) -> None:
        """Restore gain to 0.0 dB on every output in *output_indices*.

        Called in finally blocks and TTL cleanup to ensure no sub is left muted
        after an alignment session ends (normally or due to browser disconnect).
        """
        tasks = [self.set_output_gain(idx, 0.0) for idx in output_indices]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for output, result in zip(output_indices, results):
            if isinstance(result, Exception):
                # Log but do not raise — we want to restore as many as possible.
                import logging
                logging.getLogger(__name__).warning(
                    "restore_all_gains: failed to restore output %d: %s", output, result
                )
