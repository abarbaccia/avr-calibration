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
from .drivers.denon import DenonDriver, DenonSweepContext
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
    :root {
      --bg: #0d0f14; --card: #1a1f2e; --border: #2d3748; --text: #e2e8f0;
      --muted: #94a3b8; --dim: #64748b; --accent: #3b82f6; --green: #4ade80;
      --yellow: #fbbf24; --red: #f87171; --teal: #2dd4bf;
    }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg); color: var(--text); min-height: 100vh;
      display: flex; flex-direction: column; align-items: center;
      padding: 1.5rem 1rem 2rem;
    }
    a { color: var(--accent); text-decoration: none; }
    a:hover { text-decoration: underline; }

    /* ── Layout ── */
    .container { width: 100%; max-width: 900px; }
    .header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 1.5rem; }
    .header h1 { font-size: 1.2rem; font-weight: 600; color: var(--muted); letter-spacing: .04em; text-transform: uppercase; }
    #versionChip {
      font-size: .7rem; font-weight: 600; letter-spacing: .03em;
      background: var(--card); border: 1px solid var(--border); border-radius: 999px;
      padding: .2rem .6rem; color: var(--muted); cursor: default; white-space: nowrap;
    }
    #versionChip.up-to-date { color: var(--green); border-color: var(--green); }
    #versionChip.update-available { color: var(--yellow); border-color: var(--yellow); }

    /* ── Cards ── */
    .card {
      background: var(--card); border: 1px solid var(--border); border-radius: 12px;
      padding: 1.25rem 1.5rem; margin-bottom: 1rem;
    }
    .card h2 {
      font-size: .8rem; text-transform: uppercase; letter-spacing: .08em;
      color: var(--dim); margin-bottom: .75rem;
    }

    /* ── Hero score ── */
    .hero { display: flex; align-items: center; gap: 1.5rem; flex-wrap: wrap; }
    .score-ring {
      width: 100px; height: 100px; border-radius: 50%;
      display: flex; flex-direction: column; align-items: center; justify-content: center;
      flex-shrink: 0;
    }
    .score-ring .value { font-size: 1.6rem; font-weight: 700; line-height: 1; }
    .score-ring .unit { font-size: .7rem; color: var(--muted); margin-top: 2px; }
    .score-ring.optimal { border: 3px solid var(--green); color: var(--green); }
    .score-ring.good { border: 3px solid var(--yellow); color: var(--yellow); }
    .score-ring.poor { border: 3px solid var(--red); color: var(--red); }
    .score-ring.none { border: 3px solid var(--border); color: var(--dim); }
    .hero-meta { flex: 1; min-width: 200px; }
    .hero-meta .label { font-size: .78rem; color: var(--dim); margin-bottom: .25rem; }
    .hero-meta .detail { font-size: .88rem; color: var(--text); margin-bottom: .5rem; }
    .hero-actions { display: flex; gap: .5rem; flex-wrap: wrap; margin-top: .25rem; }

    /* ── Hardware status (inline) ── */
    .hw-bar { display: flex; gap: .75rem; flex-wrap: wrap; padding: .5rem 0; }
    .hw-item { display: flex; align-items: center; gap: .35rem; font-size: .78rem; color: var(--muted); }
    .hw-dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
    .hw-dot.ok { background: var(--green); box-shadow: 0 0 4px var(--green); }
    .hw-dot.err { background: var(--red); box-shadow: 0 0 4px var(--red); }

    /* ── Buttons ── */
    button, .btn {
      padding: .45rem 1rem; border-radius: 6px; font-size: .82rem; font-weight: 500;
      cursor: pointer; border: none; transition: opacity .15s;
    }
    button:disabled { opacity: .4; cursor: not-allowed; }
    .btn-primary { background: var(--accent); color: #fff; }
    .btn-primary:hover:not(:disabled) { opacity: .85; }
    .btn-secondary { background: #334155; color: var(--text); }
    .btn-secondary:hover:not(:disabled) { background: #475569; }
    .btn-danger { background: transparent; color: var(--red); border: 1px solid var(--red); }
    .btn-danger:hover:not(:disabled) { background: rgba(239,68,68,.1); }
    .btn-save { background: var(--teal); color: var(--bg); }
    .btn-save:hover:not(:disabled) { opacity: .85; }
    .btn-sm { padding: .3rem .6rem; font-size: .75rem; }

    /* ── FR Chart area ── */
    .chart-controls {
      display: flex; align-items: center; gap: .75rem; margin-bottom: .5rem; flex-wrap: wrap;
    }
    .chart-controls label { font-size: .78rem; color: var(--dim); margin: 0; white-space: nowrap; }
    .chart-controls select {
      padding: .3rem .5rem; background: var(--bg); border: 1px solid var(--border);
      border-radius: 5px; color: var(--text); font-size: .78rem;
    }
    canvas { width: 100% !important; }
    #plotStatus { font-size: .78rem; color: var(--dim); margin-top: .35rem; text-align: center; }

    /* ── Delta table ── */
    .delta-tbl { font-size: .78rem; }
    .delta-tbl td { padding: .3rem .5rem; }
    .delta-tbl tr.ok td { color: var(--green); }
    .delta-tbl tr.warn td { color: var(--yellow); }
    .delta-tbl tr.bad td { color: var(--red); }

    /* ── Tables ── */
    table { width: 100%; border-collapse: collapse; font-size: .8rem; }
    th { color: var(--dim); font-weight: 500; text-align: left; padding: .4rem .5rem;
         border-bottom: 1px solid var(--border); }
    td { padding: .4rem .5rem; border-bottom: 1px solid #1a2030; color: #cbd5e1; }
    tr.clickable { cursor: pointer; }
    tr.clickable:hover td { background: #1e2535; }
    tr.selected td { background: #1e2535; border-left: 3px solid var(--accent); }
    th.cb-col, td.cb-col { width: 2rem; text-align: center; padding: .4rem .25rem; cursor: default; }

    /* ── Badges ── */
    .badge { display: inline-block; padding: .2rem .6rem; border-radius: 4px; font-size: .75rem;
             font-weight: 600; }
    .badge-optimal { background: rgba(34,197,94,.15); color: var(--green); border: 1px solid #22c55e; }
    .badge-warn    { background: rgba(245,158,11,.15); color: var(--yellow); border: 1px solid #f59e0b; }
    .badge-danger  { background: rgba(239,68,68,.15);  color: var(--red); border: 1px solid #ef4444; }
    .badge-empty   { background: rgba(100,116,139,.15);color: var(--muted); border: 1px solid #475569; }
    @keyframes versionPulse { 0%,100% { opacity:1; } 50% { opacity:.5; } }
    .badge-upgrading { animation: versionPulse 1.2s ease-in-out infinite;
      background: rgba(45,212,191,.15); color: var(--teal); border: 1px solid var(--teal); }

    /* ── Collapsible sections ── */
    .collapse-header {
      display: flex; align-items: center; justify-content: space-between;
      cursor: pointer; user-select: none;
    }
    .collapse-header h2 { margin-bottom: 0; }
    .collapse-header .count { font-size: .75rem; color: var(--dim); }
    .collapse-header .arrow { font-size: .7rem; color: var(--dim); transition: transform .2s; }
    .collapse-header.open .arrow { transform: rotate(90deg); }
    .collapse-body { overflow: hidden; transition: max-height .25s ease; }
    .collapse-body.collapsed { max-height: 0 !important; }

    /* ── Saved states ── */
    .state-list { display: flex; flex-direction: column; gap: .5rem; }
    .state-item {
      display: flex; align-items: center; justify-content: space-between; gap: .75rem;
      background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
      padding: .6rem .75rem;
    }
    .state-info { flex: 1; }
    .state-name { font-size: .85rem; font-weight: 500; }
    .state-meta { font-size: .72rem; color: var(--dim); margin-top: .15rem; }
    .state-actions { display: flex; gap: .35rem; }

    /* ── Overlay chips ── */
    .overlay-chips { display: flex; gap: .35rem; flex-wrap: wrap; margin-bottom: .5rem; }
    .overlay-chip {
      display: inline-flex; align-items: center; gap: .25rem;
      padding: .2rem .5rem; border-radius: 4px; font-size: .72rem; font-weight: 500;
      cursor: default;
    }
    .overlay-chip .remove { cursor: pointer; opacity: .6; font-size: .8rem; }
    .overlay-chip .remove:hover { opacity: 1; }

    /* ── Save modal ── */
    .modal-overlay {
      position: fixed; inset: 0; background: rgba(0,0,0,.6); z-index: 1000;
      display: flex; align-items: center; justify-content: center;
    }
    .modal-overlay.hidden { display: none; }
    .modal {
      background: var(--card); border: 1px solid var(--border); border-radius: 12px;
      padding: 1.5rem; width: 90%; max-width: 400px;
    }
    .modal h3 { font-size: 1rem; margin-bottom: 1rem; }
    .modal input[type=text] {
      width: 100%; padding: .5rem .75rem; background: var(--bg); border: 1px solid var(--border);
      border-radius: 6px; color: var(--text); font-size: .9rem; margin-bottom: .75rem;
    }
    .modal input[type=text]:focus { outline: none; border-color: var(--accent); }
    .modal-actions { display: flex; justify-content: flex-end; gap: .5rem; }

    /* ── Harman delta colors ── */
    .harman-good { color: var(--green); }
    .harman-ok   { color: var(--yellow); }
    .harman-bad  { color: var(--red); }

    /* ── Toast ── */
    #toast {
      position: fixed; bottom: 1.5rem; left: 50%; transform: translateX(-50%);
      background: var(--card); border: 1px solid var(--border); border-radius: 8px;
      padding: .5rem 1rem; font-size: .82rem; color: var(--text); z-index: 2000;
      opacity: 0; transition: opacity .3s; pointer-events: none;
    }
    #toast.show { opacity: 1; }

    /* ── Version footer ── */
    #versionFooter {
      width: 100%; max-width: 900px; margin-top: .5rem; padding: .4rem 1rem;
      display: flex; align-items: center; justify-content: space-between; gap: .75rem;
      flex-wrap: wrap; font-size: .75rem; color: var(--dim);
    }
  </style>
</head>
<body>

<div class="container">
  <!-- Header -->
  <div class="header">
    <h1><a href="/" style="color:inherit;text-decoration:none">AVR Calibration</a></h1>
    <span id="versionChip" title="Running version">...</span>
  </div>

  <!-- Hero: Room Score + Latest FR -->
  <div class="card" id="heroCard">
    <div class="hero">
      <div class="score-ring none" id="scoreRing">
        <span class="value" id="scoreValue">--</span>
        <span class="unit">dB RMS</span>
      </div>
      <div class="hero-meta">
        <div class="label" id="heroLabel">No measurements yet</div>
        <div class="detail" id="heroDetail">Take a measurement to see your room's bass response</div>
        <div class="detail" id="heroContext" style="font-size:.75rem;color:var(--dim);margin-top:-.25rem;margin-bottom:.5rem;"></div>
        <div class="hw-bar" id="hwBar">Loading...</div>
      </div>
    </div>
  </div>

  <!-- FR Plot (always visible when data exists) -->
  <div class="card" id="plotCard" style="display:none">
    <div class="chart-controls">
      <label for="curveSelect">Target:</label>
      <select id="curveSelect" onchange="onCurveChange()">
        <option value="harman">Harman</option>
        <option value="ht">HT-Aggressive</option>
        <option value="music">Musicality</option>
        <option value="flat">Flat</option>
      </select>
      <div style="flex:1"></div>
      <button class="btn-save btn-sm" onclick="showSaveModal()">Save State</button>
      <button class="btn-secondary btn-sm" onclick="exportChart()">Export PNG</button>
    </div>
    <div class="overlay-chips" id="overlayChips"></div>
    <canvas id="frPlot"></canvas>
    <p id="plotStatus"></p>
  </div>

  <!-- Convergence Delta (shown when viewing a session) -->
  <div class="card" id="deltaCard" style="display:none">
    <h2>Convergence vs Target</h2>
    <table class="delta-tbl" id="deltaTable">
      <thead><tr><th>Band (Hz)</th><th>SPL</th><th>Target</th><th>Delta</th></tr></thead>
      <tbody id="deltaBody"></tbody>
    </table>
  </div>

  <!-- Saved States -->
  <div class="card" id="statesCard">
    <div class="collapse-header open" onclick="toggleSection('states')">
      <h2>Saved States <span class="count" id="statesCount"></span></h2>
      <span class="arrow" id="statesArrow">&#9654;</span>
    </div>
    <div class="collapse-body" id="statesBody">
      <div class="state-list" id="stateList">
        <div style="color:var(--dim);font-size:.82rem;">No saved states yet</div>
      </div>
    </div>
  </div>

  <!-- Sweep History -->
  <div class="card">
    <div class="collapse-header open" onclick="toggleSection('hist')">
      <h2>Measurements <span class="count" id="histCount"></span></h2>
      <span class="arrow" id="histArrow">&#9654;</span>
    </div>
    <div class="collapse-body" id="histSection">
      <div style="display:flex;gap:.5rem;margin-bottom:.5rem;">
        <button class="btn-secondary btn-sm" id="overlayBtn" style="display:none" onclick="overlaySelected()">Overlay Selected</button>
        <button class="btn-secondary btn-sm" id="avgBtn" style="display:none" onclick="averageSelected()">Average Selected</button>
      </div>
      <table id="histTable">
        <thead>
          <tr><th class="cb-col"></th><th>#</th><th>Date</th><th>Type</th><th>Peak SPL</th><th>&Delta; Harman</th></tr>
        </thead>
        <tbody id="histBody"></tbody>
      </table>
    </div>
  </div>

  <!-- Calibration Runs -->
  <div class="card" id="runsCard">
    <div class="collapse-header" onclick="toggleSection('runs')">
      <h2>Calibration Runs <span class="count" id="runsCount"></span></h2>
      <span class="arrow" id="runsArrow">&#9654;</span>
    </div>
    <div class="collapse-body collapsed" id="runsSection">
      <table><thead><tr>
        <th>#</th><th>Date</th><th>Recipe</th><th>Target</th>
        <th>Status</th><th>Iters</th><th>Baseline</th><th>Final</th>
      </tr></thead><tbody id="runsBody"></tbody></table>
    </div>
  </div>

  <!-- Run Detail -->
  <div class="card" id="runDetailCard" style="display:none">
    <h2>Run Detail</h2>
    <canvas id="convergenceChart" height="200"></canvas>
    <table style="margin-top:.75rem"><thead><tr>
      <th>Iter</th><th>RMS Before</th><th>RMS After</th><th>Safety</th><th>Filters</th>
    </tr></thead><tbody id="runIterBody"></tbody></table>
  </div>

</div><!-- /container -->

<!-- Save State Modal -->
<div class="modal-overlay hidden" id="saveModal" onclick="if(event.target===this)closeSaveModal()">
  <div class="modal">
    <h3>Save Current State</h3>
    <input type="text" id="stateName" placeholder="Name (e.g. Harman Converged v1)">
    <input type="text" id="stateNotes" placeholder="Notes (optional)">
    <div class="modal-actions">
      <button class="btn-secondary" onclick="closeSaveModal()">Cancel</button>
      <button class="btn-save" onclick="doSaveState()">Save</button>
    </div>
  </div>
</div>

<div id="toast"></div>

<div id="versionFooter">
  <div id="versionBadge"><span class="badge badge-empty">Checking version...</span></div>
  <div style="display:flex;align-items:center;gap:.75rem;flex-wrap:wrap;">
    <span id="versionStatus" role="status" aria-live="polite" tabindex="-1"
          style="display:none;"></span>
    <div id="upgradeConfirm" style="display:none;font-size:.8rem;">
      Restart to install update?
      <button type="button" onclick="confirmUpgrade()" class="btn-save btn-sm" style="margin-left:.5rem;">Confirm</button>
      <button type="button" onclick="cancelUpgrade()" class="btn-secondary btn-sm">Cancel</button>
    </div>
    <button type="button" id="upgradeBtn" class="btn-save btn-sm" style="display:none;" onclick="showUpgradeConfirm()">Upgrade</button>
  </div>
</div>

<footer style="width:100%;max-width:900px;margin-top:.25rem;text-align:center;padding:.5rem 0;">
  <a href="https://github.com/abarbaccia/avr-calibration" target="_blank" rel="noopener"
     style="color:var(--dim);font-size:.72rem;">github.com/abarbaccia/avr-calibration</a>
</footer>

<script>
// ── Global state ────────────────────────────────────────────────────────────
let frChart = null;
let convergenceChart = null;
let selectedSessionId = null;
let overlayIds = [];
let latestSession = null;
let allSessions = [];

const OVERLAY_COLORS = ['#3b82f6','#f472b6','#a78bfa','#fb923c','#34d399','#f87171'];

// ── Target curves ───────────────────────────────────────────────────────────
let targetCurveType = localStorage.getItem('targetCurve') || 'harman';

function onCurveChange() {
  targetCurveType = document.getElementById('curveSelect').value;
  localStorage.setItem('targetCurve', targetCurveType);
  refreshChart();
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
  return freqs.map(f => f >= 80 ? refSpl : refSpl + 3 * Math.log2(80 / f));
}

function curveLabel() {
  return {harman:'Harman', ht:'HT-Aggressive', music:'Musicality', flat:'Flat'}[targetCurveType] + ' Target';
}

// ── Toast ───────────────────────────────────────────────────────────────────
function toast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 2500);
}

// ── Collapsible sections ────────────────────────────────────────────────────
function toggleSection(name) {
  const body = document.getElementById(name + (name === 'hist' ? 'Section' : name === 'runs' ? 'Section' : 'Body'));
  const header = body.previousElementSibling || body.closest('.card').querySelector('.collapse-header');
  body.classList.toggle('collapsed');
  header.classList.toggle('open');
}

// ── Convergence delta table ─────────────────────────────────────────────────
const THIRD_OCT = [25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200];

function renderDeltaTable(freqs, spl) {
  const target = getTargetCurve(freqs, spl);
  const tbody = document.getElementById('deltaBody');
  if (!tbody) return;
  const rows = THIRD_OCT.map(fc => {
    let bi = 0, bd = Infinity;
    freqs.forEach((f, i) => { const d = Math.abs(f - fc); if (d < bd) { bd = d; bi = i; } });
    const measSpl = spl[bi];
    const targSpl = target[bi];
    const delta = measSpl - targSpl;
    const cls = Math.abs(delta) <= 3 ? 'ok' : Math.abs(delta) <= 6 ? 'warn' : 'bad';
    const sign = delta >= 0 ? '+' : '';
    return '<tr class="'+cls+'"><td>'+fc+' Hz</td><td>'+measSpl.toFixed(1)+'</td><td>'+targSpl.toFixed(1)+'</td><td>'+sign+delta.toFixed(1)+'</td></tr>';
  });
  tbody.innerHTML = rows.join('');
  document.getElementById('deltaCard').style.display = '';
}

// ── FR chart rendering ──────────────────────────────────────────────────────
let portTuneHz = null;
let chartData = {};  // {primary: {freqs, spl, label}, overlays: [{id, freqs, spl, label}]}

function renderChart() {
  const p = chartData.primary;
  if (!p || !p.freqs || !p.freqs.length) return;

  document.getElementById('plotCard').style.display = '';
  const info = classifyLabel(p.label);
  document.getElementById('plotStatus').textContent = info.desc + (p.label && p.label !== info.desc ? ' (' + p.label + ')' : '');

  const sel = document.getElementById('curveSelect');
  if (sel) sel.value = targetCurveType;

  const ctx = document.getElementById('frPlot').getContext('2d');
  if (frChart) frChart.destroy();

  const targetLine = getTargetCurve(p.freqs, p.spl);

  const datasets = [
    {
      label: classifyLabel(p.label).desc,
      data: p.spl,
      borderColor: OVERLAY_COLORS[0],
      backgroundColor: 'rgba(59,130,246,.08)',
      borderWidth: 2,
      pointRadius: 0,
      tension: 0.3,
      fill: chartData.overlays.length === 0,
    },
    {
      label: curveLabel(),
      data: targetLine,
      borderColor: '#94a3b8',
      borderDash: [5, 5],
      borderWidth: 1,
      pointRadius: 0,
      tension: 0,
      fill: false,
    },
  ];

  // Overlays
  chartData.overlays.forEach((ov, i) => {
    datasets.push({
      label: ov.label,
      data: ov.spl,
      borderColor: OVERLAY_COLORS[(i + 1) % OVERLAY_COLORS.length],
      borderWidth: 1.5,
      pointRadius: 0,
      tension: 0.3,
      fill: false,
    });
  });

  // Port tune marker
  if (portTuneHz && p.freqs[0] <= portTuneHz && portTuneHz <= p.freqs[p.freqs.length-1]) {
    const allSpl = [...p.spl, ...targetLine].filter(v => v != null && isFinite(v));
    datasets.push({
      label: 'Port tune (' + portTuneHz + ' Hz)',
      data: [{x: portTuneHz, y: Math.min(...allSpl)-3}, {x: portTuneHz, y: Math.max(...allSpl)+3}],
      borderColor: '#f59e0b',
      borderDash: [4, 4],
      borderWidth: 1.5,
      pointRadius: 0,
      fill: false,
      parsing: false,
    });
  }

  frChart = new Chart(ctx, {
    type: 'line',
    data: { labels: p.freqs.map(f => f.toFixed(1)), datasets },
    options: {
      animation: false,
      responsive: true,
      scales: {
        x: {
          type: 'logarithmic',
          min: p.freqs[0],
          max: p.freqs[p.freqs.length - 1],
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
      plugins: {
        legend: { labels: { color: '#94a3b8', font: { size: 11 } } },
        tooltip: { mode: 'index', intersect: false },
      }
    }
  });

  renderDeltaTable(p.freqs, p.spl);
  renderOverlayChips();
}

function refreshChart() {
  if (chartData.primary) renderChart();
}

function renderOverlayChips() {
  const el = document.getElementById('overlayChips');
  if (!chartData.overlays || chartData.overlays.length === 0) {
    el.innerHTML = '';
    return;
  }
  el.innerHTML = chartData.overlays.map((ov, i) => {
    const color = OVERLAY_COLORS[(i + 1) % OVERLAY_COLORS.length];
    return '<span class="overlay-chip" style="background:' + color + '22;color:' + color + ';border:1px solid ' + color + '">'
      + ov.label + ' <span class="remove" onclick="removeOverlay(' + ov.id + ')">&times;</span></span>';
  }).join('');
}

function removeOverlay(id) {
  overlayIds = overlayIds.filter(x => x !== id);
  chartData.overlays = chartData.overlays.filter(x => x.id !== id);
  renderChart();
  // Uncheck in history
  const cb = document.querySelector('#histBody input[data-id="'+id+'"]');
  if (cb) cb.checked = false;
  updateHistButtons();
}

// ── Load session into chart ─────────────────────────────────────────────────
async function loadSession(id) {
  selectedSessionId = id;
  const url = new URL(window.location);
  url.searchParams.set('session', id);
  history.pushState({ session: id }, '', url);

  document.querySelectorAll('#histBody tr').forEach(tr => {
    tr.classList.toggle('selected', parseInt(tr.dataset.sessionId) === id);
  });

  try {
    const r = await fetch('/api/sessions/' + id);
    if (!r.ok) return;
    const s = await r.json();
    const startFr = s.start_fr;
    if (startFr && startFr.frequencies) {
      chartData.primary = { freqs: startFr.frequencies, spl: startFr.spl, label: s.label || 'Session #' + s.id };
      chartData.overlays = chartData.overlays || [];
      renderChart();
    }
  } catch (e) {
    console.warn('loadSession error:', e);
  }
}

// ── Overlay multiple sessions ───────────────────────────────────────────────
async function overlaySelected() {
  const checked = [...document.querySelectorAll('#histBody input[type=checkbox]:checked')];
  const ids = checked.map(cb => parseInt(cb.dataset.id));
  if (ids.length === 0) return;

  try {
    const r = await fetch('/api/sessions/overlay?ids=' + ids.join(','));
    if (!r.ok) return;
    const sessions = await r.json();

    // First becomes primary if no primary set
    if (!chartData.primary && sessions.length > 0) {
      const first = sessions.shift();
      chartData.primary = { freqs: first.frequencies, spl: first.spl, label: first.label };
    }

    chartData.overlays = sessions.map(s => ({
      id: s.id, freqs: s.frequencies, spl: s.spl, label: s.label,
    }));
    overlayIds = sessions.map(s => s.id);
    renderChart();
  } catch (e) {
    console.warn('overlay error:', e);
  }
}

// ── Average ─────────────────────────────────────────────────────────────────
async function averageSelected() {
  const checked = [...document.querySelectorAll('#histBody input[type=checkbox]:checked')];
  const ids = checked.map(cb => parseInt(cb.dataset.id));
  if (ids.length < 2) return;

  try {
    const r = await fetch('/api/sessions/average', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_ids: ids }),
    });
    if (!r.ok) { toast('Average failed'); return; }
    const result = await r.json();
    chartData.primary = { freqs: result.frequencies_hz, spl: result.spl_dbfs, label: 'Average of ' + result.n_positions + ' positions' };
    chartData.overlays = [];
    renderChart();
    toast('Averaged ' + result.n_positions + ' positions');
  } catch (e) {
    toast('Average error: ' + e.message);
  }
}

// ── History ─────────────────────────────────────────────────────────────────
async function loadHistory() {
  const r = await fetch('/api/sessions');
  if (!r.ok) return;
  allSessions = await r.json();
  document.getElementById('histCount').textContent = '(' + allSessions.length + ')';

  const tbody = document.getElementById('histBody');
  tbody.innerHTML = allSessions.map(s => {
    const ts = s.timestamp.slice(0,19).replace('T',' ');
    const info = classifyLabel(s.label);
    const typeColors = {combined:'#3b82f6', solo:'#a78bfa', crawl:'#fb923c', baseline:'#64748b', iteration:'#4ade80', other:'#94a3b8', unknown:'#94a3b8'};
    const typeLabel = '<span style="display:inline-block;padding:1px 6px;border-radius:3px;font-size:.7rem;background:'+typeColors[info.type]+'22;color:'+typeColors[info.type]+';border:1px solid '+typeColors[info.type]+'44">'+info.type+'</span>';
    const peak = s.peak_spl.toFixed(1) + ' dBFS';
    let deltaStr = '\\u2014';
    let deltaCls = '';
    if (s.harman_delta_db != null) {
      deltaStr = s.harman_delta_db.toFixed(1) + ' dB';
      deltaCls = s.harman_delta_db <= 3 ? 'harman-good' : s.harman_delta_db <= 6 ? 'harman-ok' : 'harman-bad';
    }
    return '<tr class="clickable" data-session-id="'+s.id+'" onclick="loadSession('+s.id+')">'
      + '<td class="cb-col" onclick="event.stopPropagation()"><input type="checkbox" data-id="'+s.id+'" onchange="updateHistButtons()"></td>'
      + '<td>'+s.id+'</td><td>'+ts+'</td><td>'+typeLabel+'</td>'
      + '<td style="color:#38bdf8">'+peak+'</td><td class="'+deltaCls+'">'+deltaStr+'</td></tr>';
  }).join('');

  // Auto-load latest session into hero + chart
  if (allSessions.length > 0) {
    latestSession = allSessions[0];
    loadSession(latestSession.id);
    updateHero(latestSession);
  }
}

function updateHistButtons() {
  const checked = document.querySelectorAll('#histBody input[type=checkbox]:checked');
  document.getElementById('overlayBtn').style.display = checked.length >= 1 ? '' : 'none';
  document.getElementById('overlayBtn').textContent = 'Overlay ' + checked.length + ' Selected';
  document.getElementById('avgBtn').style.display = checked.length >= 2 ? '' : 'none';
  document.getElementById('avgBtn').textContent = 'Average ' + checked.length + ' Selected';
}

// ── Hero score ──────────────────────────────────────────────────────────────
function classifyLabel(label) {
  if (!label) return { type: 'unknown', desc: 'Unknown measurement' };
  const l = label.toLowerCase();
  if (l.includes('combined') || l.includes('both'))
    return { type: 'combined', desc: 'Combined sub response (all subs playing)' };
  if (l.match(/sub\\s*1|sub1|solo.*1|output.*0/i))
    return { type: 'solo', desc: 'Sub 1 solo measurement' };
  if (l.match(/sub\\s*2|sub2|solo.*2|output.*1/i))
    return { type: 'solo', desc: 'Sub 2 solo measurement' };
  if (l.includes('solo'))
    return { type: 'solo', desc: 'Solo sub measurement' };
  if (l.includes('subcrawl') || l.includes('crawl'))
    return { type: 'crawl', desc: 'Sub crawl position test' };
  if (l.includes('baseline'))
    return { type: 'baseline', desc: 'Baseline measurement (before EQ)' };
  if (l.includes('iter'))
    return { type: 'iteration', desc: 'Calibration iteration' };
  if (l === 'mcp-triggered' || l === 'headless')
    return { type: 'combined', desc: 'Combined response at listening position' };
  return { type: 'other', desc: label };
}

function updateHero(session) {
  const ring = document.getElementById('scoreRing');
  const val = document.getElementById('scoreValue');
  const label = document.getElementById('heroLabel');
  const detail = document.getElementById('heroDetail');
  const ctx = document.getElementById('heroContext');

  if (!session) return;

  const rms = session.harman_delta_db;
  if (rms != null) {
    val.textContent = rms.toFixed(1);
    ring.className = 'score-ring ' + (rms <= 2 ? 'optimal' : rms <= 4 ? 'good' : 'poor');
    label.textContent = rms <= 2 ? 'Optimal' : rms <= 4 ? 'Good' : 'Needs work';
  } else {
    val.textContent = '--';
    ring.className = 'score-ring none';
    label.textContent = 'No target comparison';
  }

  const ts = session.timestamp.slice(0,19).replace('T',' ');
  const info = classifyLabel(session.label);
  detail.textContent = 'Session #' + session.id + ' \\u2014 ' + ts;
  ctx.textContent = info.desc + (rms != null ? ' \\u2014 vs ' + curveLabel() : '');
}

// ── System status (hardware bar) ────────────────────────────────────────────
async function loadStatus() {
  try {
    const r = await fetch('/api/status');
    if (!r.ok) return;
    const data = await r.json();
    portTuneHz = data.port_tune_hz || null;

    const bar = document.getElementById('hwBar');
    bar.innerHTML = data.devices.map(d => {
      const cls = d.connected ? 'ok' : 'err';
      return '<span class="hw-item"><span class="hw-dot '+cls+'"></span>'+d.name+'</span>';
    }).join('');

    if (data.last_run) {
      const lr = data.last_run;
      bar.innerHTML += '<span class="hw-item" style="margin-left:.5rem;border-left:1px solid var(--border);padding-left:.75rem;">'
        + '<span class="hw-dot '+(lr.converged?'ok':'err')+'"></span>'
        + 'Last: '+lr.recipe_name+' '+(lr.final_rms?.toFixed(1)||'?')+' dB</span>';
    }
  } catch(e) { console.warn('status load failed:', e); }
}

// ── Calibration runs ────────────────────────────────────────────────────────
async function loadRuns() {
  try {
    const r = await fetch('/api/runs');
    if (!r.ok) return;
    const runs = await r.json();
    document.getElementById('runsCount').textContent = '(' + runs.length + ')';
    const tbody = document.getElementById('runsBody');
    tbody.innerHTML = runs.map(run => {
      const ts = run.timestamp.slice(0,19).replace('T',' ');
      const status = run.converged ? '<span class="badge badge-optimal">Converged</span>' :
                     run.error ? '<span class="badge badge-danger">Error</span>' :
                     '<span class="badge badge-warn">Max iters</span>';
      const baseline = run.baseline_rms != null ? run.baseline_rms.toFixed(1) + ' dB' : '\\u2014';
      const final_rms = run.final_rms != null ? run.final_rms.toFixed(1) + ' dB' : '\\u2014';
      return '<tr class="clickable" onclick="loadRunDetail('+run.id+')">'
        + '<td>'+run.id+'</td><td>'+ts+'</td><td>'+run.recipe_name+'</td><td>'+run.target+'</td>'
        + '<td>'+status+'</td><td>'+run.iterations_run+'</td><td>'+baseline+'</td><td>'+final_rms+'</td></tr>';
    }).join('');
    if (runs.length === 0) {
      tbody.innerHTML = '<tr><td colspan="8" style="color:var(--dim);text-align:center;">No calibration runs yet</td></tr>';
    }
  } catch(e) { console.warn('runs load failed:', e); }
}

async function loadRunDetail(runId) {
  try {
    const r = await fetch('/api/runs/' + runId);
    if (!r.ok) return;
    const detail = await r.json();
    document.getElementById('runDetailCard').style.display = '';

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
        animation: false, responsive: true,
        scales: {
          y: { title: { display: true, text: 'RMS Deviation (dB)', color: '#64748b' },
               ticks: { color: '#64748b' }, grid: { color: '#1e293b' } },
          x: { ticks: { color: '#64748b' }, grid: { color: '#1e293b' } }
        },
        plugins: { legend: { labels: { color: '#94a3b8', font: { size: 11 } } } }
      }
    });

    const tbody = document.getElementById('runIterBody');
    tbody.innerHTML = iters.map(it => {
      const safety = it.safety_ok ? '<span class="badge badge-optimal">OK</span>' :
                     '<span class="badge badge-danger" title="'+(it.safety_error||'')+'">Rejected</span>';
      const nFilters = (it.filters_applied || []).length;
      return '<tr><td>'+it.iteration+'</td><td>'+it.rms_before.toFixed(1)+' dB</td>'
        + '<td>'+it.rms_after.toFixed(1)+' dB</td><td>'+safety+'</td><td>'+nFilters+' filters</td></tr>';
    }).join('');
  } catch(e) { console.warn('run detail load failed:', e); }
}

// ── Saved states ────────────────────────────────────────────────────────────
async function loadStates() {
  try {
    const r = await fetch('/api/states');
    if (!r.ok) return;
    const states = await r.json();
    document.getElementById('statesCount').textContent = '(' + states.length + ')';
    const list = document.getElementById('stateList');

    if (states.length === 0) {
      list.innerHTML = '<div style="color:var(--dim);font-size:.82rem;">No saved states yet. Click "Save State" above the chart to save your current DSP configuration.</div>';
      return;
    }

    list.innerHTML = states.map(s => {
      const ts = s.timestamp.slice(0,19).replace('T',' ');
      const rms = s.rms_deviation != null ? s.rms_deviation.toFixed(1) + ' dB RMS' : '';
      const curve = s.target_curve || '';
      const meta = [curve, rms, ts].filter(Boolean).join(' \\u2014 ');
      return '<div class="state-item">'
        + '<div class="state-info"><div class="state-name">'+s.name+'</div>'
        + '<div class="state-meta">'+meta+(s.notes ? ' \\u2014 '+s.notes : '')+'</div></div>'
        + '<div class="state-actions">'
        + (s.measurement_session_id ? '<button class="btn-secondary btn-sm" onclick="loadSession('+s.measurement_session_id+')">View</button>' : '')
        + '<button class="btn-danger btn-sm" onclick="deleteState('+s.id+')">Delete</button>'
        + '</div></div>';
    }).join('');
  } catch(e) { console.warn('states load failed:', e); }
}

function showSaveModal() {
  document.getElementById('saveModal').classList.remove('hidden');
  document.getElementById('stateName').focus();
}

function closeSaveModal() {
  document.getElementById('saveModal').classList.add('hidden');
  document.getElementById('stateName').value = '';
  document.getElementById('stateNotes').value = '';
}

async function doSaveState() {
  const name = document.getElementById('stateName').value.trim();
  if (!name) { toast('Please enter a name'); return; }
  const notes = document.getElementById('stateNotes').value.trim() || null;

  const body = {
    name,
    notes,
    target_curve: targetCurveType,
    measurement_session_id: selectedSessionId,
    rms_deviation: latestSession?.harman_delta_db || null,
  };

  try {
    const r = await fetch('/api/states', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) { toast('Save failed'); return; }
    closeSaveModal();
    toast('State saved: ' + name);
    loadStates();
  } catch(e) { toast('Save error: ' + e.message); }
}

async function deleteState(id) {
  if (!confirm('Delete this saved state?')) return;
  try {
    await fetch('/api/states/' + id, { method: 'DELETE' });
    toast('State deleted');
    loadStates();
  } catch(e) { toast('Delete error'); }
}

// ── Export chart ─────────────────────────────────────────────────────────────
function exportChart() {
  if (!frChart) return;
  const canvas = document.getElementById('frPlot');
  const link = document.createElement('a');
  link.download = 'fr-session-' + (selectedSessionId || 'unknown') + '.png';
  link.href = canvas.toDataURL('image/png');
  link.click();
}

// ── Version ─────────────────────────────────────────────────────────────────
let _upgradePolling = false;

function _setChip(text, cls, title) {
  const chip = document.getElementById('versionChip');
  if (!chip) return;
  chip.textContent = text;
  chip.classList.remove('up-to-date', 'update-available');
  if (cls) chip.classList.add(cls);
  chip.title = title;
}

async function loadVersion() {
  try {
    const r = await fetch('/api/version');
    if (!r.ok) throw new Error(r.statusText);
    const d = await r.json();
    const sha7 = d.current_sha !== 'unknown' ? d.current_sha.slice(0,7) : 'unknown';
    const semver = (d.semantic_version && d.semantic_version !== 'unknown') ? d.semantic_version : sha7;
    const badge = document.getElementById('versionBadge');
    const btn = document.getElementById('upgradeBtn');
    const confirm = document.getElementById('upgradeConfirm');

    if (d.up_to_date) {
      badge.innerHTML = '<span class="badge badge-optimal">v'+sha7+' \\u2014 Up to date</span>';
      btn.style.display = 'none'; confirm.style.display = 'none';
      _setChip('v'+semver, 'up-to-date', 'v'+semver+' ('+sha7+') \\u2014 Up to date');
    } else if (d.latest_sha) {
      badge.innerHTML = '<span class="badge badge-warn">v'+sha7+' \\u2014 Update available</span>';
      btn.style.display = ''; confirm.style.display = 'none';
      _setChip('v'+semver+' \\u25b2', 'update-available', 'Update available');
    } else if (d.checking) {
      badge.innerHTML = '<span class="badge badge-empty">v'+sha7+'</span>';
      btn.style.display = 'none'; confirm.style.display = 'none';
      _setChip('v'+semver, null, 'Checking...');
      setTimeout(loadVersion, 8000);
    } else {
      badge.innerHTML = '<span class="badge badge-empty">v'+sha7+'</span>';
      btn.style.display = 'none'; confirm.style.display = 'none';
      _setChip('v'+semver, null, 'v'+semver);
    }
  } catch (e) {
    document.getElementById('versionBadge').innerHTML = '<span class="badge badge-empty">Version unavailable</span>';
    _setChip('\\u2014', null, 'Version unavailable');
  }
}

function showUpgradeConfirm() {
  document.getElementById('upgradeBtn').style.display = 'none';
  document.getElementById('upgradeConfirm').style.display = '';
}

function cancelUpgrade() {
  document.getElementById('upgradeConfirm').style.display = 'none';
  document.getElementById('upgradeBtn').style.display = '';
}

async function confirmUpgrade() {
  document.getElementById('upgradeConfirm').style.display = 'none';
  const badge = document.getElementById('versionBadge');
  const status = document.getElementById('versionStatus');
  badge.innerHTML = '<span class="badge badge-upgrading">Upgrading...</span>';
  status.textContent = 'Upgrading...'; status.style.display = '';
  _upgradePolling = true;

  try {
    const r = await fetch('/api/upgrade', {method: 'POST'});
    if (r.status === 409) { status.textContent = 'Upgrade already in progress.'; return; }
    if (!r.ok) { status.textContent = 'Upgrade failed'; _upgradePolling = false; loadVersion(); return; }
  } catch (e) { status.textContent = 'Upgrade request failed'; _upgradePolling = false; loadVersion(); return; }

  const startTime = Date.now();
  async function poll() {
    if (!_upgradePolling) return;
    const elapsed = Math.round((Date.now() - startTime) / 1000);
    status.textContent = 'Upgrading... ' + elapsed + 's';
    if (elapsed >= 180) { status.textContent = 'Taking too long. Check Pi.'; _upgradePolling = false; loadVersion(); return; }
    try {
      const h = await fetch('/health', {cache: 'no-store'});
      if (h.ok && elapsed >= 5) { status.textContent = 'Updated! Reloading...'; setTimeout(() => window.location.reload(), 2000); return; }
    } catch (_) {}
    setTimeout(poll, 3000);
  }
  setTimeout(poll, 5000);
}

// ── Boot ────────────────────────────────────────────────────────────────────
chartData = { primary: null, overlays: [] };

loadHistory();
loadStatus();
loadRuns();
loadStates();
loadVersion();

setInterval(loadStatus, 30000);

window.addEventListener('popstate', (e) => {
  const id = e.state && e.state.session;
  if (id) loadSession(id);
});
</script>

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

    MeasurementEngine.measure() handles UMIK selection and route-aware playback.
    Denon lifecycle (input/volume switching) is managed here via DenonSweepContext
    when the HDMI route is configured.

    Requires UMIK-1 connected and the 'measurement' extra installed (arm64/amd64 images).
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

    async with _measurement_lock:
        engine = MeasurementEngine(cfg)
        try:
            denon_ctx = DenonSweepContext.from_config(cfg)
            if denon_ctx:
                async with denon_ctx:
                    fr = await engine.measure()
            else:
                fr = await engine.measure()
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc))

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


@app.get("/api/sessions/overlay")
async def overlay_sessions(ids: str) -> list[dict]:
    """Return FR data for multiple sessions, for overlay charting.

    Query: /api/sessions/overlay?ids=1,2,3
    """
    try:
        session_ids = [int(x.strip()) for x in ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(status_code=422, detail="ids must be comma-separated integers")

    store = SessionStore()
    results = []
    for sid in session_ids[:6]:  # cap at 6 overlays
        session = store.get_session(sid)
        if session is None:
            continue
        fr = session.start_fr
        if not fr or not fr.frequencies:
            continue
        results.append({
            "id": session.id,
            "label": session.label or f"Session #{session.id}",
            "timestamp": session.timestamp,
            "frequencies": fr.frequencies,
            "spl": fr.spl,
        })
    return results


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

    # Denon (via DenonDriver abstraction)
    denon_host = cfg.denon.get("host")
    if denon_host:
        try:
            driver = DenonDriver(denon_host)
            state = await driver.get_state()
            if state.get("connected"):
                devices.append({
                    "name": "Denon AVR",
                    "connected": True,
                    "detail": f"Input: {state.get('input')}, Volume: {state.get('volume')} dB",
                })
            else:
                devices.append({"name": "Denon AVR", "connected": False, "detail": denon_host})
        except Exception:
            devices.append({"name": "Denon AVR", "connected": False, "detail": denon_host})

    # miniDSP
    minidsp_host, minidsp_port = cfg.minidsp_host_port
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"http://{minidsp_host}:{minidsp_port}/devices/0")
            if r.status_code == 200:
                data = r.json()
                master = data.get("master", {})
                devices.append({
                    "name": "miniDSP 2x4 HD",
                    "connected": True,
                    "detail": f"Preset: {master.get('preset', '?')}, Source: {master.get('source', '?')}",
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

    port_tune_hz = cfg.sub.get("port_tune_hz")
    return {"devices": devices, "last_run": last_run, "port_tune_hz": port_tune_hz}


# ── Saved states ─────────────────────────────────────────────────────────────


class SaveStateRequest(BaseModel):
    name: str
    eq_filters: list[dict] | None = None
    delays: dict | None = None
    polarities: dict | None = None
    gains: dict | None = None
    target_curve: str | None = None
    rms_deviation: float | None = None
    measurement_session_id: int | None = None
    notes: str | None = None


@app.get("/api/states")
async def list_states() -> list[dict]:
    """List all saved DSP states."""
    store = SessionStore()
    return store.list_states()


@app.post("/api/states")
async def save_state(body: SaveStateRequest) -> dict:
    """Save the current DSP state as a named snapshot."""
    store = SessionStore()
    state_id = store.save_state(
        name=body.name,
        eq_filters=body.eq_filters,
        delays=body.delays,
        polarities=body.polarities,
        gains=body.gains,
        target_curve=body.target_curve,
        rms_deviation=body.rms_deviation,
        measurement_session_id=body.measurement_session_id,
        notes=body.notes,
    )
    return {"id": state_id, "status": "saved"}


@app.get("/api/states/{state_id}")
async def get_state(state_id: int) -> dict:
    """Return full details for a saved state."""
    store = SessionStore()
    state = store.get_state(state_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"State #{state_id} not found")
    return state


@app.delete("/api/states/{state_id}")
async def delete_state(state_id: int) -> dict:
    """Delete a saved state."""
    store = SessionStore()
    if not store.delete_state(state_id):
        raise HTTPException(status_code=404, detail=f"State #{state_id} not found")
    return {"status": "deleted"}


# ── Config helper ─────────────────────────────────────────────────────────────

def _load_config() -> Config:
    if not CONFIG_PATH.exists():
        raise HTTPException(
            status_code=503,
            detail=f"No config at {CONFIG_PATH}. Run 'calibrate check' first.",
        )
    return Config.load(CONFIG_PATH)
