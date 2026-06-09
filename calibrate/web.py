"""FastAPI web server — read-only calibration dashboard.

Shows system status, measurement history, calibration runs, and FR plots.
Observation deck for AI-driven calibration — Claude drives measurements via MCP.
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
from .drivers.registry import load_drivers_from_graph
from .graph import default_display_name
from .measurement import FrequencyResponse
from .measurement_client import MeasurementServiceClient
from .storage import SessionStore

app = FastAPI(title="avr-calibration")



# ── Constants ─────────────────────────────────────────────────────────────────

_GHCR_REGISTRY = "ghcr.io"
_GHCR_IMAGE = "abarbaccia/avr-calibration"
_VERSION_CACHE_TTL = 3600
_version_cache: dict = {}
_DATA_DIR = Path(os.environ.get("HOME", "/data")) / ".avr-calibration"


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
      --bg: #0d0f14; --card: #1a1f2e; --card2: #151a27; --border: #2d3748;
      --text: #e2e8f0; --muted: #94a3b8; --dim: #64748b;
      --accent: #3b82f6; --green: #4ade80; --yellow: #fbbf24;
      --red: #f87171; --teal: #2dd4bf;
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
    .header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 1rem; }
    .header h1 { font-size: 1.2rem; font-weight: 600; color: var(--muted); letter-spacing: .04em; text-transform: uppercase; }
    #versionChip {
      font-size: .7rem; font-weight: 600; letter-spacing: .03em;
      background: var(--card); border: 1px solid var(--border); border-radius: 999px;
      padding: .2rem .6rem; color: var(--muted); cursor: pointer; white-space: nowrap;
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

    /* ── Hardware status bar ── */
    .hw-bar-card {
      background: var(--card); border: 1px solid var(--border); border-radius: 12px;
      padding: .75rem 1.25rem; margin-bottom: 1rem;
    }
    .hw-bar { display: flex; gap: 1rem; flex-wrap: wrap; align-items: center; }
    .hw-item { display: flex; align-items: center; gap: .35rem; font-size: .78rem; color: var(--muted); }
    .hw-dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
    .hw-dot.ok { background: var(--green); box-shadow: 0 0 4px var(--green); }
    .hw-dot.err { background: var(--red); box-shadow: 0 0 4px var(--red); }
    .hw-detail { font-size: .7rem; color: var(--dim); }

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
    .trend-up { color: var(--green); }
    .trend-down { color: var(--red); }
    .trend-flat { color: var(--dim); }

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
    .badge-run     { background: rgba(59,130,246,.12); color: var(--accent); border: 1px solid rgba(59,130,246,.3); font-size: .68rem; }

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

    /* ── DSP state ── */
    .dsp-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: .75rem; margin-top: .5rem; }
    .dsp-output {
      background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
      padding: .6rem .75rem;
    }
    .dsp-output-header { display: flex; align-items: center; gap: .4rem; margin-bottom: .4rem; }
    .dsp-output-label { font-weight: 600; font-size: .85rem; }
    .dsp-output-type { font-size: .68rem; color: var(--dim); }
    .dsp-param { font-size: .75rem; color: var(--muted); margin-bottom: .15rem; }
    .dsp-param span { color: var(--text); }
    .dsp-filter-list { font-size: .72rem; color: var(--muted); margin-top: .3rem; }
    .dsp-filter-list .f-row { display: flex; gap: .5rem; padding: .1rem 0; border-bottom: 1px solid #1a2030; }
    .dsp-filter-list .f-row:last-child { border-bottom: none; }
    .dsp-section-label { font-size: .75rem; font-weight: 600; color: var(--dim); text-transform: uppercase; letter-spacing: .05em; margin-bottom: .4rem; }

    /* ── Activity timeline ── */
    .timeline { display: flex; flex-direction: column; gap: .25rem; }
    .timeline-event {
      display: flex; align-items: flex-start; gap: .6rem;
      padding: .35rem 0; border-bottom: 1px solid rgba(45,55,72,.4);
      font-size: .78rem;
    }
    .timeline-event:last-child { border-bottom: none; }
    .timeline-time { color: var(--dim); font-size: .7rem; white-space: nowrap; min-width: 3.5rem; }
    .timeline-dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; margin-top: 5px; }
    .timeline-dot.measurement { background: var(--accent); }
    .timeline-dot.run_start { background: var(--yellow); }
    .timeline-dot.run_complete { background: var(--green); }
    .timeline-dot.eq_applied { background: var(--teal); }
    .timeline-summary { color: var(--text); flex: 1; }

    /* ── Tabs ── */
    .tab-bar { display: flex; gap: 0; margin-bottom: 1rem; border-bottom: 2px solid var(--border); }
    .tab-btn {
      padding: .5rem 1.25rem; font-size: .82rem; font-weight: 500; cursor: pointer;
      background: none; border: none; color: var(--dim); border-bottom: 2px solid transparent;
      margin-bottom: -2px; transition: color .15s, border-color .15s;
    }
    .tab-btn:hover { color: var(--text); }
    .tab-btn.active { color: var(--accent); border-bottom-color: var(--accent); }
    .tab-panel { display: none; }
    .tab-panel.active { display: block; }

    /* ── Run cards ── */
    .run-card {
      background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
      margin-bottom: .5rem; overflow: hidden;
    }
    .run-card-header {
      display: flex; align-items: center; gap: .75rem; padding: .6rem .75rem;
      cursor: pointer; user-select: none; flex-wrap: wrap;
    }
    .run-card-header:hover { background: #1e2535; }
    .run-card-arrow { font-size: .7rem; color: var(--dim); transition: transform .2s; flex-shrink: 0; }
    .run-card-arrow.open { transform: rotate(90deg); }
    .run-card-title { font-size: .85rem; font-weight: 500; flex: 1; }
    .run-card-rms { font-size: .78rem; font-weight: 600; }
    .run-card-body { padding: .5rem .75rem .75rem; border-top: 1px solid var(--border); }
    .run-card-meta { font-size: .75rem; color: var(--dim); margin-bottom: .5rem; }
    .run-sessions { display: flex; gap: .35rem; flex-wrap: wrap; margin-top: .35rem; }
    .run-session-chip {
      display: inline-block; padding: .15rem .45rem; border-radius: 4px; font-size: .7rem;
      background: rgba(59,130,246,.1); color: var(--accent); border: 1px solid rgba(59,130,246,.25);
      cursor: pointer;
    }
    .run-session-chip:hover { background: rgba(59,130,246,.2); }

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
    .modal input[type=text], .modal textarea {
      width: 100%; padding: .5rem .75rem; background: var(--bg); border: 1px solid var(--border);
      border-radius: 6px; color: var(--text); font-size: .9rem; margin-bottom: .75rem;
      font-family: inherit;
    }
    .modal input[type=text]:focus, .modal textarea:focus { outline: none; border-color: var(--accent); }
    .modal textarea { resize: vertical; min-height: 60px; }
    .modal-actions { display: flex; justify-content: flex-end; gap: .5rem; }

    /* ── Feedback ── */
    .feedback-bar {
      display: flex; align-items: center; gap: .5rem; margin-top: .5rem;
      padding: .4rem 0; font-size: .78rem;
    }
    .feedback-btn {
      padding: .2rem .5rem; border-radius: 4px; font-size: .85rem; cursor: pointer;
      background: var(--bg); border: 1px solid var(--border); color: var(--muted);
      transition: all .15s;
    }
    .feedback-btn:hover { border-color: var(--accent); color: var(--text); }
    .feedback-btn.active-up { border-color: var(--green); color: var(--green); background: rgba(74,222,128,.1); }
    .feedback-btn.active-down { border-color: var(--red); color: var(--red); background: rgba(248,113,113,.1); }
    .feedback-existing { font-size: .72rem; color: var(--dim); font-style: italic; }

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

    /* ── Ad-hoc group ── */
    .adhoc-group { margin-top: .75rem; }
    .adhoc-header { font-size: .78rem; color: var(--dim); font-weight: 500; margin-bottom: .35rem; }
    .adhoc-item {
      display: flex; align-items: center; gap: .5rem; padding: .3rem .5rem;
      font-size: .78rem; cursor: pointer; border-radius: 4px;
    }
    .adhoc-item:hover { background: #1e2535; }
  </style>
</head>
<body>

<div class="container">
  <!-- Header -->
  <div class="header">
    <h1><a href="/" style="color:inherit;text-decoration:none">AVR Calibration</a></h1>
    <span id="versionChip" title="Running version" onclick="toggleVersionPopover()">...</span>
  </div>

  <!-- Hardware Status Bar -->
  <div class="hw-bar-card">
    <div class="hw-bar" id="hwBar">Loading hardware status...</div>
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
        <div class="detail" id="heroContext" style="font-size:.75rem;color:var(--dim);margin-top:-.25rem"></div>
      </div>
    </div>
  </div>

  <!-- Active DSP State (always visible) -->
  <div class="card" id="dspCard" style="display:none">
    <h2>Active DSP State</h2>
    <div id="dspContent" style="font-size:.82rem;"></div>
  </div>

  <!-- FR Plot -->
  <div class="card" id="plotCard" style="display:none">
    <div class="chart-controls">
      <label for="curveSelect">Compare:</label>
      <select id="curveSelect">
        <option value="">Add curve...</option>
        <option value="harman">Harman</option>
        <option value="ht">HT-Aggressive</option>
        <option value="music">Musicality</option>
        <option value="flat">Flat</option>
      </select>
      <button class="btn-secondary btn-sm" onclick="onAddComparison()">Add</button>
      <div style="flex:1"></div>
      <button class="btn-save btn-sm" onclick="showSaveModal()">Save State</button>
      <button class="btn-secondary btn-sm" onclick="exportChart()">Export PNG</button>
    </div>
    <div class="overlay-chips" id="overlayChips"></div>
    <canvas id="frPlot"></canvas>
    <p id="plotStatus"></p>
    <!-- Feedback bar for current session -->
    <div class="feedback-bar" id="feedbackBar" style="display:none">
      <span style="color:var(--dim)">How does it sound?</span>
      <button class="feedback-btn" id="fbUp" onclick="submitFeedback('up')">&#x1f44d;</button>
      <button class="feedback-btn" id="fbDown" onclick="submitFeedback('down')">&#x1f44e;</button>
      <input type="text" id="fbText" placeholder="Optional: too boomy, thin, etc." style="flex:1;padding:.25rem .5rem;background:var(--bg);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:.75rem;">
      <span class="feedback-existing" id="fbExisting"></span>
    </div>
  </div>

  <!-- Convergence Delta (shown when viewing a session) -->
  <div class="card" id="deltaCard" style="display:none">
    <h2>Convergence vs Target</h2>
    <table class="delta-tbl" id="deltaTable">
      <thead><tr><th>Band (Hz)</th><th>SPL</th><th>Target</th><th>Delta</th></tr></thead>
      <tbody id="deltaBody"></tbody>
    </table>
  </div>

  <!-- Activity Timeline -->
  <div class="card" id="activityCard">
    <div class="collapse-header open" onclick="toggleSection('activity')">
      <h2>Activity</h2>
      <span class="arrow" id="activityArrow">&#9654;</span>
    </div>
    <div class="collapse-body" id="activityBody">
      <div class="timeline" id="activityTimeline">
        <div style="color:var(--dim);font-size:.82rem;">Loading activity...</div>
      </div>
    </div>
  </div>

  <!-- Tab bar: Runs | Sessions -->
  <div class="tab-bar">
    <button class="tab-btn active" data-tab="runs" onclick="switchTab('runs')">Runs</button>
    <button class="tab-btn" data-tab="sessions" onclick="switchTab('sessions')">Sessions</button>
  </div>

  <!-- Runs tab -->
  <div class="tab-panel active" id="tab-runs">
    <div id="runsContent">
      <div style="color:var(--dim);font-size:.82rem;">Loading calibration runs...</div>
    </div>
  </div>

  <!-- Sessions tab -->
  <div class="tab-panel" id="tab-sessions">
    <div style="display:flex;gap:.5rem;margin-bottom:.5rem;">
      <button class="btn-secondary btn-sm" id="overlayBtn" style="display:none" onclick="overlaySelected()">Overlay Selected</button>
      <button class="btn-secondary btn-sm" id="avgBtn" style="display:none" onclick="averageSelected()">Average Selected</button>
    </div>
    <table id="histTable">
      <thead>
        <tr><th class="cb-col"></th><th>#</th><th>Date</th><th>Type</th><th>Peak SPL</th><th>&Delta; Target</th><th>Run</th></tr>
      </thead>
      <tbody id="histBody"></tbody>
    </table>
  </div>

  <!-- Run Detail (convergence chart) -->
  <div class="card" id="runDetailCard" style="display:none">
    <h2>Run Detail</h2>
    <canvas id="convergenceChart" height="200"></canvas>
    <table style="margin-top:.75rem"><thead><tr>
      <th>Iter</th><th>RMS Before</th><th>RMS After</th><th>Safety</th><th>Filters</th>
    </tr></thead><tbody id="runIterBody"></tbody></table>
  </div>

  <!-- Saved States -->
  <div class="card" id="statesCard">
    <div class="collapse-header" onclick="toggleSection('states')">
      <h2>Saved States <span class="count" id="statesCount"></span></h2>
      <span class="arrow" id="statesArrow">&#9654;</span>
    </div>
    <div class="collapse-body collapsed" id="statesBody">
      <div class="state-list" id="stateList">
        <div style="color:var(--dim);font-size:.82rem;">No saved states yet</div>
      </div>
    </div>
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

<footer style="width:100%;max-width:900px;margin-top:.5rem;text-align:center;padding:.5rem 0;">
  <span id="versionFooterText" style="color:var(--dim);font-size:.72rem;"></span>
  <span style="color:var(--dim);font-size:.72rem;"> &middot; </span>
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
let previousRms = null;

const OVERLAY_COLORS = ['#3b82f6','#f472b6','#a78bfa','#fb923c','#34d399','#f87171'];

// ── Target curves ───────────────────────────────────────────────────────────
let storedTarget = null;
const HARMAN_TABLE = {20:6, 25:5, 31.5:4, 40:3, 50:2, 63:1, 80:0, 100:0, 125:0, 160:-1, 200:-2};

function harmanOffset(f) {
  const keys = Object.keys(HARMAN_TABLE).map(Number).sort((a,b)=>a-b);
  if (f <= keys[0]) return HARMAN_TABLE[keys[0]];
  if (f >= keys[keys.length-1]) return HARMAN_TABLE[keys[keys.length-1]];
  for (let i = 0; i < keys.length-1; i++) {
    if (f >= keys[i] && f <= keys[i+1]) {
      const t = (Math.log(f)-Math.log(keys[i]))/(Math.log(keys[i+1])-Math.log(keys[i]));
      return HARMAN_TABLE[keys[i]] + t * (HARMAN_TABLE[keys[i+1]] - HARMAN_TABLE[keys[i]]);
    }
  }
  return 0;
}

let comparisonCurves = [];
function onAddComparison() {
  const sel = document.getElementById('curveSelect');
  const type = sel.value;
  if (!type || comparisonCurves.includes(type)) return;
  comparisonCurves.push(type);
  refreshChart();
}

function removeComparison(type) {
  comparisonCurves = comparisonCurves.filter(c => c !== type);
  refreshChart();
}

function buildComparisonCurve(type, freqs, refSpl) {
  if (type === 'flat') return freqs.map(f => ({x: f, y: refSpl}));
  if (type === 'ht') return freqs.map(f => ({x: f, y: f >= 100 ? refSpl : refSpl + 4 * Math.log2(100 / f)}));
  if (type === 'music') return freqs.map(f => {
    const oct = Math.log2(f / 30);
    return {x: f, y: refSpl + 4 * Math.exp(-(oct * oct) / (2 * 0.7 * 0.7))};
  });
  return freqs.map(f => ({x: f, y: refSpl + harmanOffset(f)}));
}

const COMPARISON_COLORS = {harman:'#94a3b8', flat:'#64748b', ht:'#fb923c', music:'#a78bfa'};
const COMPARISON_LABELS = {harman:'Harman', flat:'Flat', ht:'HT-Aggressive', music:'Musicality'};

// ── Toast ───────────────────────────────────────────────────────────────────
function toast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 2500);
}

// ── Collapsible sections ────────────────────────────────────────────────────
function toggleSection(name) {
  const body = document.getElementById(name + 'Body');
  const header = body.previousElementSibling || body.closest('.card').querySelector('.collapse-header');
  body.classList.toggle('collapsed');
  header.classList.toggle('open');
}

// ── Tabs ────────────────────────────────────────────────────────────────────
function switchTab(tab) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.id === 'tab-' + tab));
}

// ── Convergence delta table ─────────────────────────────────────────────────
const THIRD_OCT = [25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200];

function renderDeltaTable(freqs, spl) {
  const tbody = document.getElementById('deltaBody');
  if (!tbody || !storedTarget || !storedTarget.points || storedTarget.points.length === 0) {
    document.getElementById('deltaCard').style.display = 'none';
    return;
  }
  const tPts = storedTarget.points;
  const rows = THIRD_OCT.map(fc => {
    let bi = 0, bd = Infinity;
    freqs.forEach((f, i) => { const d = Math.abs(f - fc); if (d < bd) { bd = d; bi = i; } });
    let ti = 0, td = Infinity;
    tPts.forEach((pt, i) => { const d = Math.abs(pt.freq - fc); if (d < td) { td = d; ti = i; } });
    const measSpl = spl[bi];
    const targSpl = tPts[ti].spl;
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
let chartData = {};

function toXY(freqs, spl) {
  return freqs.map((f, i) => ({x: f, y: spl[i]}));
}

function classifyLabel(label) {
  if (!label) return { type: 'unknown', desc: 'Unknown measurement', position: null };
  const l = label.toLowerCase();
  let position = null;
  const atMatch = label.match(/@\\s*(.+)$/);
  if (atMatch) position = atMatch[1].trim();
  const pos = position ? ' at ' + position : '';
  if (l.includes('sub1-solo') || l.match(/sub\\s*1.*solo/))
    return { type: 'solo', desc: 'Sub 1 solo' + pos, position };
  if (l.includes('sub2-solo') || l.match(/sub\\s*2.*solo/))
    return { type: 'solo', desc: 'Sub 2 solo' + pos, position };
  if (l.includes('solo'))
    return { type: 'solo', desc: 'Solo sub' + pos, position };
  if (l.includes('subcrawl') || l.includes('crawl'))
    return { type: 'crawl', desc: 'Sub crawl' + pos, position };
  if (l.includes('baseline'))
    return { type: 'baseline', desc: 'Baseline (before EQ)' + pos, position };
  if (l.match(/iter-?\\d|iteration/))
    return { type: 'iteration', desc: 'Calibration iteration' + pos, position };
  if (l.includes('combined'))
    return { type: 'combined', desc: 'Combined response' + pos, position };
  if (l === 'mcp-triggered' || l === 'headless')
    return { type: 'combined', desc: 'Combined response' + pos, position };
  return { type: 'other', desc: label, position };
}

function renderChart() {
  const p = chartData.primary;
  if (!p || !p.freqs || !p.freqs.length) return;

  document.getElementById('plotCard').style.display = '';
  const info = classifyLabel(p.label);
  document.getElementById('plotStatus').textContent = info.desc + (p.label && p.label !== info.desc ? ' (' + p.label + ')' : '');

  const ctx = document.getElementById('frPlot').getContext('2d');
  if (frChart) frChart.destroy();

  const datasets = [
    {
      label: classifyLabel(p.label).desc,
      data: toXY(p.freqs, p.spl),
      borderColor: OVERLAY_COLORS[0],
      backgroundColor: 'rgba(59,130,246,.08)',
      borderWidth: 2,
      pointRadius: 0,
      tension: 0.3,
      fill: chartData.overlays.length === 0,
    },
  ];

  if (storedTarget && storedTarget.points && storedTarget.points.length > 0) {
    datasets.push({
      label: (storedTarget.type || 'Target').charAt(0).toUpperCase() + (storedTarget.type || 'target').slice(1) + ' Target',
      data: storedTarget.points.map(pt => ({x: pt.freq, y: pt.spl})),
      borderColor: '#4ade80',
      borderDash: [6, 3],
      borderWidth: 2,
      pointRadius: 0,
      tension: 0,
      fill: false,
    });
  }

  const compRef = (() => {
    let bi = 0, bd = Infinity;
    p.freqs.forEach((f, i) => { const d = Math.abs(f - 80); if (d < bd) { bd = d; bi = i; } });
    return p.spl[bi];
  })();
  if (comparisonCurves.length > 0) {
    comparisonCurves.forEach(type => {
      datasets.push({
        label: COMPARISON_LABELS[type] || type,
        data: buildComparisonCurve(type, p.freqs, compRef),
        borderColor: COMPARISON_COLORS[type] || '#64748b',
        borderDash: [3, 3],
        borderWidth: 1,
        pointRadius: 0,
        tension: 0,
        fill: false,
      });
    });
  }

  chartData.overlays.forEach((ov, i) => {
    datasets.push({
      label: ov.label,
      data: toXY(ov.freqs, ov.spl),
      borderColor: OVERLAY_COLORS[(i + 1) % OVERLAY_COLORS.length],
      borderWidth: 1.5,
      pointRadius: 0,
      tension: 0.3,
      fill: false,
    });
  });

  if (portTuneHz && p.freqs[0] <= portTuneHz && portTuneHz <= p.freqs[p.freqs.length-1]) {
    const targetSpl = (storedTarget && storedTarget.points) ? storedTarget.points.map(pt => pt.spl) : [];
    const allSplVals = [...p.spl, ...targetSpl].filter(v => v != null && isFinite(v));
    datasets.push({
      label: 'Port tune (' + portTuneHz + ' Hz)',
      data: [{x: portTuneHz, y: Math.min(...allSplVals)-3}, {x: portTuneHz, y: Math.max(...allSplVals)+3}],
      borderColor: '#f59e0b',
      borderDash: [4, 4],
      borderWidth: 1.5,
      pointRadius: 0,
      fill: false,
    });
  }

  frChart = new Chart(ctx, {
    type: 'line',
    data: { datasets },
    options: {
      animation: false,
      responsive: true,
      parsing: { xAxisKey: 'x', yAxisKey: 'y' },
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

function refreshChart() { if (chartData.primary) renderChart(); }

function renderOverlayChips() {
  const el = document.getElementById('overlayChips');
  const compChips = comparisonCurves.map(type => {
    const color = COMPARISON_COLORS[type] || '#64748b';
    return '<span class="overlay-chip" style="background:' + color + '22;color:' + color + ';border:1px solid ' + color + '">'
      + (COMPARISON_LABELS[type] || type) + ' <span class="remove" data-type="' + type + '" onclick="removeComparison(this.dataset.type)">&times;</span></span>';
  });
  const overlayChips = (chartData.overlays || []).map((ov, i) => {
    const color = OVERLAY_COLORS[(i + 1) % OVERLAY_COLORS.length];
    return '<span class="overlay-chip" style="background:' + color + '22;color:' + color + ';border:1px solid ' + color + '">'
      + ov.label + ' <span class="remove" onclick="removeOverlay(' + ov.id + ')">&times;</span></span>';
  });
  el.innerHTML = [...compChips, ...overlayChips].join('');
}

function removeOverlay(id) {
  overlayIds = overlayIds.filter(x => x !== id);
  chartData.overlays = chartData.overlays.filter(x => x.id !== id);
  renderChart();
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
    storedTarget = s.target_curve || null;
    const startFr = s.start_fr;
    if (startFr && startFr.frequencies) {
      chartData.primary = { freqs: startFr.frequencies, spl: startFr.spl, label: s.label || 'Session #' + s.id };
      chartData.overlays = chartData.overlays || [];
      renderChart();
    }
    // Show feedback bar
    document.getElementById('feedbackBar').style.display = '';
    loadFeedback(id);
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

// ── History (Sessions tab) ──────────────────────────────────────────────────
async function loadHistory() {
  const r = await fetch('/api/sessions');
  if (!r.ok) return;
  allSessions = await r.json();

  const tbody = document.getElementById('histBody');
  const typeColors = {combined:'#3b82f6', solo:'#a78bfa', crawl:'#fb923c', baseline:'#64748b', iteration:'#4ade80', other:'#94a3b8', unknown:'#94a3b8'};

  tbody.innerHTML = allSessions.map(s => {
    const ts = s.timestamp.slice(0,19).replace('T',' ');
    const info = classifyLabel(s.label);
    const typeLabel = '<span style="display:inline-block;padding:1px 6px;border-radius:3px;font-size:.7rem;background:'+typeColors[info.type]+'22;color:'+typeColors[info.type]+';border:1px solid '+typeColors[info.type]+'44">'+info.type+'</span>';
    const peak = s.peak_spl.toFixed(1) + ' dBFS';
    let deltaStr = '\u2014';
    let deltaCls = '';
    if (s.harman_delta_db != null) {
      deltaStr = s.harman_delta_db.toFixed(1) + ' dB';
      deltaCls = s.harman_delta_db <= 3 ? 'harman-good' : s.harman_delta_db <= 6 ? 'harman-ok' : 'harman-bad';
    }
    const runBadge = s.run_context
      ? '<span class="badge badge-run">Run #'+s.run_context.run_id+'</span>'
      : '<span style="font-size:.68rem;color:var(--dim)">Ad-hoc</span>';
    return '<tr class="clickable" data-session-id="'+s.id+'" onclick="loadSession('+s.id+')">'
      + '<td class="cb-col" onclick="event.stopPropagation()"><input type="checkbox" data-id="'+s.id+'" onchange="updateHistButtons()"></td>'
      + '<td>'+s.id+'</td><td>'+ts+'</td><td>'+typeLabel+'</td>'
      + '<td style="color:#38bdf8">'+peak+'</td><td class="'+deltaCls+'">'+deltaStr+'</td>'
      + '<td>'+runBadge+'</td></tr>';
  }).join('');

  // Update Sessions tab count
  document.querySelector('[data-tab="sessions"]').textContent = 'Sessions (' + allSessions.length + ')';

  // Auto-load latest session
  if (allSessions.length > 0) {
    latestSession = allSessions[0];
    // Store previous RMS for trend
    if (allSessions.length > 1 && allSessions[1].harman_delta_db != null) {
      previousRms = allSessions[1].harman_delta_db;
    }
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
function updateHero(session) {
  const ring = document.getElementById('scoreRing');
  const val = document.getElementById('scoreValue');
  const label = document.getElementById('heroLabel');
  const detail = document.getElementById('heroDetail');
  const ctx = document.getElementById('heroContext');

  if (!session) return;

  const rc = session.run_context;
  const useRun = rc && rc.converged === true && rc.final_rms != null;
  const rms = useRun ? rc.final_rms : session.harman_delta_db;
  if (rms != null) {
    val.textContent = rms.toFixed(1);
    ring.className = 'score-ring ' + (rms <= 2 ? 'optimal' : rms <= 4 ? 'good' : 'poor');

    let trendHtml = '';
    if (previousRms != null) {
      const diff = rms - previousRms;
      if (Math.abs(diff) < 0.2) trendHtml = ' <span class="trend-flat">\u2192</span>';
      else if (diff < 0) trendHtml = ' <span class="trend-up">\u2193 ' + Math.abs(diff).toFixed(1) + '</span>';
      else trendHtml = ' <span class="trend-down">\u2191 ' + diff.toFixed(1) + '</span>';
    }
    const statusText = useRun ? 'Converged' : (rms <= 2 ? 'Optimal' : rms <= 4 ? 'Good' : 'Needs work');
    label.innerHTML = statusText + trendHtml;
  } else {
    val.textContent = '--';
    ring.className = 'score-ring none';
    label.textContent = 'No target comparison';
  }

  const ts = session.timestamp.slice(0,19).replace('T',' ');
  const info = classifyLabel(session.label);
  detail.textContent = 'Session #' + session.id + ' \u2014 ' + ts;
  const ctxSuffix = useRun
    ? ' \u2014 Run #' + rc.run_id + ' (' + rc.target + ')'
    : (rms != null && storedTarget ? ' \u2014 vs ' + (storedTarget.type || 'Target') : '');
  ctx.textContent = info.desc + ctxSuffix;
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
      return '<span class="hw-item"><span class="hw-dot '+cls+'"></span>'+d.name
        + (d.detail && d.connected ? '<span class="hw-detail"> '+d.detail+'</span>' : '')
        + '</span>';
    }).join('');
  } catch(e) { console.warn('status load failed:', e); }
}

// ── Active DSP state ────────────────────────────────────────────────────────
async function loadDspState() {
  try {
    const r = await fetch('/api/dsp-state');
    if (!r.ok) return;
    const data = await r.json();

    if (data.target_curve) {
      storedTarget = data.target_curve;
      refreshChart();
    }

    const card = document.getElementById('dspCard');
    if (!data.active) { card.style.display = 'none'; return; }

    card.style.display = '';
    const el = document.getElementById('dspContent');
    let html = '<div class="dsp-grid">';

    const outputs = Object.entries(data.outputs || {}).sort((a,b) => a[0]-b[0]);
    for (const [idx, out] of outputs) {
      const typeColor = out.type === 'sub' ? 'var(--accent)' : out.type === 'shaker' ? 'var(--yellow)' : 'var(--dim)';
      html += '<div class="dsp-output">';
      html += '<div class="dsp-output-header"><span class="dsp-output-label">' + out.label + '</span>';
      html += '<span class="dsp-output-type" style="color:'+typeColor+'">' + out.type + '</span></div>';

      if (out.gain_db != null) html += '<div class="dsp-param">Gain: <span>' + (out.gain_db >= 0 ? '+' : '') + out.gain_db.toFixed(1) + ' dB</span></div>';
      if (out.delay_ms != null) html += '<div class="dsp-param">Delay: <span>' + out.delay_ms.toFixed(1) + ' ms</span></div>';
      if (out.polarity_inverted != null) html += '<div class="dsp-param">Polarity: <span>' + (out.polarity_inverted ? 'Inverted' : 'Normal') + '</span></div>';

      if (out.eq && out.eq.length > 0) {
        html += '<div class="dsp-filter-list">';
        for (const f of out.eq) {
          const gain = f.gain_db != null ? (f.gain_db >= 0 ? '+' : '') + f.gain_db.toFixed(1) + 'dB' : '';
          const q = f.q ? 'Q=' + f.q.toFixed(2) : '';
          html += '<div class="f-row"><span>' + f.type + '</span><span>' + f.freq.toFixed(0) + 'Hz</span><span>' + gain + '</span><span>' + q + '</span></div>';
        }
        html += '</div>';
      } else {
        html += '<div class="dsp-param" style="color:var(--dim)">No EQ applied</div>';
      }
      html += '</div>';
    }
    html += '</div>';

    if (data.input_eq && data.input_eq.filters && data.input_eq.filters.length > 0) {
      html += '<div style="margin-top:.75rem"><div class="dsp-section-label">Shared Input EQ</div>';
      html += '<div class="dsp-filter-list">';
      for (const f of data.input_eq.filters) {
        const gain = f.gain_db != null ? (f.gain_db >= 0 ? '+' : '') + f.gain_db.toFixed(1) + 'dB' : '';
        const q = f.q ? 'Q=' + f.q.toFixed(2) : '';
        html += '<div class="f-row"><span>' + f.type + '</span><span>' + f.freq.toFixed(0) + 'Hz</span><span>' + gain + '</span><span>' + q + '</span></div>';
      }
      html += '</div></div>';
    }

    el.innerHTML = html;
  } catch(e) { console.warn('dsp state load failed:', e); }
}

// ── Activity timeline ───────────────────────────────────────────────────────
async function loadActivity() {
  try {
    const r = await fetch('/api/activity?limit=10');
    if (!r.ok) return;
    const events = await r.json();
    const el = document.getElementById('activityTimeline');

    if (events.length === 0) {
      el.innerHTML = '<div style="color:var(--dim);font-size:.82rem;">No activity yet. Start a calibration with Claude to see events here.</div>';
      return;
    }

    el.innerHTML = events.map(ev => {
      const ts = ev.timestamp.slice(11,16);
      return '<div class="timeline-event">'
        + '<span class="timeline-time">' + ts + '</span>'
        + '<span class="timeline-dot ' + ev.type + '"></span>'
        + '<span class="timeline-summary">' + ev.summary + '</span>'
        + '</div>';
    }).join('');
  } catch(e) { console.warn('activity load failed:', e); }
}

// ── Calibration runs (Runs tab) ─────────────────────────────────────────────
let runsData = [];
async function loadRuns() {
  try {
    const r = await fetch('/api/runs');
    if (!r.ok) return;
    runsData = await r.json();

    // Update Runs tab count
    document.querySelector('[data-tab="runs"]').textContent = 'Runs (' + runsData.length + ')';

    const container = document.getElementById('runsContent');
    if (runsData.length === 0) {
      container.innerHTML = '<div style="color:var(--dim);font-size:.82rem;padding:.5rem 0;">No calibration runs yet. Start a calibration with Claude to see runs here.</div>';
      return;
    }

    // Build run cards
    let html = '';
    for (const run of runsData) {
      const converged = run.converged;
      const statusBadge = converged ? '<span class="badge badge-optimal">Converged</span>' :
                         run.error ? '<span class="badge badge-danger">Error</span>' :
                         '<span class="badge badge-warn">Max iters</span>';
      const baseline = run.baseline_rms != null ? run.baseline_rms.toFixed(1) : '?';
      const final_rms = run.final_rms != null ? run.final_rms.toFixed(1) : '?';
      const rmsColor = run.final_rms != null ? (run.final_rms <= 2 ? 'var(--green)' : run.final_rms <= 4 ? 'var(--yellow)' : 'var(--red)') : 'var(--dim)';
      const ts = run.timestamp.slice(0,10);
      const sessionIds = run.session_ids || [];

      html += '<div class="run-card" id="run-'+run.id+'">';
      html += '<div class="run-card-header" onclick="toggleRun('+run.id+')">';
      html += '<span class="run-card-arrow" id="runArrow-'+run.id+'">&#9654;</span>';
      html += '<span class="run-card-title">#'+run.id+' &mdash; '+run.recipe_name+' &mdash; '+run.target+'</span>';
      html += statusBadge;
      html += '<span class="run-card-rms" style="color:'+rmsColor+'">'+baseline+' &rarr; '+final_rms+' dB</span>';
      html += '<span style="font-size:.7rem;color:var(--dim)">'+ts+'</span>';
      html += '</div>';

      html += '<div class="run-card-body" id="runBody-'+run.id+'" style="display:none">';
      html += '<div class="run-card-meta">';
      html += 'Iterations: '+(run.iterations_run||0)+' &middot; Target: '+run.target;
      html += '</div>';

      if (sessionIds.length > 0) {
        html += '<div style="font-size:.75rem;color:var(--dim);margin-bottom:.25rem">Associated measurements:</div>';
        html += '<div class="run-sessions">';
        for (const sid of sessionIds) {
          html += '<span class="run-session-chip" onclick="event.stopPropagation();loadSession('+sid+')">#'+sid+'</span>';
        }
        html += '</div>';
      }

      html += '<div style="margin-top:.5rem;display:flex;gap:.35rem;">';
      html += '<button class="btn-secondary btn-sm" onclick="event.stopPropagation();loadRunDetail('+run.id+')">Convergence</button>';
      if (sessionIds.length >= 2) {
        html += '<button class="btn-secondary btn-sm" onclick="event.stopPropagation();compareRunBA('+run.id+','+sessionIds[0]+','+sessionIds[sessionIds.length-1]+')">Compare First/Last</button>';
      }
      html += '</div>';
      html += '</div>';
      html += '</div>';
    }

    // Ad-hoc sessions (not in any run)
    const runSessionIds = new Set(runsData.flatMap(r => r.session_ids || []));
    const adhocSessions = allSessions.filter(s => !runSessionIds.has(s.id));
    if (adhocSessions.length > 0) {
      html += '<div class="adhoc-group">';
      html += '<div class="adhoc-header">Ad-hoc Measurements ('+adhocSessions.length+')</div>';
      for (const s of adhocSessions.slice(0, 20)) {
        const info = classifyLabel(s.label);
        const ts = s.timestamp.slice(0,10);
        html += '<div class="adhoc-item" onclick="loadSession('+s.id+')">';
        html += '<span style="color:var(--accent)">#'+s.id+'</span>';
        html += '<span>'+info.desc+'</span>';
        html += '<span style="color:var(--dim);margin-left:auto">'+ts+'</span>';
        html += '</div>';
      }
      html += '</div>';
    }

    container.innerHTML = html;
  } catch(e) { console.warn('runs load failed:', e); }
}

function toggleRun(runId) {
  const body = document.getElementById('runBody-' + runId);
  const arrow = document.getElementById('runArrow-' + runId);
  const visible = body.style.display !== 'none';
  body.style.display = visible ? 'none' : '';
  arrow.classList.toggle('open', !visible);
}

async function compareRunBA(runId, firstSessionId, lastSessionId) {
  // Overlay first and last session for before/after comparison
  try {
    const r = await fetch('/api/sessions/overlay?ids=' + firstSessionId + ',' + lastSessionId);
    if (!r.ok) return;
    const sessions = await r.json();
    if (sessions.length >= 1) {
      chartData.primary = { freqs: sessions[0].frequencies, spl: sessions[0].spl, label: sessions[0].label + ' (first)' };
    }
    if (sessions.length >= 2) {
      chartData.overlays = [{ id: sessions[1].id, freqs: sessions[1].frequencies, spl: sessions[1].spl, label: sessions[1].label + ' (last)' }];
    }
    renderChart();
    toast('Showing first vs last measurement for Run #' + runId);
  } catch(e) { toast('Compare error'); }
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
      const meta = [curve, rms, ts].filter(Boolean).join(' \u2014 ');
      return '<div class="state-item">'
        + '<div class="state-info"><div class="state-name">'+s.name+'</div>'
        + '<div class="state-meta">'+meta+(s.notes ? ' \u2014 '+s.notes : '')+'</div></div>'
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
    target_curve: storedTarget ? storedTarget.type : null,
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

// ── Feedback ────────────────────────────────────────────────────────────────
async function loadFeedback(sessionId) {
  const existing = document.getElementById('fbExisting');
  const upBtn = document.getElementById('fbUp');
  const downBtn = document.getElementById('fbDown');
  upBtn.className = 'feedback-btn';
  downBtn.className = 'feedback-btn';
  existing.textContent = '';
  document.getElementById('fbText').value = '';

  try {
    const r = await fetch('/api/feedback/' + sessionId);
    if (!r.ok) return;
    const entries = await r.json();
    if (entries.length > 0) {
      const last = entries[entries.length - 1];
      if (last.content_tag === 'up') upBtn.classList.add('active-up');
      if (last.content_tag === 'down') downBtn.classList.add('active-down');
      const text = last.text.replace(/^\\[(up|down)\\]\\s*/, '');
      if (text) existing.textContent = text;
    }
  } catch(e) {}
}

async function submitFeedback(rating) {
  if (!selectedSessionId) return;
  const text = document.getElementById('fbText').value.trim() || null;
  try {
    const r = await fetch('/api/feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: selectedSessionId, rating, text }),
    });
    if (!r.ok) { toast('Feedback failed'); return; }
    toast('Feedback saved');
    loadFeedback(selectedSessionId);
  } catch(e) { toast('Feedback error'); }
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

    const footer = document.getElementById('versionFooterText');
    footer.textContent = 'v' + semver + (sha7 !== 'unknown' ? ' (' + sha7 + ')' : '');

    if (d.up_to_date) {
      _setChip('v'+semver, 'up-to-date', 'Up to date');
    } else if (d.latest_sha) {
      _setChip('v'+semver+' \u25b2', 'update-available', 'Update available \u2014 click to upgrade');
    } else if (d.checking) {
      _setChip('v'+semver, null, 'Checking...');
      setTimeout(loadVersion, 8000);
    } else {
      _setChip('v'+semver, null, 'v'+semver);
    }
  } catch (e) {
    _setChip('\u2014', null, 'Version unavailable');
  }
}

function toggleVersionPopover() {
  const chip = document.getElementById('versionChip');
  if (chip.classList.contains('update-available')) {
    if (confirm('Restart to install update?')) {
      chip.textContent = 'Upgrading...';
      fetch('/api/upgrade', {method: 'POST'}).then(r => {
        if (r.ok) {
          toast('Upgrade triggered \u2014 reloading in 30s...');
          setTimeout(() => window.location.reload(), 30000);
        } else {
          toast('Upgrade failed');
          loadVersion();
        }
      }).catch(() => { toast('Upgrade request failed'); loadVersion(); });
    }
  }
}

// ── Auto-refresh ────────────────────────────────────────────────────────────
let lastSessionCount = 0;
async function checkForUpdates() {
  try {
    const r = await fetch('/api/sessions');
    if (!r.ok) return;
    const sessions = await r.json();
    if (sessions.length !== lastSessionCount && lastSessionCount > 0) {
      // New data arrived — refresh everything
      allSessions = sessions;
      loadHistory();
      loadDspState();
      loadActivity();
      loadRuns();
    }
    lastSessionCount = sessions.length;
  } catch(e) {}
}

// ── Boot ────────────────────────────────────────────────────────────────────
chartData = { primary: null, overlays: [] };

loadHistory();
loadStatus();
loadDspState();
loadRuns();
loadStates();
loadActivity();
loadVersion();

setInterval(loadStatus, 30000);
setInterval(loadActivity, 10000);
setInterval(checkForUpdates, 10000);

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


class FeedbackRequest(BaseModel):
    session_id: int
    rating: str  # "up" or "down"
    text: str | None = None


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
    """Return all sessions for the history table, with Harman delta and run context."""
    from .analysis import HarmanTarget, rms_deviation

    store = SessionStore()
    sessions = store.list_sessions()

    # Build run context: map sessions to runs by timestamp correlation
    runs = store.get_runs(limit=100)
    run_windows: list[dict] = []
    for run in runs:
        run_start = run["timestamp"]
        # Find the end of the run: latest iteration timestamp or run timestamp
        detail = store.get_run_detail(run["id"])
        iters = detail.get("iterations", []) if detail else []
        run_end = run_start
        for it in iters:
            # Iterations don't have timestamps, so use the run's convergence
            pass
        run_windows.append({
            "run_id": run["id"],
            "recipe_name": run["recipe_name"],
            "target": run["target"],
            "timestamp": run_start,
            "iterations_run": run.get("iterations_run") or 0,
            "converged": run.get("converged"),
            "final_rms": run.get("final_rms"),
        })

    result = []
    for s in sessions:
        harman_delta: float | None = None
        try:
            if s.target_curve and s.start_fr and s.start_fr.frequencies:
                ref = s.target_curve.get("reference_spl")
                band_raw = s.target_curve.get("band", [20.0, 200.0])
                band: tuple[float, float] = (float(band_raw[0]), float(band_raw[1]))
                if ref is not None:
                    target = HarmanTarget(reference_spl=float(ref), band=band)
                    harman_delta = round(rms_deviation(s.start_fr, target, band), 1)
        except Exception:
            pass  # analysis failure — leave as None

        # Find run context by timestamp proximity (session within 2h of run start)
        run_context: dict | None = None
        for rw in run_windows:
            try:
                from datetime import datetime, timezone
                # Parse both timestamps — handle Z suffix
                run_ts = rw["timestamp"].replace("Z", "+00:00")
                sess_ts = s.timestamp.replace("Z", "+00:00")
                rt = datetime.fromisoformat(run_ts)
                st = datetime.fromisoformat(sess_ts)
                delta = (st - rt).total_seconds()
                # Session is part of a run if it's after run start and within 2 hours
                if 0 <= delta <= 7200:
                    run_context = {
                        "run_id": rw["run_id"],
                        "recipe_name": rw["recipe_name"],
                        "target": rw["target"],
                        "converged": bool(rw["converged"]) if rw["converged"] is not None else None,
                        "final_rms": rw["final_rms"],
                    }
                    break
            except (ValueError, TypeError):
                continue

        result.append({
            "id": s.id,
            "timestamp": s.timestamp,
            "label": s.label,
            "peak_spl": s.start_fr.peak_spl,
            "freq_at_peak": s.start_fr.freq_at_peak,
            "n_freqs": len(s.start_fr.frequencies),
            "has_end_fr": s.end_fr is not None,
            "harman_delta_db": harman_delta,
            "run_context": run_context,
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
        "target_curve": session.target_curve,
    }


@app.get("/api/runs")
async def list_runs(limit: int = 20) -> list[dict]:
    """List calibration runs with associated session IDs."""
    store = SessionStore()
    runs = store.get_runs(limit=limit)

    # Correlate sessions to runs by timestamp (sessions within 2h of run start)
    sessions = store.list_sessions()
    for run in runs:
        run_ts_str = run["timestamp"]
        associated: list[int] = []
        try:
            from datetime import datetime
            rt_str = run_ts_str.replace("Z", "+00:00")
            rt = datetime.fromisoformat(rt_str)
            for s in sessions:
                st_str = s.timestamp.replace("Z", "+00:00")
                st = datetime.fromisoformat(st_str)
                delta = (st - rt).total_seconds()
                if 0 <= delta <= 7200:
                    associated.append(s.id)
        except (ValueError, TypeError):
            pass
        run["session_ids"] = associated
    return runs


@app.get("/api/runs/{run_id}")
async def get_run_detail(run_id: int) -> dict:
    """Return run detail with iteration history and associated sessions."""
    store = SessionStore()
    detail = store.get_run_detail(run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Run #{run_id} not found")

    # Find associated sessions by timestamp
    sessions = store.list_sessions()
    run_ts_str = detail["timestamp"]
    associated: list[int] = []
    try:
        from datetime import datetime
        rt_str = run_ts_str.replace("Z", "+00:00")
        rt = datetime.fromisoformat(rt_str)
        for s in sessions:
            st_str = s.timestamp.replace("Z", "+00:00")
            st = datetime.fromisoformat(st_str)
            delta = (st - rt).total_seconds()
            if 0 <= delta <= 7200:
                associated.append(s.id)
    except (ValueError, TypeError):
        pass
    detail["session_ids"] = associated
    return detail


# ── Activity timeline ────────────────────────────────────────────────────────


@app.get("/api/activity")
async def activity_timeline(limit: int = 15) -> list[dict]:
    """Unified activity timeline from sessions, runs, and DSP state changes.

    Returns significant events (measurements, EQ changes, run start/complete)
    sorted most-recent-first. No schema changes — queries existing tables.
    """
    store = SessionStore()
    events: list[dict] = []

    # Recent measurements
    sessions = store.list_sessions()
    for s in sessions[:limit]:
        events.append({
            "type": "measurement",
            "timestamp": s.timestamp,
            "summary": f"Measurement #{s.id}" + (f" ({s.label})" if s.label else ""),
            "detail": {
                "session_id": s.id,
                "label": s.label,
                "peak_spl": s.start_fr.peak_spl if s.start_fr and s.start_fr.frequencies else None,
            },
        })

    # Recent calibration run events
    runs = store.get_runs(limit=limit)
    for run in runs:
        converged = run.get("converged")
        final_rms = run.get("final_rms")
        if converged is not None:
            status = "converged" if converged else ("error" if run.get("error") else "max-iters")
            rms_str = f" — {final_rms:.1f} dB RMS" if final_rms is not None else ""
            events.append({
                "type": "run_complete",
                "timestamp": run["timestamp"],
                "summary": f"Run #{run['id']} {status}: {run['recipe_name']}{rms_str}",
                "detail": {
                    "run_id": run["id"],
                    "recipe_name": run["recipe_name"],
                    "target": run["target"],
                    "converged": bool(converged),
                    "final_rms": final_rms,
                },
            })
        else:
            events.append({
                "type": "run_start",
                "timestamp": run["timestamp"],
                "summary": f"Run #{run['id']} started: {run['recipe_name']}",
                "detail": {
                    "run_id": run["id"],
                    "recipe_name": run["recipe_name"],
                    "target": run["target"],
                },
            })

    # Recent DSP state changes
    dsp_state = store.get_active_dsp()
    for key, data in dsp_state.items():
        ts = data.get("timestamp")
        if not ts:
            continue
        if key.startswith("output_eq_"):
            idx = key.split("_")[-1]
            n_filters = len(data.get("filters", []))
            events.append({
                "type": "eq_applied",
                "timestamp": ts,
                "summary": f"EQ applied: {n_filters} filters → output {idx}",
                "detail": {"output_index": int(idx), "n_filters": n_filters},
            })
        elif key == "input_eq":
            n_filters = len(data.get("filters", []))
            events.append({
                "type": "eq_applied",
                "timestamp": ts,
                "summary": f"Input EQ applied: {n_filters} shared filters",
                "detail": {"shared": True, "n_filters": n_filters},
            })

    # Sort by timestamp descending, cap at limit
    events.sort(key=lambda e: e["timestamp"], reverse=True)
    return events[:limit]


# ── Feedback ─────────────────────────────────────────────────────────────────


@app.post("/api/feedback")
async def submit_feedback(body: FeedbackRequest) -> dict:
    """Submit subjective feedback on a measurement session.

    Rating is 'up' or 'down', with optional free-text description.
    """
    if body.rating not in ("up", "down"):
        raise HTTPException(status_code=422, detail="rating must be 'up' or 'down'")

    store = SessionStore()
    session = store.get_session(body.session_id)
    if session is None:
        raise HTTPException(
            status_code=404, detail=f"Session #{body.session_id} not found"
        )

    text = f"[{body.rating}]"
    if body.text:
        text += f" {body.text}"

    feedback_id = store.add_feedback(body.session_id, text, content_tag=body.rating)
    return {"id": feedback_id, "status": "saved"}


@app.get("/api/feedback/{session_id}")
async def get_feedback(session_id: int) -> list[dict]:
    """Return all feedback entries for a session."""
    store = SessionStore()
    return store.get_feedback(session_id)


def _format_processor_detail(kind: str, state: dict) -> str:
    """Render a processor's ``get_state()`` dict into a one-line status string.

    Kept kind-aware (not driver-aware) because the useful fields differ by
    role: AVRs surface input/volume, DSPs surface preset/source or volume.
    Any field the driver omits is silently skipped.
    """
    if kind == "avr":
        parts = []
        if (inp := state.get("input")) is not None:
            parts.append(f"Input: {inp}")
        if (vol := state.get("volume")) is not None:
            parts.append(f"Volume: {vol} dB")
        return ", ".join(parts) or str(state.get("host", ""))

    parts = []
    preset = state.get("preset")
    source = state.get("source")
    if preset is not None or source is not None:
        parts.append(f"Preset: {preset if preset is not None else '?'}")
        if source is not None:
            parts.append(f"Source: {source}")
    if (vol := state.get("volume")) is not None:
        parts.append(f"Volume: {vol} dB")
    if (cpu := state.get("cpu_load")) is not None:
        parts.append(f"CPU: {cpu}")
    return ", ".join(parts) or str(state.get("host", ""))


@app.get("/api/status")
async def system_status() -> dict:
    """Return system device status and last calibration run."""
    devices = []
    cfg = _load_config()

    try:
        registry = load_drivers_from_graph(cfg)
    except Exception as e:
        registry = None
        devices.append({"name": "Signal graph", "connected": False, "detail": str(e)})

    if registry is not None:
        for proc in cfg.signal_graph.processors:
            label = default_display_name(proc)
            driver = registry.get(proc.name)
            if driver is None:
                devices.append({"name": label, "connected": False, "detail": "not loaded"})
                continue
            try:
                state = await driver.get_state()
            except Exception as e:
                devices.append({"name": label, "connected": False, "detail": str(e)})
                continue
            if not state.get("connected"):
                devices.append({
                    "name": label,
                    "connected": False,
                    "detail": str(state.get("host", "")),
                })
                continue
            devices.append({
                "name": label,
                "connected": True,
                "detail": _format_processor_detail(proc.kind, state),
            })

    # UMIK — query via bare-metal measurement service (Docker has no PipeWire access)
    try:
        mic_name = cfg.mic.get("name", "UMIK")
        meas_client = MeasurementServiceClient()
        _idx, _dev = await meas_client.find_umik_device(name_substring=mic_name)
        if _idx is not None and _dev is not None:
            devices.append({"name": f"UMIK ({mic_name})", "connected": True, "detail": str(_dev.get("name", ""))})
        else:
            devices.append({"name": f"UMIK ({mic_name})", "connected": False, "detail": "Not found"})
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


# ── Active DSP state ──────────────────────────────────────────────────────────


@app.get("/api/dsp-state")
async def dsp_state() -> dict:
    """Return the active DSP state persisted by the MCP server.

    Returns a structured view: per-output EQ/delay/polarity/gain + shared input EQ.
    This data survives restarts because it's written to SQLite on every apply_eq,
    set_delay, set_polarity, and set_output_gain call.
    """
    store = SessionStore()
    raw = store.get_active_dsp()

    if not raw:
        return {"active": False, "outputs": {}, "input_eq": None}

    cfg = _load_config()
    slots = cfg.minidsp.get("output_slots", [])

    outputs = {}
    for slot in slots:
        idx = slot["index"]
        label = slot.get("label", f"Output {idx}")
        slot_type = slot.get("type", "unknown")
        out = {
            "label": label,
            "type": slot_type,
            "eq": None,
            "delay_ms": None,
            "polarity_inverted": None,
            "gain_db": None,
        }
        eq_entry = raw.get(f"output_eq_{idx}")
        if eq_entry:
            out["eq"] = eq_entry.get("filters")
            out["eq_timestamp"] = eq_entry.get("timestamp")
        delay_entry = raw.get(f"delay_{idx}")
        if delay_entry:
            out["delay_ms"] = delay_entry.get("delay_ms")
        pol_entry = raw.get(f"polarity_{idx}")
        if pol_entry:
            out["polarity_inverted"] = pol_entry.get("inverted")
        gain_entry = raw.get(f"gain_{idx}")
        if gain_entry:
            out["gain_db"] = gain_entry.get("gain_db")
        outputs[str(idx)] = out

    input_eq = None
    ie = raw.get("input_eq")
    if ie:
        input_eq = {"filters": ie.get("filters"), "timestamp": ie.get("timestamp")}

    target_curve = None
    tc = raw.get("target_curve")
    if tc:
        target_curve = {
            "type": tc.get("type"),
            "reference_spl": tc.get("reference_spl"),
            "band": tc.get("band"),
            "points": tc.get("points"),
            "timestamp": tc.get("timestamp"),
        }

    return {"active": True, "outputs": outputs, "input_eq": input_eq, "target_curve": target_curve}


# ── Config helper ─────────────────────────────────────────────────────────────

def _load_config() -> Config:
    if not CONFIG_PATH.exists():
        raise HTTPException(
            status_code=503,
            detail=f"No config at {CONFIG_PATH}. Run 'calibrate check' first.",
        )
    return Config.load(CONFIG_PATH)
