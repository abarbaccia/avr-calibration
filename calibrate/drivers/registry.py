"""Driver registry — factory functions that load AVR and DSP drivers from config.

Adding a new driver requires:
  1. Implement a subclass of AVRDriver or DSPDriver in a new module.
  2. Add one entry to _AVR_DRIVERS or _DSP_DRIVERS below.
  3. Set avr_driver/dsp_driver in config.yaml.

No other files need to change.
"""

from __future__ import annotations

from ..config import Config
from .avr_driver import AVRDriver
from .base import DriverError
from .denon import DenonDriver
from .dsp_driver import DSPDriver
from .minidsp import MinidspDriver

_AVR_DRIVERS: dict[str, type[AVRDriver]] = {
    "denon": DenonDriver,
}

_DSP_DRIVERS: dict[str, type[DSPDriver]] = {
    "minidsp": MinidspDriver,
}


def load_avr_driver(config: Config) -> AVRDriver:
    """Instantiate the configured AVRDriver.

    Reads config.avr_driver_name (default: "denon") and constructs the
    appropriate driver with connection parameters from config.

    Raises ValueError if the driver name is not in the registry.
    """
    name = config.avr_driver_name
    cls = _AVR_DRIVERS.get(name)
    if cls is None:
        raise ValueError(
            f"Unknown AVR driver: {name!r}. "
            f"Valid options: {sorted(_AVR_DRIVERS)}"
        )
    if cls is DenonDriver:
        host = config.denon.get("host")
        return DenonDriver(host=host)
    return cls()  # type: ignore[call-arg]


def load_dsp_driver(config: Config) -> DSPDriver:
    """Instantiate the configured DSPDriver.

    Reads config.dsp_driver_name (default: "minidsp") and constructs the
    appropriate driver with connection parameters from config.

    Raises ValueError if the driver name is not in the registry.
    """
    name = config.dsp_driver_name
    cls = _DSP_DRIVERS.get(name)
    if cls is None:
        raise ValueError(
            f"Unknown DSP driver: {name!r}. "
            f"Valid options: {sorted(_DSP_DRIVERS)}"
        )
    if cls is MinidspDriver:
        host = config.minidsp.get("host", "localhost")
        port = int(config.minidsp.get("port", 5380))
        return MinidspDriver(host=host, port=port)
    return cls()  # type: ignore[call-arg]
