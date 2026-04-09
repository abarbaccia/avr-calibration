"""Adapter for minidspd — status reads via HTTP, all writes via CLI.

minidspd exposes a local REST API for reads.  ALL writes go through the
minidsp CLI (WebSocket transport) because HTTP config writes reset routing
and PEQ state, and the HTTP config API has a sign bug in the a1/a2 biquad
coefficients that causes DSP hangs requiring physical power-cycle to recover.

HTTP (reads only):
  GET  /devices                         → list connected devices
  GET  /devices/{idx}                   → master status (preset, source, volume, mute)

CLI (all writes):
  minidsp source <name>                 → switch source (Analog/Toslink/Usb)
  minidsp preset <N>                    → switch preset (0-3)
  minidsp output <N> gain -- <dB>       → set output gain
  minidsp output <N> mute on|off        → set output mute
  minidsp output <N> delay <ms>         → set output delay
  minidsp output <N> invert on|off      → set output polarity
  minidsp output <N> peq <slot> set ... → write biquad coefficients
  minidsp output <N> fir import <path>  → write FIR coefficients
  minidsp input  <N> routing <out> ...  → configure routing matrix

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

FIR_MAX_TAPS_PER_OUTPUT: int = 2048
"""Maximum FIR taps per output channel on the miniDSP 2x4 HD."""

FIR_SHARED_TAP_POOL: int = 4096
"""Total FIR taps shared across all 4 outputs. Each output takes from this pool."""

FIR_SAMPLE_RATE: int = 96000
"""Internal sample rate of the miniDSP 2x4 HD FIR engine."""

APF_RESERVED_SLOTS: frozenset[int] = frozenset({0, 1})
"""PEQ slot indices reserved for APF all-pass filters (TODO-10).

Slots 2-9 are available for amplitude EQ (ALIGNMENT_PEQ_SLOTS).
"""

ALIGNMENT_PEQ_SLOTS: range = range(2, 10)
"""PEQ slots used by the alignment amplitude-EQ pass."""

VALID_SOURCES: frozenset[str] = frozenset({"Analog", "Toslink", "Usb", "Spdif", "Aes"})
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


# ── CLI helper ─────────────────────────────────────────────────────────────────

async def _run_minidsp_cli(*args: str, ignore_exit_codes: tuple[int, ...] = ()) -> None:
    """Run a minidsp CLI command, raising MinidspApiError on non-zero exit.

    The CLI connects to minidspd's WebSocket transport (not HTTP batch writes).
    Call this sequentially — concurrent CLI sessions corrupt device state.

    *ignore_exit_codes*: exit codes that are treated as success. Used for FIR
    commands (import, clear) which return exit code 1 due to an unrecognised
    post-load device response (cmd_id: 01) even though the write succeeds.
    See mrene/minidsp-rs#766.
    """
    proc = await asyncio.create_subprocess_exec(
        "minidsp", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0 and proc.returncode not in ignore_exit_codes:
        raise MinidspApiError(
            proc.returncode or 1,
            f"minidsp {' '.join(args)}: {stderr.decode().strip()}",
        )


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
        """Switch the active preset slot to *preset* (0-3) via CLI.

        Uses CLI (not HTTP) — HTTP master-status writes reset routing and PEQ
        state via the WebSocket transport. CLI is the safe write path.

        Raises ValueError if preset is out of range.
        Raises MinidspApiError on CLI error.
        """
        if not (0 <= preset <= MAX_PRESET_INDEX):
            raise ValueError(
                f"preset={preset} out of range; must be 0-{MAX_PRESET_INDEX}"
            )
        await _run_minidsp_cli("preset", str(preset))

    async def switch_source(self, source: str) -> None:
        """Switch the input source to *source* (Analog/Toslink/Usb) via CLI.

        Uses CLI (not HTTP) — HTTP writes reset routing and PEQ biquads.
        Source switch also resets routing and mutes; callers must reconfigure
        routing and restore mute/gain/PEQ state after calling this.

        Raises ValueError if source is not in VALID_SOURCES.
        Raises MinidspApiError on CLI error.
        """
        if source not in VALID_SOURCES:
            raise ValueError(
                f"source={source!r} invalid; must be one of {sorted(VALID_SOURCES)}"
            )
        await _run_minidsp_cli("source", source.lower())

    MUTE_GAIN_DB: float = -127.0

    @staticmethod
    def _validate_output(output: int) -> None:
        """Raise ValueError if output index is out of range."""
        if not (0 <= output <= MAX_OUTPUT_INDEX):
            raise ValueError(
                f"output={output} out of range; must be 0-{MAX_OUTPUT_INDEX}"
            )

    async def set_output_gain(self, output: int, gain_db: float) -> None:
        """Set output *output* gain to *gain_db* dB via CLI.

        Uses CLI (not HTTP) to avoid state resets from the HTTP transport.
        Typical use: mute with MUTE_GAIN_DB (-127) or restore to 0.0.
        """
        self._validate_output(output)
        await _run_minidsp_cli("output", str(output), "gain", "--", str(gain_db))

    async def mute_outputs(self, output_indices: list[int]) -> None:
        """Mute outputs sequentially via CLI (output mute on).

        Sequential (not parallel) to avoid concurrent CLI invocations that
        can corrupt device state via the WebSocket transport.
        """
        for idx in output_indices:
            await _run_minidsp_cli("output", str(idx), "mute", "on")

    async def unmute_outputs(self, output_indices: list[int]) -> None:
        """Unmute outputs sequentially via CLI (output mute off)."""
        for idx in output_indices:
            await _run_minidsp_cli("output", str(idx), "mute", "off")

    async def set_output_delay(self, output: int, delay_ms: float) -> None:
        """Set output *output* delay to *delay_ms* milliseconds via CLI.

        Raises ValueError if delay_ms is negative or > MAX_DELAY_MS.
        """
        self._validate_output(output)
        if delay_ms < 0.0:
            raise ValueError(f"delay_ms={delay_ms} cannot be negative")
        if delay_ms > MAX_DELAY_MS:
            raise ValueError(
                f"delay_ms={delay_ms} exceeds hardware maximum {MAX_DELAY_MS} ms"
            )
        await _run_minidsp_cli("output", str(output), "delay", str(delay_ms))

    async def set_output_polarity(self, output: int, inverted: bool) -> None:
        """Set output *output* phase inversion via CLI.

        Raises ValueError if output index is invalid.
        Raises MinidspApiError on hardware or daemon error.
        """
        self._validate_output(output)
        await _run_minidsp_cli("output", str(output), "invert", "on" if inverted else "off")


    async def set_output_peq_cli(
        self,
        output: int,
        entries: list[dict[str, Any]],
    ) -> None:
        """Write PEQ entries to *output* via the minidsp CLI (not HTTP).

        The miniDSP 2x4 HD firmware uses a POSITIVE-sign recurrence:
            y[n] = b0*x[n] + b1*x[n-1] + b2*x[n-2] + a1_hw*y[n-1] + a2_hw*y[n-2]
        scipy/standard convention uses NEGATIVE signs for the feedback coefficients.
        We must negate a1 and a2 before sending to hardware, otherwise the effective
        poles are at |z|≈2.4 (unstable), causing the DSP to immediately overflow and
        freeze at 0.0 dBFS — a hang requiring physical power-cycle to recover.

        Each entry: {"index": slot, "coeff": {b0,b1,b2,a1,a2}, "bypass": bool}
        Active slots (bypass=False): write coefficients then un-bypass.
        Bypassed slots (bypass=True): set bypass on (no coefficient write needed).
        """
        self._validate_output(output)

        for entry in entries:
            slot = str(entry["index"])
            if entry.get("bypass", False):
                await _run_minidsp_cli("output", str(output), "peq", slot, "bypass", "on")
            else:
                c = entry["coeff"]
                await _run_minidsp_cli(
                    "output", str(output), "peq", slot, "set", "--",
                    str(c["b0"]), str(c["b1"]), str(c["b2"]),
                    str(-c["a1"]), str(-c["a2"]),  # negate: scipy→miniDSP sign convention
                )
                await _run_minidsp_cli("output", str(output), "peq", slot, "bypass", "off")

    async def check_for_dsp_hang(self, suspect_outputs: list[int]) -> None:
        """Poll output levels and raise MinidspApiError if any output is frozen.

        A DSP hang (output level frozen at 0.0 dBFS) indicates the filter pipeline
        overflowed — typically from a sign convention bug (sending scipy's negative
        a1/a2 instead of the negated values the hardware expects).  Normal idle
        levels are -94 to -120 dBFS.  Recovery requires a physical power-cycle;
        bypassing slots or switching presets will not clear it.
        """
        try:
            status = await self.get_device_status()
            output_levels = status.get("output_levels", [])
        except Exception:
            return  # Best-effort: don't fail if status read is unavailable

        for out in suspect_outputs:
            if out < len(output_levels) and output_levels[out] == 0.0:
                raise MinidspApiError(
                    -1,
                    f"DSP hang detected after PEQ write — output {out} frozen at 0.0 dBFS. "
                    f"Physically power-cycle the miniDSP to recover. "
                    f"(All output levels: {output_levels})"
                )

    async def set_input_peq_cli(
        self,
        input_index: int,
        entries: list[dict[str, Any]],
    ) -> None:
        """Write PEQ entries to input *input_index* via the minidsp CLI (not HTTP).

        Same sign convention fix as set_output_peq_cli — negate a1/a2 before
        sending to hardware (scipy negative → miniDSP positive convention).
        """
        for entry in entries:
            slot = str(entry["index"])
            if entry.get("bypass", False):
                await _run_minidsp_cli("input", str(input_index), "peq", slot, "bypass", "on")
            else:
                c = entry["coeff"]
                await _run_minidsp_cli(
                    "input", str(input_index), "peq", slot, "set", "--",
                    str(c["b0"]), str(c["b1"]), str(c["b2"]),
                    str(-c["a1"]), str(-c["a2"]),  # negate: scipy→miniDSP sign convention
                )
                await _run_minidsp_cli("input", str(input_index), "peq", slot, "bypass", "off")

    async def set_output_fir_from_file(self, output: int, path: str) -> None:
        """Load FIR coefficients from a WAV file and activate them via CLI.

        The WAV file must be mono float32 at FIR_SAMPLE_RATE (96000 Hz).
        After loading, bypass is set to off (filter active).
        """
        self._validate_output(output)
        # fir import returns exit code 1 on success (minidsp-rs#766) — ignore it
        await _run_minidsp_cli("output", str(output), "fir", "import", path, ignore_exit_codes=(1,))
        await _run_minidsp_cli("output", str(output), "fir", "bypass", "off")

    async def set_output_fir_bypass(self, output: int, bypassed: bool) -> None:
        """Enable or disable the FIR bypass for *output* via CLI."""
        self._validate_output(output)
        await _run_minidsp_cli(
            "output", str(output), "fir", "bypass", "on" if bypassed else "off"
        )

    async def clear_output_fir(self, output: int) -> None:
        """Clear FIR coefficients and reset to passthrough (bypass off) via CLI.

        Explicitly sets bypass=off after clearing to ensure a deterministic
        passthrough state regardless of firmware behaviour post-clear.
        """
        self._validate_output(output)
        # fir clear returns exit code 1 on success (minidsp-rs#766) — ignore it
        await _run_minidsp_cli("output", str(output), "fir", "clear", ignore_exit_codes=(1,))
        await _run_minidsp_cli("output", str(output), "fir", "bypass", "off")

    async def set_input_routing(
        self,
        input_index: int,
        output_enabled: dict[int, bool],
    ) -> None:
        """Set the routing matrix for *input_index* via CLI.

        Uses CLI (not HTTP) so routing survives subsequent CLI PEQ writes.
        The HTTP routing API is reset by the CLI WebSocket transport on connect.

        *output_enabled* maps each output index to whether it should receive
        signal from this input.  Example to route input 1 to outputs 0, 2, 3:

            await client.set_input_routing(1, {0: True, 1: False, 2: True, 3: True})
        """
        for out_idx, enabled in output_enabled.items():
            await _run_minidsp_cli(
                "input", str(input_index), "routing", str(out_idx),
                "enable", "true" if enabled else "false",
            )

    async def restore_all_gains(self, output_indices: list[int]) -> None:
        """Unmute outputs sequentially via CLI.

        Called in finally blocks and TTL cleanup to ensure no sub is left muted
        after an alignment session ends (normally or due to browser disconnect).
        """
        for idx in output_indices:
            try:
                await _run_minidsp_cli("output", str(idx), "mute", "off")
            except Exception:
                import logging
                logging.getLogger(__name__).warning(
                    "restore_all_gains: failed to unmute output %d", idx
                )
