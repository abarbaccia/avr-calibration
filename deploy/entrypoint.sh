#!/bin/sh
# Entrypoint for the avr-calibration Docker container.
# Starts minidspd (DSP control), MCP server (Claude control plane),
# and uvicorn (web dashboard).
#
# Graceful shutdown: traps SIGTERM/SIGINT and stops child processes in
# reverse order — uvicorn first, then MCP server, then minidspd last.
# minidspd must close its USB HID connection cleanly or the miniDSP
# device can be left with corrupted parameter RAM.

mkdir -p /data/.avr-calibration

# ── Shutdown handler ──────────────────────────────────────────────────────────
# Track child PIDs so the trap can shut them down gracefully.
MINIDSPD_PID=""
MCP_PID=""
UVICORN_PID=""

cleanup() {
    echo "Caught signal — shutting down gracefully..."

    # Stop uvicorn first (no new requests)
    if [ -n "$UVICORN_PID" ] && kill -0 "$UVICORN_PID" 2>/dev/null; then
        echo "Stopping uvicorn (pid $UVICORN_PID)..."
        kill -TERM "$UVICORN_PID" 2>/dev/null
        wait "$UVICORN_PID" 2>/dev/null
    fi

    # Stop MCP server
    if [ -n "$MCP_PID" ] && kill -0 "$MCP_PID" 2>/dev/null; then
        echo "Stopping MCP server (pid $MCP_PID)..."
        kill -TERM "$MCP_PID" 2>/dev/null
        wait "$MCP_PID" 2>/dev/null
    fi

    # Stop minidspd LAST — give it time to close the USB HID connection
    if [ -n "$MINIDSPD_PID" ] && kill -0 "$MINIDSPD_PID" 2>/dev/null; then
        echo "Stopping minidspd (pid $MINIDSPD_PID)..."
        kill -TERM "$MINIDSPD_PID" 2>/dev/null
        # Wait up to 5 seconds for clean USB shutdown
        i=0
        while [ $i -lt 50 ] && kill -0 "$MINIDSPD_PID" 2>/dev/null; do
            sleep 0.1
            i=$((i + 1))
        done
        # Force kill only if still alive after 5s
        if kill -0 "$MINIDSPD_PID" 2>/dev/null; then
            echo "minidspd did not exit cleanly — sending SIGKILL"
            kill -KILL "$MINIDSPD_PID" 2>/dev/null
        else
            echo "minidspd stopped cleanly"
        fi
    fi

    echo "Shutdown complete."
    exit 0
}

trap cleanup TERM INT

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

# ── uvicorn (web dashboard) ──────────────────────────────────────────────────
# Run in background so we can wait on it and forward signals via the trap.
# Using exec would bypass the trap handler.
python -m uvicorn calibrate.web:app \
    --host 0.0.0.0 --port 8000 &
UVICORN_PID=$!

# Wait for uvicorn — if it exits on its own, clean up the others
wait "$UVICORN_PID"
cleanup
