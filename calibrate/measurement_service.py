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


def _read_pw_capture_links() -> dict | None:
    """Probe PipeWire link state for camilladsp_capture contamination.

    Runs ``pw-link -l`` (host-side) and returns a dict:
      {
        "umik_into_dsp": bool,   # True when any UMIK source is linked into camilladsp_capture
        "sources": [str, ...],   # list of node names currently linked into camilladsp_capture
      }

    Returns None on any error (pw-link absent, timeout, parse failure) so
    callers can omit the field gracefully when the information is unavailable.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["pw-link", "-l"],
            capture_output=True,
            text=True,
            timeout=3.0,
        )
    except FileNotFoundError:
        return None
    except Exception:
        return None

    output = result.stdout or ""
    # pw-link -l format:
    #   <source_node>:<source_port>
    #     |-> <dest_node>:<dest_port>
    #   ...
    # Parse by tracking the current source node and collecting destinations
    # that contain "camilladsp_capture".
    current_source: str | None = None
    sources_into_dsp: set[str] = set()

    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if stripped.startswith("|->"):
            # Destination line: check if it feeds camilladsp_capture
            dest = stripped[3:].strip()
            if "camilladsp_capture" in dest and current_source is not None:
                sources_into_dsp.add(current_source)
        else:
            # Source line: extract node name (everything before the first colon)
            if ":" in stripped:
                current_source = stripped.split(":")[0]
            else:
                current_source = None

    umik_into_dsp = any(
        "umik" in s.lower() or "minidsp_umik" in s.lower()
        for s in sources_into_dsp
    )
    return {
        "umik_into_dsp": umik_into_dsp,
        "sources": sorted(sources_into_dsp),
    }


@app.get("/health")
async def health():
    mode = _read_audio_mode()
    payload: dict = {"status": "ok"}
    if mode is not None:
        payload["audio_mode"] = mode
    if _SOURCE_HASHES:
        payload["source_hashes"] = _SOURCE_HASHES
    pw_links = _read_pw_capture_links()
    if pw_links is not None:
        payload["pw_capture_links"] = pw_links
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
        # R11: under the 2-ch sample-locked loopback the UMIK is consumed into
        # loopback_ref and is no longer a standalone PortAudio device, so
        # _find_umik_device returns None. The mic still arrives via the default
        # PortAudio input (loopback path), so tolerate a missing explicit index
        # and let play_and_record fall back to sd.default.device[0]. Only the
        # legacy 1-ch path hard-fails on a missing UMIK.
        _loopback_2ch = int(cfg.measurement.get("loopback_ref_pw_channels", 1)) >= 2
        if mic_idx is None and not _loopback_2ch:
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


# ── Hardware mute / audio stack diagnostics ────────────────────────────────────
#
# These two endpoints run Pi-host commands (amixer, pw-dump, pw-link, systemctl)
# that the Docker MCP container cannot reach directly. The MCP server delegates
# here via MeasurementServiceClient — the same pattern used by check_system and
# the UMIK contamination preflight check.


class HardwareMuteRequest(BaseModel):
    output_index: int
    muted: bool


# CamillaDSP output_index → Scarlett "Line 0N Mute" control name.
# output_index 5 = CamillaDSP channel 5 (sub_front_right) = Scarlett Line 06
# output_index 6 = CamillaDSP channel 6 (sub_nearfield)   = Scarlett Line 07
# output_index 7 = CamillaDSP channel 7 (shaker)          = Scarlett Line 08
# General formula: Scarlett line number = output_index + 1, formatted "Line %02d"
_KNOWN_OUTPUT_INDICES: set[int] = {5, 6, 7}


def _output_index_to_scarlett_line(output_index: int) -> str:
    """Return the amixer control name for a CamillaDSP output index.

    Scarlett 18i20 line numbers are 1-based; CamillaDSP output indices map
    as: Line_N = output_index + 1.  The audio-mode script uses 'Line 08 Mute'
    for the shaker (index 7), confirming the formula.
    """
    line_number = output_index + 1
    return f"Line {line_number:02d} Mute"


@app.post("/hardware_mute_output")
async def hardware_mute_output(req: HardwareMuteRequest):
    """Mute or unmute a Scarlett 18i20 physical output via amixer.

    WHY THIS EXISTS: the existing mute_output MCP tool mutes INSIDE CamillaDSP
    by pushing a gain=-127dB config update, which shifts the PipeWire processing
    quantum and invalidates loopback timing / cross-measurement phase comparison.
    Hardware mute at the Scarlett analog output makes no DSP config change —
    required for Trinnov-style per-sub solo isolation where phase coherence must
    stay comparable across solos.

    Maps output_index to Scarlett control name using the formula:
      control = "Line {output_index + 1:02d} Mute"
    amixer value: on=muted, off=unmuted (standard ALSA toggle sense).
    """
    import subprocess

    output_index = req.output_index
    if output_index not in _KNOWN_OUTPUT_INDICES:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": (
                    f"output_index {output_index} is not a known hardware-mutable output. "
                    f"Known indices: {sorted(_KNOWN_OUTPUT_INDICES)} "
                    f"(5=sub_front_right Line06, 6=sub_nearfield Line07, 7=shaker Line08)"
                ),
            },
        )

    line_ctl = _output_index_to_scarlett_line(output_index)
    amixer_state = "on" if req.muted else "off"
    card = "USB"

    try:
        result = subprocess.run(
            ["amixer", "-c", card, "sset", line_ctl, amixer_state],
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except FileNotFoundError:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": "amixer not found — is alsa-utils installed on the Pi host?"},
        )
    except subprocess.TimeoutExpired:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": f"amixer timed out setting {line_ctl}"},
        )
    except Exception as exc:
        log.exception("hardware_mute_output: amixer failed")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})

    if result.returncode != 0:
        stderr = result.stderr.strip()
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "error": f"amixer returned exit {result.returncode}: {stderr or result.stdout.strip()}",
            },
        )

    log.info(
        "hardware_mute_output: output_index=%d line=%s muted=%s",
        output_index, line_ctl, req.muted,
    )
    return JSONResponse(content={
        "ok": True,
        "output_index": output_index,
        "line": line_ctl,
        "muted": req.muted,
        "amixer_card": card,
    })


def _check_pw_link_l() -> dict:
    """Run pw-link -l and parse wiring state for the audio stack health check.

    Returns a dict with:
      - input3_linked: bool  (avr_cal_sweep:monitor_FL → camilladsp_capture:input_3)
      - loopback_ref_linked: bool  (avr_cal_sweep:monitor_FL → loopback_ref:playback_FL, or legacy playback_1)
      - umik_into_dsp: bool  (any UMIK → camilladsp_capture link — feedback-loop hazard)
      - umik_sources: list[str]  (node names of UMIK sources linked into DSP capture)
      - playback_link_count: int  (camilladsp_playback → Scarlett links, expect ~20)
      - error: str | None
    """
    import subprocess

    try:
        result = subprocess.run(
            ["pw-link", "-l"],
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except FileNotFoundError:
        return {"error": "pw-link not found"}
    except subprocess.TimeoutExpired:
        return {"error": "pw-link -l timed out"}
    except Exception as exc:
        return {"error": str(exc)}

    output = result.stdout or ""

    # Parse pw-link -l output:
    #   <source_node>:<source_port>
    #     |-> <dest_node>:<dest_port>
    current_source_port: str | None = None
    input3_linked = False
    loopback_ref_linked = False
    umik_sources: set[str] = set()
    playback_link_count = 0

    for raw_line in output.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("|->"):
            dest = stripped[3:].strip()
            if current_source_port is None:
                continue
            src_node = current_source_port.split(":")[0]

            # Load-bearing LFE feed to subs
            if (
                "avr_cal_sweep" in src_node
                and "camilladsp_capture:input_3" in dest
            ):
                input3_linked = True

            # Deconvolution reference tap. Under R11 the loopback_ref sink is
            # 2-channel so the port is playback_FL; the legacy 1-ch sink used
            # playback_1. Accept either.
            if "avr_cal_sweep" in src_node and (
                "loopback_ref:playback_FL" in dest
                or "loopback_ref:playback_1" in dest
            ):
                loopback_ref_linked = True

            # UMIK → camilladsp_capture (feedback-loop hazard)
            if (
                ("umik" in src_node.lower() or "minidsp_umik" in src_node.lower())
                and "camilladsp_capture" in dest
            ):
                umik_sources.add(src_node)

            # Count camilladsp_playback → Scarlett links (expect ~20)
            if "camilladsp_playback" in src_node and "scarlett" in dest.lower():
                playback_link_count += 1

        else:
            current_source_port = stripped if ":" in stripped else None

    return {
        "error": None,
        "input3_linked": input3_linked,
        "loopback_ref_linked": loopback_ref_linked,
        "umik_into_dsp": len(umik_sources) > 0,
        "umik_sources": sorted(umik_sources),
        "playback_link_count": playback_link_count,
    }


def _check_pw_dump_umik() -> dict:
    """Run pw-dump and extract UMIK node state + resample.quality.

    Returns:
      - present: bool
      - node_state: str | None  (e.g. "running", "idle", "suspended")
      - resample_quality: int | None  (must be 14 for UMIK — flag if not)
      - autoconnect: bool | None  (should be false for UMIK)
      - error: str | None
    """
    import json as _json
    import subprocess

    try:
        result = subprocess.run(
            ["pw-dump"],
            capture_output=True,
            text=True,
            timeout=10.0,
        )
    except FileNotFoundError:
        return {"error": "pw-dump not found"}
    except subprocess.TimeoutExpired:
        return {"error": "pw-dump timed out"}
    except Exception as exc:
        return {"error": str(exc)}

    try:
        nodes = _json.loads(result.stdout)
    except Exception as exc:
        return {"error": f"pw-dump JSON parse error: {exc}"}

    # Find the UMIK node — match by media.class=Audio/Source and UMIK in name
    umik_node: dict | None = None
    for node in nodes:
        if not isinstance(node, dict):
            continue
        props = node.get("info", {}).get("props", {})
        node_name = str(props.get("node.name", "") or props.get("alsa.card_name", ""))
        media_class = str(props.get("media.class", ""))
        if "umik" in node_name.lower() and "Audio/Source" in media_class:
            umik_node = node
            break

    if umik_node is None:
        return {
            "error": None,
            "present": False,
            "node_state": None,
            "resample_quality": None,
            "autoconnect": None,
        }

    info = umik_node.get("info", {})
    props = info.get("props", {})
    state_str = str(info.get("state", "")).lower()

    resample_raw = props.get("resample.quality")
    resample_quality: int | None = None
    if resample_raw is not None:
        try:
            resample_quality = int(resample_raw)
        except (ValueError, TypeError):
            pass

    autoconnect_raw = props.get("node.autoconnect")
    autoconnect: bool | None = None
    if autoconnect_raw is not None:
        if isinstance(autoconnect_raw, bool):
            autoconnect = autoconnect_raw
        else:
            autoconnect = str(autoconnect_raw).lower() not in ("false", "0", "no")

    return {
        "error": None,
        "present": True,
        "node_state": state_str or None,
        "resample_quality": resample_quality,
        "autoconnect": autoconnect,
    }


def _check_camilladsp_state() -> dict:
    """Query CamillaDSP websocket for state and CPU load.

    Returns:
      - state: str | None  (e.g. "Running", "Starting", "Idle")
      - cpu_load_percent: float | None
      - error: str | None
    """
    import asyncio as _asyncio
    import json as _json

    async def _query() -> dict:
        try:
            import websockets  # type: ignore[import]
        except ImportError:
            return {"error": "websockets library not installed"}
        try:
            async with websockets.connect(
                "ws://127.0.0.1:1234", open_timeout=3, close_timeout=2
            ) as ws:
                await ws.send('"GetState"')
                state_raw = _json.loads(await _asyncio.wait_for(ws.recv(), timeout=3))
                state_val = state_raw.get("GetState", {}).get("value")

                # GetCpuLoad — not all CamillaDSP versions support this; soft-fail
                cpu: float | None = None
                try:
                    await ws.send('"GetCpuLoad"')
                    cpu_raw = _json.loads(await _asyncio.wait_for(ws.recv(), timeout=2))
                    cpu = float(cpu_raw.get("GetCpuLoad", {}).get("result", 0.0))
                except Exception:
                    pass

                return {
                    "error": None,
                    "state": str(state_val) if state_val else None,
                    "cpu_load_percent": cpu,
                }
        except Exception as exc:
            return {"error": str(exc), "state": None, "cpu_load_percent": None}

    # We're called from run_in_executor so there's no running loop in this thread.
    return _asyncio.run(_query())


def _check_service_states(service_names: list[str]) -> dict[str, str]:
    """Return systemd active state for each named service.

    Values are systemd active-state strings: "active", "inactive",
    "failed", "activating", etc.  Returns "unknown" on error.
    """
    import subprocess

    result: dict[str, str] = {}
    for svc in service_names:
        try:
            r = subprocess.run(
                ["systemctl", "is-active", svc],
                capture_output=True,
                text=True,
                timeout=5.0,
            )
            result[svc] = r.stdout.strip() or "unknown"
        except Exception:
            result[svc] = "unknown"
    return result


@app.get("/audio_stack_health")
async def audio_stack_health():
    """One-call health report of the audio infrastructure.

    Runs host-side commands (pw-dump, pw-link, systemctl, CamillaDSP websocket)
    that the Docker MCP container cannot reach, and returns structured JSON.
    The MCP tool ``diagnose_audio_stack`` calls this endpoint via
    MeasurementServiceClient.

    Response fields:
      camilladsp: {state, cpu_load_percent, error}
      umik: {present, node_state, resample_quality, autoconnect, error}
      wiring: {input3_linked, loopback_ref_linked, umik_into_dsp, umik_sources,
               playback_link_count, error}
      services: {<service>: <active-state>, ...}
      healthy: bool
      warnings: [str, ...]
    """
    import asyncio as _asyncio

    warnings: list[str] = []

    # Run independent probes in a thread pool so they don't block each other.
    loop = _asyncio.get_event_loop()
    camilla_task = loop.run_in_executor(None, _check_camilladsp_state)
    umik_task = loop.run_in_executor(None, _check_pw_dump_umik)
    wiring_task = loop.run_in_executor(None, _check_pw_link_l)
    services_task = loop.run_in_executor(
        None,
        _check_service_states,
        ["camilladsp.service", "avr-measurement.service", "camilladsp-watchdog.service"],
    )

    camilla, umik, wiring, services = await _asyncio.gather(
        camilla_task, umik_task, wiring_task, services_task,
    )

    # ── Warning accumulation ────────────────────────────────────────────────

    # CamillaDSP
    if camilla.get("error"):
        warnings.append(f"camilladsp probe error: {camilla['error']}")
    elif camilla.get("state") != "Running":
        warnings.append(f"camilladsp not Running (state={camilla.get('state')})")

    # UMIK
    if umik.get("error"):
        warnings.append(f"umik probe error: {umik['error']}")
    elif not umik.get("present"):
        warnings.append("UMIK microphone node not found in PipeWire graph")
    else:
        rq = umik.get("resample_quality")
        if rq != 14:
            warnings.append(
                f"UMIK resample.quality={rq} (expected 14) — "
                "ALSA capture quality degraded; check wireplumber-umik.lua"
            )
        if umik.get("autoconnect") is True:
            warnings.append(
                "UMIK node.autoconnect=true — WirePlumber may auto-link UMIK "
                "into DSP capture (feedback-loop hazard); set autoconnect_to: null"
            )

    # Wiring
    if wiring.get("error"):
        warnings.append(f"pw-link probe error: {wiring['error']}")
    else:
        if not wiring.get("input3_linked"):
            warnings.append(
                "MISSING: avr_cal_sweep:monitor_FL → camilladsp_capture:input_3 "
                "(load-bearing LFE sub feed) — subs will be silent during cal"
            )
        if not wiring.get("loopback_ref_linked"):
            warnings.append(
                "MISSING: avr_cal_sweep:monitor_FL → loopback_ref:playback_FL "
                "(deconvolution reference tap) — coherence will collapse"
            )
        if wiring.get("umik_into_dsp"):
            srcs = ", ".join(wiring.get("umik_sources", [])) or "unknown"
            warnings.append(
                f"FEEDBACK LOOP HAZARD: UMIK linked into camilladsp_capture "
                f"(sources: {srcs}) — mic→DSP→subs→room→mic loop; "
                "random polarity, SPL drift, xcorr instability. "
                "Run: audio-mode wire (re-wires and removes UMIK links)"
            )
        plc = wiring.get("playback_link_count", 0)
        if plc < 20:
            warnings.append(
                f"Only {plc}/20 camilladsp_playback→Scarlett links up — "
                "some outputs may be silent"
            )

    # Services
    for svc, state in services.items():
        if state == "unknown":
            warnings.append(f"service {svc} state unknown (systemctl unavailable?)")
        elif state != "active":
            warnings.append(f"service {svc} is {state} (expected active)")

    healthy = len(warnings) == 0

    return JSONResponse(content={
        "ok": True,
        "healthy": healthy,
        "warnings": warnings,
        "camilladsp": {
            "state": camilla.get("state"),
            "cpu_load_percent": camilla.get("cpu_load_percent"),
            "error": camilla.get("error"),
        },
        "umik": {
            "present": umik.get("present", False),
            "node_state": umik.get("node_state"),
            "resample_quality": umik.get("resample_quality"),
            "autoconnect": umik.get("autoconnect"),
            "error": umik.get("error"),
        },
        "wiring": {
            "input3_linked": wiring.get("input3_linked", False),
            "loopback_ref_linked": wiring.get("loopback_ref_linked", False),
            "umik_into_dsp": wiring.get("umik_into_dsp", False),
            "umik_sources": wiring.get("umik_sources", []),
            "playback_link_count": wiring.get("playback_link_count", 0),
            "error": wiring.get("error"),
        },
        "services": services,
    })


# ── Entrypoint ─────────────────────────────────────────────────────────────────

def main():
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="127.0.0.1", port=8767)


if __name__ == "__main__":
    main()
