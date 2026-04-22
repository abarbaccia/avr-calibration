"""DSPDriver — abstract base class for DSP hardware drivers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Config


@dataclass(frozen=True)
class DSPCapabilities:
    """Hardware-specific DSP limits and feature flags.

    Exposed via ``DSPDriver.capabilities`` so callers (CLI validation, alignment
    math, recipes) can reason about limits without importing hardware-specific
    constants. Values are hardware-determined, not user-configurable.

    Fields:
        max_delay_ms: Per-output delay ceiling in ms (miniDSP 2x4 HD: 30.0;
            CamillaDSP: bounded by chunk size, effectively unlimited).
        max_preset_index: Highest valid preset slot index. Use -1 when the DSP
            has no preset slots (CamillaDSP — single active pipeline).
        valid_sources: Source names accepted by ``set_source``. Empty frozenset
            when the DSP has no source switching (CamillaDSP).
        processing_rate: Internal DSP sample rate in Hz. Biquad coefficients
            must be computed at this rate.
        max_peq_slots: Number of PEQ slots available per output for user EQ.
        fir_capable: True when the DSP supports FIR filters via apply_fir.
        fir_min_taps: Smallest tap count design_fir will accept.
        fir_max_taps_per_output: Ceiling on FIR taps for a single output.
        fir_shared_tap_pool: Total taps shared across all outputs, or None when
            there is no shared ceiling (CamillaDSP).
        fir_sample_rate_hz: Sample rate at which FIR coefficients are evaluated.
    """
    max_delay_ms: float
    max_preset_index: int
    valid_sources: frozenset[str]
    processing_rate: int
    max_peq_slots: int
    fir_capable: bool
    fir_min_taps: int
    fir_max_taps_per_output: int
    fir_shared_tap_pool: int | None
    fir_sample_rate_hz: int


class DSPDriver(ABC):
    """Protocol for DSP/crossover hardware control.

    Implementations: MinidspDriver (wraps MinidspClient → minidspd HTTP API).
    Future: CamillaDSPDriver, etc.

    Lifecycle:
        driver = load_dsp_driver(config)
        await driver.setup()          # in Starlette lifespan
        ...
        await driver.close()          # in Starlette lifespan teardown
    """

    @property
    @abstractmethod
    def capabilities(self) -> DSPCapabilities:
        """Hardware-specific DSP limits and feature flags.

        See ``DSPCapabilities`` for the fields. Callers should query this
        instead of importing constants from a specific driver module.
        """

    async def setup(self) -> None:
        """Async initialisation — called once in server lifespan.

        Default: no-op.
        """

    async def close(self) -> None:
        """Teardown — called once on server shutdown.

        Default: no-op.
        """

    @abstractmethod
    async def get_state(self) -> dict:
        """Return current DSP hardware state.

        Returns a dict with at least:
            connected (bool), host (str)
        On success also includes: preset, source, volume, mute.

        Raises DriverError on hardware communication failure.
        """

    @abstractmethod
    async def current_preset(self) -> int:
        """Return the active preset slot index (0-based).

        Returns 0 on failure rather than raising, so callers can proceed
        with a safe default when hardware is transiently unreachable.
        """

    @abstractmethod
    async def apply_eq(
        self, preset: int, filters: list[dict],
        output_index: int | None = None,
    ) -> None:
        """Validate and apply EQ filters to DSP output(s).

        *filters* is a list of dicts with keys: freq, gain_db, q, type.
        If *output_index* is given, writes only to that output (per-sub EQ).
        Otherwise writes to all configured sub outputs.

        Runs SafetyValidator under an asyncio lock before any hardware write.
        Updates in-memory EQ state only if ALL hardware writes succeed.

        Raises DriverError on validation, safety, or hardware failure.
        """

    @abstractmethod
    async def apply_input_eq(
        self, preset: int, filters: list[dict],
        input_index: int | None = None,
    ) -> None:
        """Validate and apply EQ filters to the DSP input channel.

        Use for shared EQ (e.g. Harman target) that should affect all outputs.
        If *input_index* is given, targets that input; otherwise uses active_input.

        Raises DriverError on validation, safety, or hardware failure.
        """

    @abstractmethod
    async def set_preset(self, preset: int) -> None:
        """Switch the active DSP preset slot.

        Raises DriverError on invalid preset index or hardware failure.
        """

    @abstractmethod
    async def set_routing(self, routing: dict) -> None:
        """Apply an input→output routing matrix.

        *routing* maps input_index (int) → {output_index (int): enabled (bool)}.

        Raises DriverError on hardware failure.
        """

    @abstractmethod
    async def mute_outputs(self, output_indices: list[int]) -> None:
        """Mute the specified output channels.

        Raises DriverError on hardware failure.
        """

    @abstractmethod
    async def unmute_outputs(self, output_indices: list[int]) -> None:
        """Unmute the specified output channels.

        Raises DriverError on hardware failure.
        """

    @abstractmethod
    async def set_output_gain(self, output_index: int, gain_db: float) -> None:
        """Set gain for a single output in dB.

        Raises DriverError on hardware failure.
        """

    @abstractmethod
    async def set_output_delay(self, output_index: int, delay_ms: float) -> None:
        """Set delay for a single output in milliseconds.

        Raises DriverError on hardware failure.
        """

    @abstractmethod
    async def set_output_polarity(self, output_index: int, inverted: bool) -> None:
        """Set polarity for a single output (inverted=True flips phase 180°).

        Raises DriverError on hardware failure.
        """

    @abstractmethod
    async def set_master_gain(self, gain_db: float) -> None:
        """Set master output gain (global attenuation) in dB.

        Raises DriverError on hardware failure.
        """

    @abstractmethod
    async def get_output_state(self) -> dict[int, dict]:
        """Return per-output state dict.

        Returns {output_index: {gain_db, delay_ms, polarity_inverted, fir_taps}}.
        Only reflects state set via this driver instance since startup — hardware
        state from before server start is not readable from minidspd.
        """

    async def configure_active_input(self, active_input: int) -> None:
        """Route active_input to all outputs and mute the other input.

        Default: no-op. Override for DSPs with configurable input routing
        (e.g. miniDSP 2x4 HD where one analog input may be defective).
        """

    async def set_source(self, source: str) -> None:
        """Switch the active DSP input source (e.g. 'Analog', 'Usb', 'Toslink').

        Default: raises NotImplementedError. Override for DSPs with source switching.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support set_source")

    def sweep_context(
        self, config: "Config"
    ) -> AbstractAsyncContextManager | None:
        """Return an async context manager that prepares the DSP for a sweep session.

        Entered once at calibration start and exited once at end. Used by DSPs
        that need source switching or routing changes to let sweep audio reach
        the outputs (e.g. miniDSP 2x4 HD — swap Analog→USB input).

        Default: return None (no sweep-time setup needed — the driver is always
        ready to pass sweep audio through, as with CamillaDSP).
        """
        return None
