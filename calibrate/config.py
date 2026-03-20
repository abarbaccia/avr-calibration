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
    },
}

CONFIG_TEMPLATE = """\
# AVR Calibration Configuration
# Run 'calibrate check' after editing to verify everything is reachable.

denon:
  host: "192.168.1.100"  # IP address of your Denon X3800H

minidsp:
  host: "localhost"
  port: 5380             # default minidspd port (run: minidspd)

mic:
  name: "UMIK"           # substring matched against audio device names

measurement:
  freq_min: 20           # Hz — lower bound of calibration band
  freq_max: 200          # Hz — upper bound (bass calibration only)
  sweep_duration: 3.0    # seconds
  sample_rate: 48000     # Hz
  input_channel: 1       # audio device channel for microphone
  output_channel: 1      # audio device channel for subwoofer output
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
