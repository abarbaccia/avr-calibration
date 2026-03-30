"""Configuration loading for avr-calibration."""

from pathlib import Path
import yaml

CONFIG_PATH = Path.home() / ".avr-calibration" / "config.yaml"

DEFAULT_CONFIG: dict = {
    "denon": {
        "host": None,
    },
    "minidsp": {
        "host": "localhost",
        "port": 5380,
        "signal_path": None,
    },
    "mic": {
        "name": "UMIK",
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
  host: "192.168.1.100"  # IP address of your Denon X3800H

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
    def denon(self) -> dict:
        return self._data.get("denon", {})

    @property
    def minidsp(self) -> dict:
        return self._data.get("minidsp", {})

    @property
    def mic(self) -> dict:
        return self._data.get("mic", {})

    @property
    def measurement(self) -> dict:
        return self._data.get("measurement", {})

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
