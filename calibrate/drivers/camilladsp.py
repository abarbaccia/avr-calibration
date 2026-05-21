"""CamillaDSPDriver — DSPDriver implementation for CamillaDSP.

Target signal path: Pi → HDMI capture → CamillaDSP → USB DAC (e.g. 18i20) → subs.
Control: CamillaDSP websocket API (default ws://host:1234) to a daemon running
on the same host (or reachable via SSH tunnel during development).

Design: **driver owns the whole pipeline.** On every state-mutating call
(apply_eq, set_output_gain, …) the driver rebuilds a complete YAML config
from its shadow state and pushes it via SetConfig — the daemon reloads
atomically. There are no preset slots and no named-block surgery.

Shadow state mirrors MinidspDriver so MCP-facing behavior (`get_output_state`,
per-output EQ readback via in-memory cache) stays consistent across drivers.

Differences from MinidspDriver:

- **No preset slots.** `current_preset()` returns 0; `set_preset()` is a no-op.
  The `preset` arg on `apply_eq` / `apply_input_eq` is accepted for protocol
  compatibility but ignored — all state lives on the single active pipeline.
- **Mixer, not matrix routing.** `set_routing` translates an input→output
  enable matrix into a CamillaDSP Mixer stage.
- **FIR is first-class** via inline Conv filters — no temp file, no shared
  tap pool, no 2048-tap ceiling.
- **Master gain via SetVolume**, not a pipeline Gain block.

CamillaDSP websocket protocol:
  Request  — bare string for no-arg commands ("GetVersion") or one-key object
             for commands with args ({"SetVolume": -10.0}).
  Response — one-key object keyed by the command name, value is
             {"result": "Ok"|"Error", "value": <payload>}. On Error the value
             carries the error message string.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from typing import Any

import yaml

from ..dsp import freq_gain_q_to_biquad  # noqa: F401  — used indirectly in tests
from ..safety import FilterSpec, SafetyValidator
from .base import DriverError
from .dsp_driver import DSPCapabilities, DSPDriver

log = logging.getLogger(__name__)


_WS_TIMEOUT_S: float = 5.0
_WS_TIMEOUT_LARGE_S: float = 30.0  # SetConfig with large FIR payloads (~500 KB YAML)


# ── Defaults for the CamillaDSP audio devices ────────────────────────────────
# Callers override via CamillaDSPDriver(..., capture_device=..., playback_device=...)
# or via the `camilladsp` block in config.yaml. The defaults describe the target
# HDMI-capture → USB-DAC path: 2-channel loopback capture feeding a 10-channel
# USB playback (18i20 analog outs).

_DEFAULT_CAPTURE_DEVICE: dict[str, Any] = {
    "type": "Alsa",
    "device": "hw:Loopback,1,0",
    "channels": 2,
    "format": "S32_LE",
}
_DEFAULT_PLAYBACK_DEVICE: dict[str, Any] = {
    "type": "Alsa",
    "device": "hw:USB,0,0",
    "channels": 10,
    "format": "S32_LE",
}


class _CamillaWSClient:
    """Thin async JSON-RPC wrapper over a single CamillaDSP websocket.

    Not reentrant — one call at a time. The driver serialises calls behind its
    own asyncio.Lock.
    """

    def __init__(self, host: str, port: int, timeout: float = _WS_TIMEOUT_S) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout
        self._ws: Any | None = None

    @property
    def connected(self) -> bool:
        return self._ws is not None

    async def connect(self) -> None:
        import websockets

        url = f"ws://{self._host}:{self._port}"
        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(url), timeout=self._timeout
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise DriverError(f"cannot reach CamillaDSP at {url}: {exc}") from exc

    async def close(self) -> None:
        if self._ws is None:
            return
        try:
            await self._ws.close()
        finally:
            self._ws = None

    async def call(self, command: str, value: Any = None) -> Any:
        """Send one command, await its response, return the unwrapped value.

        On a stale connection (server closed socket without notification) the
        first send raises immediately.  We clear the handle and reconnect once
        before giving up so callers never see a "transport error" from a
        connection that simply idled out.

        Raises DriverError on transport failure, unexpected response shape, or
        a non-Ok result from the daemon.
        """
        if self._ws is None:
            raise DriverError("CamillaDSP websocket is not connected")

        # SetConfig / SetConfigJson carry large YAML/JSON payloads (can exceed
        # 500 KB when 4096-tap FIRs are embedded inline).  Use a longer timeout
        # so the send + server reload don't race against a 5-second wall clock.
        timeout = (
            _WS_TIMEOUT_LARGE_S
            if command in {"SetConfig", "SetConfigJson"}
            else self._timeout
        )

        request: Any = command if value is None else {command: value}
        payload = json.dumps(request)
        for attempt in range(2):
            try:
                await asyncio.wait_for(self._ws.send(payload), timeout=timeout)
                raw = await asyncio.wait_for(self._ws.recv(), timeout=timeout)
                break
            except asyncio.TimeoutError as exc:
                raise DriverError(f"CamillaDSP {command} timed out") from exc
            except Exception as exc:
                if attempt == 0:
                    # Connection likely dropped (idle timeout, buffer underrun
                    # recovery, etc.).  Discard the stale handle and reconnect.
                    log.warning(
                        "CamillaDSP %s transport error (%s) — reconnecting", command, exc
                    )
                    self._ws = None
                    try:
                        await self.connect()
                    except DriverError as reconnect_exc:
                        raise DriverError(
                            f"CamillaDSP {command} transport error and reconnect failed: {reconnect_exc}"
                        ) from reconnect_exc
                else:
                    raise DriverError(f"CamillaDSP {command} transport error: {exc}") from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DriverError(f"CamillaDSP {command} returned non-JSON: {raw!r}") from exc

        if not isinstance(data, dict) or command not in data:
            raise DriverError(
                f"CamillaDSP {command} unexpected response shape: {data!r}"
            )

        inner = data[command]
        if not isinstance(inner, dict):
            raise DriverError(f"CamillaDSP {command} malformed inner: {inner!r}")

        result = inner.get("result")
        if result != "Ok":
            msg = inner.get("value") or result or "unknown error"
            raise DriverError(f"CamillaDSP {command} failed: {msg}")

        return inner.get("value")


class _NoOpSweepContext:
    """Trivial async context manager for USB-route sweeps.

    CamillaDSP is the sole owner of the audio hardware (no userland bridge).
    Capture is direct from the multichannel USB DAC, so there is no loopback
    write side to prime and no bridge service to stop. Sweep setup/teardown
    is therefore a no-op at this layer.
    """

    def __init__(self) -> None:
        self.active = False

    async def enter(self) -> "_NoOpSweepContext":
        self.active = True
        return self

    async def exit(self) -> None:
        self.active = False

    async def __aenter__(self) -> "_NoOpSweepContext":
        return await self.enter()

    async def __aexit__(self, *_) -> None:
        await self.exit()


class CamillaDSPDriver(DSPDriver):
    """DSPDriver for CamillaDSP via its websocket control API.

    State-mutating methods (apply_eq, set_output_*, mute_outputs, set_routing,
    apply_fir) update in-memory shadow state under an asyncio.Lock, rebuild the
    full pipeline config, and push it to the daemon via SetConfig. Callers that
    need to inspect what was written should use `get_output_state`.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 1234,
        sub_outputs: list[int] | None = None,
        output_channels: int = 10,
        input_channels: int = 2,
        processing_rate: int = 48_000,
        chunksize: int = 1024,
        capture_device: dict | None = None,
        playback_device: dict | None = None,
        max_peq_slots: int = 16,
        capture_channels: int | None = None,
        lfe_input_channel: int | None = None,
        capture_samplerate: int | None = None,
        resampler: dict | None = None,
        routed_outputs: list[int] | None = None,
        queuelimit: int | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._sub_outputs = sub_outputs or [0, 1]
        # routed_outputs = every transducer output that should receive the
        # LFE input on driver init. Distinct from _sub_outputs (which scopes
        # sub-only operations like default FIR-clear targets) so the shaker
        # and any future non-sub transducer also get default routing without
        # being mistakenly treated as a sub. Falls back to _sub_outputs for
        # back-compat with callers that don't supply the new param.
        self._routed_outputs = list(routed_outputs) if routed_outputs is not None else list(self._sub_outputs)
        self._output_channels = output_channels
        self._input_channels = input_channels
        # capture_channels is the PHYSICAL channel count opened on the ALSA
        # capture device. input_channels is the LOGICAL count seen by the
        # lfe_source + output_router mixers. They differ when CamillaDSP
        # captures a multichannel device (e.g. Focusrite 18i20) and only a
        # single physical channel carries the LFE signal.
        self._capture_channels = int(capture_channels) if capture_channels else input_channels
        self._lfe_input_channel = int(lfe_input_channel) if lfe_input_channel is not None else None
        if self._capture_channels != self._input_channels and self._lfe_input_channel is None:
            raise ValueError(
                f"capture_channels={self._capture_channels} != input_channels={self._input_channels} "
                f"requires lfe_input_channel to specify which physical channel to fan out"
            )
        if self._lfe_input_channel is not None and not (0 <= self._lfe_input_channel < self._capture_channels):
            raise ValueError(
                f"lfe_input_channel={self._lfe_input_channel} out of range "
                f"for capture_channels={self._capture_channels}"
            )
        self._processing_rate = processing_rate
        # Multi-rate processing: when capture_samplerate is set and differs from
        # processing_rate, CamillaDSP inserts a synchronous resampler between the
        # capture device and the pipeline. Use this to run sub-band processing
        # (long FIRs for room correction) at a low rate (e.g. 8 kHz) while the
        # capture device runs at a normal rate (48 kHz). The CPU saving is roughly
        # (capture_rate / processing_rate) for FFT-partitioned convolution work.
        # Playback always runs at processing_rate; use a `plug` ALSA device on
        # the playback side so the kernel handles upsampling to the hardware rate.
        self._capture_samplerate = int(capture_samplerate) if capture_samplerate else None
        self._resampler = dict(resampler) if resampler else None
        if self._capture_samplerate is not None and self._capture_samplerate == self._processing_rate:
            # No-op rate equality — drop both fields so the emitted config stays
            # minimal and CamillaDSP doesn't insert a pointless resampler.
            self._capture_samplerate = None
            self._resampler = None
        self._chunksize = chunksize
        self._queuelimit = int(queuelimit) if queuelimit is not None else None
        self._capture_device = dict(capture_device) if capture_device else dict(_DEFAULT_CAPTURE_DEVICE)
        self._playback_device = dict(playback_device) if playback_device else dict(_DEFAULT_PLAYBACK_DEVICE)
        # Keep the declared device channel counts aligned with the driver's
        # channel counts — CamillaDSP will reject a mismatched config otherwise.
        self._capture_device["channels"] = self._capture_channels
        self._playback_device["channels"] = output_channels

        self._lock = asyncio.Lock()
        self._max_peq_slots = max_peq_slots
        self._client = _CamillaWSClient(host, port)

        # Default routing: input 0 → every transducer output declared by
        # the signal graph (subs + shaker + future non-sub channels). Other
        # outputs silent. Broadcasting a sweep to every 18i20 analog output
        # at full gain would blast any wired main/tweeter, so the driver
        # starts in the minimum safe state — but "minimum safe" must
        # include the shaker, which used to be missing because routing was
        # keyed off `sub_outputs` alone (that bug silenced the shaker on
        # every container restart, fixed 2026-05-02).
        self._routing: dict[int, dict[int, bool]] = {
            inp: {out: False for out in range(output_channels)}
            for inp in range(input_channels)
        }
        for out in self._routed_outputs:
            self._routing[0][int(out)] = True

        # Shadow state — each mutation rebuilds the pipeline from this.
        self._output_eq: dict[int, list[dict]] = {}   # output_index → filter specs
        self._input_eq: dict[int, list[dict]] = {}    # input_index → filter specs
        self._input_gain: dict[int, float] = {}       # input_index → gain_db (flat make-up gain pre-mixer)
        self._output_gain: dict[int, float] = {}
        self._output_delay: dict[int, float] = {}
        self._output_polarity: dict[int, bool] = {}
        self._output_muted: dict[int, bool] = {}
        self._fir_state: dict[int, list[float]] = {}

        # Back-compat: expose _eq_state for any code that still reads the
        # MinidspDriver-shaped shadow. Populated from _output_eq/_input_eq
        # on push so external readers see consistent state.
        self._eq_state: dict = {}

    @property
    def capabilities(self) -> DSPCapabilities:
        # CamillaDSP delay is bounded by chunk size, not architectural — 1 s is
        # a generous ceiling that no sensible calibration will ever hit.
        # Preset slots don't exist (single active pipeline); max_preset_index=-1.
        # Source switching isn't a CamillaDSP concept; valid_sources is empty.
        # FIR: per-channel Conv filters have no architectural tap ceiling; 65536
        # (≈1.37 s at 48 kHz) is plenty for sub-bass decay shaping. No shared pool.
        return DSPCapabilities(
            max_delay_ms=1000.0,
            max_preset_index=-1,
            valid_sources=frozenset(),
            processing_rate=self._processing_rate,
            max_peq_slots=self._max_peq_slots,
            fir_capable=True,
            fir_min_taps=64,
            fir_max_taps_per_output=65536,
            fir_shared_tap_pool=None,
            fir_sample_rate_hz=self._processing_rate,
        )

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def setup(self) -> None:
        """Open the websocket and confirm the daemon is alive.

        Intentionally does **not** push a default pipeline. On a fresh install
        the daemon retains whatever config it was started with (e.g. the
        user's ``initial.yml``) until the first state mutation (apply_eq /
        set_output_* / etc.) causes the driver to push its shadow. On a
        restart, ``rehydrate_from_active_state`` repopulates the shadow from
        persisted calibration state and pushes once — so a mid-session MCP
        restart doesn't wipe the running calibration.
        """
        async with self._lock:
            await self._client.connect()
            try:
                version = await self._client.call("GetVersion")
            except DriverError:
                await self._client.close()
                raise
            log.info("CamillaDSP %s at %s:%d", version, self._host, self._port)

    async def close(self) -> None:
        async with self._lock:
            await self._client.close()

    # ── state readback ────────────────────────────────────────────────────────

    async def get_state(self) -> dict:
        """Return current CamillaDSP hardware state.

        Returns {connected, host, state, volume, mute, cpu_load, source, preset}.
        `source` and `preset` are stubbed for DSPDriver protocol compatibility —
        CamillaDSP has neither concept.
        """
        async with self._lock:
            if not self._client.connected:
                return {"connected": False, "host": self._host}

            state = await self._client.call("GetState")
            volume = await self._client.call("GetVolume")
            mute = await self._client.call("GetMute")
            try:
                cpu_load = await self._client.call("GetProcessingLoad")
            except DriverError:
                cpu_load = None

            return {
                "connected": True,
                "host": self._host,
                "state": state,
                "volume": volume,
                "mute": mute,
                "cpu_load": cpu_load,
                # Present for DSPDriver protocol compatibility — not meaningful
                # on CamillaDSP, but callers may read them unconditionally.
                "source": None,
                "preset": 0,
            }

    async def current_preset(self) -> int:
        # CamillaDSP has no preset slots; the single active pipeline is preset 0.
        return 0

    def get_output_state(self) -> dict[int, dict]:
        """Return in-memory per-output shadow state.

        Same shape as MinidspDriver.get_output_state. Covers the configured
        output channel count (default 10 for the 18i20 analog outs).
        """
        return {
            idx: {
                "gain_db": self._output_gain.get(idx, 0.0),
                "delay_ms": self._output_delay.get(idx, 0.0),
                "polarity_inverted": self._output_polarity.get(idx, False),
                "fir_taps": len(self._fir_state.get(idx, [])),
            }
            for idx in range(self._output_channels)
        }

    def get_mute_state(self) -> dict[int, bool]:
        """Return tracked per-output mute state (only includes explicitly set outputs)."""
        return dict(self._output_muted)

    def _has_pending_state(self) -> bool:
        """True when shadow carries any calibration state worth pushing.

        Used to decide whether a post-rehydrate push is necessary: a fresh
        install with no prior state leaves the daemon's ``initial.yml`` alone;
        a restarted session pushes the rehydrated filters once so the daemon
        matches the shadow.
        """
        return bool(
            self._output_eq or self._input_eq or self._input_gain
            or self._output_gain or self._output_delay
            or self._output_polarity or self._output_muted
            or any(v for v in self._fir_state.values())
        )

    async def rehydrate_from_active_state(
        self, active_state: dict[str, dict],
    ) -> None:
        """Rebuild shadow from persisted active_dsp_state, then push to daemon.

        Called by the MCP server lifespan after ``setup``. Accepts the
        namespaced key shape ``processor:<name>:output:<idx>:<field>`` (and
        the input variant). Legacy flat keys are migrated by the storage
        layer before they reach this method, so parsing them here is purely
        defensive.

        The push at the end reconciles the daemon with the shadow — without
        it a mid-session MCP restart would leave the daemon at its startup
        config while the driver's shadow claims the last-applied filters are
        live. Only pushes when the shadow has content; a fresh install with
        empty ``active_dsp_state`` stays a pure no-op so the daemon's
        initial.yml remains untouched.
        """
        from ..storage import parse_dsp_key

        for key, data in active_state.items():
            parsed = parse_dsp_key(key)
            if parsed is None:
                continue
            try:
                if parsed["kind"] == "output":
                    idx = parsed["output_index"]
                    field = parsed["field"]
                    if field == "eq":
                        filters = data.get("filters", [])
                        if filters:
                            self._output_eq[idx] = list(filters)
                            self._eq_state[(0, idx)] = list(filters)
                    elif field == "gain":
                        self._output_gain[idx] = float(data["gain_db"])
                    elif field == "delay":
                        self._output_delay[idx] = float(data["delay_ms"])
                    elif field == "polarity":
                        self._output_polarity[idx] = bool(data["inverted"])
                    elif field == "fir":
                        coeffs = data.get("coefficients", [])
                        if coeffs:
                            self._fir_state[idx] = [float(c) for c in coeffs]
                    elif field == "mute":
                        self._output_muted[idx] = bool(data["muted"])
                elif parsed["kind"] == "input" and parsed["field"] == "eq":
                    filters = data.get("filters", [])
                    if filters:
                        # CamillaDSP has no active_input concept — every
                        # input channel gets the shared filter set, matching
                        # apply_input_eq's broadcast semantics.
                        for inp in range(self._input_channels):
                            self._input_eq[inp] = list(filters)
                            self._eq_state[("input", inp, 0)] = list(filters)
                elif parsed["kind"] == "input" and parsed["field"] == "gain":
                    inp = parsed.get("input_index")
                    if inp is None:
                        continue
                    self._input_gain[int(inp)] = float(data["gain_db"])
            except (KeyError, ValueError, TypeError) as exc:
                log.warning("rehydrate_from_active_state: skipping key=%s: %s", key, exc)

        if self._has_pending_state():
            async with self._lock:
                await self._push_config_locked()
            log.info("CamillaDSP rehydrate: shadow restored and pushed to daemon")

    # ── pipeline construction ────────────────────────────────────────────────

    def _parse_filter_specs(self, filters: list[dict]) -> list[FilterSpec]:
        try:
            return [
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

    @staticmethod
    def _filter_block(spec: FilterSpec) -> dict:
        """Map a FilterSpec to a CamillaDSP filter block ({type, parameters}).

        CamillaDSP accepts canonical filter types directly — no need to
        pre-compute biquad coefficients. Most map to a Biquad block; higher-order
        HPF/LPF map to a BiquadCombo block so the full Butterworth cascade is
        a single CamillaDSP filter.
        """
        t = spec.type.lower()
        # Peaking and shelves: Biquad with {freq, q, gain}
        if t in {"peaking", "pk", "peq"}:
            return {
                "type": "Biquad",
                "parameters": {
                    "type": "Peaking", "freq": spec.freq, "q": spec.q,
                    "gain": spec.gain_db,
                },
            }
        if t in {"high_shelf", "highshelf", "hs"}:
            return {
                "type": "Biquad",
                "parameters": {
                    "type": "Highshelf", "freq": spec.freq, "q": spec.q,
                    "gain": spec.gain_db,
                },
            }
        if t in {"low_shelf", "lowshelf", "ls"}:
            return {
                "type": "Biquad",
                "parameters": {
                    "type": "Lowshelf", "freq": spec.freq, "q": spec.q,
                    "gain": spec.gain_db,
                },
            }
        if t in {"notch", "no"}:
            return {
                "type": "Biquad",
                "parameters": {"type": "Notch", "freq": spec.freq, "q": spec.q},
            }
        if t in {"allpass", "ap", "apf"}:
            # Unity-magnitude phase-only filter. CamillaDSP "Allpass" takes
            # only freq + q; gain_db is meaningless and ignored.
            return {
                "type": "Biquad",
                "parameters": {"type": "Allpass", "freq": spec.freq, "q": spec.q},
            }
        # 4th-order Butterworth HPF/LPF → BiquadCombo (single filter, two sections).
        if t in {"hpf", "highpass"}:
            return {
                "type": "BiquadCombo",
                "parameters": {
                    "type": "ButterworthHighpass",
                    "freq": spec.freq,
                    "order": 4,
                },
            }
        if t in {"lpf", "lowpass"}:
            return {
                "type": "BiquadCombo",
                "parameters": {
                    "type": "ButterworthLowpass",
                    "freq": spec.freq,
                    "order": 4,
                },
            }
        raise DriverError(f"unsupported filter type for CamillaDSP: {spec.type!r}")

    def _build_filters(self) -> dict[str, dict]:
        filters: dict[str, dict] = {}

        # Per-input flat gain (pre-PEQ, pre-mixer). Make-up gain for upstream
        # attenuation lives here so per-output gains stay alignment-only.
        for inp_idx, gain in self._input_gain.items():
            if gain == 0.0:
                continue
            filters[f"cal_in{inp_idx}_gain"] = {
                "type": "Gain",
                "parameters": {
                    "gain": float(gain),
                    "inverted": False,
                    "mute": False,
                },
            }

        # Per-input PEQ
        for inp_idx, specs_raw in self._input_eq.items():
            specs = self._parse_filter_specs(specs_raw)
            for i, spec in enumerate(specs):
                filters[f"cal_in{inp_idx}_peq_{i}"] = self._filter_block(spec)

        # Per-output PEQ
        for out_idx, specs_raw in self._output_eq.items():
            specs = self._parse_filter_specs(specs_raw)
            for i, spec in enumerate(specs):
                filters[f"cal_out{out_idx}_peq_{i}"] = self._filter_block(spec)

        # Per-output Delay (always present — pass-through at 0 ms)
        for out_idx in range(self._output_channels):
            delay_ms = self._output_delay.get(out_idx, 0.0)
            filters[f"cal_out{out_idx}_delay"] = {
                "type": "Delay",
                "parameters": {
                    "delay": float(delay_ms),
                    "unit": "ms",
                    "subsample": True,
                },
            }

        # Per-output Gain (covers gain + polarity + mute in one block)
        for out_idx in range(self._output_channels):
            filters[f"cal_out{out_idx}_gain"] = {
                "type": "Gain",
                "parameters": {
                    "gain": float(self._output_gain.get(out_idx, 0.0)),
                    "inverted": bool(self._output_polarity.get(out_idx, False)),
                    "mute": bool(self._output_muted.get(out_idx, False)),
                },
            }

        # Per-output FIR (only when coefficients are set)
        for out_idx, coeffs in self._fir_state.items():
            if not coeffs:
                continue
            filters[f"cal_out{out_idx}_fir"] = {
                "type": "Conv",
                "parameters": {
                    "type": "Values",
                    "values": [float(c) for c in coeffs],
                },
            }

        return filters

    def _build_capture_mixer(self) -> dict | None:
        """Build the lfe_source mixer that fans the LFE physical channel to logical inputs.

        Returns ``None`` when capture_channels == input_channels (no reshape needed).
        Otherwise picks lfe_input_channel from the physical capture stream and fans
        it out to all logical input channels.
        """
        if self._capture_channels == self._input_channels:
            return None
        assert self._lfe_input_channel is not None
        mapping: list[dict] = []
        for logical in range(self._input_channels):
            mapping.append({
                "dest": logical,
                "sources": [{
                    "channel": self._lfe_input_channel,
                    "gain": 0.0,
                    "inverted": False,
                    "mute": False,
                }],
            })
        return {
            "channels": {"in": self._capture_channels, "out": self._input_channels},
            "mapping": mapping,
        }

    def _build_mixer(self) -> dict:
        """Build the input→output routing mixer.

        Each output channel lists exactly those inputs routed to it (enabled).
        Outputs with no routed input get no sources (silence).

        Muted outputs keep their source entries (routing stays inspectable in
        GetConfigJson) but flip every source's ``mute: true``. This silences
        the mixer output upstream of the per-output FIR/Delay/PEQ chain — so
        the chain receives zero input and produces zero output, sidestepping
        the run-15 issue where the per-output Gain filter's ``mute: true``
        was not honored when Conv/Delay sat earlier in the per-output pipeline
        (PR #96). The earlier fix worked by dropping mixer sources entirely,
        which silently rewrote routing on every mute call and confused any
        consumer that read the live config to verify state. Muting at the
        mixer-source level is upstream of the failing per-output Gain, so
        the same FIR-leak failure mode does not apply.
        """
        mapping: list[dict] = []
        muted_output = lambda o: self._output_muted.get(o, False)
        for out_idx in range(self._output_channels):
            sources = []
            for inp_idx in range(self._input_channels):
                enabled = self._routing.get(inp_idx, {}).get(out_idx, False)
                if enabled:
                    sources.append({
                        "channel": inp_idx,
                        "gain": 0.0,
                        "inverted": False,
                        "mute": muted_output(out_idx),
                    })
            mapping.append({"dest": out_idx, "sources": sources})
        mixers: dict = {
            "output_router": {
                "channels": {"in": self._input_channels, "out": self._output_channels},
                "mapping": mapping,
            }
        }
        capture_mixer = self._build_capture_mixer()
        if capture_mixer is not None:
            mixers["lfe_source"] = capture_mixer
        return mixers

    def _build_pipeline(self) -> list[dict]:
        """Emit pipeline steps: input PEQ → mixer → per-output processing.

        CamillaDSP 2.x+ pipeline Filter steps use ``channels: [N]`` (list, plural)
        rather than ``channel: N`` (scalar, singular) — the list form lets a
        single step apply identical filters to several channels at once.
        """
        steps: list[dict] = []

        # lfe_source mixer fans the single LFE physical channel to all logical inputs.
        if self._capture_channels != self._input_channels:
            steps.append({"type": "Mixer", "name": "lfe_source"})

        # Input-side flat gain (pre-PEQ, per input channel).
        for inp_idx in range(self._input_channels):
            if self._input_gain.get(inp_idx, 0.0) == 0.0:
                continue
            steps.append({
                "type": "Filter",
                "channels": [inp_idx],
                "names": [f"cal_in{inp_idx}_gain"],
            })

        # Input-side PEQ, per input channel (pre-mixer).
        for inp_idx in range(self._input_channels):
            specs = self._input_eq.get(inp_idx, [])
            if not specs:
                continue
            names = [f"cal_in{inp_idx}_peq_{i}" for i in range(len(specs))]
            steps.append({"type": "Filter", "channels": [inp_idx], "names": names})

        # Routing mixer — always present so the router sees the channel count change.
        steps.append({"type": "Mixer", "name": "output_router"})

        # Per-output processing. FIR first (if set), then PEQ, then delay, then
        # gain/polarity/mute as a single Gain block.
        for out_idx in range(self._output_channels):
            names: list[str] = []
            if self._fir_state.get(out_idx):
                names.append(f"cal_out{out_idx}_fir")
            eq_count = len(self._output_eq.get(out_idx, []))
            names.extend(f"cal_out{out_idx}_peq_{i}" for i in range(eq_count))
            names.append(f"cal_out{out_idx}_delay")
            names.append(f"cal_out{out_idx}_gain")
            steps.append({"type": "Filter", "channels": [out_idx], "names": names})

        return steps

    def _build_config(self) -> dict:
        """Assemble the full CamillaDSP config dict from shadow state."""
        devices: dict = {
            "samplerate": self._processing_rate,
            "chunksize": self._chunksize,
            "capture": self._capture_device,
            "playback": self._playback_device,
        }
        if self._capture_samplerate is not None:
            devices["capture_samplerate"] = self._capture_samplerate
        if self._resampler is not None:
            devices["resampler"] = dict(self._resampler)
        if self._queuelimit is not None:
            devices["queuelimit"] = self._queuelimit
        return {
            "devices": devices,
            "filters": self._build_filters(),
            "mixers": self._build_mixer(),
            "pipeline": self._build_pipeline(),
        }

    async def _push_config_locked(self) -> None:
        """Push the current shadow state as a CamillaDSP config.

        Caller must hold self._lock.
        """
        config = self._build_config()
        config_yaml = yaml.safe_dump(config, default_flow_style=False, sort_keys=False)
        await self._client.call("SetConfig", config_yaml)

    # ── Cal-mode capture (loopback) ───────────────────────────────────────────

    async def _sync_fir_state_from_active_locked(self) -> None:
        """Pull Conv FIR coefficients from the running CamillaDSP config into shadow state.

        Caller must hold ``self._lock``. Preserves FIR coefficients applied
        outside this driver (e.g. via SetConfigJson directly) so a subsequent
        pipeline rebuild doesn't revert them to shadow state.

        Only Conv filters named ``cal_out{N}_fir`` are synced — those are the
        per-output FIR slots this driver owns. Other Conv filters are ignored.
        """
        try:
            raw = await self._client.call("GetConfigJson")
        except DriverError as exc:
            log.warning("FIR resync: GetConfigJson failed (%s); skipping", exc)
            return
        if not isinstance(raw, str):
            return
        try:
            cfg = json.loads(raw)
        except json.JSONDecodeError:
            return
        filters = (cfg or {}).get("filters") or {}
        if not isinstance(filters, dict):
            return
        for name, spec in filters.items():
            if not isinstance(spec, dict) or spec.get("type") != "Conv":
                continue
            if not (name.startswith("cal_out") and name.endswith("_fir")):
                continue
            try:
                idx = int(name[len("cal_out"):-len("_fir")])
            except ValueError:
                continue
            params = spec.get("parameters") or {}
            if params.get("type") != "Values":
                continue
            values = params.get("values")
            if not isinstance(values, list) or not values:
                continue
            self._fir_state[idx] = [float(c) for c in values]

    # ── EQ ────────────────────────────────────────────────────────────────────

    async def apply_eq(
        self,
        preset: int,
        filters: list[dict],
        output_index: int | None = None,
        simulation_verified: bool = False,
        bypass_iteration_limit: bool = False,
    ) -> None:
        """Validate and apply output EQ, then push the pipeline.

        *preset* is accepted for protocol compatibility and ignored — CamillaDSP
        has no preset slots. When *output_index* is None, the filters are
        applied to every configured sub output.
        """
        filter_specs = self._parse_filter_specs(filters)
        if len(filter_specs) > self._max_peq_slots:
            raise DriverError(
                f"too many filters: {len(filter_specs)} > {self._max_peq_slots} PEQ slots"
            )

        targets = [output_index] if output_index is not None else list(self._sub_outputs)

        async with self._lock:
            validator = SafetyValidator()
            # Use the widest existing per-output EQ as the baseline — matches the
            # MinidspDriver behavior of validating against the active state.
            prev_specs: list[FilterSpec] | None = None
            for tgt in targets:
                existing = self._output_eq.get(tgt, [])
                if existing:
                    prev_specs = self._parse_filter_specs(existing)
                    break

            result = validator.validate(
                filter_specs, prev_specs,
                simulation_verified=simulation_verified,
                bypass_iteration_limit=bypass_iteration_limit,
            )
            if not result.ok:
                raise DriverError(f"SafetyValidator: {result.error}")

            filter_record = [
                {"freq": f.freq, "gain_db": f.gain_db, "q": f.q, "type": f.type}
                for f in filter_specs
            ]
            # Update shadow state first so _build_config reflects the new filters.
            for tgt in targets:
                self._output_eq[tgt] = list(filter_record)
                self._eq_state[(preset, tgt)] = list(filter_record)

            try:
                await self._push_config_locked()
            except DriverError:
                # Roll shadow back — caller's baseline must stay accurate.
                for tgt in targets:
                    self._output_eq.pop(tgt, None)
                    self._eq_state.pop((preset, tgt), None)
                raise

    async def apply_input_eq(
        self,
        preset: int,
        filters: list[dict],
        input_index: int | None = None,
        simulation_verified: bool = False,
        bypass_iteration_limit: bool = False,
    ) -> None:
        """Validate and apply input EQ, then push the pipeline.

        *preset* is accepted for protocol compatibility and ignored. When
        *input_index* is None, the filters are written to every configured
        input channel so the EQ applies regardless of which input the signal
        arrives on (same semantics as MinidspDriver's dual-input write).
        """
        filter_specs = self._parse_filter_specs(filters)
        if len(filter_specs) > self._max_peq_slots:
            raise DriverError(
                f"too many filters: {len(filter_specs)} > {self._max_peq_slots} PEQ slots"
            )

        if input_index is not None:
            target_inputs = [input_index]
        else:
            target_inputs = list(range(self._input_channels))

        async with self._lock:
            validator = SafetyValidator()
            prev_specs: list[FilterSpec] | None = None
            for inp in target_inputs:
                existing = self._input_eq.get(inp, [])
                if existing:
                    prev_specs = self._parse_filter_specs(existing)
                    break

            result = validator.validate(
                filter_specs, prev_specs,
                simulation_verified=simulation_verified,
                bypass_iteration_limit=bypass_iteration_limit,
            )
            if not result.ok:
                raise DriverError(f"SafetyValidator: {result.error}")

            filter_record = [
                {"freq": f.freq, "gain_db": f.gain_db, "q": f.q, "type": f.type}
                for f in filter_specs
            ]
            prev_inputs = {inp: list(self._input_eq.get(inp, [])) for inp in target_inputs}
            for inp in target_inputs:
                self._input_eq[inp] = list(filter_record)
                self._eq_state[("input", inp, preset)] = list(filter_record)

            try:
                await self._push_config_locked()
            except DriverError:
                for inp in target_inputs:
                    if prev_inputs[inp]:
                        self._input_eq[inp] = prev_inputs[inp]
                    else:
                        self._input_eq.pop(inp, None)
                    self._eq_state.pop(("input", inp, preset), None)
                raise

    # ── preset / source ──────────────────────────────────────────────────────

    async def set_preset(self, preset: int) -> None:
        # No-op: CamillaDSP has no preset slots. Log so it's visible when callers
        # assume miniDSP-style preset switching.
        log.debug("set_preset(%d): no-op — CamillaDSP has no preset slots", preset)

    # ── routing ──────────────────────────────────────────────────────────────

    async def set_routing(self, routing: dict) -> None:
        """Update the input→output routing matrix.

        *routing* maps input_index (int) → {output_index (int): enabled (bool)}.
        Existing rows not mentioned in *routing* are left untouched.
        """
        async with self._lock:
            # Shallow-merge into the existing matrix so partial updates work.
            prev = {k: dict(v) for k, v in self._routing.items()}
            for inp, out_map in routing.items():
                inp = int(inp)
                row = self._routing.setdefault(inp, {})
                for out, enabled in out_map.items():
                    row[int(out)] = bool(enabled)
            try:
                await self._push_config_locked()
            except DriverError:
                self._routing = prev
                raise

    # ── mute / gain / delay / polarity ───────────────────────────────────────

    async def mute_outputs(self, output_indices: list[int]) -> None:
        async with self._lock:
            prev = {idx: self._output_muted.get(idx, False) for idx in output_indices}
            for idx in output_indices:
                self._output_muted[int(idx)] = True
            try:
                await self._push_config_locked()
            except DriverError:
                for idx, val in prev.items():
                    self._output_muted[idx] = val
                raise

    async def unmute_outputs(self, output_indices: list[int]) -> None:
        async with self._lock:
            prev = {idx: self._output_muted.get(idx, False) for idx in output_indices}
            for idx in output_indices:
                self._output_muted[int(idx)] = False
            try:
                await self._push_config_locked()
            except DriverError:
                for idx, val in prev.items():
                    self._output_muted[idx] = val
                raise

    async def set_output_gain(self, output_index: int, gain_db: float) -> None:
        async with self._lock:
            prev = self._output_gain.get(output_index)
            self._output_gain[int(output_index)] = float(gain_db)
            try:
                await self._push_config_locked()
            except DriverError:
                if prev is None:
                    self._output_gain.pop(output_index, None)
                else:
                    self._output_gain[output_index] = prev
                raise

    async def set_input_gain(self, input_index: int, gain_db: float) -> None:
        """Set flat gain (dB) on a DSP input channel, pre-PEQ and pre-mixer.

        Make-up gain for upstream attenuation (e.g. a turned-down Scarlett
        analog-in trim). Lives in its own layer so per-output gains stay
        alignment-only and input PEQ stays target-curve-only.
        """
        async with self._lock:
            prev = self._input_gain.get(input_index)
            self._input_gain[int(input_index)] = float(gain_db)
            try:
                await self._push_config_locked()
            except DriverError:
                if prev is None:
                    self._input_gain.pop(input_index, None)
                else:
                    self._input_gain[input_index] = prev
                raise

    async def set_output_delay(self, output_index: int, delay_ms: float) -> None:
        async with self._lock:
            prev = self._output_delay.get(output_index)
            self._output_delay[int(output_index)] = float(delay_ms)
            try:
                await self._push_config_locked()
            except DriverError:
                if prev is None:
                    self._output_delay.pop(output_index, None)
                else:
                    self._output_delay[output_index] = prev
                raise

    async def set_output_polarity(self, output_index: int, inverted: bool) -> None:
        async with self._lock:
            prev = self._output_polarity.get(output_index)
            self._output_polarity[int(output_index)] = bool(inverted)
            try:
                await self._push_config_locked()
            except DriverError:
                if prev is None:
                    self._output_polarity.pop(output_index, None)
                else:
                    self._output_polarity[output_index] = prev
                raise

    async def set_master_gain(self, gain_db: float) -> None:
        """Set master output volume via the global SetVolume command.

        CamillaDSP keeps master volume separate from pipeline gain — this goes
        through `SetVolume` (dB), not a Gain block, so it survives config reloads.
        """
        async with self._lock:
            await self._client.call("SetVolume", float(gain_db))

    def sweep_context(self, config):
        """Return an async context manager that prepares CamillaDSP for a sweep.

        USB route: no-op — CamillaDSP captures directly from the USB DAC, no
        bridge or loopback priming required.

        HDMI route: returns a ``DSPHDMISweepContext`` for master-gain management.
        """
        from .dsp_driver import DSPHDMISweepContext
        route = config.measurement.get("playback_route", "usb")
        if route == "hdmi":
            return DSPHDMISweepContext(self, config)
        return _NoOpSweepContext()

    async def pipeline_state(self) -> str:
        """Return the CamillaDSP pipeline state string.

        Calls ``GetState`` over the websocket and returns the state value
        (e.g. ``"Running"``, ``"Inactive"``, ``"Error"``). Returns
        ``"Unknown"`` on any error (not connected, timeout, etc.) so
        callers can treat an unknown state as a warning without raising.
        """
        try:
            async with self._lock:
                if not self._client.connected:
                    return "Unknown"
                state = await self._client.call("GetState")
                if isinstance(state, str):
                    return state
                return str(state) if state is not None else "Unknown"
        except Exception:
            return "Unknown"

    # ── FIR ───────────────────────────────────────────────────────────────────

    def get_fir_pre_delay_ms(self) -> float:
        """Estimate the maximum FIR pre-ring window across all outputs.

        Returns the longest duration (in ms) of significant non-causal
        content among loaded FIRs — anti-pulse modal cancellation places
        energy before the main impulse, and the IR onset detector needs
        to know how much leading window to skip.

        Heuristic: locate the main-impulse sample (max |coeff|), measure
        how many samples of significant content precede it (envelope above
        -20 dB of peak), convert to ms at the FIR sample rate. Returns 0
        if no FIRs are loaded.
        """
        try:
            import numpy as _np
        except Exception:
            return 0.0
        sr = float(self.capabilities.fir_sample_rate_hz or 8000)
        if sr <= 0:
            return 0.0
        worst_ms = 0.0
        for coeffs in self._fir_state.values():
            if not coeffs:
                continue
            arr = _np.asarray(coeffs, dtype=float)
            if arr.size == 0:
                continue
            abs_arr = _np.abs(arr)
            peak = float(abs_arr.max())
            if peak <= 0:
                continue
            main_idx = int(_np.argmax(abs_arr))
            threshold = peak * 0.10  # -20 dB
            # Find earliest sample BEFORE the main impulse that exceeds threshold.
            pre = abs_arr[:main_idx]
            above = _np.where(pre >= threshold)[0]
            if len(above) == 0:
                continue
            pre_samples = main_idx - int(above[0])
            ms = 1000.0 * pre_samples / sr
            if ms > worst_ms:
                worst_ms = ms
        return worst_ms

    async def apply_fir(
        self,
        output_index: int,
        coefficients: list[float],
        sample_rate: int | None = None,
        intent: str = "general",
    ) -> None:
        """Write FIR coefficients to a single output.

        *sample_rate* is accepted for parity with MinidspDriver.apply_fir but
        must match the processing rate — CamillaDSP applies Conv filters at the
        pipeline rate, with no per-filter resampling.

        *intent* selects the FIR safety profile: ``"general"`` (default,
        strict thermal/excursion cap) or ``"modal_cancel"`` (looser cap for
        anti-pulse modal-cancellation FIRs from ``design_modal_fir`` whose
        magnitude is intentionally hot at the mode but cancels at the
        listener — see SafetyValidator.validate_fir docstring).
        """
        caps = self.capabilities
        if not coefficients:
            raise DriverError("coefficients list is empty; use clear_fir() to reset")
        if len(coefficients) > caps.fir_max_taps_per_output:
            raise DriverError(
                f"too many FIR taps: {len(coefficients)} > {caps.fir_max_taps_per_output}"
            )
        if sample_rate is not None and sample_rate != caps.fir_sample_rate_hz:
            raise DriverError(
                f"FIR sample rate {sample_rate} != processing rate {caps.fir_sample_rate_hz}"
            )

        # Safety: validate the FIR's magnitude response against the default
        # profile (SVS PB12-NSD) before any pipeline write, matching the
        # existing apply_eq behaviour. MCP server may re-validate against a
        # transducer-specific profile — that's belt-and-braces, not duplicate.
        from ..safety import SafetyValidationError, SafetyValidator
        try:
            SafetyValidator().validate_fir(
                list(coefficients),
                sample_rate=int(sample_rate or caps.fir_sample_rate_hz),
                intent=intent,
            )
        except SafetyValidationError as exc:
            raise DriverError(str(exc))

        async with self._lock:
            prev = list(self._fir_state.get(output_index, []))
            self._fir_state[int(output_index)] = [float(c) for c in coefficients]
            try:
                await self._push_config_locked()
            except DriverError:
                if prev:
                    self._fir_state[output_index] = prev
                else:
                    self._fir_state.pop(output_index, None)
                raise

    async def clear_fir(self, output_index: int) -> None:
        """Clear FIR coefficients on a single output (filter removed from pipeline)."""
        async with self._lock:
            prev = list(self._fir_state.get(output_index, []))
            self._fir_state[int(output_index)] = []
            try:
                await self._push_config_locked()
            except DriverError:
                if prev:
                    self._fir_state[output_index] = prev
                else:
                    self._fir_state.pop(output_index, None)
                raise
