"""FastAPI web server — browser-based measurement UI.

Architecture
────────────
The Pi plays the log sweep through the miniDSP while the browser (on the
user's laptop) captures audio via the UMIK mic using the Web Audio API.
The browser sends the raw Float32 PCM to the Pi for deconvolution.

Measurement flow
────────────────
  1. Browser  →  POST /api/measure/start
  2. Pi       ←  {token, sample_rate, sweep_duration, countdown_ms}
  3. Browser      starts getUserMedia recording immediately
  4. Pi           plays sweep after countdown_ms (blocking in bg thread)
  5. Browser      records for sweep_duration + 2 s then stops
  6. Browser  →  POST /api/measure/record  (binary Float32LE body, X-Token header)
  7. Pi           deconvolves sweep + recording → FrequencyResponse
  8. Browser  ←  {session_id, frequencies_hz, spl_dbfs, peak_spl, freq_at_peak}
"""

from __future__ import annotations

import logging
import struct
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from .config import Config, CONFIG_PATH
from .measurement import MeasurementEngine, FrequencyResponse, MeasurementQualityError
from .storage import SessionStore

app = FastAPI(title="avr-calibration")

# token → {sweep_samples, sample_rate, freq_min, freq_max, sweep_duration, label}
_pending_sweeps: dict[str, dict] = {}
_pending_lock = threading.Lock()

COUNTDOWN_MS = 1500   # time browser has to set up recording before sweep plays


# ── HTML page ─────────────────────────────────────────────────────────────────

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AVR Calibration</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #0d0f14; color: #e2e8f0; min-height: 100vh;
      display: flex; flex-direction: column; align-items: center;
      padding: 2rem 1rem;
    }
    h1 { font-size: 1.4rem; font-weight: 600; color: #94a3b8; letter-spacing: .05em;
         text-transform: uppercase; margin-bottom: 2rem; }
    .card {
      background: #1a1f2e; border: 1px solid #2d3748; border-radius: 12px;
      padding: 1.5rem; width: 100%; max-width: 760px; margin-bottom: 1.5rem;
    }
    .card h2 { font-size: .85rem; text-transform: uppercase; letter-spacing: .08em;
               color: #64748b; margin-bottom: 1rem; }
    label { font-size: .875rem; color: #94a3b8; display: block; margin-bottom: .25rem; }
    input[type=text], select {
      width: 100%; padding: .5rem .75rem; background: #0d0f14; border: 1px solid #2d3748;
      border-radius: 6px; color: #e2e8f0; font-size: .9rem; margin-bottom: 1rem;
    }
    input[type=text]:focus, select:focus { outline: none; border-color: #3b82f6; }
    button {
      padding: .6rem 1.4rem; border-radius: 6px; font-size: .9rem; font-weight: 500;
      cursor: pointer; border: none; transition: opacity .15s;
    }
    button:disabled { opacity: .4; cursor: not-allowed; }
    #measureBtn { background: #3b82f6; color: #fff; width: 100%; padding: .75rem; }
    #measureBtn:not(:disabled):hover { opacity: .85; }
    #status {
      margin-top: 1rem; font-size: .875rem; min-height: 1.4em; color: #94a3b8;
      text-align: center;
    }
    #status.error { color: #f87171; }
    #status.ok    { color: #4ade80; }
    .countdown {
      font-size: 2rem; font-weight: 700; color: #3b82f6; text-align: center;
      margin: .5rem 0; display: none;
    }
    canvas { width: 100% !important; }
    table { width: 100%; border-collapse: collapse; font-size: .82rem; }
    th { color: #64748b; font-weight: 500; text-align: left; padding: .4rem .5rem;
         border-bottom: 1px solid #2d3748; }
    td { padding: .4rem .5rem; border-bottom: 1px solid #1a2030; color: #cbd5e1; }
    tr:hover td { background: #1e2535; }
    .peak { color: #38bdf8; }
    .feedback-row { display: flex; gap: .5rem; margin-top: .75rem; }
    .feedback-row input { flex: 1; margin-bottom: 0; }
    .feedback-row select { width: 10rem; margin-bottom: 0; }
    .feedback-row button { background: #334155; color: #cbd5e1; white-space: nowrap; }
    .feedback-row button:hover { background: #475569; }
  </style>
</head>
<body>
  <h1>AVR Calibration</h1>

  <div class="card">
    <h2>Measure</h2>
    <label for="micSelect">Microphone</label>
    <select id="micSelect"><option value="">— loading devices —</option></select>

    <label for="labelInput">Session label (optional)</label>
    <input type="text" id="labelInput" placeholder="e.g. before EQ, with Atmos">

    <button id="measureBtn" onclick="startMeasurement()">Start Measurement</button>

    <div class="countdown" id="countdown"></div>
    <div id="status">Ready. Select your microphone and press Start.</div>
  </div>

  <div class="card" id="plotCard" style="display:none">
    <h2>Frequency Response</h2>
    <canvas id="frPlot"></canvas>
  </div>

  <div class="card" id="feedbackCard" style="display:none">
    <h2>Subjective Feedback</h2>
    <div class="feedback-row">
      <input type="text" id="feedbackText" placeholder="e.g. bass sounded muddy during Fury Road chase">
      <select id="feedbackTag">
        <option value="">no tag</option>
        <option value="movie">movie</option>
        <option value="music">music</option>
        <option value="game">game</option>
      </select>
      <button onclick="submitFeedback()">Add</button>
    </div>
  </div>

  <div class="card">
    <h2>History</h2>
    <table id="histTable">
      <thead>
        <tr><th>#</th><th>Date (UTC)</th><th>Label</th><th>Peak SPL</th><th>Pts</th></tr>
      </thead>
      <tbody id="histBody"></tbody>
    </table>
  </div>

  <script>
  let currentSessionId = null;
  let frChart = null;

  // ── Microphone enumeration ─────────────────────────────────────────────
  async function loadMics() {
    try {
      // Need a temporary permission prompt to get device labels
      const tmp = await navigator.mediaDevices.getUserMedia({ audio: true });
      tmp.getTracks().forEach(t => t.stop());
      const devices = await navigator.mediaDevices.enumerateDevices();
      const mics = devices.filter(d => d.kind === 'audioinput');
      const sel = document.getElementById('micSelect');
      sel.innerHTML = mics.map((m, i) =>
        `<option value="${m.deviceId}">${m.label || 'Microphone ' + (i+1)}</option>`
      ).join('');
    } catch (e) {
      setStatus('Microphone access denied: ' + e.message, 'error');
    }
  }

  // ── Measurement ────────────────────────────────────────────────────────
  async function startMeasurement() {
    const btn = document.getElementById('measureBtn');
    btn.disabled = true;
    setStatus('Contacting Pi…');

    const label = document.getElementById('labelInput').value.trim() || null;
    const micId = document.getElementById('micSelect').value;

    let startResp;
    try {
      const r = await fetch('/api/measure/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ label })
      });
      if (!r.ok) throw new Error(await r.text());
      startResp = await r.json();
    } catch (e) {
      setStatus('Failed to start: ' + e.message, 'error');
      btn.disabled = false;
      return;
    }

    const { token, sample_rate, sweep_duration, countdown_ms } = startResp;
    const totalRecordMs = countdown_ms + (sweep_duration + 2) * 1000;

    setStatus('Setting up microphone…');
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          deviceId: micId ? { exact: micId } : undefined,
          sampleRate: sample_rate,
          channelCount: 1,
          echoCancellation: false,
          noiseSuppression: false,
          autoGainControl: false,
        }
      });
    } catch (e) {
      setStatus('Microphone error: ' + e.message, 'error');
      btn.disabled = false;
      return;
    }

    // Collect samples via ScriptProcessorNode
    const audioCtx = new AudioContext({ sampleRate: sample_rate });
    const source = audioCtx.createMediaStreamSource(stream);
    const bufSize = 4096;
    const processor = audioCtx.createScriptProcessor(bufSize, 1, 1);
    const chunks = [];

    processor.onaudioprocess = (e) => {
      const data = e.inputBuffer.getChannelData(0);
      chunks.push(new Float32Array(data));
    };
    source.connect(processor);
    processor.connect(audioCtx.destination);

    // Countdown display
    const cd = document.getElementById('countdown');
    cd.style.display = 'block';
    const deadline = Date.now() + countdown_ms;

    const tickInterval = setInterval(() => {
      const left = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
      cd.textContent = left > 0 ? left + 's' : '🎵';
      if (Date.now() >= deadline) {
        cd.textContent = '🎵 playing sweep…';
        clearInterval(tickInterval);
      }
    }, 100);

    setStatus('Recording… (sweep plays in ' + (countdown_ms/1000).toFixed(1) + 's)');

    // Wait for total recording duration
    await new Promise(r => setTimeout(r, totalRecordMs));

    // Stop recording
    source.disconnect();
    processor.disconnect();
    stream.getTracks().forEach(t => t.stop());
    audioCtx.close();
    cd.style.display = 'none';
    clearInterval(tickInterval);

    // Concatenate all chunks into one Float32Array
    const totalLen = chunks.reduce((s, c) => s + c.length, 0);
    const merged = new Float32Array(totalLen);
    let offset = 0;
    for (const c of chunks) { merged.set(c, offset); offset += c.length; }

    setStatus('Sending recording to Pi for analysis…');

    let result;
    try {
      const r = await fetch('/api/measure/record', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/octet-stream',
          'X-Token': token,
          'X-Sample-Rate': String(sample_rate),
        },
        body: merged.buffer,
      });
      if (!r.ok) throw new Error(await r.text());
      result = await r.json();
    } catch (e) {
      setStatus('Analysis failed: ' + e.message, 'error');
      btn.disabled = false;
      return;
    }

    currentSessionId = result.session_id;
    setStatus(`Session #${result.session_id} saved. Peak: ${result.peak_spl.toFixed(1)} dBFS at ${result.freq_at_peak.toFixed(0)} Hz`, 'ok');

    renderFR(result.frequencies_hz, result.spl_dbfs);
    document.getElementById('feedbackCard').style.display = '';
    loadHistory();
    btn.disabled = false;
  }

  // ── FR plot ────────────────────────────────────────────────────────────
  function renderFR(freqs, spl) {
    document.getElementById('plotCard').style.display = '';
    const ctx = document.getElementById('frPlot').getContext('2d');
    if (frChart) frChart.destroy();
    frChart = new Chart(ctx, {
      type: 'line',
      data: {
        labels: freqs.map(f => f.toFixed(1)),
        datasets: [{
          label: 'SPL (dBFS)',
          data: spl,
          borderColor: '#3b82f6',
          backgroundColor: 'rgba(59,130,246,.1)',
          borderWidth: 1.5,
          pointRadius: 0,
          tension: 0.3,
          fill: true,
        }]
      },
      options: {
        animation: false,
        responsive: true,
        scales: {
          x: {
            type: 'logarithmic',
            min: freqs[0],
            max: freqs[freqs.length - 1],
            ticks: { color: '#64748b', maxTicksLimit: 8,
              callback: v => v < 1000 ? v+'Hz' : (v/1000)+'kHz' },
            grid: { color: '#1e293b' },
          },
          y: {
            ticks: { color: '#64748b' },
            grid: { color: '#1e293b' },
            title: { display: true, text: 'dBFS', color: '#64748b' },
          }
        },
        plugins: { legend: { display: false } }
      }
    });
  }

  // ── Feedback ───────────────────────────────────────────────────────────
  async function submitFeedback() {
    if (!currentSessionId) return;
    const text = document.getElementById('feedbackText').value.trim();
    if (!text) return;
    const tag = document.getElementById('feedbackTag').value || null;
    await fetch(`/api/feedback/${currentSessionId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, content_tag: tag }),
    });
    document.getElementById('feedbackText').value = '';
  }

  // ── History ────────────────────────────────────────────────────────────
  async function loadHistory() {
    const r = await fetch('/api/sessions');
    if (!r.ok) return;
    const sessions = await r.json();
    const tbody = document.getElementById('histBody');
    tbody.innerHTML = sessions.map(s => {
      const ts = s.timestamp.slice(0,19).replace('T',' ');
      const label = s.label || '—';
      const peak = s.peak_spl.toFixed(1) + ' dBFS';
      return `<tr>
        <td>${s.id}</td><td>${ts}</td><td>${label}</td>
        <td class="peak">${peak}</td><td>${s.n_freqs}</td>
      </tr>`;
    }).join('');
  }

  function setStatus(msg, cls='') {
    const el = document.getElementById('status');
    el.textContent = msg;
    el.className = cls;
  }

  loadMics();
  loadHistory();
  </script>
</body>
</html>
"""


# ── Pydantic models ────────────────────────────────────────────────────────────

class StartRequest(BaseModel):
    label: Optional[str] = None


class FeedbackRequest(BaseModel):
    text: str
    content_tag: Optional[str] = None


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return _HTML


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/api/measure/start")
async def measure_start(body: StartRequest) -> dict:
    """
    Generate the log sweep and schedule playback.

    The Pi waits COUNTDOWN_MS milliseconds before playing so the browser
    has time to set up getUserMedia recording.  Returns the token used to
    match the subsequent /api/measure/record call.
    """
    cfg = _load_config()
    engine = MeasurementEngine(cfg)

    try:
        samples, sample_rate, sweep_duration = engine.generate_sweep()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    token = str(uuid.uuid4())
    with _pending_lock:
        _pending_sweeps[token] = {
            "sweep_samples": samples,
            "sample_rate": sample_rate,
            "sweep_duration": sweep_duration,
            "freq_min": cfg.measurement.get("freq_min", 20),
            "freq_max": cfg.measurement.get("freq_max", 200),
            "label": body.label,
        }

    # Play sweep in background after countdown delay
    def _play():
        time.sleep(COUNTDOWN_MS / 1000.0)
        try:
            engine.play_signal(samples, sample_rate)
        except RuntimeError as exc:
            logger.warning("play_signal failed: %s", exc)

    threading.Thread(target=_play, daemon=True).start()

    return {
        "token": token,
        "sample_rate": sample_rate,
        "sweep_duration": sweep_duration,
        "countdown_ms": COUNTDOWN_MS,
    }


@app.post("/api/measure/record")
async def measure_record(
    request: Request,
    x_token: str = Header(...),
    x_sample_rate: Optional[int] = Header(default=None),
) -> dict:
    """
    Receive binary Float32LE PCM from the browser, deconvolve with the stored
    sweep, persist, and return the frequency response.
    """
    with _pending_lock:
        pending = _pending_sweeps.pop(x_token, None)
    if pending is None:
        raise HTTPException(status_code=404, detail="Unknown token or expired")

    body = await request.body()
    if len(body) < 4:
        raise HTTPException(status_code=400, detail="Recording too short")

    n_samples = len(body) // 4
    recording_samples = list(struct.unpack(f"<{n_samples}f", body[:n_samples * 4]))

    cfg = _load_config()
    engine = MeasurementEngine(cfg)
    sr = x_sample_rate or pending["sample_rate"]

    try:
        fr = engine.compute_fr(
            sweep_samples=pending["sweep_samples"],
            recording_samples=recording_samples,
            freq_min=pending["freq_min"],
            freq_max=pending["freq_max"],
            sample_rate=sr,
        )
    except MeasurementQualityError as exc:
        return JSONResponse(
            status_code=422,
            content={
                "error": "measurement_quality",
                "check": exc.check,
                "detail": exc.detail,
                "suggestion": exc.suggestion,
            },
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    store = SessionStore()
    session_id = store.save_measurement(fr, label=pending["label"])

    return {
        "session_id": session_id,
        "frequencies_hz": fr.frequencies,
        "spl_dbfs": fr.spl,
        "peak_spl": fr.peak_spl,
        "freq_at_peak": fr.freq_at_peak,
        "warnings": fr.warnings,
    }


@app.get("/api/sessions")
async def list_sessions() -> list[dict]:
    """Return all sessions for the history table."""
    store = SessionStore()
    sessions = store.list_sessions()
    return [
        {
            "id": s.id,
            "timestamp": s.timestamp,
            "label": s.label,
            "peak_spl": s.start_fr.peak_spl,
            "freq_at_peak": s.start_fr.freq_at_peak,
            "n_freqs": len(s.start_fr.frequencies),
            "has_end_fr": s.end_fr is not None,
        }
        for s in sessions
    ]


@app.post("/api/feedback/{session_id}")
async def add_feedback(session_id: int, body: FeedbackRequest) -> dict:
    """Add a subjective feedback note to a session."""
    store = SessionStore()
    if store.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail=f"Session #{session_id} not found")
    fid = store.add_feedback(
        session_id=session_id,
        text=body.text,
        content_tag=body.content_tag,
    )
    return {"feedback_id": fid}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_config() -> Config:
    if not CONFIG_PATH.exists():
        raise HTTPException(
            status_code=503,
            detail=f"No config at {CONFIG_PATH}. Run 'calibrate check' first.",
        )
    return Config.load(CONFIG_PATH)
