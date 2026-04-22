"""DenonDriver — AVRDriver implementation wrapping the denonavr library.

Supports Denon and Marantz AV receivers via the denonavr HTTP/telnet protocol.

Design notes:
  - Constructor is synchronous (no network calls) — safe to call at import time.
  - setup() is a no-op; denonavr creates a new HTTP session per call.
  - close() is a no-op; no persistent connections to clean up.
  - All network calls are wrapped with asyncio.wait_for(timeout=5.0) and
    re-raised as DriverError so callers handle one exception type.

DenonSweepContext: async context manager for measurement sweep lifecycle.
  Saves current Denon input/volume, switches to sweep input/volume, settles,
  then restores on exit (best-effort). Used by MCP tools and web endpoints
  that need to run a sweep through the Denon's HDMI path.
"""

from __future__ import annotations

import asyncio
import logging

from .avr_driver import AVRDriver
from .base import DriverError

log = logging.getLogger(__name__)

_DENON_MIN_DB: float = -80.0
_DENON_MAX_DB: float = 18.0


async def _connect_receiver(host: str):
    """Create, setup, and update a DenonAVR instance. Raises DriverError on failure."""
    import denonavr

    try:
        receiver = denonavr.DenonAVR(host)
        await asyncio.wait_for(receiver.async_setup(), timeout=5.0)
        await receiver.async_update()
        return receiver
    except asyncio.TimeoutError:
        raise DriverError(f"timeout connecting to {host}")
    except Exception as exc:
        raise DriverError(str(exc))


class DenonDriver(AVRDriver):
    """AVRDriver for Denon X-series (and Marantz) receivers via denonavr."""

    def __init__(self, host: str | None) -> None:
        self._host = host  # sync — no network; safe in constructor

    async def get_state(self) -> dict:
        if not self._host:
            return {"connected": False, "error": "no host configured"}
        receiver = await _connect_receiver(self._host)
        return {
            "connected": True,
            "host": self._host,
            "model": receiver.model_name or "Denon AVR",
            "volume": receiver.volume,
            "input": receiver.input_func,
            "mute": receiver.muted,
        }

    async def set_volume(self, level_db: float) -> float:
        """Set volume to *level_db* dB. Returns confirmed level from hardware."""
        if not self._host:
            raise DriverError("no host configured")
        receiver = await _connect_receiver(self._host)
        clamped = max(_DENON_MIN_DB, min(_DENON_MAX_DB, level_db))
        await receiver.async_set_volume(clamped)
        await receiver.async_update()
        return receiver.volume  # type: ignore[no-any-return]

    async def discover(self) -> list[str]:
        """SSDP scan for Denon/Marantz AVRs on the local network."""
        try:
            import denonavr
            found = await asyncio.wait_for(denonavr.async_discover(), timeout=10.0)
            return [d["host"] for d in found if "host" in d]
        except asyncio.TimeoutError:
            return []
        except Exception:
            return []

    def sweep_context(self, config):
        """Return a DenonSweepContext when HDMI route is configured, else None.

        Delegates to the existing standalone ``DenonSweepContext.from_config``
        so the composer in ``SignalGraph.sweep_context`` can treat all
        processors uniformly — DSP and AVR both expose ``sweep_context``
        through their driver.
        """
        return DenonSweepContext.from_config(config)


class DenonSweepContext:
    """Async context manager for Denon sweep lifecycle.

    TODO: Add a class-level asyncio.Lock so concurrent HDMI sweeps serialize
          rather than interleaving state save/restore.

    Usage:
        async with DenonSweepContext(host, sweep_input, sweep_volume) as ctx:
            fr = await engine.measure()

    On enter: connects to Denon, saves current input/volume, switches to
    sweep input/volume, waits for settle. On exit: restores saved state
    (best-effort, exceptions caught).

    Volume safety: sweep_volume must be <= MAX_SWEEP_VOLUME_DB (default 0 dB / reference).
    """

    MAX_SWEEP_VOLUME_DB: float = 0.0  # reference level — configurable ceiling

    @classmethod
    def from_config(cls, config, manage_volume: bool = True) -> "DenonSweepContext | None":
        """Build from a Config object, or return None if HDMI sweep not configured.

        Args:
            manage_volume: If False, skip setting/restoring volume on enter/exit.
                Useful when the caller manages volume itself (e.g. calibrate_level).
        """
        route = config.measurement.get("playback_route", "usb")
        if route != "hdmi":
            return None
        host = config.denon.get("host")
        sweep_input = config.measurement.get("denon_sweep_input")
        if not host or not sweep_input:
            return None
        return cls(
            host=host,
            sweep_input=sweep_input,
            sweep_volume=float(config.measurement.get("denon_sweep_volume", -10.0)),
            settle_ms=config.measurement.get("denon_settle_ms", 5000),
            manage_volume=manage_volume,
            pure_direct=bool(config.measurement.get("denon_pure_direct", True)),
        )

    def __init__(
        self,
        host: str,
        sweep_input: str,
        sweep_volume: float = -10.0,
        settle_ms: int = 5000,
        manage_volume: bool = True,
        pure_direct: bool = True,
    ) -> None:
        if manage_volume and sweep_volume > self.MAX_SWEEP_VOLUME_DB:
            raise ValueError(
                f"sweep_volume must be <= {self.MAX_SWEEP_VOLUME_DB} dB, got {sweep_volume}"
            )
        self._host = host
        self._sweep_input = sweep_input
        self._sweep_volume = sweep_volume
        self._settle_ms = settle_ms
        self._manage_volume = manage_volume
        self._pure_direct = pure_direct
        self._receiver = None
        self._saved_input: str | None = None
        self._saved_volume: float | None = None
        self._saved_sound_mode: str | None = None

    async def __aenter__(self) -> "DenonSweepContext":
        self._receiver = await _connect_receiver(self._host)

        self._saved_input = self._receiver.input_func
        self._saved_volume = self._receiver.volume

        # Save sound mode so we can restore it on exit.
        try:
            self._saved_sound_mode = self._receiver.soundmode.sound_mode
            log.info("Denon sweep: saved sound mode: %s", self._saved_sound_mode)
        except Exception as exc:
            log.warning("Could not read sound mode: %s", exc)

        log.info(
            "Denon sweep: switching to input=%s %s%s(was %s / %s / %s)",
            self._sweep_input,
            f"volume={self._sweep_volume:.1f} dB " if self._manage_volume else "",
            "Pure Direct " if self._pure_direct else "(keeping current sound mode) ",
            self._saved_input, self._saved_volume, self._saved_sound_mode,
        )
        await self._receiver.async_set_input_func(self._sweep_input)
        if self._manage_volume:
            await self._receiver.async_set_volume(self._sweep_volume)

        if self._pure_direct:
            try:
                await self._receiver.soundmode.async_set_sound_mode("PURE DIRECT")
            except Exception as exc:
                log.warning("Could not set Pure Direct: %s", exc)
        else:
            log.info("Denon sweep: pure_direct=False, keeping sound mode: %s", self._saved_sound_mode)

        await asyncio.sleep(self._settle_ms / 1000.0)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._receiver is None:
            return
        try:
            log.info(
                "Denon sweep: restoring input=%s volume=%s sound_mode=%s",
                self._saved_input, self._saved_volume, self._saved_sound_mode,
            )
            # Only restore sound mode if we changed it (pure_direct=True)
            if self._pure_direct and self._saved_sound_mode is not None:
                await asyncio.wait_for(
                    self._receiver.soundmode.async_set_sound_mode(self._saved_sound_mode),
                    timeout=5.0,
                )
            if self._saved_input is not None:
                await asyncio.wait_for(
                    self._receiver.async_set_input_func(self._saved_input),
                    timeout=5.0,
                )
            if self._manage_volume and self._saved_volume is not None:
                await asyncio.wait_for(
                    self._receiver.async_set_volume(self._saved_volume),
                    timeout=5.0,
                )
        except Exception as exc:
            log.warning("Failed to restore Denon state: %s", exc)
        self._receiver = None
