"""AVRDriver — abstract base class for AV receiver hardware drivers."""

from __future__ import annotations

from abc import ABC, abstractmethod


class AVRDriver(ABC):
    """Protocol for AV receiver control.

    Implementations: DenonDriver (wraps denonavr library).
    Future: YamahaDriver, MarantzDriver, etc.

    Lifecycle:
        driver = load_avr_driver(config)
        await driver.setup()          # in Starlette lifespan
        ...
        await driver.close()          # in Starlette lifespan teardown
    """

    async def setup(self) -> None:
        """Async initialisation — called once in server lifespan.

        Default: no-op.  Override when the driver needs to establish a
        persistent connection or perform an async handshake.
        """

    async def close(self) -> None:
        """Teardown — called once on server shutdown.

        Default: no-op.  Override when the driver holds open sockets or
        sessions that need explicit cleanup.
        """

    @abstractmethod
    async def get_state(self) -> dict:
        """Return current AVR hardware state.

        Returns a dict with at least:
            connected (bool), host (str)
        On success also includes: volume, input, mute.

        Raises DriverError on hardware communication failure.
        """

    @abstractmethod
    async def set_volume(self, level_db: float) -> float:
        """Set AVR volume to *level_db* dB.

        Returns the confirmed volume level as reported by the hardware
        after the change is applied.

        Raises DriverError on hardware communication failure or if no
        host is configured.
        """

    async def discover(self) -> list[str]:
        """Return a list of discovered AVR hostnames/IPs on the local network.

        Default: returns [] (no auto-discovery).  Override when the underlying
        protocol supports SSDP or mDNS discovery.
        """
        return []
