"""DenonDriver — AVRDriver implementation wrapping the denonavr library.

Supports Denon and Marantz AV receivers via the denonavr HTTP/telnet protocol.

Design notes:
  - Constructor is synchronous (no network calls) — safe to call at import time.
  - setup() is a no-op; denonavr creates a new HTTP session per call.
  - close() is a no-op; no persistent connections to clean up.
  - All network calls are wrapped with asyncio.wait_for(timeout=5.0) and
    re-raised as DriverError so callers handle one exception type.
"""

from __future__ import annotations

import asyncio

from .avr_driver import AVRDriver
from .base import DriverError

_DENON_MIN_DB: float = -80.0
_DENON_MAX_DB: float = 18.0


class DenonDriver(AVRDriver):
    """AVRDriver for Denon X-series (and Marantz) receivers via denonavr."""

    def __init__(self, host: str | None) -> None:
        self._host = host  # sync — no network; safe in constructor

    async def get_state(self) -> dict:
        if not self._host:
            return {"connected": False, "error": "no host configured"}
        try:
            import denonavr
            receiver = denonavr.DenonAVR(self._host)
            await asyncio.wait_for(receiver.async_setup(), timeout=5.0)
            await receiver.async_update()
            return {
                "connected": True,
                "host": self._host,
                "volume": receiver.volume,
                "input": receiver.input_func,
                "mute": receiver.muted,
            }
        except asyncio.TimeoutError:
            raise DriverError(f"timeout connecting to {self._host}")
        except Exception as exc:
            raise DriverError(str(exc))

    async def set_volume(self, level_db: float) -> float:
        """Set volume to *level_db* dB. Returns confirmed level from hardware."""
        if not self._host:
            raise DriverError("no host configured")
        try:
            import denonavr
            receiver = denonavr.DenonAVR(self._host)
            await asyncio.wait_for(receiver.async_setup(), timeout=5.0)
            await receiver.async_update()
            clamped = max(_DENON_MIN_DB, min(_DENON_MAX_DB, level_db))
            volume_level = (clamped - _DENON_MIN_DB) / (_DENON_MAX_DB - _DENON_MIN_DB)
            await receiver.async_set_volume_level(volume_level)
            await receiver.async_update()
            return receiver.volume  # type: ignore[no-any-return]
        except asyncio.TimeoutError:
            raise DriverError(f"timeout connecting to {self._host}")
        except Exception as exc:
            raise DriverError(str(exc))
