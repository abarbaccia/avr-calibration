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
  read_eq                — current EQ state (in-memory, updated by apply_eq)
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
  eq://current           — current EQ filter state

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
    fmt: str = "full",
) -> dict:
    """Return last *limit* measurement sessions from SessionStore.

    Args:
        limit: Number of sessions to return.
        min_hz: Low-frequency cutoff — only return data at or above this frequency.
        max_hz: High-frequency cutoff — only return data at or below this frequency.
        decimation: Keep every Nth point (1 = all points, 2 = every other, etc.).
        fmt: Output format — "full" (separate freq_hz[]/spl_db[] arrays) or
             "compact" (single "fr" string "freq1:spl1,freq2:spl2,...", much smaller).
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
                if fr.phase and fmt == "full":
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
                    # In compact mode, strip large sub-fields (group_delay is ~17KB per session)
                    _COMPACT_META_SKIP = {"group_delay"}
                    entry["metadata"] = {
                        k: v for k, v in s.metadata.items()
                        if k not in _COMPACT_META_SKIP
                    }
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


async def _tool_read_eq() -> dict:
    """Return current EQ filter state (in-memory, updated by apply_eq)."""
    try:
        preset = await _dsp.current_preset()  # type: ignore[union-attr]
        filters = await _dsp.read_eq(preset)  # type: ignore[union-attr]
        return _ok(preset=preset, filters=filters)
    except DriverError as exc:
        return _err(f"read_eq error: {exc}")


async def _tool_read_input_eq() -> dict:
    """Return current input EQ filter state (in-memory, updated by apply_input_eq)."""
    try:
        preset = await _dsp.current_preset()  # type: ignore[union-attr]
        filters = await _dsp.read_input_eq(preset)  # type: ignore[union-attr]
        return _ok(preset=preset, filters=filters, target="input")
    except DriverError as exc:
        return _err(f"read_input_eq error: {exc}")


async def _tool_apply_eq(
    filters: list[dict], output_index: int | None = None,
) -> dict:
    """Validate and apply EQ filters to DSP output(s).

    If *output_index* is given, writes only to that single output (per-sub EQ).
    Otherwise writes to all configured sub outputs (broadcast mode).

    Returns {ok: True} or {ok: False, error: "SafetyValidator: ..."} on rejection.
    """
    try:
        preset = await _dsp.current_preset()  # type: ignore[union-attr]
        await _dsp.apply_eq(preset, filters, output_index=output_index)  # type: ignore[union-attr]
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
        await _dsp.apply_input_eq(preset, filters)  # type: ignore[union-attr]
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
) -> dict:
    """Compute RMS deviation of a measurement against a target curve.

    Automatically detects and excludes:
    - Null zones: frequencies where measured SPL is > null_threshold_db below the band average
    - Below-port rolloff: frequencies below port_rolloff_hz where the sub physically can't produce output

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
        converged = rms < 2.0  # Standard convergence threshold

        # Downsample per_band_errors to 1/3-octave summary for compact output
        summary = []
        for centre in _THIRD_OCTAVE_CENTRES:
            if centre < band_lo or centre > band_hi:
                continue
            factor = 2 ** (1 / 6)
            lo = centre / factor
            hi = centre * factor
            band_entries = [e for e in per_band_errors if lo <= e["freq_hz"] < hi and not e.get("excluded")]
            if band_entries:
                avg_error = sum(e["error_db"] for e in band_entries) / len(band_entries)
                avg_measured = sum(e["measured_db"] for e in band_entries) / len(band_entries)
                target_at_centre = interpolate_target(centre)
                summary.append({
                    "freq_hz": centre,
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
        denon_ctx = DenonSweepContext.from_config(cfg)
        from .drivers.minidsp import MinidspSweepContext
        minidsp_ctx = MinidspSweepContext.from_config(cfg, driver=_dsp)

        if denon_ctx:
            async with denon_ctx:
                fr = await engine.measure()
        elif minidsp_ctx:
            async with minidsp_ctx:
                fr = await engine.measure()
        else:
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

        # Strip large fields from the tool response (they're saved in the DB)
        response_metadata = {k: v for k, v in metadata.items() if k != "group_delay"}

        return _ok(
            session_id=session_id,
            label=full_label,
            metadata=response_metadata,
            message="Measurement complete — use get_measurement_history() to retrieve results.",
        )
    except Exception as exc:
        return _err(f"measurement failed: {exc}")


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
    from .measurement import MeasurementEngine, MeasurementQualityError
    from .config import update_config
    from .drivers.minidsp import MinidspSweepContext
    import math as _math
    import numpy as _np

    def _ir_spl(fr) -> float:
        """SPL estimate from deconvolved IR peak.

        The IR peak (20*log10(max|h(t)|)) tracks actual SPL linearly because
        the deconvolution H=Y/X scales with recording level.  Empirically the
        values match acoustic dB SPL within a few dB for our sweep config.
        Unlike recording_peak_dbfs, the IR peak is immune to ambient noise
        and electrical interference because deconvolution separates signal
        from noise.
        """
        if fr.impulse_response:
            arr = _np.array(fr.impulse_response, dtype=_np.float64)
            return float(round(20.0 * _np.log10(_np.max(_np.abs(arr)) + 1e-12), 1))
        # Fallback: FR magnitude peak (less reliable for absolute SPL)
        return fr.peak_spl

    cfg = _config()
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

        minidsp_ctx = MinidspSweepContext.from_config(cfg)

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

        if minidsp_ctx:
            async with minidsp_ctx:
                return await _usb_predict_verify()
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


async def _tool_get_config() -> dict:
    """Return the current config.yaml plus EQ capabilities discovery."""
    try:
        cfg = _config()
        data = dict(cfg._data)

        # EQ capabilities — tells Claude what PEQ resources are available
        active_input = cfg.minidsp.get("active_input") or 0
        sub_outputs = cfg.sub_outputs
        slots = list(range(2, 10))  # slots 2-9

        eq_capabilities: dict = {
            "input_peq": {
                "input_index": active_input,
                "available_slots": slots,
                "num_slots": len(slots),
                "description": "Shared EQ applied to all outputs (e.g. Harman target curve)",
                "tool": "apply_input_eq",
            },
            "output_peq": [],
            "fir_capable": True,
            "fir_max_taps_per_output": 2048,
            "fir_shared_tap_pool": 4096,
            "fir_sample_rate_hz": 96000,
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
    """Return per-output gain, delay, and polarity from in-memory driver tracking."""
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

        sample_rate = session.start_fr.sample_rate if session.start_fr else 48000
        ir_arr = np.array(ir, dtype=np.float64)

        search_samples = max(1, int(search_window_ms / 1000.0 * sample_rate))
        search_samples = min(search_samples, len(ir_arr))
        search_window = ir_arr[:search_samples]

        abs_window = np.abs(search_window)
        max_idx = int(np.argmax(abs_window))
        # Onset detection: first sample within 20 dB of the absolute peak.
        # argmax finds the LOUDEST peak — in strong-mode rooms this can be a
        # late resonance instead of the direct sound.  Onset finds first arrival.
        onset_threshold = abs_window[max_idx] * 0.1  # -20 dB
        peak_idx = int(np.argmax(abs_window > onset_threshold))
        peak_sign = 1 if ir_arr[peak_idx] >= 0.0 else -1
        peak_time_s = peak_idx / sample_rate
        spl_db = float(20.0 * np.log10(abs_window[max_idx] + 1e-12))

        return _ok(
            session_id=session.id,
            peak_time_s=round(peak_time_s, 6),
            peak_time_ms=round(peak_time_s * 1000.0, 3),
            peak_sign=peak_sign,
            spl_db=round(spl_db, 1),
            sample_rate=sample_rate,
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
                        "Output format. 'compact': FR data as 'freq:spl,...' string (~12 chars/point, "
                        "recommended for filter design). 'full': separate freq_hz[] and spl_db[] arrays "
                        "(verbose, may exceed token limits). Default: 'full'."
                    ),
                    "default": "full",
                },
            },
        },
    ),
    Tool(
        name="read_eq",
        description=(
            "Return the current DSP EQ filter state. Tracked in-memory — "
            "reflects filters applied via apply_eq() since the MCP server started. "
            "Returns preset index and list of active filters with freq, gain_db, q, type."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    Tool(
        name="read_input_eq",
        description=(
            "Return the current DSP input EQ filter state. Tracked in-memory — "
            "reflects filters applied via apply_input_eq() since the MCP server started. "
            "Returns preset index and list of active input filters with freq, gain_db, q, type."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
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
            "Write FIR filter coefficients to a single DSP output on the miniDSP 2x4 HD. "
            "Coefficients are floats normalized so the peak is <= 1.0. "
            "The miniDSP FIR engine runs at 96000 Hz; max 2048 taps per output "
            "(4096 shared across all 4 outputs). "
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
                        "Max 2048 taps. Must be at 96000 Hz. "
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
            "from the RMS calculation. Returns rms_db, converged (rms < 2.0), per-band summary, "
            "and null zone identification. Use this after each EQ iteration to check convergence."
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
            },
            "required": ["session_id", "target_curve"],
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
            fmt=arguments.get("format", "full"),
        )
    elif name == "read_eq":
        result = await _tool_read_eq()
    elif name == "read_input_eq":
        result = await _tool_read_input_eq()
    elif name == "apply_eq":
        output_index = arguments.get("output_index")
        if output_index is not None:
            output_index = int(output_index)
        result = await _tool_apply_eq(arguments.get("filters", []), output_index=output_index)
    elif name == "apply_input_eq":
        result = await _tool_apply_input_eq(
            arguments.get("filters", []),
            target_curve=arguments.get("target_curve"),
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
        )
    elif name == "compare_sessions":
        result = await _tool_compare_sessions(
            session_a=int(arguments["session_a"]),
            session_b=int(arguments["session_b"]),
            min_hz=float(arguments.get("min_hz", 20.0)),
            max_hz=float(arguments.get("max_hz", 120.0)),
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

    elif uri == "eq://current":
        try:
            preset = await _dsp.current_preset()  # type: ignore[union-attr]
            filters = await _dsp.read_eq(preset)  # type: ignore[union-attr]
            return json.dumps({"preset": preset, "filters": filters})
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
        Resource(
            uri="eq://current",
            name="Current EQ",
            description="Current DSP EQ filter state (in-memory)",
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
