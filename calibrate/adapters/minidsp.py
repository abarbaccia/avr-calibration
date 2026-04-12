"""Adapter for minidspd — all I/O via the minidsp CLI (WebSocket transport).

ALL communication goes through the minidsp CLI because:
  - HTTP config writes reset routing and PEQ state when the next CLI session opens.
  - The HTTP config API has a sign bug in a1/a2 biquad coefficients that causes
    DSP hangs requiring physical power-cycle to recover.
  - CLI status reads (minidsp -o json status) give the same data as GET /devices/{idx}.

CLI (writes):
  minidsp source <name>                 → switch source (Analog/Toslink/Usb)
  minidsp preset <N>                    → switch preset (0-3)
  minidsp output <N> gain -- <dB>       → set output gain
  minidsp output <N> mute on|off        → set output mute
  minidsp output <N> delay <ms>         → set output delay
  minidsp output <N> invert on|off      → set output polarity
  minidsp output <N> peq <slot> set ... → write biquad coefficients
  minidsp output <N> fir import <path>  → write FIR coefficients
  minidsp input  <N> routing <out> ...  → configure routing matrix

CLI (reads):
  minidsp -o json status                → master status (preset, source, volume, mute,
                                          input_levels, output_levels)

Safety:
  - delay_ms > MAX_DELAY_MS  → ValueError (hardware limit is 30 ms)
  - slot in APF_RESERVED_SLOTS → ValueError (slots 0-1 reserved for APF)
"""

from __future__ import annotations

import asyncio
import json as _json
from typing import Any

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

PEQ_WRITE_DELAY_S: float = 0.3
"""Delay in seconds between sequential PEQ CLI writes.

The miniDSP 2x4 HD firmware can silently drop PEQ writes when commands arrive
faster than the internal commit cycle.  The ezbeq project (which also uses
minidsp-rs) has an identical workaround (``slotChangeDelay``).  Without this
delay, only the last filter in a batch reliably takes effect.
"""

CLI_COMMAND_DELAY_S: float = 0.1
"""Delay in seconds after every CLI command (non-PEQ).

The minidspd WebSocket transport serialises commands internally, but the
miniDSP 2x4 HD firmware needs time to commit each write to the SHARC DSP.
Without this gap, rapid-fire commands (e.g. 13 parallel set_delay/polarity/
gain calls during DSP reset) can be silently dropped by the firmware.
PEQ writes use the longer PEQ_WRITE_DELAY_S instead.
"""

VALID_SOURCES: frozenset[str] = frozenset({"Analog", "Toslink", "Usb", "Spdif", "Aes"})
"""Valid input source names for the miniDSP 2x4 HD."""

MAX_PRESET_INDEX: int = 3
"""Highest valid preset slot index (miniDSP 2x4 HD has 4 presets: 0-3)."""


# ── Exceptions ─────────────────────────────────────────────────────────────────

class MinidspApiError(RuntimeError):
    """Raised when a minidsp CLI command fails.

    Attributes:
        status_code  -- CLI exit code (or -1 for DSP hang detection)
        path         -- the command and stderr output
    """

    def __init__(self, status_code: int, path: str) -> None:
        self.status_code = status_code
        self.path = path
        super().__init__(f"minidsp error {status_code}: {path}")


# ── CLI helper ─────────────────────────────────────────────────────────────────

_cli_lock = asyncio.Lock()
"""Serialises ALL minidsp CLI access.

The minidspd daemon uses a single WebSocket connection to the hardware.
Concurrent CLI invocations can corrupt device state or be silently dropped
by the miniDSP 2x4 HD firmware.  This lock ensures only one CLI command
runs at a time, with a post-command delay for firmware commit.
"""


async def _run_minidsp_cli(
    *args: str,
    ignore_exit_codes: tuple[int, ...] = (),
    post_delay: float | None = None,
) -> None:
    """Run a minidsp CLI command, raising MinidspApiError on non-zero exit.

    The CLI connects to minidspd's WebSocket transport (not HTTP batch writes).
    All calls are serialised via _cli_lock — concurrent CLI sessions corrupt
    device state.

    *ignore_exit_codes*: exit codes that are treated as success. Used for FIR
    commands (import, clear) which return exit code 1 due to an unrecognised
    post-load device response (cmd_id: 01) even though the write succeeds.
    See mrene/minidsp-rs#766.

    *post_delay*: seconds to sleep after the command completes.  Defaults to
    CLI_COMMAND_DELAY_S (0.1s).  PEQ callers pass their own longer delay via
    the existing asyncio.sleep(PEQ_WRITE_DELAY_S) after this function returns.
    """
    delay = post_delay if post_delay is not None else CLI_COMMAND_DELAY_S
    async with _cli_lock:
        proc = await asyncio.create_subprocess_exec(
            "minidsp", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()  # reap zombie
            raise MinidspApiError(1, f"minidsp {' '.join(args)}: timed out after 10s")
        if proc.returncode != 0 and proc.returncode not in ignore_exit_codes:
            raise MinidspApiError(
                proc.returncode or 1,
                f"minidsp {' '.join(args)}: {stderr.decode().strip()}",
            )
        if delay > 0:
            await asyncio.sleep(delay)


async def _get_status_via_cli() -> dict:
    """Read device status via CLI. Returns same structure as the former HTTP GET /devices/{idx}.

    Output shape:
      {
        "master": {"preset": 0, "source": "Analog", "volume": -30.0, "mute": false},
        "input_levels": [...],
        "output_levels": [...]
      }

    Raises MinidspApiError on CLI failure (daemon not running, device not connected, etc.).
    """
    proc = await asyncio.create_subprocess_exec(
        "minidsp", "-o", "json", "status",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()  # reap zombie
        raise MinidspApiError(1, "minidsp status: timed out after 5s")
    if proc.returncode != 0:
        raise MinidspApiError(
            proc.returncode or 1,
            f"minidsp status: {stderr.decode().strip()}",
        )
    if not stdout:
        raise MinidspApiError(1, "minidsp status: empty output")
    try:
        return _json.loads(stdout)
    except Exception as exc:
        raise MinidspApiError(1, f"minidsp status: invalid JSON: {exc}")


# ── Client ─────────────────────────────────────────────────────────────────────

class MinidspClient:
    """CLI-only client for the miniDSP 2x4 HD via the minidsp CLI (WebSocket transport).

    All I/O goes through the minidsp CLI. No HTTP. The HTTP API is not used
    because HTTP config writes reset routing/PEQ state and have a sign bug in
    the a1/a2 biquad coefficients that causes DSP hangs.

    Usage:

        client = MinidspClient()
        await client.set_output_gain(0, -6.0)
        await client.set_output_delay(0, 4.5)
        await client.set_output_polarity(0, inverted=True)
        await client.set_input_routing(1, {0: True, 1: False, 2: True, 3: True})
        await client.switch_preset(1)
        await client.switch_source("Toslink")
        await client.restore_all_gains([0, 1])
        status = await client.get_device_status()
    """

    def __init__(self, host: str = "localhost", port: int = 5380, device_index: int = 0) -> None:
        # Signature kept for backward compatibility; CLI path ignores these — the
        # minidsp binary connects to its default daemon address.
        pass

    # ── Public API ─────────────────────────────────────────────────────────────

    async def get_devices(self) -> list[dict]:
        """Return the list of connected miniDSP devices via CLI.

        Returns a single-element list if the device responds to a status query.
        Raises MinidspApiError if the CLI fails (daemon not running, no device, etc.).
        """
        await _get_status_via_cli()
        return [{"product_name": "miniDSP 2x4 HD", "version": {"serial": ""}}]

    async def get_device_status(self) -> dict:
        """Return the current master status for the device via CLI.

        Response shape:
          {
            "master": {"preset": 0, "source": "Analog", "volume": -30.0, "mute": false},
            "input_levels": [...],
            "output_levels": [...]
          }

        Raises MinidspApiError on CLI failure.
        """
        return await _get_status_via_cli()

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

    async def set_master_gain(self, gain_db: float) -> None:
        """Set the miniDSP master output gain (-127 to 0 dB) via CLI.

        This is a global attenuation applied before all outputs — useful for
        controlling sweep volume without touching per-output alignment gains.
        """
        clamped = max(-127.0, min(0.0, gain_db))
        await _run_minidsp_cli("gain", "--", str(clamped))

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
                await asyncio.sleep(PEQ_WRITE_DELAY_S)
                await _run_minidsp_cli("output", str(output), "peq", slot, "bypass", "off")
            await asyncio.sleep(PEQ_WRITE_DELAY_S)

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
                await asyncio.sleep(PEQ_WRITE_DELAY_S)
                await _run_minidsp_cli("input", str(input_index), "peq", slot, "bypass", "off")
            await asyncio.sleep(PEQ_WRITE_DELAY_S)

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
