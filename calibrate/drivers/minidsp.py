"""MinidspDriver — DSPDriver implementation wrapping MinidspClient (minidspd REST API).

Owns the in-memory EQ state for all presets (minidspd has no GET endpoint for PEQ).
All state-mutating operations (apply_eq) run under an asyncio.Lock that covers
the full read→validate→write→update sequence, preventing concurrent calls from
applying conflicting changes.

P0 safety: apply_eq only updates _eq_state after ALL hardware writes succeed.
If any write fails mid-loop, hardware is partially configured but _eq_state is
unchanged — SafetyValidator will diff against the correct baseline on the next call.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..adapters.minidsp import (
    ALIGNMENT_PEQ_SLOTS,
    MinidspApiError,
    MinidspClient,
)
from ..dsp import freq_gain_q_to_biquad
from ..safety import FilterSpec, SafetyValidator
from .base import DriverError
from .dsp_driver import DSPDriver

_AVAILABLE_SLOTS: list[int] = list(ALIGNMENT_PEQ_SLOTS)  # slots 2-9
_BYPASS_BIQUAD: dict[str, Any] = {
    "b0": 1.0, "b1": 0.0, "b2": 0.0, "a1": 0.0, "a2": 0.0, "bypass": True
}


class MinidspDriver(DSPDriver):
    """DSPDriver for miniDSP 2x4 HD via minidspd REST API."""

    def __init__(self, host: str, port: int, device_index: int = 0) -> None:
        self._client = MinidspClient(host=host, port=port, device_index=device_index)
        self._host = host
        self._eq_state: dict[int, list[dict]] = {}
        self._lock = asyncio.Lock()

    async def get_state(self) -> dict:
        try:
            status = await asyncio.wait_for(
                self._client.get_device_status(), timeout=5.0
            )
            master = status.get("master", {})
            return {
                "connected": True,
                "host": self._host,
                "preset": master.get("preset"),
                "source": master.get("source"),
                "volume": master.get("volume"),
                "mute": master.get("mute"),
            }
        except asyncio.TimeoutError:
            raise DriverError(f"timeout connecting to {self._host}")
        except MinidspApiError as exc:
            raise DriverError(str(exc))
        except Exception as exc:
            raise DriverError(str(exc))

    async def current_preset(self) -> int:
        """Return active preset index. Returns 0 on failure (safe default)."""
        try:
            status = await self._client.get_device_status()
            return int(status.get("master", {}).get("preset", 0))
        except Exception:
            return 0

    async def read_eq(self, preset: int) -> list[dict]:
        """Return in-memory EQ state for *preset* ([] if never applied)."""
        return list(self._eq_state.get(preset, []))

    async def apply_eq(self, preset: int, filters: list[dict]) -> None:
        """Validate and apply EQ filters atomically under asyncio lock.

        The full sequence — parse → SafetyValidator → biquad convert →
        hardware write → state update — runs under a single asyncio.Lock.
        This prevents two concurrent apply_eq calls from both passing
        SafetyValidator against the same baseline before either writes.

        _eq_state is updated ONLY if all hardware writes succeed (P0 rollback).
        """
        # Parse filter specs (outside lock — pure computation, no network)
        try:
            filter_specs = [
                FilterSpec(
                    freq=float(f["freq"]),
                    gain_db=float(f["gain_db"]),
                    q=float(f.get("q", 0.707)),
                    type=f["type"],
                )
                for f in filters
            ]
        except (KeyError, ValueError, TypeError) as exc:
            raise DriverError(f"invalid filter spec: {exc}")

        # Slot count check (outside lock — no shared state)
        if len(filter_specs) > len(_AVAILABLE_SLOTS):
            raise DriverError(
                f"too many filters: {len(filter_specs)} requested, "
                f"{len(_AVAILABLE_SLOTS)} PEQ slots available (slots 2-9)"
            )

        async with self._lock:
            # Read current state under lock (prevents concurrent baseline divergence)
            prev_raw = self._eq_state.get(preset, [])
            prev_specs = [
                FilterSpec(
                    freq=float(f["freq"]),
                    gain_db=float(f["gain_db"]),
                    q=float(f.get("q", 0.707)),
                    type=f["type"],
                )
                for f in prev_raw
            ] if prev_raw else None

            # SafetyValidator under lock
            validator = SafetyValidator()
            result = validator.validate(filter_specs, prev_specs)
            if not result.ok:
                raise DriverError(f"SafetyValidator: {result.error}")

            # Hardware write — ALL must succeed before state update
            try:
                for output in [0, 1]:
                    for slot_offset, fspec in enumerate(filter_specs):
                        slot = _AVAILABLE_SLOTS[slot_offset]
                        biquad = freq_gain_q_to_biquad(
                            freq=fspec.freq,
                            gain_db=fspec.gain_db,
                            q=fspec.q,
                            filter_type=fspec.type,
                        )
                        await self._client.set_output_peq(
                            output=output, slot=slot, biquad=biquad
                        )
                    # Bypass unused slots
                    for slot in _AVAILABLE_SLOTS[len(filter_specs):]:
                        await self._client.set_output_peq(
                            output=output,
                            slot=slot,
                            biquad=dict(_BYPASS_BIQUAD),
                        )
            except MinidspApiError as exc:
                # Do NOT update _eq_state — hardware is partially configured
                raise DriverError(f"minidsp write failed: {exc}")
            except Exception as exc:
                raise DriverError(f"apply_eq error: {exc}")

            # All writes succeeded — update in-memory state
            self._eq_state[preset] = [
                {"freq": f.freq, "gain_db": f.gain_db, "q": f.q, "type": f.type}
                for f in filter_specs
            ]

    async def set_preset(self, preset: int) -> None:
        try:
            await self._client.switch_preset(preset)
        except (ValueError, MinidspApiError) as exc:
            raise DriverError(str(exc))

    async def set_routing(self, routing: dict) -> None:
        try:
            for input_index, output_enabled in routing.items():
                await self._client.set_input_routing(int(input_index), output_enabled)
        except MinidspApiError as exc:
            raise DriverError(str(exc))
