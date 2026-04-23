"""Driver registry — factory functions that load AVR and DSP drivers from config.

Adding a new driver requires:
  1. Implement a subclass of AVRDriver or DSPDriver in a new module.
  2. Add one entry to _AVR_DRIVERS or _DSP_DRIVERS below.
  3. Set avr_driver/dsp_driver in config.yaml.

No other files need to change.

## DriverRegistry

For the signal-graph topology, the registry grows to hold **many** drivers
keyed by processor name — a scene with two miniDSPs + one Denon = three
entries in the registry. ``load_drivers_from_graph(config)`` walks the
graph's processor list and instantiates one driver per node. The legacy
``load_dsp_driver`` / ``load_avr_driver`` still work; they return the first
driver of the requested kind, matching today's single-X behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass, field

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


@dataclass
class DriverRegistry:
    """Named bag of driver instances keyed by processor name.

    Typical usage from `mcp_server.py` lifespan::

        registry = load_drivers_from_graph(config)
        for name, drv in registry.all():
            await drv.setup()

    Single-DSP installs see exactly one DSP and one AVR entry; the convenience
    accessors (``default_dsp()`` / ``default_avr()``) return them directly.
    """

    drivers: dict[str, AVRDriver | DSPDriver] = field(default_factory=dict)

    def __contains__(self, name: str) -> bool:
        return name in self.drivers

    def __getitem__(self, name: str) -> AVRDriver | DSPDriver:
        return self.drivers[name]

    def get(self, name: str) -> AVRDriver | DSPDriver | None:
        return self.drivers.get(name)

    def all(self) -> list[tuple[str, AVRDriver | DSPDriver]]:
        return list(self.drivers.items())

    def dsps(self) -> list[tuple[str, DSPDriver]]:
        return [(n, d) for n, d in self.drivers.items() if isinstance(d, DSPDriver)]

    def avrs(self) -> list[tuple[str, AVRDriver]]:
        return [(n, d) for n, d in self.drivers.items() if isinstance(d, AVRDriver)]

    def default_dsp(self) -> DSPDriver | None:
        """First DSP driver — used for legacy single-DSP dispatch."""
        dsps = self.dsps()
        return dsps[0][1] if dsps else None

    def default_avr(self) -> AVRDriver | None:
        dsps = self.avrs()
        return dsps[0][1] if dsps else None

    def default_dsp_name(self) -> str | None:
        dsps = self.dsps()
        return dsps[0][0] if dsps else None

    def default_avr_name(self) -> str | None:
        avrs = self.avrs()
        return avrs[0][0] if avrs else None


def load_drivers_from_graph(config: Config) -> DriverRegistry:
    """Walk the config's signal graph and build one driver per processor node.

    Legacy installs (no ``signal_graph:`` block) get one driver each for AVR
    and DSP — the graph's legacy shim synthesises two processor nodes whose
    names match the legacy ``avr_driver`` / ``dsp_driver`` config keys, so
    this function produces the same object graph as calling the legacy
    factories directly.
    """
    graph = config.signal_graph
    registry = DriverRegistry()
    for proc in graph.processors:
        registry.drivers[proc.name] = _instantiate_processor(config, proc)
    return registry


def _instantiate_processor(config: Config, proc) -> AVRDriver | DSPDriver:
    """Construct one driver for a graph processor node.

    Routes by ``proc.kind`` + ``proc.driver_ref``, reusing the same
    connection-parameter extraction as the legacy ``load_*_driver``
    functions. Multiple processors may share a ``driver_ref`` class (two
    miniDSPs on different ports); connection parameters come from
    ``proc.config_key`` (or fall back to ``proc.driver_ref``) so each
    processor reads its own block from config.yaml.
    """
    if proc.kind == "avr":
        cls = _AVR_DRIVERS.get(proc.driver_ref)
        if cls is None:
            raise ValueError(
                f"Unknown AVR driver {proc.driver_ref!r} (processor {proc.name!r})"
            )
        if cls is DenonDriver:
            host = config.denon.get("host")
            return DenonDriver(host=host)
        return cls()  # type: ignore[call-arg]

    if proc.kind == "dsp":
        cls = _DSP_DRIVERS.get(proc.driver_ref)
        if cls is None:
            raise ValueError(
                f"Unknown DSP driver {proc.driver_ref!r} (processor {proc.name!r})"
            )
        if cls is MinidspDriver:
            return _make_minidsp(config)
        if cls is CamillaDSPDriver:
            return _make_camilladsp(config, proc)
        return cls()  # type: ignore[call-arg]

    raise ValueError(f"Unknown processor kind {proc.kind!r} on {proc.name!r}")


def _make_minidsp(config: Config) -> MinidspDriver:
    host, port = config.minidsp_host_port
    active_input = config.active_input
    usb_input = config.measurement.get("output_channel", 1) - 1
    processing_rate = int(config.eq_capabilities.get("processing_rate", 96_000))
    return MinidspDriver(
        host=host, port=port,
        sub_outputs=config.sub_outputs,
        active_input=active_input,
        usb_input=usb_input,
        processing_rate=processing_rate,
    )


def _make_camilladsp(config: Config, proc) -> CamillaDSPDriver:
    # config_key lets one CamillaDSP processor read a named block other than
    # the default "camilladsp" — useful when two CamillaDSP instances run on
    # different sockets. Falls back to the standard "camilladsp" key.
    cam = getattr(config, proc.config_key or "camilladsp", None) or config.camilladsp
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


def load_avr_driver(config: Config) -> AVRDriver:
    """Legacy single-AVR entry point. Returns the first AVR from the registry.

    Existing callers (CLI `signal_path_apply`, preflight checks, older tests)
    call this directly. The registry-based lookup ensures they keep working
    regardless of how many drivers the graph defines.
    """
    registry = load_drivers_from_graph(config)
    drv = registry.default_avr()
    if drv is None:
        name = config.avr_driver_name
        if name not in _AVR_DRIVERS:
            raise ValueError(
                f"Unknown AVR driver: {name!r}. "
                f"Valid options: {sorted(_AVR_DRIVERS)}"
            )
        raise ValueError("No AVR processor found in signal graph")
    return drv


def load_dsp_driver(config: Config) -> DSPDriver:
    """Legacy single-DSP entry point. Returns the first DSP from the registry."""
    registry = load_drivers_from_graph(config)
    drv = registry.default_dsp()
    if drv is None:
        name = config.dsp_driver_name
        if name not in _DSP_DRIVERS:
            raise ValueError(
                f"Unknown DSP driver: {name!r}. "
                f"Valid options: {sorted(_DSP_DRIVERS)}"
            )
        raise ValueError("No DSP processor found in signal graph")
    return drv
