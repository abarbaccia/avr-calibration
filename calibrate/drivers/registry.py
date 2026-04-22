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
from .camilladsp import CamillaDSPDriver
from .denon import DenonDriver
from .dsp_driver import DSPDriver
from .minidsp import MinidspDriver

_AVR_DRIVERS: dict[str, type[AVRDriver]] = {
    "denon": DenonDriver,
}

_DSP_DRIVERS: dict[str, type[DSPDriver]] = {
    "minidsp": MinidspDriver,
    "camilladsp": CamillaDSPDriver,
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
        host, port = config.minidsp_host_port
        active_input = config.minidsp.get("active_input") or 0
        usb_input = config.measurement.get("output_channel", 1) - 1
        processing_rate = int(config.eq_capabilities.get("processing_rate", 96_000))
        return MinidspDriver(
            host=host, port=port,
            sub_outputs=config.sub_outputs,
            active_input=active_input,
            usb_input=usb_input,
            processing_rate=processing_rate,
        )
    if cls is CamillaDSPDriver:
        cam = config.camilladsp
        kwargs: dict = {
            "host": cam.get("host", "127.0.0.1"),
            "port": int(cam.get("port", 1234)),
            "sub_outputs": config.sub_outputs,
            "output_channels": int(cam.get("output_channels", 10)),
            "input_channels": int(cam.get("input_channels", 2)),
            "processing_rate": int(cam.get("samplerate", 48_000)),
            "chunksize": int(cam.get("chunksize", 1024)),
        }
        if cam.get("capture") is not None:
            kwargs["capture_device"] = cam["capture"]
        if cam.get("playback") is not None:
            kwargs["playback_device"] = cam["playback"]
        if cam.get("max_peq_slots") is not None:
            kwargs["max_peq_slots"] = int(cam["max_peq_slots"])
        return CamillaDSPDriver(**kwargs)
    return cls()  # type: ignore[call-arg]
