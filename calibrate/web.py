"""Web server for avr-calibration.

Serves the measurement UI — access from any browser on your local network.
Run via: uv run calibrate web
Or directly: uvicorn calibrate.web:app --host 0.0.0.0 --port 8000
"""

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI(title="avr-calibration")


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    """Placeholder — full web UI coming in next release."""
    return """<!doctype html>
<html>
<head><meta charset="utf-8"><title>AVR Calibration</title></head>
<body style="font-family:sans-serif;max-width:480px;margin:80px auto;text-align:center">
  <h2>AVR Calibration</h2>
  <p>Web UI coming soon.</p>
  <p>For now, use the CLI:</p>
  <pre style="text-align:left;background:#f4f4f4;padding:16px;border-radius:6px">
uv run calibrate measure
uv run calibrate history
uv run calibrate show 1</pre>
</body>
</html>"""


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
