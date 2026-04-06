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

import functools

from ..adapters.minidsp import (
    ALIGNMENT_PEQ_SLOTS,
    MinidspApiError,
    MinidspClient,
)
from ..dsp import freq_gain_q_to_biquad
from ..safety import FilterSpec, SafetyValidator
from .base import DriverError
from .dsp_driver import DSPDriver


def _driver_api(fn):
    """Decorator that wraps MinidspApiError/ValueError into DriverError."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except DriverError:
            raise
        except (ValueError, MinidspApiError) as exc:
            raise DriverError(str(exc))
    return wrapper

_AVAILABLE_SLOTS: list[int] = list(ALIGNMENT_PEQ_SLOTS)  # slots 2-9
_BYPASS_BIQUAD: dict[str, Any] = {
    "b0": 1.0, "b1": 0.0, "b2": 0.0, "a1": 0.0, "a2": 0.0, "bypass": True
}


class MinidspDriver(DSPDriver):
    """DSPDriver for miniDSP 2x4 HD via minidspd REST API."""

    def __init__(self, host: str, port: int, device_index: int = 0,
                 sub_outputs: list[int] | None = None) -> None:
        self._client = MinidspClient(host=host, port=port, device_index=device_index)
        self._host = host
        self._sub_outputs = sub_outputs or [0, 1]
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
                "input_levels": status.get("input_levels"),
                "output_levels": status.get("output_levels"),
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

            # Hardware write — batch all PEQ slots per output into one request
            # to avoid rapid-fire individual writes that can corrupt the DSP.
            try:
                for output in self._sub_outputs:
                    peq_entries: list[dict[str, Any]] = []
                    # Active filter slots
                    for slot_offset, fspec in enumerate(filter_specs):
                        slot = _AVAILABLE_SLOTS[slot_offset]
                        biquad = freq_gain_q_to_biquad(
                            freq=fspec.freq,
                            gain_db=fspec.gain_db,
                            q=fspec.q,
                            filter_type=fspec.type,
                        )
                        peq_entries.append({"index": slot, "coeff": biquad})
                    # Bypass unused slots
                    for slot in _AVAILABLE_SLOTS[len(filter_specs):]:
                        peq_entries.append({
                            "index": slot,
                            "coeff": {"b0": 1.0, "b1": 0.0, "b2": 0.0, "a1": 0.0, "a2": 0.0},
                            "bypass": True,
                        })
                    await self._client.set_output_peq_batch(output, peq_entries)
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

    @_driver_api
    async def set_preset(self, preset: int) -> None:
        await self._client.switch_preset(preset)

    @_driver_api
    async def mute_outputs(self, output_indices: list[int]) -> None:
        """Mute outputs by setting gain to -127 dB."""
        await self._client.mute_outputs(output_indices)

    @_driver_api
    async def unmute_outputs(self, output_indices: list[int]) -> None:
        """Unmute outputs by restoring gain to 0 dB."""
        await self._client.unmute_outputs(output_indices)

    @_driver_api
    async def set_output_delay(self, output_index: int, delay_ms: float) -> None:
        """Set delay for a single output in milliseconds."""
        await self._client.set_output_delay(output_index, delay_ms)

    @_driver_api
    async def set_output_polarity(self, output_index: int, inverted: bool) -> None:
        """Set polarity for a single output (inverted=True flips phase)."""
        await self._client.set_output_polarity(output_index, inverted)

    @_driver_api
    async def set_routing(self, routing: dict) -> None:
        for input_index, output_enabled in routing.items():
            await self._client.set_input_routing(int(input_index), output_enabled)

    @_driver_api
    async def configure_active_input(self, active_input: int) -> None:
        """Route *active_input* to all outputs and mute the other input.

        Used when one analog input is broken or unused — ensures all sub
        outputs receive signal from the working input.
        """
        all_outputs = {0: True, 1: True, 2: True, 3: True}
        all_muted = {0: False, 1: False, 2: False, 3: False}
        other_input = 1 - active_input
        await self._client.set_input_routing(active_input, all_outputs)
        await self._client.set_input_routing(other_input, all_muted)
