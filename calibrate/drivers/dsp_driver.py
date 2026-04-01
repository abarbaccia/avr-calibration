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
    async def apply_eq(self, preset: int, filters: list[dict]) -> None:
        """Validate and apply EQ filters to DSP hardware.

        *filters* is a list of dicts with keys: freq, gain_db, q, type.

        Runs SafetyValidator under an asyncio lock before any hardware write.
        Updates in-memory EQ state only if ALL hardware writes succeed
        (partial-write rollback).

        Raises DriverError on:
          - Invalid filter spec (KeyError/ValueError/TypeError in parsing)
          - SafetyValidator rejection ("SafetyValidator: ...")
          - Too many filters for available hardware slots ("too many filters: ...")
          - Hardware write failure ("minidsp write failed: ...")
          - Unexpected error ("apply_eq error: ...")
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
