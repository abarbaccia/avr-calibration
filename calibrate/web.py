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

Sub-alignment flow
──────────────────
  1. Browser  →  POST /api/align-subs/start
  2. Pi       ←  {token, sample_rate, sweep_duration, countdown_ms, step: 0, n_steps: N}
                  Pi: mutes all subs except first, schedules sweep
  3. Browser  →  POST /api/align-subs/record  (X-Token, X-Step=0, Float32LE body)
  4. Pi       ←  {next_step: 1, ...}  (Pi: mutes sub 0, unmutes sub 1, schedules sweep)
                  ... repeat until step = N-1 ...
  5. Browser  →  POST /api/align-subs/record  (X-Step=N-1, final)
  6. Pi           runs Phase 2-4 (delays, polarity, level), restores gains
  7. Browser  ←  {alignment_summary: {...}}
  8. Browser  →  POST /api/align-subs/cancel  (optional early abort, restores gains)
"""

from __future__ import annotations

import asyncio
import logging
import math
import statistics
import struct
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

import httpx
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from .config import Config, CONFIG_PATH
from .measurement import MeasurementEngine, FrequencyResponse, MeasurementQualityError
from .storage import SessionStore

app = FastAPI(title="avr-calibration")

# token → {sweep_samples, sample_rate, freq_min, freq_max, sweep_duration, label}
_pending_sweeps: dict[str, dict] = {}
_pending_lock = threading.Lock()

# token → AlignmentSession
_pending_alignments: dict[str, "_AlignmentSession"] = {}
_align_lock = threading.Lock()

COUNTDOWN_MS = 1500   # time browser has to set up recording before sweep plays
ALIGNMENT_SESSION_TTL_S = 600   # evict stale alignment sessions after 10 min
ALIGNMENT_CLEANUP_INTERVAL_S = 60  # how often the cleanup thread wakes up


# ── Alignment session state ────────────────────────────────────────────────────

@dataclass
class _AlignmentSession:
    token: str
    created_at: float
    sub_outputs: list[int]
    sweep_samples: list[float]
    sample_rate: int
    sweep_duration: float
    step: int
    ir_results: list = field(default_factory=list)
    minidsp_host: str = "localhost"
    minidsp_port: int = 5380
    ir_search_window_ms: float = 50.0
    complete: bool = False


# ── Background TTL cleanup ─────────────────────────────────────────────────────

def _alignment_cleanup_loop() -> None:
    """Daemon thread — evict expired alignment sessions and restore sub gains."""
    while True:
        time.sleep(ALIGNMENT_CLEANUP_INTERVAL_S)
        now = time.time()
        expired: list[_AlignmentSession] = []
        with _align_lock:
            for token, session in list(_pending_alignments.items()):
                if now - session.created_at > ALIGNMENT_SESSION_TTL_S:
                    expired.append(session)
                    del _pending_alignments[token]
        for session in expired:
            logger.warning(
                "Alignment session %s expired — restoring sub gains", session.token[:8]
            )
            _restore_sub_gains(session)


def _restore_sub_gains(session: _AlignmentSession) -> None:
    """Restore all sub outputs to 0 dB (sync wrapper for async client)."""
    from .adapters.minidsp import MinidspClient

    client = MinidspClient(session.minidsp_host, session.minidsp_port)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(client.restore_all_gains(session.sub_outputs))
    except Exception as exc:
        logger.error("restore_sub_gains failed: %s", exc)
    finally:
        loop.close()


# Start cleanup thread at module load
threading.Thread(target=_alignment_cleanup_loop, daemon=True).start()


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
    tr { cursor: pointer; }
    tr:hover td { background: #1e2535; }
    tr.selected td { background: #1e2535; border-left: 3px solid #3b82f6; }
    .peak { color: #38bdf8; }
    .feedback-row { display: flex; gap: .5rem; margin-top: .75rem; }
    .feedback-row input { flex: 1; margin-bottom: 0; }
    .feedback-row select { width: 10rem; margin-bottom: 0; }
    .feedback-row button { background: #334155; color: #cbd5e1; white-space: nowrap; }
    .feedback-row button:hover { background: #475569; }
    /* Target curve selector */
    .curve-row { display: flex; align-items: center; gap: .75rem; margin-bottom: .75rem; }
    .curve-row label { font-size: .8rem; color: #64748b; margin: 0; white-space: nowrap; }
    .curve-row select { width: auto; margin-bottom: 0; font-size: .8rem; }
    /* Convergence delta table */
    .delta-tbl td { font-size: .8rem; }
    .delta-tbl tr.ok td { color: #4ade80; }
    .delta-tbl tr.warn td { color: #fbbf24; }
    .delta-tbl tr.bad td { color: #f87171; }
    /* Multi-select average */
    .hist-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 1rem; }
    .hist-header h2 { margin-bottom: 0; }
    #avgBtn { background: #334155; color: #cbd5e1; font-size: .8rem; padding: .35rem .8rem; display: none; }
    #avgBtn:not(:disabled):hover { background: #475569; }
    th.cb-col, td.cb-col { width: 2rem; text-align: center; padding: .4rem .25rem; cursor: default; }
    /* Blend check */
    #blendStatus { font-size: .8rem; color: #94a3b8; margin-top: .75rem; min-height: 1.2em; }
    #blendBtn { background: #334155; color: #cbd5e1; font-size: .85rem; margin-top: .75rem; }
    #blendBtn:not(:disabled):hover { background: #475569; }
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

    <label for="posLabel">Seat position (optional)</label>
    <input type="text" id="posLabel" placeholder="e.g. left, center, right">

    <button id="measureBtn" onclick="startMeasurement()">Start Measurement</button>

    <div class="countdown" id="countdown"></div>
    <div id="status">Ready. Select your microphone and press Start.</div>

    <button id="blendBtn" onclick="startBlendCheck()">Check Sub/Sat Blend (40–160 Hz)</button>
    <div id="blendStatus"></div>
  </div>

  <div class="card" id="plotCard" style="display:none">
    <div class="curve-row">
      <label for="curveSelect">Target curve:</label>
      <select id="curveSelect" onchange="onCurveChange()">
        <option value="harman">Harman (+3 dB/oct below 80 Hz)</option>
        <option value="flat">Flat</option>
      </select>
    </div>
    <canvas id="frPlot"></canvas>
    <p id="plotStatus" style="font-size:.8rem;color:#64748b;margin-top:.5rem;text-align:center;"></p>
    <div style="text-align:right;margin-top:.5rem">
      <button id="exportBtn" onclick="exportChart()" style="background:#334155;color:#cbd5e1;font-size:.8rem;padding:.4rem .9rem">Export PNG</button>
    </div>
  </div>

  <div class="card" id="deltaCard" style="display:none">
    <h2>Convergence vs Target</h2>
    <table class="delta-tbl" id="deltaTable">
      <thead><tr><th>Band (Hz)</th><th>SPL</th><th>Target</th><th>Delta</th></tr></thead>
      <tbody id="deltaBody"></tbody>
    </table>
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
    <div class="hist-header">
      <h2>History</h2>
      <button id="avgBtn" onclick="averageSelected()">Average Selected</button>
    </div>
    <table id="histTable">
      <thead>
        <tr><th class="cb-col"></th><th>#</th><th>Date (UTC)</th><th>Label</th><th>Peak SPL</th><th>Pts</th></tr>
      </thead>
      <tbody id="histBody"></tbody>
    </table>
  </div>

  <script>
  let currentSessionId = null;
  let selectedSessionId = null;
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
    const position_label = document.getElementById('posLabel').value.trim() || null;
    const micId = document.getElementById('micSelect').value;

    let startResp;
    try {
      const r = await fetch('/api/measure/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ label, position_label })
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
    selectedSessionId = result.session_id;
    setStatus(`Session #${result.session_id} saved. Peak: ${result.peak_spl.toFixed(1)} dBFS at ${result.freq_at_peak.toFixed(0)} Hz`, 'ok');

    renderFR(result.frequencies_hz, result.spl_dbfs, null, null, `Session #${result.session_id}`);
    history.pushState({ session: result.session_id }, '', `?session=${result.session_id}`);
    document.getElementById('feedbackCard').style.display = '';
    loadHistory();
    btn.disabled = false;
  }

  // ── Target curve ───────────────────────────────────────────────────────
  let targetCurveType = localStorage.getItem('targetCurve') || 'harman';

  function onCurveChange() {
    targetCurveType = document.getElementById('curveSelect').value;
    localStorage.setItem('targetCurve', targetCurveType);
    if (frChart && frChart.data.labels.length) {
      const freqs = frChart.data.labels.map(Number);
      const spl = frChart.data.datasets[0].data;
      renderFR(freqs, spl, null, null, frChart.data.datasets[0].label);
    }
  }

  function getTargetCurve(freqs, spl) {
    const sorted = [...spl].sort((a, b) => a - b);
    const refSpl = sorted[Math.floor(sorted.length / 2)];
    if (targetCurveType === 'flat') return freqs.map(() => refSpl);
    // Harman: flat above 80 Hz, +3 dB/octave below 80 Hz
    return freqs.map(f => f >= 80 ? refSpl : refSpl + 3 * Math.log2(80 / f));
  }

  // ── Convergence delta table ────────────────────────────────────────────
  // 1/3-octave centre frequencies 25–200 Hz (ISO preferred)
  const THIRD_OCT = [25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200];

  function renderDeltaTable(freqs, spl) {
    const target = getTargetCurve(freqs, spl);
    const tbody = document.getElementById('deltaBody');
    if (!tbody) return;
    const rows = THIRD_OCT.map(fc => {
      // find closest measured freq bin
      let bi = 0, bd = Infinity;
      freqs.forEach((f, i) => { const d = Math.abs(f - fc); if (d < bd) { bd = d; bi = i; } });
      const measSpl = spl[bi];
      const targSpl = target[bi];
      const delta = measSpl - targSpl;
      const cls = Math.abs(delta) <= 3 ? 'ok' : Math.abs(delta) <= 6 ? 'warn' : 'bad';
      const sign = delta >= 0 ? '+' : '';
      return `<tr class="${cls}"><td>${fc} Hz</td><td>${measSpl.toFixed(1)}</td><td>${targSpl.toFixed(1)}</td><td>${sign}${delta.toFixed(1)}</td></tr>`;
    });
    tbody.innerHTML = rows.join('');
    document.getElementById('deltaCard').style.display = '';
  }

  // ── FR plot ────────────────────────────────────────────────────────────
  function renderFR(freqs, spl, endFreqs, endSpl, label) {
    if (!freqs || !freqs.length) {
      document.getElementById('plotCard').style.display = '';
      setPlotStatus('No frequency data for this session.');
      return;
    }
    document.getElementById('plotCard').style.display = '';
    setPlotStatus(label || '');
    // Sync curve selector with saved preference
    const sel = document.getElementById('curveSelect');
    if (sel) sel.value = targetCurveType;

    const ctx = document.getElementById('frPlot').getContext('2d');
    if (frChart) frChart.destroy();

    const targetLine = getTargetCurve(freqs, spl);

    const datasets = [
      {
        label: endFreqs ? 'Before EQ' : 'SPL (dBFS)',
        data: spl,
        borderColor: '#3b82f6',
        backgroundColor: 'rgba(59,130,246,.1)',
        borderWidth: 1.5,
        pointRadius: 0,
        tension: 0.3,
        fill: !endFreqs,
      },
      {
        label: targetCurveType === 'harman' ? 'Harman Target' : 'Flat Target',
        data: targetLine,
        borderColor: '#94a3b8',
        borderDash: [5, 5],
        borderWidth: 1,
        pointRadius: 0,
        tension: 0,
        fill: false,
      },
    ];

    renderDeltaTable(freqs, spl);

    if (endFreqs && endFreqs.length) {
      datasets.push({
        label: 'After EQ',
        data: endSpl,
        borderColor: '#4ade80',
        borderWidth: 1.5,
        pointRadius: 0,
        tension: 0.3,
        fill: false,
      });
    }

    frChart = new Chart(ctx, {
      type: 'line',
      data: {
        labels: freqs.map(f => f.toFixed(1)),
        datasets,
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
        plugins: { legend: { labels: { color: '#94a3b8', font: { size: 11 } } } }
      }
    });
  }

  function setPlotStatus(msg) {
    const el = document.getElementById('plotStatus');
    if (el) el.textContent = msg;
  }

  function exportChart() {
    if (!frChart) return;
    const canvas = document.getElementById('frPlot');
    const link = document.createElement('a');
    link.download = `fr-session-${selectedSessionId || 'unknown'}.png`;
    link.href = canvas.toDataURL('image/png');
    link.click();
  }

  async function loadSession(id) {
    selectedSessionId = id;
    currentSessionId = id;
    document.getElementById('feedbackCard').style.display = '';

    // Update URL
    const url = new URL(window.location);
    url.searchParams.set('session', id);
    history.pushState({ session: id }, '', url);

    // Highlight row
    document.querySelectorAll('#histBody tr').forEach(tr => {
      tr.classList.toggle('selected', parseInt(tr.dataset.sessionId) === id);
    });

    try {
      const r = await fetch(`/api/sessions/${id}`);
      if (!r.ok) { setPlotStatus('Failed to load session ' + id); return; }
      const s = await r.json();
      const startFr = s.start_fr;
      const endFr = s.end_fr;
      renderFR(
        startFr ? startFr.frequencies : [],
        startFr ? startFr.spl : [],
        endFr ? endFr.frequencies : null,
        endFr ? endFr.spl : null,
        s.label || `Session #${s.id}`,
      );
    } catch (e) {
      setPlotStatus('Error: ' + e.message);
    }
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
      const sel = s.id === selectedSessionId ? ' selected' : '';
      return `<tr class="${sel}" data-session-id="${s.id}" onclick="loadSession(${s.id})">
        <td class="cb-col" onclick="event.stopPropagation()">
          <input type="checkbox" data-id="${s.id}" onchange="updateAvgButton()">
        </td>
        <td>${s.id}</td><td>${ts}</td><td>${label}</td>
        <td class="peak">${peak}</td><td>${s.n_freqs}</td>
      </tr>`;
    }).join('');
  }

  function updateAvgButton() {
    const checked = document.querySelectorAll('#histBody input[type=checkbox]:checked');
    const btn = document.getElementById('avgBtn');
    btn.style.display = checked.length >= 2 ? '' : 'none';
    btn.textContent = `Average ${checked.length} Sessions`;
  }

  // ── Multi-position spatial average ────────────────────────────────────
  async function averageSelected() {
    const checked = [...document.querySelectorAll('#histBody input[type=checkbox]:checked')];
    const ids = checked.map(cb => parseInt(cb.dataset.id));
    if (ids.length < 2) return;
    setStatus('Averaging sessions…');
    try {
      const r = await fetch('/api/sessions/average', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_ids: ids }),
      });
      if (!r.ok) { setStatus('Average failed: ' + (await r.text()), 'error'); return; }
      const result = await r.json();
      setStatus(`Averaged ${result.n_positions} positions`, 'ok');
      renderFR(result.frequencies_hz, result.spl_dbfs, null, null, `Average of ${result.n_positions} positions`);
    } catch (e) {
      setStatus('Average error: ' + e.message, 'error');
    }
  }

  // ── Sub/satellite blend check ──────────────────────────────────────────
  async function startBlendCheck() {
    const btn = document.getElementById('blendBtn');
    const statusEl = document.getElementById('blendStatus');
    btn.disabled = true;
    statusEl.textContent = 'Starting blend-check sweep…';

    const micId = document.getElementById('micSelect').value;
    let startResp;
    try {
      const r = await fetch('/api/blend-check/start', { method: 'POST',
        headers: { 'Content-Type': 'application/json' }, body: '{}' });
      if (!r.ok) throw new Error(await r.text());
      startResp = await r.json();
    } catch (e) {
      statusEl.textContent = 'Failed: ' + e.message;
      btn.disabled = false; return;
    }

    const { token, sample_rate, sweep_duration, countdown_ms } = startResp;
    const totalRecordMs = countdown_ms + (sweep_duration + 2) * 1000;

    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          deviceId: micId ? { exact: micId } : undefined,
          sampleRate: sample_rate, channelCount: 1,
          echoCancellation: false, noiseSuppression: false, autoGainControl: false,
        }
      });
    } catch (e) {
      statusEl.textContent = 'Mic error: ' + e.message;
      btn.disabled = false; return;
    }

    const audioCtx = new AudioContext({ sampleRate: sample_rate });
    const source = audioCtx.createMediaStreamSource(stream);
    const processor = audioCtx.createScriptProcessor(4096, 1, 1);
    const chunks = [];
    processor.onaudioprocess = (e) => chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
    source.connect(processor); processor.connect(audioCtx.destination);

    statusEl.textContent = `Recording blend check… (sweep in ${(countdown_ms/1000).toFixed(1)}s)`;
    await new Promise(r => setTimeout(r, totalRecordMs));

    source.disconnect(); processor.disconnect();
    stream.getTracks().forEach(t => t.stop()); audioCtx.close();

    const totalLen = chunks.reduce((s, c) => s + c.length, 0);
    const merged = new Float32Array(totalLen);
    let off = 0; for (const c of chunks) { merged.set(c, off); off += c.length; }

    let result;
    try {
      const r = await fetch('/api/measure/record', {
        method: 'POST',
        headers: { 'Content-Type': 'application/octet-stream',
                   'X-Token': token, 'X-Sample-Rate': String(sample_rate) },
        body: merged.buffer,
      });
      if (!r.ok) throw new Error(await r.text());
      result = await r.json();
    } catch (e) {
      statusEl.textContent = 'Analysis failed: ' + e.message;
      btn.disabled = false; return;
    }

    const freqs = result.frequencies_hz;
    const spl = result.spl_dbfs;
    // Score: how close is the crossover region (40–160 Hz) to target?
    const target = getTargetCurve(freqs, spl);
    const xLo = freqs.findIndex(f => f >= 40);
    let xHi = freqs.findIndex(f => f > 160);
    const xSlice = spl.slice(xLo, xHi === -1 ? undefined : xHi);
    const tSlice = target.slice(xLo, xHi === -1 ? undefined : xHi);
    const rms = Math.sqrt(xSlice.reduce((s, v, i) => s + (v - tSlice[i]) ** 2, 0) / xSlice.length);
    const grade = rms <= 3 ? 'Good' : rms <= 6 ? 'Fair' : 'Poor';
    statusEl.textContent = `Blend: ${grade} — RMS deviation ${rms.toFixed(1)} dB vs target (40–160 Hz)`;

    renderFR(freqs, spl, null, null, 'Blend check (40–160 Hz)');
    btn.disabled = false;
  }

  function setStatus(msg, cls='') {
    const el = document.getElementById('status');
    el.textContent = msg;
    el.className = cls;
  }

  loadMics();
  loadHistory().then(() => {
    const urlParams = new URLSearchParams(window.location.search);
    const urlSession = urlParams.get('session');
    if (urlSession) loadSession(parseInt(urlSession, 10));
  });

  window.addEventListener('popstate', (e) => {
    const id = e.state && e.state.session;
    if (id) loadSession(id);
  });
  </script>
</body>
</html>
"""


# ── Pydantic models ────────────────────────────────────────────────────────────

class StartRequest(BaseModel):
    label: Optional[str] = None
    position_label: Optional[str] = None


class AverageRequest(BaseModel):
    session_ids: list[int] = Field(..., min_length=2, max_length=20)


class FeedbackRequest(BaseModel):
    text: str
    content_tag: Optional[str] = None


class AlignSubsStartRequest(BaseModel):
    pass  # no body fields for now; config drives sub_outputs


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
    # Combine label + position_label for storage
    combined_label = body.label
    if body.position_label:
        combined_label = f"{body.label} [{body.position_label}]" if body.label else body.position_label
    with _pending_lock:
        _pending_sweeps[token] = {
            "sweep_samples": samples,
            "sample_rate": sample_rate,
            "sweep_duration": sweep_duration,
            "freq_min": cfg.measurement.get("freq_min", 20),
            "freq_max": cfg.measurement.get("freq_max", 200),
            "label": combined_label,
        }

    # Play sweep in background after countdown delay
    def _play():
        time.sleep(COUNTDOWN_MS / 1000.0)
        try:
            engine.play_signal(samples, sample_rate)
        except Exception as exc:
            logger.warning("play_signal failed (%s): %s", type(exc).__name__, exc)

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

    # Blend-check sweeps are ephemeral — don't persist to store
    if pending.get("session_type") == "blend_check":
        return {
            "session_id": None,
            "frequencies_hz": fr.frequencies,
            "spl_dbfs": fr.spl,
            "peak_spl": fr.peak_spl,
            "freq_at_peak": fr.freq_at_peak,
            "warnings": fr.warnings,
        }

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


@app.post("/api/blend-check/start")
async def blend_check_start() -> dict:
    """Generate a 40–160 Hz sweep for sub/sat crossover coherence checking.

    The resulting token is marked session_type='blend_check' so measure_record
    skips persisting it to the session store.
    """
    cfg = _load_config()
    engine = MeasurementEngine(cfg)

    try:
        samples, sample_rate, sweep_duration = engine.generate_sweep(
            freq_min=40, freq_max=160
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    token = str(uuid.uuid4())
    with _pending_lock:
        _pending_sweeps[token] = {
            "sweep_samples": samples,
            "sample_rate": sample_rate,
            "sweep_duration": sweep_duration,
            "freq_min": 40,
            "freq_max": 160,
            "label": None,
            "session_type": "blend_check",
        }

    def _play():
        time.sleep(COUNTDOWN_MS / 1000.0)
        try:
            engine.play_signal(samples, sample_rate)
        except Exception as exc:
            logger.warning("blend-check play_signal failed: %s", exc)

    threading.Thread(target=_play, daemon=True).start()

    return {
        "token": token,
        "sample_rate": sample_rate,
        "sweep_duration": sweep_duration,
        "countdown_ms": COUNTDOWN_MS,
    }


@app.post("/api/sessions/average")
async def average_sessions(body: AverageRequest) -> dict:
    """Average multiple sessions in the linear pressure domain.

    Converts SPL to linear amplitude (10^(spl/20)), averages across positions,
    then converts back.  Requires all sessions to share the same frequency array.
    """
    store = SessionStore()
    frs: list[FrequencyResponse] = []
    for sid in body.session_ids:
        session = store.get_session(sid)
        if session is None:
            raise HTTPException(status_code=404, detail=f"Session #{sid} not found")
        fr = session.start_fr
        if not fr or not fr.frequencies:
            continue  # skip sessions with empty FR (sentinel)
        frs.append(fr)

    if len(frs) < 2:
        raise HTTPException(
            status_code=422,
            detail="Fewer than 2 sessions have valid frequency response data",
        )

    ref_freqs = frs[0].frequencies
    for fr in frs[1:]:
        if len(fr.frequencies) != len(ref_freqs):
            raise HTTPException(
                status_code=422,
                detail="Sessions have different frequency array lengths — cannot average",
            )
        if any(abs(f - r) > 0.5 for f, r in zip(fr.frequencies, ref_freqs)):
            raise HTTPException(
                status_code=422,
                detail="Sessions have incompatible frequency ranges — cannot average",
            )

    n = len(ref_freqs)
    averaged_spl = []
    for i in range(n):
        linear_sum = sum(10 ** (fr.spl[i] / 20.0) for fr in frs)
        result = linear_sum / len(frs)
        averaged_spl.append(20 * math.log10(result) if result > 0 else -120.0)

    return {
        "frequencies_hz": ref_freqs,
        "spl_dbfs": averaged_spl,
        "n_positions": len(frs),
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


@app.get("/api/sessions/{session_id}")
async def get_session_detail(session_id: int) -> dict:
    """Return full frequency response data for a single session."""
    store = SessionStore()
    session = store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session #{session_id} not found")
    logger.info("session %d fetched", session_id)

    def _fr_dict(fr: FrequencyResponse | None) -> dict | None:
        if fr is None or not fr.frequencies:
            return None
        return {"frequencies": fr.frequencies, "spl": fr.spl}

    return {
        "id": session.id,
        "label": session.label,
        "timestamp": session.timestamp,
        "start_fr": _fr_dict(session.start_fr),
        "end_fr": _fr_dict(session.end_fr),
    }


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


# ── Sub-alignment endpoints ───────────────────────────────────────────────────

@app.post("/api/align-subs/start")
async def align_subs_start() -> dict:
    """Start a multi-sub alignment session.

    1. Reads sub_outputs from config (list of miniDSP output indices).
    2. Generates a log sweep.
    3. Mutes all sub outputs except the first.
    4. Schedules sweep playback after COUNTDOWN_MS.
    5. Returns token + session metadata for the browser to begin recording.
    """
    from .adapters.minidsp import MinidspClient, MinidspApiError

    cfg = _load_config()
    sub_outputs: list[int] = cfg.measurement.get("sub_outputs", [])
    if not sub_outputs:
        raise HTTPException(
            status_code=422,
            detail="measurement.sub_outputs not configured — add sub output indices to config.yaml",
        )

    engine = MeasurementEngine(cfg)
    try:
        samples, sample_rate, sweep_duration = engine.generate_sweep()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    minidsp_host = cfg.minidsp.get("host", "localhost")
    minidsp_port = cfg.minidsp.get("port", 5380)
    ir_search_window_ms = cfg.measurement.get("ir_search_window_ms", 50.0)

    # Mute all sub outputs except the first before scheduling the sweep
    client = MinidspClient(minidsp_host, minidsp_port)
    from .alignment import MUTE_GAIN_DB

    async def _mute_others() -> None:
        for output_idx in sub_outputs[1:]:
            await client.set_output_gain(output_idx, MUTE_GAIN_DB)

    try:
        await _mute_others()
    except httpx.ConnectError as exc:
        raise HTTPException(status_code=503, detail=f"Cannot reach minidspd: {exc}")
    except MinidspApiError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    token = str(uuid.uuid4())
    session = _AlignmentSession(
        token=token,
        created_at=time.time(),
        sub_outputs=sub_outputs,
        sweep_samples=samples,
        sample_rate=sample_rate,
        sweep_duration=sweep_duration,
        step=0,
        minidsp_host=minidsp_host,
        minidsp_port=minidsp_port,
        ir_search_window_ms=ir_search_window_ms,
    )
    with _align_lock:
        _pending_alignments[token] = session

    def _play():
        time.sleep(COUNTDOWN_MS / 1000.0)
        try:
            engine.play_signal(samples, sample_rate)
        except Exception as exc:
            logger.warning("align-subs play_signal failed: %s", exc)

    threading.Thread(target=_play, daemon=True).start()

    return {
        "token": token,
        "sample_rate": sample_rate,
        "sweep_duration": sweep_duration,
        "countdown_ms": COUNTDOWN_MS,
        "step": 0,
        "n_steps": len(sub_outputs),
    }


@app.post("/api/align-subs/record")
async def align_subs_record(
    request: Request,
    x_token: str = Header(...),
    x_step: int = Header(...),
    x_sample_rate: Optional[int] = Header(default=None),
) -> JSONResponse:
    """Receive a recording for the current sub step.

    For steps 0..N-2: extract the IR, advance to the next sub (mute current,
    unmute next, schedule sweep), and return next_step metadata.

    For step N-1 (final): extract IR, run Phases 2-4, restore gains, and
    return the alignment summary.
    """
    import httpx as _httpx
    from .adapters.minidsp import MinidspClient, MinidspApiError
    from .alignment import measure_sub_ir, run_alignment_phases, MUTE_GAIN_DB

    with _align_lock:
        session = _pending_alignments.get(x_token)
    if session is None:
        raise HTTPException(status_code=404, detail="Unknown alignment token or expired")

    body = await request.body()
    if len(body) < 4:
        raise HTTPException(status_code=400, detail="Recording too short")

    n_samples = len(body) // 4
    recording_samples = list(struct.unpack(f"<{n_samples}f", body[:n_samples * 4]))

    cfg = _load_config()
    engine = MeasurementEngine(cfg)
    sr = x_sample_rate or session.sample_rate
    sub_index = x_step  # step N corresponds to sub_outputs[N]

    # Extract IR for this sub
    try:
        ir_result = await measure_sub_ir(
            engine=engine,
            recording_samples=recording_samples,
            sweep_samples=session.sweep_samples,
            sample_rate=sr,
            sub_index=sub_index,
            ir_search_window_ms=session.ir_search_window_ms,
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

    with _align_lock:
        session.ir_results.append(ir_result)
        session.step = x_step + 1

    n_steps = len(session.sub_outputs)
    is_final = (x_step + 1 >= n_steps)

    client = MinidspClient(session.minidsp_host, session.minidsp_port)

    if not is_final:
        # Advance to next sub: mute current, restore next, schedule sweep
        current_output = session.sub_outputs[x_step]
        next_output = session.sub_outputs[x_step + 1]

        async def _advance_subs() -> None:
            await client.set_output_gain(current_output, MUTE_GAIN_DB)
            await client.set_output_gain(next_output, 0.0)

        try:
            await _advance_subs()
        except Exception as exc:
            logger.warning("align-subs advance_subs failed: %s", exc)

        def _play_next() -> None:
            time.sleep(COUNTDOWN_MS / 1000.0)
            try:
                engine.play_signal(session.sweep_samples, session.sample_rate)
            except Exception as exc:
                logger.warning("align-subs play_signal (next) failed: %s", exc)

        threading.Thread(target=_play_next, daemon=True).start()

        return JSONResponse(content={
            "token": session.token,
            "next_step": x_step + 1,
            "n_steps": n_steps,
            "sample_rate": session.sample_rate,
            "sweep_duration": session.sweep_duration,
            "countdown_ms": COUNTDOWN_MS,
        })

    # ── Final step: run Phases 2-4 and restore gains ─────────────────────────
    try:
        summary = await run_alignment_phases(
            ir_results=session.ir_results,
            sub_outputs=session.sub_outputs,
            client=client,
        )
    finally:
        # Always restore gains — even if phases partially fail
        await client.restore_all_gains(session.sub_outputs)
        with _align_lock:
            session.complete = True
            _pending_alignments.pop(session.token, None)

    return JSONResponse(content={
        "alignment_summary": {
            "sub_results": [
                {
                    "sub_index": r.sub_index,
                    "peak_time_s": r.peak_time_s,
                    "peak_sign": r.peak_sign,
                    "polarity_inverted": r.polarity_inverted,
                    "spl_db": r.spl_db,
                }
                for r in summary.sub_results
            ],
            "delay_offsets_ms": summary.delay_offsets_ms,
            "gain_trims_db": summary.gain_trims_db,
        }
    })


@app.post("/api/align-subs/cancel")
async def align_subs_cancel(x_token: str = Header(...)) -> dict:
    """Cancel an in-progress alignment session and restore all sub gains."""
    from .adapters.minidsp import MinidspClient

    with _align_lock:
        session = _pending_alignments.pop(x_token, None)
    if session is None:
        raise HTTPException(status_code=404, detail="Unknown alignment token or expired")

    client = MinidspClient(session.minidsp_host, session.minidsp_port)
    await client.restore_all_gains(session.sub_outputs)

    return {"status": "cancelled"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_config() -> Config:
    if not CONFIG_PATH.exists():
        raise HTTPException(
            status_code=503,
            detail=f"No config at {CONFIG_PATH}. Run 'calibrate check' first.",
        )
    return Config.load(CONFIG_PATH)
