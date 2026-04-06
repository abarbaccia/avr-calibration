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

MAX_OUTPUT_INDEX: int = 3
"""Highest valid output index for miniDSP 2x4 HD (4 outputs: 0-3)."""

APF_RESERVED_SLOTS: frozenset[int] = frozenset({0, 1})
"""PEQ slot indices reserved for APF all-pass filters (TODO-10).

Slots 2-9 are available for amplitude EQ (ALIGNMENT_PEQ_SLOTS).
"""

ALIGNMENT_PEQ_SLOTS: range = range(2, 10)
"""PEQ slots used by the alignment amplitude-EQ pass."""

VALID_SOURCES: frozenset[str] = frozenset({"Analog", "Toslink", "USB"})
"""Valid input source names for the miniDSP 2x4 HD."""

MAX_PRESET_INDEX: int = 3
"""Highest valid preset slot index (miniDSP 2x4 HD has 4 presets: 0-3)."""


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
        await client.switch_preset(1)
        await client.switch_source("Toslink")
        await client.restore_all_gains([0, 1])
    """

    # TODO: Pool httpx client instead of creating one per call — prevents fd leaks
    #       in long-running sessions. Replace per-method AsyncClient() with a shared
    #       instance created in __init__ and closed explicitly.

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

    async def _patch_master(self, fields: dict[str, Any]) -> None:
        """POST master-status fields to /devices/{idx}.

        minidspd 0.1.x uses POST /devices/{idx} with a MasterStatus body
        to mutate preset, source, volume, or mute.  Only the supplied fields
        are changed; omitted fields are ignored by the daemon.
        """
        path = f"/devices/{self._device_index}"
        url = f"{self._base}{path}"
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=fields)
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

    async def get_device_status(self) -> dict:
        """Return the current master status for the device.

        Response shape from minidspd:
          {
            "master": {"preset": 0, "source": "Analog", "volume": -30.0, "mute": false},
            "input_levels": [...],
            "output_levels": [...]
          }

        Raises MinidspApiError on HTTP error.
        """
        path = f"/devices/{self._device_index}"
        url = f"{self._base}{path}"
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url)
        if response.status_code >= 400:
            raise MinidspApiError(response.status_code, path)
        return response.json()  # type: ignore[no-any-return]

    async def switch_preset(self, preset: int) -> None:
        """Switch the active preset slot to *preset* (0-3).

        Uses POST /devices/{idx} with {"preset": N} — the correct API for
        minidspd 0.1.x (the /preset/:n path endpoint does not exist).

        Raises ValueError if preset is out of range.
        Raises MinidspApiError on HTTP error.
        """
        if not (0 <= preset <= MAX_PRESET_INDEX):
            raise ValueError(
                f"preset={preset} out of range; must be 0-{MAX_PRESET_INDEX}"
            )
        await self._patch_master({"preset": preset})

    async def switch_source(self, source: str) -> None:
        """Switch the input source to *source* (Analog/Toslink/USB).

        Uses POST /devices/{idx} with {"source": name} — same pattern as
        switch_preset.

        Raises ValueError if source is not in VALID_SOURCES.
        Raises MinidspApiError on HTTP error.
        """
        if source not in VALID_SOURCES:
            raise ValueError(
                f"source={source!r} invalid; must be one of {sorted(VALID_SOURCES)}"
            )
        await self._patch_master({"source": source})

    MUTE_GAIN_DB: float = -127.0

    @staticmethod
    def _validate_output(output: int) -> None:
        """Raise ValueError if output index is out of range."""
        if not (0 <= output <= MAX_OUTPUT_INDEX):
            raise ValueError(
                f"output={output} out of range; must be 0-{MAX_OUTPUT_INDEX}"
            )

    async def set_output_gain(self, output: int, gain_db: float) -> None:
        """Set output *output* gain to *gain_db* dB.

        Typical use: mute with MUTE_GAIN_DB (-127) or restore to 0.0.
        """
        self._validate_output(output)
        await self._post_config({"outputs": [{"index": output, "gain": gain_db}]})

    async def mute_outputs(self, output_indices: list[int]) -> None:
        """Mute multiple outputs in parallel by setting gain to -127 dB.

        Raises MinidspApiError if any output fails to mute.
        """
        tasks = [self.set_output_gain(idx, self.MUTE_GAIN_DB) for idx in output_indices]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        errors = [
            (idx, r) for idx, r in zip(output_indices, results)
            if isinstance(r, Exception)
        ]
        if errors:
            detail = "; ".join(f"output {idx}: {e}" for idx, e in errors)
            raise MinidspApiError(0, f"mute_outputs partial failure: {detail}")

    async def unmute_outputs(self, output_indices: list[int]) -> None:
        """Unmute multiple outputs in parallel by restoring gain to 0 dB."""
        await self.restore_all_gains(output_indices)

    async def set_output_delay(self, output: int, delay_ms: float) -> None:
        """Set output *output* delay to *delay_ms* milliseconds.

        Raises ValueError if delay_ms is negative or > MAX_DELAY_MS.
        """
        self._validate_output(output)
        if delay_ms < 0.0:
            raise ValueError(f"delay_ms={delay_ms} cannot be negative")
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

        Raises ValueError if output index is invalid.
        Raises MinidspApiError on hardware or daemon error.
        """
        self._validate_output(output)
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
        self._validate_output(output)
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

    async def set_output_peq_batch(
        self,
        output: int,
        entries: list[dict[str, Any]],
    ) -> None:
        """Write multiple PEQ slots to *output* in a single HTTP request.

        Each entry in *entries* must have: {"index": slot, "coeff": {b0,b1,b2,a1,a2}}
        and optionally "bypass": bool.

        This is much safer than individual set_output_peq calls — one atomic POST
        instead of N sequential ones, reducing the chance of partial writes that
        can leave the miniDSP in a stuck state.

        Raises ValueError if any slot is in APF_RESERVED_SLOTS.
        """
        self._validate_output(output)
        for entry in entries:
            if entry["index"] in APF_RESERVED_SLOTS:
                raise ValueError(
                    f"PEQ slot {entry['index']} is reserved for APF filters; "
                    f"use slots {list(ALIGNMENT_PEQ_SLOTS)}"
                )
        await self._post_config({
            "outputs": [{"index": output, "peq": entries}]
        })

    async def set_input_peq_batch(
        self,
        input_index: int,
        entries: list[dict[str, Any]],
    ) -> None:
        """Write multiple PEQ slots to input *input_index* in a single HTTP request.

        Each entry in *entries* must have: {"index": slot, "coeff": {b0,b1,b2,a1,a2}}
        and optionally "bypass": bool.

        Used to apply shared EQ (e.g. Harman target curve) to the input channel,
        affecting all outputs equally.
        """
        await self._post_config({
            "inputs": [{"index": input_index, "peq": entries}]
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
