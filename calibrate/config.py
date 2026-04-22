"""Configuration loading for avr-calibration."""

import os
from pathlib import Path

import yaml

CONFIG_PATH = Path.home() / ".avr-calibration" / "config.yaml"

DEFAULT_CONFIG: dict = {
    "avr_driver": "denon",
    "dsp_driver": "minidsp",
    "denon": {
        "host": None,
    },
    "minidsp": {
        "host": "localhost",
        "port": 5380,
        "active_input": None,
        "signal_path": None,
        "input_labels": {},
        "output_slots": [
            {"index": 0, "label": "", "type": "sub"},
            {"index": 1, "label": "", "type": "sub"},
            {"index": 2, "label": "", "type": "unused"},
            {"index": 3, "label": "", "type": "unused"},
        ],
    },
    "camilladsp": {
        "host": "127.0.0.1",
        "port": 1234,
        "samplerate": 48000,
        "chunksize": 1024,
        "input_channels": 2,
        "output_channels": 10,
        "capture": None,
        "playback": None,
        "max_peq_slots": 16,
    },
    "signal_graph": None,
    "mic": {
        "name": "UMIK",
    },
    "eq_capabilities": {
        "processing_rate": 96000,
    },
    "sub": {
        "port_tune_hz": None,
    },
    "measurement": {
        "freq_min": 20,
        "freq_max": 200,
        "sweep_duration": 3.0,
        "sample_rate": 48000,
        "input_channel": 1,
        "output_channel": 1,
        "playback_route": "usb",
        "denon_sweep_input": None,
        "denon_sweep_volume": -10.0,
        "denon_settle_ms": 5000,
        "denon_pure_direct": True,
        "sweep_channel": "lfe",
        "playback_device": "miniDSP",
        "hdmi_playback_device": None,
        "mic_device_index": None,
        "hdmi_device_index": None,
        "usb_device_index": None,
        "master_gain_hdmi_db": None,
        "sub_outputs": [0, 1],
        "ir_search_window_ms": 50.0,
    },
    "hdmi_channel_map": {
        "left": 1,
        "right": 2,
        "lfe": 3,
        "center": 4,
        "surround_left": 5,
        "surround_right": 6,
    },
    "headroom": {
        "start_volume_db": -30.0,
        "max_volume_db": -10.0,
        "step_db": 1.0,
        "hold_duration_s": 2.0,
        "tones_per_speaker": 4,
        "min_tone_spacing_hz": 30.0,
        "min_tone_frequency_hz": 200.0,
        "amplitude": 0.5,
    },
    "speakers": [],
    "connections": [],
}

CONFIG_TEMPLATE = """\
# AVR Calibration Configuration
# Run 'calibrate check' after editing to verify everything is reachable.

# dsp_driver: minidsp        # "minidsp" (default) or "camilladsp"

denon:
  # host: "192.168.1.100"  # Optional: set a fixed IP to skip SSDP auto-discovery.
  #                         # Leave commented out — the equipment check will find
  #                         # your Denon automatically via SSDP (recommended).

minidsp:
  host: "localhost"      # minidspd runs inside the container (--device=/dev/hidraw0)
  port: 5380             # default minidspd port
  signal_path:           # optional: declare your signal path to apply it on startup
    source: "Analog"     # Analog | Toslink | USB
    preset: 0            # preset slot 0-3
    routing:             # input → output mapping
      - input: 0
        outputs: [0, 1, 2, 3]   # output indices this input routes to (unmuted)
      - input: 1
        outputs: [0, 1, 2, 3]

# CamillaDSP (used when dsp_driver: camilladsp). The driver owns the whole
# pipeline — on every EQ/gain/delay change it regenerates the config and
# reloads the daemon. Hand-authored CamillaDSP pipelines on this daemon will
# be replaced the first time the MCP server connects.
# camilladsp:
#   host: "127.0.0.1"
#   port: 1234
#   samplerate: 48000
#   chunksize: 1024
#   input_channels: 2
#   output_channels: 10
#   capture:
#     type: Alsa
#     device: "hw:Loopback,1,0"
#     channels: 2
#     format: S32LE
#   playback:
#     type: Alsa
#     device: "hw:USB,0,0"
#     channels: 10
#     format: S32LE

sub:
  port_tune_hz: 22       # Hz — ported sub tuning frequency (shown on FR chart)

mic:
  name: "UMIK"           # substring matched against audio device names

measurement:
  freq_min: 20           # Hz — lower bound of calibration band
  freq_max: 200          # Hz — upper bound (bass calibration only)
  sweep_duration: 3.0    # seconds
  sample_rate: 48000     # Hz
  input_channel: 1       # audio device channel for microphone
  output_channel: 1      # audio device channel for subwoofer output
  playback_route: "usb"  # "usb" = direct to miniDSP, "hdmi" = via Denon full chain
  denon_sweep_input: null       # Denon input to switch to during HDMI sweep
                                # Run: python -c "import asyncio, denonavr; r=denonavr.DenonAVR('YOUR_IP'); asyncio.run(r.async_setup()); asyncio.run(r.async_update()); print(r.input_func_list)"
  denon_sweep_volume: -10.0    # dB — default starting volume (max 0 dB / reference)
  denon_settle_ms: 5000        # ms to wait after Denon input/volume change (HDMI needs 3-5s for HDCP)
  denon_pure_direct: true      # true = force Pure Direct during sweep; false = keep current sound mode
  sweep_channel: "lfe"         # "lfe" = LFE/subwoofer channel, "left"/"right" = main
  playback_device: "miniDSP"   # substring matched against USB audio device names
  hdmi_playback_device: null   # HDMI audio device name; null = system default
  mic_device_index: null       # ALSA device index for UMIK mic; null = find by name
  hdmi_device_index: null      # ALSA device index for HDMI output; null = find by name
  usb_device_index: null       # ALSA device index for USB/miniDSP output; null = find by name
  master_gain_hdmi_db: null    # miniDSP master gain for HDMI route; null = don't change
  sub_outputs: [0, 1]          # miniDSP output indices for each sub (0-indexed)
  ir_search_window_ms: 50.0    # IR peak search window; 50 ms = 17 m at 343 m/s

# HDMI channel map — CEA-861 standard 5.1 layout (1-based channel indices)
# Override if your sink uses a different mapping.
hdmi_channel_map:
  left: 1
  right: 2
  lfe: 3
  center: 4
  surround_left: 5
  surround_right: 6
"""


class Config:
    def __init__(self, data: dict) -> None:
        self._data = data

    @property
    def avr_driver_name(self) -> str:
        return str(self._data.get("avr_driver", "denon"))

    @property
    def dsp_driver_name(self) -> str:
        return str(self._data.get("dsp_driver", "minidsp"))

    @property
    def denon(self) -> dict:
        return self._data.get("denon", {})

    @property
    def minidsp(self) -> dict:
        return self._data.get("minidsp", {})

    @property
    def camilladsp(self) -> dict:
        return self._data.get("camilladsp", {})

    @property
    def signal_graph(self):
        """Return the parsed SignalGraph, synthesising from legacy config if absent.

        Every install has a graph — the absence of a ``signal_graph:`` block
        in YAML just means "synthesise one from the legacy single-DSP fields."
        That keeps MCP tools, safety, storage, and the sweep composer uniform;
        they never have to branch on "is there a graph or not."
        """
        from .graph import SignalGraph
        block = self._data.get("signal_graph")
        if block:
            return SignalGraph.from_dict(block)
        return SignalGraph.from_legacy(self)

    @property
    def mic(self) -> dict:
        return self._data.get("mic", {})

    @property
    def sub(self) -> dict:
        return self._data.get("sub", {})

    @property
    def measurement(self) -> dict:
        return self._data.get("measurement", {})

    @property
    def connections(self) -> dict:
        return self._data.get("connections", {})

    @property
    def headroom(self) -> dict:
        return self._data.get("headroom", {})

    @property
    def eq_capabilities(self) -> dict:
        return self._data.get("eq_capabilities", {})

    @property
    def minidsp_host_port(self) -> tuple[str, int]:
        """Return (host, port) for the miniDSP daemon connection."""
        return (
            self.minidsp.get("host", "localhost"),
            int(self.minidsp.get("port", 5380)),
        )

    @property
    def sub_outputs(self) -> list[int]:
        """Output indices where type='sub'. Falls back to measurement.sub_outputs."""
        slots = self.minidsp.get("output_slots", [])
        typed = [s["index"] for s in slots if s.get("type") == "sub"]
        if typed:
            return typed
        return self.measurement.get("sub_outputs", [0, 1])

    @property
    def speakers(self) -> list[dict]:
        return self._data.get("speakers", [])

    @property
    def hdmi_channel_map(self) -> dict[str, int]:
        """HDMI channel map: speaker role → 1-based channel index."""
        return self._data.get("hdmi_channel_map", {
            "left": 1, "right": 2, "lfe": 3,
            "center": 4, "surround_left": 5, "surround_right": 6,
        })

    def hdmi_channel_for(self, role: str) -> int | None:
        """Resolve a speaker role name to a 1-based HDMI channel index.

        Accepts exact keys ("lfe"), common aliases ("sub", "subwoofer"),
        and case-insensitive matching. Returns None if not found.
        """
        aliases = {
            "sub": "lfe", "subwoofer": "lfe", "sw": "lfe",
            "fl": "left", "front_left": "left",
            "fr": "right", "front_right": "right",
            "fc": "center", "front_center": "center", "c": "center",
            "sl": "surround_left", "rl": "surround_left", "rear_left": "surround_left",
            "sr": "surround_right", "rr": "surround_right", "rear_right": "surround_right",
        }
        key = role.lower().strip()
        key = aliases.get(key, key)
        return self.hdmi_channel_map.get(key)

    @property
    def shaker_outputs(self) -> list[int]:
        """Output indices where type='shaker'."""
        slots = self.minidsp.get("output_slots", [])
        return [s["index"] for s in slots if s.get("type") == "shaker"]

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "Config":
        if not path.exists():
            return cls(DEFAULT_CONFIG.copy())
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        # Deep merge with defaults so missing keys fall back gracefully
        merged: dict = {}
        for key, default_val in DEFAULT_CONFIG.items():
            user_val = data.get(key)
            if isinstance(default_val, dict) and isinstance(user_val, dict):
                merged[key] = {**default_val, **user_val}
            elif user_val is not None:
                merged[key] = user_val
            else:
                merged[key] = default_val
        return cls(merged)

    @classmethod
    def create_template(cls, path: Path = CONFIG_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            f.write(CONFIG_TEMPLATE)


def update_config(updates: dict, path: Path = CONFIG_PATH) -> None:
    """Deep-merge `updates` into the on-disk config.yaml.

    Existing keys not mentioned in `updates` are preserved verbatim.
    Nested dicts are shallow-merged one level deep (sub-keys are replaced, not recursed).
    """
    if path.exists():
        with open(path) as f:
            data: dict = yaml.safe_load(f) or {}
    else:
        data = {}
    for key, val in updates.items():
        if isinstance(val, dict) and isinstance(data.get(key), dict):
            data[key] = {**data[key], **val}
        else:
            data[key] = val
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        yaml.safe_dump(data, f, default_flow_style=False, allow_unicode=True)
    os.replace(tmp, path)
