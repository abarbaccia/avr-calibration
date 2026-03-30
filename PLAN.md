<!-- /autoplan restore point: /home/andrew/.gstack/projects/abarbaccia-avr-calibration/main-autoplan-restore-20260330-000801.md -->
# Plan: Measurement Curve Viewer (History Click-to-View)

**Branch:** main
**Feature:** Click any row in the History table to load and display its frequency response curve

## Problem

The webapp already plots the FR curve after a fresh measurement, but the history table
is display-only — you can see a session's peak SPL and timestamp, but you can't view
the actual curve for any past session. You'd have to re-measure to see a curve.

## Goal

Click a row in the History table → the FR chart above updates to show that session's curve.
If the session has both `start_fr` and `end_fr` (before/after EQ), show both overlaid.

## Current State

- `GET /api/sessions` returns summary rows: `{id, timestamp, label, peak_spl, freq_at_peak, n_freqs, has_end_fr}` — no FR data
- `SessionStore.get_session(id)` already returns a full `Session` with `start_fr` + optional `end_fr`
- `FrequencyResponse` has `frequencies: list[float]` + `spl: list[float]`
- `renderFR(freqs, spl)` already exists in the JS and uses Chart.js (already loaded)

## Implementation Plan

### Step 1: Add `GET /api/sessions/{session_id}` endpoint (web.py)
Return full FR data for one session:
```json
{
  "id": 3,
  "label": "after EQ",
  "timestamp": "...",
  "start_fr": {"frequencies_hz": [...], "spl_dbfs": [...]},
  "end_fr": {"frequencies_hz": [...], "spl_dbfs": [...]} | null
}
```

### Step 2: Make history rows clickable (JS in web.py HTML)
- Add `onclick="loadSession(${s.id})"` to each `<tr>` in `loadHistory()`
- Highlight the selected row
- `loadSession(id)` — fetch `/api/sessions/{id}`, call `renderFR()` with the data

### Step 3: Update `renderFR()` to support overlay (before/after)
- Accept an optional second dataset (end_fr)
- If present, render two lines: "Before EQ" (blue) and "After EQ" (green)
- Chart title shows session label + date

### Step 4: Tests for the new endpoint (tests/test_web.py)
- `test_get_session_returns_fr_data` — happy path: session exists, returns frequencies + spl
- `test_get_session_404` — session not found returns 404
- `test_get_session_with_end_fr` — session with end_fr includes both datasets

## Files Changed
- `calibrate/web.py` — new endpoint + updated JS

## Approved Scope (post /autoplan gate — 2026-03-30)

### In scope
- `GET /api/sessions/{session_id}` endpoint — full FR data
- Clickable history rows with `.selected` highlight
- `renderFR()` updated for before/after EQ overlay + null guard
- PNG export button (~10 LOC)
- **Harman target overlay** — dashed reference line on chart (~15 LOC JS)
- **URL deep linking** — `?session=3` on load, `history.pushState()` on click (~15 LOC JS)
- Null/corrupt FR defensive handling in `_row_to_session()` (fixes pre-existing `list_sessions` crash too)
- Guard `peak_spl` / `freq_at_peak` for empty spl list

### Not in scope
- Waterfall/decay plots (TODO-R3)
- Delta-from-Harman column in history table (TODO-CV2 — requires analysis pipeline)
- Inline sparklines per history row (deferred)

---

# CEO Review — Phase 1

## System Audit

- **Recent activity (30 days):** Docker pipeline, USB/HDMI sub-alignment, arm/v7 fixes dominate. `calibrate/web.py` touched 6 times.
- **Stash:** `stash@{0}` from `feature/history-show` branch — just a version bump, not related.
- **TODOS interaction:** TODO-R2 (satellite correction path) unaffected. TODO-R3 (waterfall/decay) — this plan is a prerequisite.
- **No TODOs/FIXMEs in web.py.**
- **Taste calibration:** `SessionStore` and `FrequencyResponse` serialization are well-designed. The `_HTML` string in `web.py` is getting long (~340 LOC of embedded HTML/JS) — acceptable for a Pi single-file app, but worth noting.

## 0A. Premise Challenge

1. **Right problem?** Yes. The history table currently shows `peak_spl` + timestamp but you can't view the actual FR curve for any past session. The FR curve IS the measurement. Not seeing it from history defeats the purpose of storing history.

2. **Assumed premises that could be wrong:**
   - `start_fr` is always valid JSON in the DB — **FALSE for aborted/interrupted measurements.** Must handle null/malformed start_fr. (HIGH)
   - `FrequencyResponse.from_json()` handles old schema — **TRUE**, backward compat already in place (`.setdefault("warnings", [])`).
   - `renderFR()` can trivially accept a second dataset — **TRUE**, Chart.js multi-dataset is straightforward.

3. **What if we do nothing?** History table is a log of timestamps and peak values. You can't use it to understand whether calibration is improving. Material loss of tool value.

## 0B. Existing Code Leverage

| Sub-problem | Existing code |
|---|---|
| Fetch full session | `SessionStore.get_session(id)` — already returns `Session` with `start_fr` + `end_fr` |
| FR serialization | `FrequencyResponse.to_json()` / `from_json()` — already handles backward compat |
| Chart rendering | `renderFR(freqs, spl)` already exists, Chart.js already loaded via CDN |
| Null FR handling | NOT YET — needs to be added |

Zero new storage work. This is the simplest possible implementation of the feature.

## 0C. Dream State Mapping

```
CURRENT STATE                      THIS PLAN                    12-MONTH IDEAL
History = list of sessions      Click row → FR curve loads    Timeline with trend view
with peak SPL + timestamp       Before/After EQ overlay        Harman target reference
No curve visibility             Export PNG                     Convergence progress bar
                                                               Session comparison mode
```

## 0C-bis. Implementation Alternatives

```
APPROACH A: Minimal (MVP)
  Summary: GET /api/sessions/{id} + clickable rows + single dataset rendering
  Effort:  S (~40 LOC)
  Risk:    Low
  Pros:    Tiny diff, re-uses all existing infrastructure, solves the stated need
  Cons:    No before/after overlay (most sessions only have start_fr)
  Reuses:  renderFR(), get_session(), FrequencyResponse

APPROACH B: Full plan (recommended)
  Summary: Same as A + before/after overlay for sessions with end_fr + PNG export + null handling
  Effort:  S (~80 LOC)
  Risk:    Low
  Pros:    Complete — handles end_fr case, export button is 10 LOC, marginal extra effort
  Cons:    None meaningful
  Reuses:  All of the above

APPROACH C: With Harman target overlay (TASTE DECISION)
  Summary: Approach B + Harman target curve as reference line on every chart
  Effort:  S (~100 LOC — need to embed Harman target data for 20-200 Hz range)
  Risk:    Low
  Pros:    Makes FR charts interpretable — users see if they're above/below target
           The system's entire purpose is to converge to Harman target
  Cons:    Requires embedding Harman target data (not yet in codebase)
           Scope expands beyond "see the curves from measurements"
  Reuses:  Approach B + new static data array
```

RECOMMENDATION: Approach B (full plan). Auto-decided: P1+P2 (complete, in blast radius, <1h CC).
Harman target overlay: TASTE DECISION — surfaces at final gate.

## CEO Dual Voices

**CODEX SAYS (CEO):** Unavailable — `codex` binary not found. [subagent-only]

**CLAUDE SUBAGENT (CEO — strategic independence):**

Key findings (independent review):

1. (HIGH) Harman target overlay belongs in R1 — "Showing a bare FR curve without [Harman target] is like showing a speedometer without a speed limit sign." 10 lines of JS. The regret scenario: touch this chart again in R2 for the overlay.

2. (HIGH) Null/corrupt FR data — `start_fr` assumed always valid; sessions from aborted measurements could have null or corrupt FR. Needs defensive handling in both API and JS.

3. (HIGH) `start_fr: null` case — old versions or aborted sessions. The API response spec doesn't document this. JS must show an error state, not a blank chart.

4. (MEDIUM) Visual convergence indicator — "delta from Harman target" column in history table makes every curve interpretable. Requires analysis pipeline; defer to TODO.

5. (MEDIUM) URL deep linking — `?session=3` on page load, `history.pushState()` when selecting. ~15 LOC. TASTE DECISION.

6. (LOW) Inline sparklines per row — premium R2 feature.

```
CEO DUAL VOICES — CONSENSUS TABLE:
═══════════════════════════════════════════════════════════════
  Dimension                           Claude  Codex  Consensus
  ──────────────────────────────────── ─────── ─────── ─────────
  1. Premises valid?                   No*     N/A    N/A (single model)
  2. Right problem to solve?           Yes     N/A    N/A
  3. Scope calibration correct?        Partial N/A    N/A
  4. Alternatives sufficiently explored? No    N/A    N/A
  5. Competitive/market risks covered? N/A     N/A    N/A
  6. 6-month trajectory sound?         Partial N/A    N/A
═══════════════════════════════════════════════════════════════
*Premise 1 invalid: start_fr assumed always valid; aborted sessions may have null FR
```

## CEO Review Sections

### Section 1: Goal & Outcome Mapping
Goal: "see curves from measurements." Outcome: understand calibration progress.
Gap: curves without Harman target reference are harder to interpret. TASTE DECISION flagged.

### Section 2: Error & Rescue Registry

| Error | Trigger | Handler | User sees |
|---|---|---|---|
| 404 from `/api/sessions/{id}` | Invalid id | HTTP 404 | "Session not found" |
| `null` start_fr in DB | Aborted measurement | Return `{start_fr: null}` | Error state in chart |
| Malformed FR JSON in DB | Corruption | `JSONDecodeError` → 500 | Server error |
| `renderFR()` called with null | null start_fr not guarded | JS TypeError | Blank/crash |
| `frChart.destroy()` on null | First load, no prior chart | Guard: `if (frChart)` — already in code | N/A (already handled) |

### Section 3: Failure Modes Registry

| Mode | Probability | Impact | Mitigation |
|---|---|---|---|
| Session with null start_fr | Low (aborted measurements) | Chart crash | Return `start_fr: null`, guard in JS |
| Session with 0 frequency points | Very low | Empty chart | Validate `len(fr.frequencies) > 0` |
| Network timeout fetching session | Low (Pi Zero on LAN) | Spinner hangs | Timeout + error message in JS |
| Chart canvas not destroyed | First-run only | Already handled by `if (frChart)` | N/A |

Critical gap: null start_fr handling is NOT in the current plan. Must add.

### Section 4: What is NOT in scope
- Harman target overlay (TASTE DECISION — surfaces at gate)
- URL deep linking (TASTE DECISION — surfaces at gate)
- Delta-from-target column (deferred → TODOS.md)
- Inline sparklines (deferred)
- Waterfall/decay (TODO-R3)

### Section 5: What already exists
- `SessionStore.get_session(id)` in `storage.py:115`
- `FrequencyResponse.to_json()` / `from_json()` in `measurement.py:57-64`
- `renderFR(freqs, spl)` in `web.py:381-421` (Chart.js, already loaded)
- `if (frChart) frChart.destroy()` — canvas reuse already handled

### Section 6: TODOS touched
- TODO-R2 (satellite correction path) — unaffected
- TODO-R3 (waterfall/decay) — this plan is a prerequisite
- TODO-3 (content-tagged feedback) — unaffected

### Section 7: Temporal interrogation
- **HOUR 1:** Endpoint live, rows clickable, curve loads
- **HOUR 2:** Before/after overlay works for sessions with end_fr
- **HOUR 3:** Tests pass, PR ready
- **POTENTIAL ISSUE:** Pi Zero 2 W is slow — FR data is tiny (~200 float pairs = ~3KB), no latency concern.

### Section 8: Security
- New endpoint `GET /api/sessions/{id}` — no auth on any existing endpoint. Consistent. Session IDs are sequential integers (low-entropy, but this is a LAN-only device; acceptable).
- No new attack surface beyond what already exists.

### Section 9: Observability
- Add `logger.info("session %d fetched", session_id)` to the new endpoint. Consistent with existing pattern.
- No metrics needed at this scale.

### Section 10: 6-Month Trajectory
Sound. This is a lake-boiling feature — all infrastructure exists. The one regret risk is Harman target overlay (flagged as TASTE DECISION). The delta-from-Harman column is a follow-on worth capturing in TODOS.md.

## Updated Plan (post CEO review)

### Step 1: Add `GET /api/sessions/{session_id}` endpoint (web.py)
Return full FR data. Handle `start_fr: null` explicitly:
```json
{
  "id": 3,
  "label": "after EQ",
  "timestamp": "...",
  "start_fr": {"frequencies_hz": [...], "spl_dbfs": [...]} | null,
  "end_fr": {"frequencies_hz": [...], "spl_dbfs": [...]} | null
}
```
Log the fetch. Return 404 if session not found.

### Step 2: Make history rows clickable
- `onclick="loadSession(${s.id})"` on `<tr>` in `loadHistory()`
- Add `.selected` CSS class + row highlight on click
- `loadSession(id)` — fetch, guard null start_fr, call `renderFR()`

### Step 3: Update `renderFR()` for overlay + null guard
- Accept `(startFreqs, startSpl, endFreqs=null, endSpl=null, label='')`
- If end data present: two lines — "Before EQ" (blue) + "After EQ" (green)
- Guard: if `startFreqs` is null, show "Measurement data unavailable"
- Chart subtitle: session label + date

### Step 4: Add PNG export button
- `<button onclick="exportChart()">Export PNG</button>` — shown when chart is visible
- `canvas.toDataURL('image/png')` → anchor download. ~10 LOC.

### Step 5: Tests (tests/test_web.py)
- `test_get_session_returns_fr_data` — happy path
- `test_get_session_404` — missing session
- `test_get_session_with_end_fr` — both datasets returned
- `test_get_session_null_start_fr` — malformed/null start_fr handled gracefully (returns 200 with `start_fr: null`, not 500)

## CEO Completion Summary

| Item | Status |
|---|---|
| Premises challenged | Done — 1 invalid premise found (null start_fr) |
| Alternatives evaluated | Done — 3 approaches, Approach B selected |
| Error & Rescue Registry | Done |
| Failure Modes Registry | Done |
| Dream state delta | Done |
| NOT in scope | Done |
| What already exists | Done |
| Dual voices | [subagent-only] — Codex unavailable |

## Decision Audit Trail

| # | Phase | Decision | Classification | Principle | Rationale | Rejected |
|---|-------|----------|----------------|-----------|-----------|---------|
| 1 | CEO | Include null start_fr handling in scope | Mechanical | P1 (completeness) | Aborted measurements can have null FR — JS crash if unhandled | None |
| 2 | CEO | Include PNG export button | Mechanical | P1+P2 | 10 LOC, in blast radius, catches the most common follow-up ask | Defer |
| 3 | CEO | Harman target overlay → TASTE DECISION | Taste | P3 vs P1 | P1 says include (trivial effort, high value), P3 says scope was "see measurements not target" | Deferred to gate |
| 4 | CEO | URL deep linking → TASTE DECISION | Taste | P3 vs P1 | ~15 LOC, good UX, but user didn't ask for it | Deferred to gate |
| 5 | CEO | Delta-from-Harman column → TODOS.md | Mechanical | P3 | Requires analysis pipeline not yet built | Added to TODO |
| 6 | CEO | Accept premises (all others) | Mechanical | P6 | Premises 2-5 confirmed by reading code | N/A |
| 7 | CEO | Mode = SELECTIVE EXPANSION | Mechanical | P1+P6 | Hold core scope, surface expansions as TASTE decisions | N/A |

---

# Eng Review — Phase 3

## Step 0: Scope Challenge

**Files touched:** `calibrate/web.py`, `tests/test_web.py` — 2 files. No complexity smell.
**New classes/services:** 0. No new abstractions.
**Distribution:** No new artifacts. Deploys via existing Docker pipeline / `hotfix.sh`.

Sub-problem → existing code map:
| Sub-problem | Existing code |
|---|---|
| Fetch full session by id | `SessionStore.get_session(id)` — `storage.py:115` |
| FR serialization | `FrequencyResponse.to_json()` / `from_json()` — `measurement.py:57-64` |
| Chart rendering | `renderFR()` — `web.py:381-421`, Chart.js loaded at `web.py:136` |
| API routing | FastAPI `@app.get()` pattern — all existing endpoints |

Search: unavailable. Proceeding with in-distribution knowledge only.

TODOS cross-reference:
- TODO-R3 (waterfall/decay) — this plan is a prerequisite
- No deferred items blocking this plan

## Eng Dual Voices

**CODEX SAYS (eng):** Unavailable — `codex` binary not found. [subagent-only]

**CLAUDE SUBAGENT (eng — independent review):**

6 findings. 3 are P1 silent failures.

1. [P1, 9/10] Field name mismatch — spec says `frequencies_hz`/`spl_dbfs`, but `FrequencyResponse` serializes as `frequencies`/`spl`. If endpoint returns `asdict(fr)`, JS gets `undefined` → silent blank chart.
2. [P1, 10/10] Null start_fr handling — `_row_to_session()` calls `from_json()` unconditionally; `NOT NULL` constraint prevents null but not corruption. Defensive `try/except JSONDecodeError` belongs in `_row_to_session`, not the endpoint.
3. [P1, 9/10] `renderFR()` null guard must precede `new Chart(ctx, {...})`. If guard fires inside the config object literal, JS crashes.
4. [P2, 10/10] Pre-existing: `GET /api/sessions` crashes if any session has malformed `start_fr` (same storage path). Fix same place.
5. [P2, 8/10] Test fixtures need explicit DB isolation — use `tmp_path` + monkeypatch `storage.DB_PATH` or mock `SessionStore`.
6. [P3, 7/10] Full FR data exposed via sequential IDs — acceptable for LAN-only device, but should be documented.

```
ENG DUAL VOICES — CONSENSUS TABLE:
═══════════════════════════════════════════════════════════════
  Dimension                           Claude  Codex  Consensus
  ──────────────────────────────────── ─────── ─────── ─────────
  1. Architecture sound?               Yes*    N/A    N/A (single model)
  2. Test coverage sufficient?         No      N/A    N/A
  3. Performance risks addressed?      Yes     N/A    N/A
  4. Security threats covered?         Partial N/A    N/A
  5. Error paths handled?              No      N/A    N/A
  6. Deployment risk manageable?       Yes     N/A    N/A
═══════════════════════════════════════════════════════════════
*3 P1 silent failure paths identified; architecture is sound once these are resolved.
```

## Section 1: Architecture

```
Browser
  │
  ├── GET /api/sessions/{session_id}
  │        │
  │        ▼
  │   FastAPI endpoint (web.py)
  │        │
  │        ├── SessionStore.get_session(id)      [storage.py:115]
  │        │        │
  │        │        ├── None? → 404
  │        │        │
  │        │        └── _row_to_session(row)     [storage.py:159]
  │        │                 │
  │        │                 ├── start_fr: FrequencyResponse.from_json()  [NEEDS try/except]
  │        │                 └── end_fr: Optional[FrequencyResponse]      [already guarded]
  │        │
  │        └── Returns {id, label, timestamp, start_fr: {frequencies, spl}, end_fr: ...}
  │
  └── JS: loadSession(id) → fetch → renderFR(start, end) → Chart.js

History table interaction:
  loadHistory() → <tr onclick="loadSession(id)"> → .selected CSS highlight
```

**[P1, 9/10] `web.py` (new endpoint) — Field name mismatch.** The plan's API spec uses `frequencies_hz`/`spl_dbfs`, but `FrequencyResponse.frequencies` and `FrequencyResponse.spl` are the actual field names. Auto-decision: use native names `frequencies` / `spl` in the response. Simpler, no translation layer. Update Step 1 spec and all JS references. (P5 — explicit over clever)

**[P1, 10/10] `storage.py:159` — `from_json()` uncaught on corruption.** Auto-decision: add `try/except (json.JSONDecodeError, TypeError, KeyError)` in `_row_to_session()` — return a sentinel `FrequencyResponse` with zero frequencies and a warning if parsing fails. This also fixes the pre-existing `GET /api/sessions` crash path. In blast radius, same fix. (P2 — boil lakes)

## Section 2: Code Quality

**[P1, 9/10] `web.py:381` — `renderFR()` null guard placement.** Auto-decision: null guard must be the first line of the function body, before `document.getElementById('plotCard').style.display = ''` and before `new Chart(ctx, ...)`. Show error message in `plotCard` instead of chart. (P5 — explicit over clever)

**[P2, 10/10] Pre-existing: `web.py:611-613` — `list_sessions` crash on malformed FR.** Access to `s.start_fr.peak_spl`, `s.start_fr.freq_at_peak`, `len(s.start_fr.frequencies)` all crash if `_row_to_session` returns a session with corrupt FR. Fixed by the `_row_to_session` try/except above. Note: this means the sentinel `FrequencyResponse` with zero frequencies needs safe `peak_spl` / `freq_at_peak` properties — check: `peak_spl` calls `max(self.spl)` → raises `ValueError` on empty list. Auto-decision: also add guard to `peak_spl` and `freq_at_peak` properties. (P1)

**[P3, confidence 8/10] `web.py` — `_HTML` string at 340+ LOC.** Tech debt, not blocking. Not in scope.

**No DRY violations.** The new endpoint follows the exact same pattern as `list_sessions`. Consistent.

## Section 3: Test Review

**Test framework:** pytest (per CLAUDE.md — `uv run python -m pytest tests/ -v`)

**Test diagram — every new codepath and edge case:**

```
New endpoint: GET /api/sessions/{session_id}
├── Happy path: session exists, start_fr only
│     └── test_get_session_detail_happy_path
├── Happy path: session has both start_fr and end_fr
│     └── test_get_session_detail_with_end_fr
├── Not found: invalid session_id
│     └── test_get_session_detail_404
├── Field names: response uses 'frequencies' and 'spl' (not 'frequencies_hz')
│     └── test_get_session_detail_field_names  ← critical, prevents silent JS blank
├── Malformed start_fr in DB (corruption)
│     └── test_get_session_detail_malformed_fr  ← needs direct DB write to bypass NOT NULL
│
Storage layer fix: _row_to_session() with corrupt start_fr
├── JSONDecodeError → sentinel FR returned
│     └── test_row_to_session_malformed_json
├── Sentinel FR: peak_spl + freq_at_peak safe on empty spl list
│     └── test_fr_peak_spl_empty_list
│
Pre-existing fix: GET /api/sessions with one corrupt session
├── Corrupt session → skipped or sentinel, others returned normally
│     └── test_list_sessions_tolerates_corrupt_fr
```

**Test isolation requirement:** All new tests that touch `SessionStore` must use `tmp_path` + monkeypatch:
```python
@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("calibrate.storage.DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr("calibrate.web.SessionStore", lambda: SessionStore(tmp_path / "test.db"))
    return SessionStore(tmp_path / "test.db")
```
(Or mock `SessionStore` as existing tests likely do — check `test_web.py` for the pattern.)

**Missing from original plan:**
- `test_get_session_detail_field_names` — catches P1 field naming bug
- `test_get_session_detail_malformed_fr` — catches corruption path
- `test_row_to_session_malformed_json` — unit test for storage fix
- `test_fr_peak_spl_empty_list` — unit test for sentinel properties
- `test_list_sessions_tolerates_corrupt_fr` — integration test for pre-existing fix

**Auto-decision:** Add all 5 missing tests to Step 5. (P1 — completeness)

## Section 4: Performance

FR data size: ~200 freq points × 2 float arrays = ~400 values. JSON-serialized ≈ 3-4KB per session. Single SQLite SELECT by primary key. Sub-millisecond on Pi Zero 2 W. No caching needed. No N+1 query.

`list_sessions` fetches all sessions for the history table — no change to this. Fine at typical history sizes (<100 sessions).

## Mandatory Eng Outputs

### NOT in scope (Eng phase)
- Paginating `GET /api/sessions` (not needed at typical history sizes)
- Extracting `_HTML` string into a template file (tech debt, separate PR)
- Session deletion endpoint

### What already exists (confirmed by code read)
- `SessionStore.get_session()` — `storage.py:115` — confirmed returns `Session` with `start_fr` + `end_fr`
- `renderFR()` — `web.py:381-421` — confirmed Chart.js, already destroys prior chart
- `FrequencyResponse.from_json()` backward compat — `measurement.py:63` — confirmed `.setdefault("warnings", [])`
- `if (frChart) frChart.destroy()` — `web.py:386` — confirmed canvas reuse handled

### Failure Modes Registry (complete)

| Mode | Probability | Impact | Mitigation |
|---|---|---|---|
| Field name mismatch in API response | High (obvious implementation mistake) | Blank chart, no error | Explicitly name fields in endpoint; test field names |
| Malformed FR JSON in DB | Very low | 500 on new endpoint, crash on list_sessions | try/except in `_row_to_session()` |
| null guard fires inside Chart config | High (implementation mistake) | JS TypeError | Guard before `new Chart()` |
| Test hits real home-dir DB | Medium (CI) | Flaky tests or false passes | tmp_path + monkeypatch DB_PATH |
| `peak_spl` on empty spl list | Low (sentinel FR only) | ValueError in list_sessions | Guard in `peak_spl` / `freq_at_peak` |

## Updated Final Plan (post Eng review)

### Step 1: Add `GET /api/sessions/{session_id}` endpoint (web.py)
```python
@app.get("/api/sessions/{session_id}")
async def get_session_detail(session_id: int) -> dict:
    store = SessionStore()
    session = store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session #{session_id} not found")
    logger.info("session %d fetched", session_id)
    def _fr_dict(fr):
        if fr is None: return None
        return {"frequencies": fr.frequencies, "spl": fr.spl}
    return {
        "id": session.id,
        "label": session.label,
        "timestamp": session.timestamp,
        "start_fr": _fr_dict(session.start_fr),
        "end_fr": _fr_dict(session.end_fr),
    }
```
Note: uses native field names `frequencies`/`spl` — no renaming. All JS uses same names.

### Step 2: Fix `_row_to_session()` in storage.py (defensive JSON parsing)
Wrap `FrequencyResponse.from_json()` calls in try/except. Return a sentinel on failure.
Also guard `FrequencyResponse.peak_spl` and `freq_at_peak` for empty lists.

### Step 3: Make history rows clickable (JS)
- `onclick="loadSession(${s.id})"` on each `<tr>`
- `.selected` CSS class: `tr.selected td { background: #1e2535; border-left: 2px solid #3b82f6; }`
- `loadSession(id)` fetches `/api/sessions/${id}`, calls `renderFR()`

### Step 4: Update `renderFR()` (null guard first, then overlay)
```javascript
function renderFR(startFreqs, startSpl, endFreqs=null, endSpl=null, label='') {
  const plotCard = document.getElementById('plotCard');
  if (!startFreqs || startFreqs.length === 0) {
    plotCard.style.display = '';
    // show error message
    return;
  }
  plotCard.style.display = '';
  // ... Chart.js setup with optional second dataset
}
```
Update existing call site: `renderFR(result.frequencies, result.spl)`.

### Step 5: PNG export button
`<button onclick="exportChart()">Export PNG</button>` — shown when `frChart` is non-null.
`canvas.toDataURL('image/png')` → `<a download="fr.png">` click. ~10 LOC.

### Step 6: Tests (tests/test_web.py)
All tests use `tmp_path` + monkeypatch for DB isolation.
- `test_get_session_detail_happy_path` — start_fr only
- `test_get_session_detail_with_end_fr` — both datasets
- `test_get_session_detail_404` — missing session
- `test_get_session_detail_field_names` — verify `frequencies`/`spl` keys (not `frequencies_hz`)
- `test_get_session_detail_malformed_fr` — direct DB write of invalid JSON
- `test_row_to_session_malformed_json` — unit test for storage fix
- `test_fr_peak_spl_empty_list` — sentinel properties
- `test_list_sessions_tolerates_corrupt_fr` — pre-existing fix integration test

## Eng Completion Summary

| Item | Status |
|---|---|
| Scope challenge | Done — 2 files, 0 new classes |
| Architecture ASCII diagram | Done |
| Test diagram mapping codepaths | Done |
| "NOT in scope" | Done |
| "What already exists" | Done |
| Failure modes registry | Done |
| Critical gaps | 3 P1 silent failures identified and fixed |
| Dual voices | [subagent-only] — Codex unavailable |

## Decision Audit Trail (continued)

| # | Phase | Decision | Classification | Principle | Rationale | Rejected |
|---|-------|----------|----------------|-----------|-----------|---------|
| 8 | Eng | Use native field names `frequencies`/`spl` in API (not `frequencies_hz`) | Mechanical | P5 (explicit) | Avoids translation layer; no renaming bugs; JS uses same names as Python | Use `frequencies_hz` |
| 9 | Eng | Add try/except in `_row_to_session()` for corrupt FR | Mechanical | P1+P2 | Fixes pre-existing crash in list_sessions; same fix covers new endpoint | Defer |
| 10 | Eng | null guard before `new Chart()` (not inside config) | Mechanical | P5 (explicit) | Inside-config guard causes TypeError on null; before-Chart guard is safe | Inside config |
| 11 | Eng | Fix pre-existing `list_sessions` crash | Mechanical | P2 (boil lakes) | In blast radius (storage.py `_row_to_session`), same 3-line fix | Defer |
| 12 | Eng | Add `peak_spl`/`freq_at_peak` empty-list guard | Mechanical | P1 | Sentinel FR with empty spl raises ValueError in existing list_sessions | Defer |
| 13 | Eng | Require tmp_path + monkeypatch for test DB isolation | Mechanical | P1 | CI will hit real home-dir DB otherwise | Use mocks only |
| 14 | Eng | Add 5 missing tests to test plan | Mechanical | P1 (completeness) | Field name test is the most critical — prevents silent blank chart | Defer |

---

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 1 | issues_open | 2 TASTE decisions, 1 critical premise corrected (null FR) |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | unavailable | `codex` binary not found |
| Eng Review | `/plan-eng-review` | Architecture & tests | 1 | issues_open | 3 P1 silent failures identified + 3 lower severity; all auto-fixed in plan |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | skipped | UI detection heuristic missed (plain HTML/JS, no framework terms) |

**VERDICT:** REVIEWED via `/autoplan` — 14 auto-decisions made, 2 TASTE decisions surfaced at gate. All P1 issues resolved in plan. Ready to implement.
