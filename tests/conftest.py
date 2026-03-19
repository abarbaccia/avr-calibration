"""Shared pytest fixtures."""

import sys
import pytest
from unittest.mock import MagicMock
from calibrate.config import Config


@pytest.fixture(autouse=True, scope="session")
def fake_sounddevice_module():
    """
    Inject a fake sounddevice module into sys.modules for the entire test session.

    sounddevice imports PortAudio at module load time and raises OSError if the
    shared library isn't present (common in CI). Since check_mic() does a lazy
    import, we pre-populate sys.modules so that import resolves to our mock.
    Individual tests configure query_devices() return values as needed.
    """
    mock_sd = MagicMock()
    sys.modules.setdefault("sounddevice", mock_sd)
    yield mock_sd


@pytest.fixture
def config() -> Config:
    return Config({
        "denon": {"host": "192.168.1.100"},
        "minidsp": {"host": "localhost", "port": 5380},
        "mic": {"name": "UMIK"},
    })


def make_input_device(name: str, channels: int = 2, sample_rate: float = 48000.0) -> dict:
    return {
        "name": name,
        "max_input_channels": channels,
        "max_output_channels": 0,
        "default_samplerate": sample_rate,
    }


def make_output_device(name: str) -> dict:
    return {
        "name": name,
        "max_input_channels": 0,
        "max_output_channels": 2,
        "default_samplerate": 48000.0,
    }
