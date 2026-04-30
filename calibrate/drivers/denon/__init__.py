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
from typing import Mapping

from . import audyssey_tcp
from ..avr_driver import AVRDriver
from ..base import DriverError

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

    # Per-channel applied-delay ceiling enforced by the AVR firmware DSP. The
    # configured Audyssey distance can exceed this (we can store any value via
    # direct TCP), but the AVR will only apply at most this much delay to a
    # speaker — calibration code that needs to compensate FIR group delay
    # via mains distance MUST budget against this. Empirical X3800H value;
    # other models likely differ.
    MAX_SPEAKER_DELAY_MS: float = audyssey_tcp.MAX_APPLIED_DELAY_MS

    def __init__(self, host: str | None) -> None:
        self._host = host  # sync — no network; safe in constructor

    async def get_state(self) -> dict:
        if not self._host:
            return {"connected": False, "error": "no host configured"}
        receiver = await _connect_receiver(self._host)
        # NOTE: AVR HTTP responds in standby — Telnet replies + sweep audio
        # do NOT work when power != "ON". Surface power so callers can guard
        # before sending Telnet/sweep commands.
        return {
            "connected": True,
            "host": self._host,
            "model": receiver.model_name or "Denon AVR",
            "power": receiver.power,
            "volume": receiver.volume,
            "input": receiver.input_func,
            "mute": receiver.muted,
            "max_speaker_delay_ms": self.MAX_SPEAKER_DELAY_MS,
        }

    async def async_power_on(self) -> str:
        """Power the AVR on and wait for it to report ON.

        Returns the final power state. Raises DriverError on timeout.
        """
        if not self._host:
            raise DriverError("no host configured")
        receiver = await _connect_receiver(self._host)
        if receiver.power == "ON":
            return "ON"
        try:
            await receiver.async_power_on()
        except Exception as exc:
            raise DriverError(f"power_on failed: {exc}") from exc
        # Poll up to 10 s for the AVR to report ON. HDMI handshake usually
        # needs another ~3 s on top, but ON status is enough for callers
        # to know Telnet/sweeps will respond.
        for _ in range(20):
            await asyncio.sleep(0.5)
            await receiver.async_update()
            if receiver.power == "ON":
                return "ON"
        raise DriverError("AVR did not report power=ON within 10 s")

    async def set_speaker_distances(
        self,
        channel_distances_m: Mapping[str, float],
        *,
        n_positions: int = 1,
        commit: bool = False,
        use_custom: bool = False,
    ) -> None:
        """Push per-channel Audyssey distances directly to the AVR.

        Bypasses the MultEQ Editor app's UI cap (59.1 ft / 18 m on X3800H)
        by speaking the Audyssey TCP protocol on port 1256. Use this to
        compensate sub-path FIR latency by setting the sub channel's
        distance well above the mains.

        IMPORTANT: this writes Audyssey calibration state. With
        ``commit=True`` the change persists to NVRAM. Callers should
        obtain explicit user confirmation before pushing — per the
        "signal-path writes need human approval" rule. Failure to reach
        the AVR or malformed data will raise ``DriverError``.

        See ``MAX_SPEAKER_DELAY_MS`` for the ceiling on actually-applied
        delay regardless of the configured value.

        Args:
            channel_distances_m: e.g. ``{"FL": 4.05, "SW1": 30.72}``.
                Channel names match Audyssey commandIds (FL/FR/C/SLA/...).
            n_positions: measurement-position count from the AVR's stored
                calibration. Get this from a saved .ady file's
                ``responseData`` map size, or pass 1 for a single position.
            commit: if True, send ``AudyFinFlg=Fin`` to persist to NVRAM.
        """
        if not self._host:
            raise DriverError("no host configured")
        try:
            await audyssey_tcp.push_speaker_distances(
                self._host,
                channel_distances_m,
                n_positions=n_positions,
                commit=commit,
                use_custom=use_custom,
            )
        except (OSError, ValueError) as exc:
            raise DriverError(f"audyssey push failed: {exc}")

    async def telnet_query(
        self,
        commands: list[str],
        *,
        port: int = 23,
        connect_timeout: float = 5.0,
        reply_timeout: float = 2.0,
        require_power_on: bool = True,
    ) -> dict[str, str]:
        """Run one or more Denon Telnet commands and return their replies.

        Loud-fail wrapper around the raw Telnet protocol. If the AVR is in
        standby, replies vanish silently — this helper detects that state
        and raises DriverError with a clear message instead of returning
        empty strings.

        Args:
            commands: e.g. ``["MV?", "CV?", "SI?"]``. The trailing CR is
                added automatically.
            port: Telnet port, default 23.
            connect_timeout: socket-connect timeout in seconds.
            reply_timeout: per-command read timeout in seconds.
            require_power_on: if True (default), check ``avr.power`` first
                and raise if not ON. Disable only when intentionally
                probing standby state.

        Returns:
            dict mapping each input command (verbatim, without CR) to the
            decoded reply string with CRs stripped.

        Raises:
            DriverError: power is off, connection fails, or no replies
                returned for any of the commands.
        """
        if not self._host:
            raise DriverError("no host configured")

        if require_power_on:
            receiver = await _connect_receiver(self._host)
            if receiver.power != "ON":
                raise DriverError(
                    f"AVR power is {receiver.power!r} — Telnet replies will not "
                    "arrive in standby. Power on the AVR (or call "
                    "DenonDriver.async_power_on()) before sending Telnet commands."
                )

        import socket
        import time

        loop = asyncio.get_running_loop()

        def _do_telnet() -> dict[str, str]:
            sock = socket.create_connection((self._host, port), timeout=connect_timeout)
            sock.settimeout(reply_timeout)
            # Drain any queued banner / prior session state.
            time.sleep(0.5)
            try:
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
            except (socket.timeout, TimeoutError):
                pass

            replies: dict[str, str] = {}
            try:
                for cmd in commands:
                    sock.sendall((cmd + "\r").encode("ascii"))
                    time.sleep(0.4)
                    buf = b""
                    try:
                        while True:
                            chunk = sock.recv(4096)
                            if not chunk:
                                break
                            buf += chunk
                    except (socket.timeout, TimeoutError):
                        pass
                    replies[cmd] = buf.decode("ascii", errors="replace").strip()
            finally:
                try:
                    sock.close()
                except OSError:
                    pass
            return replies

        try:
            replies = await loop.run_in_executor(None, _do_telnet)
        except OSError as exc:
            raise DriverError(f"Telnet connection failed: {exc}") from exc

        empty = [cmd for cmd, reply in replies.items() if not reply]
        if empty and len(empty) == len(commands):
            raise DriverError(
                f"Denon Telnet returned no replies for any of {commands}. "
                "Likely causes (in order of probability): "
                "(1) AVR is in standby (verify power=ON via get_state); "
                "(2) Telnet connection pool exhausted — wait 30 s and retry; "
                "(3) AVR firmware in a stuck state, power-cycle the unit."
            )
        return replies

    async def set_volume(self, level_db: float) -> float:
        """Set volume to *level_db* dB. Returns confirmed level from hardware."""
        if not self._host:
            raise DriverError("no host configured")
        receiver = await _connect_receiver(self._host)
        clamped = max(_DENON_MIN_DB, min(_DENON_MAX_DB, level_db))
        await receiver.async_set_volume(clamped)
        await receiver.async_update()
        return receiver.volume  # type: ignore[no-any-return]

    async def audyssey_status(self) -> dict:
        """Report whether Audyssey distance compensation is currently active.

        Audyssey distance/level/EQ only applies when:
          - Sound mode is NOT "PURE DIRECT" (Pure Direct bypasses all DSP)
          - MultEQ is NOT "Off"

        Returns a dict with ``active`` (bool|None), ``sound_mode`` (str|None),
        ``multi_eq`` (str|None), and ``reason`` (str|None) explaining when
        ``active`` is False. ``active=None`` means we couldn't determine
        (e.g. fields not yet populated by the denonavr lib) — treat as
        unknown rather than asserting either way.
        """
        if not self._host:
            raise DriverError("no host configured")
        receiver = await _connect_receiver(self._host)
        try:
            await receiver.audyssey.async_update()
        except Exception as exc:
            log.warning("audyssey async_update failed: %s", exc)

        try:
            sound_mode = receiver.soundmode.sound_mode
        except Exception:
            sound_mode = None
        try:
            multi_eq = receiver.audyssey.multi_eq
        except Exception:
            multi_eq = None

        reason: str | None = None
        active: bool | None
        if sound_mode is None and multi_eq is None:
            active = None
        elif isinstance(sound_mode, str) and sound_mode.upper() == "PURE DIRECT":
            active = False
            reason = "sound mode is Pure Direct — bypasses all DSP including Audyssey"
        elif isinstance(multi_eq, str) and multi_eq.lower() == "off":
            active = False
            reason = "MultEQ is Off — Audyssey distance/level/EQ disabled"
        else:
            active = True

        return {
            "active": active,
            "sound_mode": sound_mode,
            "multi_eq": multi_eq,
            "reason": reason,
        }

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

        # Power-state guard. The AVR's HTTP / denonavr library reports
        # connected=True in standby — but Telnet replies vanish and sweeps
        # produce silence. Auto-power-on so callers don't have to track it.
        if self._receiver.power != "ON":
            log.info("Denon sweep: AVR is in %s — powering on", self._receiver.power)
            try:
                await self._receiver.async_power_on()
            except Exception as exc:
                raise RuntimeError(
                    "AVR is not powered on and async_power_on failed: "
                    f"{exc}. Turn the AVR on manually before measuring."
                ) from exc
            for _ in range(20):
                await asyncio.sleep(0.5)
                await self._receiver.async_update()
                if self._receiver.power == "ON":
                    log.info("Denon sweep: AVR is now ON")
                    break
            else:
                raise RuntimeError(
                    "AVR did not report power=ON within 10 s — sweeps will be silent. "
                    "Turn the AVR on manually and retry."
                )

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
