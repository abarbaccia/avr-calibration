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
  analyze_ir             — IR onset time, polarity, SPL (solo-sub alignment ONLY — invalid cross-path)
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
from .drivers.registry import load_avr_driver, load_drivers_from_graph, load_dsp_driver

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

# Registry of all processor drivers keyed by processor name (from the signal
# graph). The module-global ``_avr`` and ``_dsp`` stay as convenience singletons
# pointing at the first entry of each kind, preserving every legacy call site.
_drivers = None  # DriverRegistry | None

# Server-side cache for FIR coefficients produced by design_fir. Lets callers
# opt out of the coefficient round-trip (design_fir returns arrays up to
# ~65 536 floats; ~140 KB JSON at 8 192 taps) by passing
# return_coefficients=false and later referencing the cached design by its
# source session_id in apply_fir(design_session_id=...). Cleared implicitly
# on process restart — callers must re-design if the server has been bounced.
_fir_design_cache: dict[int, list[float]] = {}
# Tracks the design intent of cached coefficients so apply_fir can pass the
# right ``intent`` to SafetyValidator (e.g., ``modal_cancel`` for FIRs from
# ``design_modal_fir`` to admit their intentionally hot modal-band gain).
_fir_design_intent: dict[int, str] = {}


def _default_dsp_name() -> str | None:
    """Return the processor name of the active default DSP, or None.

    Used for DSP-state storage keys so that multi-DSP installs don't collide
    on flat keys like ``output_eq_0``.
    """
    if _drivers is None:
        return None
    return _drivers.default_dsp_name()

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
    target: str | None = None,
    simulation_verified: bool = False,
) -> dict:
    """Validate and apply EQ filters to DSP output(s).

    Resolution order:
      - ``target`` (group / transducer / role name) → per-transducer apply
      - ``output_index`` → single-output apply (legacy)
      - both omitted → broadcast to all configured sub outputs (legacy)

    Target path validates the filter set against the target transducer's
    per-profile SafetyValidator at the MCP layer before reaching the driver;
    the driver's own SVS-default validation runs as a second line of defence.

    Returns {ok: True} or {ok: False, error: "SafetyValidator: ..."} on rejection.
    """
    if target is not None and output_index is not None:
        return _err("apply_eq: pass either target or output_index, not both")

    from .storage import dsp_output_key
    default_processor_name = _default_dsp_name() or "dsp"

    # Target path: dispatch through the graph. Each transducer's apply_eq call
    # goes to the driver that actually owns its output — supports cross-DSP
    # groups (e.g. "full_system" spanning a CamillaDSP and a miniDSP).
    if target is not None:
        try:
            records = _resolve_for_dispatch(target)
        except _DispatchError as exc:
            return exc.err

        from .safety import FilterSpec, SafetyValidator
        specs = [
            FilterSpec(
                freq=float(f["freq"]), gain_db=float(f["gain_db"]),
                q=float(f.get("q", 0.707)), type=f["type"],
            )
            for f in filters
        ]
        # Pre-validate every transducer's profile before any write — a single
        # reject aborts the whole target, so callers never end up in a partial
        # state where half the group got filters.
        for rec in records:
            validator = SafetyValidator(rec["profile"])
            result = validator.validate(
                specs, previous_filters=None, simulation_verified=simulation_verified,
            )
            if not result.ok:
                return _err(f"{result.error} [target={rec['transducer'].name!r}]")

        # Dispatch per transducer to its own driver. Each driver has its own
        # preset semantics (miniDSP has slots 0-3, CamillaDSP is a no-op) —
        # query per driver, not globally.
        applied = 0
        try:
            for rec in records:
                driver = rec["driver"]
                preset_for_driver = await driver.current_preset()
                await driver.apply_eq(
                    preset_for_driver, filters,
                    output_index=rec["output_index"],
                    simulation_verified=simulation_verified,
                )
                _persist_dsp_state(
                    dsp_output_key(rec["processor"], rec["output_index"], "eq"),
                    {
                        "filters": filters,
                        "preset": preset_for_driver,
                        "transducer": rec["transducer"].name,
                    },
                )
                applied += 1
            return _ok(
                filters_applied=len(filters),
                target=target,
                applied=[
                    {
                        "transducer": rec["transducer"].name,
                        "processor": rec["processor"],
                        "output_index": rec["output_index"],
                    }
                    for rec in records
                ],
            )
        except DriverError as exc:
            return _err(f"{exc} (applied to {applied}/{len(records)} transducers)")

    # Legacy paths: output_index or broadcast
    try:
        preset = await _dsp.current_preset()  # type: ignore[union-attr]
    except DriverError as exc:
        return _err(str(exc))
    processor_name = default_processor_name
    try:
        await _dsp.apply_eq(preset, filters, output_index=output_index, simulation_verified=simulation_verified)  # type: ignore[union-attr]
        if output_index is not None:
            _persist_dsp_state(
                dsp_output_key(processor_name, output_index, "eq"),
                {"filters": filters, "preset": preset},
            )
        else:
            cfg = _config()
            for slot in cfg.minidsp.get("output_slots", []):
                if slot.get("type") == "sub":
                    _persist_dsp_state(
                        dsp_output_key(processor_name, slot["index"], "eq"),
                        {"filters": filters, "preset": preset},
                    )
        return _ok(filters_applied=len(filters), preset=preset,
                    output_index=output_index)
    except DriverError as exc:
        return _err(str(exc))


async def _tool_apply_input_eq(
    filters: list[dict],
    target_curve: dict | None = None,
    target: str | None = None,
    simulation_verified: bool = False,
) -> dict:
    """Validate and apply EQ filters to the DSP input channel.

    Applies shared EQ (e.g. Harman target curve) to the active input,
    affecting all outputs equally. When ``target`` names a group or
    transducer, the filter set is validated against the **strictest** profile
    among the downstream transducers — an input filter is shared across
    outputs, so it must be safe for the weakest driver in the chain.

    Optional *target_curve* persists the optimization target for dashboard display.

    Returns {ok: True} or {ok: False, error: "SafetyValidator: ..."} on rejection.
    """
    from .storage import dsp_input_key

    # Target path: bucket transducers by processor, call apply_input_eq on each
    # processor's driver with the strictest profile among that processor's
    # transducers. This lets a target like "front_soundstage" span a miniDSP
    # (subs) plus a CamillaDSP (mains) — each gets its own shared-input EQ.
    if target is not None:
        try:
            records = _resolve_for_dispatch(target)
        except _DispatchError as exc:
            return exc.err

        cfg = _config()
        graph = cfg.signal_graph

        by_processor: dict[str, list[dict]] = {}
        for rec in records:
            by_processor.setdefault(rec["processor"], []).append(rec)

        from .safety import FilterSpec, SafetyValidator
        specs = [
            FilterSpec(
                freq=float(f["freq"]), gain_db=float(f["gain_db"]),
                q=float(f.get("q", 0.707)), type=f["type"],
            )
            for f in filters
        ]
        # Pre-validate each processor's strictest-of-bucket profile upfront so
        # we never half-apply.
        strict_profiles: dict[str, Any] = {}
        for proc_name, recs in by_processor.items():
            t_tuple = tuple(r["transducer"] for r in recs)
            profile = graph.strictest_profile(t_tuple)
            strict_profiles[proc_name] = profile
            validator = SafetyValidator(profile)
            result = validator.validate(
                specs, previous_filters=None, simulation_verified=simulation_verified,
            )
            if not result.ok:
                return _err(
                    f"{result.error} [target={target!r}, processor={proc_name!r}, "
                    f"strictest={profile.name!r}]"
                )

        applied_buckets: list[dict] = []
        try:
            for proc_name, recs in by_processor.items():
                driver = recs[0]["driver"]
                preset_for_driver = await driver.current_preset()
                await driver.apply_input_eq(
                    preset_for_driver, filters,
                    simulation_verified=simulation_verified,
                )
                _persist_dsp_state(
                    dsp_input_key(proc_name, "eq"),
                    {
                        "filters": filters,
                        "preset": preset_for_driver,
                        "target": target,
                        "strictest_profile": strict_profiles[proc_name].name,
                    },
                )
                applied_buckets.append({
                    "processor": proc_name,
                    "strictest_profile": strict_profiles[proc_name].name,
                    "transducers": [r["transducer"].name for r in recs],
                })
            if target_curve:
                _persist_dsp_state("target_curve", target_curve)
            return _ok(
                filters_applied=len(filters), target=target,
                applied=applied_buckets,
            )
        except DriverError as exc:
            return _err(str(exc))

    # Legacy path: single default-DSP apply.
    try:
        preset = await _dsp.current_preset()  # type: ignore[union-attr]
    except DriverError as exc:
        return _err(str(exc))

    try:
        await _dsp.apply_input_eq(preset, filters, simulation_verified=simulation_verified)  # type: ignore[union-attr]
        processor_name = _default_dsp_name() or "dsp"
        _persist_dsp_state(
            dsp_input_key(processor_name, "eq"),
            {"filters": filters, "preset": preset},
        )
        if target_curve:
            # target_curve is a calibration intent, not processor state — keep flat.
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
    exclude_geometry: bool = True,
    weight_by_coherence: bool = False,
) -> dict:
    """Compute RMS deviation of a measurement against a target curve.

    Automatically detects and excludes:
    - Null zones: frequencies where measured SPL is > null_threshold_db below the band average
    - Below-port rolloff: frequencies below port_rolloff_hz where the sub physically can't produce output
    - Geometry nulls (optional, on by default): 1/3-octave bands that analyze_phase
      classifies as 'geometry' — near-π phase offset caused by cancellation at the
      listener position. EQ cannot fix these; including them inflates RMS and
      drives the driving agent to keep iterating past convergence. Run 14's RMS
      was dominated by the 27 Hz and 69 Hz bands for exactly this reason.

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

        # Geometry bands (near-π cancellation) from analyze_phase. EQ can't fix
        # them, so treat them like null zones for RMS purposes.
        geometry_ranges: list[tuple[float, float]] = []
        if exclude_geometry:
            try:
                geometry_ranges = await _get_geometry_band_ranges(
                    session_id, min_hz=band_lo, max_hz=band_hi,
                )
            except Exception as exc:
                log.warning(
                    "compute_deviation: geometry exclusion unavailable (%s); "
                    "falling back to SPL-based null detection only", exc,
                )
                geometry_ranges = []

        def _in_geometry(freq_hz: float) -> bool:
            return any(lo <= freq_hz < hi for lo, hi in geometry_ranges)

        # Build a coherence lookup for the session so we can weight errors by
        # measurement reliability. Low-coherence bands (<0.7) carry large
        # measurement noise and shouldn't drive the optimizer into chasing
        # artifacts. When weight_by_coherence is false this just populates
        # per-band entries for inspection.
        coh_by_freq: dict[float, float] = {}
        if fr.coherence:
            for f, c in zip(fr.frequencies, fr.coherence):
                coh_by_freq[float(f)] = float(c)

        # Classify each frequency point
        per_band_errors = []
        included_errors: list[float] = []
        included_coherence: list[float] = []
        excluded_null = []
        excluded_rolloff = []
        excluded_geometry = []

        for freq, measured in pairs:
            target = interpolate_target(freq)
            if target is None:
                continue

            error = measured - target  # positive = above target, negative = below
            coh = coh_by_freq.get(float(freq), 1.0)  # fall back to full weight if unknown

            # Check exclusions
            is_null = measured < (band_avg - null_threshold_db)
            is_rolloff = freq < port_rolloff_hz
            is_geometry = _in_geometry(freq)

            entry = {
                "freq_hz": round(freq, 1),
                "measured_db": round(measured, 1),
                "target_db": round(target, 1),
                "error_db": round(error, 1),
                "coherence": round(coh, 3),
                "excluded": is_null or is_rolloff or is_geometry,
            }

            # Priority: geometry first (most specific), then null, then rolloff.
            if is_geometry:
                excluded_geometry.append(round(freq, 1))
                entry["exclude_reason"] = "geometry"
            elif is_null:
                excluded_null.append(round(freq, 1))
                entry["exclude_reason"] = "null"
            elif is_rolloff:
                excluded_rolloff.append(round(freq, 1))
                entry["exclude_reason"] = "rolloff"
            else:
                included_errors.append(error)
                included_coherence.append(coh)

            per_band_errors.append(entry)

        if not included_errors:
            return _err("no usable frequency points after excluding nulls and rolloff")

        # Compute unweighted RMS first (backward-compatible, always reported).
        rms_unweighted = math.sqrt(
            sum(e ** 2 for e in included_errors) / len(included_errors)
        )

        # Coherence-weighted RMS: reliable bands (high coherence) dominate;
        # noisy bands contribute proportionally less. A band with coherence
        # 0.3 contributes less than half what a band with 0.7 contributes.
        if weight_by_coherence and any(c > 0 for c in included_coherence):
            weight_sum = sum(included_coherence) or 1e-9
            rms_weighted = math.sqrt(
                sum(c * e ** 2 for c, e in zip(included_coherence, included_errors))
                / weight_sum
            )
            rms = rms_weighted
        else:
            rms = rms_unweighted

        # Noise-floor estimate: what RMS would look like if everything WERE
        # noise. Each bin's noise variance scales as (1 - coherence²) — bins
        # with coh=0.5 contribute ~0.75 weight, coh=0.9 contributes ~0.19.
        # Comparing RMS to this noise estimate tells the caller how much of
        # their remaining error is signal vs measurement noise. If
        # RMS ≈ noise_floor, further iteration is chasing noise.
        if included_coherence:
            noise_variance = sum((1.0 - c * c) for c in included_coherence) / len(included_coherence)
            # Calibrate to dB-level noise; this heuristic targets a typical
            # ±1 dB-per-bin range at coherence 0.7.
            noise_floor_db = math.sqrt(noise_variance) * 2.0
        else:
            noise_floor_db = 0.0

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

        # Surface T60 ringing in excluded bands. compute_deviation excludes
        # port-rolloff, geometry, and null-zone bands from RMS (EQ can't fix
        # them), but the LLM still needs to know if those bands have bad
        # ringing (T60 > 400 ms) that calls for physical treatment.
        excluded_band_diagnostics: list[dict] = []
        try:
            decay_result = await _tool_analyze_decay(session_id=session_id)
            if decay_result.get("ok"):
                for mode in decay_result.get("modes", []):
                    freq_hz = float(mode.get("freq_hz", 0.0))
                    t60_ms = float(mode.get("t60_ms", 0.0))
                    if t60_ms <= 400.0:
                        continue
                    if freq_hz < port_rolloff_hz:
                        reason = "port_rolloff"
                    elif _in_geometry(freq_hz):
                        reason = "geometry"
                    elif any(
                        zone["lo_hz"] <= freq_hz <= zone["hi_hz"]
                        for zone in null_zones
                    ):
                        reason = "null_zone"
                    else:
                        continue
                    excluded_band_diagnostics.append({
                        "freq_hz": round(freq_hz, 1),
                        "t60_ms": round(t60_ms, 1),
                        "peak_db": round(float(mode.get("peak_db", 0.0)), 1),
                        "reason": reason,
                    })
            else:
                log.warning(
                    "compute_deviation: analyze_decay failed (%s); "
                    "excluded_band_diagnostics will be empty",
                    decay_result.get("error"),
                )
        except Exception as exc:
            log.warning(
                "compute_deviation: analyze_decay raised (%s); "
                "excluded_band_diagnostics will be empty", exc,
            )

        # Geometry-dominated flag: when more than half the in-band points
        # are excluded as geometry nulls, the RMS / mean / max stats are
        # computed from < 50% of the band and are not representative.
        # Surface a single boolean so callers can branch (e.g. recommend
        # sub repositioning instead of further EQ iteration) without
        # comparing counts to thresholds themselves.
        total_points = (
            len(included_errors) + len(excluded_null)
            + len(excluded_rolloff) + len(excluded_geometry)
        )
        geometry_dominated = (
            total_points > 0
            and len(excluded_geometry) > total_points * 0.5
        )
        return _ok(
            session_id=session_id,
            rms_db=round(rms, 2),
            rms_unweighted_db=round(rms_unweighted, 2),
            noise_floor_estimate_db=round(noise_floor_db, 2),
            weight_by_coherence=weight_by_coherence,
            converged=converged,
            convergence_threshold=convergence_threshold,
            resolution=resolution,
            mean_error_db=round(mean_error, 2),
            max_error_db=round(max_error, 1),
            included_points=len(included_errors),
            mean_included_coherence=round(
                sum(included_coherence) / len(included_coherence)
                if included_coherence else 0.0,
                3,
            ),
            excluded_null_points=len(excluded_null),
            excluded_rolloff_points=len(excluded_rolloff),
            excluded_geometry_points=len(excluded_geometry),
            geometry_dominated=geometry_dominated,
            geometry_bands=[
                {"lo_hz": round(lo, 1), "hi_hz": round(hi, 1)}
                for lo, hi in geometry_ranges
            ],
            null_zones=null_zones,
            summary=summary,
            excluded_band_diagnostics=excluded_band_diagnostics,
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
    exclude_geometry: bool = True,
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

        # Geometry bands (near-π cancellation) from analyze_phase — same
        # treatment as compute_deviation: exclude so the anchor doesn't get
        # dragged toward an unreachable limiting frequency.
        geometry_ranges_at: list[tuple[float, float]] = []
        if exclude_geometry:
            try:
                geometry_ranges_at = await _get_geometry_band_ranges(
                    session_id, min_hz=band_lo, max_hz=band_hi,
                )
            except Exception as exc:
                log.warning(
                    "anchor_target: geometry exclusion unavailable (%s); "
                    "falling back to SPL-based null detection only", exc,
                )
                geometry_ranges_at = []

        # Compute headroom at each valid frequency
        headroom_values: list[tuple[float, float, float, float]] = (
            []
        )  # (freq, measured, offset, headroom)
        excluded_null: list[float] = []
        excluded_rolloff: list[float] = []
        excluded_geometry_at: list[float] = []

        for freq, measured in pairs:
            offset = interpolate_offset(freq)
            if offset is None:
                continue

            is_null = measured < (band_avg - null_threshold_db)
            is_rolloff = freq < port_rolloff_hz
            is_geometry = any(lo <= freq < hi for lo, hi in geometry_ranges_at)

            if is_geometry:
                excluded_geometry_at.append(round(freq, 1))
            elif is_null:
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

        # Build anchored target curve. Flag anchored points that are below
        # port_rolloff_hz as unreachable — the sub physically can't produce
        # those levels, so Phase 3 filter design should ignore them.
        anchored_points = []
        for p in offsets_sorted:
            point = {
                "freq": p["freq_hz"],
                "spl": round(reference_spl + p["offset_db"], 2),
            }
            if p["freq_hz"] < port_rolloff_hz:
                point["reachable"] = False
                point["reason"] = "below port-tune rolloff"
            anchored_points.append(point)

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
            excluded_geometry_points=len(excluded_geometry_at),
            geometry_bands=[
                {"lo_hz": round(lo, 1), "hi_hz": round(hi, 1)}
                for lo, hi in geometry_ranges_at
            ],
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
    freq_range: list[float],
    target_curve: dict | None = None,
    target_offsets: list[dict] | None = None,
    constraints: dict | None = None,
    num_filters: int = 1,
    exclude_geometry: bool = True,
    baseline_filters: list[dict] | None = None,
) -> dict:
    """Find the optimal PEQ filter(s) to minimize RMS in a frequency range.

    When ``num_filters == 1`` (default), runs grid-search-plus-refinement for
    one peaking filter — the LLM picks the region, the tool finds the best
    (freq, gain, Q).

    When ``num_filters > 1``, uses scipy's Levenberg-Marquardt
    (``least_squares`` with trust-region bounds) to **jointly** optimize all
    filter parameters at once. 3N free variables per call.

    Target anchoring: pass ``target_offsets`` (relative dB, e.g. Harman offsets)
    and the tool calls anchor_target internally to compute the correct reference
    SPL for the baseline — avoids the common mistake of reusing a stale
    reference_spl. Or pass ``target_curve`` directly with absolute SPL points.

    Geometry exclusion: when ``exclude_geometry=True`` (default), near-π
    cancellation bands (from analyze_phase) are dropped from the residuals
    before fitting so the LM doesn't waste filter slots trying to fill
    unfixable nulls.

    constraints: {max_boost_db, min_q, max_q, filter_type, preserve_mean,
    doublet_penalty, doublet_max_hz}. ``preserve_mean=True`` adds a penalty
    that keeps mean(correction) ≈ 0 so broadband level stays balanced.
    ``doublet_penalty`` > 0 penalises opposing-sign filter pairs closer than
    ``doublet_max_hz`` (default 5 Hz) to discourage ugly +N/-N doublets.
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

        if num_filters < 1:
            return _err("num_filters must be >= 1")
        if num_filters > 8:
            return _err("num_filters > 8 not supported (SafetyValidator slot budget)")

        range_lo, range_hi = float(freq_range[0]), float(freq_range[1])

        # Auto-anchor: if caller passed target_offsets, compute ref_spl from
        # the current baseline so the fit doesn't inherit a stale anchor.
        anchored_reference_spl: float | None = None
        anchor_warning: str | None = None
        if target_offsets and not target_curve:
            anchor = await _tool_anchor_target(
                session_id=session_id,
                target_offsets=target_offsets,
                band=[range_lo, range_hi],
                exclude_geometry=exclude_geometry,
            )
            if not anchor.get("ok"):
                return _err(
                    f"auto-anchor failed: {anchor.get('error', 'unknown')}"
                )
            anchored_reference_spl = float(anchor["reference_spl"])

            # Bug 2 fix: detect when the anchor diverges above the measured
            # SPL at the reference frequency — this forces the optimizer to
            # place boost filters just to reach the target level, producing
            # doublets. Clamp when max_boost_db==0; warn when >3 dB divergence.
            _c_early = constraints or {}
            _max_boost_early = float(_c_early.get("max_boost_db", 6.0))
            _ref_freq = anchor.get("limiting_freq_hz")
            if _ref_freq is not None:
                # Find measured SPL at the reference frequency
                _meas_at_ref = min(
                    zip(fr.frequencies, fr.spl),
                    key=lambda pair: abs(pair[0] - _ref_freq),
                )[1]
                _anchor_excess = anchored_reference_spl - _meas_at_ref
                if _max_boost_early == 0 and _anchor_excess > 0:
                    # Clamp: anchor cannot exceed measured SPL when no boosts
                    # are allowed, or every correction becomes a required boost.
                    anchored_reference_spl = _meas_at_ref
                elif _anchor_excess > 3.0:
                    anchor_warning = (
                        f"anchor is {_anchor_excess:.1f}dB above measured at "
                        f"{_ref_freq:.1f} Hz; consider using target_curve with "
                        f"absolute SPL to prevent forced boosts"
                    )

            target_curve = {
                "points": anchor["anchored_points"],
                "band": [range_lo, range_hi],
            }
            # If anchor was clamped, rebuild anchored_points with updated reference
            if _ref_freq is not None and _max_boost_early == 0:
                _c_early_anchor_orig = float(anchor["reference_spl"])
                _anchor_shift = anchored_reference_spl - _c_early_anchor_orig
                if _anchor_shift != 0.0:
                    target_curve["points"] = [
                        {"freq": p["freq"], "spl": round(p["spl"] + _anchor_shift, 2),
                         **{k: v for k, v in p.items() if k not in ("freq", "spl")}}
                        for p in anchor["anchored_points"]
                    ]

        if not target_curve:
            return _err("must pass target_curve or target_offsets")
        points = target_curve.get("points", [])
        if not points:
            return _err("target_curve must include 'points'")

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

        # Geometry-band exclusion: pull 1/3-octave ranges classified as
        # near-π cancellation by analyze_phase so the optimizer doesn't
        # waste slots trying to fill unfixable nulls.
        geometry_ranges: list[tuple[float, float]] = []
        if exclude_geometry:
            try:
                geometry_ranges = await _get_geometry_band_ranges(
                    session_id, min_hz=range_lo, max_hz=range_hi,
                )
            except Exception as exc:
                log.warning("fit_correction_filter: geometry exclusion failed: %s", exc)

        def _in_geometry(freq_hz: float) -> bool:
            return any(lo <= freq_hz <= hi for lo, hi in geometry_ranges)

        # baseline_filters: when caller passes the currently-applied filter
        # bank, apply it to the measured FR first so the optimiser sees the
        # residual *after* the existing correction. Lets the fit add new
        # filters on top instead of replacing the whole bank.
        def _apply_baseline(freq_hz: float) -> float:
            total = 0.0
            for bf in baseline_filters or []:
                total += _biquad_response(
                    freq_hz,
                    bf.get("type", "peaking"),
                    float(bf["freq"]),
                    float(bf["gain_db"]),
                    float(bf.get("q", 0.707)),
                )
            return total

        # Build error pairs in the target range
        error_pairs = []
        excluded_geometry_points = 0
        for f, measured in zip(fr.frequencies, fr.spl):
            if f < range_lo or f > range_hi:
                continue
            target = interp_target(f)
            if target is None:
                continue
            if _in_geometry(f):
                excluded_geometry_points += 1
                continue
            corrected = measured + _apply_baseline(f)
            error_pairs.append((f, corrected, target, corrected - target))

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
        preserve_mean = bool(c.get("preserve_mean", False))
        # Bug 1 fix: when max_boost_db==0 and preserve_mean=True, the
        # optimizer is in a degenerate state — preserve_mean penalises net
        # downward mean corrections, but max_boost_db=0 prevents any
        # compensating boosts. Auto-suppress preserve_mean in this case and
        # report it in the response so the caller is aware.
        if max_boost == 0 and preserve_mean:
            preserve_mean = False
            preserve_mean_suppressed = True
        else:
            preserve_mean_suppressed = False
        # Soft-constraint weight for the mean(correction)≈0 penalty. Default
        # sqrt(N) puts the penalty on comparable footing with per-point SSE
        # so the optimiser trades mean drift roughly 1:1 against fit quality.
        # Raise this above 1.0 (multiplier) to force the optimiser harder
        # toward exact mean preservation at some cost to SSE — useful when
        # the caller cares more about broadband level than L2 fit.
        preserve_mean_strength = float(c.get("preserve_mean_strength", 1.0))
        doublet_penalty = float(c.get("doublet_penalty", 0.0))
        doublet_max_hz = float(c.get("doublet_max_hz", 5.0))
        # Cap Q on positive-gain filters. LM otherwise parks a broad Q~0.5
        # boost at the band edge that bleeds into adjacent bands and pushes
        # mean level up. Cuts rarely have this pathology (narrow cuts are
        # benign). None/0 disables the penalty.
        max_q_boost = c.get("max_q_boost")
        max_q_boost = float(max_q_boost) if max_q_boost is not None else None
        boost_q_penalty = float(c.get("boost_q_penalty", 5.0))

        # ── Multi-filter joint optimization (N > 1) ─────────────────────────
        # For N > 1, treat the filter parameters as a continuous optimization
        # problem and solve with scipy.optimize.least_squares (trust-region
        # Levenberg-Marquardt). One tool call produces a full converged PEQ
        # set that would otherwise require N measure-and-tweak iterations
        # by hand.
        if num_filters > 1:
            try:
                import numpy as np
                from scipy.optimize import least_squares
            except Exception as exc:
                return _err(f"num_filters>1 requires scipy+numpy ({exc})")

            freqs_arr = np.array([e[0] for e in error_pairs])
            measured_arr = np.array([e[1] for e in error_pairs])
            target_arr = np.array([e[2] for e in error_pairs])

            # Log-uniform initial filter centres across the band so the
            # starting point isn't pathologically bad. Gains start at
            # `-sign(error)` small; Q starts at 2 (mid-broad).
            init_freqs = [
                range_lo * (range_hi / range_lo) ** (i / (num_filters - 1))
                for i in range(num_filters)
            ]
            # Each filter's starting gain tries to reduce the local error
            # at that centre frequency — keeps LM from wandering.
            init_gains = []
            for fc in init_freqs:
                # Linear-interpolate local error at fc
                if fc <= freqs_arr[0]:
                    local_err = measured_arr[0] - target_arr[0]
                elif fc >= freqs_arr[-1]:
                    local_err = measured_arr[-1] - target_arr[-1]
                else:
                    idx = int(np.searchsorted(freqs_arr, fc))
                    local_err = measured_arr[idx] - target_arr[idx]
                # Want a filter gain of -local_err, clamped to safe range
                init_gains.append(float(np.clip(-local_err, -10.0, max_boost * 0.8)))

            # Pack initial params: [f_0, g_0, q_0, f_1, g_1, q_1, ...]
            x0 = []
            for fc, g in zip(init_freqs, init_gains):
                x0.extend([float(fc), g, 2.0])
            x0 = np.array(x0)

            # Bounds: per-filter (freq in [lo, hi], gain in [-15, max_boost], Q in [min_q, max_q]).
            lb, ub = [], []
            for _ in range(num_filters):
                lb.extend([range_lo, -15.0, min_q])
                ub.extend([range_hi, max_boost, max_q])

            # Penalty weights scaled to RMS magnitude. sqrt(N) so the
            # single scalar penalty term matches the N per-point residuals
            # when summed in quadrature.
            n_points = len(freqs_arr)
            mean_weight = (
                preserve_mean_strength * float(np.sqrt(n_points))
                if preserve_mean else 0.0
            )

            def residuals(x: np.ndarray) -> np.ndarray:
                """Per-frequency residual: target - (measured + sum of filter responses)."""
                correction = np.zeros_like(freqs_arr)
                gains = np.empty(num_filters)
                centres = np.empty(num_filters)
                for i in range(num_filters):
                    fc, g, q = float(x[3 * i]), float(x[3 * i + 1]), float(x[3 * i + 2])
                    centres[i] = fc
                    gains[i] = g
                    for j, fj in enumerate(freqs_arr):
                        correction[j] += _biquad_response(fj, "peaking", fc, g, q)

                base = measured_arr + correction - target_arr
                extras: list[np.ndarray] = [base]

                # preserve_mean: penalise net DC shift of the correction so
                # the LM optimiser can't satisfy L2 by riding the level up
                # or down across the whole band.
                if mean_weight > 0:
                    extras.append(np.array([mean_weight * float(np.mean(correction))]))

                # Boost-Q penalty: one slot per filter — non-zero only when
                # the filter is a boost (positive gain) with Q below
                # max_q_boost. Penalises low-Q positive-gain filters that
                # otherwise bleed broadband level up.
                if max_q_boost is not None:
                    bq = np.zeros(num_filters)
                    for i in range(num_filters):
                        if gains[i] > 0:
                            q_i = float(x[3 * i + 2])
                            if q_i < max_q_boost:
                                bq[i] = (
                                    boost_q_penalty
                                    * (max_q_boost - q_i)
                                    * gains[i]
                                )
                    extras.append(bq)

                # Doublet penalty: one slot per unordered filter pair so the
                # residual vector is a fixed size (scipy requires it). Each
                # slot is non-zero only when the pair is both close (within
                # doublet_max_hz) and opposing-sign; magnitude scales with
                # the geometric mean of the gains and falls off linearly
                # with spacing.
                if doublet_penalty > 0 and num_filters > 1:
                    n_pairs = num_filters * (num_filters - 1) // 2
                    penalties = np.zeros(n_pairs)
                    k = 0
                    for i in range(num_filters):
                        for j in range(i + 1, num_filters):
                            spacing = abs(centres[i] - centres[j])
                            if spacing < doublet_max_hz and gains[i] * gains[j] < 0:
                                strength = float(np.sqrt(abs(gains[i] * gains[j])))
                                proximity = 1.0 - spacing / doublet_max_hz
                                penalties[k] = doublet_penalty * strength * proximity
                            k += 1
                    extras.append(penalties)

                return np.concatenate(extras) if len(extras) > 1 else base

            try:
                result = least_squares(
                    residuals, x0, bounds=(lb, ub),
                    method="trf", max_nfev=400,
                )
            except Exception as exc:
                return _err(f"least_squares failed: {exc}")

            fit_filters: list[dict] = []
            for i in range(num_filters):
                fc = float(result.x[3 * i])
                g = float(result.x[3 * i + 1])
                q = float(result.x[3 * i + 2])
                # Drop filters whose gain is effectively zero — LM may park a
                # redundant slot at 0 dB, and that just wastes a PEQ slot.
                if abs(g) < 0.15:
                    continue
                fit_filters.append({
                    "type": "peaking",
                    "freq": round(fc, 1),
                    "gain_db": round(g, 2),
                    "q": round(q, 2),
                })
            fit_filters.sort(key=lambda f: f["freq"])

            # Final RMS is computed from the base residuals only — we don't
            # want the penalty terms inflating the reported number. Rebuild
            # the correction from the optimiser's solution.
            final_correction = np.zeros_like(freqs_arr)
            for i in range(num_filters):
                fc = float(result.x[3 * i])
                g = float(result.x[3 * i + 1])
                q = float(result.x[3 * i + 2])
                for j, fj in enumerate(freqs_arr):
                    final_correction[j] += _biquad_response(fj, "peaking", fc, g, q)
            base_resid = measured_arr + final_correction - target_arr
            rms_after = float(np.sqrt(np.mean(base_resid ** 2)))
            mean_correction = float(np.mean(final_correction))

            result_payload: dict = dict(
                session_id=session_id,
                freq_range=[range_lo, range_hi],
                num_filters_requested=num_filters,
                num_filters_returned=len(fit_filters),
                rms_before=round(rms_before, 3),
                rms_after=round(rms_after, 3),
                rms_improvement=round(rms_before - rms_after, 3),
                mean_correction_db=round(mean_correction, 3),
                filters=fit_filters,
                optimizer_status=int(result.status),
                optimizer_message=str(result.message),
                excluded_geometry_points=excluded_geometry_points,
                geometry_bands=[
                    {"lo_hz": round(lo, 1), "hi_hz": round(hi, 1)}
                    for lo, hi in geometry_ranges
                ],
            )
            if anchored_reference_spl is not None:
                result_payload["anchored_reference_spl"] = round(anchored_reference_spl, 2)
            if preserve_mean_suppressed:
                result_payload["preserve_mean_suppressed"] = True
            if anchor_warning:
                result_payload["anchor_warning"] = anchor_warning
            return _ok(**result_payload)

        # ── Single-filter grid search (N == 1) ──────────────────────────────
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
            _no_improve: dict = dict(
                session_id=session_id,
                message="no filter improves the response in this range",
                rms_before=round(rms_before, 3),
            )
            if preserve_mean_suppressed:
                _no_improve["preserve_mean_suppressed"] = True
            if anchor_warning:
                _no_improve["anchor_warning"] = anchor_warning
            return _ok(**_no_improve)

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

        _single_payload: dict = dict(
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
        if preserve_mean_suppressed:
            _single_payload["preserve_mean_suppressed"] = True
        if anchor_warning:
            _single_payload["anchor_warning"] = anchor_warning
        return _ok(**_single_payload)
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


async def _get_geometry_band_ranges(
    session_id: int, min_hz: float, max_hz: float,
) -> list[tuple[float, float]]:
    """Return per-1/3-octave (lo_hz, hi_hz) ranges classified as 'geometry'.

    Thin wrapper around _tool_analyze_phase used by compute_deviation and
    anchor_target to auto-exclude near-π cancellation bands — EQ can't fix
    them, and letting them dominate RMS drives the driving agent to chase
    a convergence target that isn't physically reachable. Returns [] if
    analyze_phase has no phase data or fails entirely.
    """
    result = await _tool_analyze_phase(
        session_id=session_id, min_hz=min_hz, max_hz=max_hz,
    )
    if not result.get("ok") or not result.get("has_phase_data"):
        return []
    out: list[tuple[float, float]] = []
    for band in result.get("bands", []):
        if band.get("classification") != "geometry":
            continue
        centre = float(band["freq_hz"])
        factor = 2 ** (1 / 6)
        out.append((centre / factor, centre * factor))
    return out


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

        # Per-bin phase difference on a common frequency grid.
        # Resample B onto A's grid so we can subtract phases bin-for-bin
        # before averaging (critical: averaging wrapped radians is wrong;
        # we take the complex-vector mean of exp(j·Δφ) per band instead).
        phase_b_on_a = np.interp(freqs_a, freqs_b, phase_b)
        spl_b_on_a = np.interp(freqs_a, freqs_b, spl_b)
        delta_phase = phase_b_on_a - phase_a  # per-bin phase difference (rad)

        bands = []
        band_centres_used: list[float] = []
        band_delta_phase_rad: list[float] = []  # unwrapped-representative Δφ per band
        band_weights: list[float] = []
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

            # Complex unit-vector mean of Δφ across the band (wrap-safe).
            delta_band = delta_phase[mask_a]
            mean_vec = np.mean(np.exp(1j * delta_band))
            phase_diff_wrapped = float(np.angle(mean_vec))
            # |mean_vec| ∈ [0,1] — high when per-bin Δφ is consistent,
            # low when phase scatters (unreliable band, low weight).
            concentration = float(np.abs(mean_vec))
            phase_diff_deg = round(math.degrees(phase_diff_wrapped), 1)

            # Per-bin average absolute phase (for predicted-sum vector math).
            avg_phase_a = float(np.angle(np.mean(np.exp(1j * phase_a[mask_a]))))
            avg_phase_b = float(np.angle(np.mean(np.exp(1j * phase_b_on_a[mask_a]))))

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
                "phase_concentration": round(concentration, 3),
                "predicted_sum_db": round(sum_spl, 1),
                "reinforcement_db": reinforcement_db,
                "classification": classification,
            })
            band_centres_used.append(centre)
            band_delta_phase_rad.append(phase_diff_wrapped)
            band_weights.append(concentration)

        # Summary statistics
        cancelling = [b for b in bands if b["classification"] == "cancelling"]
        reinforcing = [b for b in bands if b["classification"] == "reinforcing"]

        # ── Delay estimate from phase-slope fit ──────────────────────
        # Δφ(f) ≈ −2π·Δt·f  (sub B trailing sub A by Δt seconds).
        # Weighted least-squares fit of UNWRAPPED Δφ vs f over the band
        # centres gives an unambiguous delay recommendation.
        delay_estimate = None
        if len(band_centres_used) >= 3:
            f_arr = np.array(band_centres_used, dtype=float)
            p_arr = np.unwrap(np.array(band_delta_phase_rad, dtype=float))
            w_arr = np.array(band_weights, dtype=float)
            # Fit p = m*f + b (weighted). delay = -m / (2π).
            W = np.diag(w_arr)
            A = np.vstack([f_arr, np.ones_like(f_arr)]).T
            try:
                lhs = A.T @ W @ A
                rhs = A.T @ W @ p_arr
                slope, intercept = np.linalg.solve(lhs, rhs)
                delay_ms = -float(slope) / (2.0 * math.pi) * 1000.0
                # R² as a confidence signal
                pred = A @ np.array([slope, intercept])
                ss_res = float(np.sum(w_arr * (p_arr - pred) ** 2))
                ss_tot = float(np.sum(w_arr * (p_arr - np.average(p_arr, weights=w_arr)) ** 2))
                r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
                delay_estimate = {
                    "delay_ms": round(delay_ms, 3),
                    "interpretation": (
                        f"sub_b trails sub_a by {delay_ms:+.2f} ms "
                        "(positive → apply delay_ms to sub_a; negative → apply |delay_ms| to sub_b)"
                    ),
                    "fit_r2": round(r2, 3),
                    "fit_intercept_deg": round(math.degrees(float(intercept)), 1),
                    "n_bands": len(band_centres_used),
                    "mean_concentration": round(float(np.mean(w_arr)), 3),
                }
            except np.linalg.LinAlgError:
                delay_estimate = None

        return _ok(
            session_a=session_a,
            session_b=session_b,
            bands=bands,
            reinforcing_bands=len(reinforcing),
            cancelling_bands=len(cancelling),
            delay_estimate=delay_estimate,
            note="Phase diff near 0°=reinforcing (+6dB ideal), near 180°=cancelling (deep null). "
                 "Use delay_estimate.delay_ms for alignment — it's a phase-slope fit across bands "
                 "(unambiguous, unlike single-band Δφ). High fit_r2 and mean_concentration → trust it; "
                 "low values → phase is scattered, rely on polarity/positioning instead. "
                 "Cancelling bands after alignment cannot be fixed with EQ — consider repositioning.",
        )
    except Exception as exc:
        return _err(f"compare_sub_phase failed: {exc}")


async def _tool_optimize_sub_alignment(
    session_ids: list[int],
    target_curve: dict | None = None,
    min_hz: float = 20.0,
    max_hz: float = 120.0,
    max_delay_ms: float = 30.0,
    search_polarity: bool = True,
    gain_search_db: float = 3.0,
    priority_band: list[float] | None = None,
    seed: int = 42,
) -> dict:
    """MSO-style numerical sub alignment.

    Given N solo-sub session IDs (one measurement per sub, all others muted),
    search per-sub (delay_ms, gain_db, polarity_inverted) that minimizes the
    RMS deviation of the PREDICTED combined FR vs target in [min_hz, max_hz].

    When target_curve is None we minimize the std-dev of the combined FR in
    band (flatness objective). Pass a target_curve = {"points":[{"freq","spl"},…]}
    to optimize against e.g. Harman or cinema-bass.

    ``priority_band``: optional [lo_hz, hi_hz]. When supplied, the objective
    weights points inside this band 3× higher than baseline, so the optimizer
    preferentially drives the deepest-null elimination in that range. Use to
    collapse the deep-bass-priority + wideband 2-call workflow (recipe
    Phase 1.5) into one call: pass ``priority_band=[20, 50]`` and the
    optimizer attacks deep-bass nulls without a separate narrow re-pass.

    Bounds:
      delay_ms  ∈ [0, max_delay_ms]  (lower-bound 0: optimizer delays leading
                                       subs rather than negatively delaying
                                       trailing subs)
      gain_db   ∈ [-gain_search_db, +gain_search_db]  (level match)
      polarity  ∈ {0, 1}             (continuous in DE, snapped on return)

    Returns per-sub recommendations + predicted combined FR + improvement_db
    + per_band_polarity diagnostic (for each 1/3-octave band, indicates
    whether flipping each sub's polarity individually would IMPROVE the
    band's SPL — surfaces the polarity insight that the global optimizer
    can miss when its objective averages across the full band).
    """
    try:
        from .storage import SessionStore
        import numpy as np

        if not session_ids or len(session_ids) < 2:
            return _err("optimize_sub_alignment: need at least 2 session_ids")

        store = SessionStore()
        sessions = store.list_sessions()
        by_id = {s.id: s for s in sessions}
        subs = []
        for sid in session_ids:
            s = by_id.get(sid)
            if s is None:
                return _err(f"session {sid} not found")
            if not s.impulse_response:
                return _err(f"session {sid} has no impulse_response")
            if not s.start_fr or not s.start_fr.sample_rate:
                return _err(f"session {sid} has no sample_rate")
            subs.append(s)

        sr = int(subs[0].start_fr.sample_rate)
        for s in subs:
            if int(s.start_fr.sample_rate) != sr:
                return _err(f"session {s.id}: sample_rate mismatch ({s.start_fr.sample_rate} vs {sr})")

        # Pad all IRs to the max length so shift+sum aligns sample-for-sample.
        irs = [np.asarray(s.impulse_response, dtype=np.float64) for s in subs]
        n = max(len(ir) for ir in irs)
        irs_padded = np.stack([np.pad(ir, (0, n - len(ir))) for ir in irs])

        # Frequency grid for the combined FR.
        freqs = np.fft.rfftfreq(n, d=1.0 / sr)
        in_band = (freqs >= min_hz) & (freqs <= max_hz)
        if not np.any(in_band):
            return _err(f"no frequency bins in [{min_hz}, {max_hz}] Hz")

        # Target vector over the full rfft grid (only evaluated in-band).
        target_db: "np.ndarray | None" = None
        if target_curve is not None:
            points = target_curve.get("points") if isinstance(target_curve, dict) else None
            if not points:
                return _err("target_curve must be {'points':[{freq,spl},…]}")
            tgt_f = np.array([float(p["freq"]) for p in points], dtype=np.float64)
            tgt_spl = np.array([float(p["spl"]) for p in points], dtype=np.float64)
            order = np.argsort(tgt_f)
            tgt_f = tgt_f[order]
            tgt_spl = tgt_spl[order]
            # log-freq interpolation, dB is linear
            target_db = np.interp(np.log(np.maximum(freqs, 1e-6)), np.log(tgt_f), tgt_spl)
        else:
            # Default objective uses a single-target vector = per-frequency
            # "achievable ceiling" (max solo-FR across subs). Reaching it means
            # subs sum constructively; falling below means cancellation.
            per_sub_db = np.stack([
                20.0 * np.log10(np.abs(np.fft.rfft(ir)) + 1e-12) for ir in irs_padded
            ])
            target_db = np.max(per_sub_db, axis=0)

        N_subs = len(subs)
        max_delay_samples = int(max_delay_ms * sr / 1000.0)

        def combined_fr_db(params: "np.ndarray") -> "np.ndarray":
            combined = np.zeros(n, dtype=np.float64)
            for i in range(N_subs):
                delay_ms = float(params[3 * i])
                gain_db = float(params[3 * i + 1])
                pol = float(params[3 * i + 2])
                d_samples = int(round(delay_ms * sr / 1000.0))
                if d_samples >= n:
                    d_samples = n - 1
                shifted = np.zeros(n, dtype=np.float64)
                if d_samples == 0:
                    shifted = irs_padded[i].copy()
                else:
                    shifted[d_samples:] = irs_padded[i][:n - d_samples]
                sign = -1.0 if pol > 0.5 else 1.0
                g_lin = 10.0 ** (gain_db / 20.0)
                combined += sign * g_lin * shifted
            H = np.fft.rfft(combined)
            return 20.0 * np.log10(np.abs(H) + 1e-12)

        # Frequency weights: linear ramp from 1.0 at min_hz to 0.1 at max_hz.
        # Sub calibration cares overwhelmingly about the low end (20–80 Hz is
        # where the subs actually live vs. mains taking over around 80 Hz).
        # Without weighting, the optimizer can happily trade 3 dB at 50 Hz for
        # 5 dB at 120 Hz — a bad deal for a sub. Weighting steers it toward
        # fixes in the band that actually matters.
        in_band_freqs = freqs[in_band]
        freq_weights = np.clip(
            1.0 - (in_band_freqs - min_hz) / max(max_hz - min_hz, 1e-9) * 0.9,
            0.1, 1.0,
        )

        # Priority-band boost: when caller specifies ``priority_band``,
        # multiply weights inside that band by 3.0 so the optimizer
        # prefers fixing nulls there over flatness elsewhere. Collapses
        # the wideband + narrowband 2-call workflow into one call
        # (recipe Phase 1.5).
        if priority_band is not None:
            try:
                pb_lo, pb_hi = float(priority_band[0]), float(priority_band[1])
                pb_mask = (in_band_freqs >= pb_lo) & (in_band_freqs <= pb_hi)
                freq_weights = np.where(pb_mask, freq_weights * 3.0, freq_weights)
            except (IndexError, TypeError, ValueError):
                pass  # invalid priority_band — silently ignore

        # Coherence weights: bins with low coherence (<0.7) carry substantial
        # measurement noise and should influence the optimizer proportionally
        # less. Use the minimum coherence across all supplied solo sessions —
        # if ANY sub is unreliable at a frequency, the combined-response
        # prediction at that frequency is unreliable too. Missing coherence
        # (legacy sessions) falls back to weight=1.0.
        coh_arr: "np.ndarray | None" = None
        try:
            per_sub_coh = []
            for s in subs:
                if s.start_fr and s.start_fr.coherence:
                    c_vals = np.array(s.start_fr.coherence, dtype=np.float64)
                    c_f = np.array(s.start_fr.frequencies, dtype=np.float64)
                    # Interp coherence onto the full freq grid then mask in-band
                    c_interp = np.interp(freqs, c_f, c_vals)
                    per_sub_coh.append(c_interp)
            if per_sub_coh:
                coh_arr = np.minimum.reduce(per_sub_coh)[in_band]
                coh_arr = np.clip(coh_arr, 0.1, 1.0)
        except Exception:
            coh_arr = None

        combined_weights = (
            freq_weights * coh_arr if coh_arr is not None else freq_weights
        )

        def objective(params: "np.ndarray") -> float:
            fr = combined_fr_db(params)
            in_fr = fr[in_band]
            in_t = target_db[in_band]
            # Asymmetric penalty: one-sided RMS below the target ceiling. Being
            # ABOVE the per-freq ceiling (rare — requires N subs summing
            # coherently at a freq where one sub alone was already near max) is
            # fine; being below means cancellation, which is what we're trying
            # to fix. One-sided avoids the "trivially flat at -inf is good"
            # failure mode of a symmetric-RMS objective on a flatness target.
            below = np.maximum(0.0, in_t - in_fr)
            weighted = combined_weights * below
            return float(np.sqrt(np.mean(weighted ** 2)))

        # Bounds: delay ∈ [0, max_delay_ms], gain ∈ [±gain], polarity ∈ [0, 1].
        # Polarity is fixed to false for sub_0 — absolute polarity is
        # unobservable in sub-only optimization (inverting all subs gives an
        # acoustically identical predicted combined response), so we collapse
        # the 2^N polarity space to 2^(N-1) by anchoring sub_0. This makes
        # the result CANONICAL: any sub with polarity_inverted=true in the
        # output is flipped RELATIVE to sub_0, which is the only meaningful
        # signal the optimizer can produce. Without this, the search can
        # arbitrarily return all-inverted (numerically equivalent to all-
        # normal) and confuse callers into applying an absolute polarity
        # flip that does nothing for sub-vs-sub interaction.
        pol_hi = 1.0 if search_polarity else 0.0
        bounds = []
        for i in range(N_subs):
            bounds.append((0.0, float(max_delay_ms)))
            bounds.append((-float(gain_search_db), float(gain_search_db)))
            if i == 0:
                bounds.append((0.0, 0.0))  # sub_0 polarity anchored to false
            else:
                bounds.append((0.0, pol_hi))

        # Baseline: everyone aligned at 0 / 0 / 0 — current pre-optimization state.
        baseline_params = np.zeros(3 * N_subs)
        baseline_err = objective(baseline_params)

        from scipy.optimize import differential_evolution
        result = differential_evolution(
            objective,
            bounds=bounds,
            maxiter=200,
            popsize=15,
            tol=1e-4,
            seed=seed,
            polish=True,
        )

        # Build per-sub recommendations.
        per_sub = []
        best = result.x
        for i, s in enumerate(subs):
            delay_ms = round(float(best[3 * i]), 3)
            gain_db = round(float(best[3 * i + 1]), 2)
            polarity_inverted = bool(best[3 * i + 2] > 0.5)
            per_sub.append({
                "session_id": s.id,
                "label": s.label,
                "delay_ms": delay_ms,
                "gain_db": gain_db,
                "polarity_inverted": polarity_inverted,
            })

        # Normalize to MINIMUM-LATENCY form. Inter-sub alignment depends only
        # on the *delta* between subs; an absolute offset added to every sub's
        # delay shifts the whole sub-chain in time without changing how the
        # subs sum at the listening position. The differential_evolution
        # optimizer often returns recommendations with all delays > 0 because
        # the search bounds [0, max_delay_ms] are symmetric and the cost
        # surface is flat with respect to the common offset. Returning those
        # absolute delays as-is means the caller adds 15-25 ms of pure latency
        # to the sub chain for nothing — that latency then pushes the
        # sub-vs-mains offset out of alignment and burns Audyssey distance-
        # push budget. Subtract the minimum so the trailing sub gets 0 and
        # leading subs get only the delta needed to catch up. The combined FR
        # is mathematically identical; only the absolute time anchor changes.
        delays = [r["delay_ms"] for r in per_sub]
        if delays:
            min_delay = min(delays)
            for r in per_sub:
                r["delay_ms"] = round(r["delay_ms"] - min_delay, 3)

        optimized_err = float(result.fun)
        improvement_db = round(baseline_err - optimized_err, 2)

        # Return the predicted combined FR on the native grid for downstream sim/plot.
        predicted_combined_db = combined_fr_db(best).tolist()

        # Convenience: band-limited predicted FR summary.
        in_band_freqs = freqs[in_band].tolist()
        in_band_pred = [float(x) for x in combined_fr_db(best)[in_band]]

        # Per-band polarity diagnostic. For each 1/3-octave centre,
        # compute the combined SPL with the optimizer's recommendation
        # vs the same recommendation with each non-anchored sub flipped
        # individually. If a flip improves SPL by >2 dB in a band, the
        # global optimizer's polarity choice is suboptimal at that band
        # — usually because the wideband objective averaged across bands
        # masked a per-band cancellation. Surfacing this lets the LLM
        # decide per-band rather than only seeing the global answer.
        third_octave = [25.0, 31.5, 40.0, 50.0, 63.0, 80.0, 100.0, 125.0]
        half_step = 2 ** (1 / 6)
        per_band_polarity: list[dict] = []
        try:
            current_fr = combined_fr_db(best)
            for centre in third_octave:
                if centre < min_hz or centre > max_hz:
                    continue
                lo, hi = centre / half_step, centre * half_step
                mask = (freqs >= lo) & (freqs < hi)
                if not np.any(mask):
                    continue
                current_band_spl = float(np.mean(current_fr[mask]))
                flips = []
                for i in range(1, N_subs):  # sub_0 is anchored
                    flipped = best.copy()
                    flipped[3 * i + 2] = 1.0 - best[3 * i + 2]
                    flipped_fr = combined_fr_db(flipped)
                    flipped_band_spl = float(np.mean(flipped_fr[mask]))
                    delta = flipped_band_spl - current_band_spl
                    if delta > 2.0:
                        flips.append({
                            "session_id": subs[i].id,
                            "spl_gain_if_flipped_db": round(delta, 1),
                        })
                if flips:
                    per_band_polarity.append({
                        "freq_hz": centre,
                        "current_spl_db": round(current_band_spl, 1),
                        "flips_that_help": flips,
                    })
        except Exception:
            per_band_polarity = []

        return _ok(
            per_sub=per_sub,
            band_hz=[min_hz, max_hz],
            priority_band=priority_band if priority_band else None,
            baseline_error_db=round(baseline_err, 3),
            optimized_error_db=round(optimized_err, 3),
            improvement_db=improvement_db,
            objective="rms_vs_target" if target_curve is not None else "rms_vs_per_freq_max",
            predicted_combined={
                "frequencies": in_band_freqs,
                "spl_db": in_band_pred,
            },
            per_band_polarity=per_band_polarity,
            converged=bool(result.success),
            n_evaluations=int(result.nfev),
            note=(
                "Per-sub recommendations minimize predicted combined-FR error in-band. "
                "Apply delay via set_delay, gain via set_output_gain, polarity via "
                "set_polarity per session_id. Delays are in MINIMUM-LATENCY form: "
                "the trailing sub is anchored at 0 ms; leading subs get only the "
                "inter-sub delta. ``per_band_polarity`` lists bands where flipping "
                "an individual sub would gain >2 dB SPL vs the global recommendation "
                "— useful for catching cancellations the wideband objective averaged "
                "out. ``priority_band`` (if set) weighted that range 3× in the "
                "objective for deep-bass-priority alignment."
            ),
        )
    except Exception as exc:
        return _err(f"optimize_sub_alignment failed: {exc}")


async def _tool_sweep_inter_sub_delay(
    session_ids: list[int],
    sub_polarity: list[bool] | None = None,
    sub_gain_db: list[float] | None = None,
    base_delays_ms: list[float] | None = None,
    priority_band: list[float] = [28.0, 50.0],
    sweep_range_ms: float = 2.0,
    step_ms: float = 0.25,
) -> dict:
    """Automated inter-sub delay sweep — predicts deepest-null depth in
    the priority band for each delay step on the trailing sub.

    Replaces the manual ±2 ms human-driven sweep in recipe Phase 1.5.
    Takes post-alignment solo session_ids (one per sub), the polarity /
    gain / base-delay state already applied to each sub, and sweeps the
    *non-leading* sub's delay across ``[base − sweep_range, base + sweep_range]``
    in ``step_ms`` increments. For each step, predicts the combined FR
    by shift-and-summing the IRs and computes the deepest 1/3-octave-band
    null depth in ``priority_band`` (default 28-50 Hz). Returns the step
    that minimizes the deepest null.

    Args:
        session_ids: solo measurement session IDs (2 subs assumed; for
            3+ subs, pass the two whose inter-delay you want to sweep).
        sub_polarity: list of bools, one per sub. Defaults to all False.
        sub_gain_db: list of floats, one per sub. Defaults to all 0.
        base_delays_ms: list of floats, one per sub — the current delay
            already applied. The trailing sub's delay is swept around
            its base value; the leading sub's stays put. Defaults to
            all 0.
        priority_band: ``[lo_hz, hi_hz]`` for null detection. Default
            [28, 50] = the deep-bass priority band.
        sweep_range_ms: ± range around the trailing sub's base delay.
        step_ms: sweep granularity. Default 0.25 ms = 9 measurements
            for ±2 ms range.

    Returns each step's predicted deepest-null depth and the optimal
    delta. Apply the recommended delta via ``set_delay`` on the
    trailing sub.
    """
    try:
        from .storage import SessionStore
        import numpy as np

        if not session_ids or len(session_ids) < 2:
            return _err("sweep_inter_sub_delay: need at least 2 session_ids")

        store = SessionStore()
        sessions = store.list_sessions()
        by_id = {s.id: s for s in sessions}
        subs = []
        for sid in session_ids:
            s = by_id.get(sid)
            if s is None:
                return _err(f"session {sid} not found")
            if not s.impulse_response:
                return _err(f"session {sid} has no impulse_response")
            if not s.start_fr or not s.start_fr.sample_rate:
                return _err(f"session {sid} has no sample_rate")
            subs.append(s)

        N = len(subs)
        sr = int(subs[0].start_fr.sample_rate)
        for s in subs:
            if int(s.start_fr.sample_rate) != sr:
                return _err(
                    f"session {s.id}: sample_rate mismatch "
                    f"({s.start_fr.sample_rate} vs {sr})"
                )

        polarity = list(sub_polarity) if sub_polarity else [False] * N
        gain_db = list(sub_gain_db) if sub_gain_db else [0.0] * N
        base_delays = list(base_delays_ms) if base_delays_ms else [0.0] * N
        if len(polarity) != N or len(gain_db) != N or len(base_delays) != N:
            return _err(
                f"polarity/gain/base_delays must each have {N} elements"
            )

        # Identify the trailing sub (the one whose base delay is highest —
        # min-latency form puts trailing sub at 0, but if both are at 0
        # we pick sub_1 by convention).
        trailing_idx = int(max(range(N), key=lambda i: base_delays[i]))
        if all(d == base_delays[0] for d in base_delays):
            trailing_idx = 1  # default to sub_1 when no leader is identifiable

        # Pad IRs to the longest length.
        irs = [np.asarray(s.impulse_response, dtype=np.float64) for s in subs]
        n = max(len(ir) for ir in irs) + int(
            (max(base_delays) + sweep_range_ms + 5.0) * sr / 1000.0
        )
        irs_padded = np.stack([
            np.pad(ir, (0, n - len(ir))) for ir in irs
        ])

        freqs = np.fft.rfftfreq(n, d=1.0 / sr)
        pb_lo, pb_hi = float(priority_band[0]), float(priority_band[1])

        # Build 1/3-octave centres in the priority band.
        third_octave_all = [25.0, 31.5, 40.0, 50.0, 63.0, 80.0, 100.0]
        centres = [c for c in third_octave_all if pb_lo <= c <= pb_hi]
        half_step = 2 ** (1 / 6)

        def predict_null(trailing_delay_ms: float) -> dict:
            combined = np.zeros(n, dtype=np.float64)
            for i in range(N):
                d_ms = (
                    trailing_delay_ms if i == trailing_idx else base_delays[i]
                )
                d_samples = int(round(d_ms * sr / 1000.0))
                if d_samples >= n:
                    d_samples = n - 1
                shifted = np.zeros(n, dtype=np.float64)
                if d_samples == 0:
                    shifted = irs_padded[i].copy()
                else:
                    shifted[d_samples:] = irs_padded[i][:n - d_samples]
                sign = -1.0 if polarity[i] else 1.0
                g_lin = 10.0 ** (gain_db[i] / 20.0)
                combined += sign * g_lin * shifted
            H = np.abs(np.fft.rfft(combined)) + 1e-12
            spl_db = 20.0 * np.log10(H)
            band_mean = float(np.mean(spl_db[(freqs >= pb_lo) & (freqs <= pb_hi)]))
            band_minima = []
            for centre in centres:
                lo, hi = centre / half_step, centre * half_step
                mask = (freqs >= lo) & (freqs < hi)
                if not np.any(mask):
                    continue
                band_minima.append(float(np.min(spl_db[mask])))
            if not band_minima:
                return {"deepest_null_db": 0.0, "band_mean_db": band_mean}
            return {
                "deepest_null_db": min(band_minima),
                "band_mean_db": band_mean,
                "depth_below_mean_db": min(band_minima) - band_mean,
            }

        # Sweep around the trailing sub's base delay.
        steps = []
        center = base_delays[trailing_idx]
        n_steps = int(round(sweep_range_ms / step_ms))
        for k in range(-n_steps, n_steps + 1):
            d = max(0.0, center + k * step_ms)
            pred = predict_null(d)
            steps.append({
                "trailing_delay_ms": round(d, 3),
                "delta_from_base_ms": round(d - center, 3),
                **{k: round(v, 2) for k, v in pred.items()},
            })

        # Pick the step with the SHALLOWEST deepest null (highest min SPL).
        # Tie-break by smallest |delta_from_base| to prefer minimal change.
        steps_sorted = sorted(
            steps,
            key=lambda x: (
                -x["deepest_null_db"],
                abs(x["delta_from_base_ms"]),
            ),
        )
        best = steps_sorted[0]

        return _ok(
            session_ids=session_ids,
            trailing_sub_session_id=subs[trailing_idx].id,
            trailing_sub_label=subs[trailing_idx].label,
            priority_band_hz=[pb_lo, pb_hi],
            base_delay_ms=center,
            recommended_delay_ms=best["trailing_delay_ms"],
            recommended_delta_ms=best["delta_from_base_ms"],
            recommended_deepest_null_db=best["deepest_null_db"],
            baseline_deepest_null_db=next(
                (s["deepest_null_db"] for s in steps
                 if s["delta_from_base_ms"] == 0.0),
                None,
            ),
            steps=steps,
            note=(
                f"Swept {len(steps)} delay steps in "
                f"±{sweep_range_ms} ms / {step_ms} ms-step. Recommended "
                f"delay {best['trailing_delay_ms']:.3f} ms shallowest the "
                f"deepest null in {pb_lo:.0f}-{pb_hi:.0f} Hz to "
                f"{best['deepest_null_db']:.1f} dB. Apply via "
                f"set_delay(output_index=<trailing_sub>, delay_ms="
                f"{best['trailing_delay_ms']:.3f})."
            ),
        )
    except Exception as exc:
        return _err(f"sweep_inter_sub_delay failed: {exc}")


async def _tool_design_fir(
    session_id: int,
    target_curve: dict | None = None,
    num_taps: int = 1024,
    phase_mode: str = "minimum",
    freq_focus_hz: list[float] | None = None,
    return_coefficients: bool = True,
    preringing_ms: float = 25.0,
) -> dict:
    """Design FIR correction coefficients from a measurement.

    The LLM decides the strategy (phase mode, tap count, frequency focus).
    This tool computes the coefficients and returns them with a predicted
    response, pre-ringing estimate, and the latency the FIR will add.

    phase_mode:
      - "minimum": no pre-ringing, magnitude-only correction. Latency ≈ 0.
        Leaves modal ringing intact — the filter shortens the peak, not the
        decay. Safe default.
      - "linear": symmetric impulse, full magnitude + phase correction.
        Latency = num_taps / 2 / sample_rate. At 65 536 taps / 48 kHz this
        is 683 ms, far past the AVR's per-channel speaker-distance
        compensation range.
      - "mixed": homomorphic decomposition into a min-phase magnitude part
        and a bounded excess-phase all-pass part. The excess-phase component
        is windowed so pre-ringing stays within ``preringing_ms`` (default
        25 ms). This actively cancels modal decay while keeping the latency
        compensable via the AVR's MAINS speaker-distance setting (the FIR
        delays the sub chain only; mains must be set LARGER than physical
        to wait for the FIR-delayed sub). Below ~100 Hz the ear integrates
        over 20-30 ms so the pre-ringing is inaudible.
        Latency ≈ ``preringing_ms`` + a few ms for the min-phase core.
        Set ``preringing_ms=0`` to degenerate to minimum-phase.

    return_coefficients: when False, the coefficient array is cached server-side
    keyed by session_id and omitted from the response. Callers then apply the
    FIR via apply_fir(output_index, design_session_id=session_id). Useful when
    the full array (8k+ taps ≈ 140 KB JSON) would exceed client token budgets.

    preringing_ms: (mixed-phase only) maximum pre-ringing window in ms.
    Bounds the audio latency of the filter and the inaudible-smear window
    below 100 Hz. Default 25 ms matches bass psychoacoustic thresholds.
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
        else:  # mixed — proper homomorphic decomposition with bounded pre-ringing
            # Mixed-phase via the Kirkeby-style construction:
            #   1. Start from the linear-phase target impulse (real spectrum)
            #   2. Split it into min-phase magnitude × excess-phase all-pass
            #   3. Window the excess-phase impulse to cap pre-ringing
            #   4. Convolve the windowed excess with the min-phase core
            #
            # Result: magnitude ≈ linear-phase version (full correction), phase
            # advances the early portion of the modal decay so convolving with
            # source audio partially cancels the room's post-ringing tail.
            # Latency is bounded by the windowed excess-phase extent.
            H = fir_correction
            fir_td_full = np.fft.irfft(H, n=n_fft)

            # Min-phase core — same path as phase_mode="minimum"
            try:
                fir_min = minimum_phase(
                    fir_td_full[:num_taps * 2], method="homomorphic", n_fft=n_fft,
                )
                fir_min = fir_min[:num_taps]
            except Exception:
                log.warning("minimum_phase() failed in mixed-phase; falling back to min-phase only")
                fir_td = fir_td_full[:num_taps] * np.hanning(num_taps)
                pre_ringing_ms = 0.0
                # jump to post-mixed normalise/output path
                fir_min = fir_td
            else:
                # Excess-phase all-pass = FFT(linear) / FFT(min-phase). Magnitude
                # ≈ 1 by construction (same magnitude response); the phase of
                # H_excess encodes the group-delay difference. Guard against
                # near-zero H_min at frequencies outside the focus band where
                # both impulses have ~0 energy — division blows up otherwise.
                n_fft_ap = max(n_fft, 4 * num_taps)
                H_lin = np.fft.rfft(fir_td_full, n=n_fft_ap)
                # Centre the linear impulse so H_lin has symmetric phase; this
                # keeps the all-pass's mass near the centre of the buffer,
                # which makes windowing well-defined.
                H_min_fft = np.fft.rfft(
                    np.concatenate([fir_min, np.zeros(n_fft_ap - num_taps)]),
                    n=n_fft_ap,
                )
                eps = 1e-10 * float(np.max(np.abs(H_min_fft)) or 1.0)
                denom = H_min_fft.copy()
                denom_mag = np.abs(denom)
                small = denom_mag < eps
                if np.any(small):
                    denom[small] = eps
                H_excess = H_lin / denom
                # Normalise to strictly unit-magnitude (numerical cleanup so
                # windowing doesn't accidentally amplify magnitude).
                mag = np.abs(H_excess)
                mag[mag < 1e-12] = 1.0
                H_excess = H_excess / mag

                # Band-limit the all-pass to the correction band. Outside the
                # focus range we WANT pass-through (no phase correction, no
                # magnitude change). Letting the raw all-pass extend to full
                # bandwidth forces its time-domain impulse to be long, which
                # then loses magnitude when we window it — hurting correction
                # in the very band we're trying to fix.
                ap_freqs = np.fft.rfftfreq(n_fft_ap, d=1.0 / fir_fs)
                if freq_focus_hz:
                    focus_lo_ap, focus_hi_ap = float(freq_focus_hz[0]), float(freq_focus_hz[1])
                else:
                    focus_lo_ap, focus_hi_ap = 20.0, 200.0
                # Smooth ramp to pass-through above 1.5× focus_hi — keeps
                # magnitude smooth at the cutoff rather than a hard step.
                upper_edge = focus_hi_ap * 1.5
                ramp_width = max(1.0, focus_hi_ap * 0.25)
                ramp = np.clip((ap_freqs - upper_edge) / ramp_width, 0.0, 1.0)
                # Where ramp == 1, replace H_excess with 1.0 (pass-through).
                H_excess = H_excess * (1.0 - ramp) + 1.0 * ramp

                # Back to time domain. fftshift brings the zero-delay sample
                # to the centre of the buffer — the all-pass impulse is
                # typically symmetric-ish around there, so windowing around
                # the centre is natural.
                h_excess = np.fft.fftshift(np.fft.irfft(H_excess, n=n_fft_ap))

                # Cap pre-ringing. `preringing_ms` sets the symmetric Hann
                # half-width around the centre peak. Setting it to 0 makes
                # the window a single sample, which degenerates to min-phase.
                pre_samples = max(1, int(round(preringing_ms / 1000.0 * fir_fs)))
                total_win = 2 * pre_samples
                # Centre of the fftshifted buffer
                centre = n_fft_ap // 2
                win = np.zeros_like(h_excess)
                hann_w = np.hanning(total_win) if total_win > 1 else np.array([1.0])
                lo = max(0, centre - pre_samples)
                hi = min(n_fft_ap, lo + total_win)
                hann_w = hann_w[: hi - lo]
                win[lo:hi] = hann_w
                h_excess_windowed = h_excess * win

                # Undo the fftshift so the windowed excess is causal-ish
                # relative to a buffer of num_taps. We keep only the
                # non-zero span around the centre for the convolution.
                excess_lo = max(0, centre - pre_samples)
                excess_hi = min(n_fft_ap, centre + pre_samples)
                h_excess_short = h_excess_windowed[excess_lo:excess_hi]

                # Convolve min-phase core with the windowed excess-phase
                # all-pass. Result length is num_taps + len(excess) - 1;
                # trim back to num_taps from the front (preserving the main
                # energy peak position within the buffer).
                fir_td = np.convolve(fir_min, h_excess_short, mode="full")
                fir_td = fir_td[:num_taps]

                # Pre-ringing time is the window half-width. The peak of the
                # final impulse lands at ≈ pre_samples samples in, which is
                # also the effective latency.
                pre_ringing_ms = round(pre_samples / fir_fs * 1000, 2)

        # Normalize so peak <= 1.0
        peak = float(np.max(np.abs(fir_td)))
        if peak > 0:
            fir_td = fir_td / peak

        # Effective audio latency: position of the impulse's energy peak.
        # For min-phase this lands at sample 0 (~0 ms). For linear-phase it
        # lands at N/2. For mixed-phase it lands at the pre-ringing window
        # half-width. AVR Audio-Delay settings need to compensate for this.
        peak_idx = int(np.argmax(np.abs(fir_td)))
        latency_ms = round(peak_idx / fir_fs * 1000, 2)

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

        # Cache even when returning the full array — callers may still prefer
        # the by-reference apply path to avoid re-uploading ~140 KB of floats.
        _fir_design_cache[int(session_id)] = coefficients
        peak_abs = float(np.max(np.abs(fir_td))) if num_taps else 0.0

        # Surface the AVR's per-channel delay-buffer ceiling so the LLM can see
        # immediately whether this FIR's latency is compensable via mains
        # speaker-distance. Empirical X3800H ceiling is ~65 ms; other models
        # may differ. ``None`` means the active AVR driver does not advertise
        # a limit — caller should treat as unknown rather than infinite.
        avr_max_delay_ms = getattr(_avr, "MAX_SPEAKER_DELAY_MS", None) if _avr else None
        if avr_max_delay_ms is not None:
            avr_compensable = latency_ms <= avr_max_delay_ms
            avr_headroom_ms = round(float(avr_max_delay_ms) - latency_ms, 2)
        else:
            avr_compensable = None  # unknown — don't claim either way
            avr_headroom_ms = None

        result = {
            "session_id": session_id,
            "num_taps": num_taps,
            "phase_mode": phase_mode,
            "pre_ringing_ms": pre_ringing_ms,
            "latency_ms": latency_ms,
            "freq_resolution_hz": freq_resolution,
            "peak_abs": round(peak_abs, 6),
            "predicted_effect": predicted_bands,
            "avr_max_delay_ms": avr_max_delay_ms,
            "avr_compensable": avr_compensable,
            "avr_headroom_ms": avr_headroom_ms,
            "design_cached": True,
        }

        budget_msg = ""
        if avr_max_delay_ms is not None:
            if avr_compensable:
                budget_msg = (
                    f" AVR can compensate (max {avr_max_delay_ms}ms, "
                    f"{avr_headroom_ms}ms headroom)."
                )
            else:
                budget_msg = (
                    f" WARNING: AVR can compensate at most {avr_max_delay_ms}ms — "
                    f"this FIR exceeds the budget by "
                    f"{round(latency_ms - avr_max_delay_ms, 2)}ms. "
                    f"Reduce taps, switch to minimum-phase, or accept residual "
                    f"sub-mains misalignment."
                )

        if return_coefficients:
            result["coefficients"] = coefficients
            result["note"] = (
                f"FIR at {fir_fs}Hz, {num_taps} taps, {phase_mode} phase. "
                f"Freq resolution: {freq_resolution}Hz. Pre-ringing: {pre_ringing_ms}ms. "
                f"Latency: {latency_ms}ms (compensate via per-channel mains "
                f"speaker-distance — set mains LARGER than physical so they "
                f"wait for the FIR-delayed sub; lip-sync/Audio-Delay does NOT "
                f"help, that delays all audio uniformly).{budget_msg} "
                f"Pass coefficients to apply_fir(output_index, coefficients) or "
                f"apply_fir(output_index, design_session_id={session_id})."
            )
        else:
            result["note"] = (
                f"FIR at {fir_fs}Hz, {num_taps} taps, {phase_mode} phase. "
                f"Freq resolution: {freq_resolution}Hz. Pre-ringing: {pre_ringing_ms}ms. "
                f"Latency: {latency_ms}ms.{budget_msg} Coefficients cached "
                f"server-side; apply via apply_fir(output_index, "
                f"design_session_id={session_id})."
            )
        return _ok(**result)
    except Exception as exc:
        return _err(f"design_fir failed: {exc}")


async def _tool_design_modal_fir(
    session_id: int,
    intents: list[dict] | None = None,
    target_curve: dict | None = None,
    target_t60_ms: float = 300.0,
    peak_action_db: float = 3.0,
    short_loud_t60_factor: float = 0.5,
    short_loud_peak_db: float = 12.0,
    long_ringy_t60_factor: float = 2.0,
    anti_pulse_cancel_strength: float = 0.6,
    num_taps: int = 4096,
    max_pre_ring_ms: float = 25.0,
    samplerate: int = 48000,
    return_coefficients: bool = False,
) -> dict:
    """Design a modal-aware mixed-phase FIR with explicit per-mode treatment.

    Unlike ``design_fir`` (magnitude correction with allowed non-causality),
    this tool **actively cancels modal ringing** in the time domain by
    placing band-limited anti-pulses one half-wavelength before the main
    impulse for each mode flagged as ``anti_pulse``. Anti-pulse cancellation
    reduces both the modal peak magnitude AND the T60 decay tail, where
    pure magnitude correction only reduces the peak.

    Per-mode treatment (LLM-supplied via ``intents``, or auto-classified
    against ``target_t60_ms``):
      - ``anti_pulse``: T60 > 2× target → place inverted band-limited
        impulse half-wavelength before main. Cancels both peak and T60.
        Costs half-wavelength of pre-ring per mode (e.g. 7.14 ms at 70 Hz).
      - ``linear_notch``: T60 < 0.5× target AND peak > 12 dB → linear-phase
        precise notch. Costs ~3-5 ms pre-ring; surgical magnitude cut.
      - ``min_phase``: T60 moderately above target → conservative min-phase
        magnitude EQ. Zero pre-ring cost; reduces peak but not T60.
      - ``skip``: T60 already ≤ target, OR peak < 3 dB — leave alone.

    The ``target_t60_ms`` parameter is the room-quality target — modes
    already meeting it are skipped; modes far above it get aggressive
    treatment. Industry references:
      - 250 ms — mastering / control room
      - 300 ms — THX / Dolby reference (default)
      - 500 ms — acceptable home theater
      - >700 ms — untreated room

    The total pre-ring budget is bounded by ``max_pre_ring_ms`` (default
    25 ms — psychoacoustic threshold for sub-band content). The actual
    pre-ring used is the maximum half-wavelength + bandpass tail across
    all anti-pulse-treated modes.

    Args:
        session_id: measurement session whose ``decay_modes`` and
            ``impulse_response`` provide the modal data and base
            magnitude correction.
        intents: list of per-mode design intents. Each entry:
            ``{freq_hz, t60_ms, peak_db, treatment, cancel_strength?,
            rationale?}``. ``treatment`` ∈ {anti_pulse, linear_notch,
            min_phase, skip}. ``cancel_strength`` (0-1, default 0.6)
            controls how aggressively to cancel. If ``intents`` is None,
            the tool classifies each ``decay_mode`` from the session
            metadata using the default heuristic (T60>800ms+peak>6 →
            anti_pulse; T60<400ms+peak>12 → linear_notch; peak<3 → skip;
            else min_phase).
        target_curve: optional unified target-curve correction. When supplied,
            a min-phase magnitude correction layer is convolved into the
            modal-cancellation FIR so a single FIR delivers both T60
            reduction AND target-curve shaping. Without it the caller must
            stack PEQ on top, which can fight the anti-pulses. Shape:
            ``{"points": [{"freq": 25, "spl": 5}, ...], "band": [20, 100]}``.
            ``points`` is the absolute target SPL (anchored to the
            measurement's midband 60-100 Hz so absolute SPL drops out).
            ``band`` (optional) restricts the magnitude correction to the
            sub band; outside it, correction tapers to 0 dB.
        target_t60_ms: room-quality target T60 (default 300 ms). Modes
            already meeting this are skipped; modes far above it (T60 >
            2× target) get anti-pulse treatment. Used only for
            auto-classification when ``intents`` is None. See the
            "T60 references" table above the args.
        num_taps: FIR length (default 4096 at 8 kHz internal = 512 ms span).
        max_pre_ring_ms: maximum pre-ringing budget across all anti-pulses
            (default 25 ms). Larger budget = more aggressive modal
            cancellation but more sub-chain latency.
        return_coefficients: when False (default), coefficient array is
            cached server-side keyed by session_id and omitted from the
            response. Apply via apply_fir(output_index,
            design_session_id=session_id).

    Returns:
        per_mode_treatments: list of {freq_hz, treatment, rationale,
            anti_pulse_pre_ms?, anti_pulse_amplitude?, predicted_t60_reduction_pct?}
        pre_delay_ms: actual pre-ring used (≤ max_pre_ring_ms).
        peak_amplitude: max |coefficient| after composition + normalization.
        total_taps, sample_rate: FIR shape.
        notes: warnings (e.g. anti-pulse clipped to fit budget).
        coefficients: only when return_coefficients=True.
    """
    from .storage import SessionStore
    from .modal_fir import (
        ModalAwareFIRDesigner,
        ModeIntent,
        classify_mode_default,
        latency_budget_breakdown,
    )

    try:
        store = SessionStore()
        sessions = store.list_sessions()
        session = next((s for s in sessions if s.id == session_id), None)
        if session is None:
            return _err(f"session {session_id} not found")

        meta = session.metadata or {}
        if isinstance(meta, str):
            import json as _json
            meta = _json.loads(meta)

        decay_modes = meta.get("decay_modes") or []
        if not decay_modes:
            return _err(
                f"session {session_id} has no decay_modes in metadata; "
                f"re-measure or run analyze_decay first"
            )

        # Base correction is a passthrough impulse — the modal FIR's job is
        # to add anti-pulses (and, when ``target_curve`` is supplied, a
        # min-phase magnitude correction layer). Using the room IR here
        # would re-inject the room's resonances rather than flatten them.
        base_correction = [0.0] * num_taps
        base_correction[0] = 1.0

        # Translate intents (dicts) to ModeIntent objects, or auto-classify
        if intents:
            intent_objs = [
                ModeIntent(
                    freq_hz=float(i["freq_hz"]),
                    t60_ms=float(i.get("t60_ms", 0)),
                    peak_db=float(i.get("peak_db", 0)),
                    treatment=str(i["treatment"]),
                    cancel_strength=float(i.get("cancel_strength", 0.6)),
                    bp_q=float(i.get("bp_q", 1.5)),
                    envelope=str(i.get("envelope", "gabor")),
                    rationale=str(i.get("rationale", "")),
                )
                for i in intents
            ]
        else:
            intent_objs = [
                classify_mode_default(
                    float(m["freq_hz"]),
                    float(m["t60_ms"]),
                    float(m["peak_db"]),
                    target_t60_ms=float(target_t60_ms),
                    peak_action_db=float(peak_action_db),
                    short_loud_t60_factor=float(short_loud_t60_factor),
                    short_loud_peak_db=float(short_loud_peak_db),
                    long_ringy_t60_factor=float(long_ringy_t60_factor),
                    anti_pulse_cancel_strength=float(anti_pulse_cancel_strength),
                )
                for m in decay_modes
            ]

        # Optional unified target-curve correction. When ``target_curve`` is
        # provided, build the source-FR points (1/3-octave SPL from the
        # session) so the designer can compute the residual error and add
        # a min-phase magnitude correction layer in the same FIR.
        target_curve_db: list[tuple[float, float]] | None = None
        source_fr_db: list[tuple[float, float]] | None = None
        magnitude_focus_hz: tuple[float, float] | None = None
        if target_curve and isinstance(target_curve, dict):
            pts = target_curve.get("points") or []
            if pts:
                target_curve_db = [
                    (float(p.get("freq", p.get("freq_hz", 0))), float(p.get("spl", p.get("spl_db", 0))))
                    for p in pts
                ]
            band = target_curve.get("band") or target_curve.get("focus_hz")
            if isinstance(band, (list, tuple)) and len(band) == 2:
                magnitude_focus_hz = (float(band[0]), float(band[1]))
            # Pull session's source FR. FrequencyResponse stores raw
            # ``frequencies`` and ``spl`` arrays; the magnitude designer
            # interpolates onto its own grid so we forward them as-is.
            try:
                fr = session.start_fr
                if fr is not None and getattr(fr, "frequencies", None):
                    source_fr_db = list(zip(
                        [float(f) for f in fr.frequencies],
                        [float(s) for s in fr.spl],
                    ))
            except Exception:
                source_fr_db = None
            if not source_fr_db:
                # Fallback: pull a 1/3-octave summary from metadata if present.
                fr_bands = meta.get("third_octave_spl") or meta.get("fr_bands") or []
                if fr_bands:
                    source_fr_db = [
                        (float(b["freq_hz"]), float(b["spl_db"]))
                        for b in fr_bands
                    ]

        designer = ModalAwareFIRDesigner(
            sample_rate=int(samplerate),
            n_taps=int(num_taps),
            max_pre_ring_ms=float(max_pre_ring_ms),
        )
        coeffs, summary = designer.design(
            decay_modes=decay_modes,
            base_correction=base_correction,
            intents=intent_objs,
            target_t60_ms=float(target_t60_ms),
            peak_action_db=float(peak_action_db),
            short_loud_t60_factor=float(short_loud_t60_factor),
            short_loud_peak_db=float(short_loud_peak_db),
            long_ringy_t60_factor=float(long_ringy_t60_factor),
            anti_pulse_cancel_strength=float(anti_pulse_cancel_strength),
            target_curve_db=target_curve_db,
            source_fr_db=source_fr_db,
            magnitude_focus_hz=magnitude_focus_hz,
        )

        _fir_design_cache[int(session_id)] = list(coeffs)
        _fir_design_intent[int(session_id)] = "modal_cancel"

        result = {
            "session_id": session_id,
            "num_taps": summary.total_taps,
            "sample_rate": summary.sample_rate,
            "pre_delay_ms": round(summary.pre_delay_ms, 3),
            "pre_delay_samples": summary.pre_delay_samples,
            "peak_amplitude": round(summary.peak_amplitude, 4),
            "per_mode_treatments": summary.mode_treatments,
            "notes": summary.notes,
            "latency_budget": latency_budget_breakdown(summary),
            "design_cached": True,
            "note": (
                f"Modal-aware FIR at 8000Hz, {summary.total_taps} taps. "
                f"Pre-ring: {summary.pre_delay_ms:.1f}ms (budget {max_pre_ring_ms:.0f}ms). "
                f"{len([t for t in summary.mode_treatments if t['treatment']=='anti_pulse'])} anti-pulses. "
                f"Apply via apply_fir(output_index, design_session_id={session_id})."
            ),
        }
        if return_coefficients:
            result["coefficients"] = list(coeffs)
        return _ok(**result)
    except Exception as exc:
        return _err(f"design_modal_fir failed: {exc}")


async def _tool_set_speaker_distances(
    distances: dict[str, float],
    n_positions: int = 1,
    commit: bool = False,
    use_custom: bool = False,
) -> dict:
    """Push per-channel Audyssey distances to the AVR via direct TCP.

    Bypasses the MultEQ Editor app's UI cap (59.1 ft / 18 m on X3800H).
    Used to compensate sub-only FIR group delay by setting mains
    distances LARGER than physical, or sub LARGER than mains, so the
    AVR delays the appropriate channels.

    Channel names are Audyssey commandIds: FL, C, FR, SLA, SRA, TFL,
    TFR, TRL, TRR, SBL, SBR, SW1, SW2, SW3, SW4. Values in METERS.

    With ``commit=False`` the change is volatile (lost on AVR power
    cycle). With ``commit=True`` the AVR persists to NVRAM.

    The AVR firmware applies at most ``MAX_SPEAKER_DELAY_MS`` of delay
    per channel regardless of the configured value — see
    ``get_state().max_speaker_delay_ms``. Pushing past that is allowed
    but only the first N ms of delay actually reach the speakers.

    The driver method itself does NOT prompt; recipes/agents must
    obtain explicit user confirmation before calling this tool, per
    the signal-path-write rule.
    """
    if _avr is None:
        return _err("no AVR driver loaded")
    if not hasattr(_avr, "set_speaker_distances"):
        return _err(f"{type(_avr).__name__} does not support direct distance writes")
    if not distances:
        return _err("distances is empty")
    try:
        await _avr.set_speaker_distances(  # type: ignore[attr-defined]
            distances,
            n_positions=int(n_positions),
            commit=bool(commit),
            use_custom=bool(use_custom),
        )
    except DriverError as exc:
        return _err(f"distance push failed: {exc}")

    avr_max_delay_ms = getattr(_avr, "MAX_SPEAKER_DELAY_MS", None)

    # Check whether Audyssey distance compensation is currently active. The
    # write itself succeeds in any sound mode, but distance compensation
    # is only applied when the AVR is using Audyssey (non-Pure-Direct,
    # MultEQ on). Surfacing this catches the silent "wrote but not applied"
    # failure that's easy to miss otherwise.
    audyssey: dict | None = None
    if hasattr(_avr, "audyssey_status"):
        try:
            audyssey = await _avr.audyssey_status()  # type: ignore[attr-defined]
        except DriverError as exc:
            audyssey = {"active": None, "error": str(exc)}

    parts = ["Distances written."]
    parts.append("Persisted to NVRAM." if commit else "Volatile — pass commit=True to persist.")
    if avr_max_delay_ms:
        parts.append(f"AVR caps applied delay at {avr_max_delay_ms}ms per channel.")
    if audyssey is not None:
        if audyssey.get("active") is False:
            parts.append(
                f"WARNING: Audyssey is INACTIVE ({audyssey.get('reason') or 'see sound mode'}); "
                "distances are stored but NOT being applied to the audio path. "
                "Switch to a non-Pure-Direct sound mode with MultEQ on to use them."
            )
        elif audyssey.get("active") is None and "error" not in audyssey:
            parts.append(
                "Could not confirm Audyssey active state — verify with a measurement."
            )
        elif "error" in audyssey:
            parts.append(f"Audyssey state probe failed: {audyssey['error']}")

    return _ok(
        distances_cm={ch: round(m * 100) for ch, m in distances.items()},
        n_positions=int(n_positions),
        committed=bool(commit),
        avr_max_delay_ms=avr_max_delay_ms,
        audyssey=audyssey,
        message=" ".join(parts),
    )


# ── Audyssey FIR upload ────────────────────────────────────────────────
# Module-level cache of AVR-format polyphase-decimated coefficient vectors.
# Keyed by (cache_key, channel_id) → list[float] of length 1024 (speaker)
# or 704 (sub). Populated by ``design_avr_fir``; consumed by ``apply_avr_fir``.
_AVR_FIR_CACHE: dict[tuple[str, str], list[float]] = {}


async def _tool_design_avr_fir(
    channel_id: str,
    target_curve_db: list[dict],
    cache_key: str,
    samplerate_hz: float = 48000.0,
) -> dict:
    """Design + polyphase-decimate an AVR-format FIR for one channel.

    Pipeline: ``target_curve_db`` (per-frequency gain targets) →
    16,321-tap (speaker) / 16,055-tap (sub) impulse response →
    XT32 4-band polyphase decimation → 1024 / 704 AVR coefficients.

    The result is cached server-side keyed by (cache_key, channel_id).
    Apply via ``apply_avr_fir(cache_key=...)``.

    Args:
        channel_id: Audyssey commandId — FL, C, FR, SLA, SRA, TFL,
            TFR, TRL, TRR, SBL, SBR, SW1, SW2, SW3, SW4, LFE.
        target_curve_db: list of ``{freq_hz: float, gain_db: float}``
            points defining the desired EQ curve. Outside the supplied
            frequency range gain tapers to 0 dB. Sub channels typically
            specify points across 20-200 Hz; speakers 20-20,000 Hz.
        cache_key: opaque caller-chosen identifier — usually a session
            id or "<sid>-iter-N". Pair with ``apply_avr_fir`` to commit.
        samplerate_hz: design sample rate. Default 48 kHz (matches the
            AVR's native processing rate for the 48 kHz coefficient bank).

    Returns ``{ok, channel_id, cache_key, fir_taps, peak_amplitude,
    is_sub}``.
    """
    from .audyssey_fir import (
        convert_xt32, design_correction_ir, get_channel_byte, is_sub_channel,
    )

    if not target_curve_db:
        return _err("target_curve_db is empty")
    try:
        # Validate the channel exists before doing the IR FFT.
        get_channel_byte(channel_id, "XT32")
    except ValueError as exc:
        return _err(str(exc))

    freqs: list[float] = []
    gains: list[float] = []
    for pt in target_curve_db:
        try:
            freqs.append(float(pt["freq_hz"]))
            gains.append(float(pt["gain_db"]))
        except (KeyError, TypeError, ValueError) as exc:
            return _err(f"bad target_curve_db entry {pt!r}: {exc}")
    # Sort by frequency so the interpolator sees monotonic input.
    order = sorted(range(len(freqs)), key=lambda i: freqs[i])
    freqs = [freqs[i] for i in order]
    gains = [gains[i] for i in order]

    is_sub = is_sub_channel(channel_id)
    try:
        ir = design_correction_ir(
            target_freqs_hz=freqs,
            target_gain_db=gains,
            is_sub=is_sub,
            samplerate_hz=float(samplerate_hz),
        )
        coefs = convert_xt32(ir)
    except (ValueError, RuntimeError) as exc:
        return _err(f"FIR design failed: {exc}")

    _AVR_FIR_CACHE[(str(cache_key), channel_id)] = coefs
    import numpy as np
    arr = np.asarray(coefs)
    return _ok(
        channel_id=channel_id,
        cache_key=str(cache_key),
        fir_taps=len(coefs),
        peak_amplitude=float(np.max(np.abs(arr))) if len(arr) else 0.0,
        is_sub=is_sub,
        message=(
            f"Designed {len(coefs)}-tap AVR FIR for {channel_id} "
            f"({'sub' if is_sub else 'speaker'} chain). "
            f"Cached as ({cache_key!r}, {channel_id!r})."
        ),
    )


async def _tool_apply_avr_fir(
    host: str,
    ady_path: str,
    cache_key: str,
    channel_ids: list[str] | None = None,
    distances_override_m: dict[str, float] | None = None,
    target_curves: list[str] | None = None,
    samplerates_hz: list[int] | None = None,
    inter_packet_delay_ms: float = 5.0,
) -> dict:
    """Push cached AVR-format FIR coefficients to the receiver.

    HARD RULE: this tool overwrites the AVR's MultEQ filter banks. The
    AVR's prior calibration is replaced for every channel listed in
    ``channel_ids``. Recovery from a botched push requires re-uploading
    the original .ady via the MultEQ Editor app or this tool with the
    original IRs.

    Caller MUST NOT enter Manual Setup > Distances on the AVR after a
    successful push — that triggers firmware re-validation that snaps
    Distance values back to the variance cap.

    The FULL Audyssey envelope is pushed (16 ordered fields) to keep the
    MultEQ EQ params coherent — the partial-envelope FR drift seen in
    earlier `set_speaker_distances(use_custom=True)` runs is avoided
    by this tool.

    Args:
        host: AVR IP / hostname.
        ady_path: path to the .ady file with the AVR's stored
            calibration state. Provides per-channel speaker-type,
            crossover, level, and existing distance values.
        cache_key: identifier used at ``design_avr_fir`` time.
        channel_ids: subset of channels to upload. Defaults to all
            channels in the .ady that have a cached FIR under
            ``cache_key``.
        distances_override_m: optional ``{channel: meters}`` map to
            override .ady distances during the upload (e.g. SW1=20.0
            for the variance-cap bypass).
        target_curves: which target-curve banks to write. Default
            writes both Flat ("00") and Reference ("01").
        samplerates_hz: which sample rates to ship. Default XT32's
            three (32k, 44.1k, 48k).
        inter_packet_delay_ms: pause between SET_COEFDT packets.

    Returns the per-stage ACK summary plus an ``ok`` flag.
    """
    import json as _json
    from pathlib import Path

    from .drivers.denon.audyssey_filter_upload import (
        channels_in_ady, push_avr_filters,
    )

    p = Path(ady_path)
    if not p.exists():
        return _err(f".ady not found: {ady_path}")
    try:
        with p.open() as f:
            ady = _json.load(f)
    except (OSError, _json.JSONDecodeError) as exc:
        return _err(f"failed to load .ady: {exc}")

    available = channels_in_ady(ady)
    selected = list(channel_ids) if channel_ids else list(available)
    missing_in_cache: list[str] = []
    channel_filters: dict[str, list[float]] = {}
    for cid in selected:
        if cid not in available:
            return _err(f"channel {cid!r} not in .ady (available: {available})")
        coefs = _AVR_FIR_CACHE.get((str(cache_key), cid))
        if coefs is None:
            missing_in_cache.append(cid)
        else:
            channel_filters[cid] = coefs
    if missing_in_cache:
        return _err(
            f"no cached FIR for cache_key={cache_key!r} on channels "
            f"{missing_in_cache} — call design_avr_fir for each first"
        )

    overrides = {ch: float(m) for ch, m in (distances_override_m or {}).items()}
    tc_arg = tuple(target_curves) if target_curves else None
    sr_arg = (
        tuple(int(r) for r in samplerates_hz) if samplerates_hz else None
    )

    push_kwargs = {
        "ady": ady,
        "channel_filters": channel_filters,
        "distances_override_m": overrides or None,
        "inter_packet_delay_ms": float(inter_packet_delay_ms),
    }
    if tc_arg:
        push_kwargs["target_curves"] = tc_arg
    if sr_arg:
        push_kwargs["samplerates_hz"] = sr_arg

    try:
        summary = await push_avr_filters(host, **push_kwargs)
    except (OSError, ValueError) as exc:
        return _err(f"AVR upload failed: {exc}")

    return _ok(**summary, channels_uploaded=list(channel_filters.keys()))


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

        # When the DSP driver is in cal mode, route the sweep into its loopback
        # capture device so the sweep enters CamillaDSP via snd-aloop. Bypasses
        # the AVR — Audyssey/MultEQ filters cannot color the cal stimulus.
        cal_active = bool(getattr(_dsp, "cal_mode_active", False))
        cal_playback = getattr(_dsp, "cal_playback_device", None) if cal_active else None

        # Graph composes the right stack: HDMI route → AVR neutralisation
        # (DenonSweepContext) + DSP HDMI-mode context (source=Analog +
        # master_gain_hdmi_db). USB route → DSP USB sweep context only.
        # Drivers that don't need sweep-time setup return None and are skipped.
        graph = cfg.signal_graph
        targets = tuple(graph.transducers_by_role("sub")) or graph.transducers

        if route == "hdmi" and _drivers is not None:
            async with graph.sweep_context_for_route(route, targets, cfg, _drivers):
                fr = await engine.measure(playback_device_override=cal_playback)
        elif route == "hdmi":
            # Legacy path used when the driver registry isn't populated (older
            # test setups that patch `_dsp` directly without the lifespan).
            # Behaves exactly as before the graph refactor.
            denon_ctx = DenonSweepContext.from_config(cfg)
            if denon_ctx:
                async with denon_ctx:
                    fr = await engine.measure(playback_device_override=cal_playback)
            else:
                fr = await engine.measure(playback_device_override=cal_playback)
        else:
            # USB mode keeps the persistent-session pattern so repeat
            # measurements don't thrash the DSP source switch. The persistent
            # session is equivalent to entering the DSP's sweep context once
            # and holding it open across measurements.
            await _ensure_sweep_session()
            fr = await engine.measure(playback_device_override=cal_playback)

        # Compute IR-derived metadata at capture time. Query the DSP for the
        # current per-output FIR pre-delay so the onset detector can skip
        # any FIR-injected pre-ring (multi-pulse modal-cancellation FIRs
        # produce strong pre-arrival content that would otherwise walk the
        # detected onset back into the FIR's own non-causal window).
        fir_pre_delay_ms = 0.0
        try:
            if _dsp is not None:
                getter = getattr(_dsp, "get_fir_pre_delay_ms", None)
                if callable(getter):
                    fir_pre_delay_ms = float(getter() or 0.0)
        except Exception:
            fir_pre_delay_ms = 0.0
        metadata = compute_session_metadata(fr, fir_pre_delay_ms=fir_pre_delay_ms)
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


async def _tool_fit_shelf_for_target(
    session_id: int,
    target_curve: dict,
    min_hz: float = 20.0,
    max_hz: float = 120.0,
    shelf_type: str = "low_shelf",
    freq_bounds: list[float] | None = None,
    gain_bounds: list[float] | None = None,
    q_bounds: list[float] | None = None,
) -> dict:
    """Fit a shelf filter (freq, gain_db, Q) to minimize RMS deviation of
    the predicted post-filter FR from a target curve in [min_hz, max_hz].

    Replaces the manual shelf-tuning iteration (applying +5, +6, +7 dB
    shelves by eye until Harman looks right): this runs scipy's
    differential-evolution search over the 3-param filter and returns the
    optimal parameters. Apply via ``apply_input_eq`` with the mandatory
    HPF prepended.

    **Default ranges** (sane for bass shelf shaping on a subwoofer):
      freq ∈ [25, 80] Hz
      gain_db ∈ [-6, +10]
      Q ∈ [0.3, 1.5]

    **Predicted RMS** is the objective value at the optimum — same
    definition the recipe uses to judge convergence.

    Overrides: pass ``freq_bounds`` / ``gain_bounds`` / ``q_bounds`` as
    [lo, hi] lists to narrow or widen the search.
    """
    try:
        import numpy as np
        from scipy.optimize import differential_evolution

        from .storage import SessionStore

        if shelf_type not in ("low_shelf", "high_shelf"):
            return _err(f"shelf_type must be low_shelf or high_shelf, got {shelf_type!r}")

        tgt_points = target_curve.get("points") if isinstance(target_curve, dict) else None
        if not tgt_points:
            return _err("target_curve must be {'points': [{freq, spl}, ...]}")

        store = SessionStore()
        sessions = store.list_sessions()
        sess = next((s for s in sessions if s.id == session_id), None)
        if sess is None:
            return _err(f"session {session_id} not found")
        if not sess.start_fr or not sess.start_fr.frequencies:
            return _err(f"session {session_id} has no FR data")

        freqs = np.array(sess.start_fr.frequencies)
        spls = np.array(sess.start_fr.spl)
        in_band = (freqs >= min_hz) & (freqs <= max_hz)
        band_freqs = freqs[in_band]
        band_spls = spls[in_band]

        # Target interp to our frequency grid (log-frequency, linear dB).
        tgt_f = np.array([float(p["freq"]) for p in tgt_points], dtype=np.float64)
        tgt_spl = np.array([float(p["spl"]) for p in tgt_points], dtype=np.float64)
        order = np.argsort(tgt_f)
        tgt_f = tgt_f[order]
        tgt_spl = tgt_spl[order]
        target_interp = np.interp(
            np.log(np.maximum(band_freqs, 1e-6)),
            np.log(tgt_f),
            tgt_spl,
        )

        # Default bounds
        fb = list(freq_bounds) if freq_bounds else [25.0, 80.0]
        gb = list(gain_bounds) if gain_bounds else [-6.0, 10.0]
        qb = list(q_bounds) if q_bounds else [0.3, 1.5]

        def shelf_response_db(params: "np.ndarray") -> "np.ndarray":
            fc, g, q = float(params[0]), float(params[1]), float(params[2])
            # Apply the shelf to each in-band frequency by ADDING its
            # predicted effect to the measured SPL (simulate_eq does the
            # same — linear time-invariant: filter effect adds in dB).
            effect = np.array(
                [_biquad_response(float(f), shelf_type, fc, g, q) for f in band_freqs]
            )
            return band_spls + effect

        # Level-shift the target to match the measurement's mean before
        # evaluating the shelf. Real-world target curves are specified by
        # SHAPE, not by absolute dBFS — the target's own anchor can be at
        # any level, and we want to find the shelf that best matches the
        # target shape regardless of where it sits on the SPL axis. The
        # level shift is applied per-iteration using the post-filter
        # measurement, not once against the raw baseline; that way a shelf
        # that boosts low-end (which lifts the measured mean) gets properly
        # credited for following an up-tilted target shape.
        target_mean = float(np.mean(target_interp))

        def objective(params: "np.ndarray") -> float:
            predicted = shelf_response_db(params)
            predicted_mean = float(np.mean(predicted))
            target_shifted = target_interp + (predicted_mean - target_mean)
            return float(np.sqrt(np.mean((predicted - target_shifted) ** 2)))

        # Baseline uses the same level-shift rule applied to the raw
        # (unfiltered) measurement, so baseline_rms and predicted_rms are
        # directly comparable.
        _raw_mean = float(np.mean(band_spls))
        _target_baseline = target_interp + (_raw_mean - target_mean)
        baseline_rms = float(np.sqrt(np.mean((band_spls - _target_baseline) ** 2)))

        result = differential_evolution(
            objective,
            bounds=[tuple(fb), tuple(gb), tuple(qb)],
            maxiter=200,
            popsize=20,
            tol=1e-4,
            seed=42,
            polish=True,
        )

        best_fc, best_g, best_q = float(result.x[0]), float(result.x[1]), float(result.x[2])
        best_rms = float(result.fun)

        return _ok(
            recommended_filter={
                "type": shelf_type,
                "freq": round(best_fc, 2),
                "gain_db": round(best_g, 2),
                "q": round(best_q, 3),
            },
            baseline_rms_db=round(baseline_rms, 3),
            predicted_rms_db=round(best_rms, 3),
            improvement_db=round(baseline_rms - best_rms, 3),
            band_hz=[min_hz, max_hz],
            converged=bool(result.success),
            n_evaluations=int(result.nfev),
            note=(
                "Apply via apply_input_eq with 18 Hz HPF prepended. The predicted_rms_db "
                "is what the RMS-vs-target metric should read after a post-apply "
                "measurement (± measurement noise)."
            ),
        )
    except Exception as exc:
        return _err(f"fit_shelf_for_target failed: {exc}")


async def _tool_start_calibration(
    recipe_name: str,
    target: str,
    reset_state: bool = True,
    preserve_eq: bool = False,
) -> dict:
    """One-call Phase 0: reset stale DSP state, snapshot hardware state, open
    a calibration run record. Returns the run_id to thread through subsequent
    save_calibration_iteration / update_calibration_run calls.

    **Why this exists:** every prior calibration run forgot one of three
    setup steps: (a) clear stale persisted state, (b) record baseline
    hardware state, (c) open a run record so measurements are linkable
    post-hoc. Today's session on 2026-04-24 had 30+ loose sessions with
    no run_id → they're invisible in get_calibration_runs. Today's
    session also ran with a stale polarity flip for hours.

    Skip reset_state only if you're resuming a run in progress.
    """
    try:
        from .storage import SessionStore
        # (1) Clean state — the #1 cause of wasted calibration time.
        reset_summary: dict | None = None
        if reset_state:
            reset_summary = await _tool_reset_dsp_defaults(preserve_eq=preserve_eq)
            if not reset_summary.get("ok"):
                return _err(
                    f"start_calibration: reset_dsp_defaults failed: "
                    f"{reset_summary.get('error')}"
                )
        # (2) Snapshot current hardware state for later review.
        device_state = await _tool_get_device_state()
        # (3) Open a run record.
        store = SessionStore()
        run_id = store.save_run(
            recipe_name,
            target,
            device_state=device_state if device_state.get("ok") else None,
            run_type="calibration",
        )
        return _ok(
            run_id=run_id,
            recipe_name=recipe_name,
            target=target,
            reset=reset_summary,
            device_state=device_state,
            note=(
                "Calibration run opened. Pass run_id to save_calibration_iteration "
                "after each measurement pass, and to update_calibration_run at "
                "the end with final_rms / converged / target_curve_data."
            ),
        )
    except Exception as exc:
        return _err(f"start_calibration failed: {exc}")


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
        active_input = cfg.active_input
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
        # Always include the resolved graph — recipes read this to discover
        # transducer/group names without a second round-trip.
        data["signal_graph"] = cfg.signal_graph.summary()
        return _ok(config=data)
    except Exception as exc:
        return _err(f"config error: {exc}")


async def _tool_get_signal_graph() -> dict:
    """Return a compact signal-graph summary for LLM reasoning."""
    try:
        cfg = _config()
        return _ok(graph=cfg.signal_graph.summary())
    except Exception as exc:
        return _err(f"get_signal_graph error: {exc}")


async def _tool_resolve_target(target: str) -> dict:
    """Resolve a group/transducer/role string to the concrete transducer list."""
    try:
        cfg = _config()
        graph = cfg.signal_graph
        transducers = graph.resolve_target(target)
        resolved = [
            {
                "transducer": t.name,
                "role": t.role,
                "processor": t.processor_ref,
                "output_index": t.output_index,
                "profile": t.safety_profile_ref,
                "position": t.position,
            }
            for t in transducers
        ]
        return _ok(target=target, resolved=resolved)
    except Exception as exc:
        return _err(f"resolve_target error: {exc}")


# ── Shared target dispatch ────────────────────────────────────────────────────
#
# Every tool that accepts a `target` param uses the same resolution: walk the
# signal graph, look up each transducer's driver in the registry, return
# dispatch records. Collected here so individual tools stay short and share
# identical error messages.


class _DispatchError(RuntimeError):
    """Raised when target resolution fails; carries the MCP error dict."""

    def __init__(self, err_dict: dict) -> None:
        super().__init__(err_dict.get("error", "dispatch error"))
        self.err = err_dict


def _resolve_for_dispatch(target: str) -> list[dict]:
    """Resolve target to a list of dispatch records, or raise _DispatchError.

    Each record: {transducer, processor, output_index, profile, driver}.
    The driver is pulled from the module-level ``_drivers`` registry using
    the transducer's ``processor_ref``; a missing driver is a hard fail.
    """
    cfg = _config()
    graph = cfg.signal_graph
    transducers = graph.resolve_target(target)
    if not transducers:
        raise _DispatchError(_err(f"unknown target {target!r}"))

    if _drivers is None:
        raise _DispatchError(_err("no drivers loaded"))

    records = []
    for t in transducers:
        driver = _drivers.get(t.processor_ref)
        if driver is None:
            raise _DispatchError(_err(
                f"target {t.name!r} references processor {t.processor_ref!r} "
                f"which is not in the driver registry"
            ))
        records.append({
            "transducer": t,
            "processor": t.processor_ref,
            "output_index": t.output_index,
            "profile": graph.profile_for(t),
            "driver": driver,
        })
    return records


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


async def _tool_mute_output(
    output_indices: list[int] | None = None,
    target: str | None = None,
) -> dict:
    """Mute DSP outputs.

    Pass ``target`` (group/transducer/role) to dispatch by name through the
    signal graph, or ``output_indices`` for raw legacy dispatch. Mutually
    exclusive; at least one is required.
    """
    if target is not None and output_indices:
        return _err("mute_output: pass either target or output_indices, not both")
    from .storage import dsp_output_key
    try:
        if target is not None:
            records = _resolve_for_dispatch(target)
            by_driver: dict[int, list[int]] = {}
            for rec in records:
                by_driver.setdefault(id(rec["driver"]), []).append(rec["output_index"])
            touched: list[dict] = []
            for rec in records:
                dkey = id(rec["driver"])
                if dkey in by_driver:
                    idxs = by_driver.pop(dkey)
                    await rec["driver"].mute_outputs(idxs)
                    for idx in idxs:
                        _persist_dsp_state(
                            dsp_output_key(rec["processor"], int(idx), "mute"),
                            {"muted": True},
                        )
                    touched.append({"processor": rec["processor"], "outputs": idxs})
            return _ok(target=target, muted=touched)
        if not output_indices:
            return _err("mute_output: target or output_indices required")
        await _dsp.mute_outputs(output_indices)  # type: ignore[union-attr]
        processor = _default_dsp_name() or "dsp"
        for idx in output_indices:
            _persist_dsp_state(
                dsp_output_key(processor, int(idx), "mute"), {"muted": True},
            )
        return _ok(muted=output_indices)
    except _DispatchError as exc:
        return exc.err
    except Exception as exc:
        return _err(f"mute failed: {exc}")


async def _tool_unmute_output(
    output_indices: list[int] | None = None,
    target: str | None = None,
) -> dict:
    """Unmute DSP outputs. See ``mute_output`` for ``target`` semantics."""
    if target is not None and output_indices:
        return _err("unmute_output: pass either target or output_indices, not both")
    from .storage import dsp_output_key
    try:
        if target is not None:
            records = _resolve_for_dispatch(target)
            by_driver: dict[int, list[int]] = {}
            for rec in records:
                by_driver.setdefault(id(rec["driver"]), []).append(rec["output_index"])
            touched: list[dict] = []
            for rec in records:
                dkey = id(rec["driver"])
                if dkey in by_driver:
                    idxs = by_driver.pop(dkey)
                    await rec["driver"].unmute_outputs(idxs)
                    for idx in idxs:
                        _persist_dsp_state(
                            dsp_output_key(rec["processor"], int(idx), "mute"),
                            {"muted": False},
                        )
                    touched.append({"processor": rec["processor"], "outputs": idxs})
            return _ok(target=target, unmuted=touched)
        if not output_indices:
            return _err("unmute_output: target or output_indices required")
        await _dsp.unmute_outputs(output_indices)  # type: ignore[union-attr]
        processor = _default_dsp_name() or "dsp"
        for idx in output_indices:
            _persist_dsp_state(
                dsp_output_key(processor, int(idx), "mute"), {"muted": False},
            )
        return _ok(unmuted=output_indices)
    except _DispatchError as exc:
        return exc.err
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


async def _tool_set_delay(
    delay_ms: float,
    output_index: int | None = None,
    target: str | None = None,
) -> dict:
    """Set delay for DSP output(s) in milliseconds.

    ``target`` (group/transducer/role) dispatches through the graph; same
    delay value is written to every resolved output. ``output_index`` is the
    raw single-output legacy path. Exactly one is required.
    """
    if target is not None and output_index is not None:
        return _err("set_delay: pass either target or output_index, not both")
    from .storage import dsp_output_key
    try:
        if target is not None:
            records = _resolve_for_dispatch(target)
            touched: list[dict] = []
            for rec in records:
                await rec["driver"].set_output_delay(rec["output_index"], delay_ms)
                _persist_dsp_state(
                    dsp_output_key(rec["processor"], rec["output_index"], "delay"),
                    {"delay_ms": delay_ms},
                )
                touched.append({
                    "transducer": rec["transducer"].name,
                    "processor": rec["processor"],
                    "output_index": rec["output_index"],
                })
            return _ok(target=target, delay_ms=delay_ms, applied=touched)
        if output_index is None:
            return _err("set_delay: target or output_index required")
        await _dsp.set_output_delay(output_index, delay_ms)  # type: ignore[union-attr]
        _persist_dsp_state(
            dsp_output_key(_default_dsp_name() or "dsp", output_index, "delay"),
            {"delay_ms": delay_ms},
        )
        return _ok(output_index=output_index, delay_ms=delay_ms)
    except _DispatchError as exc:
        return exc.err
    except DriverError as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err(f"set_delay error: {exc}")


async def _tool_set_polarity(
    inverted: bool,
    output_index: int | None = None,
    target: str | None = None,
) -> dict:
    """Set polarity (inverted=True flips phase). See ``set_delay`` for targeting."""
    if target is not None and output_index is not None:
        return _err("set_polarity: pass either target or output_index, not both")
    from .storage import dsp_output_key
    try:
        if target is not None:
            records = _resolve_for_dispatch(target)
            touched: list[dict] = []
            for rec in records:
                await rec["driver"].set_output_polarity(rec["output_index"], inverted)
                _persist_dsp_state(
                    dsp_output_key(rec["processor"], rec["output_index"], "polarity"),
                    {"inverted": inverted},
                )
                touched.append({
                    "transducer": rec["transducer"].name,
                    "processor": rec["processor"],
                    "output_index": rec["output_index"],
                })
            return _ok(target=target, inverted=inverted, applied=touched)
        if output_index is None:
            return _err("set_polarity: target or output_index required")
        await _dsp.set_output_polarity(output_index, inverted)  # type: ignore[union-attr]
        _persist_dsp_state(
            dsp_output_key(_default_dsp_name() or "dsp", output_index, "polarity"),
            {"inverted": inverted},
        )
        return _ok(output_index=output_index, inverted=inverted)
    except _DispatchError as exc:
        return exc.err
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
    """Extract IR onset time, polarity sign, and SPL from a stored session.

    Valid use: solo-sub time alignment — measure each sub solo with identical
    DSP processing on each, then subtract the earliest peak_time_s from the
    latest to get the delay offset. Both measurements share the same FIR and
    buffer latency, so those terms cancel.

    INVALID use: cross-path comparisons (sub-vs-mains, FIR-chain vs no-FIR-chain,
    cal-mode vs HDMI). The detected peak sits inside the sub chain's FIR
    non-causal window (~42 ms for a 4096-tap linear-phase filter @ 48 kHz);
    its absolute value reflects FIR shape + buffer latency, not acoustic
    arrival. Use ``compare_sub_phase`` (phase-slope fit) or the loopback
    alignment rig for sub-vs-mains timing instead.
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

        # Solo subs at room-realistic distances peak in the 0–30 ms range
        # (mic ≤10 m). A peak past ~80 ms means the chain has a long FIR or
        # buffer in the path — analyze_ir's absolute number is then a property
        # of that processing, not acoustic arrival, and is invalid for
        # cross-path comparison (sub vs mains, FIR-chain vs no-FIR).
        cross_path_warning: str | None = None
        if onset["peak_time_ms"] > 80.0:
            cross_path_warning = (
                f"peak_time_ms={onset['peak_time_ms']:.1f} exceeds typical solo-sub "
                "acoustic range — likely reflects FIR/buffer latency on the chain. "
                "Do NOT use this value to compare against measurements with different "
                "processing (e.g. mains via HDMI). Use compare_sub_phase for cross-path timing."
            )

        return _ok(
            session_id=session.id,
            peak_time_s=round(onset["peak_time_ms"] / 1000.0, 6),
            cross_path_warning=cross_path_warning,
            **onset,
        )
    except Exception as exc:
        return _err(f"analyze_ir failed: {exc}")


async def _tool_apply_fir(
    output_index: int,
    coefficients: list[float] | None = None,
    design_session_id: int | None = None,
) -> dict:
    """Write FIR coefficients to a single DSP output.

    Source (exactly one required):
      - ``coefficients``: inline float array (legacy path)
      - ``design_session_id``: the session_id passed to a prior ``design_fir``
        call. Coefficients are retrieved from the server-side cache; avoids
        shipping large arrays through the tool call.
    """
    if coefficients is not None and design_session_id is not None:
        return _err("apply_fir: pass either coefficients or design_session_id, not both")
    if coefficients is None and design_session_id is None:
        return _err("apply_fir: provide either coefficients or design_session_id")

    source: str
    intent = "general"
    if design_session_id is not None:
        cached = _fir_design_cache.get(int(design_session_id))
        if not cached:
            return _err(
                f"apply_fir: no cached design for session_id={design_session_id}. "
                f"Run design_fir first; cache is cleared on server restart."
            )
        coefficients = cached
        source = f"design_session_id={design_session_id}"
        intent = _fir_design_intent.get(int(design_session_id), "general")
    else:
        source = "inline"

    # Safety: validate FIR magnitude against the per-output transducer's
    # profile before calling the driver. Driver also re-validates against
    # the default profile — this layer provides the transducer-specific
    # check (e.g. tighter limits for a shaker vs. a sub).
    try:
        from .safety import SafetyValidationError, SafetyValidator
        cfg = _config()
        graph = cfg.signal_graph
        profile = None
        fir_rate: int | None = None
        for t in graph.transducers:
            if t.output_index == int(output_index):
                profile = graph.profile_for(t)
                break
        try:
            caps = _dsp.capabilities  # type: ignore[union-attr]
            fir_rate = int(caps.fir_sample_rate_hz)
        except Exception:
            fir_rate = 96_000
        SafetyValidator(profile).validate_fir(
            list(coefficients), sample_rate=fir_rate, intent=intent,
        )
    except SafetyValidationError as exc:
        return _err(str(exc))
    except Exception:
        # Graph/profile lookup failure shouldn't prevent the driver's own
        # default-profile safety check from running — fall through.
        pass

    try:
        await _dsp.apply_fir(output_index, coefficients)  # type: ignore[union-attr]
    except DriverError as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err(f"apply_fir error: {exc}")

    # Persist so a CamillaDSP rebuild (triggered by any later apply_*/set_* call)
    # or MCP-server restart rehydrates the FIR back into the shadow state.
    from .storage import dsp_output_key
    processor = _default_dsp_name() or "dsp"
    _persist_dsp_state(
        dsp_output_key(processor, int(output_index), "fir"),
        {"coefficients": list(coefficients), "num_taps": len(coefficients)},
    )
    return _ok(output_index=output_index, taps=len(coefficients), source=source)


async def _tool_design_corrective_fir(
    session_id: int,
    target_curve: dict,
    output_index: int,
    num_taps: int = 1024,
    focus_hz: list[float] | None = None,
    return_coefficients: bool = False,
) -> dict:
    """Design a magnitude-correction FIR for the *residual* between a measured
    listener FR and the target curve, then convolve with the existing FIR
    cached on ``output_index``. Returns a new design_session_id whose
    coefficients can be applied via ``apply_fir(design_session_id=...)``.

    This is the **empirical 2-step** workflow:
      1. Apply some baseline correction (e.g. a modal-cancellation FIR via
         ``design_modal_fir``).
      2. Measure the listener result (the ``session_id`` argument here).
      3. This tool computes the residual ``target − measured`` and designs
         a min-phase FIR that closes that gap, convolved on top of the
         existing FIR for ``output_index``.
      4. Apply via ``apply_fir(output_index, design_session_id=<returned>)``.

    Use after ``design_modal_fir`` / ``apply_fir`` revealed a per-room FR
    deviation from the target curve that wasn't predictable from the FIR
    design alone (anti-pulse phase interaction with the room's modal
    response — see recipe Section 2.2b).

    Args:
        session_id: post-baseline-FIR measurement (the room's response
            after the existing FIR is applied to ``output_index``).
        target_curve: ``{"points": [{"freq", "spl"}, ...], "band": [lo, hi]}``.
            Same shape as ``design_fir``'s target_curve. Anchored to the
            60-100 Hz midband so absolute SPL drops out.
        output_index: DSP output whose existing cached FIR to convolve onto.
            Must have a cached design (i.e. an earlier ``design_*_fir`` call
            populated ``_fir_design_cache[session_id]`` referenced by an
            apply_fir(design_session_id=...)). When no cached FIR is found,
            falls back to a passthrough impulse (so this tool still works
            for the first-pass case where there's no baseline FIR yet).
        num_taps: corrective FIR length (default 1024 — short, since this
            is just magnitude correction at low frequencies).
        focus_hz: ``[lo, hi]`` band where correction is applied; outside
            tapers to 0 dB. Default: target_curve's ``band`` if present,
            else [25, 120].
        return_coefficients: include the convolved FIR taps in the response.
    """
    from .storage import SessionStore
    import numpy as _np

    try:
        store = SessionStore()
        sessions = store.list_sessions()
        session = next((s for s in sessions if s.id == session_id), None)
        if session is None:
            return _err(f"session {session_id} not found")
        if not session.start_fr or not session.start_fr.frequencies:
            return _err(f"session {session_id} has no FR data")

        # Target curve plumbing
        if not isinstance(target_curve, dict) or not target_curve.get("points"):
            return _err("target_curve.points required")
        target_points = [
            (float(p.get("freq", p.get("freq_hz", 0))),
             float(p.get("spl", p.get("spl_db", 0))))
            for p in target_curve["points"]
        ]
        band = target_curve.get("band") or focus_hz
        focus = (float(band[0]), float(band[1])) if isinstance(band, (list, tuple)) and len(band) == 2 else (25.0, 120.0)

        # Source FR from the session (raw arrays).
        fr = session.start_fr
        source_pairs = list(zip(
            [float(f) for f in fr.frequencies],
            [float(s) for s in fr.spl],
        ))

        # Existing FIR for output_index — find any cached design that was
        # applied to this output. Fall back to passthrough if none.
        existing_fir = None
        for sid, coeffs in _fir_design_cache.items():
            # Best-effort: assume the most recently applied design is the
            # right baseline. Without an apply→cache reverse index we
            # approximate by using the largest session_id that has cached
            # coefficients (chronological proxy).
            existing_fir = list(coeffs)
        if existing_fir is None:
            existing_fir = [0.0] * num_taps
            existing_fir[0] = 1.0

        # Design the corrective magnitude FIR.
        from .modal_fir import _design_magnitude_correction_fir
        sample_rate = 8000  # FIR processing rate
        existing_arr = _np.asarray(existing_fir, dtype=_np.float32)
        corrective = _design_magnitude_correction_fir(
            fir=existing_arr,
            target_db=target_points,
            source_fr_db=source_pairs,
            sample_rate=sample_rate,
            n_taps=int(num_taps),
            focus_hz=focus,
        )
        # Convolve corrective with existing — combined FIR delivers both.
        combined = _np.convolve(existing_arr, corrective)
        # Cap at a reasonable length so apply_fir doesn't reject. Use the
        # longer of the two inputs.
        max_len = max(len(existing_arr), int(num_taps))
        combined = combined[:max_len].astype(_np.float32)
        peak = float(_np.max(_np.abs(combined)))
        if peak > 1.0:
            combined = combined / (peak * 1.001)

        # Cache under a synthetic session_id derived from the source.
        cache_id = int(session_id)
        _fir_design_cache[cache_id] = combined.tolist()
        _fir_design_intent[cache_id] = "modal_cancel"

        result = {
            "session_id": session_id,
            "output_index": int(output_index),
            "num_taps": int(len(combined)),
            "sample_rate": sample_rate,
            "peak_amplitude": round(float(_np.max(_np.abs(combined))), 4),
            "design_cached": True,
            "note": (
                "Empirical 2-step corrective FIR convolved on top of the "
                "existing cached FIR. Apply via "
                f"apply_fir(output_index={output_index}, "
                f"design_session_id={session_id})."
            ),
        }
        if return_coefficients:
            result["coefficients"] = combined.tolist()
        return _ok(**result)
    except Exception as exc:
        return _err(f"design_corrective_fir failed: {exc}")


async def _tool_clear_fir(output_index: int) -> dict:
    """Clear FIR coefficients and reset output to passthrough."""
    try:
        await _dsp.clear_fir(output_index)  # type: ignore[union-attr]
    except DriverError as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err(f"clear_fir error: {exc}")

    # Mirror the clear to persisted state so a rehydrate after restart doesn't
    # resurrect a stale FIR that was just cleared.
    from .storage import dsp_output_key
    processor = _default_dsp_name() or "dsp"
    _persist_dsp_state(
        dsp_output_key(processor, int(output_index), "fir"),
        {"coefficients": [], "num_taps": 0},
    )
    return _ok(output_index=output_index, message="FIR cleared")


async def _tool_reset_dsp_defaults(
    dry_run: bool = False,
    preserve_eq: bool = False,
) -> dict:
    """Clear ALL persisted per-output DSP state to defaults.

    Resets every configured transducer to: polarity=normal, gain=0 dB,
    delay=0 ms, FIR cleared, per-output EQ cleared (or HPF-only if the
    transducer profile requires an infrasonic HPF). Input EQ goes to
    HPF-only.

    Returns a record of every value that was cleared, so the caller sees
    exactly what stale state was overridden.

    **Why this exists:** `active_dsp_state` persists across container
    restarts. A polarity flip or gain trim from a prior calibration
    silently re-applies on every boot. That corrupted multiple hours of
    calibration work on 2026-04-24. `check_system` now WARNS; this tool
    ACTS. Call this as Phase 0 of every calibration run unless you
    explicitly want to preserve prior state.

    `dry_run=True`: return the list of changes that WOULD be made
    without touching hardware.
    `preserve_eq=True`: keep EQ filters, only reset polarity/gain/delay/FIR.
    """
    from .storage import SessionStore, dsp_output_key
    try:
        cfg = _config()
        graph = cfg.signal_graph
        transducers = getattr(graph, "transducers", ()) or ()
        if not transducers:
            return _err("no signal_graph transducers to reset")

        # What each transducer's safety profile requires post-reset.
        # Profiles with hpf_freq_hz > 0 must retain that HPF after EQ reset.
        profiles_by_name: dict[str, Any] = {
            p.name: p for p in (getattr(graph, "profiles", ()) or ())
        }

        changes: list[dict] = []

        for t in transducers:
            proc = getattr(t, "processor", None) or _default_dsp_name() or "dsp"
            out_idx = int(getattr(t, "output_index"))
            tname = getattr(t, "name", f"output_{out_idx}")

            # Determine required HPF for this transducer's profile.
            profile_name = getattr(t, "profile", None)
            profile = profiles_by_name.get(profile_name) if profile_name else None
            hpf_freq = float(getattr(profile, "hpf_freq_hz", 0.0) or 0.0) if profile else 0.0
            default_eq = (
                [{"type": "hpf", "freq": hpf_freq, "gain_db": 0, "q": 0.707}]
                if hpf_freq > 0 else []
            )

            t_changes: dict = {"transducer": tname, "output_index": out_idx, "processor": proc}

            # polarity → false
            if not dry_run:
                try:
                    await _dsp.set_output_polarity(out_idx, False)  # type: ignore[union-attr]
                    _persist_dsp_state(
                        dsp_output_key(proc, out_idx, "polarity"),
                        {"inverted": False},
                    )
                except Exception as exc:
                    log.warning("reset_dsp_defaults polarity %s: %s", tname, exc)
            t_changes["polarity_reset"] = True

            # gain → 0 dB
            if not dry_run:
                try:
                    await _dsp.set_output_gain(out_idx, 0.0)  # type: ignore[union-attr]
                    _persist_dsp_state(
                        dsp_output_key(proc, out_idx, "gain"),
                        {"gain_db": 0.0},
                    )
                except Exception as exc:
                    log.warning("reset_dsp_defaults gain %s: %s", tname, exc)
            t_changes["gain_reset"] = True

            # delay → 0 ms
            if not dry_run:
                try:
                    await _dsp.set_output_delay(out_idx, 0.0)  # type: ignore[union-attr]
                    _persist_dsp_state(
                        dsp_output_key(proc, out_idx, "delay"),
                        {"delay_ms": 0.0},
                    )
                except Exception as exc:
                    log.warning("reset_dsp_defaults delay %s: %s", tname, exc)
            t_changes["delay_reset"] = True

            # FIR → cleared
            if not dry_run:
                try:
                    await _dsp.clear_fir(out_idx)  # type: ignore[union-attr]
                    _persist_dsp_state(
                        dsp_output_key(proc, out_idx, "fir"),
                        {"coefficients": [], "num_taps": 0},
                    )
                except Exception as exc:
                    log.warning("reset_dsp_defaults fir %s: %s", tname, exc)
            t_changes["fir_cleared"] = True

            # Per-output EQ → HPF-only (or empty if no HPF required)
            if not preserve_eq:
                if not dry_run:
                    try:
                        await _dsp.apply_eq(out_idx, default_eq)  # type: ignore[union-attr]
                        _persist_dsp_state(
                            dsp_output_key(proc, out_idx, "eq"),
                            {"filters": default_eq, "preset": 0, "transducer": tname},
                        )
                    except Exception as exc:
                        log.warning("reset_dsp_defaults eq %s: %s", tname, exc)
                t_changes["eq_reset"] = True
                t_changes["eq_default"] = default_eq

            changes.append(t_changes)

        # Input EQ → HPF-only (preserves the mandatory infrasonic filter,
        # clears any target-curve shaping).
        input_changes: dict = {"input_eq_reset": False}
        if not preserve_eq:
            try:
                input_hpf = [{"type": "hpf", "freq": 18.0, "gain_db": 0, "q": 0.707}]
                if not dry_run:
                    await _dsp.apply_input_eq(input_hpf)  # type: ignore[union-attr]
                    _persist_dsp_state("processor:" + (_default_dsp_name() or "dsp") + ":input:eq",
                                       {"filters": input_hpf, "preset": 0})
                input_changes["input_eq_reset"] = True
                input_changes["input_eq_default"] = input_hpf
            except Exception as exc:
                log.warning("reset_dsp_defaults input_eq: %s", exc)

        return _ok(
            dry_run=dry_run,
            transducers_reset=len(changes),
            changes=changes,
            input=input_changes,
            note=(
                "Per-output polarity/gain/delay/fir set to defaults; per-output EQ set to "
                "profile-mandated HPF; input EQ set to infrasonic HPF only. All persisted "
                "state in active_dsp_state mirrored. Run check_system to verify clean state."
            ),
        )
    except Exception as exc:
        return _err(f"reset_dsp_defaults error: {exc}")


async def _tool_set_output_gain(
    gain_db: float,
    output_index: int | None = None,
    target: str | None = None,
) -> dict:
    """Set gain for DSP output(s) in dB. See ``set_delay`` for targeting."""
    if target is not None and output_index is not None:
        return _err("set_output_gain: pass either target or output_index, not both")
    from .storage import dsp_output_key
    try:
        if target is not None:
            records = _resolve_for_dispatch(target)
            touched: list[dict] = []
            for rec in records:
                await rec["driver"].set_output_gain(rec["output_index"], gain_db)
                _persist_dsp_state(
                    dsp_output_key(rec["processor"], rec["output_index"], "gain"),
                    {"gain_db": gain_db},
                )
                touched.append({
                    "transducer": rec["transducer"].name,
                    "processor": rec["processor"],
                    "output_index": rec["output_index"],
                })
            return _ok(target=target, gain_db=gain_db, applied=touched)
        if output_index is None:
            return _err("set_output_gain: target or output_index required")
        await _dsp.set_output_gain(output_index, gain_db)  # type: ignore[union-attr]
        _persist_dsp_state(
            dsp_output_key(_default_dsp_name() or "dsp", output_index, "gain"),
            {"gain_db": gain_db},
        )
        return _ok(output_index=output_index, gain_db=gain_db)
    except _DispatchError as exc:
        return exc.err
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


async def _tool_set_cal_mode(enabled: bool) -> dict:
    """Switch CamillaDSP capture between live and calibration sources.

    enabled=True: capture from snd-aloop loopback (hw:Loopback,1,0). The sweep
                  player writes to hw:Loopback,0,0 → CamillaDSP processes (FIR,
                  PEQ, gain, delay) → Focusrite outputs → subs. AVR is bypassed
                  so Audyssey/MultEQ filters cannot color the cal stimulus.
    enabled=False: restore live capture (e.g. Focusrite analog input fed by AVR
                   LFE pre-out) for normal listening.

    No-op if already in the requested mode. Always pair an enable with a
    disable when the calibration finishes — leaving cal mode active will
    silence the system for movies/music.
    """
    try:
        if not hasattr(_dsp, "set_cal_mode"):
            return _err("set_cal_mode: active DSP driver does not support cal-mode capture")
        await _dsp.set_cal_mode(bool(enabled))  # type: ignore[union-attr]
        active = getattr(_dsp, "cal_mode_active", None)
        playback = getattr(_dsp, "cal_playback_device", None)
        return _ok(cal_mode_active=bool(active), cal_playback_device=playback)
    except DriverError as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err(f"set_cal_mode error: {exc}")


async def _tool_configure_matrix(active_input: int | None = None) -> dict:
    """Route the active DSP input to enabled outputs, skipping defective/unused ones."""
    try:
        cfg = _config()
        input_idx = active_input if active_input is not None else cfg.active_input
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


async def _tool_set_routing(routing: dict) -> dict:
    """Apply an arbitrary input→output routing matrix to the active DSP.

    Generic variant of ``configure_matrix`` — not limited to 2 inputs / 4
    outputs. ``routing`` is a partial matrix; rows not mentioned stay at
    their current driver state. Values are booleans: True means the input
    channel is routed (unmuted) to the output channel, False means muted.

    JSON keys arrive as strings — converted to int here before the driver
    call. Example payload::

        {"routing": {"2": {"1": true, "2": true, "3": true}}}

    …routes input channel 2 (0-indexed) to output channels 1, 2, 3 on the
    default DSP, leaving every other cell unchanged.
    """
    try:
        parsed: dict[int, dict[int, bool]] = {}
        for inp, out_map in routing.items():
            inp_i = int(inp)
            if not isinstance(out_map, dict):
                return _err(
                    f"set_routing: value for input {inp!r} must be an object "
                    f"of output→bool, got {type(out_map).__name__}"
                )
            parsed[inp_i] = {int(k): bool(v) for k, v in out_map.items()}
        await _dsp.set_routing(parsed)  # type: ignore[union-attr]
        return _ok(routing=parsed)
    except DriverError as exc:
        return _err(str(exc))
    except (ValueError, TypeError) as exc:
        return _err(f"set_routing: invalid routing shape: {exc}")
    except Exception as exc:
        return _err(f"set_routing error: {exc}")


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


async def _tool_recommend_fir_phase(
    session_id: int,
    t60_threshold_ms: float = 500.0,
    peak_db_threshold: float = 0.0,
    freq_min: float = 20.0,
    freq_max: float = 200.0,
    mains_distance_budget_ms: float = 53.0,
    preringing_ms: float = 25.0,
) -> dict:
    """Recommend FIR phase mode + tap count based on post-FIR decay.

    Codifies the recipe's Phase 2.5a decision point so the LLM cannot skip it:
    a min-phase FIR only flattens magnitude, it does not actively cancel
    decay. If a solo post-FIR measurement still shows a prominent mode
    (peak_db above target-band average AND T60 above threshold), a
    mixed-phase FIR can cancel some of that decay — but only if the FIR
    impulse is at least as long as the mode's T60.

    Returns:
      - recommendation: "minimum" (current FIR is fine) or "mixed"
        (re-design with phase_mode="mixed").
      - offending_modes: modes that triggered the "mixed" recommendation
        (peak_db ≥ threshold AND T60 ≥ threshold).
      - suggested_num_taps: when recommending mixed, the smallest tap count
        whose impulse length covers the longest offending T60, clamped to
        the driver's fir_max_taps_per_output and rounded up to a power of 2.

    Call this AFTER applying the initial min-phase FIR and taking a
    post-FIR solo measurement.
    """
    from .storage import SessionStore
    from .decay import analyze_decay as _analyze_decay

    try:
        store = SessionStore()
        sessions = store.list_sessions()
        session = next((s for s in sessions if s.id == session_id), None)
        if session is None:
            return _err(f"session {session_id} not found")

        ir = session.impulse_response
        if not ir:
            return _err(
                f"session {session_id} has no impulse response — re-run measure"
            )
        sample_rate = session.start_fr.sample_rate if session.start_fr else 48000

        modes = _analyze_decay(
            ir,
            sample_rate=sample_rate,
            t60_threshold_ms=min(t60_threshold_ms, 300.0),
            freq_min=freq_min,
            freq_max=freq_max,
        )

        offenders = [
            {
                "freq_hz": m.freq_hz,
                "t60_ms": m.t60_ms,
                "peak_db": m.peak_db,
                "priority": m.priority,
            }
            for m in modes
            if m.peak_db >= peak_db_threshold and m.t60_ms >= t60_threshold_ms
        ]

        if not offenders:
            worst = modes[0] if modes else None
            detail = (
                f"worst mode: {worst.freq_hz:.1f} Hz T60={worst.t60_ms:.0f} ms "
                f"peak={worst.peak_db:.1f} dB"
                if worst else
                "no ringing modes found"
            )
            return _ok(
                session_id=session_id,
                recommendation="minimum",
                offending_modes=[],
                t60_threshold_ms=t60_threshold_ms,
                peak_db_threshold=peak_db_threshold,
                note=(
                    f"No modes exceed the {t60_threshold_ms:.0f} ms T60 + "
                    f"{peak_db_threshold:.1f} dB peak threshold — min-phase "
                    f"FIR is adequate. ({detail})"
                ),
            )

        # Mixed-phase recommended. Size the FIR so its impulse duration
        # covers the longest offending T60 with some headroom (2×).
        worst_t60_ms = max(m["t60_ms"] for m in offenders)
        fir_fs = 48_000
        fir_max = 65_536
        if _dsp is not None:
            caps = _dsp.capabilities
            if caps.fir_capable:
                fir_fs = caps.fir_sample_rate_hz
                fir_max = caps.fir_max_taps_per_output
        min_taps = int((worst_t60_ms / 1000.0) * 2.0 * fir_fs)
        # Round up to next power of two for FFT-friendly sizes.
        suggested = 1
        while suggested < max(8192, min_taps):
            suggested <<= 1
        suggested = min(suggested, fir_max)

        # Latency budgeting. With proper mixed-phase, the FIR's added audio
        # latency is ≈ preringing_ms (the windowed excess-phase extent) — NOT
        # the tap count. Because CamillaDSP only processes the LFE/sub chain
        # in this signal path, the latency is sub-only — the AVR must delay
        # its MAINS/centre/surrounds by the same amount via per-channel
        # speaker-distance compensation, NOT via the global "Audio Delay /
        # lip-sync" slider (which delays *all* audio uniformly relative to
        # video and does nothing for sub-vs-mains alignment).
        # Denon X-series UI caps speaker distance at 60 ft ≈ 53 ms. Beyond
        # that, write the value via MultEQ-X / ratbuddyssey through the OCA
        # protocol on port 1256 — the firmware accepts larger values than
        # the UI exposes.
        suggested_preringing_ms = preringing_ms
        fits_in_budget = suggested_preringing_ms <= mains_distance_budget_ms
        if not fits_in_budget:
            suggested_preringing_ms = mains_distance_budget_ms
        # Estimated latency = preringing window half-width (peak of mixed
        # impulse lands at that offset) + a sample or two for the min-phase
        # core's tiny group delay.
        estimated_latency_ms = round(suggested_preringing_ms, 2)

        offender_summary = ", ".join(
            f"{m['freq_hz']:.1f} Hz T60={m['t60_ms']:.0f} ms" for m in offenders[:3]
        )
        budget_note = (
            f"Sub-chain latency fits in the mains-distance budget "
            f"({mains_distance_budget_ms:.0f} ms ≈ "
            f"{mains_distance_budget_ms * 1.13:.0f} ft). Compensate by "
            f"setting the AVR mains/centre/surrounds distance LARGER than "
            f"physical so they wait for the FIR-delayed sub."
        ) if fits_in_budget else (
            f"WARNING: requested pre-ringing {preringing_ms:.0f} ms exceeds "
            f"the {mains_distance_budget_ms:.0f} ms mains-distance UI cap "
            f"(~{mains_distance_budget_ms * 1.13:.0f} ft on Denon X-series). "
            f"Either clamp pre-ringing to {suggested_preringing_ms:.0f} ms "
            f"(losing some decay cancellation), or push the per-channel "
            f"distance past the UI limit via MultEQ-X / ratbuddyssey on "
            f"port 1256 — the firmware accepts the value, only the UI "
            f"clamps it."
        )
        return _ok(
            session_id=session_id,
            recommendation="mixed",
            offending_modes=offenders,
            suggested_num_taps=suggested,
            suggested_preringing_ms=suggested_preringing_ms,
            estimated_latency_ms=estimated_latency_ms,
            mains_distance_budget_ms=mains_distance_budget_ms,
            fits_in_budget=fits_in_budget,
            t60_threshold_ms=t60_threshold_ms,
            peak_db_threshold=peak_db_threshold,
            note=(
                f"Mixed-phase FIR recommended: {len(offenders)} mode(s) exceed "
                f"the {t60_threshold_ms:.0f} ms T60 threshold ({offender_summary}). "
                f"Re-run design_fir with phase_mode='mixed', "
                f"num_taps={suggested} (impulse length "
                f"{suggested / fir_fs * 1000:.0f} ms ≥ 2× worst T60 "
                f"{worst_t60_ms:.0f} ms), preringing_ms={suggested_preringing_ms:.0f}. "
                f"Filter will add ~{estimated_latency_ms:.0f} ms of audio latency. "
                f"{budget_note}"
            ),
        )
    except Exception as exc:
        return _err(f"recommend_fir_phase failed: {exc}")


async def _tool_verify_fir_effect(
    pre_session_id: int,
    post_session_id: int,
    predicted_effect: list[dict],
    tolerance_db: float = 2.0,
    min_hz: float = 20.0,
    max_hz: float = 120.0,
) -> dict:
    """Compare a designed FIR's predicted effect against the measured delta.

    After applying a FIR, the predicted magnitude change (from design_fir's
    ``predicted_effect`` field) and the measured delta (post - pre solo
    measurement at 1/3-octave) should match within ~2 dB. Bigger divergences
    usually mean one of:

      - The FIR didn't land: pipeline rebuild wiped it, shadow state didn't
        include it, or the apply call was silently rejected.
      - Room interaction the design didn't model (room reflections hitting
        the mic that weren't in the pre-FIR measurement).
      - Measurement variance — one of the two measurements was noisy. Run
        again if coherence was low.

    Arguments:
      pre_session_id, post_session_id — solo measurements taken before and
        after apply_fir on the same sub.
      predicted_effect — the list[{freq_hz, fir_effect_db}] returned by
        design_fir. Forward it verbatim.
      tolerance_db — max allowed |predicted - measured| in any band before
        flagging. Default 2 dB.

    Returns per-band comparison, a list of bands that exceed ``tolerance_db``
    (``off_spec_bands``), and overall RMS discrepancy. ``within_tolerance``
    is True only when every band in the focus range passes.
    """
    from .storage import SessionStore
    import math

    try:
        store = SessionStore()
        sessions = store.list_sessions()
        pre = next((s for s in sessions if s.id == pre_session_id), None)
        post = next((s for s in sessions if s.id == post_session_id), None)
        if pre is None:
            return _err(f"pre_session_id {pre_session_id} not found")
        if post is None:
            return _err(f"post_session_id {post_session_id} not found")
        if not pre.start_fr or not pre.start_fr.frequencies:
            return _err(f"pre session {pre_session_id} has no FR data")
        if not post.start_fr or not post.start_fr.frequencies:
            return _err(f"post session {post_session_id} has no FR data")
        if not predicted_effect:
            return _err(
                "predicted_effect is required — forward the value returned by "
                "design_fir verbatim"
            )

        # Downsample both to 1/3-octave for robust per-band comparison.
        pre_bands = _downsample_to_third_octave(pre.start_fr.frequencies, pre.start_fr.spl)
        post_bands = _downsample_to_third_octave(post.start_fr.frequencies, post.start_fr.spl)
        pre_by_freq = {b["freq_hz"]: b["spl_db"] for b in pre_bands}
        post_by_freq = {b["freq_hz"]: b["spl_db"] for b in post_bands}

        # Index predicted effect by freq so we can match to the band centres.
        pred_by_freq = {
            float(p["freq_hz"]): float(p["fir_effect_db"])
            for p in predicted_effect
        }

        bands: list[dict] = []
        off_spec: list[dict] = []
        discrepancies: list[float] = []
        for freq in sorted(pred_by_freq):
            if freq < min_hz or freq > max_hz:
                continue
            if freq not in pre_by_freq or freq not in post_by_freq:
                continue
            measured_delta = post_by_freq[freq] - pre_by_freq[freq]
            predicted_delta = pred_by_freq[freq]
            discrepancy = measured_delta - predicted_delta
            entry = {
                "freq_hz": round(freq, 1),
                "predicted_db": round(predicted_delta, 1),
                "measured_db": round(measured_delta, 1),
                "discrepancy_db": round(discrepancy, 1),
                "within_tolerance": abs(discrepancy) <= tolerance_db,
            }
            bands.append(entry)
            discrepancies.append(discrepancy)
            if not entry["within_tolerance"]:
                off_spec.append(entry)

        if not bands:
            return _err(
                "no bands matched between predicted_effect, pre session, and "
                "post session in the requested frequency range"
            )

        rms = math.sqrt(sum(d * d for d in discrepancies) / len(discrepancies))
        within = len(off_spec) == 0

        if within:
            note = (
                f"FIR landed as designed across {len(bands)} bands "
                f"(RMS discrepancy {rms:.2f} dB, within ±{tolerance_db:.1f} dB "
                f"tolerance)."
            )
        else:
            worst = max(off_spec, key=lambda b: abs(b["discrepancy_db"]))
            note = (
                f"FIR effect diverges from prediction in {len(off_spec)} of "
                f"{len(bands)} bands (RMS discrepancy {rms:.2f} dB). Worst: "
                f"{worst['freq_hz']} Hz predicted {worst['predicted_db']:+.1f} dB, "
                f"measured {worst['measured_db']:+.1f} dB "
                f"(Δ {worst['discrepancy_db']:+.1f} dB). Check: did the FIR "
                f"actually apply (get_output_state.fir_taps)? Was coherence "
                f"high enough on both measurements? Did the room state change "
                f"between measurements?"
            )

        return _ok(
            pre_session_id=pre_session_id,
            post_session_id=post_session_id,
            bands=bands,
            off_spec_bands=off_spec,
            within_tolerance=within,
            tolerance_db=tolerance_db,
            rms_discrepancy_db=round(rms, 2),
            note=note,
        )
    except Exception as exc:
        return _err(f"verify_fir_effect failed: {exc}")


async def _tool_verify_input_eq_effect(
    pre_session_id: int,
    post_session_id: int,
    predicted_filters: list[dict],
    tolerance_db: float = 2.0,
    min_hz: float = 20.0,
    max_hz: float = 200.0,
) -> dict:
    """Compare an input-EQ filter set's predicted effect against the measured delta.

    After applying input PEQ via ``apply_input_eq``, the predicted FR change
    (computed by simulating ``predicted_filters`` against ``pre_session_id``)
    and the measured delta (post − pre at 1/3-octave) should match within
    ~2 dB. Bigger divergences flag:

      - Filter slot conflict — the existing input EQ wasn't fully replaced.
      - CamillaDSP biquad coefficient quantization (rare; usually <0.5 dB).
      - Routing mismatch — the signal isn't actually flowing through the
        channel(s) the filters were written to.
      - Measurement variance — coherence too low at the divergent band.

    Mirrors the contract of ``verify_fir_effect``. ``predicted_filters`` is
    the same list you passed to ``apply_input_eq``; the tool simulates them
    against ``pre_session_id`` to compute the predicted delta.
    """
    from .storage import SessionStore
    import math

    try:
        store = SessionStore()
        sessions = store.list_sessions()
        pre = next((s for s in sessions if s.id == pre_session_id), None)
        post = next((s for s in sessions if s.id == post_session_id), None)
        if pre is None:
            return _err(f"pre_session_id {pre_session_id} not found")
        if post is None:
            return _err(f"post_session_id {post_session_id} not found")
        if not pre.start_fr or not pre.start_fr.frequencies:
            return _err(f"pre session {pre_session_id} has no FR data")
        if not post.start_fr or not post.start_fr.frequencies:
            return _err(f"post session {post_session_id} has no FR data")
        if not predicted_filters:
            return _err("predicted_filters is required")

        # Compute predicted FR after applying the filters to the pre session.
        sim_result = await _tool_simulate_eq(
            session_id=pre_session_id,
            filters=predicted_filters,
            min_hz=min_hz,
            max_hz=max_hz,
        )
        if not sim_result.get("ok"):
            return _err(f"simulate_eq failed: {sim_result.get('error')}")

        # 1/3-octave bands for both sessions and the simulated post.
        pre_bands = _downsample_to_third_octave(
            pre.start_fr.frequencies, pre.start_fr.spl,
        )
        post_bands = _downsample_to_third_octave(
            post.start_fr.frequencies, post.start_fr.spl,
        )
        pre_by_freq = {b["freq_hz"]: b["spl_db"] for b in pre_bands}
        post_by_freq = {b["freq_hz"]: b["spl_db"] for b in post_bands}

        # Parse simulate_eq's compact "freq:spl,..." string into a dict.
        pred_post_by_freq: dict[float, float] = {}
        try:
            raw = sim_result.get("predicted_fr", "")
            for chunk in raw.split(","):
                if ":" not in chunk:
                    continue
                freq_str, spl_str = chunk.split(":", 1)
                pred_post_by_freq[float(freq_str)] = float(spl_str)
        except Exception:
            pass

        # Re-band the high-resolution simulated FR to 1/3-octave for comparison.
        if pred_post_by_freq:
            sim_freqs = sorted(pred_post_by_freq)
            sim_spl = [pred_post_by_freq[f] for f in sim_freqs]
            sim_bands = _downsample_to_third_octave(sim_freqs, sim_spl)
            sim_by_freq = {b["freq_hz"]: b["spl_db"] for b in sim_bands}
        else:
            sim_by_freq = {}

        bands: list[dict] = []
        off_spec: list[dict] = []
        discrepancies: list[float] = []
        for freq in sorted(pre_by_freq):
            if freq < min_hz or freq > max_hz:
                continue
            if freq not in post_by_freq or freq not in sim_by_freq:
                continue
            measured_delta = post_by_freq[freq] - pre_by_freq[freq]
            predicted_delta = sim_by_freq[freq] - pre_by_freq[freq]
            discrepancy = measured_delta - predicted_delta
            entry = {
                "freq_hz": freq,
                "predicted_delta_db": round(predicted_delta, 2),
                "measured_delta_db": round(measured_delta, 2),
                "discrepancy_db": round(discrepancy, 2),
            }
            bands.append(entry)
            discrepancies.append(discrepancy)
            if abs(discrepancy) > tolerance_db:
                off_spec.append(entry)

        rms = math.sqrt(
            sum(d * d for d in discrepancies) / len(discrepancies)
        ) if discrepancies else 0.0
        within = len(off_spec) == 0
        note = (
            "All bands within tolerance — input EQ landed as predicted."
            if within
            else f"{len(off_spec)} bands exceed {tolerance_db} dB tolerance — "
                 f"check filter slot conflict / routing / coherence at "
                 f"those bands."
        )
        return _ok(
            bands=bands,
            off_spec_bands=off_spec,
            within_tolerance=within,
            tolerance_db=tolerance_db,
            rms_discrepancy_db=round(rms, 2),
            note=note,
        )
    except Exception as exc:
        return _err(f"verify_input_eq_effect failed: {exc}")


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
            "Apply EQ filters to DSP output(s). Target a scope by name via `target` — "
            "a group name ('bass', 'front_soundstage'), a transducer name ('sub_left', "
            "'left_main'), or a role ('sub', 'main'). Pass `output_index` instead for raw "
            "output dispatch (legacy). Omit both for the legacy broadcast-to-all-subs "
            "behaviour. Filters are validated by SafetyValidator with the target's "
            "per-transducer profile; unsafe filters return {ok: false, "
            "error: 'SafetyValidator: ...'}. Each filter: {freq, gain_db, q, type}. "
            "A mandatory HPF (default 18 Hz) must be included when the profile requires one.\n\n"
            "**For per-sub modal correction, prefer design_fir on FIR-capable hardware.** "
            "PEQ reduces modal peak amplitude but leaves the ringing time unchanged; FIR "
            "both flattens magnitude AND shortens T60 decay. Check `eq_capabilities.fir_capable` "
            "in get_config. Use apply_eq for: (a) the mandatory infrasonic HPF, (b) per-sub "
            "safety limiting, (c) fallback when the hardware is FIR-incapable (miniDSP 2x4 HD)."
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
                "target": {
                    "type": "string",
                    "description": (
                        "Named scope — group ('bass'), transducer ('sub_left'), or "
                        "role ('sub'). Resolves via the signal graph. Mutually exclusive "
                        "with output_index; omit both for legacy broadcast-to-subs."
                    ),
                },
                "output_index": {
                    "type": "integer",
                    "description": (
                        "Raw DSP output index. Legacy path; prefer `target` for new "
                        "recipes. If omitted (and target omitted), writes to all "
                        "configured sub outputs."
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
            "Use this for the TARGET CURVE — the shared shape every output receives "
            "(Harman, cinema-bass, flat). Designed to be applied AFTER per-sub correction "
            "(design_fir / apply_eq per sub) has flattened each sub's individual response. "
            "Do NOT use input EQ to notch individual modes — modal cuts belong on the "
            "specific sub that excites them. Pair with fit_shelf_for_target to auto-derive "
            "the shelf parameters for a given target curve. "
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
        name="set_speaker_distances",
        description=(
            "Push per-channel Audyssey speaker distances directly to the AVR via "
            "TCP, bypassing the MultEQ Editor app's UI cap (59.1 ft / 18 m on "
            "X3800H). Use to compensate sub-only FIR group delay by setting the "
            "sub LARGER than physical (so AVR delays mains to wait for the "
            "FIR-delayed sub) or, equivalently, mains LARGER than physical. "
            "The configured value persists past the UI cap; the AVR firmware "
            "still caps the *applied* per-channel delay at "
            "MAX_SPEAKER_DELAY_MS (see get_state). "
            "IMPORTANT: distances are only APPLIED when Audyssey is active — "
            "i.e. sound mode is NOT Pure Direct AND MultEQ is NOT Off. The "
            "tool response includes an `audyssey` field reporting whether the "
            "current sound mode + MultEQ setting will actually apply the "
            "distances; if `audyssey.active` is False the write succeeds but "
            "the audio path silently ignores it. "
            "REQUIRES EXPLICIT USER CONFIRMATION before calling — this writes "
            "Audyssey calibration state. Always summarise distances + commit "
            "flag and wait for the user to approve."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "distances": {
                    "type": "object",
                    "description": (
                        "Channel→meters map. Keys are Audyssey commandIds: "
                        "FL, C, FR, SLA, SRA, TFL, TFR, TRL, TRR, SBL, SBR, "
                        "SW1, SW2, SW3, SW4. Values in METERS as floats."
                    ),
                    "additionalProperties": {"type": "number"},
                },
                "n_positions": {
                    "type": "integer",
                    "description": (
                        "Number of measurement positions in the AVR's stored "
                        "calibration. Get from a saved .ady file's responseData "
                        "size, or pass 1 for single-position. Default: 1."
                    ),
                    "default": 1,
                },
                "commit": {
                    "type": "boolean",
                    "description": (
                        "If true, send AudyFinFlg=Fin to persist to NVRAM. "
                        "If false (default), the change is volatile and lost "
                        "on AVR power cycle."
                    ),
                    "default": False,
                },
                "use_custom": {
                    "type": "boolean",
                    "description": (
                        "When true, uses the OCA-style envelope bypass: payload "
                        "is {Distance, AudyFinFlg=NotFin}, followed by an explicit "
                        "AudyFinFlg=Fin commit before EXIT_AUDMD. Verified to "
                        "extend the firmware applied-delay cap from ~38 ms (UI "
                        "limit) to ~55 ms (envelope limit) on X3800H. ``commit`` "
                        "is forced to True when this is set — the Fin commit IS "
                        "the bypass mechanism. CRITICAL: caller must NOT enter "
                        "Manual Setup > Distances on the AVR after pushing — "
                        "that re-validates and snaps back to the 6 m cap. "
                        "Side effect: minimal envelope (Distance + NotFin only) "
                        "doesn't carry MultEQ EQ params, so the AVR may apply "
                        "defaults for AudyMultEq/AudyEqRef/AudyEqSet on commit, "
                        "drifting mains FR by ±5-10 dB in mids. For full-state "
                        "preservation see scripts/audyssey_push_full_envelope.py."
                    ),
                    "default": False,
                },
            },
            "required": ["distances"],
        },
    ),
    Tool(
        name="design_avr_fir",
        description=(
            "Design an AVR-format polyphase FIR for one Denon/Marantz "
            "Audyssey channel from a target-curve specification. The "
            "result is cached server-side keyed by (cache_key, channel_id) "
            "and applied via apply_avr_fir. Pipeline: target FR → "
            "16,321-tap (speaker) / 16,055-tap (sub) IR → XT32 4-band "
            "polyphase decimation → 1024 / 704 AVR-format coefficients. "
            "Use one design_avr_fir call per channel; then a single "
            "apply_avr_fir to push them all in one TCP session."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "channel_id": {
                    "type": "string",
                    "description": (
                        "Audyssey channel commandId — e.g. FL, C, FR, SLA, "
                        "SRA, TFL, TFR, TRL, TRR, SBL, SBR, SW1, SW2, SW3, "
                        "SW4, LFE."
                    ),
                },
                "target_curve_db": {
                    "type": "array",
                    "description": (
                        "List of {freq_hz, gain_db} points defining the "
                        "desired EQ curve. Outside the supplied frequency "
                        "range gain tapers to 0 dB. Sub channels typically "
                        "specify points across 20-200 Hz; speakers "
                        "20-20,000 Hz."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "freq_hz": {"type": "number"},
                            "gain_db": {"type": "number"},
                        },
                        "required": ["freq_hz", "gain_db"],
                    },
                },
                "cache_key": {
                    "type": "string",
                    "description": (
                        "Caller-chosen identifier — usually a session id "
                        "or a string like 'iter-3'. apply_avr_fir uses "
                        "this to find the coefficient set."
                    ),
                },
                "samplerate_hz": {
                    "type": "number",
                    "description": (
                        "FIR design sample rate. Default 48000 (matches "
                        "the AVR's native processing rate for the 48 kHz "
                        "bank — XT32 stores three banks but uploads the "
                        "same coefficients to all)."
                    ),
                    "default": 48000,
                },
            },
            "required": ["channel_id", "target_curve_db", "cache_key"],
        },
    ),
    Tool(
        name="apply_avr_fir",
        description=(
            "Push cached AVR-format FIR coefficients to the receiver via "
            "the Audyssey TCP/1256 protocol. Overwrites the AVR's MultEQ "
            "filter banks with the per-channel coefficients designed by "
            "prior design_avr_fir calls (matched by cache_key). The full "
            "16-field SET_SETDAT envelope is sent to keep the AVR's EQ "
            "state coherent; coefficient streams are shipped per channel × "
            "target_curve × sample_rate; the AudyFinFlg=Fin commit at "
            "the end persists everything to the AVR's flash. "
            "HARD RULE: caller MUST NOT enter Manual Setup > Distances "
            "on the AVR after a successful push — that triggers firmware "
            "re-validation. Always backup the original .ady before calling "
            "this tool. REQUIRES EXPLICIT USER CONFIRMATION."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "host": {
                    "type": "string",
                    "description": "AVR IP or hostname.",
                },
                "ady_path": {
                    "type": "string",
                    "description": (
                        "Path to a .ady file with the AVR's stored "
                        "calibration state. Provides per-channel "
                        "speaker-type, crossover, level, and the "
                        "channel list. Distances are taken from .ady "
                        "but can be selectively overridden via "
                        "distances_override_m."
                    ),
                },
                "cache_key": {
                    "type": "string",
                    "description": "Cache key used at design_avr_fir time.",
                },
                "channel_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Subset of channels to upload. Default: all "
                        "channels in the .ady that have a cached FIR "
                        "under cache_key."
                    ),
                },
                "distances_override_m": {
                    "type": "object",
                    "description": (
                        "Optional {channel: meters} map to override .ady "
                        "distances during the upload. Use to push past "
                        "the variance cap (e.g. SW1=20.0)."
                    ),
                    "additionalProperties": {"type": "number"},
                },
                "target_curves": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Which target-curve banks to write. Default "
                        "[\"00\", \"01\"] writes both Flat and Reference "
                        "so user can toggle at runtime via AudyEqSet."
                    ),
                },
                "samplerates_hz": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": (
                        "Sample rates to ship per channel. Default "
                        "[32000, 44100, 48000] (XT32's three banks)."
                    ),
                },
                "inter_packet_delay_ms": {
                    "type": "number",
                    "description": (
                        "Pause between SET_COEFDT packets in ms. "
                        "Helps less-buffered receivers keep up. Default 5."
                    ),
                    "default": 5.0,
                },
            },
            "required": ["host", "ady_path", "cache_key"],
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
        name="fit_shelf_for_target",
        description=(
            "Fit a shelf filter (freq, gain_db, Q) that minimizes RMS deviation of the "
            "predicted post-filter FR from a target curve in-band. Replaces manual "
            "shelf-iteration against a Harman or cinema-bass target. Returns the "
            "recommended filter parameters and predicted RMS at the optimum. Apply via "
            "apply_input_eq with 18 Hz HPF prepended."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "integer",
                    "description": "Measurement session to fit against",
                },
                "target_curve": {
                    "type": "object",
                    "description": "{'points': [{'freq': Hz, 'spl': dB}, ...]}",
                },
                "min_hz": {"type": "number", "description": "Lower band edge (default: 20)"},
                "max_hz": {"type": "number", "description": "Upper band edge (default: 120)"},
                "shelf_type": {
                    "type": "string",
                    "enum": ["low_shelf", "high_shelf"],
                    "description": "Shelf kind (default: low_shelf — the usual bass-curve shaper)",
                },
                "freq_bounds": {
                    "type": "array", "items": {"type": "number"},
                    "description": "[lo, hi] Hz (default: [25, 80])",
                },
                "gain_bounds": {
                    "type": "array", "items": {"type": "number"},
                    "description": "[lo, hi] dB (default: [-6, +10])",
                },
                "q_bounds": {
                    "type": "array", "items": {"type": "number"},
                    "description": "[lo, hi] Q (default: [0.3, 1.5])",
                },
            },
            "required": ["session_id", "target_curve"],
        },
    ),
    Tool(
        name="start_calibration",
        description=(
            "One-call Phase 0 of a calibration run. Resets persisted per-output DSP state "
            "to defaults (polarity/gain/delay/FIR/EQ), snapshots current hardware state, and "
            "opens a calibration run record. Returns run_id for use with save_calibration_iteration "
            "/ update_calibration_run. Prevents the 'stale state from prior run silently corrupts "
            "new measurements' failure mode that ate 4 hours on 2026-04-24. "
            "Pass reset_state=false only if resuming a run that's already configured. "
            "Pass preserve_eq=true to keep existing EQ filters (rare — usually you want fresh)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "recipe_name": {
                    "type": "string",
                    "description": "Recipe being followed, e.g. 'bass-calibration-fir'",
                },
                "target": {
                    "type": "string",
                    "description": "Target curve, e.g. 'harman-bass', 'cinema-bass', 'flat'",
                },
                "reset_state": {
                    "type": "boolean",
                    "description": "Reset persisted DSP state to defaults (default: true)",
                },
                "preserve_eq": {
                    "type": "boolean",
                    "description": "If resetting, keep EQ filters (default: false)",
                },
            },
            "required": ["recipe_name", "target"],
        },
    ),
    Tool(
        name="save_calibration_run",
        description=(
            "Create a new calibration run record. Returns the run_id for use with "
            "save_calibration_iteration and update_calibration_run. Call this at the "
            "start of a calibration session. Optionally captures equipment state "
            "(get_device_state output) for later review. "
            "Prefer start_calibration — it combines this with dsp_reset_defaults in one call."
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
            "denon, minidsp, mic, sub, measurement, connections. Also includes "
            "the resolved signal_graph (synthesised from legacy fields when no "
            "explicit `signal_graph:` block is in config.yaml) so recipes can "
            "discover transducer names, groups, and profiles in one call."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    Tool(
        name="get_signal_graph",
        description=(
            "Return a compact summary of the signal graph: sources, processors, "
            "transducers (name, role, processor, output_index, profile, position), "
            "groups, and safety profiles. Read this once at the start of a recipe "
            "to discover scope names ('bass', 'front_soundstage', 'left_main') that "
            "can be passed as `target` to apply_eq / apply_input_eq. Legacy installs "
            "without a signal_graph in config.yaml still return a synthesised graph "
            "so callers never have to branch on 'is there a graph or not.'"
        ),
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    Tool(
        name="resolve_target",
        description=(
            "Resolve a scope string to the list of transducers it covers. Accepts "
            "a group name ('bass'), a transducer name ('sub_left'), or a role "
            "('sub'). Returns an array of {transducer, processor, output_index, "
            "profile, position}. Use this before calling tools that don't yet "
            "accept a `target` parameter (set_delay, set_polarity, set_output_gain, "
            "mute_output, unmute_output) — resolve to concrete output indices, "
            "then pass those to the per-output tools. An empty return means the "
            "target didn't match any group, transducer, or role."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Group, transducer, or role name.",
                },
            },
            "required": ["target"],
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
            "Pass `target` (group / transducer / role) for graph-resolved dispatch "
            "(e.g. mute everything except `target='sub_1'`), or `output_indices` for "
            "raw dispatch on the default DSP. Mutually exclusive."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Named scope (group, transducer, or role).",
                },
                "output_indices": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Raw output indices on the default DSP (legacy).",
                },
            },
        },
    ),
    Tool(
        name="unmute_output",
        description=(
            "Unmute DSP outputs by restoring gain to 0 dB. "
            "Takes the same `target` / `output_indices` shape as mute_output."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Named scope (group, transducer, or role).",
                },
                "output_indices": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Raw output indices on the default DSP (legacy).",
                },
            },
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
            "Set delay for DSP output(s) in milliseconds. Pass `target` (group / "
            "transducer / role) to dispatch through the graph — the same delay "
            "value is written to every resolved output, including across multiple "
            "processors when the target spans them. Pass `output_index` for raw "
            "single-output dispatch on the default DSP. Mutually exclusive."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Named scope (group, transducer, or role).",
                },
                "output_index": {
                    "type": "integer",
                    "description": "Raw DSP output index on the default DSP (legacy).",
                },
                "delay_ms": {
                    "type": "number",
                    "description": "Delay in milliseconds",
                },
            },
            "required": ["delay_ms"],
        },
    ),
    Tool(
        name="set_polarity",
        description=(
            "Set polarity (inverted=true flips phase 180°). Same `target` / "
            "`output_index` dispatch as set_delay."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Named scope (group, transducer, or role).",
                },
                "output_index": {
                    "type": "integer",
                    "description": "Raw DSP output index (legacy).",
                },
                "inverted": {
                    "type": "boolean",
                    "description": "true = inverted (180° flip), false = normal",
                },
            },
            "required": ["inverted"],
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
            "Extract IR onset time, polarity sign, and SPL from a stored measurement session. "
            "VALID for solo-sub alignment ONLY (each measurement must share the same DSP chain). "
            "Subtract the earliest peak_time_s from the latest to get the delay offset between subs; "
            "peak_sign tells you polarity (flip if it differs from the reference sub); "
            "spl_db is the relative level for gain matching. "
            "INVALID for cross-path comparisons (sub-vs-mains, FIR-chain vs no-FIR, "
            "cal-mode vs HDMI) — peak_time_s reflects FIR shape and buffer latency, not "
            "acoustic arrival. Use compare_sub_phase or the loopback rig for cross-path timing. "
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
            "Tap-count ceiling and FIR sample rate come from eq_capabilities "
            "(fir_max_taps_per_output and fir_sample_rate_hz). "
            "Use after analyze_decay to shorten room-mode ringing that PEQ cannot fix — "
            "FIR corrects the time-domain decay; PEQ only reduces the peak magnitude. "
            "After writing, get_output_state will show fir_taps = len(coefficients). "
            "Source is one of `coefficients` (inline float array; must be peak-normalized "
            "≤ 1.0) or `design_session_id` (pulls coefficients from the last design_fir "
            "call on that session, skipping the large-payload round-trip). "
            "Coefficients persist across CamillaDSP config rebuilds and MCP restarts."
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
                        "Normalize so peak abs value <= 1.0. "
                        "Mutually exclusive with design_session_id."
                    ),
                },
                "design_session_id": {
                    "type": "integer",
                    "description": (
                        "session_id from a prior design_fir call. Uses the server-side "
                        "cached coefficients. Mutually exclusive with coefficients. "
                        "Cache is cleared on MCP restart — re-run design_fir if the "
                        "server was bounced since the design."
                    ),
                },
            },
            "required": ["output_index"],
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
        name="design_corrective_fir",
        description=(
            "Empirical 2-step corrective FIR. Computes target − measured FR "
            "from a post-baseline-FIR session, designs a min-phase magnitude "
            "correction FIR for the residual, and convolves it with the "
            "existing cached FIR for ``output_index``. Returns a "
            "design_session_id for ``apply_fir(design_session_id=...)``. "
            "Use after design_modal_fir + apply_fir reveal a per-room "
            "deviation from the target curve that the modal FIR alone "
            "didn't predict (anti-pulse phase interaction with the room's "
            "response — see recipe Section 2.2b)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "integer",
                    "description": (
                        "Post-baseline-FIR measurement (the room's response "
                        "after any existing FIR is applied)."
                    ),
                },
                "target_curve": {
                    "type": "object",
                    "description": (
                        "Target curve points + optional band. "
                        "Shape: {'points': [{'freq': 25, 'spl': 5}, ...], "
                        "'band': [25, 120]}. Anchored to 60-100 Hz midband "
                        "so absolute SPL drops out."
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
                        },
                    },
                    "required": ["points"],
                },
                "output_index": {
                    "type": "integer",
                    "description": "DSP output to convolve onto.",
                },
                "num_taps": {
                    "type": "integer",
                    "default": 1024,
                    "description": "Corrective FIR length. Default 1024.",
                },
                "focus_hz": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "[lo, hi] band for correction; outside tapers to 0 dB.",
                },
                "return_coefficients": {
                    "type": "boolean",
                    "default": False,
                },
            },
            "required": ["session_id", "target_curve", "output_index"],
        },
    ),
    Tool(
        name="reset_dsp_defaults",
        description=(
            "Reset ALL persisted per-output DSP state (polarity, gain, delay, FIR, per-output EQ) "
            "to defaults, plus input EQ to infrasonic HPF only. Use as Phase 0 of every calibration "
            "run — active_dsp_state persists across container restarts, so a polarity flip or gain "
            "trim from a prior run silently corrupts subsequent measurements until explicitly cleared. "
            "Returns a detailed record of every value that was reset. Pass dry_run=true to preview "
            "without touching hardware. Pass preserve_eq=true to reset only polarity/gain/delay/FIR "
            "(useful for re-alignment without losing EQ work)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "dry_run": {
                    "type": "boolean",
                    "description": "Preview changes without applying (default: false)",
                },
                "preserve_eq": {
                    "type": "boolean",
                    "description": "Keep EQ filters, only reset polarity/gain/delay/FIR (default: false)",
                },
            },
        },
    ),
    Tool(
        name="set_cal_mode",
        description=(
            "Switch CamillaDSP capture between live and calibration sources. "
            "Cal mode (enabled=true) routes capture from snd-aloop loopback so a "
            "sweep player can inject directly into CamillaDSP, bypassing the AVR — "
            "Audyssey/MultEQ filters cannot color the cal stimulus on this path. "
            "Live mode (enabled=false) restores capture from the AVR/Focusrite line "
            "for normal listening. Always pair enable→disable around a calibration "
            "session; leaving cal mode on silences the system for movies/music. "
            "Returns the active mode and the loopback playback device measure() "
            "writes to in cal mode."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "enabled": {
                    "type": "boolean",
                    "description": "True to enter cal mode (loopback capture); False to return to live capture.",
                },
            },
            "required": ["enabled"],
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
            "Set gain for DSP output(s) in dB. Range: -127 to +6 dB. Same `target` / "
            "`output_index` dispatch as set_delay — the same gain value is written to "
            "every resolved output. Use for per-sub level trimming during calibration. "
            "Avoid gains above 0 dB (risks clipping). mute_output / unmute_output are "
            "preferred for temporary silencing."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Named scope (group, transducer, or role).",
                },
                "output_index": {
                    "type": "integer",
                    "description": "Raw DSP output index (legacy).",
                },
                "gain_db": {
                    "type": "number",
                    "description": "Gain in dB. Range: -127 to +6.",
                },
            },
            "required": ["gain_db"],
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
        name="set_routing",
        description=(
            "Apply an arbitrary input→output routing matrix to the active DSP. "
            "Generic variant of configure_matrix — not limited to 2 inputs / 4 "
            "outputs (necessary for CamillaDSP + 18i20 with 20 inputs / 10 outputs). "
            "The passed routing is MERGED onto the driver's current state: rows "
            "not mentioned stay as-is. Values are booleans (true = routed/unmuted, "
            "false = muted). Keys are 0-indexed channel numbers as strings in JSON. "
            "Example: {routing: {'2': {'1': true, '2': true, '3': true}}} routes "
            "analog input 3 (0-indexed 2) to outputs 2, 3, 4 on the 18i20."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "routing": {
                    "type": "object",
                    "description": (
                        "Partial routing matrix: {input_index_str: "
                        "{output_index_str: bool}}. Indices are 0-based."
                    ),
                    "additionalProperties": {
                        "type": "object",
                        "additionalProperties": {"type": "boolean"},
                    },
                },
            },
            "required": ["routing"],
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
        name="recommend_fir_phase",
        description=(
            "Recommend FIR phase mode based on a post-FIR solo measurement. "
            "Codifies the bass-calibration-fir recipe's Phase 2.5a decision "
            "point so the LLM can't silently skip it: call this AFTER "
            "applying the initial min-phase FIR and measuring that sub solo. "
            "If any mode has peak_db >= peak_db_threshold (default 0, i.e. above "
            "band average) AND T60 >= t60_threshold_ms (default 500), returns "
            "recommendation='mixed' with a suggested tap count whose impulse "
            "length covers 2× the worst T60 (clamped to the driver's "
            "fir_max_taps_per_output). Otherwise recommendation='minimum' and "
            "the current FIR is adequate."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "integer",
                    "description": (
                        "Solo post-FIR measurement session to analyse. "
                        "This must be a measurement taken AFTER applying the "
                        "initial min-phase FIR to the sub in question."
                    ),
                },
                "t60_threshold_ms": {
                    "type": "number",
                    "description": (
                        "T60 threshold for flagging a mode. Default 500 ms — "
                        "matches the recipe's mixed-phase trigger. Lower values "
                        "make the check more aggressive."
                    ),
                    "default": 500.0,
                },
                "peak_db_threshold": {
                    "type": "number",
                    "description": (
                        "Only consider modes with peak_db >= this value. "
                        "Default 0.0 (above band average). Set lower to include "
                        "less prominent modes."
                    ),
                    "default": 0.0,
                },
                "freq_min": {"type": "number", "default": 20.0},
                "freq_max": {"type": "number", "default": 200.0},
                "mains_distance_budget_ms": {
                    "type": "number",
                    "description": (
                        "Per-channel mains/centre/surround speaker-distance "
                        "headroom available to compensate for FIR-induced sub "
                        "latency. The FIR delays the sub chain only; mains must "
                        "be delayed by the same amount via DISTANCE settings "
                        "(not the global 'Audio Delay/lip-sync' slider, which "
                        "is for video sync). Default 53 ms ≈ 60 ft, the "
                        "Denon X-series UI cap. The firmware accepts larger "
                        "values; write them via MultEQ-X / ratbuddyssey on "
                        "port 1256 to push past the UI clamp."
                    ),
                    "default": 53.0,
                },
                "preringing_ms": {
                    "type": "number",
                    "description": (
                        "Preferred pre-ringing window for mixed-phase. Default "
                        "25 ms (inaudible for bass below ~100 Hz). This is "
                        "both the filter latency AND the psychoacoustic smear "
                        "budget — larger values cancel more modal decay but "
                        "consume more AVR Audio-Delay headroom."
                    ),
                    "default": 25.0,
                },
            },
            "required": ["session_id"],
        },
    ),
    Tool(
        name="verify_fir_effect",
        description=(
            "Compare a designed FIR's predicted effect (from design_fir's "
            "predicted_effect field) against the measured delta between pre-FIR "
            "and post-FIR solo measurements. Flags bands where |predicted - "
            "measured| exceeds tolerance_db (default 2.0). Use this after "
            "apply_fir to catch: (1) FIR that didn't land (pipeline wipe, "
            "silent rejection), (2) unexpected room interaction, (3) noisy "
            "measurement. Returns per-band comparison, off-spec band list, "
            "and overall RMS discrepancy."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "pre_session_id": {
                    "type": "integer",
                    "description": "Solo measurement session_id from BEFORE apply_fir.",
                },
                "post_session_id": {
                    "type": "integer",
                    "description": "Solo measurement session_id from AFTER apply_fir on the same sub.",
                },
                "predicted_effect": {
                    "type": "array",
                    "description": (
                        "The `predicted_effect` field from design_fir's response. "
                        "List of {freq_hz, fir_effect_db}. Forward verbatim."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "freq_hz": {"type": "number"},
                            "fir_effect_db": {"type": "number"},
                        },
                        "required": ["freq_hz", "fir_effect_db"],
                    },
                },
                "tolerance_db": {
                    "type": "number",
                    "description": "Max |predicted - measured| per band before flagging. Default 2.0.",
                    "default": 2.0,
                },
                "min_hz": {"type": "number", "default": 20.0},
                "max_hz": {"type": "number", "default": 120.0},
            },
            "required": ["pre_session_id", "post_session_id", "predicted_effect"],
        },
    ),
    Tool(
        name="verify_input_eq_effect",
        description=(
            "Compare an input-EQ filter set's predicted effect (simulated "
            "against the pre session) against the measured delta between "
            "pre and post-apply_input_eq sessions. Flags bands where "
            "|predicted − measured| exceeds tolerance_db (default 2.0). "
            "Use after apply_input_eq to catch: (1) filter slot conflict "
            "(existing EQ wasn't replaced), (2) routing mismatch (signal "
            "not flowing through the channels the filters were written to), "
            "(3) measurement variance. Returns per-band comparison, "
            "off-spec band list, and RMS discrepancy."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "pre_session_id": {"type": "integer"},
                "post_session_id": {"type": "integer"},
                "predicted_filters": {
                    "type": "array",
                    "description": (
                        "Same list passed to apply_input_eq. Each item: "
                        "{freq, gain_db, q, type}."
                    ),
                    "items": {"type": "object"},
                },
                "tolerance_db": {"type": "number", "default": 2.0},
                "min_hz": {"type": "number", "default": 20.0},
                "max_hz": {"type": "number", "default": 200.0},
            },
            "required": ["pre_session_id", "post_session_id", "predicted_filters"],
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
                "exclude_geometry": {
                    "type": "boolean",
                    "description": (
                        "Default true. When true, runs analyze_phase on the "
                        "session and excludes 1/3-octave bands classified as "
                        "'geometry' (near-π phase offset from cancellation at "
                        "the listener position) from the RMS calculation. EQ "
                        "cannot fix these; including them inflates RMS and "
                        "drives the driving agent to iterate past convergence."
                    ),
                },
                "weight_by_coherence": {
                    "type": "boolean",
                    "description": (
                        "Default false. When true, weights each frequency's "
                        "contribution to RMS by its measured coherence — "
                        "low-coherence bands (noisy measurement) contribute "
                        "proportionally less. Also returns noise_floor_estimate_db; "
                        "if rms_db ≈ noise_floor_estimate_db, further iteration is "
                        "chasing measurement noise. Use this to know when to stop."
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
                "exclude_geometry": {
                    "type": "boolean",
                    "description": (
                        "Default true. When true, runs analyze_phase and excludes "
                        "1/3-octave bands classified as 'geometry' (cancellation "
                        "nulls) from the headroom calculation — prevents the "
                        "anchor from being dragged toward a target the room can't "
                        "physically reach."
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
        name="optimize_sub_alignment",
        description=(
            "MSO-style multi-sub alignment. Given one solo-sub session per sub "
            "(mute others, measure, repeat), searches per-sub (delay_ms, gain_db, "
            "polarity_inverted) that minimize predicted combined-FR error in-band. "
            "Default objective is flatness of the combined response; pass a target_curve "
            "(e.g. Harman, cinema-bass) to optimize against a specific target instead. "
            "Replaces the compare_sub_phase delay_estimate for alignment — this directly "
            "optimizes the thing you care about (combined SPL) rather than inferring it "
            "from a phase-slope fit that can fail in strongly modal rooms. "
            "Scales to N subs. Apply per-sub recommendations via set_delay / "
            "set_output_gain / set_polarity, then measure combined to verify."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": (
                        "Solo measurement session IDs, one per sub (2+ required). Order "
                        "determines how recommendations are labeled in the response."
                    ),
                },
                "target_curve": {
                    "type": "object",
                    "description": (
                        "Optional {points:[{freq,spl},…]}. Omit for flatness objective."
                    ),
                },
                "min_hz": {"type": "number", "description": "Lower band edge (default: 20)"},
                "max_hz": {"type": "number", "description": "Upper band edge (default: 120)"},
                "max_delay_ms": {
                    "type": "number",
                    "description": "Max per-sub delay the optimizer will assign (default: 30)",
                },
                "search_polarity": {
                    "type": "boolean",
                    "description": "Include polarity flip in search (default: true)",
                },
                "gain_search_db": {
                    "type": "number",
                    "description": "±range for per-sub gain trim search (default: 3 dB)",
                },
                "priority_band": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": (
                        "Optional [lo_hz, hi_hz]. Weights this band 3× in the "
                        "objective so the optimizer preferentially eliminates "
                        "deep-bass nulls. Use to collapse the wideband + "
                        "narrowband 2-call workflow into one call (recipe Phase 1.5)."
                    ),
                },
            },
            "required": ["session_ids"],
        },
    ),
    Tool(
        name="sweep_inter_sub_delay",
        description=(
            "Automated inter-sub delay sweep. Replaces the manual ±2 ms "
            "human-driven sweep in recipe Phase 1.5. Takes post-alignment "
            "solo session IDs, the polarity / gain / base-delay state already "
            "applied, and sweeps the trailing sub's delay in fine steps to "
            "find the value that shallowest the deepest 1/3-octave null in "
            "the priority band (default 28-50 Hz)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Solo session IDs (2 subs).",
                },
                "sub_polarity": {
                    "type": "array",
                    "items": {"type": "boolean"},
                    "description": "Polarity per sub (default all false).",
                },
                "sub_gain_db": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "Gain dB per sub (default all 0).",
                },
                "base_delays_ms": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": (
                        "Currently-applied delay per sub. Trailing sub is the "
                        "one with the largest base delay; its delay is swept."
                    ),
                },
                "priority_band": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "[lo_hz, hi_hz] for null detection (default [28, 50]).",
                },
                "sweep_range_ms": {
                    "type": "number",
                    "description": "± range around base delay (default 2.0).",
                },
                "step_ms": {
                    "type": "number",
                    "description": "Step granularity (default 0.25).",
                },
            },
            "required": ["session_ids"],
        },
    ),
    Tool(
        name="design_fir",
        description=(
            "**Magnitude-correction FIR.** Designed against a session's FR; produces a "
            "filter whose magnitude response is the inverse of the measured response "
            "(scaled to the target curve). Phase mode is one of: "
            "``minimum`` (zero pre-ring; flattens magnitude only — leaves T60 untouched), "
            "``linear`` (symmetric impulse; full magnitude+phase correction; substantial latency), "
            "``mixed`` (homomorphic decomposition with bounded pre-ring; reduces some "
            "modal resonance via phase correction but does NOT actively cancel modes). "
            "\n\n"
            "**WHEN TO USE THIS:** baseline magnitude shaping per sub. Smooth FR targets "
            "(Harman shelf, flat). Modes with short T60 (<400 ms) where peak reduction "
            "is enough. \n\n"
            "**WHEN TO USE design_modal_fir INSTEAD:** rooms with long modal ringing "
            "(any mode T60 > 500 ms at peak > +6 dB). The phase_mode='mixed' option here "
            "does NOT actively cancel modes — it just allows non-causal magnitude correction. "
            "Active anti-pulse cancellation (which actually shortens T60, not just the peak) "
            "is design_modal_fir's job."
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
                "return_coefficients": {
                    "type": "boolean",
                    "description": (
                        "Default true. Set false to skip the coefficient array in the "
                        "response — coefficients are cached server-side keyed by "
                        "session_id; apply via apply_fir(design_session_id=<session_id>). "
                        "Use false when the array (e.g. 8k+ taps ≈ 140 KB JSON) would "
                        "exceed the client's token budget."
                    ),
                },
                "preringing_ms": {
                    "type": "number",
                    "description": (
                        "Mixed-phase only: maximum pre-ringing window in ms. "
                        "Bounds how far the excess-phase impulse extends before "
                        "the main energy peak — this is both the filter's added "
                        "audio latency AND the psychoacoustic smear window. "
                        "Default 25 ms (inaudible below ~100 Hz). Set to 0 to "
                        "degenerate to minimum-phase. Ignored by minimum/linear modes."
                    ),
                    "default": 25.0,
                },
            },
            "required": ["session_id"],
        },
    ),
    Tool(
        name="design_modal_fir",
        description=(
            "**Active modal-cancellation FIR.** Designs a mixed-phase FIR that "
            "places band-limited anti-pulses one half-wavelength before the main "
            "impulse for each room mode flagged as ``anti_pulse``. This actively "
            "cancels modal ringing in the time domain — both the peak magnitude "
            "AND the T60 decay tail are reduced. \n\n"
            "**WHEN TO USE THIS** (over plain ``design_fir``): \n"
            "- Combined or solo measurement shows mode T60 > 500 ms at peak > +6 dB \n"
            "- Multiple ringy modes (47, 70, 94 Hz, etc) — handles all in one FIR \n"
            "- You want T60 reduction, not just magnitude flattening \n\n"
            "**WHEN NOT TO USE** (use ``design_fir`` instead): \n"
            "- Rooms with already-short T60 (< 400 ms) — anti-pulse adds latency for "
            "marginal benefit \n"
            "- Pure target-curve shaping with no modal problem \n"
            "- Tight latency budgets (< 5 ms) — anti-pulse needs half-wavelength of "
            "pre-ring per mode (e.g. 7.14 ms at 70 Hz) \n\n"
            "Per-mode treatments via the ``intents`` argument: ``anti_pulse`` (cancel "
            "T60), ``linear_notch`` (precise magnitude cut), ``min_phase`` (gentle "
            "magnitude EQ), ``skip``. Omit ``intents`` to auto-classify based on "
            "T60+peak heuristics."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "integer",
                    "description": (
                        "Measurement session whose decay_modes drive the design. "
                        "Use combined-sub session for shared correction; per-sub "
                        "session if each sub needs distinct treatment."
                    ),
                },
                "intents": {
                    "type": "array",
                    "description": (
                        "Optional per-mode intents. If omitted, auto-classify via "
                        "default heuristic. Each entry: "
                        "{freq_hz, t60_ms, peak_db, treatment, cancel_strength?, rationale?}"
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "freq_hz": {"type": "number"},
                            "t60_ms": {"type": "number"},
                            "peak_db": {"type": "number"},
                            "treatment": {
                                "type": "string",
                                "enum": ["anti_pulse", "linear_notch", "min_phase", "skip"],
                            },
                            "cancel_strength": {
                                "type": "number",
                                "description": "0-1, default 0.6 — how aggressively to cancel.",
                            },
                            "bp_q": {
                                "type": "number",
                                "description": (
                                    "Bandpass Q on the anti-pulse envelope. "
                                    "Higher = narrower band = less adjacent-band "
                                    "spectral leakage (lets safety caps pass) but "
                                    "longer time-domain tail. Default 1.5; raise "
                                    "to 3-4 if a per-band thermal cap trips."
                                ),
                            },
                            "envelope": {
                                "type": "string",
                                "enum": ["gabor", "butterworth"],
                                "description": (
                                    "Anti-pulse envelope shape. ``gabor`` "
                                    "(default) is a Gaussian-windowed sinusoid "
                                    "with optimal time-frequency localization "
                                    "— minimal adjacent-band skirts. "
                                    "``butterworth`` is the legacy filtered-"
                                    "impulse with wider skirts; kept for "
                                    "regression / A-B testing only."
                                ),
                            },
                            "rationale": {"type": "string"},
                        },
                        "required": ["freq_hz", "treatment"],
                    },
                },
                "target_t60_ms": {
                    "type": "number",
                    "description": (
                        "Room-quality target T60 — modes already meeting it "
                        "are skipped, modes far above (T60 > target × "
                        "long_ringy_t60_factor) get anti-pulse treatment. "
                        "Industry references: 250 ms mastering, 300 ms "
                        "THX/Dolby (default), 500 ms HT, >700 ms untreated. "
                        "Used for auto-classification when ``intents`` is "
                        "omitted."
                    ),
                    "default": 300.0,
                },
                "peak_action_db": {
                    "type": "number",
                    "description": (
                        "Below this peak magnitude, modes are skipped "
                        "regardless of T60 (too quiet to bother)."
                    ),
                    "default": 3.0,
                },
                "short_loud_t60_factor": {
                    "type": "number",
                    "description": (
                        "Multiplier on target_t60_ms defining 'short' for "
                        "the linear_notch rule. T60 < target × this AND "
                        "peak > short_loud_peak_db → linear_notch."
                    ),
                    "default": 0.5,
                },
                "short_loud_peak_db": {
                    "type": "number",
                    "description": (
                        "Peak threshold for the linear_notch rule. Loud "
                        "short peaks get a precise magnitude cut instead "
                        "of an anti-pulse."
                    ),
                    "default": 12.0,
                },
                "long_ringy_t60_factor": {
                    "type": "number",
                    "description": (
                        "Multiplier on target_t60_ms defining 'long ringy' "
                        "for the anti_pulse rule. T60 > target × this → "
                        "anti_pulse."
                    ),
                    "default": 2.0,
                },
                "anti_pulse_cancel_strength": {
                    "type": "number",
                    "description": (
                        "How aggressively each anti_pulse cancels its mode. "
                        "0-1; default 0.6. Higher = more T60 reduction but "
                        "risks over-cancellation creating dips. Predicted "
                        "T60 reduction ≈ 60% × this."
                    ),
                    "default": 0.6,
                },
                "num_taps": {
                    "type": "integer",
                    "description": "FIR length. Default 4096 at 8 kHz internal = 512 ms span.",
                    "default": 4096,
                },
                "max_pre_ring_ms": {
                    "type": "number",
                    "description": (
                        "Pre-ring budget across all anti-pulses. Default 25 ms "
                        "(psychoacoustic threshold for sub-band content). The "
                        "actual pre-ring used = max half-wavelength of the "
                        "anti_pulse modes + a tail."
                    ),
                    "default": 25.0,
                },
                "samplerate": {
                    "type": "integer",
                    "description": (
                        "FIR design sample rate. Must match CamillaDSP processing "
                        "rate for coefficients to apply 1:1. Default 8000 (matches "
                        "current 8 kHz processing). Set to 48000 when running "
                        "48 kHz native (requires num_taps=24576 for same 512 ms window)."
                    ),
                    "default": 8000,
                },
                "return_coefficients": {
                    "type": "boolean",
                    "description": (
                        "When false (default), coefficients are cached server-side "
                        "and applied via apply_fir(design_session_id=...). When true, "
                        "the array is returned in the response."
                    ),
                    "default": False,
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
            "Fit PEQ filter(s) to minimize RMS error in a frequency range. "
            "With ``num_filters=1`` (default), grid-search + refine one peaking "
            "filter — the LLM picks the region, the tool finds the best "
            "(freq, gain, Q). With ``num_filters > 1``, jointly optimize N "
            "peaking filters via scipy's Levenberg-Marquardt (trust-region "
            "least squares with bounds). One call produces a converged PEQ "
            "set that would otherwise take N manual measure-and-tweak "
            "iterations. Up to 8 filters (SafetyValidator slot budget). "
            "Returns the filter list, RMS before/after, and optimizer status. "
            "Peaking-only — pair with a mandatory HPF from apply_eq."
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
                    "description": "Target curve with absolute-SPL points. Pass this OR target_offsets.",
                    "properties": {
                        "points": {"type": "array", "items": {"type": "object"}},
                        "band": {"type": "array", "items": {"type": "number"}},
                    },
                },
                "target_offsets": {
                    "type": "array",
                    "description": (
                        "Target offsets as [{freq_hz, offset_db}] relative to a "
                        "reference frequency (e.g. Harman: 80 Hz = 0 dB). "
                        "When provided, the tool calls anchor_target internally "
                        "to pick the correct reference_spl for this baseline — "
                        "avoids stale-anchor bugs. Pass this OR target_curve."
                    ),
                    "items": {"type": "object"},
                },
                "freq_range": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "[lo_hz, hi_hz] — frequency range to optimize within",
                },
                "constraints": {
                    "type": "object",
                    "description": (
                        "Optional. {max_boost_db, min_q, max_q, max_q_boost, "
                        "boost_q_penalty, filter_type, preserve_mean, "
                        "preserve_mean_strength, doublet_penalty, "
                        "doublet_max_hz}. filter_type honored only when "
                        "num_filters=1 (joint mode is peaking-only). "
                        "preserve_mean=True keeps mean(correction) ≈ 0 so "
                        "broadband level stays put; preserve_mean_strength "
                        "(default 1.0) multiplies the soft-constraint "
                        "weight. doublet_penalty > 0 discourages opposing-"
                        "sign filter pairs within doublet_max_hz. "
                        "max_q_boost caps Q on positive-gain (boost) "
                        "filters separately — prevents broad low-Q boosts "
                        "from bleeding into adjacent bands."
                    ),
                    "properties": {
                        "max_boost_db": {"type": "number"},
                        "min_q": {"type": "number"},
                        "max_q": {"type": "number"},
                        "max_q_boost": {"type": "number"},
                        "boost_q_penalty": {"type": "number"},
                        "filter_type": {"type": "string"},
                        "preserve_mean": {"type": "boolean"},
                        "preserve_mean_strength": {"type": "number"},
                        "doublet_penalty": {"type": "number"},
                        "doublet_max_hz": {"type": "number"},
                    },
                },
                "num_filters": {
                    "type": "integer",
                    "description": (
                        "Number of peaking filters to fit. Default 1 (grid "
                        "search one filter). 2-8 triggers joint Levenberg-"
                        "Marquardt optimization over 3N parameters. Use when "
                        "you want the tool to design a complete PEQ set."
                    ),
                    "default": 1,
                },
                "exclude_geometry": {
                    "type": "boolean",
                    "description": (
                        "Default true. When true, drops 1/3-octave bands "
                        "classified as near-π geometry cancellation by "
                        "analyze_phase from the residuals before fitting, so "
                        "the optimizer doesn't waste filter slots on "
                        "unfixable nulls."
                    ),
                    "default": True,
                },
                "baseline_filters": {
                    "type": "array",
                    "description": (
                        "Optional currently-applied PEQ filter bank. When "
                        "passed, the tool applies these filters to the "
                        "measured FR before fitting so the returned filters "
                        "refine the existing correction instead of replacing "
                        "it. Use with num_filters=1 to add a single targeted "
                        "tweak on top of the current bank."
                    ),
                    "items": {"type": "object"},
                },
            },
            "required": ["session_id", "freq_range"],
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
            target=arguments.get("target"),
            simulation_verified=bool(arguments.get("simulation_verified", False)),
        )
    elif name == "apply_input_eq":
        result = await _tool_apply_input_eq(
            arguments.get("filters", []),
            target_curve=arguments.get("target_curve"),
            target=arguments.get("target"),
            simulation_verified=bool(arguments.get("simulation_verified", False)),
        )
    elif name in ("set_volume", "avr_set_volume", "set_denon_volume"):
        result = await _tool_avr_set_volume(float(arguments["level_db"]))
    elif name == "set_speaker_distances":
        result = await _tool_set_speaker_distances(
            distances=dict(arguments["distances"]),
            n_positions=int(arguments.get("n_positions", 1)),
            commit=bool(arguments.get("commit", False)),
            use_custom=bool(arguments.get("use_custom", False)),
        )
    elif name == "design_avr_fir":
        result = await _tool_design_avr_fir(
            channel_id=str(arguments["channel_id"]),
            target_curve_db=list(arguments["target_curve_db"]),
            cache_key=str(arguments["cache_key"]),
            samplerate_hz=float(arguments.get("samplerate_hz", 48000.0)),
        )
    elif name == "apply_avr_fir":
        result = await _tool_apply_avr_fir(
            host=str(arguments["host"]),
            ady_path=str(arguments["ady_path"]),
            cache_key=str(arguments["cache_key"]),
            channel_ids=arguments.get("channel_ids"),
            distances_override_m=arguments.get("distances_override_m"),
            target_curves=arguments.get("target_curves"),
            samplerates_hz=arguments.get("samplerates_hz"),
            inter_packet_delay_ms=float(arguments.get("inter_packet_delay_ms", 5.0)),
        )
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
    elif name == "fit_shelf_for_target":
        result = await _tool_fit_shelf_for_target(
            session_id=int(arguments["session_id"]),
            target_curve=arguments["target_curve"],
            min_hz=float(arguments.get("min_hz", 20.0)),
            max_hz=float(arguments.get("max_hz", 120.0)),
            shelf_type=str(arguments.get("shelf_type", "low_shelf")),
            freq_bounds=arguments.get("freq_bounds"),
            gain_bounds=arguments.get("gain_bounds"),
            q_bounds=arguments.get("q_bounds"),
        )
    elif name == "start_calibration":
        result = await _tool_start_calibration(
            recipe_name=str(arguments["recipe_name"]),
            target=str(arguments["target"]),
            reset_state=bool(arguments.get("reset_state", True)),
            preserve_eq=bool(arguments.get("preserve_eq", False)),
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
    elif name == "get_signal_graph":
        result = await _tool_get_signal_graph()
    elif name == "resolve_target":
        result = await _tool_resolve_target(str(arguments["target"]))
    elif name == "set_config":
        result = await _tool_set_config(arguments["updates"])
    elif name == "discover_avr":
        result = await _tool_discover_avr()
    elif name == "mute_output":
        result = await _tool_mute_output(
            output_indices=arguments.get("output_indices"),
            target=arguments.get("target"),
        )
    elif name == "unmute_output":
        result = await _tool_unmute_output(
            output_indices=arguments.get("output_indices"),
            target=arguments.get("target"),
        )
    elif name == "end_sweep_session":
        result = await _tool_end_sweep_session()
    elif name == "set_delay":
        result = await _tool_set_delay(
            delay_ms=float(arguments["delay_ms"]),
            output_index=(int(arguments["output_index"]) if "output_index" in arguments else None),
            target=arguments.get("target"),
        )
    elif name == "set_polarity":
        result = await _tool_set_polarity(
            inverted=arguments["inverted"] is True,
            output_index=(int(arguments["output_index"]) if "output_index" in arguments else None),
            target=arguments.get("target"),
        )
    elif name == "get_output_state":
        result = await _tool_get_output_state()
    elif name == "analyze_ir":
        result = await _tool_analyze_ir(
            session_id=int(arguments["session_id"]) if "session_id" in arguments else None,
            search_window_ms=float(arguments.get("search_window_ms", 50.0)),
        )
    elif name == "apply_fir":
        coeffs_arg = arguments.get("coefficients")
        result = await _tool_apply_fir(
            output_index=int(arguments["output_index"]),
            coefficients=([float(c) for c in coeffs_arg] if coeffs_arg is not None else None),
            design_session_id=(
                int(arguments["design_session_id"]) if "design_session_id" in arguments else None
            ),
        )
    elif name == "clear_fir":
        result = await _tool_clear_fir(output_index=int(arguments["output_index"]))
    elif name == "design_corrective_fir":
        result = await _tool_design_corrective_fir(
            session_id=int(arguments["session_id"]),
            target_curve=arguments["target_curve"],
            output_index=int(arguments["output_index"]),
            num_taps=int(arguments.get("num_taps", 1024)),
            focus_hz=arguments.get("focus_hz"),
            return_coefficients=bool(arguments.get("return_coefficients", False)),
        )
    elif name == "reset_dsp_defaults":
        result = await _tool_reset_dsp_defaults(
            dry_run=bool(arguments.get("dry_run", False)),
            preserve_eq=bool(arguments.get("preserve_eq", False)),
        )
    elif name == "set_cal_mode":
        result = await _tool_set_cal_mode(enabled=bool(arguments["enabled"]))
    elif name == "set_master_gain":
        result = await _tool_set_master_gain(gain_db=float(arguments["gain_db"]))
    elif name == "set_output_gain":
        result = await _tool_set_output_gain(
            gain_db=float(arguments["gain_db"]),
            output_index=(int(arguments["output_index"]) if "output_index" in arguments else None),
            target=arguments.get("target"),
        )
    elif name == "configure_matrix":
        result = await _tool_configure_matrix(
            active_input=int(arguments["active_input"]) if "active_input" in arguments else None
        )
    elif name == "set_routing":
        result = await _tool_set_routing(arguments["routing"])
    elif name == "analyze_decay":
        result = await _tool_analyze_decay(
            session_id=int(arguments["session_id"]) if "session_id" in arguments else None,
            t60_threshold_ms=float(arguments.get("t60_threshold_ms", 300.0)),
            freq_min=float(arguments.get("freq_min", 20.0)),
            freq_max=float(arguments.get("freq_max", 200.0)),
        )
    elif name == "recommend_fir_phase":
        result = await _tool_recommend_fir_phase(
            session_id=int(arguments["session_id"]),
            t60_threshold_ms=float(arguments.get("t60_threshold_ms", 500.0)),
            peak_db_threshold=float(arguments.get("peak_db_threshold", 0.0)),
            freq_min=float(arguments.get("freq_min", 20.0)),
            freq_max=float(arguments.get("freq_max", 200.0)),
            mains_distance_budget_ms=float(arguments.get(
                "mains_distance_budget_ms",
                arguments.get("audio_delay_budget_ms", 53.0),  # back-compat alias
            )),
            preringing_ms=float(arguments.get("preringing_ms", 25.0)),
        )
    elif name == "verify_input_eq_effect":
        result = await _tool_verify_input_eq_effect(
            pre_session_id=int(arguments["pre_session_id"]),
            post_session_id=int(arguments["post_session_id"]),
            predicted_filters=list(arguments["predicted_filters"]),
            tolerance_db=float(arguments.get("tolerance_db", 2.0)),
            min_hz=float(arguments.get("min_hz", 20.0)),
            max_hz=float(arguments.get("max_hz", 200.0)),
        )
    elif name == "verify_fir_effect":
        result = await _tool_verify_fir_effect(
            pre_session_id=int(arguments["pre_session_id"]),
            post_session_id=int(arguments["post_session_id"]),
            predicted_effect=arguments["predicted_effect"],
            tolerance_db=float(arguments.get("tolerance_db", 2.0)),
            min_hz=float(arguments.get("min_hz", 20.0)),
            max_hz=float(arguments.get("max_hz", 120.0)),
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
            exclude_geometry=bool(arguments.get("exclude_geometry", True)),
            weight_by_coherence=bool(arguments.get("weight_by_coherence", False)),
        )
    elif name == "anchor_target":
        result = await _tool_anchor_target(
            session_id=int(arguments["session_id"]),
            target_offsets=arguments["target_offsets"],
            band=arguments.get("band"),
            max_boost_db=float(arguments.get("max_boost_db", 6.0)),
            null_threshold_db=float(arguments.get("null_threshold_db", 15.0)),
            port_rolloff_hz=float(arguments.get("port_rolloff_hz", 28.0)),
            exclude_geometry=bool(arguments.get("exclude_geometry", True)),
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
    elif name == "optimize_sub_alignment":
        result = await _tool_optimize_sub_alignment(
            session_ids=[int(x) for x in arguments["session_ids"]],
            target_curve=arguments.get("target_curve"),
            min_hz=float(arguments.get("min_hz", 20.0)),
            max_hz=float(arguments.get("max_hz", 120.0)),
            max_delay_ms=float(arguments.get("max_delay_ms", 30.0)),
            search_polarity=bool(arguments.get("search_polarity", True)),
            gain_search_db=float(arguments.get("gain_search_db", 3.0)),
            priority_band=arguments.get("priority_band"),
        )
    elif name == "sweep_inter_sub_delay":
        result = await _tool_sweep_inter_sub_delay(
            session_ids=[int(x) for x in arguments["session_ids"]],
            sub_polarity=arguments.get("sub_polarity"),
            sub_gain_db=arguments.get("sub_gain_db"),
            base_delays_ms=arguments.get("base_delays_ms"),
            priority_band=arguments.get("priority_band", [28.0, 50.0]),
            sweep_range_ms=float(arguments.get("sweep_range_ms", 2.0)),
            step_ms=float(arguments.get("step_ms", 0.25)),
        )
    elif name == "design_fir":
        result = await _tool_design_fir(
            session_id=int(arguments["session_id"]),
            target_curve=arguments.get("target_curve"),
            num_taps=int(arguments.get("num_taps", 1024)),
            phase_mode=arguments.get("phase_mode", "minimum"),
            freq_focus_hz=arguments.get("freq_focus_hz"),
            return_coefficients=bool(arguments.get("return_coefficients", True)),
            preringing_ms=float(arguments.get("preringing_ms", 25.0)),
        )
    elif name == "design_modal_fir":
        result = await _tool_design_modal_fir(
            session_id=int(arguments["session_id"]),
            intents=arguments.get("intents"),
            target_curve=arguments.get("target_curve"),
            target_t60_ms=float(arguments.get("target_t60_ms", 300.0)),
            peak_action_db=float(arguments.get("peak_action_db", 3.0)),
            short_loud_t60_factor=float(arguments.get("short_loud_t60_factor", 0.5)),
            short_loud_peak_db=float(arguments.get("short_loud_peak_db", 12.0)),
            long_ringy_t60_factor=float(arguments.get("long_ringy_t60_factor", 2.0)),
            anti_pulse_cancel_strength=float(arguments.get("anti_pulse_cancel_strength", 0.6)),
            num_taps=int(arguments.get("num_taps", 4096)),
            max_pre_ring_ms=float(arguments.get("max_pre_ring_ms", 25.0)),
            samplerate=int(arguments.get("samplerate", 48000)),
            return_coefficients=bool(arguments.get("return_coefficients", False)),
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
            target_curve=arguments.get("target_curve"),
            target_offsets=arguments.get("target_offsets"),
            freq_range=arguments["freq_range"],
            constraints=arguments.get("constraints"),
            num_filters=int(arguments.get("num_filters", 1)),
            exclude_geometry=bool(arguments.get("exclude_geometry", True)),
            baseline_filters=arguments.get("baseline_filters"),
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
        global _avr, _dsp, _drivers
        cfg = _config()
        _drivers = load_drivers_from_graph(cfg)
        _dsp = _drivers.default_dsp()
        _avr = _drivers.default_avr()
        for name, drv in _drivers.all():
            await drv.setup()
        log.info(
            "Drivers loaded: %s",
            ", ".join(f"{name}={type(drv).__name__}" for name, drv in _drivers.all()),
        )

        # Rehydrate DSP driver shadow state from last-persisted active_dsp_state.
        # minidspd has no readback — after restart, the hardware retains its
        # flashed filters but the driver's in-memory shadow is empty. Load
        # what was last written so get_output_state / apply_eq baselines are
        # correct instead of claiming everything is zero.
        if hasattr(_dsp, "rehydrate_from_active_state"):
            try:
                import asyncio as _asyncio
                from .storage import SessionStore
                active_state = SessionStore().get_active_dsp()
                # MinidspDriver's rehydrate is sync (hardware holds flashed
                # filters — no reconciliation push needed). CamillaDSPDriver's
                # is async (pushes shadow to the daemon so the pipeline
                # reflects the rehydrated state). Await either shape.
                result = _dsp.rehydrate_from_active_state(active_state)
                if _asyncio.iscoroutine(result):
                    await result
                log.info("DSP shadow rehydrated from %d active_dsp_state keys", len(active_state))
            except Exception as exc:
                log.warning("DSP rehydrate failed (shadow stays empty): %s", exc)

        # Configure DSP input routing if active_input is set
        has_active_input = (
            "active_input" in cfg._data
            or (isinstance(cfg._data.get(cfg.dsp_driver_name), dict)
                and "active_input" in cfg._data[cfg.dsp_driver_name])
            or cfg.minidsp.get("active_input") is not None
        )
        active_input = cfg.active_input if has_active_input else None
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
