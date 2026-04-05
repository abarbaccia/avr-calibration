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
  avr_set_volume         — generic AVR volume control
  set_denon_volume       — DEPRECATED: alias for avr_set_volume
  trigger_measurement    — Pi 5 only; returns degraded-mode error on Pi Zero
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


# ── Tool implementations ───────────────────────────────────────────────────────

async def _tool_get_device_state() -> dict:
    """Return current AVR + DSP hardware state."""
    avr_result: dict = {"connected": False}
    try:
        avr_result = await _avr.get_state()  # type: ignore[union-attr]
    except DriverError as exc:
        avr_result = {"connected": False, "error": str(exc)}
    except Exception as exc:
        avr_result = {"connected": False, "error": str(exc)}

    dsp_result: dict = {"connected": False}
    try:
        dsp_result = await _dsp.get_state()  # type: ignore[union-attr]
    except DriverError as exc:
        dsp_result = {"connected": False, "error": str(exc)}
    except Exception as exc:
        dsp_result = {"connected": False, "error": str(exc)}

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
        mic_name = _config().mic.get("name", "UMIK")
        if _find_umik_device(devices, name_substring=mic_name) is None:
            return _err(
                "trigger_measurement requires UMIK microphone — none found. "
                "Check USB connection."
            )
    except Exception:
        return _err(
            "trigger_measurement requires sounddevice — audio device enumeration failed."
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


async def _tool_run_calibration_loop(
    recipe_name: str = "harman-bass",
    preset: int = 0,
    fresh: bool = True,
) -> dict:
    """Run the full calibration loop: measure → analyze → apply EQ → re-measure → converge.

    Returns per-iteration results with RMS deviation tracking.
    """
    from .loop import LoopOrchestrator, LoopError
    from .measurement import MeasurementEngine
    from .recipe import load_recipe, RecipeError
    from .storage import SessionStore

    try:
        recipe = load_recipe(recipe_name)
    except RecipeError as exc:
        return _err(f"recipe error: {exc}")

    cfg = _config()

    try:
        engine = MeasurementEngine(cfg)
    except Exception as exc:
        return _err(f"measurement engine init failed: {exc}")

    try:
        store = SessionStore()
    except Exception:
        store = None  # non-critical, loop runs without persistence

    # Wrap measurement with DenonSweepContext if HDMI route configured
    denon_ctx = DenonSweepContext.from_config(cfg)

    async def _measure_with_denon() -> "FrequencyResponse":
        from .measurement import FrequencyResponse as _FR
        if denon_ctx:
            async with denon_ctx:
                return await engine.measure()
        return await engine.measure()

    orchestrator = LoopOrchestrator(
        minidsp=_dsp,  # type: ignore[arg-type]
        measurement_engine=None,  # use measure_fn instead
        store=store,
    )

    try:
        result = await orchestrator.run(recipe, preset=preset, fresh=fresh, measure_fn=_measure_with_denon)
    except LoopError as exc:
        return _err(f"loop error: {exc}")
    except Exception as exc:
        return _err(f"unexpected error: {exc}")

    iterations = []
    for ir in result.iteration_results:
        iterations.append({
            "iteration": ir.iteration,
            "rms_before": round(ir.rms_before, 2),
            "rms_after": round(ir.rms_after, 2),
            "filters_proposed": len(ir.filters_proposed),
            "filters_applied": len(ir.filters_applied),
            "safety_ok": ir.safety_ok,
            "safety_error": ir.safety_error or None,
        })

    return _ok(
        converged=result.converged,
        iterations_run=result.iterations_run,
        baseline_rms=round(result.baseline_rms, 2),
        final_rms=round(result.final_rms, 2),
        iterations=iterations,
        error=result.error or None,
    )


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
        name="avr_set_volume",
        description=(
            "Set the AVR volume to a specific level in dB. "
            "Range: approximately -80 to +18 dB for the Denon X3800H. "
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
        name="set_denon_volume",
        description=(
            "Deprecated: use avr_set_volume instead. "
            "Set the AVR volume to a specific level in dB. "
            "This alias is preserved for backwards compatibility with cached Claude Code sessions."
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
        name="trigger_measurement",
        description=(
            "Trigger a frequency response measurement using the UMIK-1 microphone. "
            "Requires Pi 5 (4 USB ports: miniDSP + UMIK-1). "
            "On Pi Zero 2 W, returns a structured error directing you to use the browser "
            "and then call get_measurement_history() to retrieve results."
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
        name="run_calibration_loop",
        description=(
            "Run the full closed-loop calibration: measure room response, "
            "analyze vs Harman target, propose EQ corrections, apply to miniDSP, "
            "re-measure, and iterate until converged (RMS deviation ≤ threshold). "
            "SafetyValidator guards every write. Rolls back on fatal error. "
            "Returns per-iteration RMS tracking and convergence status. "
            "This is a long-running operation (minutes, not seconds)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "recipe_name": {
                    "type": "string",
                    "description": "Recipe name to load (default: 'harman-bass'). Defines target curve, convergence threshold, max iterations.",
                    "default": "harman-bass",
                },
                "preset": {
                    "type": "integer",
                    "description": "miniDSP preset index to operate on (default: 0)",
                    "default": 0,
                },
                "fresh": {
                    "type": "boolean",
                    "description": "If true, start from empty EQ state. If false, require existing EQ snapshot for rollback (default: true).",
                    "default": True,
                },
            },
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
    elif name in ("avr_set_volume", "set_denon_volume"):
        result = await _tool_avr_set_volume(float(arguments["level_db"]))
    elif name == "trigger_measurement":
        result = await _tool_trigger_measurement()
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
    elif name == "run_calibration_loop":
        result = await _tool_run_calibration_loop(
            recipe_name=arguments.get("recipe_name", "harman-bass"),
            preset=int(arguments.get("preset", 0)),
            fresh=bool(arguments.get("fresh", True)),
        )
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
