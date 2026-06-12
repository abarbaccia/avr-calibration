"""Bare-metal measurement HTTP service.

Wraps MeasurementEngine in a FastAPI service that runs natively on the Pi
(as the ``pi`` user) with direct PipeWire access. The Docker MCP server calls
this service via HTTP instead of importing MeasurementEngine directly, which
eliminates the fragile ``/run/user/1000`` socket mount from the container.

Endpoints:
  GET  /health               → {"status": "ok"}
  GET  /devices              → list of sounddevice devices as JSON
  POST /measure              → FrequencyResponse JSON (log-sweep measurement)
  POST /measure_spl_pink     → SPL measurement dict
  POST /measure_impulse_ir   → {ir_samples, sample_rate}
  POST /play_and_measure_fft → FFT analysis dict

Listen address: 127.0.0.1:8767 (loopback only — not exposed beyond the Pi).

Usage:
    python -m calibrate.measurement_service
    uvicorn calibrate.measurement_service:app --host 127.0.0.1 --port 8767
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


# ── Source file hashes (computed at import time) ───────────────────────────────
# These are used by the preflight skew check to detect when the bare-metal
# service is running different source code than the Docker container.
# We hash the installed .py files (not .pyc) for the three files most likely
# to diverge: measurement.py, drivers/playback.py, and this file itself.

def _sha256_file(path: Path) -> str | None:
    """Return hex SHA-256 of a file, or None if unreadable."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _compute_source_hashes() -> dict[str, str]:
    """Compute SHA-256 hashes of the key measurement-path source files.

    Files are resolved relative to this module's own __file__ so the path
    is correct whether installed via pip (site-packages) or run from source.
    """
    here = Path(__file__).parent
    candidates = {
        "measurement.py": here / "measurement.py",
        "drivers/playback.py": here / "drivers" / "playback.py",
        "measurement_service.py": here / "measurement_service.py",
    }
    return {
        name: digest
        for name, path in candidates.items()
        if (digest := _sha256_file(path)) is not None
    }


# Computed once at module import so the FastAPI startup is instant.
_SOURCE_HASHES: dict[str, str] = _compute_source_hashes()

# scipy 1.13+ removed scipy.signal.hanning (use scipy.signal.windows.hann).
# pytta.generate still calls it; patch the alias at import time.
try:
    import scipy.signal as _ss
    if not hasattr(_ss, "hanning"):
        import scipy.signal.windows as _ssw
        _ss.hanning = _ssw.hann
except Exception:
    pass


# ── FastAPI app ────────────────────────────────────────────────────────────────

try:
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel
except ImportError as _exc:
    raise ImportError(
        "fastapi and uvicorn are required for the measurement service. "
        "Install with: pip install fastapi uvicorn"
    ) from _exc

app = FastAPI(title="avr-measurement", version="1.0.0")


# ── Request models ─────────────────────────────────────────────────────────────

class MeasureRequest(BaseModel):
    freq_min: Optional[int] = None
    freq_max: Optional[int] = None
    route: Optional[str] = None
    out_channel_override: Optional[int] = None
    direct_path_window_ms: Optional[float] = None


class MeasureSplPinkRequest(BaseModel):
    channel: int
    duration_s: float = 10.0
    level_dbfs: float = -20.0
    weighting: str = "C"
    n_output_channels: int = 6
    integration_time_s: float = 1.0
    cal_path: Optional[str] = None


class MeasureImpulseIrRequest(BaseModel):
    n_averages: int = 64
    record_duration_s: float = 2.5
    impulse_amplitude: float = 0.9


class PlayAndMeasureFftRequest(BaseModel):
    channel_assignments: dict
    duration_s: float = 2.0
    amplitude: float = 0.5
    fft_size: int = 8192
    n_channels: int = 6
    sample_rate: int = 48000


# ── Config helper ──────────────────────────────────────────────────────────────

def _cfg():
    """Load config from the pi-user's config directory."""
    from .config import Config
    return Config.load()


# ── Endpoints ──────────────────────────────────────────────────────────────────

_AUDIO_MODE_STATE_FILE = "/var/lib/audio-mode"


def _read_audio_mode() -> str | None:
    """Read the current audio-mode from the host state file.

    Returns the mode string (e.g. 'listening', 'cal', 'karaoke') or None
    if the state file is absent or unreadable (old deployments without the
    file, or pre-319a5a2 installs using /run/audio-mode).
    """
    import os
    try:
        with open(_AUDIO_MODE_STATE_FILE) as f:
            return f.read().strip() or None
    except OSError:
        return None


@app.get("/health")
async def health():
    mode = _read_audio_mode()
    payload: dict = {"status": "ok"}
    if mode is not None:
        payload["audio_mode"] = mode
    if _SOURCE_HASHES:
        payload["source_hashes"] = _SOURCE_HASHES
    return payload


@app.get("/devices")
async def list_devices():
    """Return all sounddevice devices as a JSON list."""
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        # Convert to plain dicts (sounddevice returns DeviceList of dicts)
        result = []
        for i, d in enumerate(devices):
            entry = dict(d)
            entry["index"] = i
            result.append(entry)
        return JSONResponse(content=result)
    except Exception as exc:
        log.exception("list_devices failed")
        return JSONResponse(status_code=500, content={"error": str(exc)})


@app.post("/measure")
async def measure(req: MeasureRequest):
    """Run a full log-sweep measurement. Returns FrequencyResponse JSON."""
    try:
        from .measurement import MeasurementEngine
        cfg = _cfg()
        engine = MeasurementEngine(cfg)
        if req.direct_path_window_ms is not None and req.direct_path_window_ms > 0:
            engine.direct_path_window_ms = float(req.direct_path_window_ms)
        fr = await engine.measure(
            freq_min=req.freq_min,
            freq_max=req.freq_max,
            route=req.route,
            out_channel_override=req.out_channel_override,
        )
        return JSONResponse(content={"ok": True, "result": fr.to_json()})
    except Exception as exc:
        log.exception("measure failed")
        return JSONResponse(status_code=500, content={"error": str(exc)})


@app.post("/measure_spl_pink")
async def measure_spl_pink(req: MeasureSplPinkRequest):
    """Play pink noise on one HDMI channel, capture from UMIK, return SPL."""
    try:
        import asyncio
        from .measurement import measure_pink_spl
        cfg = _cfg()
        sample_rate = int(cfg.measurement.get("sample_rate", 48000))
        cal_path = req.cal_path or cfg._data.get("mic", {}).get("cal_file")
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: measure_pink_spl(
                duration_s=req.duration_s,
                level_dbfs=req.level_dbfs,
                sample_rate=sample_rate,
                weighting=req.weighting,
                output_channel=req.channel,
                n_output_channels=req.n_output_channels,
                umik_cal_path=cal_path,
                integration_time_s=req.integration_time_s,
            ),
        )
        return JSONResponse(content={"ok": True, "result": result})
    except Exception as exc:
        log.exception("measure_spl_pink failed")
        return JSONResponse(status_code=500, content={"error": str(exc)})


@app.post("/measure_impulse_ir")
async def measure_impulse_ir(req: MeasureImpulseIrRequest):
    """Measure room impulse response via averaged impulse shots."""
    try:
        from .measurement import MeasurementEngine
        cfg = _cfg()
        engine = MeasurementEngine(cfg)
        ir = await engine.measure_impulse_ir(
            n_averages=req.n_averages,
            record_duration_s=req.record_duration_s,
            impulse_amplitude=req.impulse_amplitude,
        )
        sample_rate = int(cfg.measurement.get("sample_rate", 48000))
        return JSONResponse(content={
            "ok": True,
            "result": {
                "ir_samples": ir,
                "sample_rate": sample_rate,
            },
        })
    except Exception as exc:
        log.exception("measure_impulse_ir failed")
        return JSONResponse(status_code=500, content={"error": str(exc)})


@app.post("/play_and_measure_fft")
async def play_and_measure_fft(req: PlayAndMeasureFftRequest):
    """Synthesize multitone, play via HDMI, record from UMIK, return FFT analysis."""
    try:
        import asyncio
        import numpy as np
        from .headroom import analyze_fft, build_multichannel_buffer
        from .drivers.playback import MultichannelPlayback
        from .measurement import _find_umik_device
        import sounddevice as sd

        cfg = _cfg()

        # Parse channel_assignments: keys may be strings from JSON
        parsed: dict[int, list[float]] = {}
        all_freqs: list[float] = []
        for ch_str, freqs in req.channel_assignments.items():
            ch = int(ch_str)
            parsed[ch] = [float(f) for f in freqs]
            all_freqs.extend(parsed[ch])

        if not parsed:
            return JSONResponse(status_code=400, content={"error": "channel_assignments is empty"})

        buf = build_multichannel_buffer(
            parsed, req.duration_s, req.sample_rate, req.amplitude, req.n_channels,
        )

        devices = sd.query_devices()
        mic_idx = cfg.measurement.get("mic_device_index")
        if mic_idx is None:
            mic_name = cfg.mic.get("name", "UMIK")
            mic_idx = _find_umik_device(devices, name_substring=mic_name)
        if mic_idx is None:
            return JSONResponse(status_code=500, content={"error": "UMIK microphone not found — check USB connection"})

        hdmi_idx = cfg.measurement.get("hdmi_device_index")
        if hdmi_idx is None:
            candidates = [
                (i, d) for i, d in enumerate(devices)
                if d["max_output_channels"] > 0 and "hdmi" in d["name"].lower()
            ]
            candidates.sort(key=lambda x: (x[1]["name"].lower() != "hdmi", len(x[1]["name"])))
            if candidates:
                hdmi_idx = candidates[0][0]
        if hdmi_idx is None:
            return JSONResponse(status_code=500, content={"error": "No HDMI output device found"})

        player = MultichannelPlayback()
        recording, n_recorded = await asyncio.get_event_loop().run_in_executor(
            None, player.play_and_record, buf, req.sample_rate, mic_idx, hdmi_idx,
        )

        if len(recording) == 0:
            return JSONResponse(status_code=500, content={"error": "Recording is empty — no audio captured"})

        result = analyze_fft(recording, req.sample_rate, all_freqs, req.fft_size)
        peak_dbfs = 20.0 * np.log10(np.max(np.abs(recording)) + 1e-12)

        return JSONResponse(content={
            "ok": True,
            "result": {
                "duration_s": req.duration_s,
                "sample_rate": req.sample_rate,
                "recording_peak_dbfs": round(float(peak_dbfs), 1),
                **result,
            },
        })
    except Exception as exc:
        log.exception("play_and_measure_fft failed")
        return JSONResponse(status_code=500, content={"error": str(exc)})


# ── Entrypoint ─────────────────────────────────────────────────────────────────

def main():
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="127.0.0.1", port=8767)


if __name__ == "__main__":
    main()
