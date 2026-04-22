"""MCP server — HTTP/SSE interface exposing Pi hardware control to Claude.

Exposes the DSP + AVR as MCP tools and resources so Claude Code can drive
the full calibration loop without a browser.

All hardware-specific logic lives in the driver layer (calibrate/drivers/).
This file contains zero direct references to denonavr or MinidspClient —
those are encapsulated in DenonDriver and MinidspDriver respectively.

Transport: HTTP/SSE on port 8765 (configurable via MCP_PORT env var)
Framework: mcp Python SDK (official Anthropic MCP library)

Tools:
  get_device_state       — current AVR + DSP hardware status
  get_measurement_history — last N measurements from SessionStore (full FR data)
  get_fr_summary         — 1/3-octave downsampled FR (compact, for analysis)
  apply_eq               — SafetyValidator → biquad conversion → DSP write
  set_volume             — AVR volume control
  measure                — trigger sweep measurement via UMIK + PyTTa
  mute_output            — mute DSP outputs (gain → -127 dB)
  unmute_output          — unmute DSP outputs (gain → 0 dB)
  set_delay              — set output delay in ms
  set_polarity           — set output polarity (normal/inverted)
  get_output_state       — per-output gain, delay, polarity, fir_taps (in-memory tracking)
  set_output_gain        — set gain for a single DSP output (dB)
  apply_fir              — write FIR coefficients to a DSP output (via CLI, WAV temp file)
  clear_fir              — clear FIR and reset to passthrough
  analyze_ir             — IR peak time, polarity, SPL from stored session (sub alignment)
  analyze_decay          — T60 decay analysis on stored IR; returns ringing modes with priority
  configure_matrix       — configure miniDSP routing matrix (active input → all outputs)
  check_system           — pre-flight hardware checks
  fetch_recipe           — serve recipe markdown from recipes/ directory

HARD RULE — Signal Path Writes Require Human Confirmation:
  Tools that change DSP routing, input source, or preset selection MUST NOT be
  called autonomously by an AI agent. The miniDSP hardware flash is the source of
  truth; overwriting it without user approval can destroy a working configuration.

  If you (as an AI) want to change routing, source, or preset, you MUST:
    1. Describe exactly what you intend to change and why.
    2. Ask the user to confirm before calling any tool that writes to hardware.
    3. Do not proceed until the user explicitly approves.

  EQ (apply_eq) is exempt — that is the calibration output and is always
  intentional. Volume (avr_set_volume) is exempt — it is transient and safe.
  Everything else that touches DSP configuration requires explicit approval.

Resources:
  measurements://latest  — most recent measurement session

Usage (standalone, for development):
  python -m calibrate.mcp_server

Usage (via Docker Compose service):
  Entrypoint: python -m calibrate.mcp_server
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import (
    CallToolResult,
    ListResourcesResult,
    ListToolsResult,
    ReadResourceResult,
    Resource,
    TextContent,
    Tool,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Mount, Route

from .config import Config, update_config
from .drivers.avr_driver import AVRDriver
from .drivers.base import DriverError
from .drivers.denon import DenonSweepContext
from .drivers.dsp_driver import DSPDriver
from .drivers.registry import load_avr_driver, load_dsp_driver

log = logging.getLogger(__name__)


def _biquad_response(freq: float, filter_type: str, fc: float,
                     gain_db: float = 0.0, q: float = 1.0) -> float:
    """Exact biquad magnitude response at *freq* Hz via z-domain evaluation.

    Uses the RBJ Audio EQ Cookbook coefficients from dsp.py, evaluated at the
    active DSP's processing sample rate. Shared by simulate_eq and optimize_q.
    """
    import cmath
    import math

    from .dsp import SAMPLE_RATE_HZ, freq_gain_q_to_biquad

    if fc <= 0 or freq <= 0:
        return 0.0
    sample_rate = _dsp.capabilities.processing_rate if _dsp is not None else SAMPLE_RATE_HZ
    bq = freq_gain_q_to_biquad(
        freq=fc, gain_db=gain_db, q=q, filter_type=filter_type,
        sample_rate=sample_rate,
    )
    z = cmath.exp(1j * 2.0 * math.pi * freq / sample_rate)
    zi = 1.0 / z
    num = bq["b0"] + bq["b1"] * zi + bq["b2"] * zi * zi
    den = 1.0 + bq["a1"] * zi + bq["a2"] * zi * zi
    if abs(den) < 1e-30:
        return 0.0
    return 20.0 * math.log10(abs(num / den))


# ── Configuration ──────────────────────────────────────────────────────────────

MCP_PORT: int = int(os.environ.get("MCP_PORT", "8765"))
MCP_HOST: str = os.environ.get("MCP_HOST", "0.0.0.0")

# recipes/ is a sibling of calibrate/ in the repo, but when pip-installed
# it ends up at /app/recipes/ (Docker WORKDIR) not next to site-packages/.
_REPO_RECIPES = Path(__file__).parent.parent / "recipes"
_APP_RECIPES = Path("/app/recipes")
RECIPES_DIR: Path = _REPO_RECIPES if _REPO_RECIPES.is_dir() else _APP_RECIPES

# ── Driver singletons — set in lifespan, patched in tests ─────────────────────

_avr: AVRDriver | None = None
_dsp: DSPDriver | None = None

# ── Persistent sweep session ─────────────────────────────────────────────────
# Holds the miniDSP in USB source mode across multiple measurements so we
# don't switch Analog→USB→Analog on every single sweep.  Enter on the first
# measurement, exit explicitly via end_sweep_session or on server shutdown.

_sweep_session = None  # driver.sweep_context() result | None


async def _ensure_sweep_session():
    """Return the active sweep session, creating one if needed.

    Returns None when USB sweep mode is not configured.
    """
    global _sweep_session
    if _sweep_session is not None and _sweep_session.active:
        return _sweep_session

    if _dsp is None:
        return None

    cfg = _config()
    session = _dsp.sweep_context(cfg)
    if session is None:
        return None

    await session.enter()
    _sweep_session = session
    return session


async def _end_sweep_session():
    """End the persistent sweep session (restore source to Analog)."""
    global _sweep_session
    if _sweep_session is not None:
        await _sweep_session.exit()
        _sweep_session = None


# ── Helpers ────────────────────────────────────────────────────────────────────

def _ok(**kwargs: Any) -> dict:
    return {"ok": True, **kwargs}


def _err(message: str) -> dict:
    return {"ok": False, "error": message}


def _config() -> Config:
    """Load config per-call to support hot-reload without server restart."""
    return Config.load()


async def _safe_driver_state(driver: Any) -> dict:
    """Get driver state, returning {connected: False} on any error."""
    try:
        return await driver.get_state()
    except Exception as exc:
        return {"connected": False, "error": str(exc)}


def _persist_dsp_state(key: str, data: dict) -> None:
    """Save a DSP state entry to SQLite (fire-and-forget, never fails the caller)."""
    try:
        from .storage import SessionStore
        store = SessionStore()
        store.set_active_dsp(key, data)
    except Exception as exc:
        log.warning("failed to persist DSP state key=%s: %s", key, exc)


# ── Tool implementations ───────────────────────────────────────────────────────

async def _tool_get_device_state() -> dict:
    """Return current AVR + DSP hardware state."""
    avr_result = await _safe_driver_state(_avr)
    dsp_result = await _safe_driver_state(_dsp)
    return _ok(avr=avr_result, dsp=dsp_result)


def _filter_and_decimate_fr(
    freqs: list[float],
    spl: list[float],
    min_hz: float | None,
    max_hz: float | None,
    decimation: int,
) -> tuple[list[float], list[float]]:
    """Apply frequency range filter and decimation to a FR pair. Returns (freqs, spls)."""
    pairs = list(zip(freqs, spl))
    if min_hz is not None:
        pairs = [(f, v) for f, v in pairs if f >= min_hz]
    if max_hz is not None:
        pairs = [(f, v) for f, v in pairs if f <= max_hz]
    if decimation > 1:
        pairs = pairs[::decimation]
    return (
        [round(f, 2) for f, _ in pairs],
        [round(v, 2) for _, v in pairs],
    )


async def _tool_get_measurement_history(
    limit: int = 10,
    min_hz: float | None = None,
    max_hz: float | None = None,
    decimation: int = 1,
    fmt: str = "compact",
    include_phase: bool = False,
) -> dict:
    """Return last *limit* measurement sessions from SessionStore.

    Args:
        limit: Number of sessions to return.
        min_hz: Low-frequency cutoff — only return data at or above this frequency.
        max_hz: High-frequency cutoff — only return data at or below this frequency.
        decimation: Keep every Nth point (1 = all points, 2 = every other, etc.).
        fmt: Output format — "compact" (default; single "fr" string "freq:spl,...",
             ~3x smaller) or "full" (separate freq_hz[]/spl_db[] arrays).
        include_phase: When True and fmt="full", include phase_rad[] array.
             Phase is only needed for sub alignment and analyze_phase — omit it
             from the per-iteration EQ loop to save ~8K tokens per measurement.
    """
    from .storage import SessionStore
    try:
        store = SessionStore()
        sessions = store.list_sessions()[:limit]
        result = []
        for s in sessions:
            fr = s.start_fr
            freqs: list[float]
            spls: list[float]
            if fr:
                freqs, spls = _filter_and_decimate_fr(
                    fr.frequencies, fr.spl, min_hz, max_hz, decimation
                )
                if fr.phase and fmt == "full" and include_phase:
                    phase_freqs, phase_vals = _filter_and_decimate_fr(
                        fr.frequencies, fr.phase, min_hz, max_hz, decimation
                    )
                else:
                    phase_freqs, phase_vals = [], []
            else:
                freqs, spls, phase_freqs, phase_vals = [], [], [], []

            entry: dict = {
                "id": s.id,
                "timestamp": s.timestamp,
                "label": s.label,
            }
            if fmt == "compact":
                # Encode as compact "freq:spl,freq:spl,..." string — ~12 chars/point
                # vs ~40 chars/point in full JSON array format (3x smaller)
                entry["fr"] = ",".join(
                    f"{f:.2f}:{v:.1f}" for f, v in zip(freqs, spls)
                )
                entry["point_count"] = len(freqs)
            else:
                entry["freq_hz"] = freqs
                entry["spl_db"] = spls
                if phase_vals:
                    entry["phase_rad"] = [round(v, 4) for v in phase_vals]
            if s.metadata:
                if fmt == "compact":
                    # In compact mode, downsample group_delay to 1/3-octave (not strip)
                    compact_meta = {}
                    for k, v in s.metadata.items():
                        if k == "group_delay" and isinstance(v, dict) and "freq_hz" in v:
                            compact_meta[k] = _downsample_group_delay(v["freq_hz"], v.get("delay_ms", v.get("gd_ms", [])))
                        else:
                            compact_meta[k] = v
                    entry["metadata"] = compact_meta
                else:
                    entry["metadata"] = s.metadata
            result.append(entry)
        return _ok(sessions=result, count=len(result))
    except Exception as exc:
        return _err(f"storage error: {exc}")


# ── 1/3-octave centre frequencies (ISO 266) for bass calibration band ────────
_THIRD_OCTAVE_CENTRES = [
    20.0, 25.0, 31.5, 40.0, 50.0, 63.0, 80.0, 100.0, 125.0, 160.0, 200.0,
]


def _downsample_to_third_octave(
    freqs: list[float], spl: list[float],
) -> list[dict]:
    """Average FR data into 1/3-octave bands. Returns list of {freq_hz, spl_db}."""
    import math
    bands = []
    for centre in _THIRD_OCTAVE_CENTRES:
        # 1/3-octave band edges: centre / 2^(1/6) to centre * 2^(1/6)
        factor = 2 ** (1 / 6)
        lo = centre / factor
        hi = centre * factor
        vals = [s for f, s in zip(freqs, spl) if lo <= f < hi]
        if vals:
            bands.append({
                "freq_hz": centre,
                "spl_db": round(sum(vals) / len(vals), 1),
            })
    return bands


def _downsample_group_delay(
    freqs: list[float], delay_ms: list[float],
) -> list[dict]:
    """Downsample group delay to 1/3-octave bands. Returns list of {freq_hz, delay_ms}."""
    bands = []
    for centre in _THIRD_OCTAVE_CENTRES:
        factor = 2 ** (1 / 6)
        lo = centre / factor
        hi = centre * factor
        vals = [d for f, d in zip(freqs, delay_ms) if lo <= f < hi]
        if vals:
            bands.append({
                "freq_hz": centre,
                "delay_ms": round(sum(vals) / len(vals), 1),
            })
    return bands


def _downsample_coherence(
    freqs: list[float], coherence: list[float],
) -> list[dict]:
    """Downsample coherence to 1/3-octave bands. Returns list of {freq_hz, coherence}."""
    bands = []
    for centre in _THIRD_OCTAVE_CENTRES:
        factor = 2 ** (1 / 6)
        lo = centre / factor
        hi = centre * factor
        vals = [c for f, c in zip(freqs, coherence) if lo <= f < hi]
        if vals:
            bands.append({
                "freq_hz": centre,
                "coherence": round(sum(vals) / len(vals), 3),
            })
    return bands


async def _tool_get_fr_summary(
    session_ids: list[int] | None = None, limit: int = 5,
) -> dict:
    """Return 1/3-octave downsampled FR summaries — small enough for tool results.

    If *session_ids* is given, return those specific sessions.
    Otherwise return the last *limit* sessions.
    """
    from .storage import SessionStore
    try:
        store = SessionStore()
        if session_ids:
            sessions = [store.get_session(sid) for sid in session_ids]
            sessions = [s for s in sessions if s is not None]
        else:
            sessions = store.list_sessions()[:limit]

        result = []
        for s in sessions:
            fr = s.start_fr
            if not fr or not fr.frequencies:
                result.append({
                    "id": s.id, "label": s.label, "timestamp": s.timestamp,
                    "bands": [], "peak_spl": None, "freq_at_peak": None,
                })
                continue
            bands = _downsample_to_third_octave(fr.frequencies, fr.spl)
            entry: dict = {
                "id": s.id,
                "label": s.label,
                "timestamp": s.timestamp,
                "bands": bands,
                "peak_spl": round(fr.peak_spl, 1),
                "freq_at_peak": round(fr.freq_at_peak, 1),
            }
            if s.metadata and "ir" in s.metadata:
                entry["ir_summary"] = s.metadata["ir"]
            result.append(entry)
        return _ok(sessions=result, count=len(result))
    except Exception as exc:
        return _err(f"storage error: {exc}")


async def _tool_apply_eq(
    filters: list[dict],
    output_index: int | None = None,
    simulation_verified: bool = False,
) -> dict:
    """Validate and apply EQ filters to DSP output(s).

    If *output_index* is given, writes only to that single output (per-sub EQ).
    Otherwise writes to all configured sub outputs (broadcast mode).

    Returns {ok: True} or {ok: False, error: "SafetyValidator: ..."} on rejection.
    """
    try:
        preset = await _dsp.current_preset()  # type: ignore[union-attr]
        await _dsp.apply_eq(preset, filters, output_index=output_index, simulation_verified=simulation_verified)  # type: ignore[union-attr]
        # Persist to SQLite so the web dashboard can show active DSP state
        if output_index is not None:
            _persist_dsp_state(f"output_eq_{output_index}", {"filters": filters, "preset": preset})
        else:
            # Broadcast mode — persist for all sub outputs
            cfg = _config()
            for slot in cfg.minidsp.get("output_slots", []):
                if slot.get("type") == "sub":
                    _persist_dsp_state(f"output_eq_{slot['index']}", {"filters": filters, "preset": preset})
        return _ok(filters_applied=len(filters), preset=preset,
                    output_index=output_index)
    except DriverError as exc:
        return _err(str(exc))


async def _tool_apply_input_eq(
    filters: list[dict],
    target_curve: dict | None = None,
    simulation_verified: bool = False,
) -> dict:
    """Validate and apply EQ filters to the DSP input channel.

    Applies shared EQ (e.g. Harman target curve) to the active input,
    affecting all outputs equally. Uses the same SafetyValidator as output EQ.

    Optional *target_curve* persists the optimization target for dashboard display:
      {"type": "harman", "reference_spl": 72.5, "band": [20, 200],
       "points": [{"freq": 20, "spl": 78.5}, ...]}

    Returns {ok: True} or {ok: False, error: "SafetyValidator: ..."} on rejection.
    """
    try:
        preset = await _dsp.current_preset()  # type: ignore[union-attr]
        await _dsp.apply_input_eq(preset, filters, simulation_verified=simulation_verified)  # type: ignore[union-attr]
        _persist_dsp_state("input_eq", {"filters": filters, "preset": preset})
        if target_curve:
            _persist_dsp_state("target_curve", target_curve)
        return _ok(filters_applied=len(filters), preset=preset, target="input")
    except DriverError as exc:
        return _err(str(exc))


async def _tool_compute_deviation(
    session_id: int,
    target_curve: dict,
    null_threshold_db: float = 15.0,
    port_rolloff_hz: float = 28.0,
    resolution: str = "sixth_octave",
    convergence_threshold: float = 1.5,
) -> dict:
    """Compute RMS deviation of a measurement against a target curve.

    Automatically detects and excludes:
    - Null zones: frequencies where measured SPL is > null_threshold_db below the band average
    - Below-port rolloff: frequencies below port_rolloff_hz where the sub physically can't produce output

    Resolution controls the summary band density:
    - "third_octave": ~6 bands in 25-80 Hz (original, coarse)
    - "sixth_octave": ~12 bands (default — good balance of detail and readability)
    - "twelfth_octave": ~24 bands (full detail for filter design)

    Returns RMS deviation, per-band errors, convergence status, and excluded zones.
    """
    from .storage import SessionStore
    import math

    try:
        store = SessionStore()
        sessions = store.list_sessions()
        session = next((s for s in sessions if s.id == session_id), None)
        if session is None:
            return _err(f"session {session_id} not found")

        fr = session.start_fr
        if not fr or not fr.frequencies:
            return _err(f"session {session_id} has no frequency response data")

        # Parse target curve points
        points = target_curve.get("points", [])
        if not points:
            return _err("target_curve must include 'points' array with [{freq, spl}]")

        band = target_curve.get("band", [20, 80])
        band_lo, band_hi = float(band[0]), float(band[1])

        # Build target interpolation function (log-frequency, linear dB)
        target_freqs = sorted(points, key=lambda p: p["freq"])

        def interpolate_target(freq_hz: float) -> float | None:
            """Interpolate target SPL at a given frequency. Returns None if outside target range."""
            if freq_hz < target_freqs[0]["freq"] or freq_hz > target_freqs[-1]["freq"]:
                return None
            # Find surrounding points
            for i in range(len(target_freqs) - 1):
                f0, s0 = target_freqs[i]["freq"], target_freqs[i]["spl"]
                f1, s1 = target_freqs[i + 1]["freq"], target_freqs[i + 1]["spl"]
                if f0 <= freq_hz <= f1:
                    # Log-frequency interpolation
                    if f1 == f0:
                        return s0
                    t = math.log(freq_hz / f0) / math.log(f1 / f0)
                    return s0 + t * (s1 - s0)
            return target_freqs[-1]["spl"]

        # Filter FR to band range
        pairs = [(f, s) for f, s in zip(fr.frequencies, fr.spl) if band_lo <= f <= band_hi]
        if not pairs:
            return _err(f"no FR data in band {band_lo}-{band_hi} Hz")

        # Compute band average (for null detection)
        measured_spls = [s for _, s in pairs]
        band_avg = sum(measured_spls) / len(measured_spls)

        # Classify each frequency point
        per_band_errors = []
        included_errors = []
        excluded_null = []
        excluded_rolloff = []

        for freq, measured in pairs:
            target = interpolate_target(freq)
            if target is None:
                continue

            error = measured - target  # positive = above target, negative = below

            # Check exclusions
            is_null = measured < (band_avg - null_threshold_db)
            is_rolloff = freq < port_rolloff_hz

            entry = {
                "freq_hz": round(freq, 1),
                "measured_db": round(measured, 1),
                "target_db": round(target, 1),
                "error_db": round(error, 1),
                "excluded": is_null or is_rolloff,
            }

            if is_null:
                excluded_null.append(round(freq, 1))
                entry["exclude_reason"] = "null"
            elif is_rolloff:
                excluded_rolloff.append(round(freq, 1))
                entry["exclude_reason"] = "rolloff"
            else:
                included_errors.append(error)

            per_band_errors.append(entry)

        if not included_errors:
            return _err("no usable frequency points after excluding nulls and rolloff")

        # Compute RMS deviation
        rms = math.sqrt(sum(e ** 2 for e in included_errors) / len(included_errors))
        mean_error = sum(included_errors) / len(included_errors)
        max_error = max(included_errors, key=abs)
        converged = rms < convergence_threshold

        # Generate band centres based on resolution
        band_centres = _generate_band_centres(resolution, band_lo, band_hi)

        # Downsample per_band_errors to summary at chosen resolution
        summary = []
        for centre in band_centres:
            # Band edges: centre / 2^(1/(2*N)) to centre * 2^(1/(2*N))
            # where N is bands per octave (3 for third, 6 for sixth, 12 for twelfth)
            bpo = {"third_octave": 3, "sixth_octave": 6, "twelfth_octave": 12}.get(resolution, 6)
            factor = 2 ** (1 / (2 * bpo))
            lo = centre / factor
            hi = centre * factor
            band_entries = [e for e in per_band_errors if lo <= e["freq_hz"] < hi and not e.get("excluded")]
            if band_entries:
                avg_error = sum(e["error_db"] for e in band_entries) / len(band_entries)
                avg_measured = sum(e["measured_db"] for e in band_entries) / len(band_entries)
                target_at_centre = interpolate_target(centre)
                summary.append({
                    "freq_hz": round(centre, 1),
                    "measured_db": round(avg_measured, 1),
                    "target_db": round(target_at_centre, 1) if target_at_centre else None,
                    "error_db": round(avg_error, 1),
                })

        # Identify null zone ranges (contiguous excluded frequencies)
        null_zones = []
        if excluded_null:
            zone_start = excluded_null[0]
            zone_end = excluded_null[0]
            for f in excluded_null[1:]:
                if f - zone_end < 3.0:  # Within 3 Hz = same zone
                    zone_end = f
                else:
                    null_zones.append({"lo_hz": zone_start, "hi_hz": zone_end})
                    zone_start = zone_end = f
            null_zones.append({"lo_hz": zone_start, "hi_hz": zone_end})

        return _ok(
            session_id=session_id,
            rms_db=round(rms, 2),
            converged=converged,
            convergence_threshold=convergence_threshold,
            resolution=resolution,
            mean_error_db=round(mean_error, 2),
            max_error_db=round(max_error, 1),
            included_points=len(included_errors),
            excluded_null_points=len(excluded_null),
            excluded_rolloff_points=len(excluded_rolloff),
            null_zones=null_zones,
            summary=summary,
        )
    except Exception as exc:
        return _err(f"compute_deviation failed: {exc}")


async def _tool_anchor_target(
    session_id: int,
    target_offsets: list[dict],
    band: list[float] | None = None,
    max_boost_db: float = 6.0,
    null_threshold_db: float = 15.0,
    port_rolloff_hz: float = 28.0,
) -> dict:
    """Compute the optimal reference SPL for a target curve against a baseline measurement.

    Given a set of target offsets (freq_hz, offset_db relative to reference) and a
    measured frequency response, find the reference_spl that minimizes the total
    correction needed while keeping max boost within the safety limit.

    Algorithm:
    1. Interpolate target offsets at each measurement frequency
    2. Exclude nulls (> null_threshold_db below band average) and below-port rolloff
    3. At each valid frequency: headroom = measured_spl - offset
       (the reference_spl that would require zero correction at this point)
    4. reference_spl = min(headroom) + max_boost_db
       (places the reference so the hardest-to-reach frequency needs exactly max_boost_db)
    5. Return the anchored target curve with absolute SPL values

    Returns anchored_points, reference_spl, max_boost_needed, error summary at key
    frequencies, and excluded zones.
    """
    from .storage import SessionStore
    import math

    try:
        store = SessionStore()
        session = store.get_session(session_id)
        if session is None:
            return _err(f"session {session_id} not found")

        fr = session.start_fr
        if not fr or not fr.frequencies:
            return _err(f"session {session_id} has no frequency response data")

        if not target_offsets:
            return _err("target_offsets must be a non-empty list of {freq_hz, offset_db}")

        # Parse and sort target offset points
        offsets_sorted = sorted(target_offsets, key=lambda p: p["freq_hz"])
        off_freqs = [p["freq_hz"] for p in offsets_sorted]
        off_vals = [p["offset_db"] for p in offsets_sorted]

        # Determine band range
        if band:
            band_lo, band_hi = float(band[0]), float(band[1])
        else:
            band_lo, band_hi = off_freqs[0], off_freqs[-1]

        def interpolate_offset(freq_hz: float) -> float | None:
            """Log-frequency interpolation of target offset."""
            if freq_hz < off_freqs[0] or freq_hz > off_freqs[-1]:
                return None
            for i in range(len(off_freqs) - 1):
                f0, v0 = off_freqs[i], off_vals[i]
                f1, v1 = off_freqs[i + 1], off_vals[i + 1]
                if f0 <= freq_hz <= f1:
                    if f1 == f0:
                        return v0
                    t = math.log(freq_hz / f0) / math.log(f1 / f0)
                    return v0 + t * (v1 - v0)
            return off_vals[-1]

        # Filter FR to band range
        pairs = [
            (f, s) for f, s in zip(fr.frequencies, fr.spl) if band_lo <= f <= band_hi
        ]
        if not pairs:
            return _err(f"no FR data in band {band_lo}-{band_hi} Hz")

        # Band average for null detection
        measured_spls = [s for _, s in pairs]
        band_avg = sum(measured_spls) / len(measured_spls)

        # Compute headroom at each valid frequency
        headroom_values: list[tuple[float, float, float, float]] = (
            []
        )  # (freq, measured, offset, headroom)
        excluded_null: list[float] = []
        excluded_rolloff: list[float] = []

        for freq, measured in pairs:
            offset = interpolate_offset(freq)
            if offset is None:
                continue

            is_null = measured < (band_avg - null_threshold_db)
            is_rolloff = freq < port_rolloff_hz

            if is_null:
                excluded_null.append(round(freq, 1))
            elif is_rolloff:
                excluded_rolloff.append(round(freq, 1))
            else:
                # headroom = measured - offset
                # This is the reference_spl at which this frequency needs 0 correction
                headroom = measured - offset
                headroom_values.append((freq, measured, offset, headroom))

        if not headroom_values:
            return _err("no valid frequency points after excluding nulls and rolloff")

        # Reference = min(headroom) + max_boost
        min_headroom = min(h for _, _, _, h in headroom_values)
        limiting_freq = next(
            freq for freq, _, _, h in headroom_values if h == min_headroom
        )
        reference_spl = min_headroom + max_boost_db

        # Build anchored target curve (absolute SPL at each offset point)
        anchored_points = []
        for p in offsets_sorted:
            anchored_points.append(
                {
                    "freq": p["freq_hz"],
                    "spl": round(reference_spl + p["offset_db"], 2),
                }
            )

        # Build error summary at each target offset frequency
        error_summary = []
        for p in offsets_sorted:
            target_freq = p["freq_hz"]
            target_spl = reference_spl + p["offset_db"]
            # Find nearest measured point
            closest = min(pairs, key=lambda pair: abs(pair[0] - target_freq))
            meas_freq, meas_spl = closest
            error = target_spl - meas_spl  # positive = boost needed, negative = cut needed
            error_summary.append(
                {
                    "freq_hz": round(target_freq, 1),
                    "target_spl": round(target_spl, 1),
                    "measured_spl": round(meas_spl, 1),
                    "error_db": round(error, 1),
                    "action": "boost" if error > 0.5 else ("cut" if error < -0.5 else "ok"),
                }
            )

        # Null zone ranges
        null_zones: list[dict] = []
        if excluded_null:
            zone_start = excluded_null[0]
            zone_end = excluded_null[0]
            for f in excluded_null[1:]:
                if f - zone_end < 3.0:
                    zone_end = f
                else:
                    null_zones.append({"lo_hz": zone_start, "hi_hz": zone_end})
                    zone_start = zone_end = f
            null_zones.append({"lo_hz": zone_start, "hi_hz": zone_end})

        return _ok(
            session_id=session_id,
            reference_spl=round(reference_spl, 2),
            max_boost_db=round(max_boost_db, 1),
            limiting_freq_hz=round(limiting_freq, 1),
            band_avg_spl=round(band_avg, 1),
            anchored_points=anchored_points,
            error_summary=error_summary,
            excluded_null_points=len(excluded_null),
            excluded_rolloff_points=len(excluded_rolloff),
            null_zones=null_zones,
            valid_points=len(headroom_values),
        )
    except Exception as exc:
        return _err(f"anchor_target failed: {exc}")


def _generate_band_centres(
    resolution: str, lo_hz: float, hi_hz: float
) -> list[float]:
    """Generate fractional-octave band centres between lo_hz and hi_hz.

    Supports: "third_octave" (ISO 266), "sixth_octave", "twelfth_octave".
    """
    import math
    if resolution == "third_octave":
        return [c for c in _THIRD_OCTAVE_CENTRES if lo_hz <= c <= hi_hz]

    # Generate from base frequency 1000 Hz using ISO formula: f = 1000 * 2^(k/N)
    bpo = 6 if resolution == "sixth_octave" else 12
    centres = []
    # k ranges to cover 20-200 Hz: 1000 * 2^(k/N) → k = N * log2(f/1000)
    k_min = int(math.floor(bpo * math.log2(lo_hz / 1000)))
    k_max = int(math.ceil(bpo * math.log2(hi_hz / 1000)))
    for k in range(k_min, k_max + 1):
        c = 1000.0 * (2.0 ** (k / bpo))
        if lo_hz <= c <= hi_hz:
            centres.append(round(c, 2))
    return centres


async def _tool_compare_sessions(
    session_a: int,
    session_b: int,
    min_hz: float = 20.0,
    max_hz: float = 120.0,
) -> dict:
    """Compare frequency responses between two sessions.

    Returns per-1/3-octave-band deltas (B minus A) and overall statistics.
    Useful for verifying that EQ changes had the intended effect.
    """
    from .storage import SessionStore

    try:
        store = SessionStore()
        sessions = store.list_sessions()
        sa = next((s for s in sessions if s.id == session_a), None)
        sb = next((s for s in sessions if s.id == session_b), None)
        if sa is None:
            return _err(f"session {session_a} not found")
        if sb is None:
            return _err(f"session {session_b} not found")

        for s_check, s_id in [(sa, session_a), (sb, session_b)]:
            if not s_check.start_fr or not s_check.start_fr.frequencies:
                return _err(f"session {s_id} has no frequency response data")

        # Downsample both to 1/3-octave bands for comparison
        bands_a = _downsample_to_third_octave(sa.start_fr.frequencies, sa.start_fr.spl)
        bands_b = _downsample_to_third_octave(sb.start_fr.frequencies, sb.start_fr.spl)

        # Build lookup by freq
        a_by_freq = {b["freq_hz"]: b["spl_db"] for b in bands_a}
        b_by_freq = {b["freq_hz"]: b["spl_db"] for b in bands_b}

        deltas = []
        all_freqs = sorted(set(a_by_freq.keys()) | set(b_by_freq.keys()))
        for freq in all_freqs:
            if freq < min_hz or freq > max_hz:
                continue
            spl_a = a_by_freq.get(freq)
            spl_b = b_by_freq.get(freq)
            if spl_a is not None and spl_b is not None:
                delta = round(spl_b - spl_a, 1)
                deltas.append({
                    "freq_hz": freq,
                    "session_a_db": spl_a,
                    "session_b_db": spl_b,
                    "delta_db": delta,
                })

        delta_values = [d["delta_db"] for d in deltas]
        if delta_values:
            import math
            avg_delta = round(sum(delta_values) / len(delta_values), 1)
            max_delta = round(max(delta_values, key=abs), 1)
            rms_delta = round(math.sqrt(sum(d ** 2 for d in delta_values) / len(delta_values)), 2)
        else:
            avg_delta = max_delta = rms_delta = 0.0

        return _ok(
            session_a={"id": session_a, "label": sa.label},
            session_b={"id": session_b, "label": sb.label},
            bands=deltas,
            avg_delta_db=avg_delta,
            max_delta_db=max_delta,
            rms_delta_db=rms_delta,
        )
    except Exception as exc:
        return _err(f"compare_sessions failed: {exc}")


async def _tool_simulate_eq(
    session_id: int,
    filters: list[dict],
    min_hz: float = 20.0,
    max_hz: float = 120.0,
) -> dict:
    """Predict FR after applying proposed PEQ filters to a measurement.

    Pure simulation — no hardware writes. The LLM designs filters, this tool
    shows what the result would look like so the LLM can iterate before applying.
    """
    from .storage import SessionStore
    import math

    try:
        store = SessionStore()
        sessions = store.list_sessions()
        session = next((s for s in sessions if s.id == session_id), None)
        if session is None:
            return _err(f"session {session_id} not found")

        fr = session.start_fr
        if not fr or not fr.frequencies:
            return _err(f"session {session_id} has no frequency response data")

        def hpf_response(fc: float, order: int, freq: float) -> float:
            if fc <= 0 or freq <= 0:
                return 0.0
            ratio = fc / freq
            return -10.0 * order * math.log10(1.0 + ratio ** 2)

        # Compute filter effect at each frequency
        predicted_fr: list[dict] = []
        for f, measured in zip(fr.frequencies, fr.spl):
            if f < min_hz or f > max_hz:
                continue
            total_correction = 0.0
            for filt in filters:
                ftype = filt.get("type", "peaking")
                fc = float(filt.get("freq", 0))
                gain = float(filt.get("gain_db", 0))
                q = float(filt.get("q", 1.0))
                if ftype == "peaking":
                    total_correction += _biquad_response(f, "peaking", fc, gain, q)
                elif ftype == "hpf":
                    # Skip: the measurement already includes whatever HPF was
                    # active on the miniDSP during the sweep.  Adding the HPF
                    # response here would double-apply the attenuation, causing
                    # predicted bass levels to be far too low.
                    pass
                elif ftype in ("low_shelf", "high_shelf"):
                    total_correction += _biquad_response(f, ftype, fc, gain, q)
            predicted_fr.append({
                "freq_hz": round(f, 1),
                "original_db": round(measured, 1),
                "predicted_db": round(measured + total_correction, 1),
                "correction_db": round(total_correction, 1),
            })

        # Compute compact FR string for the prediction
        compact = ",".join(f"{p['freq_hz']}:{p['predicted_db']}" for p in predicted_fr)

        return _ok(
            session_id=session_id,
            num_filters=len(filters),
            point_count=len(predicted_fr),
            predicted_fr=compact,
        )
    except Exception as exc:
        return _err(f"simulate_eq failed: {exc}")


# ── LLM filter-design math tools ─────────────────────────────────────────────
# These tools provide DATA and SIMULATION — the LLM provides JUDGMENT.
# They help the LLM iterate faster by exposing biquad math, per-filter
# contribution analysis, sensitivity gradients, and optimal gain interpolation.


async def _tool_evaluate_transfer_function(
    filters: list[dict],
    query_freqs: list[float],
) -> dict:
    """Evaluate combined PEQ transfer function at specific frequencies.

    Pure math — no measurement data needed. Returns the combined magnitude
    in dB at each query frequency, plus per-filter breakdown. Use this to
    quickly check filter interaction before running a full simulation.
    """
    try:
        results = []
        for freq in query_freqs:
            total_db = 0.0
            per_filter = []
            for filt in filters:
                ftype = filt.get("type", "peaking")
                fc = float(filt.get("freq", 0))
                gain = float(filt.get("gain_db", 0))
                q = float(filt.get("q", 1.0))
                if ftype == "hpf":
                    contribution = 0.0  # HPF already in measurement
                elif ftype in ("peaking", "low_shelf", "high_shelf"):
                    contribution = _biquad_response(freq, ftype, fc, gain, q)
                else:
                    contribution = 0.0
                total_db += contribution
                per_filter.append({
                    "type": ftype,
                    "freq": fc,
                    "gain_db": gain,
                    "q": q,
                    "contribution_db": round(contribution, 2),
                })
            results.append({
                "freq_hz": round(freq, 1),
                "total_db": round(total_db, 2),
                "per_filter": per_filter,
            })
        return _ok(
            num_filters=len(filters),
            num_freqs=len(query_freqs),
            results=results,
        )
    except Exception as exc:
        return _err(f"evaluate_transfer_function failed: {exc}")


async def _tool_per_filter_contribution(
    filters: list[dict],
    session_id: int,
    query_freqs: list[float] | None = None,
) -> dict:
    """Show each filter's individual contribution at specific frequencies.

    Loads a measurement session and shows how each filter affects the
    predicted SPL at each query frequency. The LLM can see which filter
    is responsible for an over-cut and adjust surgically.

    If query_freqs is omitted, uses sixth-octave centres in 20-120 Hz.
    """
    from .storage import SessionStore

    try:
        store = SessionStore()
        sessions = store.list_sessions()
        session = next((s for s in sessions if s.id == session_id), None)
        if session is None:
            return _err(f"session {session_id} not found")

        fr = session.start_fr
        if not fr or not fr.frequencies:
            return _err(f"session {session_id} has no frequency response data")

        if query_freqs is None:
            query_freqs = _generate_band_centres("sixth_octave", 20.0, 120.0)

        # Find closest measured SPL for each query frequency
        import bisect
        results = []
        for qf in query_freqs:
            # Binary search for closest frequency in measured data
            idx = bisect.bisect_left(fr.frequencies, qf)
            if idx >= len(fr.frequencies):
                idx = len(fr.frequencies) - 1
            elif idx > 0 and abs(fr.frequencies[idx - 1] - qf) < abs(fr.frequencies[idx] - qf):
                idx -= 1
            baseline_db = fr.spl[idx]

            total_correction = 0.0
            per_filter = []
            for filt in filters:
                ftype = filt.get("type", "peaking")
                fc = float(filt.get("freq", 0))
                gain = float(filt.get("gain_db", 0))
                q = float(filt.get("q", 1.0))
                if ftype == "hpf":
                    c = 0.0
                elif ftype in ("peaking", "low_shelf", "high_shelf"):
                    c = _biquad_response(qf, ftype, fc, gain, q)
                else:
                    c = 0.0
                total_correction += c
                per_filter.append({
                    "type": ftype,
                    "freq": fc,
                    "gain_db": gain,
                    "q": q,
                    "contribution_db": round(c, 2),
                })

            results.append({
                "freq_hz": round(qf, 1),
                "baseline_db": round(baseline_db, 1),
                "total_correction_db": round(total_correction, 2),
                "predicted_db": round(baseline_db + total_correction, 1),
                "per_filter": per_filter,
            })

        return _ok(
            session_id=session_id,
            num_filters=len(filters),
            num_freqs=len(results),
            results=results,
        )
    except Exception as exc:
        return _err(f"per_filter_contribution failed: {exc}")


async def _tool_interpolate_optimal_gain(
    freq: float,
    q: float,
    filter_type: str,
    measured_errors: list[dict],
) -> dict:
    """Interpolate the optimal gain for a filter from prior iteration data.

    Takes a list of {gain_applied, error_measured} pairs from prior iterations
    and fits a line to find where error = 0. Returns the predicted optimal gain.

    With 2 points: linear interpolation (exact zero-crossing).
    With 3+ points: least-squares linear fit (handles nonlinearity better).
    """
    import math

    try:
        if len(measured_errors) < 2:
            return _err("need at least 2 data points (gain_applied, error_measured)")

        gains = [float(e["gain_applied"]) for e in measured_errors]
        errors = [float(e["error_measured"]) for e in measured_errors]
        n = len(gains)

        # Linear least-squares fit: error = a * gain + b
        sum_g = sum(gains)
        sum_e = sum(errors)
        sum_ge = sum(g * e for g, e in zip(gains, errors))
        sum_g2 = sum(g * g for g in gains)

        denom = n * sum_g2 - sum_g * sum_g
        if abs(denom) < 1e-12:
            return _err("degenerate data — all gain values are identical")

        a = (n * sum_ge - sum_g * sum_e) / denom  # slope
        b = (sum_e * sum_g2 - sum_g * sum_ge) / denom  # intercept

        # Optimal gain: where error = 0 → a * gain + b = 0 → gain = -b/a
        if abs(a) < 1e-12:
            return _err("flat relationship — gain changes don't affect error at this frequency")

        optimal_gain = -b / a

        # Residual standard error for confidence
        residuals = [e - (a * g + b) for g, e in zip(gains, errors)]
        rss = sum(r * r for r in residuals)
        residual_se = math.sqrt(rss / max(n - 2, 1))

        # Predicted error at optimal gain (should be ~0)
        predicted_error = a * optimal_gain + b

        return _ok(
            optimal_gain_db=round(optimal_gain, 2),
            predicted_error_db=round(predicted_error, 3),
            slope=round(a, 4),
            intercept=round(b, 4),
            residual_se=round(residual_se, 3),
            n_points=n,
            filter_info={"freq": freq, "q": q, "type": filter_type},
        )
    except Exception as exc:
        return _err(f"interpolate_optimal_gain failed: {exc}")


async def _tool_sensitivity_analysis(
    filters: list[dict],
    session_id: int,
    target_curve: dict,
    perturbation_db: float = 0.5,
) -> dict:
    """Compute sensitivity of RMS to each filter parameter.

    Perturbs each filter's gain, frequency, and Q, computes the
    resulting RMS change, and returns partial derivatives. Tells the
    LLM which parameters matter most for convergence.
    """
    from .storage import SessionStore
    import math

    try:
        store = SessionStore()
        sessions = store.list_sessions()
        session = next((s for s in sessions if s.id == session_id), None)
        if session is None:
            return _err(f"session {session_id} not found")

        fr = session.start_fr
        if not fr or not fr.frequencies:
            return _err(f"session {session_id} has no FR data")

        points = target_curve.get("points", [])
        if not points:
            return _err("target_curve must include 'points'")
        band = target_curve.get("band", [20, 120])
        band_lo, band_hi = float(band[0]), float(band[1])

        target_sorted = sorted(points, key=lambda p: p["freq"])

        def interp_target(freq_hz):
            if freq_hz < target_sorted[0]["freq"] or freq_hz > target_sorted[-1]["freq"]:
                return None
            for i in range(len(target_sorted) - 1):
                f0, s0 = target_sorted[i]["freq"], target_sorted[i]["spl"]
                f1, s1 = target_sorted[i + 1]["freq"], target_sorted[i + 1]["spl"]
                if f0 <= freq_hz <= f1:
                    if f1 == f0:
                        return s0
                    t = math.log(freq_hz / f0) / math.log(f1 / f0)
                    return s0 + t * (s1 - s0)
            return target_sorted[-1]["spl"]

        # Pairs within band
        pairs = [(f, s) for f, s in zip(fr.frequencies, fr.spl) if band_lo <= f <= band_hi]
        if not pairs:
            return _err("no FR data in band")

        def compute_rms(filt_list):
            errors = []
            for f, measured in pairs:
                target = interp_target(f)
                if target is None:
                    continue
                correction = 0.0
                for filt in filt_list:
                    ftype = filt.get("type", "peaking")
                    fc = float(filt.get("freq", 0))
                    g = float(filt.get("gain_db", 0))
                    q = float(filt.get("q", 1.0))
                    if ftype in ("peaking", "low_shelf", "high_shelf"):
                        correction += _biquad_response(f, ftype, fc, g, q)
                predicted = measured + correction
                errors.append(predicted - target)
            if not errors:
                return 999.0
            return math.sqrt(sum(e ** 2 for e in errors) / len(errors))

        baseline_rms = compute_rms(filters)

        sensitivities = []
        for i, filt in enumerate(filters):
            ftype = filt.get("type", "peaking")
            if ftype == "hpf":
                sensitivities.append({
                    "index": i,
                    "type": ftype,
                    "freq": float(filt.get("freq", 0)),
                    "skipped": True,
                    "reason": "HPF not perturbed",
                })
                continue

            fc = float(filt.get("freq", 0))
            gain = float(filt.get("gain_db", 0))
            q = float(filt.get("q", 1.0))

            # Perturb gain
            f_plus = [dict(f) for f in filters]
            f_plus[i] = {**f_plus[i], "gain_db": gain + perturbation_db}
            f_minus = [dict(f) for f in filters]
            f_minus[i] = {**f_minus[i], "gain_db": gain - perturbation_db}
            d_rms_d_gain = (compute_rms(f_plus) - compute_rms(f_minus)) / (2 * perturbation_db)

            # Perturb frequency (by 1 Hz)
            freq_delta = 1.0
            f_plus = [dict(f) for f in filters]
            f_plus[i] = {**f_plus[i], "freq": fc + freq_delta}
            f_minus = [dict(f) for f in filters]
            f_minus[i] = {**f_minus[i], "freq": max(1.0, fc - freq_delta)}
            d_rms_d_freq = (compute_rms(f_plus) - compute_rms(f_minus)) / (2 * freq_delta)

            # Perturb Q (by 0.1)
            q_delta = 0.1
            f_plus = [dict(f) for f in filters]
            f_plus[i] = {**f_plus[i], "q": q + q_delta}
            f_minus = [dict(f) for f in filters]
            f_minus[i] = {**f_minus[i], "q": max(0.1, q - q_delta)}
            d_rms_d_q = (compute_rms(f_plus) - compute_rms(f_minus)) / (2 * q_delta)

            sensitivities.append({
                "index": i,
                "type": ftype,
                "freq": fc,
                "gain_db": gain,
                "q": q,
                "d_rms_d_gain": round(d_rms_d_gain, 4),
                "d_rms_d_freq": round(d_rms_d_freq, 4),
                "d_rms_d_q": round(d_rms_d_q, 4),
            })

        return _ok(
            session_id=session_id,
            baseline_rms=round(baseline_rms, 3),
            perturbation_db=perturbation_db,
            sensitivities=sensitivities,
        )
    except Exception as exc:
        return _err(f"sensitivity_analysis failed: {exc}")


async def _tool_fit_correction_filter(
    session_id: int,
    target_curve: dict,
    freq_range: list[float],
    constraints: dict | None = None,
) -> dict:
    """Find the optimal single PEQ filter to minimize RMS in a frequency range.

    The LLM decides WHICH region to correct and any constraints. This tool
    does the numerical optimization to find the best (freq, gain, Q).

    constraints: {max_boost_db, min_q, max_q, filter_type}
    """
    from .storage import SessionStore
    import math

    try:
        store = SessionStore()
        sessions = store.list_sessions()
        session = next((s for s in sessions if s.id == session_id), None)
        if session is None:
            return _err(f"session {session_id} not found")

        fr = session.start_fr
        if not fr or not fr.frequencies:
            return _err(f"session {session_id} has no FR data")

        points = target_curve.get("points", [])
        if not points:
            return _err("target_curve must include 'points'")

        range_lo, range_hi = float(freq_range[0]), float(freq_range[1])
        target_sorted = sorted(points, key=lambda p: p["freq"])

        def interp_target(freq_hz):
            if freq_hz < target_sorted[0]["freq"] or freq_hz > target_sorted[-1]["freq"]:
                return None
            for i in range(len(target_sorted) - 1):
                f0, s0 = target_sorted[i]["freq"], target_sorted[i]["spl"]
                f1, s1 = target_sorted[i + 1]["freq"], target_sorted[i + 1]["spl"]
                if f0 <= freq_hz <= f1:
                    if f1 == f0:
                        return s0
                    t = math.log(freq_hz / f0) / math.log(f1 / f0)
                    return s0 + t * (s1 - s0)
            return target_sorted[-1]["spl"]

        # Build error pairs in the target range
        error_pairs = []
        for f, measured in zip(fr.frequencies, fr.spl):
            if f < range_lo or f > range_hi:
                continue
            target = interp_target(f)
            if target is None:
                continue
            error_pairs.append((f, measured, target, measured - target))

        if not error_pairs:
            return _err(f"no data in range {range_lo}-{range_hi} Hz")

        # RMS before correction
        rms_before = math.sqrt(sum(e[3] ** 2 for e in error_pairs) / len(error_pairs))

        # Constraints
        c = constraints or {}
        max_boost = float(c.get("max_boost_db", 6.0))
        min_q = float(c.get("min_q", 0.5))
        max_q = float(c.get("max_q", 10.0))
        ftype = c.get("filter_type", "peaking")

        # Grid search + refinement (scipy optional, grid is robust)
        best_rms = rms_before
        best_params = None

        # Grid: freq from range_lo to range_hi, gain from -15 to max_boost, Q from min_q to max_q
        freq_steps = 20
        gain_steps = 15
        q_steps = 10

        for fi in range(freq_steps + 1):
            fc = range_lo * (range_hi / range_lo) ** (fi / freq_steps)  # log-spaced
            for gi in range(gain_steps + 1):
                gain = -15.0 + (15.0 + max_boost) * gi / gain_steps
                for qi in range(q_steps + 1):
                    q = min_q + (max_q - min_q) * qi / q_steps

                    # Compute RMS with this filter
                    errors = []
                    for f, measured, target, _ in error_pairs:
                        correction = _biquad_response(f, ftype, fc, gain, q)
                        errors.append(measured + correction - target)
                    rms = math.sqrt(sum(e ** 2 for e in errors) / len(errors))

                    if rms < best_rms:
                        best_rms = rms
                        best_params = {"freq": round(fc, 1), "gain_db": round(gain, 1), "q": round(q, 1)}

        if best_params is None:
            return _ok(
                session_id=session_id,
                message="no filter improves the response in this range",
                rms_before=round(rms_before, 3),
            )

        # Refine with finer grid around best params
        fc0, g0, q0 = best_params["freq"], best_params["gain_db"], best_params["q"]
        freq_span = (range_hi / range_lo) ** (1.0 / freq_steps) * fc0 - fc0
        gain_span = (15.0 + max_boost) / gain_steps
        q_span = (max_q - min_q) / q_steps

        for fi in range(11):
            fc = fc0 + freq_span * (fi - 5) / 5
            if fc < range_lo or fc > range_hi:
                continue
            for gi in range(11):
                gain = g0 + gain_span * (gi - 5) / 5
                if gain > max_boost:
                    continue
                for qi in range(11):
                    q = q0 + q_span * (qi - 5) / 5
                    if q < min_q or q > max_q:
                        continue
                    errors = []
                    for f, measured, target, _ in error_pairs:
                        correction = _biquad_response(f, ftype, fc, gain, q)
                        errors.append(measured + correction - target)
                    rms = math.sqrt(sum(e ** 2 for e in errors) / len(errors))
                    if rms < best_rms:
                        best_rms = rms
                        best_params = {"freq": round(fc, 1), "gain_db": round(gain, 1), "q": round(q, 1)}

        return _ok(
            session_id=session_id,
            freq_range=[range_lo, range_hi],
            rms_before=round(rms_before, 3),
            rms_after=round(best_rms, 3),
            rms_improvement=round(rms_before - best_rms, 3),
            best_filter={
                "type": ftype,
                **best_params,
            },
        )
    except Exception as exc:
        return _err(f"fit_correction_filter failed: {exc}")


async def _tool_predict_rms(
    filters: list[dict],
    session_id: int,
    target_curve: dict,
    null_threshold_db: float = 15.0,
    port_rolloff_hz: float = 28.0,
    convergence_threshold: float = 1.5,
) -> dict:
    """Predict RMS deviation after applying filters — simulate_eq + compute_deviation in one call.

    Combines filter simulation with target curve comparison. Returns predicted
    RMS, convergence status, and per-band errors without touching hardware.
    """
    from .storage import SessionStore
    import math

    try:
        store = SessionStore()
        sessions = store.list_sessions()
        session = next((s for s in sessions if s.id == session_id), None)
        if session is None:
            return _err(f"session {session_id} not found")

        fr = session.start_fr
        if not fr or not fr.frequencies:
            return _err(f"session {session_id} has no FR data")

        points = target_curve.get("points", [])
        if not points:
            return _err("target_curve must include 'points'")
        band = target_curve.get("band", [20, 120])
        band_lo, band_hi = float(band[0]), float(band[1])

        target_sorted = sorted(points, key=lambda p: p["freq"])

        def interp_target(freq_hz):
            if freq_hz < target_sorted[0]["freq"] or freq_hz > target_sorted[-1]["freq"]:
                return None
            for i in range(len(target_sorted) - 1):
                f0, s0 = target_sorted[i]["freq"], target_sorted[i]["spl"]
                f1, s1 = target_sorted[i + 1]["freq"], target_sorted[i + 1]["spl"]
                if f0 <= freq_hz <= f1:
                    if f1 == f0:
                        return s0
                    t = math.log(freq_hz / f0) / math.log(f1 / f0)
                    return s0 + t * (s1 - s0)
            return target_sorted[-1]["spl"]

        # Simulate EQ and compute deviation in one pass
        predicted_pairs = []
        for f, measured in zip(fr.frequencies, fr.spl):
            if f < band_lo or f > band_hi:
                continue
            correction = 0.0
            for filt in filters:
                ftype = filt.get("type", "peaking")
                fc = float(filt.get("freq", 0))
                gain = float(filt.get("gain_db", 0))
                q = float(filt.get("q", 1.0))
                if ftype in ("peaking", "low_shelf", "high_shelf"):
                    correction += _biquad_response(f, ftype, fc, gain, q)
            predicted_pairs.append((f, measured + correction))

        if not predicted_pairs:
            return _err("no data in band")

        # Compute band average for null detection
        pred_spls = [s for _, s in predicted_pairs]
        band_avg = sum(pred_spls) / len(pred_spls)

        # Classify and compute errors
        included_errors = []
        excluded_null = 0
        excluded_rolloff = 0

        for f, predicted in predicted_pairs:
            target = interp_target(f)
            if target is None:
                continue
            is_null = predicted < (band_avg - null_threshold_db)
            is_rolloff = f < port_rolloff_hz
            if is_null:
                excluded_null += 1
            elif is_rolloff:
                excluded_rolloff += 1
            else:
                included_errors.append(predicted - target)

        if not included_errors:
            return _err("no usable points after exclusions")

        rms = math.sqrt(sum(e ** 2 for e in included_errors) / len(included_errors))
        mean_error = sum(included_errors) / len(included_errors)
        converged = rms < convergence_threshold

        # Generate band summary
        band_centres = _generate_band_centres("sixth_octave", band_lo, band_hi)
        summary = []
        for centre in band_centres:
            factor = 2 ** (1 / 12)  # sixth-octave half-bandwidth
            lo = centre / factor
            hi = centre * factor
            band_entries = [(f, s) for f, s in predicted_pairs if lo <= f < hi]
            if band_entries:
                avg_pred = sum(s for _, s in band_entries) / len(band_entries)
                target_at = interp_target(centre)
                if target_at is not None:
                    summary.append({
                        "freq_hz": round(centre, 1),
                        "predicted_db": round(avg_pred, 1),
                        "target_db": round(target_at, 1),
                        "error_db": round(avg_pred - target_at, 1),
                    })

        return _ok(
            session_id=session_id,
            predicted_rms=round(rms, 2),
            converged=converged,
            convergence_threshold=convergence_threshold,
            mean_error_db=round(mean_error, 2),
            included_points=len(included_errors),
            excluded_null_points=excluded_null,
            excluded_rolloff_points=excluded_rolloff,
            summary=summary,
        )
    except Exception as exc:
        return _err(f"predict_rms failed: {exc}")


async def _tool_optimize_q(
    session_id: int,
    freq_hz: float,
    target_gain_db: float,
    band_hz: list[float] | None = None,
) -> dict:
    """Find the optimal Q for a peaking filter at a given frequency and gain.

    The LLM picks the frequency and gain direction (cut/boost). This tool
    numerically searches for the Q that minimizes residual error in the
    surrounding frequency band.
    """
    from .storage import SessionStore
    import math

    try:
        store = SessionStore()
        sessions = store.list_sessions()
        session = next((s for s in sessions if s.id == session_id), None)
        if session is None:
            return _err(f"session {session_id} not found")

        fr = session.start_fr
        if not fr or not fr.frequencies:
            return _err(f"session {session_id} has no frequency response data")

        # Default search band: +/- 1 octave around the target frequency
        if band_hz:
            band_lo, band_hi = float(band_hz[0]), float(band_hz[1])
        else:
            band_lo = freq_hz / 2.0
            band_hi = freq_hz * 2.0

        # Get measured data in the band
        band_data = [
            (f, s) for f, s in zip(fr.frequencies, fr.spl)
            if band_lo <= f <= band_hi
        ]
        if not band_data:
            return _err(f"no FR data in band {band_lo}-{band_hi} Hz")

        # Compute the "flat" target (average SPL in band) as reference
        avg_spl = sum(s for _, s in band_data) / len(band_data)

        # Search Q values from 0.5 to 10 in steps of 0.1
        best_q = 1.0
        best_rms = float("inf")
        q_candidates = [round(0.5 + i * 0.1, 1) for i in range(96)]  # 0.5 to 10.0

        for q in q_candidates:
            errors_sq = []
            for f, measured in band_data:
                correction = _biquad_response(f, "peaking", freq_hz, target_gain_db, q)
                corrected = measured + correction
                error = corrected - avg_spl
                errors_sq.append(error ** 2)
            rms = math.sqrt(sum(errors_sq) / len(errors_sq))
            if rms < best_rms:
                best_rms = rms
                best_q = q

        # Show what this filter does at the target frequency and band edges
        return _ok(
            freq_hz=freq_hz,
            gain_db=target_gain_db,
            optimal_q=best_q,
            predicted_rms_in_band=round(best_rms, 2),
            band_hz=[round(band_lo, 1), round(band_hi, 1)],
            effect_at_center_db=round(_biquad_response(freq_hz, "peaking", freq_hz, target_gain_db, best_q), 1),
            effect_at_band_lo_db=round(_biquad_response(band_lo, "peaking", freq_hz, target_gain_db, best_q), 1),
            effect_at_band_hi_db=round(_biquad_response(band_hi, "peaking", freq_hz, target_gain_db, best_q), 1),
        )
    except Exception as exc:
        return _err(f"optimize_q failed: {exc}")


def _classify_fixability(freq_hz: float, excess_gd_ms: float) -> tuple[str, bool]:
    """Classify a band as fixable / partial / geometry from excess group delay.

    The old fixed 5 ms threshold flagged essentially every room measurement
    as "not fixable" because modal ringing at sub frequencies comfortably
    exceeds 5 ms of excess group delay even when the underlying response is
    minimum-phase-correctable. Scale instead with the period of the band:

      fixable   — excess GD < max(10 ms, ¼ wavelength).
                  PEQ handles the peak cleanly.
      partial   — excess GD < max(25 ms, ½ wavelength).
                  Modal ringing is significant; FIR shortens decay better than
                  PEQ, but PEQ still reduces the peak.
      geometry  — excess GD ≥ ½ wavelength, i.e. near-π phase offset.
                  Likely cancellation at the mic; repositioning (sub placement,
                  listening position, polarity/delay between subs) beats EQ.

    The 10 ms / 25 ms floors protect against over-classifying mid/upper-bass
    bands where a short wavelength makes the raw wavelength thresholds
    unrealistically tight.

    Returns (classification, fixable_bool) where fixable is True for both
    "fixable" and "partial" — both respond to some combination of PEQ + FIR.
    """
    period_ms = 1000.0 / max(freq_hz, 1e-6)
    fixable_threshold = max(10.0, 0.25 * period_ms)
    geometry_threshold = max(25.0, 0.5 * period_ms)

    if excess_gd_ms < fixable_threshold:
        return "fixable", True
    if excess_gd_ms < geometry_threshold:
        return "partial", True
    return "geometry", False


async def _tool_analyze_phase(
    session_id: int,
    min_hz: float = 20.0,
    max_hz: float = 120.0,
) -> dict:
    """Minimum-phase decomposition and fixability analysis.

    Separates measured response into minimum-phase (EQ-correctable) and
    excess-phase components. Each 1/3-octave band gets a three-tier
    classification — see ``_classify_fixability`` for the thresholds:

    - fixable   — minimum-phase, PEQ handles the peak cleanly
    - partial   — modal ringing dominates; FIR shortens decay better than PEQ
    - geometry  — near-π phase offset at the mic; cancellation — reposition

    Returns per-band freq_hz, spl_db, min_phase_group_delay_ms,
    excess_group_delay_ms, classification, fixable (bool).
    """
    from .storage import SessionStore
    import math

    try:
        store = SessionStore()
        sessions = store.list_sessions()
        session = next((s for s in sessions if s.id == session_id), None)
        if session is None:
            return _err(f"session {session_id} not found")

        fr = session.start_fr
        if not fr or not fr.frequencies or not fr.spl:
            return _err(f"session {session_id} has no frequency response data")

        import numpy as np
        from scipy.signal import hilbert

        # Build full complex spectrum from magnitude
        freqs = np.array(fr.frequencies)
        spl = np.array(fr.spl)
        mask = (freqs >= min_hz) & (freqs <= max_hz)
        freqs_band = freqs[mask]
        spl_band = spl[mask]

        if len(freqs_band) < 10:
            return _err(f"insufficient data points in {min_hz}-{max_hz} Hz range")

        # Minimum-phase computation via Hilbert transform of log-magnitude
        # The minimum-phase response has phase determined solely by its magnitude
        # via the Hilbert transform relationship: phase_min = -H{ln|H(f)|}
        log_mag = spl_band / 20.0 * np.log(10)  # Convert dB to natural log of magnitude
        min_phase_rad = -np.imag(hilbert(log_mag))

        # Get measured phase if available
        if fr.phase:
            measured_phase = np.array(fr.phase)
            measured_phase_band = measured_phase[mask]
        else:
            # No phase data — can only report magnitude, not fixability
            measured_phase_band = None

        # Compute group delay from phase
        def compute_group_delay(phase_data, freq_data):
            """Group delay = -d(phase)/d(omega)."""
            if len(phase_data) < 2:
                return np.array([]), np.array([])
            unwrapped = np.unwrap(phase_data)
            d_phase = np.diff(unwrapped)
            d_freq = np.diff(freq_data)
            omega_diff = 2.0 * np.pi * d_freq
            with np.errstate(divide="ignore", invalid="ignore"):
                gd = np.where(np.abs(omega_diff) > 1e-12, -d_phase / omega_diff, 0.0)
            mid_freqs = (freq_data[:-1] + freq_data[1:]) / 2.0
            return mid_freqs, gd * 1000.0  # Convert to ms

        # Minimum-phase group delay
        mp_gd_freqs, mp_gd_ms = compute_group_delay(min_phase_rad, freqs_band)

        # Measured group delay and excess
        excess_gd_ms = None
        if measured_phase_band is not None:
            meas_gd_freqs, meas_gd_ms = compute_group_delay(measured_phase_band, freqs_band)
            if len(meas_gd_ms) > 0 and len(mp_gd_ms) > 0:
                # Excess group delay = measured - minimum phase
                min_len = min(len(meas_gd_ms), len(mp_gd_ms))
                excess_gd_ms = meas_gd_ms[:min_len] - mp_gd_ms[:min_len]

        # Downsample to 1/3-octave bands
        bands = []
        for centre in _THIRD_OCTAVE_CENTRES:
            if centre < min_hz or centre > max_hz:
                continue
            factor = 2 ** (1 / 6)
            lo = centre / factor
            hi = centre * factor

            # SPL in this band
            band_mask = (freqs_band >= lo) & (freqs_band < hi)
            if not np.any(band_mask):
                continue

            avg_spl = float(np.mean(spl_band[band_mask]))

            # Minimum-phase group delay in this band
            gd_mask = (mp_gd_freqs >= lo) & (mp_gd_freqs < hi) if len(mp_gd_freqs) > 0 else np.array([])
            mp_gd = float(np.mean(mp_gd_ms[gd_mask])) if np.any(gd_mask) else 0.0

            entry: dict = {
                "freq_hz": centre,
                "spl_db": round(avg_spl, 1),
                "min_phase_group_delay_ms": round(mp_gd, 1),
            }

            # Excess group delay if available
            if excess_gd_ms is not None:
                egdm = (mp_gd_freqs >= lo) & (mp_gd_freqs < hi) if len(mp_gd_freqs) > 0 else np.array([])
                if np.any(egdm):
                    excess_vals = excess_gd_ms[:len(mp_gd_freqs)][egdm]
                    if len(excess_vals) > 0:
                        avg_excess = float(np.mean(np.abs(excess_vals)))
                        entry["excess_group_delay_ms"] = round(avg_excess, 1)
                        classification, fixable = _classify_fixability(centre, avg_excess)
                        entry["classification"] = classification
                        entry["fixable"] = fixable
                    else:
                        entry["excess_group_delay_ms"] = 0.0
                        entry["classification"] = "fixable"
                        entry["fixable"] = True
                else:
                    entry["excess_group_delay_ms"] = 0.0
                    entry["classification"] = "fixable"
                    entry["fixable"] = True
            else:
                entry["classification"] = None
                entry["fixable"] = None  # No phase data, can't determine

            bands.append(entry)

        has_phase = measured_phase_band is not None
        return _ok(
            session_id=session_id,
            has_phase_data=has_phase,
            bands=bands,
            note=(
                "classification uses a frequency-scaled excess-group-delay threshold "
                "(¼-wavelength / ½-wavelength with 10 ms / 25 ms floors): "
                "'fixable' = minimum-phase, PEQ handles the peak cleanly; "
                "'partial' = modal ringing dominates — FIR shortens decay better than PEQ, "
                "but PEQ still reduces the peak; "
                "'geometry' = near-π phase offset, likely cancellation at the mic — "
                "repositioning (sub placement, polarity/delay between subs) beats EQ. "
                "fixable=True for 'fixable' and 'partial'; False for 'geometry'. "
                "NOTE: on a solo-sub measurement, excess GD reflects room reflections; "
                "on a combined measurement it also includes sub-to-sub phase mismatch "
                "(use compare_sub_phase to distinguish)."
            ) if has_phase else (
                "No phase data available — fixability cannot be determined. "
                "SPL and minimum-phase group delay are still valid."
            ),
        )
    except Exception as exc:
        return _err(f"analyze_phase failed: {exc}")


async def _tool_compare_sub_phase(
    session_a: int,
    session_b: int,
    min_hz: float = 20.0,
    max_hz: float = 120.0,
) -> dict:
    """Compare phase relationship between two solo sub measurements.

    Returns per-1/3-octave band: phase difference, predicted coherent sum
    (constructive vs destructive), and reinforcement classification.
    Use this to understand where subs help vs fight each other before
    deciding on delay/polarity corrections.
    """
    from .storage import SessionStore
    import math

    try:
        store = SessionStore()
        sessions = store.list_sessions()
        sa = next((s for s in sessions if s.id == session_a), None)
        sb = next((s for s in sessions if s.id == session_b), None)
        if sa is None:
            return _err(f"session {session_a} not found")
        if sb is None:
            return _err(f"session {session_b} not found")

        for s_check, s_id in [(sa, session_a), (sb, session_b)]:
            if not s_check.start_fr or not s_check.start_fr.frequencies:
                return _err(f"session {s_id} has no frequency response data")
            if not s_check.start_fr.phase:
                return _err(f"session {s_id} has no phase data — needed for phase comparison")

        import numpy as np

        freqs_a = np.array(sa.start_fr.frequencies)
        spl_a = np.array(sa.start_fr.spl)
        phase_a = np.array(sa.start_fr.phase)
        freqs_b = np.array(sb.start_fr.frequencies)
        spl_b = np.array(sb.start_fr.spl)
        phase_b = np.array(sb.start_fr.phase)

        bands = []
        for centre in _THIRD_OCTAVE_CENTRES:
            if centre < min_hz or centre > max_hz:
                continue
            factor = 2 ** (1 / 6)
            lo = centre / factor
            hi = centre * factor

            mask_a = (freqs_a >= lo) & (freqs_a < hi)
            mask_b = (freqs_b >= lo) & (freqs_b < hi)

            if not np.any(mask_a) or not np.any(mask_b):
                continue

            avg_spl_a = float(np.mean(spl_a[mask_a]))
            avg_spl_b = float(np.mean(spl_b[mask_b]))
            avg_phase_a = float(np.mean(phase_a[mask_a]))
            avg_phase_b = float(np.mean(phase_b[mask_b]))

            phase_diff = avg_phase_b - avg_phase_a
            # Wrap to [-pi, pi]
            phase_diff_wrapped = math.atan2(math.sin(phase_diff), math.cos(phase_diff))
            phase_diff_deg = round(math.degrees(phase_diff_wrapped), 1)

            # Predict coherent sum: vector addition of the two sub signals
            # Convert SPL to linear amplitude, add as complex vectors, convert back
            amp_a = 10.0 ** (avg_spl_a / 20.0)
            amp_b = 10.0 ** (avg_spl_b / 20.0)
            sum_real = amp_a * math.cos(avg_phase_a) + amp_b * math.cos(avg_phase_b)
            sum_imag = amp_a * math.sin(avg_phase_a) + amp_b * math.sin(avg_phase_b)
            sum_amp = math.sqrt(sum_real ** 2 + sum_imag ** 2)
            sum_spl = 20.0 * math.log10(sum_amp + 1e-12)

            # Perfect in-phase sum would be +6dB over each individual
            # Perfect out-of-phase would be deep null
            incoherent_sum = 10.0 * math.log10(amp_a ** 2 + amp_b ** 2 + 1e-12)  # power sum
            reinforcement_db = round(sum_spl - incoherent_sum, 1)

            if abs(phase_diff_deg) < 60:
                classification = "reinforcing"
            elif abs(phase_diff_deg) > 120:
                classification = "cancelling"
            else:
                classification = "partial"

            bands.append({
                "freq_hz": centre,
                "sub_a_spl_db": round(avg_spl_a, 1),
                "sub_b_spl_db": round(avg_spl_b, 1),
                "phase_diff_deg": phase_diff_deg,
                "predicted_sum_db": round(sum_spl, 1),
                "reinforcement_db": reinforcement_db,
                "classification": classification,
            })

        # Summary statistics
        cancelling = [b for b in bands if b["classification"] == "cancelling"]
        reinforcing = [b for b in bands if b["classification"] == "reinforcing"]

        return _ok(
            session_a=session_a,
            session_b=session_b,
            bands=bands,
            reinforcing_bands=len(reinforcing),
            cancelling_bands=len(cancelling),
            note="Phase diff near 0°=reinforcing (+6dB ideal), near 180°=cancelling (deep null). "
                 "Cancelling bands cannot be fixed with EQ — consider delay/polarity adjustment or repositioning.",
        )
    except Exception as exc:
        return _err(f"compare_sub_phase failed: {exc}")


async def _tool_design_fir(
    session_id: int,
    target_curve: dict | None = None,
    num_taps: int = 1024,
    phase_mode: str = "minimum",
    freq_focus_hz: list[float] | None = None,
) -> dict:
    """Design FIR correction coefficients from a measurement.

    The LLM decides the strategy (phase mode, tap count, frequency focus).
    This tool computes the coefficients and returns them with a predicted
    response and pre-ringing estimate.

    phase_mode:
      - "minimum": no pre-ringing, phase not corrected (safest)
      - "linear": symmetric, corrects phase, adds pre-ringing (cleanest)
      - "mixed": minimum-phase below freq_focus, linear above (compromise)
    """
    from .storage import SessionStore

    try:
        store = SessionStore()
        sessions = store.list_sessions()
        session = next((s for s in sessions if s.id == session_id), None)
        if session is None:
            return _err(f"session {session_id} not found")

        fr = session.start_fr
        if not fr or not fr.frequencies:
            return _err(f"session {session_id} has no frequency response data")

        import numpy as np
        from scipy.signal import minimum_phase, firwin

        # Query the active driver for FIR limits; fall back to miniDSP constants
        # when no DSP is attached (e.g. unit tests exercising the math only).
        if _dsp is not None:
            caps = _dsp.capabilities
            if not caps.fir_capable:
                return _err("active DSP does not support FIR")
            fir_min = caps.fir_min_taps
            fir_max = caps.fir_max_taps_per_output
            fir_fs = caps.fir_sample_rate_hz
        else:
            fir_min, fir_max, fir_fs = 64, 2048, 96000

        if num_taps < fir_min or num_taps > fir_max:
            return _err(f"num_taps must be {fir_min}-{fir_max} (got {num_taps})")

        # Build target magnitude response at FIR sample rate
        freqs_measured = np.array(fr.frequencies)
        spl_measured = np.array(fr.spl)

        # Target: either provided curve or flat at average SPL
        if target_curve and target_curve.get("points"):
            import math
            target_points = sorted(target_curve["points"], key=lambda p: p["freq"])
            target_spl_at_freq = []
            for f in freqs_measured:
                if f < target_points[0]["freq"]:
                    target_spl_at_freq.append(target_points[0]["spl"])
                elif f > target_points[-1]["freq"]:
                    target_spl_at_freq.append(target_points[-1]["spl"])
                else:
                    for i in range(len(target_points) - 1):
                        f0, s0 = target_points[i]["freq"], target_points[i]["spl"]
                        f1, s1 = target_points[i + 1]["freq"], target_points[i + 1]["spl"]
                        if f0 <= f <= f1:
                            t = math.log(f / f0) / math.log(f1 / f0) if f1 != f0 else 0.0
                            target_spl_at_freq.append(s0 + t * (s1 - s0))
                            break
            target_spl = np.array(target_spl_at_freq)
        else:
            target_spl = np.full_like(spl_measured, np.mean(spl_measured))

        # Compute correction curve (dB): what the FIR needs to add
        correction_db = target_spl - spl_measured

        # Optional: focus only on certain frequency range
        if freq_focus_hz:
            focus_lo, focus_hi = float(freq_focus_hz[0]), float(freq_focus_hz[1])
            # Taper correction outside focus range to zero
            for i, f in enumerate(freqs_measured):
                if f < focus_lo or f > focus_hi:
                    correction_db[i] = 0.0

        # Convert correction dB to linear magnitude
        correction_linear = 10.0 ** (correction_db / 20.0)

        # Build full-length FIR frequency response (at fir_fs)
        n_fft = num_taps * 2
        fir_freqs = np.fft.rfftfreq(n_fft, d=1.0 / fir_fs)

        # Interpolate correction to FIR frequency grid
        fir_correction = np.interp(fir_freqs, freqs_measured, correction_linear, left=1.0, right=1.0)

        # Design FIR via inverse FFT
        if phase_mode == "linear":
            # Linear phase: symmetric FIR, corrects both magnitude and phase
            H = fir_correction  # Real-valued = linear phase
            fir_td = np.fft.irfft(H, n=n_fft)
            # Window and truncate to num_taps
            fir_td = np.roll(fir_td, num_taps // 2)[:num_taps]
            window = np.hanning(num_taps)
            fir_td *= window
            pre_ringing_ms = round((num_taps / 2) / fir_fs * 1000, 2)
        elif phase_mode == "minimum":
            # Minimum phase: no pre-ringing, magnitude-only correction
            H = fir_correction
            fir_td = np.fft.irfft(H, n=n_fft)
            # Convert to minimum phase
            try:
                fir_td_mp = minimum_phase(fir_td[:num_taps * 2], method="homomorphic", n_fft=n_fft)
                fir_td = fir_td_mp[:num_taps]
            except Exception:
                # Fallback: truncate + window to reduce spectral leakage
                log.warning("minimum_phase() failed, falling back to windowed truncation")
                fir_td = fir_td[:num_taps] * np.hanning(num_taps)
            pre_ringing_ms = 0.0
        else:  # mixed
            # Mixed: minimum phase below focus, linear above
            H = fir_correction
            fir_td = np.fft.irfft(H, n=n_fft)
            try:
                fir_td_mp = minimum_phase(fir_td[:num_taps * 2], method="homomorphic", n_fft=n_fft)
                fir_td = fir_td_mp[:num_taps]
            except Exception:
                log.warning("minimum_phase() failed, falling back to windowed truncation")
                fir_td = fir_td[:num_taps] * np.hanning(num_taps)
            pre_ringing_ms = 0.0

        # Normalize so peak <= 1.0
        peak = float(np.max(np.abs(fir_td)))
        if peak > 0:
            fir_td = fir_td / peak

        # Compute predicted frequency response of the FIR
        fir_H = np.fft.rfft(fir_td, n=n_fft)
        fir_mag_db = 20.0 * np.log10(np.abs(fir_H) + 1e-12)
        fir_freqs_out = np.fft.rfftfreq(n_fft, d=1.0 / fir_fs)

        # Downsample prediction to 1/3-octave for compact output
        predicted_bands = []
        for centre in _THIRD_OCTAVE_CENTRES:
            factor = 2 ** (1 / 6)
            lo = centre / factor
            hi = centre * factor
            band_mask = (fir_freqs_out >= lo) & (fir_freqs_out < hi)
            if np.any(band_mask):
                avg_db = float(np.mean(fir_mag_db[band_mask]))
                predicted_bands.append({
                    "freq_hz": centre,
                    "fir_effect_db": round(avg_db, 1),
                })

        # Frequency resolution
        freq_resolution = round(fir_fs / num_taps, 1)

        coefficients = [round(float(c), 8) for c in fir_td]

        return _ok(
            session_id=session_id,
            num_taps=num_taps,
            phase_mode=phase_mode,
            pre_ringing_ms=pre_ringing_ms,
            freq_resolution_hz=freq_resolution,
            coefficients=coefficients,
            predicted_effect=predicted_bands,
            note=f"FIR at {fir_fs}Hz, {num_taps} taps, {phase_mode} phase. "
                 f"Freq resolution: {freq_resolution}Hz. "
                 f"Pre-ringing: {pre_ringing_ms}ms. "
                 f"Pass coefficients to apply_fir(output_index, coefficients).",
        )
    except Exception as exc:
        return _err(f"design_fir failed: {exc}")


async def _tool_avr_set_volume(level_db: float) -> dict:
    """Set AVR volume to *level_db* dB.

    No-op when playback_route is 'usb' — the Denon is not in the signal
    chain and its volume has no effect on sweep playback or SPL at the mic.
    """
    cfg = _config()
    if cfg.measurement.get("playback_route", "usb") == "usb":
        return _ok(
            level_db=None,
            message="USB mode: Denon is not in the signal chain — volume unchanged.",
        )
    try:
        confirmed_db = await _avr.set_volume(level_db)  # type: ignore[union-attr]
        return _ok(level_db=confirmed_db)
    except DriverError as exc:
        return _err(f"avr unreachable: {exc}")


async def _tool_trigger_measurement(
    label: str | None = None,
    position: str | None = None,
    target_curve: dict | None = None,
) -> dict:
    """Trigger a measurement via UMIK-1 + PyTTa.

    Calls MeasurementEngine.measure() directly (no HTTP hop). Wraps with
    DenonSweepContext when HDMI route is configured.

    Pass *target_curve* only when this measurement is part of a calibration
    loop — include the reference_spl and band used to compute the filters for
    this iteration. Leave None for raw/diagnostic captures; those sessions will
    not show a delta on the dashboard.
    """
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        from .measurement import _find_umik_device
        cfg = _config()
        mic_name = cfg.mic.get("name", "UMIK")
        if _find_umik_device(devices, name_substring=mic_name) is None:
            return _err(
                f"trigger_measurement requires {mic_name} microphone — none found. "
                "Check USB connection."
            )
    except Exception as exc:
        return _err(
            f"trigger_measurement requires sounddevice — audio device enumeration failed: {exc}"
        )

    try:
        from .measurement import MeasurementEngine, compute_session_metadata
        from .storage import SessionStore

        cfg = _config()
        engine = MeasurementEngine(cfg)
        route = cfg.measurement.get("playback_route", "usb")
        denon_ctx = DenonSweepContext.from_config(cfg)

        if denon_ctx:
            # HDMI route: ensure DSP is on Analog source and manage master gain.
            # Only perform source switching on DSPs that actually have sources;
            # CamillaDSP has a single pipeline and reports valid_sources=∅.
            saved_source = None
            saved_gain = None
            dsp_has_sources = bool(_dsp.capabilities.valid_sources)  # type: ignore[union-attr]
            try:
                state = await _dsp.get_state()  # type: ignore[union-attr]
                current_source = state.get("source", "") or ""
                if dsp_has_sources and current_source.lower() != "analog":
                    log.info("HDMI route: switching DSP source %s→Analog", current_source)
                    saved_source = current_source
                    await _dsp.set_source("Analog")  # type: ignore[union-attr]

                hdmi_gain = cfg.measurement.get("master_gain_hdmi_db")
                if hdmi_gain is not None:
                    saved_gain = float(state.get("volume", 0.0) or 0.0)
                    log.info("HDMI route: setting master gain %.1f dB (was %.1f dB)", hdmi_gain, saved_gain)
                    await _dsp.set_master_gain(float(hdmi_gain))  # type: ignore[union-attr]

                async with denon_ctx:
                    fr = await engine.measure()
            finally:
                if saved_gain is not None:
                    log.info("HDMI route: restoring master gain to %.1f dB", saved_gain)
                    try:
                        await _dsp.set_master_gain(saved_gain)  # type: ignore[union-attr]
                    except Exception as exc:
                        log.warning("Failed to restore master gain: %s", exc)
                if saved_source is not None:
                    log.info("HDMI route: restoring DSP source to %s", saved_source)
                    try:
                        await _dsp.set_source(saved_source)  # type: ignore[union-attr]
                    except Exception as exc:
                        log.warning("Failed to restore DSP source: %s", exc)
        else:
            # USB mode: use persistent sweep session (enters once, stays active).
            await _ensure_sweep_session()
            fr = await engine.measure()

        # Compute IR-derived metadata at capture time
        metadata = compute_session_metadata(fr)
        if position:
            metadata["position"] = position

        # Build descriptive label: "combined @ MLP", "sub1-solo @ MLP"
        base_label = label or "combined"
        if position and f"@ {position}" not in base_label:
            full_label = f"{base_label} @ {position}"
        else:
            full_label = base_label

        store = SessionStore()
        session_id = store.save_measurement(fr, label=full_label, metadata=metadata, target_curve=target_curve)

        # Downsample group delay to 1/3-octave summary (11 points vs ~17KB raw)
        response_metadata = dict(metadata)
        gd = metadata.get("group_delay")
        if gd and "freq_hz" in gd and "delay_ms" in gd:
            gd_summary = _downsample_group_delay(gd["freq_hz"], gd["delay_ms"])
            response_metadata["group_delay"] = gd_summary

        # Include coherence summary if available
        if fr.coherence:
            coh_summary = _downsample_coherence(fr.frequencies, fr.coherence)
            response_metadata["coherence"] = coh_summary

        return _ok(
            session_id=session_id,
            label=full_label,
            metadata=response_metadata,
            message="Measurement complete — use get_measurement_history() to retrieve results.",
        )
    except Exception as exc:
        return _err(f"measurement failed: {exc}")


async def _tool_play_and_measure_fft(
    channel_assignments: dict,
    duration_s: float = 2.0,
    amplitude: float = 0.5,
    fft_size: int = 8192,
) -> dict:
    """Synthesize multitone, play via HDMI, record from UMIK, return FFT analysis."""
    import numpy as np

    try:
        from .headroom import analyze_fft, build_multichannel_buffer
        from .drivers.playback import MultichannelPlayback
        from .measurement import _find_umik_device

        cfg = _config()
        sample_rate = cfg.measurement.get("sample_rate", 48000)

        # Parse channel_assignments: keys may be strings from JSON
        parsed: dict[int, list[float]] = {}
        all_freqs: list[float] = []
        for ch_str, freqs in channel_assignments.items():
            ch = int(ch_str)
            parsed[ch] = [float(f) for f in freqs]
            all_freqs.extend(parsed[ch])

        if not parsed:
            return _err("channel_assignments is empty")

        n_channels = max(parsed.keys())
        # HDMI requires standard channel counts
        standard_counts = [2, 6, 8]
        n_channels = next(c for c in standard_counts if c >= n_channels)

        buf = build_multichannel_buffer(
            parsed, duration_s, sample_rate, amplitude, n_channels,
        )

        # Device detection
        import sounddevice as sd
        devices = sd.query_devices()

        mic_idx = cfg.measurement.get("mic_device_index")
        if mic_idx is None:
            mic_name = cfg.mic.get("name", "UMIK")
            mic_idx = _find_umik_device(devices, name_substring=mic_name)
        if mic_idx is None:
            return _err("UMIK microphone not found — check USB connection")

        hdmi_idx = cfg.measurement.get("hdmi_device_index")
        if hdmi_idx is None:
            # Auto-detect HDMI output
            candidates = [
                (i, d) for i, d in enumerate(devices)
                if d["max_output_channels"] > 0 and "hdmi" in d["name"].lower()
            ]
            candidates.sort(key=lambda x: (x[1]["name"].lower() != "hdmi", len(x[1]["name"])))
            if candidates:
                hdmi_idx = candidates[0][0]
        if hdmi_idx is None:
            return _err("No HDMI output device found")

        player = MultichannelPlayback()
        recording, n_recorded = await asyncio.get_event_loop().run_in_executor(
            None, player.play_and_record, buf, sample_rate, mic_idx, hdmi_idx,
        )

        if len(recording) == 0:
            return _err("Recording is empty — no audio captured")

        result = analyze_fft(recording, sample_rate, all_freqs, fft_size)

        peak_dbfs = 20.0 * np.log10(np.max(np.abs(recording)) + 1e-12)
        return _ok(
            duration_s=duration_s,
            sample_rate=sample_rate,
            recording_peak_dbfs=round(float(peak_dbfs), 1),
            **result,
        )

    except Exception as exc:
        log.exception("play_and_measure_fft failed")
        return _err(f"play_and_measure_fft error: {exc}")


async def _tool_assign_headroom_tones(
    speaker_passbands: dict,
    tones_per_speaker: int = 4,
    min_spacing_hz: float = 30.0,
    min_frequency_hz: float = 200.0,
) -> dict:
    """Assign non-overlapping multitone clusters to speakers."""
    try:
        from .headroom import assign_tone_clusters

        cfg = _config()

        # Parse passbands: {role: {low_hz, high_hz}} → {role: (low, high)}
        parsed: dict[str, tuple[float, float]] = {}
        for role, band in speaker_passbands.items():
            parsed[role] = (float(band["low_hz"]), float(band["high_hz"]))

        assignments = assign_tone_clusters(
            parsed,
            tones_per_speaker=tones_per_speaker,
            min_inter_speaker_spacing_hz=min_spacing_hz,
            min_frequency_hz=min_frequency_hz,
        )

        # Build both role-keyed and channel-keyed outputs
        role_assignments = {}
        channel_assignments: dict[str, list[float]] = {}
        for role, tones in assignments.items():
            ch = cfg.hdmi_channel_for(role)
            role_assignments[role] = {
                "hdmi_channel": ch,
                "tones_hz": tones,
            }
            if ch is not None:
                channel_assignments[str(ch)] = tones

        return _ok(
            assignments=role_assignments,
            channel_assignments=channel_assignments,
        )

    except ValueError as exc:
        return _err(f"Cannot assign tones: {exc}")
    except Exception as exc:
        log.exception("assign_headroom_tones failed")
        return _err(f"assign_headroom_tones error: {exc}")


async def _tool_calibrate_level(
    target_spl_db: float = 78.0,
    start_db: float = -30.0,
    max_volume_db: float = 0.0,
) -> dict:
    """Auto-calibrate sweep level using predict-and-verify (2 sweeps).

    Targets a specific SPL at the microphone (default 78 dB — typical
    listening level).  Uses the UMIK sensitivity from the cal file to
    convert between dBFS recording level and acoustic SPL.

    1. Probe sweep at a safe starting level (start_db).
    2. Compute actual SPL from peak_dBFS + mic offset.
    3. Compute exact gain correction to hit target_spl_db.
    4. Verify with a second sweep.

    USB mode: adjusts miniDSP master gain.
    HDMI mode: adjusts AVR volume.
    """
    from .measurement import MeasurementEngine, MeasurementQualityError, parse_umik_sensitivity
    from .config import update_config
    import math as _math
    import numpy as _np

    cfg = _config()

    # Load UMIK sensitivity offset: SPL = dBFS + offset
    _mic_offset = 0.0
    cal_path = cfg._data.get("mic", {}).get("cal_file")
    if cal_path:
        try:
            _mic_offset = parse_umik_sensitivity(cal_path)
            log.info("calibrate_level: UMIK sensitivity offset = %.1f dB", _mic_offset)
        except Exception as exc:
            log.warning("calibrate_level: failed to parse UMIK sensitivity: %s", exc)

    def _ir_spl(fr) -> float:
        """SPL estimate from recording RMS + UMIK sensitivity offset.

        Uses the RMS of the raw recording (dBFS) converted to acoustic SPL
        via the UMIK cal file sensitivity.  RMS gives broadband average SPL,
        which is what the target represents.  Peak would overestimate by
        10-20 dB due to room modes and crest factor.
        """
        if fr.recording_rms_dbfs is not None:
            return float(round(fr.recording_rms_dbfs + _mic_offset, 1))
        return float(round(fr.recording_peak_dbfs + _mic_offset, 1))
    engine = MeasurementEngine(cfg)
    route = cfg.measurement.get("playback_route", "usb")

    # Gain limits
    _gain_floor = -50.0
    _gain_ceiling = 0.0

    log.info("calibrate_level: target SPL=%.0f dB, start=%.0f dB", target_spl_db, start_db)

    if route == "usb":
        if _dsp is None:
            return _err("DSP driver not loaded")

        sub_count = sum(
            1 for slot in cfg.minidsp.get("output_slots", [])
            if slot.get("type", "unused") == "sub"
        )
        _sub_count = max(1, sub_count)

        async def _usb_predict_verify() -> dict:
            # ── Step 1: probe at safe starting level ──
            probe_gain = start_db
            await _dsp.set_master_gain(probe_gain)  # type: ignore[union-attr]
            await asyncio.sleep(0.3)
            try:
                probe_fr = await engine.measure()
            except MeasurementQualityError as exc:
                return _err(
                    f"USB sweep SNR too low at {probe_gain:.0f} dB ({exc.detail}). "
                    "Turn up the sub's physical gain knob, then retry."
                )
            except Exception as exc:
                return _err(f"calibrate_level probe failed: {exc}")

            probe_spl = _ir_spl(probe_fr)

            # ── Step 2: compute correction ──
            correction = round(target_spl_db - probe_spl, 1)
            computed_gain = round(probe_gain + correction, 1)
            computed_gain = max(_gain_floor, min(_gain_ceiling, computed_gain))

            log.info(
                "calibrate_level USB: probe at %.1f dB → IR peak %.1f dB SPL, "
                "correction %.1f dB → target gain %.1f dB",
                probe_gain, probe_spl, correction, computed_gain,
            )

            # ── Step 3: verify at computed level ──
            await _dsp.set_master_gain(computed_gain)  # type: ignore[union-attr]
            await asyncio.sleep(0.3)
            try:
                verify_fr = await engine.measure()
            except MeasurementQualityError as exc:
                return _err(
                    f"USB sweep failed at computed gain {computed_gain:.1f} dB ({exc.detail}). "
                    "Turn up the sub's physical gain knob, then retry."
                )
            except Exception as exc:
                return _err(f"calibrate_level verify failed: {exc}")

            verify_spl = _ir_spl(verify_fr)

            # If verify is >3 dB above target, back off
            final_gain = computed_gain
            if verify_spl > target_spl_db + 3.0:
                overshoot = verify_spl - target_spl_db
                final_gain = round(max(_gain_floor, computed_gain - overshoot), 1)
                log.info(
                    "calibrate_level: verify %.1f dB SPL, %.1f dB over target, "
                    "backing off to %.1f dB gain",
                    verify_spl, overshoot, final_gain,
                )
                await _dsp.set_master_gain(final_gain)  # type: ignore[union-attr]
                verify_spl = round(verify_spl - overshoot, 1)

            update_config({"measurement": {"master_gain_db": final_gain}})

            _solo_offset = round(20.0 * _math.log10(_sub_count), 1) if _sub_count > 1 else 0.0
            _solo_gain = round(min(0.0, final_gain + _solo_offset), 1)
            return _ok(
                calibrated_volume_db=None,
                calibrated_master_gain_db=final_gain,
                suggested_solo_gain_db=_solo_gain,
                estimated_spl_db=verify_spl,
                message=(
                    f"USB mode: master gain set to {final_gain:.1f} dB "
                    f"(~{verify_spl:.0f} dB SPL at mic, target {target_spl_db:.0f} dB SPL). "
                    f"2 sweeps. "
                    f"For solo single-sub sweeps use suggested_solo_gain_db "
                    f"({_solo_gain:.1f} dB) — muting one of {_sub_count} subs "
                    f"reduces SPL by ~{_solo_offset:.0f} dB, allowing higher gain "
                    "without clipping."
                ),
            )

        # USB mode: use persistent sweep session (enters once, stays active).
        await _ensure_sweep_session()
        return await _usb_predict_verify()

    # ── HDMI/AVR mode ──
    if _avr is None:
        return _err("AVR driver not loaded")

    denon_ctx = DenonSweepContext.from_config(cfg, manage_volume=False)

    async def _hdmi_predict_verify() -> dict:
        # ── Step 1: probe at safe starting volume ──
        probe_vol = start_db
        await _avr.set_volume(probe_vol)  # type: ignore[union-attr]
        await asyncio.sleep(0.5)
        try:
            probe_fr = await engine.measure()
        except MeasurementQualityError as exc:
            if exc.check in ("snr", "sweep_capture"):
                return _err(
                    f"SNR too low at {probe_vol:.0f} dB ({exc.detail}). "
                    f"Try a higher start_db or check that subs are powered on."
                )
            return _err(f"measurement quality error: {exc.detail}")
        except Exception as exc:
            return _err(f"calibrate_level probe failed: {exc}")

        probe_spl = _ir_spl(probe_fr)

        # ── Step 2: compute correction ──
        correction = round(target_spl_db - probe_spl, 1)
        computed_vol = round(probe_vol + correction, 1)
        computed_vol = max(-80.0, min(max_volume_db, computed_vol))

        log.info(
            "calibrate_level HDMI: probe at %.1f dB → IR peak %.1f dB SPL, "
            "correction %.1f dB → target volume %.1f dB",
            probe_vol, probe_spl, correction, computed_vol,
        )

        # ── Step 3: verify ──
        await _avr.set_volume(computed_vol)  # type: ignore[union-attr]
        await asyncio.sleep(0.5)
        try:
            verify_fr = await engine.measure()
        except MeasurementQualityError as exc:
            if exc.check in ("snr", "sweep_capture"):
                return _err(
                    f"SNR still too low at computed volume {computed_vol:.1f} dB. "
                    "Check that subs are powered on and signal path is correct."
                )
            return _err(f"measurement quality error: {exc.detail}")
        except Exception as exc:
            return _err(f"calibrate_level verify failed: {exc}")

        verify_spl = _ir_spl(verify_fr)

        final_vol = computed_vol
        if verify_spl > target_spl_db + 3.0:
            overshoot = verify_spl - target_spl_db
            final_vol = round(max(-80.0, computed_vol - overshoot), 1)
            await _avr.set_volume(final_vol)  # type: ignore[union-attr]
            verify_spl = round(verify_spl - overshoot, 1)

        update_config({"measurement": {"denon_sweep_volume": final_vol}})
        return _ok(
            calibrated_volume_db=final_vol,
            estimated_spl_db=verify_spl,
            message=(
                f"HDMI mode: volume set to {final_vol:.1f} dB "
                f"(~{verify_spl:.0f} dB SPL at mic, target {target_spl_db:.0f} dB SPL). "
                f"2 sweeps."
            ),
        )

    if denon_ctx:
        async with denon_ctx:
            return await _hdmi_predict_verify()
    return await _hdmi_predict_verify()


async def _tool_fetch_recipe(name: str) -> dict:
    """Return the content of a recipe by name (e.g. 'core/harman-bass').

    Recipes live in the recipes/ directory of this repo.  The name is a
    relative path within that directory, without the .md extension.
    """
    # Sanitise: prevent path traversal via ".." or symlinks
    safe_name = name.strip().lstrip("/")
    if ".." in safe_name:
        return _err(f"invalid recipe name: {name!r}")

    recipe_path = RECIPES_DIR / f"{safe_name}.md"

    # P0: resolve symlinks before checking containment
    if not recipe_path.resolve().is_relative_to(RECIPES_DIR.resolve()):
        return _err(f"invalid recipe name: {name!r}")

    if not recipe_path.exists():
        return _err(f"recipe not found: {name}")

    try:
        content = recipe_path.read_text(encoding="utf-8")
        return _ok(name=name, content=content)
    except Exception as exc:
        return _err(f"failed to read recipe: {exc}")


async def _tool_get_calibration_runs(limit: int = 10, run_id: int | None = None) -> dict:
    """Return calibration run history, or detail for a single run."""
    from .storage import SessionStore

    store = SessionStore()

    if run_id is not None:
        detail = store.get_run_detail(run_id)
        if detail is None:
            return _err(f"run #{run_id} not found")
        return _ok(**detail)

    runs = store.get_runs(limit=limit)
    return _ok(runs=runs)


async def _tool_save_calibration_run(
    recipe_name: str,
    target: str,
    device_state: dict | None = None,
    run_type: str = "calibration",
) -> dict:
    """Create a new calibration run record with optional equipment state snapshot.

    *run_type*: "calibration" (default, iterative EQ loop) or "validation"
    (read-only measurement sessions, no convergence criteria).
    """
    from .storage import SessionStore

    try:
        store = SessionStore()
        run_id = store.save_run(recipe_name, target, device_state=device_state, run_type=run_type)
        return _ok(run_id=run_id)
    except Exception as exc:
        return _err(f"save_calibration_run failed: {exc}")


async def _tool_update_calibration_run(
    run_id: int,
    converged: bool,
    iterations_run: int,
    baseline_rms: float | None = None,
    final_rms: float | None = None,
    error: str = "",
    target_curve_data: dict | None = None,
    sessions: list[dict] | None = None,
) -> dict:
    """Update a calibration run with final results.

    For validation runs, pass *sessions* — a list of
    {"session_id": N, "label": "..."} recording each measurement taken.
    """
    from .storage import SessionStore

    try:
        store = SessionStore()
        store.update_run(
            run_id,
            converged=converged,
            iterations_run=iterations_run,
            baseline_rms=baseline_rms,
            final_rms=final_rms,
            error=error,
            target_curve_data=target_curve_data,
            sessions=sessions,
        )
        return _ok(run_id=run_id, updated=True)
    except Exception as exc:
        return _err(f"update_calibration_run failed: {exc}")


async def _tool_save_calibration_iteration(
    run_id: int,
    iteration: int,
    rms_before: float,
    rms_after: float,
    filters_proposed: list[dict],
    filters_applied: list[dict],
    safety_ok: bool = True,
    safety_error: str = "",
) -> dict:
    """Save one iteration of a calibration run."""
    from .storage import SessionStore

    try:
        store = SessionStore()
        iter_id = store.save_iteration(
            run_id=run_id,
            iteration=iteration,
            rms_before=rms_before,
            rms_after=rms_after,
            filters_proposed=filters_proposed,
            filters_applied=filters_applied,
            safety_ok=safety_ok,
            safety_error=safety_error,
        )
        return _ok(iteration_id=iter_id, run_id=run_id)
    except Exception as exc:
        return _err(f"save_calibration_iteration failed: {exc}")


async def _tool_get_config() -> dict:
    """Return the current config.yaml plus EQ capabilities discovery."""
    try:
        cfg = _config()
        data = dict(cfg._data)

        # EQ capabilities — tells Claude what PEQ resources are available
        active_input = cfg.minidsp.get("active_input") or 0
        sub_outputs = cfg.sub_outputs
        slots = list(range(2, 10))  # slots 2-9

        # FIR limits come from the active driver so the same keys describe
        # miniDSP (2048 taps @ 96 kHz, 4096 shared) or CamillaDSP (65536 taps @
        # processing rate, no shared pool).
        if _dsp is not None:
            caps = _dsp.capabilities
            fir_block = {
                "fir_capable": caps.fir_capable,
                "fir_min_taps": caps.fir_min_taps,
                "fir_max_taps_per_output": caps.fir_max_taps_per_output,
                "fir_shared_tap_pool": caps.fir_shared_tap_pool,
                "fir_sample_rate_hz": caps.fir_sample_rate_hz,
            }
        else:
            fir_block = {
                "fir_capable": True,
                "fir_min_taps": 64,
                "fir_max_taps_per_output": 2048,
                "fir_shared_tap_pool": 4096,
                "fir_sample_rate_hz": 96000,
            }

        eq_capabilities: dict = {
            "input_peq": {
                "input_index": active_input,
                "available_slots": slots,
                "num_slots": len(slots),
                "description": "Shared EQ applied to all outputs (e.g. Harman target curve)",
                "tool": "apply_input_eq",
            },
            "output_peq": [],
            **fir_block,
        }

        for slot_cfg in cfg.minidsp.get("output_slots", []):
            idx = slot_cfg["index"]
            if idx in sub_outputs:
                eq_capabilities["output_peq"].append({
                    "output_index": idx,
                    "label": slot_cfg.get("label", f"Output {idx}"),
                    "available_slots": slots,
                    "num_slots": len(slots),
                    "description": "Per-sub room correction EQ",
                    "tool": "apply_eq with output_index",
                })

        # Include current EQ state if driver is loaded
        if _dsp is not None:
            try:
                preset = await _dsp.current_preset()
                # Broadcast state (legacy key)
                eq_capabilities["input_peq"]["current_filters"] = (
                    _dsp._eq_state.get(("input", active_input, preset), [])  # type: ignore[attr-defined]
                )
                for entry in eq_capabilities["output_peq"]:
                    oidx = entry["output_index"]
                    entry["current_filters"] = (
                        _dsp._eq_state.get((preset, oidx), [])  # type: ignore[attr-defined]
                    )
            except Exception:
                pass  # EQ state is best-effort

        data["eq_capabilities"] = eq_capabilities
        return _ok(config=data)
    except Exception as exc:
        return _err(f"config error: {exc}")


async def _tool_set_config(updates: dict) -> dict:
    """Deep-merge updates into config.yaml and return the result."""
    try:
        update_config(updates)
        cfg = _config()
        return _ok(config=cfg._data)
    except Exception as exc:
        return _err(f"config write error: {exc}")


async def _tool_discover_avr() -> dict:
    """Run SSDP scan for Denon/Marantz AVRs on the local network."""
    try:
        hosts = await _avr.discover()  # type: ignore[union-attr]
        return _ok(receivers=hosts)
    except Exception as exc:
        return _err(f"discovery error: {exc}")


async def _tool_mute_output(output_indices: list[int]) -> dict:
    """Mute individual miniDSP outputs (gain → -127 dB)."""
    try:
        await _dsp.mute_outputs(output_indices)  # type: ignore[union-attr]
        return _ok(muted=output_indices)
    except Exception as exc:
        return _err(f"mute failed: {exc}")


async def _tool_unmute_output(output_indices: list[int]) -> dict:
    """Unmute individual miniDSP outputs (gain → 0 dB)."""
    try:
        await _dsp.unmute_outputs(output_indices)  # type: ignore[union-attr]
        return _ok(unmuted=output_indices)
    except Exception as exc:
        return _err(f"unmute failed: {exc}")


async def _tool_end_sweep_session() -> dict:
    """End the persistent USB sweep session and restore the miniDSP source.

    Call this after calibration is complete (or on error) to restore the
    miniDSP from USB source back to its original source (typically Analog).
    Safe to call if no session is active — returns ok with a no-op message.
    """
    if _sweep_session is None or not _sweep_session.active:
        return _ok(message="No active sweep session to end")
    try:
        await _end_sweep_session()
        return _ok(message="Sweep session ended, source restored to original")
    except Exception as exc:
        return _err(f"end_sweep_session failed: {exc}")


async def _tool_set_delay(output_index: int, delay_ms: float) -> dict:
    """Set delay for a single DSP output in milliseconds."""
    try:
        await _dsp.set_output_delay(output_index, delay_ms)  # type: ignore[union-attr]
        _persist_dsp_state(f"delay_{output_index}", {"delay_ms": delay_ms})
        return _ok(output_index=output_index, delay_ms=delay_ms)
    except DriverError as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err(f"set_delay error: {exc}")


async def _tool_set_polarity(output_index: int, inverted: bool) -> dict:
    """Set polarity for a single DSP output (inverted=True flips phase)."""
    try:
        await _dsp.set_output_polarity(output_index, inverted)  # type: ignore[union-attr]
        _persist_dsp_state(f"polarity_{output_index}", {"inverted": inverted})
        return _ok(output_index=output_index, inverted=inverted)
    except DriverError as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err(f"set_polarity error: {exc}")


async def _tool_get_output_state() -> dict:
    """Return last-applied per-output gain, delay, polarity, FIR from driver cache.

    minidspd has no GET endpoint for these parameters — this reflects only what
    this MCP server has written since startup, not actual hardware readback.
    """
    try:
        state = _dsp.get_output_state()  # type: ignore[union-attr]
        return _ok(outputs=state)
    except Exception as exc:
        return _err(f"get_output_state error: {exc}")


async def _tool_analyze_ir(
    session_id: int | None = None,
    search_window_ms: float = 50.0,
) -> dict:
    """Extract IR peak time, polarity sign, and SPL from a stored session.

    Used for sub alignment: measure each sub solo, call this on each session,
    compute delay offsets from peak_time_s differences, apply via set_delay.
    """
    from .storage import SessionStore

    try:
        store = SessionStore()
        sessions = store.list_sessions()
        if not sessions:
            return _err("no measurements found — run measure first")

        if session_id is not None:
            session = next((s for s in sessions if s.id == session_id), None)
            if session is None:
                return _err(f"session {session_id} not found")
        else:
            session = sessions[0]

        ir = session.impulse_response
        if not ir:
            return _err(
                f"session {session.id} has no impulse response stored — "
                "re-run measure to capture IR"
            )

        import numpy as np
        from .measurement import detect_ir_onset

        sample_rate = session.start_fr.sample_rate if session.start_fr else 48000
        xcorr_ms = session.start_fr.xcorr_peak_ms if session.start_fr else None
        ir_arr = np.array(ir, dtype=np.float64)
        onset = detect_ir_onset(ir_arr, sample_rate, search_window_ms, xcorr_peak_ms=xcorr_ms)

        return _ok(
            session_id=session.id,
            peak_time_s=round(onset["peak_time_ms"] / 1000.0, 6),
            **onset,
        )
    except Exception as exc:
        return _err(f"analyze_ir failed: {exc}")


async def _tool_apply_fir(output_index: int, coefficients: list[float]) -> dict:
    """Write FIR coefficients to a single DSP output."""
    try:
        await _dsp.apply_fir(output_index, coefficients)  # type: ignore[union-attr]
        return _ok(output_index=output_index, taps=len(coefficients))
    except DriverError as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err(f"apply_fir error: {exc}")


async def _tool_clear_fir(output_index: int) -> dict:
    """Clear FIR coefficients and reset output to passthrough."""
    try:
        await _dsp.clear_fir(output_index)  # type: ignore[union-attr]
        return _ok(output_index=output_index, message="FIR cleared")
    except DriverError as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err(f"clear_fir error: {exc}")


async def _tool_set_output_gain(output_index: int, gain_db: float) -> dict:
    """Set gain for a single DSP output in dB."""
    try:
        await _dsp.set_output_gain(output_index, gain_db)  # type: ignore[union-attr]
        _persist_dsp_state(f"gain_{output_index}", {"gain_db": gain_db})
        return _ok(output_index=output_index, gain_db=gain_db)
    except DriverError as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err(f"set_output_gain error: {exc}")


async def _tool_set_master_gain(gain_db: float) -> dict:
    """Set the miniDSP master output gain (-127 to 0 dB).

    Global attenuation applied before all outputs. Use to control sweep
    playback volume without touching per-output alignment gains.
    Always restore to 0.0 after sweeps are done.
    """
    try:
        await _dsp.set_master_gain(gain_db)  # type: ignore[union-attr]
        return _ok(gain_db=max(-127.0, min(0.0, gain_db)))
    except DriverError as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err(f"set_master_gain error: {exc}")


async def _tool_configure_matrix(active_input: int | None = None) -> dict:
    """Route the active DSP input to enabled outputs, skipping defective/unused ones."""
    try:
        cfg = _config()
        input_idx = active_input if active_input is not None else cfg.minidsp.get("active_input", 0)
        # Only route to outputs that are not marked unused in config.
        slots = cfg.minidsp.get("output_slots", [])
        enabled_indices = {s["index"] for s in slots if s.get("type") != "unused"}
        all_indices = set(range(4))
        output_enabled = {i: (i in enabled_indices) for i in all_indices}
        other_input = 1 - input_idx
        await _dsp.set_routing({input_idx: output_enabled, other_input: {i: False for i in all_indices}})  # type: ignore[union-attr]
        return _ok(active_input=input_idx, routed_outputs=sorted(enabled_indices),
                   message=f"Input {input_idx} routed to outputs {sorted(enabled_indices)}")
    except DriverError as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err(f"configure_matrix error: {exc}")


async def _tool_analyze_decay(
    session_id: int | None = None,
    t60_threshold_ms: float = 300.0,
    freq_min: float = 20.0,
    freq_max: float = 200.0,
) -> dict:
    """Run T60 decay analysis on the impulse response from a stored session."""
    from .storage import SessionStore
    from .decay import analyze_decay as _analyze_decay

    try:
        store = SessionStore()
        sessions = store.list_sessions()
        if not sessions:
            return _err("no measurements found — run measure first")

        if session_id is not None:
            session = next((s for s in sessions if s.id == session_id), None)
            if session is None:
                return _err(f"session {session_id} not found")
        else:
            session = sessions[0]

        ir = session.impulse_response
        if not ir:
            return _err(
                f"session {session.id} has no impulse response stored — "
                "re-run measure to capture IR"
            )

        sample_rate = session.start_fr.sample_rate if session.start_fr else 48000
        modes = _analyze_decay(
            ir,
            sample_rate=sample_rate,
            t60_threshold_ms=t60_threshold_ms,
            freq_min=freq_min,
            freq_max=freq_max,
        )

        return _ok(
            session_id=session.id,
            mode_count=len(modes),
            modes=[
                {
                    "freq_hz": m.freq_hz,
                    "t60_ms": m.t60_ms,
                    "peak_db": m.peak_db,
                    "suggested_q": m.suggested_q,
                    "priority": m.priority,
                }
                for m in modes
            ],
        )
    except ValueError as exc:
        return _err(f"decay analysis error: {exc}")
    except Exception as exc:
        return _err(f"analyze_decay failed: {exc}")


async def _tool_check_system() -> dict:
    """Run all pre-flight hardware checks and return results."""
    from .preflight import PreflightChecker
    try:
        cfg = _config()
        checker = PreflightChecker(cfg)
        results = await checker.run_all()
        checks = [
            {
                "name": r.name,
                "passed": r.passed,
                "detail": r.detail,
                "error": r.error,
            }
            for r in results
        ]
        all_passed = all(r.passed for r in results)
        return _ok(all_passed=all_passed, checks=checks)
    except Exception as exc:
        return _err(f"check_system error: {exc}")


# ── MCP Server ─────────────────────────────────────────────────────────────────


server = Server("avr-calibration")

_TOOLS: list[Tool] = [
    Tool(
        name="get_device_state",
        description=(
            "Return current AVR and DSP hardware state. "
            "Includes AVR volume, input, mute status, and DSP preset, "
            "source, and connection status."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    Tool(
        name="get_measurement_history",
        description=(
            "Return the last N calibration measurement sessions from the Pi's "
            "local database. Each session includes frequency response data, timestamp, and label.\n\n"
            "ALWAYS use format='compact' for bass calibration filter design — it encodes FR as "
            "'freq:spl,freq:spl,...' strings (~12 chars/point vs ~40 chars/point in full format). "
            "Combined with min_hz=20, max_hz=120, a 2-session compact response fits comfortably "
            "in context (~8KB vs 115KB full). "
            "Parse compact fr string: split on ',' then ':' to get (freq_hz, spl_db) pairs."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Number of sessions to return (default: 10)",
                    "default": 10,
                },
                "min_hz": {
                    "type": "number",
                    "description": (
                        "Low-frequency cutoff in Hz — only return data at or above this "
                        "frequency. For sub bass calibration use 20."
                    ),
                },
                "max_hz": {
                    "type": "number",
                    "description": (
                        "High-frequency cutoff in Hz — only return data at or below this "
                        "frequency. For sub bass calibration use 120."
                    ),
                },
                "decimation": {
                    "type": "integer",
                    "description": (
                        "Keep every Nth point (1 = all, 2 = every other, 4 = quarter). "
                        "Default: 1 (no decimation). With format='compact' and min_hz/max_hz, "
                        "decimation=1 already fits in context — only use higher values if you "
                        "want coarser resolution."
                    ),
                    "default": 1,
                },
                "format": {
                    "type": "string",
                    "enum": ["full", "compact"],
                    "description": (
                        "Output format. 'compact' (default): FR data as 'freq:spl,...' string "
                        "(~12 chars/point, recommended for filter design). 'full': separate "
                        "freq_hz[] and spl_db[] arrays (verbose, ~3x larger — only use if you "
                        "need raw arrays for downstream numerical work)."
                    ),
                    "default": "compact",
                },
                "include_phase": {
                    "type": "boolean",
                    "description": (
                        "When true (and format='full'), include phase_rad[] array alongside "
                        "freq_hz[]/spl_db[]. Default false — phase is only needed for sub "
                        "alignment and analyze_phase, so it's excluded from the per-iteration "
                        "EQ loop to save ~8K tokens per measurement."
                    ),
                    "default": False,
                },
            },
        },
    ),
    Tool(
        name="apply_eq",
        description=(
            "Apply EQ filters to DSP output(s). By default writes to all sub outputs "
            "(broadcast mode). Pass output_index to target a single output for per-sub EQ. "
            "Filters are validated by SafetyValidator before any hardware write — "
            "unsafe filters return {ok: false, error: 'SafetyValidator: ...'} rather than "
            "throwing. Each filter: {freq: Hz, gain_db: dB, q: float, type: 'peaking'|"
            "'low_shelf'|'high_shelf'|'hpf'}. A mandatory 18Hz HPF must always be included."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "filters": {
                    "type": "array",
                    "description": "List of EQ filter specifications",
                    "items": {
                        "type": "object",
                        "properties": {
                            "freq": {"type": "number", "description": "Centre/corner frequency in Hz"},
                            "gain_db": {"type": "number", "description": "Gain in dB (positive=boost, negative=cut)"},
                            "q": {"type": "number", "description": "Quality factor (ignored for hpf)"},
                            "type": {
                                "type": "string",
                                "enum": ["peaking", "low_shelf", "high_shelf", "hpf"],
                            },
                        },
                        "required": ["freq", "gain_db", "q", "type"],
                    },
                },
                "output_index": {
                    "type": "integer",
                    "description": (
                        "Target a single DSP output index (0-3) for per-sub EQ. "
                        "If omitted, writes to all configured sub outputs."
                    ),
                },
                "simulation_verified": {
                    "type": "boolean",
                    "description": (
                        "Set to true if the filter set was verified by simulate_eq "
                        "immediately before this apply call. Relaxes the per-iteration "
                        "change limit from +3 dB to +6 dB."
                    ),
                },
            },
            "required": ["filters"],
        },
    ),
    Tool(
        name="apply_input_eq",
        description=(
            "Apply EQ filters to the DSP input channel (shared across all outputs). "
            "Use this for the Harman target curve or any EQ that should affect all subs equally. "
            "Filters are validated by SafetyValidator. "
            "Each filter: {freq: Hz, gain_db: dB, q: float, type: 'peaking'|"
            "'low_shelf'|'high_shelf'|'hpf'}. A mandatory 18Hz HPF must always be included."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "filters": {
                    "type": "array",
                    "description": "List of EQ filter specifications",
                    "items": {
                        "type": "object",
                        "properties": {
                            "freq": {"type": "number", "description": "Centre/corner frequency in Hz"},
                            "gain_db": {"type": "number", "description": "Gain in dB (positive=boost, negative=cut)"},
                            "q": {"type": "number", "description": "Quality factor (ignored for hpf)"},
                            "type": {
                                "type": "string",
                                "enum": ["peaking", "low_shelf", "high_shelf", "hpf"],
                            },
                        },
                        "required": ["freq", "gain_db", "q", "type"],
                    },
                },
                "target_curve": {
                    "type": "object",
                    "description": "Optional: the optimization target curve for dashboard display. "
                        "Include when applying a target curve (e.g. Harman). "
                        "Shape: {type, reference_spl, band, points: [{freq, spl}]}",
                    "properties": {
                        "type": {"type": "string", "description": "Curve name, e.g. 'harman'"},
                        "reference_spl": {"type": "number", "description": "Anchor reference SPL in dB"},
                        "band": {"type": "array", "items": {"type": "number"}, "description": "[low_hz, high_hz]"},
                        "points": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "freq": {"type": "number"},
                                    "spl": {"type": "number"},
                                },
                            },
                        },
                    },
                },
                "simulation_verified": {
                    "type": "boolean",
                    "description": (
                        "Set to true if the filter set was verified by simulate_eq "
                        "immediately before this apply call. Relaxes the per-iteration "
                        "change limit from +3 dB to +6 dB."
                    ),
                },
            },
            "required": ["filters"],
        },
    ),
    Tool(
        name="calibrate_level",
        description=(
            "Auto-calibrate sweep level using predict-and-verify (2 sweeps). "
            "Targets a specific SPL at the mic (default 78 dB — typical listening level). "
            "Uses UMIK sensitivity from the cal file to convert dBFS ↔ SPL. "
            "Takes one probe sweep at a safe starting level, computes the exact gain "
            "correction to hit target_spl_db, and verifies with a second sweep. "
            "USB mode: adjusts miniDSP master gain. HDMI mode: adjusts AVR volume. "
            "Returns {ok, calibrated_master_gain_db, suggested_solo_gain_db, estimated_spl_db} (USB) or "
            "{ok, calibrated_volume_db, estimated_spl_db} (HDMI)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "target_spl_db": {
                    "type": "number",
                    "description": (
                        "Target peak level in dB SPL at the mic (default: 78). "
                        "78 dB is a typical listening level for home theater."
                    ),
                },
                "start_db": {
                    "type": "number",
                    "description": (
                        "Safe starting level for probe sweep in dB (default: -30). "
                        "Should be low enough to never clip."
                    ),
                },
                "max_volume_db": {
                    "type": "number",
                    "description": "HDMI only: maximum AVR volume ceiling in dB (default: 0 = reference)",
                },
            },
        },
    ),
    Tool(
        name="set_volume",
        description=(
            "Set the AVR volume to a specific level in dB. "
            "Range: approximately -80 to +18 dB. "
            "Returns {ok: true, level_db: N} on success, "
            "{ok: false, error: 'avr unreachable: ...'} on failure."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "level_db": {
                    "type": "number",
                    "description": "Target volume in dB (e.g. -30.0 for -30 dB)",
                }
            },
            "required": ["level_db"],
        },
    ),
    Tool(
        name="measure",
        description=(
            "Trigger a frequency response measurement using the UMIK microphone. "
            "Takes a sweep measurement via PyTTa, saves to the session store, "
            "and returns the session ID. Use get_fr_summary() for 1/3-octave FR data (compact), "
            "or get_measurement_history() for full-resolution FR. "
            "IMPORTANT: Always pass label and position so measurements are identifiable in the dashboard."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "label": {
                    "type": "string",
                    "description": (
                        "Descriptive label for this measurement. Use a consistent format: "
                        "'combined' (all subs), 'sub1-solo', 'sub2-solo', 'subcrawl-pos1', "
                        "'baseline', 'iter-1', 'iter-2', etc."
                    ),
                },
                "position": {
                    "type": "string",
                    "description": (
                        "Listening/mic position description. Examples: "
                        "'MLP' (main listening position), 'front-left', 'nearfield', "
                        "'subcrawl-candidate-1', 'seat-2'."
                    ),
                },
                "target_curve": {
                    "type": "object",
                    "description": (
                        "The optimization target being pursued in this calibration iteration. "
                        "Pass ONLY during a calibration loop — include the reference_spl and band "
                        "that were used to compute the filters for this iteration. "
                        "Omit (or pass null) for standalone/diagnostic measurements; those sessions "
                        "will not show a dB delta on the dashboard. "
                        "Shape: {type: 'harman'|'flat', reference_spl: float, band: [min_hz, max_hz], "
                        "points: [{freq, spl}, ...]}"
                    ),
                },
            },
        },
    ),
    Tool(
        name="fetch_recipe",
        description=(
            "Fetch a calibration recipe by name (e.g. 'core/harman-bass'). "
            "Recipes are Claude prompt-documents — markdown files you read and follow "
            "while calling MCP tools. Returns {ok: true, content: '...'} or "
            "{ok: false, error: 'recipe not found: ...'}"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Recipe path relative to recipes/ dir, without .md extension. E.g. 'core/harman-bass'",
                }
            },
            "required": ["name"],
        },
    ),
    Tool(
        name="get_calibration_runs",
        description=(
            "Return calibration run history. Each run shows recipe, target curve, "
            "convergence status, iterations, and RMS deviation from target. "
            "Pass run_id to get full detail including per-iteration filter data."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Number of runs to return (default: 10)",
                    "default": 10,
                },
                "run_id": {
                    "type": "integer",
                    "description": "If provided, return full detail for this run only",
                },
            },
        },
    ),
    Tool(
        name="save_calibration_run",
        description=(
            "Create a new calibration run record. Returns the run_id for use with "
            "save_calibration_iteration and update_calibration_run. Call this at the "
            "start of a calibration session. Optionally captures equipment state "
            "(get_device_state output) for later review."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "recipe_name": {
                    "type": "string",
                    "description": "Name of the recipe being run (e.g. 'bass-calibration')",
                },
                "target": {
                    "type": "string",
                    "description": "Target curve name (e.g. 'harman-bass', 'flat')",
                },
                "device_state": {
                    "type": "object",
                    "description": (
                        "Snapshot of the hardware state at run start. Pass the output "
                        "of get_device_state here to record AVR volume, DSP preset, "
                        "input routing, and EQ state alongside the run."
                    ),
                },
                "run_type": {
                    "type": "string",
                    "enum": ["calibration", "validation"],
                    "description": (
                        "'calibration' (default) for iterative EQ runs, "
                        "'validation' for read-only measurement sessions."
                    ),
                },
            },
            "required": ["recipe_name", "target"],
        },
    ),
    Tool(
        name="update_calibration_run",
        description=(
            "Update a calibration run with final results (convergence, RMS, etc.). "
            "Call this when calibration completes or fails."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "integer",
                    "description": "Run ID returned by save_calibration_run",
                },
                "converged": {
                    "type": "boolean",
                    "description": "Whether the calibration converged to the target",
                },
                "iterations_run": {
                    "type": "integer",
                    "description": "Total number of iterations completed",
                },
                "baseline_rms": {
                    "type": "number",
                    "description": "RMS deviation before calibration",
                },
                "final_rms": {
                    "type": "number",
                    "description": "RMS deviation after calibration",
                },
                "error": {
                    "type": "string",
                    "description": "Error message if calibration failed",
                },
                "target_curve_data": {
                    "type": "object",
                    "description": "Full target curve data used during calibration",
                },
                "sessions": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "For validation runs: list of {session_id, label} dicts "
                        "recording each measurement taken."
                    ),
                },
            },
            "required": ["run_id", "converged", "iterations_run"],
        },
    ),
    Tool(
        name="save_calibration_iteration",
        description=(
            "Save one iteration of a calibration run. Records RMS before/after, "
            "filters proposed, filters applied, and safety validation results."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "integer",
                    "description": "Run ID returned by save_calibration_run",
                },
                "iteration": {
                    "type": "integer",
                    "description": "Iteration number (1-based)",
                },
                "rms_before": {
                    "type": "number",
                    "description": "RMS deviation before this iteration",
                },
                "rms_after": {
                    "type": "number",
                    "description": "RMS deviation after this iteration",
                },
                "filters_proposed": {
                    "type": "array",
                    "description": "Filter set proposed (before safety validation)",
                    "items": {"type": "object"},
                },
                "filters_applied": {
                    "type": "array",
                    "description": "Filter set actually applied (after safety validation)",
                    "items": {"type": "object"},
                },
                "safety_ok": {
                    "type": "boolean",
                    "description": "Whether the proposed filters passed safety validation",
                },
                "safety_error": {
                    "type": "string",
                    "description": "Safety validation error message if safety_ok is false",
                },
            },
            "required": ["run_id", "iteration", "rms_before", "rms_after",
                         "filters_proposed", "filters_applied"],
        },
    ),
    Tool(
        name="get_config",
        description=(
            "Return the current config.yaml as a dict. Includes all sections: "
            "denon, minidsp, mic, sub, measurement, connections."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    Tool(
        name="set_config",
        description=(
            "Deep-merge updates into config.yaml. Pass a dict of sections to update. "
            "Example: {\"denon\": {\"host\": \"192.168.1.100\"}} sets the Denon IP. "
            "Existing keys not mentioned in updates are preserved."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "updates": {
                    "type": "object",
                    "description": "Dict of config sections to deep-merge. Keys: denon, minidsp, mic, sub, measurement.",
                }
            },
            "required": ["updates"],
        },
    ),
    Tool(
        name="discover_avr",
        description=(
            "Run an SSDP scan to find Denon/Marantz AVRs on the local network. "
            "Returns a list of IP addresses. Timeout: 10 seconds. "
            "Use this during setup to auto-detect the AVR before calling "
            "set_config to save the host."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    Tool(
        name="mute_output",
        description=(
            "Mute DSP outputs by setting gain to -127 dB. "
            "Use this to isolate individual subs during calibration "
            "(e.g. mute output 1 to measure sub on output 0 only)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "output_indices": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Output indices to mute",
                },
            },
            "required": ["output_indices"],
        },
    ),
    Tool(
        name="unmute_output",
        description=(
            "Unmute DSP outputs by restoring gain to 0 dB. "
            "Always unmute all outputs when calibration is done."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "output_indices": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Output indices to unmute",
                },
            },
            "required": ["output_indices"],
        },
    ),
    Tool(
        name="end_sweep_session",
        description=(
            "End the persistent USB sweep session and restore the miniDSP source "
            "to its original state (typically Analog). Call after calibration is "
            "done or on error. Safe to call if no session is active."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    Tool(
        name="set_delay",
        description=(
            "Set delay for a single DSP output in milliseconds. "
            "Used during sub alignment to time-align multiple subs. "
            "Range: 0-10 ms typical for sub alignment."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "output_index": {
                    "type": "integer",
                    "description": "DSP output index (0-3)",
                },
                "delay_ms": {
                    "type": "number",
                    "description": "Delay in milliseconds",
                },
            },
            "required": ["output_index", "delay_ms"],
        },
    ),
    Tool(
        name="set_polarity",
        description=(
            "Set polarity for a single DSP output. "
            "inverted=true flips the phase 180°. Used during sub alignment "
            "when one sub is out of phase with others (dip where others peak)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "output_index": {
                    "type": "integer",
                    "description": "DSP output index (0-3)",
                },
                "inverted": {
                    "type": "boolean",
                    "description": "true = inverted (180° flip), false = normal",
                },
            },
            "required": ["output_index", "inverted"],
        },
    ),
    Tool(
        name="get_output_state",
        description=(
            "Return per-output gain_db, delay_ms, and polarity_inverted from "
            "in-memory driver tracking. Reflects values set via set_output_gain, "
            "set_delay, set_polarity during this session. "
            "Note: hardware state from before this server started is not readable "
            "from minidspd — only changes made in this session are tracked."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    Tool(
        name="analyze_ir",
        description=(
            "Extract IR peak time, polarity sign, and SPL from a stored measurement session. "
            "Use this after measuring each sub solo to get the data needed for alignment: "
            "peak_time_s is the travel-time from sub to mic; "
            "subtract the earliest arrival from the latest to get the delay offset to apply; "
            "peak_sign tells you polarity (if it differs from the reference sub, flip it); "
            "spl_db is the relative level for gain matching. "
            "Workflow: mute_output → measure → analyze_ir → unmute_output → repeat per sub → "
            "compute offsets → set_delay / set_polarity / set_output_gain."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "integer",
                    "description": "Session ID to analyse. Omit to analyse the most recent session.",
                },
                "search_window_ms": {
                    "type": "number",
                    "description": (
                        "Time window to search for IR peak (default: 50ms = 17.5m at 343m/s). "
                        "Increase if sub is more than ~5m from mic."
                    ),
                    "default": 50.0,
                },
            },
        },
    ),
    Tool(
        name="apply_fir",
        description=(
            "Write FIR filter coefficients to a single DSP output. "
            "Coefficients are floats normalized so the peak is <= 1.0. "
            "Tap-count ceiling and FIR sample rate come from eq_capabilities "
            "(fir_max_taps_per_output and fir_sample_rate_hz). "
            "Use after analyze_decay to shorten room-mode ringing that PEQ cannot fix — "
            "FIR corrects the time-domain decay; PEQ only reduces the peak magnitude. "
            "After writing, get_output_state will show fir_taps = len(coefficients)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "output_index": {
                    "type": "integer",
                    "description": "DSP output index (0-3)",
                },
                "coefficients": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": (
                        "FIR filter coefficients as floats. "
                        "Length must be <= fir_max_taps_per_output from eq_capabilities. "
                        "Sample rate must match fir_sample_rate_hz. "
                        "Normalize so peak abs value <= 1.0."
                    ),
                },
            },
            "required": ["output_index", "coefficients"],
        },
    ),
    Tool(
        name="clear_fir",
        description=(
            "Clear FIR coefficients on a DSP output, resetting it to passthrough. "
            "Use before loading new coefficients or to undo a FIR pass."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "output_index": {
                    "type": "integer",
                    "description": "DSP output index (0-3)",
                },
            },
            "required": ["output_index"],
        },
    ),
    Tool(
        name="set_master_gain",
        description=(
            "Set the miniDSP master output gain in dB. Range: -127 to 0 dB. "
            "This is a global attenuation applied before all outputs — use it to "
            "control sweep playback volume without disturbing per-output alignment gains. "
            "Set to e.g. -30 for quiet sweeps, restore to 0 when done."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "gain_db": {
                    "type": "number",
                    "description": "Master gain in dB. Range: -127 to 0.",
                },
            },
            "required": ["gain_db"],
        },
    ),
    Tool(
        name="set_output_gain",
        description=(
            "Set gain for a single DSP output in dB. Range: -127 to +6 dB. "
            "Use this for per-sub level trimming during calibration — after measuring "
            "each sub solo, trim quieter subs up so all subs match the loudest. "
            "Avoid gains above 0 dB (risks clipping). "
            "mute_output / unmute_output are preferred for temporary silencing."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "output_index": {
                    "type": "integer",
                    "description": "DSP output index (0-3)",
                },
                "gain_db": {
                    "type": "number",
                    "description": "Gain in dB. Range: -127 to +6.",
                },
            },
            "required": ["output_index", "gain_db"],
        },
    ),
    Tool(
        name="configure_matrix",
        description=(
            "Configure the miniDSP routing matrix: route the active analog input "
            "to enabled outputs (skipping defective/unused ones) and mute the other input. "
            "Call this if subs are silent during calibration (no signal reaching DSP). "
            "Uses active_input from config by default, or pass active_input to override."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "active_input": {
                    "type": "integer",
                    "description": "Input index to route to all outputs (0 or 1). Defaults to config value.",
                },
            },
        },
    ),
    Tool(
        name="analyze_decay",
        description=(
            "Analyze room-mode T60 decay in the impulse response from a stored measurement. "
            "Returns a list of ringing modes sorted by priority (T60 × peak_amplitude), "
            "each with freq_hz, t60_ms, peak_db, and suggested_q for EQ targeting. "
            "Modes with T60 > 300ms are flagged. "
            "Correction options depend on eq_capabilities from get_config: "
            "if fir_capable=true, use apply_fir to shorten the decay duration; "
            "if fir_capable=false (miniDSP 2x4 HD), use apply_eq with suggested_q to "
            "reduce peak energy — the mode will still ring at reduced level. "
            "Call after measure() to identify problem frequencies before designing EQ filters."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "integer",
                    "description": "Session ID to analyse. Omit to analyse the most recent session.",
                },
                "t60_threshold_ms": {
                    "type": "number",
                    "description": "Minimum T60 in ms to flag as a ringing mode (default: 300).",
                    "default": 300.0,
                },
                "freq_min": {
                    "type": "number",
                    "description": "Lower frequency bound in Hz (default: 20).",
                    "default": 20.0,
                },
                "freq_max": {
                    "type": "number",
                    "description": "Upper frequency bound in Hz (default: 200).",
                    "default": 200.0,
                },
            },
        },
    ),
    Tool(
        name="check_system",
        description=(
            "Run pre-flight hardware checks: config, miniDSP (USB + daemon), "
            "Denon AVR (with auto-discovery), and signal path sync. "
            "Returns {all_passed: bool, checks: [{name, passed, detail, error}]}. "
            "Call this before starting any calibration."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    Tool(
        name="get_fr_summary",
        description=(
            "Return frequency response data downsampled to 1/3-octave bands (11 points "
            "from 20-200Hz). Much smaller than get_measurement_history — use this when you "
            "need FR shape for analysis, convergence checks, or Harman target comparison. "
            "Returns per-session: bands[{freq_hz, spl_db}], peak_spl, freq_at_peak, ir_summary."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Specific session IDs to retrieve. Omit to get the most recent sessions.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of recent sessions to return if session_ids not given (default: 5).",
                    "default": 5,
                },
            },
        },
    ),
    Tool(
        name="compute_deviation",
        description=(
            "Compute RMS deviation of a measurement session against a target curve. "
            "Automatically excludes null zones (deep cancellation dips) and below-port rolloff "
            "from the RMS calculation. Returns rms_db, converged (rms < threshold), per-band summary "
            "at configurable resolution (sixth_octave default ~12 bands), and null zone identification. "
            "Use this after each EQ iteration to check convergence."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "integer",
                    "description": "Measurement session ID to evaluate",
                },
                "target_curve": {
                    "type": "object",
                    "description": (
                        "Target curve with points. Shape: {points: [{freq, spl}], band: [lo_hz, hi_hz]}. "
                        "For Harman bass target anchored at ref dB: "
                        "points=[{freq:25,spl:ref+5},{freq:31,spl:ref+4},...,{freq:80,spl:ref}]"
                    ),
                    "properties": {
                        "points": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "freq": {"type": "number"},
                                    "spl": {"type": "number"},
                                },
                            },
                        },
                        "band": {
                            "type": "array",
                            "items": {"type": "number"},
                            "description": "[lo_hz, hi_hz] — frequency range to evaluate",
                        },
                    },
                },
                "null_threshold_db": {
                    "type": "number",
                    "description": (
                        "Frequencies where measured SPL is this many dB below the band average "
                        "are classified as nulls and excluded from RMS. Default: 15."
                    ),
                },
                "port_rolloff_hz": {
                    "type": "number",
                    "description": (
                        "Frequencies below this are excluded as below-port rolloff. Default: 28 Hz "
                        "(SVS PB12-NSD port tuning ~22 Hz, rolloff becomes steep by 28 Hz)."
                    ),
                },
                "resolution": {
                    "type": "string",
                    "enum": ["third_octave", "sixth_octave", "twelfth_octave"],
                    "description": (
                        "Summary band density. 'third_octave': ~6 bands (coarse), "
                        "'sixth_octave': ~12 bands (default), "
                        "'twelfth_octave': ~24 bands (full detail). Default: sixth_octave."
                    ),
                },
                "convergence_threshold": {
                    "type": "number",
                    "description": (
                        "RMS threshold in dB below which the result is marked 'converged'. "
                        "Default: 1.5. Adjust based on recipe's convergence goals."
                    ),
                },
            },
            "required": ["session_id", "target_curve"],
        },
    ),
    Tool(
        name="anchor_target",
        description=(
            "Compute the optimal reference SPL for anchoring a target curve against a baseline "
            "measurement. Given target offsets (relative dB at each frequency) and a measured FR, "
            "finds the reference_spl that keeps max required boost within the safety limit. "
            "Returns the anchored target curve with absolute SPL values, per-frequency error summary, "
            "and null zone identification. Call this before designing input PEQ filters for a target curve."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "integer",
                    "description": "Baseline measurement session ID to anchor against",
                },
                "target_offsets": {
                    "type": "array",
                    "description": (
                        "Target curve as relative offsets. Each item: {freq_hz, offset_db}. "
                        "offset_db is relative to the reference frequency (e.g. 80 Hz = 0 dB). "
                        "Example for Cinema Bass: [{freq_hz:20,offset_db:10},{freq_hz:25,offset_db:9},...,{freq_hz:80,offset_db:0}]"
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "freq_hz": {"type": "number"},
                            "offset_db": {"type": "number"},
                        },
                        "required": ["freq_hz", "offset_db"],
                    },
                },
                "band": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": (
                        "[lo_hz, hi_hz] — frequency range to evaluate. "
                        "Defaults to the target offset frequency range."
                    ),
                },
                "max_boost_db": {
                    "type": "number",
                    "description": "Maximum allowed boost in dB. Default: 6.0 (SafetyValidator limit).",
                },
                "null_threshold_db": {
                    "type": "number",
                    "description": (
                        "Frequencies where measured SPL is this many dB below the band average "
                        "are classified as nulls and excluded. Default: 15."
                    ),
                },
                "port_rolloff_hz": {
                    "type": "number",
                    "description": (
                        "Frequencies below this are excluded as below-port rolloff. "
                        "Default: 28 Hz."
                    ),
                },
            },
            "required": ["session_id", "target_offsets"],
        },
    ),
    Tool(
        name="compare_sessions",
        description=(
            "Compare frequency responses between two measurement sessions. "
            "Returns per-1/3-octave-band deltas (session B minus session A) and statistics. "
            "Use this to verify EQ changes had the intended effect, or to compare "
            "solo vs combined measurements."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_a": {
                    "type": "integer",
                    "description": "First (earlier/before) session ID",
                },
                "session_b": {
                    "type": "integer",
                    "description": "Second (later/after) session ID",
                },
                "min_hz": {
                    "type": "number",
                    "description": "Lower frequency bound in Hz (default: 20)",
                },
                "max_hz": {
                    "type": "number",
                    "description": "Upper frequency bound in Hz (default: 120)",
                },
            },
            "required": ["session_a", "session_b"],
        },
    ),
    Tool(
        name="simulate_eq",
        description=(
            "Predict FR after applying proposed PEQ filters to a measurement. "
            "Pure simulation — no hardware writes. Design filters yourself, then call this "
            "to see the predicted result. Iterate in simulation before applying to hardware. "
            "Returns compact predicted FR string and per-point original/predicted/correction values."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "integer",
                    "description": "Measurement session ID to simulate against",
                },
                "filters": {
                    "type": "array",
                    "description": "Proposed PEQ filters to simulate",
                    "items": {
                        "type": "object",
                        "properties": {
                            "freq": {"type": "number"},
                            "gain_db": {"type": "number"},
                            "q": {"type": "number"},
                            "type": {"type": "string", "enum": ["peaking", "low_shelf", "high_shelf", "hpf"]},
                        },
                        "required": ["freq", "gain_db", "q", "type"],
                    },
                },
                "min_hz": {"type": "number", "description": "Lower frequency bound (default: 20)"},
                "max_hz": {"type": "number", "description": "Upper frequency bound (default: 120)"},
            },
            "required": ["session_id", "filters"],
        },
    ),
    Tool(
        name="optimize_q",
        description=(
            "Find the optimal Q for a peaking filter. You decide the frequency and gain "
            "(cut or boost); this tool numerically searches for the Q that minimizes "
            "residual error in the surrounding band. Returns optimal Q and predicted effect."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "integer",
                    "description": "Measurement session to optimize against",
                },
                "freq_hz": {
                    "type": "number",
                    "description": "Center frequency of the filter in Hz",
                },
                "target_gain_db": {
                    "type": "number",
                    "description": "Desired gain in dB (negative=cut, positive=boost)",
                },
                "band_hz": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "[lo_hz, hi_hz] band to minimize error in. Default: +/- 1 octave.",
                },
            },
            "required": ["session_id", "freq_hz", "target_gain_db"],
        },
    ),
    Tool(
        name="analyze_phase",
        description=(
            "Minimum-phase decomposition and fixability analysis. Classifies each "
            "1/3-octave band as 'fixable' (minimum-phase, PEQ handles peak), 'partial' "
            "(modal ringing — FIR shortens decay better than PEQ, PEQ still reduces peak), "
            "or 'geometry' (near-π phase offset, cancellation — reposition instead). "
            "Thresholds are frequency-scaled (¼- and ½-wavelength with 10/25 ms floors). "
            "fixable=True covers 'fixable' and 'partial'; False means 'geometry'. "
            "Returns per-1/3-octave: classification, fixable, excess_group_delay_ms, "
            "min_phase_group_delay_ms, spl_db. Use BEFORE designing filters."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "integer",
                    "description": "Measurement session ID to analyze",
                },
                "min_hz": {"type": "number", "description": "Lower frequency bound (default: 20)"},
                "max_hz": {"type": "number", "description": "Upper frequency bound (default: 120)"},
            },
            "required": ["session_id"],
        },
    ),
    Tool(
        name="compare_sub_phase",
        description=(
            "Compare phase relationship between two solo sub measurements. "
            "Returns per-1/3-octave: phase difference, predicted coherent sum, and "
            "reinforcement classification (reinforcing/partial/cancelling). "
            "Use before alignment to understand where subs help vs fight each other. "
            "Cancelling bands cannot be fixed with EQ — consider delay/polarity/repositioning."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_a": {
                    "type": "integer",
                    "description": "Solo measurement session for sub A",
                },
                "session_b": {
                    "type": "integer",
                    "description": "Solo measurement session for sub B",
                },
                "min_hz": {"type": "number", "description": "Lower frequency bound (default: 20)"},
                "max_hz": {"type": "number", "description": "Upper frequency bound (default: 120)"},
            },
            "required": ["session_a", "session_b"],
        },
    ),
    Tool(
        name="design_fir",
        description=(
            "Design FIR correction filter coefficients. You decide the strategy: phase mode "
            "(minimum/linear/mixed), tap count, and frequency focus. The tool computes "
            "coefficients and returns them with a predicted effect and pre-ringing estimate. "
            "Pass coefficients to apply_fir(output_index, coefficients) to write to hardware. "
            "Read fir_min_taps, fir_max_taps_per_output, and fir_sample_rate_hz from "
            "eq_capabilities in get_config — limits depend on the active DSP."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "integer",
                    "description": "Measurement session to design correction for",
                },
                "target_curve": {
                    "type": "object",
                    "description": "Optional target curve. Omit for flat correction.",
                    "properties": {
                        "points": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {"freq": {"type": "number"}, "spl": {"type": "number"}},
                            },
                        },
                    },
                },
                "num_taps": {
                    "type": "integer",
                    "description": "Number of FIR taps. Must fall within [fir_min_taps, fir_max_taps_per_output] from eq_capabilities. More taps = better low-freq resolution but uses more DSP. Default: 1024.",
                },
                "phase_mode": {
                    "type": "string",
                    "enum": ["minimum", "linear", "mixed"],
                    "description": "minimum=no pre-ringing (safest), linear=corrects phase (adds pre-ringing), mixed=compromise. Default: minimum.",
                },
                "freq_focus_hz": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "[lo_hz, hi_hz] — only correct within this range, taper to zero outside. Omit to correct full range.",
                },
            },
            "required": ["session_id"],
        },
    ),
    # ── LLM filter-design math tools ─────────────────────────────────────────
    Tool(
        name="evaluate_transfer_function",
        description=(
            "Evaluate combined PEQ transfer function at specific frequencies. "
            "Pure math — no measurement data. Returns per-filter breakdown showing "
            "each filter's contribution in dB at each query frequency. Use to check "
            "filter interaction before running a full simulation."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "filters": {
                    "type": "array",
                    "description": "PEQ filter set to evaluate",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": ["peaking", "low_shelf", "high_shelf", "hpf"]},
                            "freq": {"type": "number"},
                            "gain_db": {"type": "number"},
                            "q": {"type": "number"},
                        },
                        "required": ["type", "freq", "gain_db", "q"],
                    },
                },
                "query_freqs": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "Frequencies (Hz) at which to evaluate the transfer function",
                },
            },
            "required": ["filters", "query_freqs"],
        },
    ),
    Tool(
        name="per_filter_contribution",
        description=(
            "Show each filter's individual contribution at specific frequencies, "
            "relative to a measurement session baseline. Returns baseline SPL, "
            "per-filter correction, and predicted SPL at each frequency. Use to "
            "identify which filter is responsible for an over-cut or under-cut."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "filters": {
                    "type": "array",
                    "description": "PEQ filter set to analyze",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string"},
                            "freq": {"type": "number"},
                            "gain_db": {"type": "number"},
                            "q": {"type": "number"},
                        },
                    },
                },
                "session_id": {
                    "type": "integer",
                    "description": "Baseline measurement session ID",
                },
                "query_freqs": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "Frequencies to evaluate. Omit for sixth-octave centres 20-120 Hz.",
                },
            },
            "required": ["filters", "session_id"],
        },
    ),
    Tool(
        name="interpolate_optimal_gain",
        description=(
            "Interpolate the optimal gain for a filter from prior iteration data. "
            "Given 2+ (gain_applied, error_measured) pairs, fits a line and finds "
            "where error = 0. Eliminates guess-and-check iteration cycles."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "freq": {"type": "number", "description": "Filter centre frequency"},
                "q": {"type": "number", "description": "Filter Q"},
                "filter_type": {"type": "string", "description": "Filter type (peaking, low_shelf, etc.)"},
                "measured_errors": {
                    "type": "array",
                    "description": "Array of {gain_applied, error_measured} from prior iterations",
                    "items": {
                        "type": "object",
                        "properties": {
                            "gain_applied": {"type": "number", "description": "Gain in dB that was applied"},
                            "error_measured": {"type": "number", "description": "Error in dB that was measured"},
                        },
                        "required": ["gain_applied", "error_measured"],
                    },
                },
            },
            "required": ["freq", "q", "filter_type", "measured_errors"],
        },
    ),
    Tool(
        name="sensitivity_analysis",
        description=(
            "Compute sensitivity of RMS to each filter parameter. Perturbs gain, "
            "frequency, and Q of each filter and reports ∂RMS/∂param. Tells you "
            "which parameters matter most — focus adjustments on high-sensitivity params."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "filters": {
                    "type": "array",
                    "description": "Current PEQ filter set",
                    "items": {"type": "object"},
                },
                "session_id": {
                    "type": "integer",
                    "description": "Baseline measurement session ID",
                },
                "target_curve": {
                    "type": "object",
                    "description": "Target curve with points and band",
                    "properties": {
                        "points": {"type": "array", "items": {"type": "object"}},
                        "band": {"type": "array", "items": {"type": "number"}},
                    },
                },
                "perturbation_db": {
                    "type": "number",
                    "description": "Perturbation size in dB for gain (default: 0.5)",
                },
            },
            "required": ["filters", "session_id", "target_curve"],
        },
    ),
    Tool(
        name="fit_correction_filter",
        description=(
            "Find the optimal single PEQ filter to minimize RMS error in a frequency "
            "range. You decide WHICH region and constraints; this tool does the "
            "numerical optimization to find the best (freq, gain, Q). Returns the "
            "filter parameters and predicted RMS improvement."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "integer",
                    "description": "Measurement session to optimize against",
                },
                "target_curve": {
                    "type": "object",
                    "description": "Target curve with points",
                    "properties": {
                        "points": {"type": "array", "items": {"type": "object"}},
                        "band": {"type": "array", "items": {"type": "number"}},
                    },
                },
                "freq_range": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "[lo_hz, hi_hz] — frequency range to optimize within",
                },
                "constraints": {
                    "type": "object",
                    "description": "Optional: {max_boost_db, min_q, max_q, filter_type}",
                    "properties": {
                        "max_boost_db": {"type": "number"},
                        "min_q": {"type": "number"},
                        "max_q": {"type": "number"},
                        "filter_type": {"type": "string"},
                    },
                },
            },
            "required": ["session_id", "target_curve", "freq_range"],
        },
    ),
    Tool(
        name="predict_rms",
        description=(
            "Predict RMS deviation after applying filters — simulate_eq + compute_deviation "
            "in one call. Returns predicted RMS, convergence status, and per-band errors "
            "without touching hardware. Use to quickly evaluate candidate filter sets."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "integer",
                    "description": "Baseline measurement session",
                },
                "filters": {
                    "type": "array",
                    "description": "Proposed PEQ filters",
                    "items": {"type": "object"},
                },
                "target_curve": {
                    "type": "object",
                    "description": "Target curve with points and band",
                    "properties": {
                        "points": {"type": "array", "items": {"type": "object"}},
                        "band": {"type": "array", "items": {"type": "number"}},
                    },
                },
                "convergence_threshold": {
                    "type": "number",
                    "description": "RMS threshold for convergence (default: 1.5)",
                },
                "null_threshold_db": {
                    "type": "number",
                    "description": "Null detection threshold (default: 15)",
                },
                "port_rolloff_hz": {
                    "type": "number",
                    "description": "Below-port rolloff exclusion (default: 28)",
                },
            },
            "required": ["session_id", "filters", "target_curve"],
        },
    ),
    Tool(
        name="play_and_measure_fft",
        description=(
            "Play multitone clusters via HDMI multichannel while recording from UMIK, "
            "then return per-frequency SPL and THD. Used for headroom/amp clipping testing.\n\n"
            "Each call represents one volume step — the LLM manages the volume stepping "
            "loop via set_volume. The tool synthesizes tones, plays them, records "
            "simultaneously, and extracts per-tone SPL + THD via Welch FFT."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "channel_assignments": {
                    "type": "object",
                    "description": (
                        "HDMI channel (1-based, as string key) → list of tone frequencies in Hz. "
                        "E.g. {\"1\": [500, 800, 1200], \"2\": [530, 830, 1230]}. "
                        "Each channel gets a unique multitone cluster for FFT isolation."
                    ),
                    "additionalProperties": {
                        "type": "array",
                        "items": {"type": "number"},
                    },
                },
                "duration_s": {
                    "type": "number",
                    "description": "Playback + recording duration per step in seconds (default: 2.0)",
                    "default": 2.0,
                },
                "amplitude": {
                    "type": "number",
                    "description": "Peak amplitude 0-1 for tone synthesis (default: 0.5)",
                    "default": 0.5,
                },
                "fft_size": {
                    "type": "integer",
                    "description": (
                        "FFT window size for Welch analysis (default: 8192). "
                        "8192 @ 48kHz = 5.86Hz resolution."
                    ),
                    "default": 8192,
                },
            },
            "required": ["channel_assignments"],
        },
    ),
    Tool(
        name="assign_headroom_tones",
        description=(
            "Assign non-overlapping multitone clusters to speakers for headroom testing. "
            "Given each speaker's flat passband, assigns 3-5 tones per speaker with "
            "constraints: minimum inter-speaker spacing (30Hz default), no tone below 200Hz, "
            "no harmonic cross-talk between speakers.\n\n"
            "Returns assignments in the format expected by play_and_measure_fft."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "speaker_passbands": {
                    "type": "object",
                    "description": (
                        "Speaker role → passband. E.g. "
                        "{\"left\": {\"low_hz\": 80, \"high_hz\": 18000}, "
                        "\"center\": {\"low_hz\": 100, \"high_hz\": 18000}}"
                    ),
                    "additionalProperties": {
                        "type": "object",
                        "properties": {
                            "low_hz": {"type": "number"},
                            "high_hz": {"type": "number"},
                        },
                    },
                },
                "tones_per_speaker": {
                    "type": "integer",
                    "description": "Number of tones per speaker (default: 4)",
                    "default": 4,
                },
                "min_spacing_hz": {
                    "type": "number",
                    "description": "Minimum gap between tones on different speakers in Hz (default: 30)",
                    "default": 30.0,
                },
                "min_frequency_hz": {
                    "type": "number",
                    "description": "Absolute minimum tone frequency in Hz (default: 200)",
                    "default": 200.0,
                },
            },
            "required": ["speaker_passbands"],
        },
    ),
]


@server.list_tools()
async def list_tools() -> list[Tool]:
    return _TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    result: dict
    if name == "get_device_state":
        result = await _tool_get_device_state()
    elif name == "get_measurement_history":
        min_hz = arguments.get("min_hz")
        max_hz = arguments.get("max_hz")
        result = await _tool_get_measurement_history(
            limit=int(arguments.get("limit", 10)),
            min_hz=float(min_hz) if min_hz is not None else None,
            max_hz=float(max_hz) if max_hz is not None else None,
            decimation=int(arguments.get("decimation", 1)),
            fmt=arguments.get("format", "compact"),
            include_phase=bool(arguments.get("include_phase", False)),
        )
    elif name == "apply_eq":
        output_index = arguments.get("output_index")
        if output_index is not None:
            output_index = int(output_index)
        result = await _tool_apply_eq(
            arguments.get("filters", []),
            output_index=output_index,
            simulation_verified=bool(arguments.get("simulation_verified", False)),
        )
    elif name == "apply_input_eq":
        result = await _tool_apply_input_eq(
            arguments.get("filters", []),
            target_curve=arguments.get("target_curve"),
            simulation_verified=bool(arguments.get("simulation_verified", False)),
        )
    elif name in ("set_volume", "avr_set_volume", "set_denon_volume"):
        result = await _tool_avr_set_volume(float(arguments["level_db"]))
    elif name in ("measure", "trigger_measurement"):
        result = await _tool_trigger_measurement(
            label=arguments.get("label"),
            position=arguments.get("position"),
            target_curve=arguments.get("target_curve"),
        )
    elif name == "calibrate_level":
        result = await _tool_calibrate_level(
            target_spl_db=float(arguments.get("target_spl_db", 78.0)),
            start_db=float(arguments.get("start_db", -30.0)),
            max_volume_db=float(arguments.get("max_volume_db", 0.0)),
        )
    elif name == "fetch_recipe":
        result = await _tool_fetch_recipe(arguments["name"])
    elif name == "get_calibration_runs":
        result = await _tool_get_calibration_runs(
            limit=int(arguments.get("limit", 10)),
            run_id=arguments.get("run_id"),
        )
    elif name == "save_calibration_run":
        result = await _tool_save_calibration_run(
            recipe_name=arguments["recipe_name"],
            target=arguments["target"],
            device_state=arguments.get("device_state"),
            run_type=arguments.get("run_type", "calibration"),
        )
    elif name == "update_calibration_run":
        result = await _tool_update_calibration_run(
            run_id=int(arguments["run_id"]),
            converged=bool(arguments["converged"]),
            iterations_run=int(arguments["iterations_run"]),
            baseline_rms=float(arguments["baseline_rms"]) if arguments.get("baseline_rms") is not None else None,
            final_rms=float(arguments["final_rms"]) if arguments.get("final_rms") is not None else None,
            error=arguments.get("error", ""),
            target_curve_data=arguments.get("target_curve_data"),
            sessions=arguments.get("sessions"),
        )
    elif name == "save_calibration_iteration":
        result = await _tool_save_calibration_iteration(
            run_id=int(arguments["run_id"]),
            iteration=int(arguments["iteration"]),
            rms_before=float(arguments["rms_before"]),
            rms_after=float(arguments["rms_after"]),
            filters_proposed=arguments.get("filters_proposed", []),
            filters_applied=arguments.get("filters_applied", []),
            safety_ok=bool(arguments.get("safety_ok", True)),
            safety_error=arguments.get("safety_error", ""),
        )
    elif name == "get_config":
        result = await _tool_get_config()
    elif name == "set_config":
        result = await _tool_set_config(arguments["updates"])
    elif name == "discover_avr":
        result = await _tool_discover_avr()
    elif name == "mute_output":
        result = await _tool_mute_output(arguments["output_indices"])
    elif name == "unmute_output":
        result = await _tool_unmute_output(arguments["output_indices"])
    elif name == "end_sweep_session":
        result = await _tool_end_sweep_session()
    elif name == "set_delay":
        result = await _tool_set_delay(
            output_index=int(arguments["output_index"]),
            delay_ms=float(arguments["delay_ms"]),
        )
    elif name == "set_polarity":
        result = await _tool_set_polarity(
            output_index=int(arguments["output_index"]),
            inverted=arguments["inverted"] is True,
        )
    elif name == "get_output_state":
        result = await _tool_get_output_state()
    elif name == "analyze_ir":
        result = await _tool_analyze_ir(
            session_id=int(arguments["session_id"]) if "session_id" in arguments else None,
            search_window_ms=float(arguments.get("search_window_ms", 50.0)),
        )
    elif name == "apply_fir":
        result = await _tool_apply_fir(
            output_index=int(arguments["output_index"]),
            coefficients=[float(c) for c in arguments["coefficients"]],
        )
    elif name == "clear_fir":
        result = await _tool_clear_fir(output_index=int(arguments["output_index"]))
    elif name == "set_master_gain":
        result = await _tool_set_master_gain(gain_db=float(arguments["gain_db"]))
    elif name == "set_output_gain":
        result = await _tool_set_output_gain(
            output_index=int(arguments["output_index"]),
            gain_db=float(arguments["gain_db"]),
        )
    elif name == "configure_matrix":
        result = await _tool_configure_matrix(
            active_input=int(arguments["active_input"]) if "active_input" in arguments else None
        )
    elif name == "analyze_decay":
        result = await _tool_analyze_decay(
            session_id=int(arguments["session_id"]) if "session_id" in arguments else None,
            t60_threshold_ms=float(arguments.get("t60_threshold_ms", 300.0)),
            freq_min=float(arguments.get("freq_min", 20.0)),
            freq_max=float(arguments.get("freq_max", 200.0)),
        )
    elif name == "check_system":
        result = await _tool_check_system()
    elif name == "get_fr_summary":
        session_ids = arguments.get("session_ids")
        if session_ids:
            session_ids = [int(sid) for sid in session_ids]
        result = await _tool_get_fr_summary(
            session_ids=session_ids,
            limit=int(arguments.get("limit", 5)),
        )
    elif name == "compute_deviation":
        result = await _tool_compute_deviation(
            session_id=int(arguments["session_id"]),
            target_curve=arguments["target_curve"],
            null_threshold_db=float(arguments.get("null_threshold_db", 15.0)),
            port_rolloff_hz=float(arguments.get("port_rolloff_hz", 28.0)),
            resolution=arguments.get("resolution", "sixth_octave"),
            convergence_threshold=float(arguments.get("convergence_threshold", 1.5)),
        )
    elif name == "anchor_target":
        result = await _tool_anchor_target(
            session_id=int(arguments["session_id"]),
            target_offsets=arguments["target_offsets"],
            band=arguments.get("band"),
            max_boost_db=float(arguments.get("max_boost_db", 6.0)),
            null_threshold_db=float(arguments.get("null_threshold_db", 15.0)),
            port_rolloff_hz=float(arguments.get("port_rolloff_hz", 28.0)),
        )
    elif name == "compare_sessions":
        result = await _tool_compare_sessions(
            session_a=int(arguments["session_a"]),
            session_b=int(arguments["session_b"]),
            min_hz=float(arguments.get("min_hz", 20.0)),
            max_hz=float(arguments.get("max_hz", 120.0)),
        )
    elif name == "simulate_eq":
        result = await _tool_simulate_eq(
            session_id=int(arguments["session_id"]),
            filters=arguments.get("filters", []),
            min_hz=float(arguments.get("min_hz", 20.0)),
            max_hz=float(arguments.get("max_hz", 120.0)),
        )
    elif name == "optimize_q":
        result = await _tool_optimize_q(
            session_id=int(arguments["session_id"]),
            freq_hz=float(arguments["freq_hz"]),
            target_gain_db=float(arguments["target_gain_db"]),
            band_hz=arguments.get("band_hz"),
        )
    elif name == "analyze_phase":
        result = await _tool_analyze_phase(
            session_id=int(arguments["session_id"]),
            min_hz=float(arguments.get("min_hz", 20.0)),
            max_hz=float(arguments.get("max_hz", 120.0)),
        )
    elif name == "compare_sub_phase":
        result = await _tool_compare_sub_phase(
            session_a=int(arguments["session_a"]),
            session_b=int(arguments["session_b"]),
            min_hz=float(arguments.get("min_hz", 20.0)),
            max_hz=float(arguments.get("max_hz", 120.0)),
        )
    elif name == "design_fir":
        result = await _tool_design_fir(
            session_id=int(arguments["session_id"]),
            target_curve=arguments.get("target_curve"),
            num_taps=int(arguments.get("num_taps", 1024)),
            phase_mode=arguments.get("phase_mode", "minimum"),
            freq_focus_hz=arguments.get("freq_focus_hz"),
        )
    # ── LLM filter-design math tools ────────────────────────────────────────
    elif name == "evaluate_transfer_function":
        result = await _tool_evaluate_transfer_function(
            filters=arguments.get("filters", []),
            query_freqs=arguments.get("query_freqs", []),
        )
    elif name == "per_filter_contribution":
        result = await _tool_per_filter_contribution(
            filters=arguments.get("filters", []),
            session_id=int(arguments["session_id"]),
            query_freqs=arguments.get("query_freqs"),
        )
    elif name == "interpolate_optimal_gain":
        result = await _tool_interpolate_optimal_gain(
            freq=float(arguments["freq"]),
            q=float(arguments["q"]),
            filter_type=arguments["filter_type"],
            measured_errors=arguments["measured_errors"],
        )
    elif name == "sensitivity_analysis":
        result = await _tool_sensitivity_analysis(
            filters=arguments.get("filters", []),
            session_id=int(arguments["session_id"]),
            target_curve=arguments["target_curve"],
            perturbation_db=float(arguments.get("perturbation_db", 0.5)),
        )
    elif name == "fit_correction_filter":
        result = await _tool_fit_correction_filter(
            session_id=int(arguments["session_id"]),
            target_curve=arguments["target_curve"],
            freq_range=arguments["freq_range"],
            constraints=arguments.get("constraints"),
        )
    elif name == "predict_rms":
        result = await _tool_predict_rms(
            filters=arguments.get("filters", []),
            session_id=int(arguments["session_id"]),
            target_curve=arguments["target_curve"],
            null_threshold_db=float(arguments.get("null_threshold_db", 15.0)),
            port_rolloff_hz=float(arguments.get("port_rolloff_hz", 28.0)),
            convergence_threshold=float(arguments.get("convergence_threshold", 1.5)),
        )
    elif name == "play_and_measure_fft":
        result = await _tool_play_and_measure_fft(
            channel_assignments=arguments["channel_assignments"],
            duration_s=float(arguments.get("duration_s", 2.0)),
            amplitude=float(arguments.get("amplitude", 0.5)),
            fft_size=int(arguments.get("fft_size", 8192)),
        )
    elif name == "assign_headroom_tones":
        result = await _tool_assign_headroom_tones(
            speaker_passbands=arguments["speaker_passbands"],
            tones_per_speaker=int(arguments.get("tones_per_speaker", 4)),
            min_spacing_hz=float(arguments.get("min_spacing_hz", 30.0)),
            min_frequency_hz=float(arguments.get("min_frequency_hz", 200.0)),
        )
    # Legacy aliases for backwards compatibility with cached sessions
    elif name == "mute_sub_outputs":
        mute = arguments.get("mute")
        unmute = arguments.get("unmute")
        results_list = []
        if mute:
            results_list.append(await _tool_mute_output(mute))
        if unmute:
            results_list.append(await _tool_unmute_output(unmute))
        failed = [r for r in results_list if not r.get("ok")]
        result = failed[0] if failed else _ok(message="mute/unmute complete")
    else:
        result = _err(f"unknown tool: {name}")

    return [TextContent(type="text", text=json.dumps(result, indent=2))]


async def _read_resource(uri: str) -> str:
    """Read a resource by URI — extracted for testability."""
    if uri == "measurements://latest":
        from .storage import SessionStore
        try:
            store = SessionStore()
            sessions = store.list_sessions()
            if not sessions:
                return json.dumps({"error": "no measurements found"})
            s = sessions[0]
            fr = s.start_fr
            return json.dumps({
                "id": s.id,
                "timestamp": s.timestamp,
                "label": s.label,
                "freq_hz": fr.frequencies if fr else [],
                "spl_db": fr.spl if fr else [],
                "notes": s.notes,
            })
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    return json.dumps({"error": f"unknown resource: {uri}"})


@server.list_resources()
async def list_resources() -> list[Resource]:
    return [
        Resource(
            uri="measurements://latest",
            name="Latest Measurement",
            description="Most recent frequency response measurement session",
            mimeType="application/json",
        ),
    ]


@server.read_resource()
async def read_resource(uri: str) -> str:
    return await _read_resource(uri)


# ── Starlette ASGI app ─────────────────────────────────────────────────────────

def create_app() -> Starlette:
    """Build the ASGI application with Streamable HTTP (primary) + SSE (legacy)."""
    # Modern transport — Claude Code uses this via /mcp endpoint
    http_manager = StreamableHTTPSessionManager(app=server, stateless=True)

    # Legacy SSE transport — kept for backwards compatibility
    sse = SseServerTransport("/messages/")

    @asynccontextmanager
    async def lifespan(app: Starlette):
        """Load drivers on startup; start transports; tear down on shutdown."""
        global _avr, _dsp
        cfg = _config()
        _avr = load_avr_driver(cfg)
        _dsp = load_dsp_driver(cfg)
        await _avr.setup()
        await _dsp.setup()
        log.info(
            "Drivers loaded: avr=%s dsp=%s",
            cfg.avr_driver_name,
            cfg.dsp_driver_name,
        )

        # Rehydrate DSP driver shadow state from last-persisted active_dsp_state.
        # minidspd has no readback — after restart, the hardware retains its
        # flashed filters but the driver's in-memory shadow is empty. Load
        # what was last written so get_output_state / apply_eq baselines are
        # correct instead of claiming everything is zero.
        if hasattr(_dsp, "rehydrate_from_active_state"):
            try:
                from .storage import SessionStore
                active_state = SessionStore().get_active_dsp()
                _dsp.rehydrate_from_active_state(active_state)
                log.info("DSP shadow rehydrated from %d active_dsp_state keys", len(active_state))
            except Exception as exc:
                log.warning("DSP rehydrate failed (shadow stays empty): %s", exc)

        # Configure DSP input routing if active_input is set
        active_input = cfg.minidsp.get("active_input")
        if active_input is not None and _dsp is not None:
            try:
                await _dsp.configure_active_input(int(active_input))
                log.info("DSP routing: active_input=%d → all outputs", active_input)
            except Exception as exc:
                log.warning("Failed to configure active_input routing: %s", exc)
        async with http_manager.run():
            yield
        # Clean up persistent sweep session before closing drivers
        await _end_sweep_session()
        await _avr.close()
        await _dsp.close()

    async def handle_mcp(request: Request) -> Response:
        await http_manager.handle_request(
            request.scope, request.receive, request._send
        )
        return Response()

    async def handle_sse(request: Request) -> Response:
        async with sse.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await server.run(
                streams[0], streams[1], server.create_initialization_options()
            )
        return Response()

    async def handle_messages(request: Request) -> Response:
        await sse.handle_post_message(request.scope, request.receive, request._send)
        return Response()

    return Starlette(
        lifespan=lifespan,
        routes=[
            Route("/mcp", endpoint=handle_mcp, methods=["GET", "POST", "DELETE"]),
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
        ],
    )


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    app = create_app()
    log.info("MCP server starting on %s:%d", MCP_HOST, MCP_PORT)
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT)


if __name__ == "__main__":
    main()
