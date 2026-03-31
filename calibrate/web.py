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
import dataclasses
import logging
import math
import os
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

from .config import Config, CONFIG_PATH, update_config
from .measurement import MeasurementEngine, FrequencyResponse, MeasurementQualityError
from .preflight import PreflightChecker
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

# ── Version / upgrade state ───────────────────────────────────────────────────

_GHCR_IMAGE = "abarbaccia/avr-calibration"
_GHCR_REGISTRY = "ghcr.io"
_VERSION_CACHE_TTL = 3600  # seconds

# {"latest_sha": str|None, "expires": float, "checked_at": float}
_version_cache: dict = {}

_DATA_DIR = Path.home() / ".avr-calibration"


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
    /* Advisory badges */
    .badge { display: inline-block; padding: .25rem .7rem; border-radius: 4px; font-size: .8rem;
             font-weight: 600; margin-bottom: .5rem; }
    .badge-optimal { background: rgba(34,197,94,.15); color: #4ade80; border: 1px solid #22c55e; }
    .badge-warn    { background: rgba(245,158,11,.15); color: #fbbf24; border: 1px solid #f59e0b; }
    .badge-danger  { background: rgba(239,68,68,.15);  color: #f87171; border: 1px solid #ef4444; }
    .badge-low     { background: rgba(59,130,246,.15); color: #93c5fd; border: 1px solid #3b82f6; }
    .badge-empty   { background: rgba(100,116,139,.15);color: #94a3b8; border: 1px solid #475569; }
    /* Dynamic EQ dismissable callout */
    #dynEqCard { border-left: 4px solid #f59e0b; }
    #dynEqCard p { font-size: .85rem; color: #94a3b8; line-height: 1.5; margin-bottom: .75rem; }
    #dynEqDismissBtn { background: #334155; color: #cbd5e1; font-size: .8rem; padding: .35rem .8rem; }
    #dynEqDismissBtn:hover { background: #475569; }
    /* Sub Trim Advisor */
    #trimInput { width: 8rem; margin-bottom: .5rem; }
    #trimGuidance { font-size: .8rem; color: #94a3b8; margin-top: .25rem; }
    /* Phase check card */
    .phase-row { display: flex; gap: .75rem; align-items: flex-end; margin-bottom: .75rem; }
    .phase-row > div { flex: 1; }
    .phase-row label { font-size: .8rem; }
    .phase-row select { margin-bottom: 0; }
    #phaseRunBtn { background: #334155; color: #cbd5e1; font-size: .85rem; white-space: nowrap; }
    #phaseRunBtn:not(:disabled):hover { background: #475569; }
    #phaseResult { margin-top: .75rem; font-size: .85rem; color: #94a3b8; }
    /* Variance band */
    .variance-note { font-size: .75rem; color: #64748b; margin-top: .25rem; text-align: center; }
    /* Cardioid toggle */
    .toggle-row { display: flex; align-items: center; gap: 1rem; margin-bottom: .75rem; }
    .toggle-row label { margin: 0; cursor: pointer; }
    input[type=checkbox].toggle { width: 2.5rem; height: 1.4rem; appearance: none;
      background: #334155; border-radius: 1rem; cursor: pointer; position: relative;
      transition: background .2s; }
    input[type=checkbox].toggle:checked { background: #2dd4bf; }
    input[type=checkbox].toggle::after { content: ''; position: absolute; top: .2rem; left: .2rem;
      width: 1rem; height: 1rem; background: #fff; border-radius: 50%; transition: left .2s; }
    input[type=checkbox].toggle:checked::after { left: 1.3rem; }
    #cardioidDetail { font-size: .82rem; color: #94a3b8; }
    #cardioidDetail .warn-note { color: #fbbf24; margin-top: .4rem; }
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
    /* Workflow nav */
    .workflow-nav { display: flex; width: 100%; max-width: 760px; margin-bottom: 2rem; }
    .workflow-nav .step { flex: 1; padding: .6rem .5rem; text-align: center; font-size: .72rem;
      font-weight: 600; letter-spacing: .04em; text-transform: uppercase; color: #64748b;
      border-bottom: 2px solid #2d3748; cursor: pointer; transition: color .15s, border-color .15s; }
    .workflow-nav .step.active { color: #3b82f6; border-color: #3b82f6; }
    .workflow-nav .step.done { color: #4ade80; border-color: #4ade80; }
    .workflow-nav .step.locked { opacity: .35; cursor: not-allowed; pointer-events: none; }
    /* Hardware check rows */
    .check-row { display: flex; align-items: flex-start; gap: .75rem; padding: .65rem 0;
      border-bottom: 1px solid #1a2030; }
    .check-row:last-child { border-bottom: none; }
    .check-badge { min-width: 3.5rem; padding: .2rem .4rem; border-radius: 4px; font-size: .72rem;
      font-weight: 700; text-align: center; flex-shrink: 0; }
    .check-badge.pass { background: rgba(34,197,94,.15); color: #4ade80; border: 1px solid #22c55e; }
    .check-badge.fail { background: rgba(239,68,68,.15); color: #f87171; border: 1px solid #ef4444; }
    .check-badge.pending { background: rgba(100,116,139,.15); color: #64748b; border: 1px solid #374151; }
    .check-badge.running { background: rgba(59,130,246,.15); color: #93c5fd; border: 1px solid #3b82f6; }
    .check-name { font-size: .875rem; font-weight: 500; color: #cbd5e1; }
    .check-detail { font-size: .78rem; color: #94a3b8; margin-top: .1rem; }
    .check-error { font-size: .75rem; color: #f87171; margin-top: .15rem; }
    /* Phase content */
    .phase-header { font-size: .95rem; font-weight: 600; color: #e2e8f0; margin-bottom: .25rem; }
    .phase-desc { font-size: .82rem; color: #64748b; margin-bottom: 1.25rem; line-height: 1.5; }
    /* Test tone */
    #testToneBtn { background: #334155; color: #cbd5e1; font-size: .85rem; margin-right: .5rem; }
    #testToneBtn.playing { background: #7c3aed; color: #fff; }
    #testToneBtn:not(:disabled):hover { background: #475569; }
    #confirmToneBtn { background: #22c55e; color: #0d0f14; font-weight: 600; font-size: .85rem; display: none; }
    #confirmToneBtn:not(:disabled):hover { opacity: .85; }
  </style>
</head>
<body>
  <div id="versionChip" title="Running version">&#8230;</div>
  <h1>AVR Calibration</h1>

  <!-- Workflow Navigator -->
  <div class="workflow-nav">
    <div class="step active" id="navStep1" onclick="showPhase(1)">1 Equipment</div>
    <div class="step locked" id="navStep2" onclick="showPhase(2)">2 Baseline</div>
    <div class="step locked" id="navStep3">3 Calibrate</div>
    <div class="step locked" id="navStep4">4 Feedback</div>
  </div>

  <!-- Phase 1: Equipment Setup -->
  <div id="phase1Content" style="display:block">

    <!-- Denon AVR Card -->
    <div class="card" id="eqDenonCard">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem">
        <h2 style="margin:0">Denon AVR</h2>
        <span id="eqDenonBadge" class="check-badge pending">&mdash;</span>
      </div>

      <!-- Discover / host row -->
      <div style="display:flex;gap:.5rem;margin-bottom:.75rem">
        <input type="text" id="eqDenonHost" placeholder="192.168.x.x (or click Discover)" style="flex:1">
        <button id="eqDenonDiscoverBtn" onclick="denonDiscover()">Discover</button>
      </div>
      <div id="eqDenonDiscoverStatus" style="font-size:.78rem;color:#94a3b8;margin-bottom:.5rem"></div>

      <!-- Input selector (shown after host is known) -->
      <div id="eqDenonInputSection" style="display:none">
        <label for="eqDenonInput" style="font-size:.75rem">Which Denon input is the Pi HDMI cable connected to?</label>
        <div style="display:flex;gap:.5rem;align-items:center;margin-bottom:.5rem">
          <select id="eqDenonInput" style="flex:1">
            <option value="">— loading inputs —</option>
          </select>
          <button id="eqDenonTestBtn" onclick="denonTestInput()">Test</button>
        </div>
        <div id="eqDenonTestStatus" style="font-size:.78rem;color:#94a3b8;margin-bottom:.4rem"></div>
        <!-- Hear confirmation — shown while tone plays -->
        <div id="eqDenonHearRow" style="display:none;background:#131720;border-radius:6px;padding:.6rem .75rem;margin-bottom:.5rem">
          <div style="font-size:.82rem;color:#94a3b8;margin-bottom:.4rem">Playing 440 Hz tone via Pi HDMI&hellip; Did you hear it through your speakers?</div>
          <div style="display:flex;gap:.5rem">
            <button onclick="denonConfirmInput(true)" style="background:#22c55e;color:#0d0f14;font-weight:600">&#10003; Yes</button>
            <button onclick="denonConfirmInput(false)" style="background:#334155;color:#cbd5e1">&#10007; No — try another</button>
          </div>
        </div>
      </div>

      <div style="display:flex;justify-content:flex-end;margin-top:.75rem">
        <button id="eqDenonSaveBtn" onclick="denonSave()" style="background:#2dd4bf;color:#0d0f14;font-weight:600" disabled>Save</button>
      </div>
      <div id="eqDenonSaveStatus" style="font-size:.78rem;margin-top:.35rem;color:#94a3b8"></div>
    </div>

    <!-- miniDSP 2x4 HD Card -->
    <div class="card" id="eqDspCard">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem">
        <h2 style="margin:0">miniDSP 2x4 HD</h2>
        <span id="eqDspBadge" class="check-badge pending">&mdash;</span>
      </div>

      <div style="margin-bottom:.75rem">
        <div style="font-size:.72rem;text-transform:uppercase;letter-spacing:.07em;color:#64748b;margin-bottom:.4rem">Input Labels</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:.5rem">
          <input type="text" id="eqDspIn0" placeholder="Input 1 (e.g. Denon LFE L)">
          <input type="text" id="eqDspIn1" placeholder="Input 2 (e.g. Denon LFE R)">
        </div>
      </div>
      <div style="margin-bottom:.75rem">
        <div style="font-size:.72rem;text-transform:uppercase;letter-spacing:.07em;color:#64748b;margin-bottom:.4rem">Output Labels</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:.5rem">
          <input type="text" id="eqDspOut0" placeholder="Output 1 (e.g. Sub L)">
          <input type="text" id="eqDspOut1" placeholder="Output 2 (e.g. Sub R)">
          <input type="text" id="eqDspOut2" placeholder="Output 3">
          <input type="text" id="eqDspOut3" placeholder="Output 4">
        </div>
      </div>
      <div style="display:flex;justify-content:space-between;align-items:center">
        <button id="eqDspTestBtn" onclick="dspTest()" style="background:#334155;color:#cbd5e1">Test Connection</button>
        <button id="eqDspSaveBtn" onclick="dspSave()" style="background:#2dd4bf;color:#0d0f14;font-weight:600">Save Labels</button>
      </div>
      <div id="eqDspStatus" style="font-size:.78rem;margin-top:.4rem;color:#94a3b8"></div>
    </div>

    <!-- Speakers & Subs Card -->
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem">
        <h2 style="margin:0">Speakers &amp; Subs</h2>
        <button id="eqAddSpeakerBtn" onclick="toggleAddSpeaker()" style="background:#334155;color:#cbd5e1;font-size:.82rem">+ Add</button>
      </div>

      <!-- Add speaker form (collapsed by default) -->
      <div id="eqAddSpeakerForm" style="display:none;background:#131720;border-radius:8px;padding:.75rem;margin-bottom:.75rem">
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:.5rem;margin-bottom:.5rem">
          <div>
            <label for="eqSpkType" style="font-size:.75rem">Type</label>
            <select id="eqSpkType">
              <option value="subwoofer">Subwoofer</option>
              <option value="front_l">Front Left</option>
              <option value="front_r">Front Right</option>
              <option value="center">Center</option>
              <option value="surround_l">Surround Left</option>
              <option value="surround_r">Surround Right</option>
              <option value="height_l">Height Left</option>
              <option value="height_r">Height Right</option>
              <option value="bass_shaker">Bass Shaker</option>
            </select>
          </div>
          <div>
            <label for="eqSpkLabel" style="font-size:.75rem">Name / Model</label>
            <input type="text" id="eqSpkLabel" placeholder="e.g. SVS PB12-NSD">
          </div>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:.5rem;margin-bottom:.5rem">
          <div>
            <label for="eqSpkLocation" style="font-size:.75rem">Room Location</label>
            <input type="text" id="eqSpkLocation" placeholder="e.g. front left corner">
          </div>
          <div id="eqSpkPortRow">
            <label for="eqSpkPortTune" style="font-size:.75rem">Port Tune (Hz)</label>
            <input type="number" id="eqSpkPortTune" placeholder="e.g. 22" step="0.5">
          </div>
        </div>
        <div style="display:flex;gap:.5rem;justify-content:flex-end">
          <button onclick="toggleAddSpeaker()" style="background:#334155;color:#cbd5e1">Cancel</button>
          <button onclick="saveSpeaker()" style="background:#3b82f6;color:#fff;font-weight:600">Add Speaker</button>
        </div>
      </div>

      <!-- Speaker list -->
      <div id="eqSpeakerList" style="display:flex;flex-direction:column;gap:.5rem"></div>
      <div id="eqSpeakerEmpty" style="font-size:.82rem;color:#475569;text-align:center;padding:1rem 0">
        No speakers added yet.
      </div>
    </div>

    <!-- Continue -->
    <div style="padding-bottom:.25rem">
      <button onclick="showPhase(2)" style="background:#3b82f6;color:#fff;width:100%;padding:.75rem;font-weight:600">
        Continue to Baseline &rarr;
      </button>
    </div>

  </div><!-- /phase1Content -->

  <!-- Phase 2: Baseline + Calibration (was Phase 3) -->
  <div id="phase2Content" style="display:none">

  <div class="card" id="dynEqCard">
    <h2>⚠ Disable Dynamic EQ</h2>
    <p>Audyssey Dynamic EQ re-applies a loudness curve at every volume level. This fights your
    calibration by adding bass boost that conflicts with the Harman target you just measured.
    Disable it in AVR Settings → Audyssey → Dynamic EQ, or set Reference Level Offset to −15 dB.</p>
    <button id="dynEqDismissBtn" onclick="dismissDynEq()">Got it — I've disabled it</button>
  </div>

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

  <div class="card" id="subTrimCard">
    <h2>Sub Trim Advisor</h2>
    <label for="trimInput">Audyssey Sub Trim Level (dB)</label>
    <input type="number" id="trimInput" step="0.5" placeholder="-10" oninput="onTrimInput(this.value)">
    <div id="trimBadge"></div>
    <div id="trimGuidance">Enter your Audyssey sub trim reading above.</div>
  </div>

  <div class="card" id="phaseCard">
    <h2>Phase Check</h2>
    <p style="font-size:.8rem;color:#64748b;margin-bottom:.75rem">
      Select a sub-only measurement and a mains-only measurement. Analyzes time offset at the crossover.
    </p>
    <div class="phase-row">
      <div>
        <label for="phaseSubSel">Sub session</label>
        <select id="phaseSubSel"><option value="">— select session —</option></select>
      </div>
      <div>
        <label for="phaseMainsSel">Mains session</label>
        <select id="phaseMainsSel"><option value="">— select session —</option></select>
      </div>
      <button id="phaseRunBtn" onclick="runPhaseCheck()">Analyze Alignment</button>
    </div>
    <div id="phaseResult"></div>
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

  <div class="card" id="cardioidCard" style="display:none">
    <h2>Sub Array Mode</h2>
    <div class="toggle-row">
      <input type="checkbox" class="toggle" id="cardioidToggle" onchange="onCardioidToggle(this.checked)">
      <label for="cardioidToggle" id="cardioidLabel">Normal — standard sub output</label>
    </div>
    <div id="cardioidDetail"></div>
  </div>

  <div class="card" id="signalPathCard">
    <h2>miniDSP 2x4 HD</h2>

    <!-- Master state strip -->
    <div id="spMasterStrip" style="display:flex;gap:1.5rem;flex-wrap:wrap;align-items:center;margin-bottom:1.25rem;padding:.6rem .9rem;background:#131720;border-radius:8px;font-size:.82rem;color:#94a3b8">
      <span>Source: <strong id="spLiveSource" style="color:#e2e8f0">—</strong></span>
      <span>Preset: <strong id="spLivePreset" style="color:#e2e8f0">—</strong></span>
      <span>Volume: <strong id="spLiveVolume" style="color:#e2e8f0">—</strong></span>
      <span id="spLiveMute" style="display:none;color:#f87171;font-weight:700">MUTED</span>
    </div>

    <!-- Block diagram: inputs | routing | outputs -->
    <div style="display:grid;grid-template-columns:1fr auto 1fr;gap:.75rem;align-items:start">

      <!-- Inputs column -->
      <div>
        <div style="font-size:.72rem;text-transform:uppercase;letter-spacing:.07em;color:#64748b;margin-bottom:.5rem">Inputs</div>
        <div id="spInputs" style="display:flex;flex-direction:column;gap:.5rem"></div>
      </div>

      <!-- Routing arrows -->
      <div id="spRoutingArrows" style="display:flex;flex-direction:column;justify-content:center;padding:0 .5rem;font-size:.78rem;color:#334155;gap:.35rem;margin-top:1.4rem"></div>

      <!-- Outputs column -->
      <div>
        <div style="font-size:.72rem;text-transform:uppercase;letter-spacing:.07em;color:#64748b;margin-bottom:.5rem">Outputs</div>
        <div id="spOutputs" style="display:flex;flex-direction:column;gap:.5rem"></div>
      </div>

    </div>

    <!-- Controls -->
    <div style="display:flex;gap:.75rem;align-items:center;flex-wrap:wrap;margin-top:1.25rem;padding-top:1rem;border-top:1px solid #1e2537">
      <div style="flex:1;min-width:130px">
        <label for="spSource" style="font-size:.75rem">Input Source</label>
        <select id="spSource">
          <option value="Analog">Analog</option>
          <option value="Toslink">Toslink</option>
          <option value="USB">USB</option>
        </select>
      </div>
      <div style="flex:1;min-width:110px">
        <label for="spPreset" style="font-size:.75rem">Preset Slot</label>
        <select id="spPreset">
          <option value="0">Preset 0</option>
          <option value="1">Preset 1</option>
          <option value="2">Preset 2</option>
          <option value="3">Preset 3</option>
        </select>
      </div>
      <button id="spApplyBtn" onclick="applySignalPath()" style="background:#2dd4bf;color:#0d0f14;font-weight:600;align-self:flex-end">Apply</button>
    </div>
    <div id="spStatus" style="margin-top:.6rem;font-size:.82rem;color:#94a3b8"></div>
  </div>

  </div><!-- /phase2Content -->

  <!-- Phase 3: Calibrate (locked placeholder) -->
  <div id="phase3Content" style="display:none" class="card">
    <div style="text-align:center;padding:2rem;color:#475569">
      <div style="font-size:2rem;margin-bottom:.5rem">&#128274;</div>
      <div style="font-size:.85rem">Complete baseline measurement to unlock Calibration.</div>
    </div>
  </div>

  <!-- Phase 4: Feedback (locked placeholder) -->
  <div id="phase4Content" style="display:none" class="card">
    <div style="text-align:center;padding:2rem;color:#475569">
      <div style="font-size:2rem;margin-bottom:.5rem">&#128274;</div>
      <div style="font-size:.85rem">Complete calibration to unlock Feedback.</div>
    </div>
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
    if (targetCurveType === 'ht')
      return freqs.map(f => f >= 100 ? refSpl : refSpl + 4 * Math.log2(100 / f));
    if (targetCurveType === 'music') return freqs.map(f => {
      const oct = Math.log2(f / 30);
      return refSpl + 4 * Math.exp(-(oct * oct) / (2 * 0.7 * 0.7));
    });
    // Harman: flat above 80 Hz, +3 dB/octave below 80 Hz
    return freqs.map(f => f >= 80 ? refSpl : refSpl + 3 * Math.log2(80 / f));
  }

  // ── Dynamic EQ dismiss ─────────────────────────────────────────────────
  function dismissDynEq() {
    localStorage.setItem('dynEqDismissed', '1');
    const card = document.getElementById('dynEqCard');
    if (card) card.style.display = 'none';
  }
  if (localStorage.getItem('dynEqDismissed') === '1') {
    const card = document.getElementById('dynEqCard');
    if (card) card.style.display = 'none';
  }

  // ── Sub Trim Advisor ────────────────────────────────────────────────────
  const TRIM_RULES = [
    { max: -12,  cls: 'badge-low',     badge: 'Too low',    msg: 'Too low — increase physical gain knob or sub output level.' },
    { max: -10,  cls: 'badge-optimal', badge: 'Optimal',    msg: 'Optimal — physical gain knob is correctly calibrated.' },
    { max:  -5,  cls: 'badge-warn',    badge: 'Acceptable', msg: 'Slightly hot — consider lowering physical gain 2–3 dB.' },
    { max: Infinity, cls: 'badge-danger', badge: 'Too hot', msg: 'Too hot — lower physical gain knob and re-run Audyssey.' },
  ];
  function onTrimInput(val) {
    const badge = document.getElementById('trimBadge');
    const guide = document.getElementById('trimGuidance');
    if (val === '' || isNaN(parseFloat(val))) {
      badge.innerHTML = '';
      guide.textContent = 'Enter your Audyssey sub trim reading above.';
      return;
    }
    const v = parseFloat(val);
    const rule = TRIM_RULES.find(r => v <= r.max);
    badge.innerHTML = `<span class="badge ${rule.cls}">${rule.badge} (${v} dB)</span>`;
    guide.textContent = rule.msg;
  }

  // ── Phase Check ────────────────────────────────────────────────────────
  async function runPhaseCheck() {
    const subId = parseInt(document.getElementById('phaseSubSel').value);
    const mainsId = parseInt(document.getElementById('phaseMainsSel').value);
    const resultEl = document.getElementById('phaseResult');
    const btn = document.getElementById('phaseRunBtn');
    if (!subId || !mainsId) {
      resultEl.textContent = 'Select both a sub session and a mains session.';
      return;
    }
    btn.disabled = true;
    resultEl.textContent = 'Computing cross-correlation…';
    try {
      const r = await fetch('/api/sessions/time-align', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sub_session_id: subId, mains_session_id: mainsId }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({ detail: r.statusText }));
        const msg = err.message || err.detail || 'Analysis failed';
        resultEl.innerHTML = `<span style="color:#f87171">⚠ ${msg}</span>`;
      } else {
        const d = await r.json();
        const sign = d.sub_leads ? 'Sub leads mains' : 'Sub lags mains';
        const absBadgeCls = Math.abs(d.offset_ms) < 1 ? 'badge-optimal' :
                            Math.abs(d.offset_ms) < 5 ? 'badge-warn' : 'badge-danger';
        resultEl.innerHTML =
          `<span class="badge ${absBadgeCls}">${sign} by ${Math.abs(d.offset_ms).toFixed(1)} ms</span>` +
          `<p style="font-size:.82rem;color:#94a3b8;margin-top:.5rem">${d.recommendation}</p>`;
      }
    } catch (e) {
      resultEl.innerHTML = `<span style="color:#f87171">Error: ${e.message}</span>`;
    }
    btn.disabled = false;
  }

  // ── Cardioid toggle ────────────────────────────────────────────────────
  async function onCardioidToggle(enabled) {
    const toggle = document.getElementById('cardioidToggle');
    const detail = document.getElementById('cardioidDetail');
    const label = document.getElementById('cardioidLabel');
    toggle.disabled = true;
    label.textContent = 'Applying…';
    try {
      const r = await fetch('/api/signal-path/cardioid', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      });
      if (!r.ok) {
        detail.innerHTML = `<span style="color:#f87171">Failed to apply — check miniDSP connection.</span>`;
        toggle.checked = !enabled; // revert
      } else {
        const d = await r.json();
        if (d.status === 'advisory_only') {
          label.textContent = 'Cardioid (manual configuration required)';
          detail.innerHTML = `<p>${d.message}</p>
            <p class="warn-note">Set Output 2: polarity inverted, delay ${d.delay_ms ? d.delay_ms.toFixed(1) : '?'} ms in miniDSP app.</p>`;
        } else if (enabled) {
          label.textContent = 'Cardioid — rear rejection active';
          detail.innerHTML = `<p>Output 2: inverted polarity, ${d.delay_ms.toFixed(1)} ms delay applied.</p>
            <p class="warn-note">Effective above ~170 Hz at 1m separation. Verify with measurement.</p>`;
        } else {
          label.textContent = 'Normal — standard sub output';
          detail.innerHTML = '';
        }
      }
    } catch (e) {
      detail.innerHTML = `<span style="color:#f87171">Error: ${e.message}</span>`;
      toggle.checked = !enabled;
    }
    toggle.disabled = false;
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
      const irNote = s.has_ir ? '' : ' ⚠';
      return `<tr class="${sel}" data-session-id="${s.id}" onclick="loadSession(${s.id})">
        <td class="cb-col" onclick="event.stopPropagation()">
          <input type="checkbox" data-id="${s.id}" onchange="updateAvgButton()">
        </td>
        <td>${s.id}</td><td>${ts}</td><td>${label}${irNote}</td>
        <td class="peak">${peak}</td><td>${s.n_freqs}</td>
      </tr>`;
    }).join('');
    // Populate phase check selects
    const phaseOpts = sessions.map(s => {
      const lbl = (s.label || `Session #${s.id}`) + (s.has_ir ? '' : ' ⚠ no IR');
      return `<option value="${s.id}">${s.id}: ${lbl}</option>`;
    }).join('');
    const emptyOpt = '<option value="">— select session —</option>';
    const subSel = document.getElementById('phaseSubSel');
    const mainsSel = document.getElementById('phaseMainsSel');
    if (subSel) subSel.innerHTML = emptyOpt + phaseOpts;
    if (mainsSel) mainsSel.innerHTML = emptyOpt + phaseOpts;
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
      if (result.spl_variance && frChart) {
        const upper = result.spl_dbfs.map((v, i) => v + result.spl_variance[i]);
        const lower = result.spl_dbfs.map((v, i) => v - result.spl_variance[i]);
        frChart.data.datasets.push({
          label: '±1σ variance band (upper)',
          data: upper,
          borderColor: 'transparent',
          backgroundColor: 'rgba(45,212,191,0.12)',
          pointRadius: 0,
          fill: '+1',
        });
        frChart.data.datasets.push({
          label: '±1σ variance band (lower)',
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

  // ── Signal Path ────────────────────────────────────────────────────────
  let _spLevelTimer = null;
  let _spRouting = [];  // routing from config (write-only on device; readable from config)

  async function loadSignalPathConfig() {
    // Fetch config + live device state in parallel
    const [cfgRes, stateRes] = await Promise.allSettled([
      fetch('/api/signal-path'),
      fetch('/api/signal-path/device-state'),
    ]);

    if (cfgRes.status === 'fulfilled' && cfgRes.value.ok) {
      try {
        const cfg = await cfgRes.value.json();
        if (cfg.source) document.getElementById('spSource').value = cfg.source;
        if (cfg.preset !== undefined && cfg.preset !== null)
          document.getElementById('spPreset').value = String(cfg.preset);
        _spRouting = cfg.routing || [];
      } catch (_) {}
    }

    let deviceState = null;
    if (stateRes.status === 'fulfilled' && stateRes.value.ok) {
      try { deviceState = await stateRes.value.json(); } catch (_) {}
    }

    _renderDiagram(deviceState);

    // Auto-refresh level meters every 2 s
    if (_spLevelTimer) clearInterval(_spLevelTimer);
    _spLevelTimer = setInterval(_refreshLevels, 2000);
  }

  async function _refreshLevels() {
    try {
      const r = await fetch('/api/signal-path/device-state');
      if (!r.ok) return;
      const d = await r.json();
      _renderDiagram(d);
    } catch (_) {}
  }

  function _renderDiagram(state) {
    const master = (state || {}).master || {};
    const inputLevels = (state || {}).input_levels || [];
    const outputLevels = (state || {}).output_levels || [];

    // Master strip
    document.getElementById('spLiveSource').textContent = master.source || '—';
    document.getElementById('spLivePreset').textContent = master.preset !== undefined && master.preset !== null ? String(master.preset) : '—';
    document.getElementById('spLiveVolume').textContent = master.volume !== undefined && master.volume !== null ? master.volume + ' dB' : '—';
    const muteEl = document.getElementById('spLiveMute');
    muteEl.style.display = master.mute ? '' : 'none';

    // Inputs (2 channels)
    const inputNames = ['Input L', 'Input R'];
    document.getElementById('spInputs').innerHTML = inputNames.map((name, i) => {
      const ch = inputLevels[i] || {};
      const lvls = (ch.levels || [])[0] || {};
      return _channelCard(name, lvls.rms_dbfs, lvls.peak_dbfs);
    }).join('');

    // Routing arrows: from config routing if available, else generic lines
    const arrowEl = document.getElementById('spRoutingArrows');
    if (_spRouting.length) {
      arrowEl.innerHTML = _spRouting.map(r =>
        `<div style="white-space:nowrap">In ${r.input} → [${(r.outputs || []).join(',')}]</div>`
      ).join('');
    } else {
      arrowEl.innerHTML = '<div style="color:#1e2537;font-size:1.1rem">→</div>'.repeat(2);
    }

    // Outputs (4 channels)
    const outputNames = ['Out 1', 'Out 2', 'Out 3', 'Out 4'];
    document.getElementById('spOutputs').innerHTML = outputNames.map((name, i) => {
      const ch = outputLevels[i] || {};
      const lvls = (ch.levels || [])[0] || {};
      return _channelCard(name, lvls.rms_dbfs, lvls.peak_dbfs);
    }).join('');
  }

  function _channelCard(label, rms, peak) {
    const hasLevel = rms !== undefined && rms !== null;
    const clampedRms = hasLevel ? Math.max(-60, Math.min(0, rms)) : -60;
    const clampedPeak = (peak !== undefined && peak !== null) ? Math.max(-60, Math.min(0, peak)) : -60;
    const rmsPct = ((clampedRms + 60) / 60 * 100).toFixed(1);
    const peakPct = ((clampedPeak + 60) / 60 * 100).toFixed(1);
    const rmsColor = clampedRms > -6 ? '#f87171' : clampedRms > -18 ? '#4ade80' : '#22d3ee';
    const valText = hasLevel ? `${rms.toFixed(1)} dBFS` : '—';
    return `<div style="background:#131720;border-radius:6px;padding:.4rem .6rem;font-size:.75rem">
      <div style="display:flex;justify-content:space-between;margin-bottom:.3rem">
        <span style="color:#94a3b8">${label}</span>
        <span style="color:#64748b">${valText}</span>
      </div>
      <div style="position:relative;height:6px;background:#1e2537;border-radius:3px;overflow:hidden">
        <div style="position:absolute;left:0;top:0;height:100%;width:${rmsPct}%;background:${rmsColor};border-radius:3px;transition:width .3s"></div>
        <div style="position:absolute;left:${peakPct}%;top:0;width:2px;height:100%;background:#f8fafc;opacity:.6"></div>
      </div>
    </div>`;
  }

  async function applySignalPath() {
    const btn = document.getElementById('spApplyBtn');
    const status = document.getElementById('spStatus');
    btn.disabled = true;
    status.textContent = 'Applying…';
    status.style.color = '#94a3b8';
    try {
      const body = {
        source: document.getElementById('spSource').value,
        preset: parseInt(document.getElementById('spPreset').value, 10),
      };
      const r = await fetch('/api/signal-path/apply', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const d = await r.json();
      if (!r.ok) {
        status.textContent = 'Error: ' + (d.detail || r.statusText);
        status.style.color = '#f87171';
      } else {
        status.textContent = `Applied — source: ${d.source}, preset: ${d.preset}${d.routing_applied ? ', routing matrix set' : ''}.`;
        status.style.color = '#4ade80';
        // Refresh diagram to reflect new device state
        setTimeout(_refreshLevels, 300);
      }
    } catch (e) {
      status.textContent = 'Error: ' + e.message;
      status.style.color = '#f87171';
    }
    btn.disabled = false;
  }

  // ── Workflow navigation ──────────────────────────────────────────────────
  let _currentPhase = parseInt(localStorage.getItem('wf_phase') || '1');

  function showPhase(n) {
    _currentPhase = n;
    localStorage.setItem('wf_phase', n);
    // Stop level meter polling when leaving the signal path phase
    if (n !== 2 && _spLevelTimer) { clearInterval(_spLevelTimer); _spLevelTimer = null; }
    [1, 2, 3, 4].forEach(i => {
      const c = document.getElementById('phase' + i + 'Content');
      if (c) c.style.display = (i === n) ? 'block' : 'none';
      const s = document.getElementById('navStep' + i);
      if (!s) return;
      s.classList.remove('active', 'done', 'locked');
      if (i < n) s.classList.add('done');
      else if (i === n) s.classList.add('active');
      else s.classList.add('locked');
    });
    if (n === 1) initEquipmentSetup();
    if (n === 2) loadSignalPathConfig();
  }

  // ── Equipment Setup ──────────────────────────────────────────────────────
  let _denonHost = null;
  let _denonInputs = [];
  let _denonConfirmedInput = null;

  async function initEquipmentSetup() {
    // Load existing Denon state from config
    try {
      const r = await fetch('/api/equipment/denon/state');
      const d = await r.json();
      if (d.connected) {
        _denonHost = d.host;
        document.getElementById('eqDenonHost').value = d.host || '';
        _populateDenonInputs(d.inputs, d.configured_sweep_input);
        document.getElementById('eqDenonInputSection').style.display = '';
        document.getElementById('eqDenonBadge').className = 'check-badge pass';
        document.getElementById('eqDenonBadge').textContent = 'OK';
        document.getElementById('eqDenonSaveBtn').disabled = false;
      } else if (d.host) {
        document.getElementById('eqDenonHost').value = d.host;
        document.getElementById('eqDenonDiscoverStatus').textContent = d.error || 'Offline';
      }
    } catch (_) {}

    // Load miniDSP status + label config
    dspTest(true);  // silent initial check

    // Load speakers from DB
    _loadSpeakers();
  }

  async function denonDiscover() {
    const btn = document.getElementById('eqDenonDiscoverBtn');
    const status = document.getElementById('eqDenonDiscoverStatus');
    btn.disabled = true;
    btn.textContent = 'Scanning…';
    status.textContent = 'Running SSDP discovery (up to 10s)…';
    status.style.color = '#94a3b8';
    try {
      const r = await fetch('/api/equipment/denon/discover', { method: 'POST' });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || r.statusText);
      _denonHost = d.host;
      document.getElementById('eqDenonHost').value = d.host;
      status.textContent = `Found at ${d.host}`;
      status.style.color = '#4ade80';
      // Fetch live state to get input list
      const r2 = await fetch('/api/equipment/denon/state');
      if (!r2.ok) throw new Error(`State fetch failed: ${r2.status}`);
      const d2 = await r2.json();
      if (d2.connected) {
        _populateDenonInputs(d2.inputs, d2.configured_sweep_input);
        document.getElementById('eqDenonInputSection').style.display = '';
        document.getElementById('eqDenonBadge').className = 'check-badge pass';
        document.getElementById('eqDenonBadge').textContent = 'OK';
        document.getElementById('eqDenonSaveBtn').disabled = false;
      }
    } catch (e) {
      status.textContent = 'Error: ' + e.message;
      status.style.color = '#f87171';
    }
    btn.disabled = false;
    btn.textContent = 'Discover';
  }

  function _populateDenonInputs(inputs, selectedInput) {
    const sel = document.getElementById('eqDenonInput');
    _denonInputs = inputs || [];
    sel.innerHTML = '<option value="">— select input —</option>' +
      _denonInputs.map(inp => `<option value="${inp}"${inp === selectedInput ? ' selected' : ''}>${inp}</option>`).join('');
    if (selectedInput) _denonConfirmedInput = selectedInput;
  }

  async function denonTestInput() {
    const host = document.getElementById('eqDenonHost').value.trim() || _denonHost;
    const input = document.getElementById('eqDenonInput').value;
    if (!host || !input) {
      document.getElementById('eqDenonTestStatus').textContent = 'Select an input first.';
      return;
    }
    const btn = document.getElementById('eqDenonTestBtn');
    const status = document.getElementById('eqDenonTestStatus');
    btn.disabled = true;
    btn.textContent = 'Switching…';
    status.textContent = '';
    document.getElementById('eqDenonHearRow').style.display = 'none';
    try {
      const r = await fetch('/api/equipment/denon/test-input', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ host, input }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || r.statusText);
      if (d.tone_played) {
        status.textContent = `Switched to ${input} and played 440 Hz tone via Pi HDMI.`;
      } else {
        status.textContent = `Switched to ${input}. (Tone not played: ${d.tone_error || 'no HDMI audio device'})`;
      }
      status.style.color = '#94a3b8';
      document.getElementById('eqDenonHearRow').style.display = '';
    } catch (e) {
      status.textContent = 'Error: ' + e.message;
      status.style.color = '#f87171';
    }
    btn.disabled = false;
    btn.textContent = 'Test';
  }

  function denonConfirmInput(heard) {
    const input = document.getElementById('eqDenonInput').value;
    document.getElementById('eqDenonHearRow').style.display = 'none';
    if (heard) {
      _denonConfirmedInput = input;
      document.getElementById('eqDenonTestStatus').textContent = `✓ Confirmed: Pi HDMI → ${input}`;
      document.getElementById('eqDenonTestStatus').style.color = '#4ade80';
      document.getElementById('eqDenonSaveBtn').disabled = false;
    } else {
      document.getElementById('eqDenonTestStatus').textContent = 'Try another input from the dropdown.';
      document.getElementById('eqDenonTestStatus').style.color = '#f87171';
    }
  }

  async function denonSave() {
    const host = document.getElementById('eqDenonHost').value.trim() || _denonHost;
    const sweepInput = _denonConfirmedInput || document.getElementById('eqDenonInput').value || null;
    const btn = document.getElementById('eqDenonSaveBtn');
    const status = document.getElementById('eqDenonSaveStatus');
    btn.disabled = true;
    try {
      const r = await fetch('/api/equipment/denon/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ host: host || null, sweep_input: sweepInput }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || r.statusText);
      status.textContent = 'Saved to config.yaml';
      status.style.color = '#4ade80';
      document.getElementById('eqDenonBadge').className = 'check-badge pass';
      document.getElementById('eqDenonBadge').textContent = 'Saved';
    } catch (e) {
      status.textContent = 'Error: ' + e.message;
      status.style.color = '#f87171';
    }
    btn.disabled = false;
  }

  async function dspTest(silent = false) {
    const badge = document.getElementById('eqDspBadge');
    const status = document.getElementById('eqDspStatus');
    if (!silent) {
      badge.className = 'check-badge running';
      badge.textContent = '…';
      status.textContent = 'Connecting…';
      status.style.color = '#94a3b8';
    }
    try {
      const r = await fetch('/api/preflight/minidsp-combined');
      const d = await r.json();
      badge.className = 'check-badge ' + (d.passed ? 'pass' : 'fail');
      badge.textContent = d.passed ? 'OK' : 'FAIL';
      if (!silent) {
        status.textContent = d.detail || '';
        status.style.color = d.passed ? '#4ade80' : '#f87171';
      }
      // Load existing labels from config
      if (d.passed) _loadDspLabels();
    } catch (e) {
      badge.className = 'check-badge fail';
      badge.textContent = 'ERR';
      if (!silent) { status.textContent = e.message; status.style.color = '#f87171'; }
    }
  }

  async function _loadDspLabels() {
    try {
      const r = await fetch('/api/signal-path');
      if (!r.ok) return;
      const cfg = await r.json();
      const conn = (cfg.connections || {}).minidsp || {};
      const inputs = conn.inputs || {};
      const outputs = conn.outputs || {};
      if (inputs['0']) document.getElementById('eqDspIn0').value = inputs['0'];
      if (inputs['1']) document.getElementById('eqDspIn1').value = inputs['1'];
      if (outputs['0']) document.getElementById('eqDspOut0').value = outputs['0'];
      if (outputs['1']) document.getElementById('eqDspOut1').value = outputs['1'];
      if (outputs['2']) document.getElementById('eqDspOut2').value = outputs['2'];
      if (outputs['3']) document.getElementById('eqDspOut3').value = outputs['3'];
    } catch (_) {}
  }

  async function dspSave() {
    const btn = document.getElementById('eqDspSaveBtn');
    const status = document.getElementById('eqDspStatus');
    btn.disabled = true;
    try {
      const r = await fetch('/api/equipment/minidsp/save-labels', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          inputs: [
            document.getElementById('eqDspIn0').value,
            document.getElementById('eqDspIn1').value,
          ],
          outputs: [
            document.getElementById('eqDspOut0').value,
            document.getElementById('eqDspOut1').value,
            document.getElementById('eqDspOut2').value,
            document.getElementById('eqDspOut3').value,
          ],
        }),
      });
      if (!r.ok) throw new Error(await r.text());
      status.textContent = 'Labels saved to config.yaml';
      status.style.color = '#4ade80';
    } catch (e) {
      status.textContent = 'Error: ' + e.message;
      status.style.color = '#f87171';
    }
    btn.disabled = false;
  }

  // Speaker management
  let _speakers = [];

  async function _loadSpeakers() {
    try {
      const r = await fetch('/api/equipment/speakers');
      _speakers = await r.json();
    } catch (_) { _speakers = []; }
    _renderSpeakers();
  }

  const SPK_TYPE_LABELS = {
    subwoofer: 'Subwoofer', front_l: 'Front L', front_r: 'Front R',
    center: 'Center', surround_l: 'Surround L', surround_r: 'Surround R',
    height_l: 'Height L', height_r: 'Height R', bass_shaker: 'Bass Shaker',
  };

  function _renderSpeakers() {
    const list = document.getElementById('eqSpeakerList');
    const empty = document.getElementById('eqSpeakerEmpty');
    empty.style.display = _speakers.length ? 'none' : '';
    list.innerHTML = _speakers.map(s => {
      const d = s.data || {};
      const meta = [d.room_location, d.port_tune_hz ? `${d.port_tune_hz} Hz tune` : null].filter(Boolean).join(' · ');
      return `<div style="display:flex;align-items:center;gap:.5rem;background:#131720;border-radius:6px;padding:.5rem .75rem">
        <div style="flex:1">
          <div style="font-size:.82rem;color:#cbd5e1;font-weight:500">${SPK_TYPE_LABELS[s.type] || s.type} — ${s.label || '(no label)'}</div>
          ${meta ? `<div style="font-size:.72rem;color:#64748b;margin-top:.1rem">${meta}</div>` : ''}
        </div>
        <button onclick="deleteSpeaker(${s.id})" style="background:transparent;color:#475569;border:none;font-size:1rem;padding:.2rem .4rem;cursor:pointer" title="Remove">&times;</button>
      </div>`;
    }).join('');
  }

  function toggleAddSpeaker() {
    const form = document.getElementById('eqAddSpeakerForm');
    form.style.display = form.style.display === 'none' ? '' : 'none';
    // Show/hide port tune for subwoofer types
    document.getElementById('eqSpkType').onchange = () => {
      const t = document.getElementById('eqSpkType').value;
      document.getElementById('eqSpkPortRow').style.display =
        (t === 'subwoofer' || t === 'bass_shaker') ? '' : 'none';
    };
    document.getElementById('eqSpkType').dispatchEvent(new Event('change'));
  }

  async function saveSpeaker() {
    const type = document.getElementById('eqSpkType').value;
    const label = document.getElementById('eqSpkLabel').value.trim();
    const location = document.getElementById('eqSpkLocation').value.trim();
    const portTune = parseFloat(document.getElementById('eqSpkPortTune').value);
    const data = {};
    if (location) data.room_location = location;
    if (!isNaN(portTune)) data.port_tune_hz = portTune;
    try {
      const r = await fetch('/api/equipment/speakers', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ type, label: label || null, data }),
      });
      if (!r.ok) throw new Error(await r.text());
      // Reset and close form
      document.getElementById('eqSpkLabel').value = '';
      document.getElementById('eqSpkLocation').value = '';
      document.getElementById('eqSpkPortTune').value = '';
      toggleAddSpeaker();
      await _loadSpeakers();
    } catch (e) {
      alert('Error saving speaker: ' + e.message);
    }
  }

  async function deleteSpeaker(id) {
    try {
      const r = await fetch('/api/equipment/speakers/' + id, { method: 'DELETE' });
      if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.detail || r.statusText); }
      await _loadSpeakers();
    } catch (e) {
      alert('Error deleting: ' + e.message);
    }
  }

  // Restore phase from localStorage on load
  showPhase(_currentPhase);

  loadMics();
  loadSignalPathConfig();
  loadHistory().then(() => {
    const urlParams = new URLSearchParams(window.location.search);
    const urlSession = urlParams.get('session');
    if (urlSession) loadSession(parseInt(urlSession, 10));
  });

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


# ── Version / upgrade helpers ─────────────────────────────────────────────────

async def _fetch_latest_sha() -> Optional[str]:
    """Fetch the latest git SHA from GHCR manifest index annotations.

    Two-step: anonymous token → manifest index. Returns None on any failure.
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


@app.get("/api/version")
async def api_version() -> dict:
    """Return current and latest git SHAs plus the semantic version. Cached for 1 hour."""
    current_sha = os.environ.get("BUILD_SHA", "unknown")

    cached = _version_cache.get("result")
    if cached and cached.get("expires", 0) > time.time():
        latest_sha = cached.get("latest_sha")
        checked_at = cached.get("checked_at")
    else:
        latest_sha = await _fetch_latest_sha()
        checked_at = time.time()
        _version_cache["result"] = {
            "latest_sha": latest_sha,
            "expires": checked_at + _VERSION_CACHE_TTL,
            "checked_at": checked_at,
        }

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


class TimeAlignRequest(BaseModel):
    sub_session_id: int
    mains_session_id: int


def _bandpass_fft(ir: list[float], f_lo: float, f_hi: float, sample_rate: int) -> list[float]:
    """FFT-domain soft bandpass filter. Pure numpy — no scipy dependency."""
    import numpy as np
    arr = np.array(ir, dtype=np.float64)
    N = len(arr)
    freqs = np.fft.rfftfreq(N, 1.0 / sample_rate)
    H = np.fft.rfft(arr)
    mask = ((freqs >= f_lo) & (freqs <= f_hi)).astype(np.float64)
    return np.fft.irfft(H * mask, n=N).tolist()


def compute_time_offset_ms(
    ir1: list[float],
    ir2: list[float],
    f_lo: float = 60.0,
    f_hi: float = 100.0,
    sample_rate: int = 48000,
) -> float:
    """Compute time offset between two IRs via bandpass cross-correlation.

    Returns lag in milliseconds (positive = ir1 leads ir2).
    """
    import numpy as np
    bp1 = np.array(_bandpass_fft(ir1, f_lo, f_hi, sample_rate))
    bp2 = np.array(_bandpass_fft(ir2, f_lo, f_hi, sample_rate))
    corr = np.correlate(bp1, bp2, mode="full")
    lag_samples = int(np.argmax(np.abs(corr))) - (len(bp2) - 1)
    return lag_samples / sample_rate * 1000.0


@app.post("/api/sessions/time-align")
async def time_align(body: TimeAlignRequest) -> dict:
    """Estimate time offset between sub and mains via bandpass cross-correlation.

    Both sessions must have an impulse response stored (re-measure if missing).
    """
    store = SessionStore()
    sub_session = store.get_session(body.sub_session_id)
    if sub_session is None:
        raise HTTPException(status_code=404, detail=f"Session #{body.sub_session_id} not found")
    mains_session = store.get_session(body.mains_session_id)
    if mains_session is None:
        raise HTTPException(status_code=404, detail=f"Session #{body.mains_session_id} not found")

    if sub_session.impulse_response is None:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "IR_NOT_AVAILABLE",
                "message": f"Session #{body.sub_session_id} has no IR — re-measure to enable phase check.",
            },
        )
    if mains_session.impulse_response is None:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "IR_NOT_AVAILABLE",
                "message": f"Session #{body.mains_session_id} has no IR — re-measure to enable phase check.",
            },
        )

    offset_ms = compute_time_offset_ms(
        sub_session.impulse_response,
        mains_session.impulse_response,
    )
    offset_feet = abs(offset_ms) * 1.13
    sub_leads = offset_ms > 0

    if sub_leads:
        rec = (
            f"Sub leads mains by {abs(offset_ms):.1f} ms ({offset_feet:.1f} ft). "
            f"Increase AVR sub distance by {offset_feet:.1f} feet."
        )
    else:
        rec = (
            f"Sub lags mains by {abs(offset_ms):.1f} ms ({offset_feet:.1f} ft). "
            f"Decrease AVR sub distance by {offset_feet:.1f} feet."
        )

    return {
        "offset_ms": round(offset_ms, 2),
        "offset_feet": round(offset_feet, 2),
        "sub_leads": sub_leads,
        "recommendation": rec,
    }


class CardioidRequest(BaseModel):
    enabled: bool
    delay_ms: Optional[float] = None


@app.post("/api/signal-path/cardioid")
async def cardioid_mode(body: CardioidRequest) -> dict:
    """Enable or disable cardioid sub array mode on output index 1.

    Cardioid: output 1 gets inverted polarity + computed delay.
    Requires 2+ sub_outputs in config. Delay defaults to sub_separation_m / 343 * 1000 ms.
    """
    from .adapters.minidsp import MinidspClient, MinidspApiError

    cfg = _load_config()
    sub_outputs = (cfg.minidsp.get("signal_path") or {}).get("sub_outputs", []) if cfg.minidsp else []
    if len(sub_outputs) < 2:
        raise HTTPException(
            status_code=422,
            detail="cardioid mode requires 2+ sub outputs configured in config.yaml",
        )

    sep_m: float = cfg.minidsp.get("sub_separation_m", 1.0) if cfg.minidsp else 1.0
    delay_ms = body.delay_ms if body.delay_ms is not None else round(sep_m / 343.0 * 1000.0, 2)

    host = cfg.minidsp.get("host", "localhost") if cfg.minidsp else "localhost"
    port = cfg.minidsp.get("port", 5380) if cfg.minidsp else 5380
    client = MinidspClient(host=host, port=port)

    try:
        if body.enabled:
            await client.set_output_polarity(1, inverted=True)
            await client.set_output_delay(1, delay_ms)
        else:
            await client.set_output_polarity(1, inverted=False)
            await client.set_output_delay(1, 0.0)
    except MinidspApiError as exc:
        if exc.status_code == 404:
            return {
                "status": "advisory_only",
                "message": "Polarity inversion not supported by this hardware. Set manually in miniDSP app.",
                "delay_ms": delay_ms,
            }
        raise HTTPException(status_code=502, detail=f"miniDSP error: {exc}")

    return {
        "status": "ok",
        "enabled": body.enabled,
        "delay_ms": delay_ms if body.enabled else 0.0,
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
            "has_ir": s.impulse_response is not None,
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


# ── Signal Path endpoints ─────────────────────────────────────────────────────

class SignalPathApplyRequest(BaseModel):
    source: Optional[str] = None   # Analog | Toslink | USB
    preset: Optional[int] = None   # 0-3


@app.get("/api/signal-path")
async def get_signal_path_config() -> dict:
    """Return the signal_path section from config.yaml.

    Returns source, preset, and routing list from config. If signal_path is not
    configured, returns empty defaults so the UI can still render.
    """
    cfg = _load_config()
    sp = cfg.minidsp.get("signal_path") or {}
    routing = sp.get("routing") or []
    return {
        "source": sp.get("source"),
        "preset": sp.get("preset"),
        "routing": routing,
        "connections": cfg.connections,
    }


@app.post("/api/signal-path/apply")
async def apply_signal_path(body: SignalPathApplyRequest) -> dict:
    """Apply source and preset (and routing from config) to the miniDSP device.

    Only the fields in the request body are applied. Routing is always applied
    from config if present.
    """
    from .adapters.minidsp import MinidspClient, MinidspApiError, VALID_SOURCES, MAX_PRESET_INDEX

    cfg = _load_config()
    host = cfg.minidsp.get("host", "localhost")
    port = cfg.minidsp.get("port", 5380)
    client = MinidspClient(host=host, port=port)

    if body.source is not None and body.source not in VALID_SOURCES:
        raise HTTPException(
            status_code=422,
            detail=f"source must be one of {sorted(VALID_SOURCES)}",
        )
    if body.preset is not None and not (0 <= body.preset <= MAX_PRESET_INDEX):
        raise HTTPException(
            status_code=422,
            detail=f"preset must be 0-{MAX_PRESET_INDEX}",
        )

    try:
        if body.preset is not None:
            await client.switch_preset(body.preset)
        if body.source is not None:
            await client.switch_source(body.source)

        routing_applied = False
        sp = cfg.minidsp.get("signal_path") or {}
        routing = sp.get("routing") or []
        for entry in routing:
            input_idx = entry.get("input", 0)
            enabled_outputs = set(entry.get("outputs", []))
            output_enabled = {i: (i in enabled_outputs) for i in range(4)}
            await client.set_input_routing(input_idx, output_enabled)
            routing_applied = True

    except MinidspApiError as exc:
        raise HTTPException(status_code=502, detail=f"miniDSP error: {exc}")

    return {
        "status": "ok",
        "source": body.source,
        "preset": body.preset,
        "routing_applied": routing_applied,
    }


@app.get("/api/signal-path/device-state")
async def get_device_state() -> dict:
    """Read the current master status from the miniDSP device.

    Returns preset, source, volume, and mute state as reported by minidspd.
    """
    from .adapters.minidsp import MinidspClient, MinidspApiError

    cfg = _load_config()
    host = cfg.minidsp.get("host", "localhost")
    port = cfg.minidsp.get("port", 5380)
    client = MinidspClient(host=host, port=port)

    try:
        status = await client.get_device_status()
    except MinidspApiError as exc:
        raise HTTPException(status_code=502, detail=f"miniDSP error: {exc}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Cannot reach miniDSP: {exc}")

    return status


# ── Equipment setup routes ───────────────────────────────────────────────────


class DenonSaveBody(BaseModel):
    host: Optional[str] = None
    sweep_input: Optional[str] = None


class DenonTestInputBody(BaseModel):
    host: str
    input: str


class MinidspLabelsBody(BaseModel):
    inputs: list[Optional[str]] = []
    outputs: list[Optional[str]] = []


class SpeakerBody(BaseModel):
    type: str
    label: Optional[str] = None
    data: Optional[dict] = None  # open blob: manufacturer, model, room_location, port_tune_hz, etc.


class SpeakerUpdateBody(BaseModel):
    type: Optional[str] = None
    label: Optional[str] = None
    data: Optional[dict] = None


@app.get("/api/equipment/denon/state")
async def equipment_denon_state() -> dict:
    """Live Denon AVR state: model, current input, all inputs, volume, mute."""
    import denonavr
    cfg = _load_config()
    host = cfg.denon.get("host")
    if not host:
        return {"connected": False, "error": "No host configured — use discover first"}
    try:
        receiver = denonavr.DenonAVR(host)
        await receiver.async_setup()
        await receiver.async_update()
        return {
            "connected": True,
            "host": host,
            "model": receiver.model_name,
            "current_input": receiver.input_func,
            "inputs": sorted(receiver.input_func_list or []),
            "volume": receiver.volume,
            "mute": receiver.mute,
            "configured_sweep_input": cfg.measurement.get("denon_sweep_input"),
        }
    except Exception as exc:
        return {"connected": False, "host": host, "error": str(exc)}


@app.post("/api/equipment/denon/discover")
async def equipment_denon_discover() -> dict:
    """SSDP discovery — finds Denon AVR on local network."""
    import denonavr
    try:
        devices = await asyncio.wait_for(denonavr.async_discover(), timeout=10.0)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=408, detail="SSDP discovery timed out (10s)")
    if not devices:
        raise HTTPException(status_code=404, detail="No Denon AVR found on network")
    host = devices[0].get("host")
    if not host:
        raise HTTPException(status_code=502, detail="Device found but no IP address returned")
    return {"host": host}


@app.post("/api/equipment/denon/save")
async def equipment_denon_save(body: DenonSaveBody) -> dict:
    """Persist Denon host and/or sweep input to config.yaml."""
    updates: dict = {}
    if body.host is not None:
        updates["denon"] = {"host": body.host}
    if body.sweep_input is not None:
        updates.setdefault("measurement", {})["denon_sweep_input"] = body.sweep_input
    if not updates:
        raise HTTPException(status_code=422, detail="Nothing to save")
    try:
        update_config(updates, path=CONFIG_PATH)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to write config: {exc}")
    return {"status": "ok",
            **({"host": body.host} if body.host is not None else {}),
            **({"sweep_input": body.sweep_input} if body.sweep_input is not None else {})}


@app.post("/api/equipment/denon/test-input")
async def equipment_denon_test_input(body: DenonTestInputBody) -> dict:
    """Switch Denon to the given input, then play a 2-second 440 Hz tone via Pi HDMI.

    Returns whether the switch succeeded and whether the tone was played.
    The browser shows "did you hear it?" — on yes, the caller saves this input.
    """
    import denonavr

    try:
        receiver = denonavr.DenonAVR(body.host)
        await receiver.async_setup()
        await receiver.async_update()
        await receiver.async_set_input_func(body.input)
        await asyncio.sleep(0.8)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Denon control failed: {exc}")

    tone_played = False
    tone_error = None
    try:
        import numpy as np
        import sounddevice as sd

        sample_rate = 48000
        duration = 2.0
        t = np.linspace(0, duration, int(sample_rate * duration), endpoint=False)
        # 440 Hz with 50 ms fade-in/out to avoid clicks
        tone = (np.sin(2 * np.pi * 440 * t) * 0.4).astype(np.float32)
        env = np.minimum(np.minimum(t / 0.05, 1.0), (duration - t) / 0.05).astype(np.float32)
        tone *= env

        # Prefer configured HDMI device; fall back to first HDMI output found
        cfg = _load_config()
        configured_hdmi = cfg.measurement.get("hdmi_playback_device")
        hdmi_device = None
        devices = sd.query_devices()
        if configured_hdmi:
            for idx, dev in enumerate(devices):
                if dev["max_output_channels"] > 0 and configured_hdmi.lower() in dev["name"].lower():
                    hdmi_device = idx
                    break
        if hdmi_device is None:
            for idx, dev in enumerate(devices):
                if dev["max_output_channels"] > 0 and "hdmi" in dev["name"].lower():
                    hdmi_device = idx
                    break

        await asyncio.to_thread(sd.play, tone.reshape(-1, 1), sample_rate, device=hdmi_device)
        await asyncio.to_thread(sd.wait)
        tone_played = True
    except Exception as exc:
        tone_error = str(exc)

    return {
        "switched": True,
        "input": body.input,
        "tone_played": tone_played,
        "tone_error": tone_error,
    }


@app.post("/api/equipment/minidsp/save-labels")
async def equipment_minidsp_save_labels(body: MinidspLabelsBody) -> dict:
    """Save miniDSP I/O labels to config.yaml under connections.minidsp."""
    inputs_dict = {str(i): lbl for i, lbl in enumerate(body.inputs) if lbl}
    outputs_dict = {str(i): lbl for i, lbl in enumerate(body.outputs) if lbl}
    # Merge with existing connections.minidsp to preserve other fields (e.g. sweep_input)
    cfg = Config.load(path=CONFIG_PATH)
    existing_minidsp = cfg.connections.get("minidsp", {})
    merged_minidsp = {**existing_minidsp, "inputs": inputs_dict, "outputs": outputs_dict}
    try:
        update_config({"connections": {"minidsp": merged_minidsp}}, path=CONFIG_PATH)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to write config: {exc}")
    return {"status": "ok"}


@app.get("/api/equipment/speakers")
async def equipment_speakers_list() -> list[dict]:
    return SessionStore().list_equipment()


@app.post("/api/equipment/speakers")
async def equipment_speakers_create(body: SpeakerBody) -> dict:
    return SessionStore().save_equipment(type=body.type, label=body.label, data=body.data)


@app.put("/api/equipment/speakers/{speaker_id}")
async def equipment_speakers_update(speaker_id: int, body: SpeakerUpdateBody) -> dict:
    result = SessionStore().update_equipment(speaker_id, label=body.label, data=body.data)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Speaker {speaker_id} not found")
    return result


@app.delete("/api/equipment/speakers/{speaker_id}")
async def equipment_speakers_delete(speaker_id: int) -> dict:
    if not SessionStore().delete_equipment(speaker_id):
        raise HTTPException(status_code=404, detail=f"Speaker {speaker_id} not found")
    return {"status": "deleted", "id": speaker_id}


# ── Preflight routes ─────────────────────────────────────────────────────────

_PREFLIGHT_CHECK_MAP: dict[str, str] = {
    # Combined checks (used by run_all and the UI)
    "minidsp-combined": "check_minidsp_combined",
    "denon-playback": "check_denon_and_playback",
    # Individual checks (available for debugging / later phases)
    "hidraw": "check_hidraw",
    "mic": "check_mic",
    "minidsp": "check_minidsp",
    "denon": "check_denon",
    "playback": "check_playback_route",
    "signal-path": "check_signal_path_sync",
    "config": "check_config",
}


@app.get("/api/preflight")
async def preflight_all() -> list[dict]:
    cfg = _load_config()
    checker = PreflightChecker(cfg)
    results = await checker.run_all()
    return [dataclasses.asdict(r) for r in results]


@app.get("/api/preflight/{check_name}")
async def preflight_check(check_name: str) -> dict:
    if check_name not in _PREFLIGHT_CHECK_MAP:
        raise HTTPException(status_code=404, detail=f"Unknown check: {check_name}")
    cfg = _load_config()
    checker = PreflightChecker(cfg)
    method = getattr(checker, _PREFLIGHT_CHECK_MAP[check_name])
    result = await method()
    return dataclasses.asdict(result)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_config() -> Config:
    if not CONFIG_PATH.exists():
        raise HTTPException(
            status_code=503,
            detail=f"No config at {CONFIG_PATH}. Run 'calibrate check' first.",
        )
    return Config.load(CONFIG_PATH)
