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
    """Create, setup, and update a DenonAVR instance. Raises DriverError on failure.

    Workaround for denonavr library bug on ``avr-x-2016``-class receivers
    (X3800H and similar): ``async_setup()`` calls ``_async_update_inputfuncs_avr``
    first, which raises "Method does not work for receiver type avr-x-2016",
    aborts, and never falls through to the working ``_async_update_inputfuncs_avr_x``.
    Result: ``input_func_list`` ends up empty and ``async_set_input_func`` fails
    for every input. Manually invoking the correct method here populates the
    full input map (including hidden HDMI inputs that show up only via this
    code path, e.g. AUX1 when un-hidden in the AVR's source-visibility menu).
    """
    import denonavr

    try:
        receiver = denonavr.DenonAVR(host)
        await asyncio.wait_for(receiver.async_setup(), timeout=5.0)
        # Defensive: if input_func_list is empty after setup, force the
        # avr-x input enumeration. Tolerate any error so existing avr-class
        # receivers (where this method is unavailable) keep working.
        if not getattr(receiver, "input_func_list", None):
            try:
                await asyncio.wait_for(
                    receiver.input._async_update_inputfuncs_avr_x(), timeout=5.0,
                )
            except (asyncio.TimeoutError, Exception):
                pass
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

    async def set_input(self, input_name: str) -> str:
        """Switch the AVR input source. Persists until explicitly changed.

        Unlike DenonSweepContext, this does NOT auto-restore on exit — the
        caller is responsible for switching back if needed.

        Args:
            input_name: Input source name as recognised by denonavr, e.g.
                'CAL', 'SHIELD', 'KARAOKE', 'Bluetooth', 'AUX1'. Names are
                case-sensitive; the AVR's input_func_list (populated by
                _connect_receiver) defines the valid set.

        Returns:
            The confirmed input_func string read back from the receiver.

        Raises:
            DriverError: no host configured, connection failure, or the
                confirmed input doesn't match the requested name after setting.
        """
        if not self._host:
            raise DriverError("no host configured")
        receiver = await _connect_receiver(self._host)
        try:
            await asyncio.wait_for(
                receiver.async_set_input_func(input_name), timeout=5.0
            )
        except asyncio.TimeoutError:
            raise DriverError(f"timeout setting input to {input_name!r}")
        except Exception as exc:
            raise DriverError(f"set_input failed: {exc}") from exc
        await asyncio.wait_for(receiver.async_update(), timeout=5.0)
        confirmed = receiver.input_func
        if confirmed != input_name:
            raise DriverError(
                f"set_input: requested {input_name!r} but AVR reports {confirmed!r}"
            )
        return confirmed  # type: ignore[return-value]

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

    async def audyssey_state_full(self) -> dict:
        """Full Audyssey + sound-mode state for calibration recipes.

        Extends ``audyssey_status`` with Telnet probes for the per-feature
        Audyssey toggles that don't surface through the denonavr HTTP API:
        Dynamic EQ, Dynamic Volume, MultEQ slot, and EQ-Set Reference vs
        Flat. Also returns a derived ``calibration_ready`` flag and a list
        of recommended actions if the state isn't ready.

        Calibration-ready means all of:
          - power == ON
          - sound_mode is NOT 'PURE DIRECT' or 'DIRECT' (both bypass
            Audyssey + our pushed FIRs)
          - multi_eq slot is one of {'FLAT', 'REFERENCE', 'BYP.LR'} (i.e.
            NOT 'OFF' — OFF disables our pushed FIRs entirely)
          - dynamic_eq is OFF (DYNEQ contaminates measurements with
            volume-dependent loudness compensation)
          - dynamic_volume is OFF (compresses dynamic range)

        For movie-watching state (post-cal), DYNEQ may be ON; this method
        returns ``calibration_ready`` rather than asserting either way so
        the caller can interpret per-context.
        """
        if not self._host:
            raise DriverError("no host configured")

        base = await self.audyssey_status()

        # Telnet-probe the toggles that aren't on the HTTP API surface.
        replies = {}
        try:
            replies = await self.telnet_query(
                ["PSDYNEQ ?", "PSDYNVOL ?", "PSMULTEQ: ?", "PW?"]
            )
        except DriverError as exc:
            log.warning("Telnet probe of Audyssey toggles failed: %s", exc)

        def _parse_reply(prefix: str) -> str | None:
            for line in replies.values():
                if line.startswith(prefix):
                    return line[len(prefix):].strip().upper()
            return None

        dynamic_eq = _parse_reply("PSDYNEQ ")
        dynamic_volume = _parse_reply("PSDYNVOL ")
        # PSMULTEQ replies as "PSMULTEQ:VALUE" (colon, no space).
        multi_eq_telnet = _parse_reply("PSMULTEQ:")
        power = _parse_reply("PW")

        # Cross-check Telnet vs HTTP for multi_eq; prefer Telnet (more
        # current — HTTP is sometimes cached at session start).
        multi_eq = multi_eq_telnet if multi_eq_telnet else (
            base.get("multi_eq") or "UNKNOWN"
        )

        recommendations: list[str] = []
        cal_ready = True

        if power != "ON":
            cal_ready = False
            recommendations.append(
                f"Power: {power!r} — power on the AVR before calibration"
            )

        sound_mode_upper = (base.get("sound_mode") or "").upper()
        if sound_mode_upper in ("PURE DIRECT", "DIRECT"):
            cal_ready = False
            recommendations.append(
                f"Sound mode: {sound_mode_upper!r} bypasses Audyssey + pushed FIRs — "
                "switch to MULTI CH STEREO, DOLBY SURROUND, or STEREO"
            )

        if multi_eq == "OFF":
            cal_ready = False
            recommendations.append(
                "MultEQ: OFF disables Audyssey filter slot — set PSMULTEQ:FLAT "
                "(via Telnet) before pushing FIRs"
            )

        if dynamic_eq == "ON":
            cal_ready = False
            recommendations.append(
                "Dynamic EQ: ON contaminates calibration measurements with "
                "volume-dependent loudness compensation. Set PSDYNEQ OFF for "
                "calibration; user can re-enable post-cal for movie watching"
            )

        if dynamic_volume == "ON":
            cal_ready = False
            recommendations.append(
                "Dynamic Volume: ON compresses dynamic range — set PSDYNVOL OFF"
            )

        return {
            "power": power,
            "sound_mode": base.get("sound_mode"),
            "multi_eq": multi_eq,
            "dynamic_eq": dynamic_eq,
            "dynamic_volume": dynamic_volume,
            "audyssey_active": base.get("active"),
            "audyssey_active_reason": base.get("reason"),
            "calibration_ready": cal_ready,
            "recommendations": recommendations,
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

    Sound mode is intentionally NOT managed here — the orchestrator sets
    the AVR sound mode before calling measure (via set_avr_mode). The sweep
    context only touches input and volume so it doesn't undo the operator's
    deliberate mode choice.

    Volume safety: sweep_volume must be <= MAX_SWEEP_VOLUME_DB.

    Default lowered to -15 dB after 2026-05-04 incident: a corrupted FIR
    push (SET_DISFIL with empty FilData/DispData) produced loud
    distorted output through MultEQ at sweep_volume_db=0 / AVR -40,
    audible from another room. Reference (0 dB) is appropriate for
    Audyssey baseline measurements where the response is expected; for
    everything else, a lower ceiling protects ears + speakers if the
    audio path is corrupted in a way the safety validator can't see.

    Override per-call by passing sweep_volume_override into
    DenonSweepContext.from_config — but the validator still caps at
    MAX_SWEEP_VOLUME_DB. To raise the absolute ceiling, change this
    constant intentionally, with a code review.
    """

    MAX_SWEEP_VOLUME_DB: float = -15.0  # protective ceiling (was 0)

    @classmethod
    def from_config(
        cls,
        config,
        manage_volume: bool = True,
        sweep_volume_override: float | None = None,
        route_override: str | None = None,
    ) -> "DenonSweepContext | None":
        """Build from a Config object, or return None if HDMI sweep not configured.

        Args:
            manage_volume: If False, skip setting/restoring volume on enter/exit.
                Useful when the caller manages volume itself (e.g. calibrate_level).
            sweep_volume_override: If set, use this volume instead of the config value.
                Useful for mains measurements which need a higher level than subs.
            route_override: If set, use this route instead of config.playback_route.
                Pass the ChainSpec-resolved route so per-channel HDMI sweeps work even
                when config.playback_route is "usb" (e.g. sub-calibration setup).
        """
        route = route_override or config.measurement.get("playback_route", "usb")
        if route != "hdmi":
            return None
        host = config.denon.get("host")
        sweep_input = config.measurement.get("denon_sweep_input")
        if not host or not sweep_input:
            return None
        volume = (
            sweep_volume_override
            if sweep_volume_override is not None
            else float(config.measurement.get("denon_sweep_volume", -10.0))
        )
        return cls(
            host=host,
            sweep_input=sweep_input,
            sweep_volume=volume,
            settle_ms=config.measurement.get("denon_settle_ms", 5000),
            manage_volume=manage_volume,
        )

    def __init__(
        self,
        host: str,
        sweep_input: str,
        sweep_volume: float = -10.0,
        settle_ms: int = 5000,
        manage_volume: bool = True,
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
        self._receiver = None
        self._saved_volume: float | None = None

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

        self._saved_volume = self._receiver.volume
        current_input = self._receiver.input_func

        # Check input precondition — never auto-switch. The orchestrator owns
        # input selection. If the AVR is on the wrong input, raise so the
        # caller can fix it explicitly.
        if current_input != self._sweep_input:
            raise DriverError(
                f"AVR is on input {current_input!r}, expected {self._sweep_input!r}. "
                f"Switch to {self._sweep_input!r} before measuring."
            )

        log.info(
            "Denon sweep: input=%s confirmed%s volume=%s",
            self._sweep_input,
            f", setting volume={self._sweep_volume:.1f} dB" if self._manage_volume else "",
            self._saved_volume,
        )
        if self._manage_volume:
            await self._receiver.async_set_volume(self._sweep_volume)

        await asyncio.sleep(self._settle_ms / 1000.0)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._receiver is None:
            return
        try:
            if self._manage_volume and self._saved_volume is not None:
                log.info("Denon sweep: restoring volume=%s", self._saved_volume)
                await asyncio.wait_for(
                    self._receiver.async_set_volume(self._saved_volume),
                    timeout=5.0,
                )
        except Exception as exc:
            log.warning("Failed to restore Denon volume: %s", exc)
        self._receiver = None
