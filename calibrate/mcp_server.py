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
  get_measurement_history — last N measurements from SessionStore
  read_eq                — current EQ state (in-memory, updated by apply_eq)
  apply_eq               — SafetyValidator → biquad conversion → DSP write
  set_volume             — AVR volume control
  measure                — trigger sweep measurement via UMIK + PyTTa
  mute_output            — mute DSP outputs (gain → -127 dB)
  unmute_output          — unmute DSP outputs (gain → 0 dB)
  set_delay              — set output delay in ms
  set_polarity           — set output polarity (normal/inverted)
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


# ── Tool implementations ───────────────────────────────────────────────────────

async def _tool_get_device_state() -> dict:
    """Return current AVR + DSP hardware state."""
    avr_result = await _safe_driver_state(_avr)
    dsp_result = await _safe_driver_state(_dsp)
    return _ok(avr=avr_result, dsp=dsp_result)


async def _tool_get_measurement_history(limit: int = 10) -> dict:
    """Return last *limit* measurement sessions from SessionStore."""
    from .storage import SessionStore
    try:
        store = SessionStore()
        sessions = store.list_sessions()[:limit]
        result = []
        for s in sessions:
            fr = s.start_fr
            result.append({
                "id": s.id,
                "timestamp": s.timestamp,
                "label": s.label,
                "freq_hz": fr.frequencies if fr else [],
                "spl_db": fr.spl if fr else [],
            })
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


async def _tool_apply_eq(filters: list[dict]) -> dict:
    """Validate and apply EQ filters to the DSP.

    Delegates validation (SafetyValidator), biquad conversion, hardware write,
    and state tracking to MinidspDriver.apply_eq — all under an asyncio lock.

    Returns {ok: True} or {ok: False, error: "SafetyValidator: ..."} on rejection.
    """
    try:
        preset = await _dsp.current_preset()  # type: ignore[union-attr]
        await _dsp.apply_eq(preset, filters)  # type: ignore[union-attr]
        return _ok(filters_applied=len(filters), preset=preset)
    except DriverError as exc:
        return _err(str(exc))


async def _tool_avr_set_volume(level_db: float) -> dict:
    """Set AVR volume to *level_db* dB."""
    try:
        confirmed_db = await _avr.set_volume(level_db)  # type: ignore[union-attr]
        return _ok(level_db=confirmed_db)
    except DriverError as exc:
        return _err(f"avr unreachable: {exc}")


async def _tool_trigger_measurement() -> dict:
    """Trigger a measurement via UMIK-1 + PyTTa.

    Calls MeasurementEngine.measure() directly (no HTTP hop). Wraps with
    DenonSweepContext when HDMI route is configured.
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
        from .measurement import MeasurementEngine
        from .storage import SessionStore

        cfg = _config()
        engine = MeasurementEngine(cfg)
        denon_ctx = DenonSweepContext.from_config(cfg)

        if denon_ctx:
            async with denon_ctx:
                fr = await engine.measure()
        else:
            fr = await engine.measure()

        store = SessionStore()
        session_id = store.save_measurement(fr, label="mcp-triggered")
        return _ok(
            session_id=session_id,
            message="Measurement complete — use get_measurement_history() to retrieve results.",
        )
    except Exception as exc:
        return _err(f"measurement failed: {exc}")


async def _tool_calibrate_level(
    start_db: float = -10.0,
    max_volume_db: float = 0.0,
    target_snr_db: float = 20.0,
    step_db: float = 3.0,
) -> dict:
    """Auto-ramp AVR volume until measurement SNR meets threshold.

    Starts at start_db, takes a test sweep, checks SNR. If too low, bumps
    volume by step_db and retries. Stops when SNR >= target or ceiling hit.
    Saves calibrated volume to config for subsequent measure calls.
    """
    if _avr is None:
        return _err("AVR driver not loaded")

    from .measurement import MeasurementEngine, MeasurementQualityError
    from .config import update_config

    cfg = _config()
    engine = MeasurementEngine(cfg)
    volume = start_db

    # Use DenonSweepContext with manage_volume=False — it handles input switching,
    # Pure Direct, and state restore; we control volume ourselves in the ramp loop.
    denon_ctx = DenonSweepContext.from_config(cfg, manage_volume=False)

    async def _ramp_loop() -> dict:
        nonlocal volume
        while volume <= max_volume_db:
            try:
                await _avr.set_volume(volume)  # type: ignore[union-attr]
                await asyncio.sleep(0.5)

                fr = await engine.measure()

                # SNR passed — save calibrated volume
                update_config({"measurement": {"denon_sweep_volume": volume}})
                return _ok(
                    calibrated_volume_db=volume,
                    message=f"Level calibrated at {volume} dB. SNR is good.",
                )

            except MeasurementQualityError as exc:
                if exc.check in ("snr", "sweep_capture"):
                    log.info(
                        "calibrate_level: %s at %.1f dB, ramping up",
                        exc.detail, volume,
                    )
                    volume += step_db
                    continue
                else:
                    return _err(f"measurement quality error: {exc.detail}")
            except Exception as exc:
                return _err(f"calibrate_level failed: {exc}")

        return _err(
            f"Could not achieve SNR >= {target_snr_db} dB even at {max_volume_db} dB. "
            "Check that subs are powered on and signal path is correct."
        )

    if denon_ctx:
        async with denon_ctx:
            return await _ramp_loop()
    return await _ramp_loop()


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
    """Return the current config.yaml as a dict."""
    try:
        cfg = _config()
        return _ok(config=cfg._data)
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
        return _ok(output_index=output_index, delay_ms=delay_ms)
    except DriverError as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err(f"set_delay error: {exc}")


async def _tool_set_polarity(output_index: int, inverted: bool) -> dict:
    """Set polarity for a single DSP output (inverted=True flips phase)."""
    try:
        await _dsp.set_output_polarity(output_index, inverted)  # type: ignore[union-attr]
        return _ok(output_index=output_index, inverted=inverted)
    except DriverError as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err(f"set_polarity error: {exc}")


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

# Text appended to any tool description that writes signal-path state (routing,
# source selection, preset switching). Forces human-in-the-loop before the AI
# proceeds. EQ and volume are intentionally excluded — they are calibration
# outputs, not hardware configuration.
_SIGNAL_PATH_WRITE_WARNING = (
    " REQUIRES HUMAN APPROVAL: before calling this tool, describe exactly what "
    "you intend to change and why, then wait for the user to explicitly confirm. "
    "Do not call this tool autonomously."
)

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
            "local database. Each session includes frequency response data "
            "(freq_hz[], spl_db[]), timestamp, and label."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Number of sessions to return (default: 10)",
                    "default": 10,
                }
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
        name="apply_eq",
        description=(
            "Apply EQ filters to the DSP subwoofer outputs. "
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
                }
            },
            "required": ["filters"],
        },
    ),
    Tool(
        name="calibrate_level",
        description=(
            "Auto-calibrate sweep volume. Ramps AVR volume from start_db toward "
            "max_volume_db in step_db increments until measurement SNR >= target_snr_db. "
            "Saves calibrated volume to config. Call before calibration to find the "
            "right sweep level. Returns {ok: true, calibrated_volume_db: N}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "start_db": {
                    "type": "number",
                    "description": "Starting volume in dB (default: -10)",
                },
                "max_volume_db": {
                    "type": "number",
                    "description": "Maximum volume ceiling in dB (default: 0 = reference)",
                },
                "target_snr_db": {
                    "type": "number",
                    "description": "Minimum acceptable SNR in dB (default: 20)",
                },
                "step_db": {
                    "type": "number",
                    "description": "Volume increment per retry in dB (default: 3)",
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
            "and returns the session ID. Use get_measurement_history() to retrieve FR data."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
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
            + _SIGNAL_PATH_WRITE_WARNING
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
            + _SIGNAL_PATH_WRITE_WARNING
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
            + _SIGNAL_PATH_WRITE_WARNING
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
        result = await _tool_get_measurement_history(
            limit=int(arguments.get("limit", 10))
        )
    elif name == "read_eq":
        result = await _tool_read_eq()
    elif name == "apply_eq":
        result = await _tool_apply_eq(arguments.get("filters", []))
    elif name in ("set_volume", "avr_set_volume", "set_denon_volume"):
        result = await _tool_avr_set_volume(float(arguments["level_db"]))
    elif name in ("measure", "trigger_measurement"):
        result = await _tool_trigger_measurement()
    elif name == "calibrate_level":
        result = await _tool_calibrate_level(
            start_db=float(arguments.get("start_db", -10.0)),
            max_volume_db=float(arguments.get("max_volume_db", 0.0)),
            target_snr_db=float(arguments.get("target_snr_db", 20.0)),
            step_db=float(arguments.get("step_db", 3.0)),
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
    elif name == "check_system":
        result = await _tool_check_system()
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
        if active_input is not None and hasattr(_dsp, "configure_active_input"):
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
