"""DSPDriver — abstract base class for DSP hardware drivers."""

from __future__ import annotations

from abc import ABC, abstractmethod


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
    async def read_eq(self, preset: int) -> list[dict]:
        """Return the in-memory EQ filter state for *preset*.

        Returns a list of filter spec dicts ({freq, gain_db, q, type}).
        Returns [] if no filters have been applied since startup.

        Note: minidspd has no GET endpoint for PEQ state — this returns
        the state tracked in memory by the driver since server start.
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
