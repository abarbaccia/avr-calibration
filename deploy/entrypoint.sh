#!/bin/sh
# Entrypoint for the avr-calibration Docker container.
# Starts minidspd (DSP control), MCP server (Claude control plane),
# and uvicorn (web dashboard).

mkdir -p /data/.avr-calibration

# ── minidspd ──────────────────────────────────────────────────────────────────
# Start the minidspd HTTP REST daemon so the web server can control the
# miniDSP 2x4HD via localhost:5380.
#
# minidspd (REST daemon) is NOT the same as `minidsp server` (deprecated TCP
# server for the mobile app). minidspd exposes the HTTP REST API that
# MinidspClient uses for gain/delay/PEQ/polarity control.
#
# Requires --privileged (or equivalent device access) on `docker run`.
# Without device access, minidspd starts but reports no devices; DSP control
# operations will fail gracefully via HTTP error in the web server.
MINIDSPD_CONF=/tmp/minidspd.toml
cat > "$MINIDSPD_CONF" << 'TOML'
[http_server]
bind_address = "127.0.0.1:5380"
TOML

echo "Starting minidspd (HTTP REST) on localhost:5380..."
minidspd --config "$MINIDSPD_CONF" >/tmp/minidspd.log 2>&1 &
MINIDSPD_PID=$!
# Give it a moment to bind the port
sleep 2
if kill -0 "$MINIDSPD_PID" 2>/dev/null; then
    echo "minidspd started (pid $MINIDSPD_PID)"
else
    echo "WARNING: minidspd exited — DSP control unavailable. Check /tmp/minidspd.log"
    cat /tmp/minidspd.log >&2
fi

# ── MCP server ────────────────────────────────────────────────────────────────
# Claude Code connects to this via SSE on port 8765.
# Runs in background so uvicorn (foreground) is the main process.
MCP_PORT="${MCP_PORT:-8765}"
echo "Starting MCP server on 0.0.0.0:${MCP_PORT}..."
python -m calibrate.mcp_server >/tmp/mcp-server.log 2>&1 &
MCP_PID=$!
sleep 1
if kill -0 "$MCP_PID" 2>/dev/null; then
    echo "MCP server started (pid $MCP_PID)"
else
    echo "WARNING: MCP server exited — Claude control unavailable. Check /tmp/mcp-server.log"
    cat /tmp/mcp-server.log >&2
fi

exec python -m uvicorn calibrate.web:app \
    --host 0.0.0.0 --port 8000
