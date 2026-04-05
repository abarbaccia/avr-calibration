"""FastAPI web server — read-only calibration dashboard.

Shows system status, measurement history, calibration runs, and FR plots.
Headless measurement via POST /api/measure (Pi 5 with UMIK-1).
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import statistics
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .config import Config, CONFIG_PATH
from .measurement import MeasurementEngine, FrequencyResponse, _find_umik_device
from .storage import SessionStore

app = FastAPI(title="avr-calibration")

# ── Constants ─────────────────────────────────────────────────────────────────

_GHCR_REGISTRY = "ghcr.io"
_GHCR_IMAGE = "abarbaccia/avr-calibration"
_VERSION_CACHE_TTL = 3600
_version_cache: dict = {}
_DATA_DIR = Path(os.environ.get("HOME", "/data")) / ".avr-calibration"

# Headless measurement lock — prevents concurrent /api/measure calls from racing
# on sd.default.device (a global PortAudio setting).
_measurement_lock = asyncio.Lock()

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
         text-transform: uppercase; margin-bottom: 2rem; padding-right: 8rem; }
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
    #status {
      margin-top: 1rem; font-size: .875rem; min-height: 1.4em; color: #94a3b8;
      text-align: center;
    }
    #status.error { color: #f87171; }
    #status.ok    { color: #4ade80; }
    canvas { width: 100% !important; }
    table { width: 100%; border-collapse: collapse; font-size: .82rem; }
    th { color: #64748b; font-weight: 500; text-align: left; padding: .4rem .5rem;
         border-bottom: 1px solid #2d3748; }
    td { padding: .4rem .5rem; border-bottom: 1px solid #1a2030; color: #cbd5e1; }
    tr { cursor: pointer; }
    tr:hover td { background: #1e2535; }
    tr.selected td { background: #1e2535; border-left: 3px solid #3b82f6; }
    .peak { color: #38bdf8; }
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
    /* Advisory badges */
    .badge { display: inline-block; padding: .25rem .7rem; border-radius: 4px; font-size: .8rem;
             font-weight: 600; margin-bottom: .5rem; }
    .badge-optimal { background: rgba(34,197,94,.15); color: #4ade80; border: 1px solid #22c55e; }
    .badge-warn    { background: rgba(245,158,11,.15); color: #fbbf24; border: 1px solid #f59e0b; }
    .badge-danger  { background: rgba(239,68,68,.15);  color: #f87171; border: 1px solid #ef4444; }
    .badge-low     { background: rgba(59,130,246,.15); color: #93c5fd; border: 1px solid #3b82f6; }
    .badge-empty   { background: rgba(100,116,139,.15);color: #94a3b8; border: 1px solid #475569; }
    /* Version chip — top-right corner */
    #versionChip {
      position: fixed; top: .75rem; right: 1rem; z-index: 9999;
      font-size: .72rem; font-weight: 600; letter-spacing: .03em;
      background: #1a1f2e; border: 1px solid #2d3748; border-radius: 999px;
      padding: .25rem .7rem; color: #94a3b8; cursor: default;
      white-space: nowrap;
    }
    #versionChip.up-to-date { color: #4ade80; border-color: #4ade80; }
    #versionChip.update-available { color: #fbbf24; border-color: #fbbf24; }
    /* Version footer */
    #versionFooter {
      width: 100%; max-width: 760px; margin-top: 1rem; padding: .5rem 1.5rem;
      background: #1a1f2e; border-top: 1px solid #2d3748; border-radius: 6px;
      display: flex; align-items: center; justify-content: space-between; gap: 1rem;
      flex-wrap: wrap;
    }
    #versionBadge { font-size: .78rem; }
    #upgradeBtn {
      background: #2dd4bf; color: #0d0f14; font-size: .82rem; padding: .35rem .9rem;
      border-radius: 5px;
    }
    #upgradeBtn:focus-visible { outline: 2px solid #2dd4bf; outline-offset: 2px; }
    #upgradeBtn:not(:disabled):hover { opacity: .85; }
    @keyframes versionPulse { 0%,100% { opacity:1; } 50% { opacity:.5; } }
    .badge-upgrading { animation: versionPulse 1.2s ease-in-out infinite;
      background: rgba(45,212,191,.15); color: #2dd4bf; border: 1px solid #2dd4bf; }
    /* Variance band */
    .variance-note { font-size: .75rem; color: #64748b; margin-top: .25rem; text-align: center; }
    /* Status grid */
    .status-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 1rem; }
    .status-item { background: #131720; border-radius: 8px; padding: .75rem 1rem; }
    /* Harman delta colors */
    .harman-good { color: #4ade80; }
    .harman-ok   { color: #fbbf24; }
    .harman-bad  { color: #f87171; }
    /* Run rows */
    .run-row { cursor: pointer; }
    .run-row:hover td { background: #1e2535; }
    /* Convergence chart */
    #convergenceChart { height: 200px; }
  </style>
</head>
<body>
  <div id="versionChip" title="Running version">&#8230;</div>
  <h1>AVR Calibration</h1>

  <!-- System Status -->
  <div class="card" id="statusCard">
    <h2>System Status</h2>
    <div class="status-grid" id="statusGrid">Loading...</div>
  </div>

  <!-- FR Plot -->
  <div class="card" id="plotCard" style="display:none">
    <div class="curve-row">
      <label for="curveSelect">Target curve:</label>
      <select id="curveSelect" onchange="onCurveChange()">
        <option value="harman">Harman (+3 dB/oct below 80 Hz)</option>
        <option value="ht">HT-Aggressive (+4 dB/oct below 100 Hz)</option>
        <option value="music">Musicality (Gaussian peak at 30 Hz)</option>
        <option value="flat">Flat</option>
      </select>
    </div>
    <canvas id="frPlot"></canvas>
    <p id="plotStatus" style="font-size:.8rem;color:#64748b;margin-top:.5rem;text-align:center;"></p>
    <div style="text-align:right;margin-top:.5rem">
      <button id="exportBtn" onclick="exportChart()" style="background:#334155;color:#cbd5e1;font-size:.8rem;padding:.4rem .9rem">Export PNG</button>
    </div>
  </div>

  <!-- Convergence Delta -->
  <div class="card" id="deltaCard" style="display:none">
    <h2>Convergence vs Target</h2>
    <table class="delta-tbl" id="deltaTable">
      <thead><tr><th>Band (Hz)</th><th>SPL</th><th>Target</th><th>Delta</th></tr></thead>
      <tbody id="deltaBody"></tbody>
    </table>
  </div>

  <!-- Calibration Runs -->
  <div class="card" id="runsCard">
    <h2>Calibration Runs</h2>
    <table><thead><tr>
      <th>#</th><th>Date</th><th>Recipe</th><th>Target</th>
      <th>Status</th><th>Iters</th><th>Baseline</th><th>Final</th>
    </tr></thead><tbody id="runsBody"></tbody></table>
  </div>

  <!-- Run Detail -->
  <div class="card" id="runDetailCard" style="display:none">
    <h2>Run Detail</h2>
    <canvas id="convergenceChart" height="200"></canvas>
    <table style="margin-top:1rem"><thead><tr>
      <th>Iter</th><th>RMS Before</th><th>RMS After</th><th>Safety</th><th>Filters</th>
    </tr></thead><tbody id="runIterBody"></tbody></table>
  </div>

  <!-- Measurement History -->
  <div class="card">
    <div class="hist-header">
      <h2>History</h2>
      <button id="avgBtn" onclick="averageSelected()">Average Selected</button>
    </div>
    <table id="histTable">
      <thead>
        <tr><th class="cb-col"></th><th>#</th><th>Date</th><th>Label</th><th>Peak SPL</th><th>&Delta; Harman</th><th>Bins</th></tr>
      </thead>
      <tbody id="histBody"></tbody>
    </table>
  </div>

  <p id="status"></p>

  <script>
  // ── Global state ──────────────────────────────────────────────────────────
  let currentSessionId = null;
  let selectedSessionId = null;
  let frChart = null;

  // ── Target curve ──────────────────────────────────────────────────────────
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
    if (targetCurveType === 'ht')
      return freqs.map(f => f >= 100 ? refSpl : refSpl + 4 * Math.log2(100 / f));
    if (targetCurveType === 'music') return freqs.map(f => {
      const oct = Math.log2(f / 30);
      return refSpl + 4 * Math.exp(-(oct * oct) / (2 * 0.7 * 0.7));
    });
    // Harman: flat above 80 Hz, +3 dB/octave below 80 Hz
    return freqs.map(f => f >= 80 ? refSpl : refSpl + 3 * Math.log2(80 / f));
  }

  // ── Convergence delta table ───────────────────────────────────────────────
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

  // ── FR plot ───────────────────────────────────────────────────────────────
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
        label: targetCurveType === 'harman' ? 'Harman Target' :
               targetCurveType === 'ht' ? 'HT-Aggressive Target' :
               targetCurveType === 'music' ? 'Musicality Target' : 'Flat Target',
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

  // ── Load session ──────────────────────────────────────────────────────────
  async function loadSession(id) {
    selectedSessionId = id;
    currentSessionId = id;

    const url = new URL(window.location);
    url.searchParams.set('session', id);
    history.pushState({ session: id }, '', url);

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

  // ── History ───────────────────────────────────────────────────────────────
  async function loadHistory() {
    const r = await fetch('/api/sessions');
    if (!r.ok) return;
    const sessions = await r.json();
    const tbody = document.getElementById('histBody');
    tbody.innerHTML = sessions.map(s => {
      const ts = s.timestamp.slice(0,19).replace('T',' ');
      const label = s.label || '\u2014';
      const peak = s.peak_spl.toFixed(1) + ' dBFS';
      const sel = s.id === selectedSessionId ? ' selected' : '';
      // Harman delta column
      let deltaStr = '\u2014';
      let deltaCls = '';
      if (s.harman_delta_db != null) {
        deltaStr = s.harman_delta_db.toFixed(1) + ' dB';
        deltaCls = s.harman_delta_db <= 3 ? 'harman-good' : s.harman_delta_db <= 6 ? 'harman-ok' : 'harman-bad';
      }
      return `<tr class="${sel}" data-session-id="${s.id}" onclick="loadSession(${s.id})">
        <td class="cb-col" onclick="event.stopPropagation()">
          <input type="checkbox" data-id="${s.id}" onchange="updateAvgButton()">
        </td>
        <td>${s.id}</td><td>${ts}</td><td>${label}</td>
        <td class="peak">${peak}</td><td class="${deltaCls}">${deltaStr}</td><td>${s.n_freqs}</td>
      </tr>`;
    }).join('');
  }

  function updateAvgButton() {
    const checked = document.querySelectorAll('#histBody input[type=checkbox]:checked');
    const btn = document.getElementById('avgBtn');
    btn.style.display = checked.length >= 2 ? '' : 'none';
    btn.textContent = `Average ${checked.length} Sessions`;
  }

  // ── Multi-position spatial average ────────────────────────────────────────
  async function averageSelected() {
    const checked = [...document.querySelectorAll('#histBody input[type=checkbox]:checked')];
    const ids = checked.map(cb => parseInt(cb.dataset.id));
    if (ids.length < 2) return;
    setStatus('Averaging sessions\u2026');
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
      if (result.spl_variance && frChart) {
        const upper = result.spl_dbfs.map((v, i) => v + result.spl_variance[i]);
        const lower = result.spl_dbfs.map((v, i) => v - result.spl_variance[i]);
        frChart.data.datasets.push({
          label: '\u00b11\u03c3 variance band (upper)',
          data: upper,
          borderColor: 'transparent',
          backgroundColor: 'rgba(45,212,191,0.12)',
          pointRadius: 0,
          fill: '+1',
        });
        frChart.data.datasets.push({
          label: '\u00b11\u03c3 variance band (lower)',
          data: lower,
          borderColor: 'transparent',
          backgroundColor: 'rgba(45,212,191,0.12)',
          pointRadius: 0,
          fill: false,
        });
        frChart.update();
      }
    } catch (e) {
      setStatus('Average error: ' + e.message, 'error');
    }
  }

  function setStatus(msg, cls='') {
    const el = document.getElementById('status');
    el.textContent = msg;
    el.className = cls;
  }

  // ── System status ─────────────────────────────────────────────────────────
  async function loadStatus() {
    try {
      const r = await fetch('/api/status');
      if (!r.ok) return;
      const data = await r.json();
      const grid = document.getElementById('statusGrid');
      grid.innerHTML = data.devices.map(d => {
        const cls = d.connected ? 'badge-optimal' : 'badge-danger';
        const label = d.connected ? 'Connected' : 'Disconnected';
        return `<div class="status-item">
          <span class="badge ${cls}">${label}</span>
          <strong>${d.name}</strong>
          <div style="font-size:.78rem;color:#94a3b8;">${d.detail || ''}</div>
        </div>`;
      }).join('');
      if (data.last_run) {
        const lr = data.last_run;
        const status = lr.converged ? '\u2713 Converged' : '\u2717 Not converged';
        grid.innerHTML += `<div class="status-item">
          <span class="badge ${lr.converged ? 'badge-optimal' : 'badge-warn'}">${status}</span>
          <strong>Last Run</strong>
          <div style="font-size:.78rem;color:#94a3b8;">${lr.recipe_name} \u2014 ${lr.final_rms?.toFixed(1) || '?'} dB RMS</div>
        </div>`;
      }
    } catch(e) { console.warn('status load failed:', e); }
  }

  // ── Calibration runs ──────────────────────────────────────────────────────
  let convergenceChart = null;

  async function loadRuns() {
    try {
      const r = await fetch('/api/runs');
      if (!r.ok) return;
      const runs = await r.json();
      const tbody = document.getElementById('runsBody');
      tbody.innerHTML = runs.map(run => {
        const ts = run.timestamp.slice(0,19).replace('T',' ');
        const status = run.converged ? '<span class="badge badge-optimal">Converged</span>' :
                       run.error ? '<span class="badge badge-danger">Error</span>' :
                       '<span class="badge badge-warn">Max iters</span>';
        const baseline = run.baseline_rms != null ? run.baseline_rms.toFixed(1) + ' dB' : '\u2014';
        const final_rms = run.final_rms != null ? run.final_rms.toFixed(1) + ' dB' : '\u2014';
        return `<tr class="run-row" onclick="loadRunDetail(${run.id})">
          <td>${run.id}</td><td>${ts}</td><td>${run.recipe_name}</td><td>${run.target}</td>
          <td>${status}</td><td>${run.iterations_run}</td><td>${baseline}</td><td>${final_rms}</td>
        </tr>`;
      }).join('');
      if (runs.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" style="color:#64748b;text-align:center;">No calibration runs yet</td></tr>';
      }
    } catch(e) { console.warn('runs load failed:', e); }
  }

  async function loadRunDetail(runId) {
    try {
      const r = await fetch(`/api/runs/${runId}`);
      if (!r.ok) return;
      const detail = await r.json();
      document.getElementById('runDetailCard').style.display = '';

      // Convergence chart
      const iters = detail.iterations || [];
      const labels = iters.map(it => 'Iter ' + it.iteration);
      const before = iters.map(it => it.rms_before);
      const after = iters.map(it => it.rms_after);

      const ctx = document.getElementById('convergenceChart').getContext('2d');
      if (convergenceChart) convergenceChart.destroy();
      convergenceChart = new Chart(ctx, {
        type: 'line',
        data: {
          labels,
          datasets: [
            { label: 'RMS Before', data: before, borderColor: '#f87171', borderWidth: 2, pointRadius: 4, tension: 0.2 },
            { label: 'RMS After', data: after, borderColor: '#4ade80', borderWidth: 2, pointRadius: 4, tension: 0.2 },
          ]
        },
        options: {
          animation: false,
          responsive: true,
          scales: {
            y: { title: { display: true, text: 'RMS Deviation (dB)', color: '#64748b' },
                 ticks: { color: '#64748b' }, grid: { color: '#1e293b' } },
            x: { ticks: { color: '#64748b' }, grid: { color: '#1e293b' } }
          },
          plugins: { legend: { labels: { color: '#94a3b8', font: { size: 11 } } } }
        }
      });

      // Iteration table
      const tbody = document.getElementById('runIterBody');
      tbody.innerHTML = iters.map(it => {
        const safety = it.safety_ok ? '<span class="badge badge-optimal">OK</span>' :
                       '<span class="badge badge-danger" title="' + (it.safety_error||'') + '">Rejected</span>';
        const nFilters = (it.filters_applied || []).length;
        return `<tr>
          <td>${it.iteration}</td><td>${it.rms_before.toFixed(1)} dB</td>
          <td>${it.rms_after.toFixed(1)} dB</td><td>${safety}</td><td>${nFilters} filters</td>
        </tr>`;
      }).join('');
    } catch(e) { console.warn('run detail load failed:', e); }
  }

  // ── Boot ──────────────────────────────────────────────────────────────────
  loadHistory().then(() => {
    const urlParams = new URLSearchParams(window.location.search);
    const urlSession = urlParams.get('session');
    if (urlSession) loadSession(parseInt(urlSession, 10));
  });
  loadStatus();
  loadRuns();
  setInterval(loadStatus, 30000);

  window.addEventListener('popstate', (e) => {
    const id = e.state && e.state.session;
    if (id) loadSession(id);
  });

  // ── Version footer ────────────────────────────────────────────────────────

  const versionFooter = document.getElementById('versionFooter');
  const versionBadge = document.getElementById('versionBadge');
  const versionStatus = document.getElementById('versionStatus');
  const upgradeBtn = document.getElementById('upgradeBtn');
  const upgradeConfirm = document.getElementById('upgradeConfirm');
  const versionChip = document.getElementById('versionChip');
  let _upgradePolling = false;

  function _setChip(text, cls, title) {
    if (!versionChip) return;
    versionChip.textContent = text;
    versionChip.classList.remove('up-to-date', 'update-available');
    if (cls) versionChip.classList.add(cls);
    versionChip.title = title;
  }

  async function loadVersion() {
    try {
      const r = await fetch('/api/version');
      if (!r.ok) throw new Error(r.statusText);
      const d = await r.json();
      const sha7 = d.current_sha !== 'unknown' ? d.current_sha.slice(0,7) : 'unknown';
      const semver = (d.semantic_version && d.semantic_version !== 'unknown') ? d.semantic_version : sha7;
      if (!d.semantic_version || d.semantic_version === 'unknown') {
        console.warn('avr-calibration: semantic_version missing from /api/version response');
      }
      if (d.up_to_date) {
        versionBadge.innerHTML = '<span class="badge badge-optimal">v' + sha7 + ' \u2014 Up to date</span>';
        upgradeBtn.style.display = 'none';
        upgradeConfirm.style.display = 'none';
        _setChip('v' + semver, 'up-to-date', 'v' + semver + ' (' + sha7 + ') \u2014 Up to date');
      } else if (d.latest_sha) {
        versionBadge.innerHTML = '<span class="badge badge-warn">v' + sha7 + ' \u2014 Update available</span>';
        upgradeBtn.style.display = '';
        upgradeConfirm.style.display = 'none';
        _setChip('v' + semver + ' \u25b2', 'update-available', 'v' + semver + ' (' + sha7 + ') \u2014 Update available');
      } else if (d.checking) {
        // Background GHCR check still in flight — show semver immediately, retry in 8s
        versionBadge.innerHTML = '<span class="badge badge-empty">v' + sha7 + '</span>';
        upgradeBtn.style.display = 'none';
        upgradeConfirm.style.display = 'none';
        _setChip('v' + semver, null, 'v' + semver + ' (' + sha7 + ')');
        setTimeout(loadVersion, 8000);
      } else {
        versionBadge.innerHTML = '<span class="badge badge-empty">v' + sha7 + ' \u2014 Version check unavailable</span>';
        upgradeBtn.style.display = 'none';
        upgradeConfirm.style.display = 'none';
        _setChip('v' + semver, null, 'v' + semver + ' (' + sha7 + ')');
      }
    } catch (e) {
      versionBadge.innerHTML = '<span class="badge badge-empty">Version unavailable</span>';
      upgradeBtn.style.display = 'none';
      _setChip('\u2014', null, 'Version unavailable');
    }
  }

  function showUpgradeConfirm() {
    upgradeBtn.style.display = 'none';
    upgradeConfirm.style.display = '';
  }

  function cancelUpgrade() {
    upgradeConfirm.style.display = 'none';
    upgradeBtn.style.display = '';
  }

  async function confirmUpgrade() {
    upgradeConfirm.style.display = 'none';
    versionBadge.innerHTML = '<span class="badge badge-upgrading">Upgrading...</span>';
    versionStatus.textContent = 'Upgrading \u2014 checking every 3s (0s elapsed)';
    versionStatus.style.display = '';
    versionStatus.focus();
    _upgradePolling = true;

    try {
      const r = await fetch('/api/upgrade', {method: 'POST'});
      if (r.status === 409) {
        versionStatus.textContent = 'Upgrade already in progress. Please wait.';
        return;
      }
      if (!r.ok) {
        const err = await r.json().catch(() => ({detail: r.statusText}));
        versionStatus.textContent = 'Upgrade failed: ' + (err.detail || r.statusText);
        _upgradePolling = false;
        await loadVersion();
        return;
      }
    } catch (e) {
      versionStatus.textContent = 'Upgrade request failed: ' + e.message;
      _upgradePolling = false;
      await loadVersion();
      return;
    }

    // Poll /health until the new container is up
    const startTime = Date.now();
    const maxWait = 180000;
    const pollInterval = 3000;

    async function poll() {
      if (!_upgradePolling) return;
      const elapsed = Math.round((Date.now() - startTime) / 1000);
      versionStatus.textContent = 'Upgrading \u2014 checking every 3s (' + elapsed + 's elapsed)';

      if (elapsed * 1000 >= maxWait) {
        versionStatus.textContent = 'Upgrade is taking longer than expected. Check the Pi\u2019s network connection.';
        _upgradePolling = false;
        await loadVersion();
        return;
      }

      try {
        const h = await fetch('/health', {cache: 'no-store'});
        if (h.ok) {
          const data = await h.json().catch(() => ({}));
          // Make sure this isn't the old container by checking after restart gap
          if (elapsed >= 5) {
            versionStatus.textContent = 'Updated successfully \u2014 reloading\u2026';
            setTimeout(() => window.location.reload(), 2000);
            return;
          }
        }
      } catch (_) { /* container restarting — expected */ }

      setTimeout(poll, pollInterval);
    }

    // Brief pause so the old container has time to receive the trigger and restart
    setTimeout(poll, 5000);
  }

  loadVersion();
  </script>

  <div id="versionFooter">
    <div id="versionBadge"><span class="badge badge-empty">Checking version\u2026</span></div>
    <div style="display:flex;align-items:center;gap:.75rem;flex-wrap:wrap;">
      <span id="versionStatus" role="status" aria-live="polite" tabindex="-1"
            style="font-size:.78rem;color:#94a3b8;display:none;"></span>
      <div id="upgradeConfirm" style="display:none;font-size:.82rem;color:#94a3b8;">
        Restart the appliance to install the update? Any active measurement will be interrupted.
        &nbsp;<button type="button" onclick="confirmUpgrade()"
          style="background:#2dd4bf;color:#0d0f14;font-size:.8rem;padding:.3rem .75rem;border-radius:4px;">
          Confirm</button>
        &nbsp;<button type="button" onclick="cancelUpgrade()"
          style="background:#334155;color:#cbd5e1;font-size:.8rem;padding:.3rem .75rem;border-radius:4px;">
          Cancel</button>
      </div>
      <button type="button" id="upgradeBtn" style="display:none;" onclick="showUpgradeConfirm()">
        Upgrade
      </button>
    </div>
  </div>
</body>
</html>
"""


# ── Pydantic models ────────────────────────────────────────────────────────────

class AverageRequest(BaseModel):
    session_ids: list[int]


class HeadlessMeasureRequest(BaseModel):
    label: str | None = None


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return _HTML


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# ── Version / upgrade helpers ─────────────────────────────────────────────────

async def _fetch_latest_sha() -> Optional[str]:
    """Fetch the latest git SHA from GHCR manifest index annotations.

    Two-step: anonymous token -> manifest index. Returns None on any failure.
    The SHA is stored as an OCI annotation on the manifest index by CI.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Step 1: anonymous bearer token (required even for public repos)
            token_resp = await client.get(
                f"https://{_GHCR_REGISTRY}/token",
                params={
                    "service": _GHCR_REGISTRY,
                    "scope": f"repository:{_GHCR_IMAGE}:pull",
                },
            )
            if token_resp.status_code == 401:
                # Retry once with fresh request (shouldn't happen for anon token)
                logger.warning("GHCR token: unexpected 401")
                return None
            token_resp.raise_for_status()
            token = token_resp.json()["token"]

            # Step 2: OCI image index for :latest — annotations include revision SHA
            manifest_resp = await client.get(
                f"https://{_GHCR_REGISTRY}/v2/{_GHCR_IMAGE}/manifests/latest",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.oci.image.index.v1+json",
                },
            )
            if manifest_resp.status_code == 429:
                logger.warning("GHCR manifest: rate limited (429)")
                return None
            manifest_resp.raise_for_status()
            annotations = manifest_resp.json().get("annotations") or {}
            return annotations.get("org.opencontainers.image.revision")
    except httpx.TimeoutException:
        logger.warning("GHCR version check timed out")
        return None
    except Exception as exc:
        logger.warning("GHCR version check failed: %s", exc)
        return None


# ── Version / upgrade routes ──────────────────────────────────────────────────

_SEMANTIC_VERSION: str | None = None


def _read_semantic_version() -> str:
    """Return the semantic version, cached for the process lifetime.

    Resolution order:
    1. APP_VERSION env var (set by Dockerfile at build time)
    2. /app/VERSION (Docker runtime — WORKDIR, copied by Dockerfile)
    3. Repo root VERSION file (local dev — two levels up from calibrate/web.py)
    """
    global _SEMANTIC_VERSION
    if _SEMANTIC_VERSION is not None:
        return _SEMANTIC_VERSION
    env_ver = os.environ.get("APP_VERSION")
    if env_ver:
        _SEMANTIC_VERSION = env_ver
        return _SEMANTIC_VERSION
    for candidate in (Path("/app/VERSION"), Path(__file__).parent.parent / "VERSION"):
        try:
            _SEMANTIC_VERSION = candidate.read_text().strip()
            return _SEMANTIC_VERSION
        except FileNotFoundError:
            continue
    _SEMANTIC_VERSION = "unknown"
    return _SEMANTIC_VERSION


async def _fetch_and_cache_version() -> None:
    """Background task: fetch the latest SHA from GHCR and populate _version_cache."""
    try:
        latest_sha = await _fetch_latest_sha()
        checked_at = time.time()
        _version_cache["result"] = {
            "latest_sha": latest_sha,
            "expires": checked_at + _VERSION_CACHE_TTL,
            "checked_at": checked_at,
        }
    finally:
        _version_cache.pop("fetching", None)


@app.get("/api/version")
async def api_version() -> dict:
    """Return current and latest git SHAs plus the semantic version. Cached for 1 hour.

    On a cold cache the GHCR check is fired as a background task so this endpoint
    returns immediately. Callers should check ``checking: true`` and retry after a
    few seconds to pick up the result.
    """
    current_sha = os.environ.get("BUILD_SHA", "unknown")

    cached = _version_cache.get("result")
    if cached and cached.get("expires", 0) > time.time():
        latest_sha = cached.get("latest_sha")
        checked_at = cached.get("checked_at")
        checking = False
    else:
        # Fire background fetch only if one isn't already running.
        if not _version_cache.get("fetching"):
            _version_cache["fetching"] = True
            asyncio.create_task(_fetch_and_cache_version())
        latest_sha = None
        checked_at = time.time()
        checking = True

    up_to_date = (
        current_sha != "unknown"
        and latest_sha is not None
        and current_sha == latest_sha
    )

    return {
        "current_sha": current_sha,
        "latest_sha": latest_sha,
        "up_to_date": up_to_date,
        "latest_checked_at": checked_at,
        "semantic_version": _read_semantic_version(),
        "checking": checking,
    }


@app.post("/api/upgrade", status_code=202)
async def api_upgrade() -> dict:
    """Trigger a host-side upgrade by writing a trigger file to the data volume.

    The host avr-calibration-update.service watches for this file via inotifywait
    and performs docker pull + health-check gated restart.
    Returns 202 immediately. Returns 409 if an upgrade is already in progress.
    """
    trigger = _DATA_DIR / "upgrade-trigger"

    if trigger.exists():
        raise HTTPException(status_code=409, detail="Upgrade already in progress")

    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        trigger.touch()
    except PermissionError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Upgrade unavailable: data volume not writable ({exc})",
        )
    except OSError as exc:
        if exc.errno == 28:  # ENOSPC
            raise HTTPException(status_code=503, detail="Upgrade unavailable: disk full")
        raise HTTPException(status_code=503, detail=f"Upgrade unavailable: {exc}")

    return {"status": "upgrade_triggered"}


@app.post("/api/measure")
async def measure_headless(body: HeadlessMeasureRequest) -> dict:
    """Headless measurement for Pi 5: Pi records via UMIK-1 using PyTTa.

    Automatically powers on the Denon, switches to the configured sweep input,
    sets sweep volume, runs the measurement, then restores previous input/volume.

    Requires UMIK-1 connected and the 'measurement' extra installed (arm64/amd64 images).
    On Pi Zero 2 W (arm/v7), use the browser-based /api/measure/start + /api/measure/record flow.
    """
    if _measurement_lock.locked():
        raise HTTPException(status_code=409, detail="measurement already in progress")

    try:
        import sounddevice as sd
        devices = sd.query_devices()
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail="sounddevice not available on this platform — use browser-based measurement",
        )

    cfg = _load_config()
    mic_name: str = cfg.mic.get("name", "UMIK")
    umik_idx = _find_umik_device(devices, name_substring=mic_name)
    if umik_idx is None:
        raise HTTPException(
            status_code=503,
            detail=f"No microphone matching '{mic_name}' found — check USB connection",
        )

    device_name: str = str(devices[umik_idx].get("name", mic_name))

    # ── Denon: power on, switch input, set sweep volume ────────────────────
    denon_host = cfg.denon.get("host")
    sweep_input = cfg.measurement.get("denon_sweep_input")
    sweep_volume: float = float(cfg.measurement.get("denon_sweep_volume", -25.0))
    receiver = None
    prev_input: str | None = None
    prev_volume: float | None = None
    was_off: bool = False

    if denon_host and sweep_input:
        try:
            import denonavr as _denonavr
            receiver = _denonavr.DenonAVR(denon_host)
            await asyncio.wait_for(receiver.async_setup(), timeout=5.0)
            await receiver.async_update()

            was_off = (receiver.power or "").upper() == "OFF"
            if was_off:
                await receiver.async_power_on()
                await asyncio.sleep(3.0)  # Denon boot takes ~2-3s
                await receiver.async_update()

            available = receiver.input_func_list or []
            if sweep_input not in available:
                raise ValueError(
                    f"Input '{sweep_input}' not found on Denon. "
                    f"Available: {sorted(available)}"
                )

            prev_input = receiver.input_func
            prev_volume = receiver.volume

            await receiver.async_set_input_func(sweep_input)
            await asyncio.sleep(0.5)
            await receiver.async_set_volume(sweep_volume)
            await asyncio.sleep(0.5)
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=f"Denon setup failed ({denon_host}): {exc}",
            )

    # ── Run measurement ─────────────────────────────────────────────────────
    try:
        async with _measurement_lock:
            engine = MeasurementEngine(cfg)
            try:
                fr = await asyncio.to_thread(engine.measure, device_name)
            except RuntimeError as exc:
                raise HTTPException(status_code=503, detail=str(exc))
    finally:
        # ── Restore Denon state ─────────────────────────────────────────────
        if receiver is not None:
            try:
                if prev_volume is not None:
                    await receiver.async_set_volume(prev_volume)
                if prev_input is not None:
                    await receiver.async_set_input_func(prev_input)
                if was_off:
                    await receiver.async_power_off()
            except Exception:
                pass  # best-effort restore; don't mask measurement errors

    store = SessionStore()
    session_id = store.save_measurement(fr, label=body.label or "headless")
    return {"session_id": session_id, "status": "ok"}


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
    spl_variance = []
    for i in range(n):
        linear_vals = [10 ** (fr.spl[i] / 20.0) for fr in frs]
        avg_linear = sum(linear_vals) / len(linear_vals)
        averaged_spl.append(20 * math.log10(avg_linear) if avg_linear > 0 else -120.0)
        # Per-bin standard deviation in dB domain
        db_vals = [fr.spl[i] for fr in frs]
        spl_variance.append(statistics.stdev(db_vals) if len(db_vals) > 1 else 0.0)

    return {
        "frequencies_hz": ref_freqs,
        "spl_dbfs": averaged_spl,
        "spl_variance": spl_variance,
        "n_positions": len(frs),
    }


@app.get("/api/sessions")
async def list_sessions() -> list[dict]:
    """Return all sessions for the history table, with Harman delta."""
    from .analysis import harman_rms

    store = SessionStore()
    sessions = store.list_sessions()
    result = []
    for s in sessions:
        harman_delta: float | None = None
        try:
            if s.start_fr and s.start_fr.frequencies:
                harman_delta = round(harman_rms(s.start_fr), 1)
        except Exception:
            pass  # analysis import or computation failure — leave as None
        result.append({
            "id": s.id,
            "timestamp": s.timestamp,
            "label": s.label,
            "peak_spl": s.start_fr.peak_spl,
            "freq_at_peak": s.start_fr.freq_at_peak,
            "n_freqs": len(s.start_fr.frequencies),
            "has_end_fr": s.end_fr is not None,
            "harman_delta_db": harman_delta,
        })
    return result


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


@app.get("/api/runs")
async def list_runs(limit: int = 20) -> list[dict]:
    """List calibration runs."""
    store = SessionStore()
    return store.get_runs(limit=limit)


@app.get("/api/runs/{run_id}")
async def get_run_detail(run_id: int) -> dict:
    """Return run detail with iteration history."""
    store = SessionStore()
    detail = store.get_run_detail(run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Run #{run_id} not found")
    return detail


@app.get("/api/status")
async def system_status() -> dict:
    """Return system device status and last calibration run."""
    devices = []
    cfg = _load_config()

    # Denon
    denon_host = cfg.denon.get("host")
    if denon_host:
        try:
            import denonavr
            receiver = denonavr.DenonAVR(denon_host)
            await asyncio.wait_for(receiver.async_setup(), timeout=5.0)
            await receiver.async_update()
            devices.append({
                "name": f"Denon {receiver.model_name or 'AVR'}",
                "connected": True,
                "detail": f"Input: {receiver.input_func}, Volume: {receiver.volume} dB",
            })
        except Exception:
            devices.append({"name": "Denon AVR", "connected": False, "detail": denon_host})

    # miniDSP
    minidsp_host = cfg.minidsp.get("host", "localhost")
    minidsp_port = cfg.minidsp.get("port", 5380)
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"http://{minidsp_host}:{minidsp_port}/devices/0/config")
            if r.status_code == 200:
                data = r.json()
                devices.append({
                    "name": "miniDSP 2x4 HD",
                    "connected": True,
                    "detail": f"Preset: {data.get('preset', '?')}, Source: {data.get('source', '?')}",
                })
            else:
                devices.append({"name": "miniDSP 2x4 HD", "connected": False, "detail": "HTTP error"})
    except Exception:
        devices.append({"name": "miniDSP 2x4 HD", "connected": False, "detail": f"{minidsp_host}:{minidsp_port}"})

    # UMIK
    try:
        import sounddevice as sd
        devs = sd.query_devices()
        mic_name = cfg.mic.get("name", "UMIK")
        umik_idx = _find_umik_device(devs, name_substring=mic_name)
        if umik_idx is not None:
            devices.append({"name": f"UMIK ({mic_name})", "connected": True, "detail": str(devs[umik_idx].get("name", ""))})
        else:
            devices.append({"name": f"UMIK ({mic_name})", "connected": False, "detail": "Not found"})
    except ImportError:
        devices.append({"name": "UMIK", "connected": False, "detail": "sounddevice not available"})
    except Exception as e:
        devices.append({"name": "UMIK", "connected": False, "detail": str(e)})

    # Last run
    store = SessionStore()
    runs = store.get_runs(limit=1)
    last_run = runs[0] if runs else None

    return {"devices": devices, "last_run": last_run}


# ── Config helper ─────────────────────────────────────────────────────────────

def _load_config() -> Config:
    if not CONFIG_PATH.exists():
        raise HTTPException(
            status_code=503,
            detail=f"No config at {CONFIG_PATH}. Run 'calibrate check' first.",
        )
    return Config.load(CONFIG_PATH)
