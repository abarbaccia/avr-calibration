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
  trigger_measurement    — Pi 4 only; returns degraded-mode error on Pi Zero
  fetch_recipe           — serve recipe markdown from recipes/ directory

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

from .config import Config
from .drivers.avr_driver import AVRDriver
from .drivers.base import DriverError
from .drivers.dsp_driver import DSPDriver
from .drivers.registry import load_avr_driver, load_dsp_driver

log = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────

MCP_PORT: int = int(os.environ.get("MCP_PORT", "8765"))
MCP_HOST: str = os.environ.get("MCP_HOST", "0.0.0.0")

RECIPES_DIR: Path = Path(__file__).parent.parent / "recipes"

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

    Requires Pi 4 with UMIK-1 connected. On Pi Zero 2 W, returns a structured
    degraded-mode error directing the user to the browser.
    """
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        umik_devices = [d for d in devices if "UMIK" in str(d.get("name", ""))]
        if not umik_devices:
            return _err(
                "trigger_measurement requires Pi 4 — no UMIK microphone found. "
                "Take a measurement in the browser and use get_measurement_history() "
                "to retrieve it."
            )
    except Exception:
        return _err(
            "trigger_measurement requires Pi 4 — audio device enumeration failed. "
            "Take a measurement in the browser and use get_measurement_history() "
            "to retrieve it."
        )

    try:
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "http://localhost:8000/api/measure",
                json={"label": "mcp-triggered"},
            )
        if response.status_code == 200:
            data = response.json()
            return _ok(
                session_id=data.get("session_id"),
                message="Measurement complete — use get_measurement_history() to retrieve results.",
            )
        return _err(f"measurement API returned HTTP {response.status_code}")
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
            "Requires Pi 4 4GB (4 USB ports: miniDSP + UMIK-1). "
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

@asynccontextmanager
async def lifespan(app: Starlette):
    """Load drivers on startup; tear them down on shutdown."""
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
    yield
    await _avr.close()
    await _dsp.close()


def create_app() -> Starlette:
    """Build the ASGI application wrapping the MCP server via SSE transport."""
    sse = SseServerTransport("/messages/")

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
