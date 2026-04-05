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
        "signal_path": None,
        "input_labels": {},
        "output_slots": [
            {"index": 0, "label": "", "type": "sub"},
            {"index": 1, "label": "", "type": "sub"},
            {"index": 2, "label": "", "type": "unused"},
            {"index": 3, "label": "", "type": "unused"},
        ],
    },
    "mic": {
        "name": "UMIK",
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
        "denon_sweep_volume": -25.0,
        "denon_settle_ms": 800,
        "sweep_channel": "lfe",
        "playback_device": "miniDSP",
        "hdmi_playback_device": None,
        "sub_outputs": [0, 1],
        "ir_search_window_ms": 50.0,
    },
}

CONFIG_TEMPLATE = """\
# AVR Calibration Configuration
# Run 'calibrate check' after editing to verify everything is reachable.

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
  denon_sweep_volume: -25.0    # dB — MUST be ≤ -25.0 (safety limit)
  denon_settle_ms: 800         # ms to wait after Denon input/volume change
  sweep_channel: "lfe"         # "lfe" = LFE/subwoofer channel, "left"/"right" = main
  playback_device: "miniDSP"   # substring matched against USB audio device names
  hdmi_playback_device: null   # HDMI audio device name; null = system default
  sub_outputs: [0, 1]          # miniDSP output indices for each sub (0-indexed)
  ir_search_window_ms: 50.0    # IR peak search window; 50 ms = 17 m at 343 m/s
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
    def sub_outputs(self) -> list[int]:
        """Output indices where type='sub'. Falls back to measurement.sub_outputs."""
        slots = self.minidsp.get("output_slots", [])
        typed = [s["index"] for s in slots if s.get("type") == "sub"]
        if typed:
            return typed
        return self.measurement.get("sub_outputs", [0, 1])

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
