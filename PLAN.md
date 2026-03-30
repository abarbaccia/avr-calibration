<!-- /autoplan restore point: /home/andrew/.gstack/projects/abarbaccia-avr-calibration/feat-equipment-verification-workflow-autoplan-restore-20260330-163015.md -->
# Plan: Equipment Verification Workflow

**Branch:** feat/equipment-verification-workflow
**Feature:** Replace the flat single-page UI with a multi-step guided workflow. Phase 1 is a placeholder room setup form. Phase 2 is a working equipment verification step (test tone, miniDSP ping, Denon ping, signal path). Phases 3-5 are wired as locked/disabled steps.

## Why Now

QA and tests are blocked — the current app can't play a test tone and there's no structured path to verify the hardware signal chain before starting calibration. The flat single-page UI makes it impossible to tell which step failed. This workflow makes setup debuggable.

## Feature Brief

**In scope:**
- Workflow shell: multi-step navigator in the web UI (phases visible, progress tracked, current phase highlighted)
- Phase 1 — Room Setup: placeholder UI (form stub: speaker name/count, room dimensions, notes field — no AI parsing)
- Phase 2 — Equipment Verification: test tone playback (Web Audio API, browser-side), miniDSP connectivity check (ping minidspd), Denon AVR connectivity check, signal path validation — all with clear per-component pass/fail badges and debug error text
- Remaining phases (Baseline Measurement, Calibration Loop, Subjective Feedback) rendered as disabled/locked steps in the navigator
- New backend API endpoints to expose the existing `PreflightChecker` checks over HTTP (already implemented in preflight.py, not yet wired to the web UI)

**Out of scope:**
- AI parsing of free-text room description (next PR)
- Changes to calibration algorithm
- Subjective feedback collection
- Visual room diagram

**Done when:**
- User lands on app, sees a step-by-step workflow navigator
- Can fill in the room setup placeholder and proceed to Phase 2
- Equipment verification plays a test tone, pings miniDSP, pings Denon, shows clear pass/fail per component
- Failures show debug output (not silent hangs)
- Passing all checks unlocks Phase 3 (disabled for now, just visually unlocked)

## What Already Exists

| Sub-problem | Existing code |
|---|---|
| Mic check | `PreflightChecker.check_mic()` in preflight.py:94 |
| miniDSP USB check | `PreflightChecker.check_hidraw()` in preflight.py:67 |
| miniDSP HTTP ping | `PreflightChecker.check_minidsp()` in preflight.py:136 |
| Denon AVR ping | `PreflightChecker.check_denon()` in preflight.py:189 |
| Playback route check | `PreflightChecker.check_playback_route()` in preflight.py:218 |
| Signal path check | `PreflightChecker.check_signal_path_sync()` in preflight.py:283 |
| Web server | FastAPI app in web.py |
| CSS theme | Dark teal theme in web.py:140–280 |
| CLI preflight | `calibrate check` command in cli.py:49 |

## Files to Change

- `calibrate/web.py` — Add workflow navigator HTML/CSS/JS, room setup placeholder UI, equipment verification UI, new `/api/preflight/*` endpoints wiring to `PreflightChecker`
- `tests/test_web.py` — Tests for new preflight API endpoints
- `tests/test_preflight.py` — Already has tests; verify coverage of new HTTP-exposed paths

## Architecture

```
Browser (workflow navigator)
  Phase 1: Room Setup form (no API call on submit — local state only)
  Phase 2: Equipment Verification
    [Test Tone]     → Web Audio API oscillator (browser-only, no backend)
    [miniDSP USB]   → GET /api/preflight/hidraw
    [miniDSP HTTP]  → GET /api/preflight/minidsp
    [Denon AVR]     → GET /api/preflight/denon
    [Playback Route]→ GET /api/preflight/playback
    [Signal Path]   → GET /api/preflight/signal-path
  Phase 3-5: Locked (placeholder cards, no functionality)
```

## UI Layout (new)

```
[ Step Navigator ]
  1. Room Setup  ●  (current)
  2. Verify Equipment
  3. Baseline Measurement  (locked)
  4. Calibration Loop  (locked)
  5. Feedback  (locked)

[ Active Phase Card ]
  — Room Setup: speaker count, name, notes, "Continue →"
  — Equipment Verification: per-component check rows with Run All / individual retry
```

## CEO Review (Phase 1 — /autoplan 2026-03-30)

### 0A. Premise Challenge

**Is this the right problem?** Yes, unambiguously.
- `PreflightChecker` (preflight.py) has 6 fully-implemented checks. None are exposed via HTTP. The CLI has them; the web UI doesn't.
- No test tone exists anywhere in the codebase.
- The flat single-page UI makes it impossible to know which setup step failed.
- TODOS.md shows QA is blocked (TODO-R1: sweep delivery blocked since 2026-03-30).

**What if we did nothing?** The calibration loop remains unreachable. Every new feature adds surface area to a system you can't verify.

**Is the plan the most direct path?** Yes. This is almost entirely wiring existing code to HTTP, plus building the workflow shell.

### 0B. Existing Code Leverage

| Sub-problem | Existing code |
|---|---|
| All 6 hardware checks | `PreflightChecker.run_all()` — complete, just needs HTTP routes |
| Mic check | `check_mic()` — sounddevice enumeration, fully tested |
| miniDSP USB | `check_hidraw()` — `/dev/hidraw0` existence, fully tested |
| miniDSP HTTP | `check_minidsp()` — httpx GET, fully tested |
| Denon AVR | `check_denon()` — denonavr, fully tested |
| Playback route | `check_playback_route()` — USB+HDMI, fully tested |
| Signal path | `check_signal_path_sync()` — config vs. device state |
| Web server | FastAPI app in web.py |
| CSS theme | Dark theme at web.py:140-280 |
| Test client pattern | `TestClient(app)` in test_web.py:57 |

Zero new check logic needed. This is pure: wire → HTTP + workflow UI shell.

### 0C. Dream State Mapping

```
CURRENT STATE              THIS PLAN                  12-MONTH IDEAL
─────────────────          ──────────────────         ──────────────────────
Flat single page           Multi-step wizard          Full guided workflow
All features visible       Phases 1-5 nav             + AI room analysis
No setup guidance          Phase 1: room stub          + Digital twin from text
Test tone broken           Phase 2: HW verify          + Smart baseline detect
Preflight in CLI only      Preflight via HTTP          + Auto-suggest EQ moves
QA blocked                 QA unblocked                + Closed loop automation
```

This plan is the right first step toward the 12-month ideal. It plants the workflow architecture.

### 0C-bis. Implementation Alternatives

```
APPROACH A: Minimal — Wire preflight to HTTP, add Run Checks button to existing flat UI
  Summary: Expose /api/preflight endpoints + add a check card to the current flat page.
  Effort: S
  Risk:  Low
  Pros:  Tiny diff, zero UI refactor, QA unblocked immediately
  Cons:  Still feels like REW; doesn't plant workflow architecture; defers UX refactor
  Reuses: preflight.py as-is, +5 routes, 1 UI card

APPROACH B: Full — Workflow shell + preflight wiring (the plan) ← RECOMMENDED
  Summary: Multi-step navigator replaces flat UI; Phase 1 placeholder; Phase 2 wired;
           Phases 3-5 locked.
  Effort: M
  Risk:  Medium (web.py is 1917-line monolith — adding nav state is non-trivial)
  Pros:  Plants right architecture; QA unblocked; UX transformed; phases 3-5 scaffolded
  Cons:  More test surface area; workflow state lives in JS
  Reuses: preflight.py, existing CSS, existing card pattern

APPROACH C: Full with server-side wizard state
  Summary: Approach B + current phase stored server-side (SQLite or in-memory)
  Effort: L
  Risk:  High
  Pros:  Workflow state survives page refresh server-side; enables server-driven gating
  Cons:  New concept + DB overhead for a 1-user device; over-engineered; ocean not lake
  Reuses: storage.py (but adds new concept)

RECOMMENDATION: Approach B — the workflow architecture IS the stated goal. Approach A defers
the UX problem. Approach C is an ocean.
```

### 0D. Cherry-Picks (SELECTIVE EXPANSION — auto-decided)

| # | Expansion | Effort | Decision | Principle |
|---|-----------|--------|----------|-----------|
| 1 | Test tone with frequency selector (20/40/60/80/100 Hz) | S | ACCEPTED | P1 completeness, direct debug value |
| 2 | Config validation check in Phase 2 (verify denon.host, etc.) | S | ACCEPTED | P1, surfaces config gaps before measurement |
| 3 | Phase progress persistence via localStorage | S | ACCEPTED | P5 explicit, trivial + good UX |
| 4 | Debug log download on failure (text file with all check results) | S | ACCEPTED | P1, "no silent failures" goal |

### 0E. Temporal Interrogation

```
HOUR 1 (workflow nav JS):
  - How is current phase tracked? → localStorage for persistence, module var for runtime.
  - Locked = un-clickable (no modal, just visual indicator).
  - Existing JS uses module-level vars (stream, audioCtx) — follow same pattern.

HOUR 2-3 (preflight HTTP endpoints):
  - /api/preflight/run-all returns all results in parallel (asyncio.gather — already in
    PreflightChecker.run_all()). Never fail-fast.
  - Per-check retry endpoints needed for "retry" UX: GET /api/preflight/{check-name}.
  - check_mic() has lazy sounddevice import. On Pi Zero (no GUI audio daemon), sounddevice
    may fail. Endpoint must surface the error clearly, not 500.

HOUR 4-5 (integration):
  - check_signal_path_sync() calls check_minidsp() and check_denon() internally — don't
    call them twice. Run-all calls check_signal_path_sync() and the others separately —
    verify no double-call in the PreflightChecker.run_all() flow.
  - Web Audio API OscillatorNode requires a user gesture (autoplay policy). The "Play Tone"
    button IS the gesture — AudioContext must be created inside the click handler.
  - test_preflight.py already covers all 6 underlying checks. New test_web.py tests are
    just for the HTTP wrapper layer (~9 tests), not re-testing check logic.

HOUR 6+ (polish/tests):
  - conftest.py already mocks sounddevice + pytta — preflight endpoint tests can leverage
    existing mock pattern.
  - Check that PreflightChecker import doesn't break existing web.py import (add to
    imports at top of web.py).
```

### Architecture Diagram

```
Browser (workflow navigator)
  ┌──────────────────────────────────────────────┐
  │  [Step 1: Room Setup]  ●  [Step 2: Verify]   │
  │  [Step 3: Baseline ⊘]  [Step 4: Loop ⊘]      │
  │  [Step 5: Feedback ⊘]                         │
  └──────────────────────────────────────────────┘
           │ active phase
           ▼
  Phase 2: Equipment Verification Card
    [Test Tone ♫] → Web Audio API OscillatorNode (browser-only, no backend)
    [miniDSP USB] → GET /api/preflight/hidraw      → PreflightChecker.check_hidraw()
    [miniDSP HTTP]→ GET /api/preflight/minidsp     → PreflightChecker.check_minidsp()
    [Denon AVR]   → GET /api/preflight/denon       → PreflightChecker.check_denon()
    [Playback]    → GET /api/preflight/playback    → PreflightChecker.check_playback_route()
    [Signal Path] → GET /api/preflight/signal-path → PreflightChecker.check_signal_path_sync()
    [Run All]     → GET /api/preflight             → PreflightChecker.run_all()
    [Config Check]→ GET /api/preflight/config      → validate required config keys
    [Download Log]→ client-side JS (formats results as text, triggers download)

  Phase 1: Room Setup (placeholder)
    Speaker count: [input]
    Speaker names: [input]
    Room notes:    [textarea]
    [Continue →] → sets phase=2 in localStorage, advances navigator

  Phases 3-5: Disabled/locked in nav (no API calls)
```

### Error & Rescue Map

```
ENDPOINT / METHOD           | WHAT CAN GO WRONG           | EXCEPTION CLASS
────────────────────────────|────────────────────────────|─────────────────────
GET /api/preflight          | sounddevice not available   | ImportError (lazy)
  (run_all)                 | minidspd timeout            | httpx.TimeoutException
                            | denonavr unreachable        | Exception (caught)
                            | asyncio.gather exception    | BaseException (caught)
────────────────────────────|────────────────────────────|─────────────────────
GET /api/preflight/{check}  | Invalid check name          | 404 (not implemented)
  (per-check retry)         | sounddevice not available   | ImportError (lazy)
                            | minidspd ConnectError       | httpx.ConnectError
────────────────────────────|────────────────────────────|─────────────────────

EXCEPTION CLASS             | RESCUED? | RESCUE ACTION          | USER SEES
────────────────────────────|──────────|─────────────────────── |──────────────────
ImportError (sounddevice)   | Y        | Return failed result   | Check row: FAIL + error text
httpx.TimeoutException      | Y        | Return failed result   | Check row: FAIL + "timeout"
httpx.ConnectError          | Y        | Return failed result   | Check row: FAIL + daemon hint
denonavr Exception          | Y        | Return failed result   | Check row: FAIL + host hint
BaseException (run_all)     | Y (existing) | captured in run_all | Check row: FAIL
Invalid /api/preflight/{x}  | N ← ADD  | Return 404             | "Unknown check name"
```

Gap: Per-check endpoint with an invalid check name (e.g. `/api/preflight/foobar`) should return 404, not 500. Add validation.

### Security & Threat Model

- New endpoints: 7 GET routes, all read-only (no mutations). No auth needed for a 1-user device.
- No user input to these endpoints (no path injection risk for simple named routes).
- Config fields (denon.host, minidsp.host) are read from server config, not request params. No injection.
- Test tone is entirely browser-side (Web Audio API) — zero server risk.
- Attack surface: no expansion beyond existing endpoints. Same trust model.

### Data Flow: Phase 2 Check Row

```
[Run All click]
  → audioCtx.createOscillator() [browser-only, no backend]
  → GET /api/preflight [server]
      → PreflightChecker.run_all()
          → asyncio.gather([check_mic, check_hidraw, check_minidsp,
                            check_denon, check_playback, check_signal_path])
      → list[CheckResult] → JSON response
  → browser renders per-check pass/fail badges

Shadow paths:
  nil: N/A — GET has no body
  empty: run_all() always returns 6 results (never empty list)
  error: each check catches exceptions → failed CheckResult (never propagates to HTTP layer)
  timeout: minidspd timeout → failed CheckResult with "wait and retry" hint
```

### Interaction Edge Cases

| Interaction | Edge Case | Handled? | How |
|---|---|---|---|
| Test tone button | Double-click | ✓ | Button disabled while oscillator playing |
| Test tone button | AudioContext already closed | ✓ | Create new context each click |
| Run All button | Double-click | ✓ | Disable button while in-flight |
| Run All button | Navigate away mid-check | ✓ | Response ignored on unmount (no cleanup needed — no sub state) |
| Per-check retry | While run-all in flight | ADD: disable retry buttons during run-all |
| Phase advance | Phase 2 not complete | ADD: "Continue" button disabled until all checks pass |
| Page refresh | Phase restored from localStorage | ✓ | Phase persistence in cherry-pick |

Gap: Retry buttons should be disabled during Run All in-flight. "Continue to Phase 3" button should be disabled until all checks pass (even if locked, be explicit).

### Performance

- GET /api/preflight runs 6 checks via asyncio.gather — all parallel. On Pi Zero 2 W (1GHz), network checks dominate: minidspd (localhost) ~5ms, denonavr (LAN) ~100-500ms. Total: ~500ms worst case. Acceptable.
- Web Audio API oscillator: negligible CPU.
- No DB queries in the preflight path.
- No caching needed — these are live hardware checks meant to reflect current state.

### Observability

- `calibrate check` (CLI) already produces preflight output. The new HTTP path doesn't add structured logging yet. Add: one log line per check result to the Pi's Docker log (for post-session debugging).
- No new dashboards needed (1-user device, not a multi-tenant service).
- Debug log download (cherry-pick #4) is the runbook for the user.

### Deployment

- No DB migrations (no new tables or columns).
- No Docker changes (adding routes to existing app, same image).
- No config changes required — new endpoints use existing Config fields.
- Rollback: git revert of web.py changes. Instant.
- Risk: zero. These are new read-only endpoints that don't touch the calibration loop.

### CEO Completion Summary

| Section | Finding | Severity | Auto-decided |
|---|---|---|---|
| Premises | Valid, unambiguous | — | — |
| Approach | Approach B (full workflow) | — | P1+P2 |
| Cherry-picks | 4 expansions approved | — | P1+P2 |
| Error handling | 1 gap: /api/preflight/{invalid} → 404 | Medium | Add 404 validation |
| Edge cases | 2 gaps: retry disable during run-all; Continue gating | Low | Add both |
| Security | No issues — read-only endpoints, 1-user device | None | — |
| Performance | ~500ms for run_all — acceptable | None | — |
| Deployment | Zero risk — no migrations, no Docker changes | None | — |

## Design Review (Phase 2 — /autoplan 2026-03-30)

**Rating: 5/10 → target 8/10 after this section**

### Step Navigator States

```
DESKTOP (≥480px):
  ┌─────────────────────────────────────────────────────┐
  │  ●─────●─────○─────○─────○                          │
  │  1     2     3     4     5                           │
  │ Room  Verify Baseline Loop Feedback                  │
  │ Setup  [●]   [⊘]   [⊘]   [⊘]                       │
  └─────────────────────────────────────────────────────┘

  Step states:
  - complete: green filled circle (#22c55e) + checkmark icon
  - active:   teal filled circle (#2dd4bf) + step number, bold label
  - locked:   gray circle (#475569) + step number, muted label (#64748b)
  - connector: 1px line (#2d3748), green between complete steps

MOBILE (<480px):
  ┌─────────────────────────────┐
  │  Step 2 of 5: Verify Equip  │
  │  ← Back  ──────────  2/5 ●  │
  └─────────────────────────────┘
  Show only active step name + progress. Full navigator hidden.
```

### Check Row States (per component)

```
PENDING (initial load):
  ○ Component Name          [—  PENDING] ─────────────────

RUNNING (while HTTP in-flight):
  ⟳ Component Name          [CHECKING...] ──────────────

PASS:
  ✓ Component Name          [PASS ●] detail text here
                                                    [retry]

FAIL:
  ✗ Component Name          [FAIL ●] error message here
                                                    [Retry]

SKIPPED (dependency not met):
  ─ Component Name          [SKIPPED] requires miniDSP USB

N/A (not configured):
  ─ Signal Path             [N/A] not configured

Colors:
  PASS badge: badge-optimal style (#22c55e)
  FAIL badge: badge-danger style (#ef4444)
  CHECKING: badge-empty style (#64748b) with spinner
  SKIPPED: badge-empty style, italic text
  N/A: badge-empty style
```

### AudioContext State Handling

```javascript
// IMPORTANT: browsers suspend AudioContext when tab loses focus.
// Always resume before starting oscillator to avoid silent playback.
async function playTestTone(freq) {
    if (!audioCtx) {
        audioCtx = new AudioContext({ sampleRate: 48000 });
    }
    await audioCtx.resume();  // ← required: context may be suspended
    oscillator = audioCtx.createOscillator();
    oscillator.type = 'sine';
    oscillator.frequency.value = freq;
    oscillator.connect(audioCtx.destination);
    oscillator.start();
}
```

### Test Tone Row (special — no HTTP, self-reported)

```
IDLE:
  ♪ Test Tone    [NOT TESTED]
  [20Hz] [40Hz] [60Hz] [80Hz] [100Hz]  ← segmented buttons
  [▶ Play Tone]

PLAYING:
  ♪ Test Tone    [PLAYING ...]
  [20Hz] [40Hz] [60Hz] [80Hz] [100Hz]
  [■ Stop]  "Can you hear the tone? Click ✓ if yes."
  [✓ Confirm]

CONFIRMED:
  ♪ Test Tone    [PASS ●]  User confirmed tone audible at 60 Hz

Note: Test tone is self-reported. The browser plays a sine wave via
Web Audio API OscillatorNode. There is no backend check. The user
clicks Confirm after hearing the tone. This is explicit in the plan.
```

### Duplicate Denon Connection Deduplication

`run_all()` calls `check_denon()` AND `check_playback_route()` in parallel. When `playback_route=hdmi`, `check_playback_route()` also creates a `DenonAVR` and calls `async_setup()` — two simultaneous connections to the same device.

**Fix:** Modify `PreflightChecker.check_playback_route()` to detect `route=hdmi` and call `self.check_denon()` internally, returning its result with `name="Playback Route"`. No duplicate connection.

```python
async def check_playback_route(self) -> CheckResult:
    route = self.config.measurement.get("playback_route", "usb")
    if route == "hdmi":
        denon_result = await self.check_denon()
        # Reuse denon result, just rename it
        return CheckResult(
            name="Playback Route",
            passed=denon_result.passed,
            detail=f"HDMI via {denon_result.detail}" if denon_result.passed else denon_result.detail,
            error=denon_result.error,
        )
    # USB path unchanged...
```

### Dependency Chain (check ordering)

```
miniDSP USB ──▶ miniDSP HTTP ──▶ Signal Path
                               └─▶ Config (partial dependency)
Denon AVR   ──▶ Playback Route (HDMI)
Microphone  ──▶ (independent)
Test Tone   ──▶ (independent, browser-only)

If miniDSP USB fails: miniDSP HTTP, Signal Path → SKIPPED
If Denon AVR fails AND playback_route=hdmi: Playback Route → SKIPPED
Config check runs independently but surfaces config field errors.
```

### "Continue to Phase 3" Button Gating

```
All 7 backend checks PASS + Test Tone CONFIRMED:
  ↓ animate in (fade + slide up, 200ms)
  [  Continue to Baseline Measurement →  ]   ← full-width teal #2dd4bf

Partial pass / any fail:
  Button NOT rendered (not disabled — absent entirely).

All PASS but test tone not confirmed:
  [  Continue to Baseline Measurement →  ]
  "Tip: confirm test tone before proceeding (optional)"
  ← shown but not blocked (test tone is advisory only)
```

### Failure Summary State

```
If all (or many) checks fail:
  ┌─── Equipment Verification ─────────────────────────────┐
  │  0 of 7 checks passed                                   │
  │  [Download Debug Log ↓]    [Run All Again ↺]            │
  │  ─────────────────────────────────────────────          │
  │  [check rows...]                                         │
  └─────────────────────────────────────────────────────────┘
  Show count at top of card. Download Log is PRIMARY CTA when failing.
```

### Accessibility Specs

- Step navigator: active step has `aria-current="step"`
- Pass/fail badges: `aria-label="pass"` / `aria-label="fail"` (color not sole indicator)
- Frequency segmented buttons: `role="radiogroup"`, each button `role="radio"` + `aria-checked`
- Retry buttons: `aria-label="Retry miniDSP USB check"`
- Spinner: `aria-label="checking"` + `role="status"`

### Config Validation Check Details

```
GET /api/preflight/config checks for:
  - denon.host          → required
  - minidsp.host        → required (default localhost OK)
  - minidsp.port        → required (default 5380 OK)
  - measurement.playback_route → required
  - mic.name            → required

PASS: "5 of 5 required fields present"
FAIL: "Missing required fields: denon.host, measurement.playback_route"
```

## Eng Review (Phase 3 — /autoplan 2026-03-30)

### Step 0: Scope Challenge

**Sub-problem → existing code map:**

| Sub-problem | Existing code | Verdict |
|---|---|---|
| Hardware checks (all 6) | `PreflightChecker.run_all()` + individual methods | Wire, don't rebuild |
| HTTP server | FastAPI in web.py | Add routes |
| Config loading | `_load_config()` in web.py:1911 | Reuse exact pattern |
| Test CSS/badge system | web.py:212-218 | Reuse badge classes |
| Test client for tests | `TestClient(app)` in test_web.py:57 | Reuse fixture |
| Config fields validation | NOT YET in PreflightChecker | ADD `check_config()` to PreflightChecker |

**Minimum set for stated goal:** Add `PreflightChecker.check_config()` + 7 HTTP routes + workflow HTML/JS. That's 3 files (preflight.py, web.py, test files).

**Complexity check:** 3 files, 1 new method on existing class, 7-8 new routes. Well under 8 files. Clean.

**TODOS cross-reference:** TODO-R1 (sweep delivery) is NOT blocking this PR. This PR is about setup verification, not sweep delivery. No new TODOs created by this plan.

**Key architectural decision:** Per-check endpoint approach.

```
Option A: Individual endpoints per check (7 routes)
  GET /api/preflight/hidraw
  GET /api/preflight/minidsp
  GET /api/preflight/denon
  GET /api/preflight/playback
  GET /api/preflight/signal-path
  GET /api/preflight/config
  GET /api/preflight  (run-all)

Option B: Parameterized endpoint
  GET /api/preflight/{check_name}
  GET /api/preflight  (run-all)

DECISION: Option B (parameterized). DRY, extensible, consistent.
Add a CHECK_MAP dict in web.py mapping check name → PreflightChecker method.
Unknown names → 404 (gap identified in CEO review).
```

### Architecture ASCII Diagram

```
web.py (FastAPI)
  │
  ├── GET /          ──▶ index() → HTML (workflow navigator + Phase 1 + Phase 2 UI)
  │
  ├── GET /api/preflight              ──▶ _run_preflight_all()
  │     │                                   → PreflightChecker(cfg).run_all()
  │     │                                   → list[CheckResult] → JSON
  │     │
  ├── GET /api/preflight/{check_name} ──▶ _run_preflight_check(check_name)
  │     │                                   → CHECK_MAP.get(check_name) or 404
  │     │                                   → PreflightChecker(cfg).{method}()
  │     │                                   → CheckResult → JSON
  │     │
  └── (all other existing routes — unchanged)

preflight.py (PreflightChecker)
  ├── run_all()          → asyncio.gather(6 checks)
  ├── check_hidraw()     → os.path.exists /dev/hidraw0
  ├── check_mic()        → sounddevice.query_devices()
  ├── check_minidsp()    → httpx GET minidspd/devices
  ├── check_denon()      → denonavr.DenonAVR().async_setup()
  ├── check_playback_route() → sounddevice or denonavr
  ├── check_signal_path_sync() → MinidspClient.get_device_status()
  └── check_config() [NEW] → validate required Config fields

CHECK_MAP in web.py:
  "hidraw"       → checker.check_hidraw
  "mic"          → checker.check_mic
  "minidsp"      → checker.check_minidsp
  "denon"        → checker.check_denon
  "playback"     → checker.check_playback_route
  "signal-path"  → checker.check_signal_path_sync
  "config"       → checker.check_config
```

### Section 1: Architecture Review

**[P2] (confidence: 9/10) preflight.py — `check_config()` belongs in PreflightChecker, not web.py.**
The config validation check is not fundamentally different from the other checks. Adding it to `PreflightChecker` keeps the HTTP layer uniform and makes the CLI `calibrate check` benefit from it too. Auto-decided: add to `PreflightChecker`.

**[P2] (confidence: 9/10) web.py — CHECK_MAP needs to handle `run_all` differently from per-check.**
`run_all` calls all checks in parallel and returns a list. Per-check returns a single `CheckResult`. The route handler must distinguish these. Use two separate endpoints: `GET /api/preflight` (all) and `GET /api/preflight/{name}` (single). Already reflected in architecture above.

**[P1] (confidence: 9/10) preflight.py — `check_config()` method design.**
Must check:
- `denon.host` → required (default is None — flag if still None)
- `minidsp.host` → present (default 'localhost' is valid — don't flag)
- `minidsp.port` → present (default 5380 is valid)
- `measurement.playback_route` → present (default 'usb' is valid)
- `mic.name` → present (default 'UMIK' is valid)
Only flag if value is None (explicitly unconfigured). Values with non-None defaults are fine.

**[P3] (confidence: 8/10) Rollback: instant (new routes don't touch existing ones).**
No schema migrations, no config format changes, no breaking changes to existing routes.

### Section 2: Code Quality Review

**[P2] (confidence: 9/10) web.py — JSON serialization of CheckResult.**
`CheckResult` is a `@dataclass`. FastAPI can't auto-serialize it without a Pydantic model or `asdict()`. Pattern: `return {"name": r.name, "passed": r.passed, "detail": r.detail, "error": r.error}` inline, or use `dataclasses.asdict(r)`. Use `dataclasses.asdict()` for DRY.

**[P2] (confidence: 9/10) web.py — Config loading pattern.**
Every route calls `cfg = _load_config()`. The preflight routes must follow this exact pattern. Don't create a module-level `PreflightChecker` instance — routes are stateless in the existing pattern.

**[P3] (confidence: 7/10) web.py — Monolith growth.**
web.py is already 1917 lines. Adding ~150-200 lines (8 routes + workflow HTML) pushes it past 2100. Not a blocker for this PR, but flag for extraction in a future PR.

### Section 3: Test Review (COMPLETE DIAGRAM)

**NEW CODEPATHS:**

```
web.py
  ├── GET /api/preflight
  │   ├── config file exists → PreflightChecker.run_all() → list[dict] 200
  │   └── config file missing → HTTPException 503
  │
  └── GET /api/preflight/{check_name}
      ├── valid name → PreflightChecker.{method}() → dict 200
      │   ├── check passes → {"passed": true, ...}
      │   └── check fails → {"passed": false, "error": "..."}
      └── invalid name → 404

preflight.py
  └── check_config() [NEW]
      ├── denon.host is not None → PASS
      ├── denon.host is None → FAIL with "Missing required fields: denon.host"
      └── all required fields valid → PASS "5 of 5 required fields present"
```

**TESTS TO ADD (test_web.py):**

```python
class TestPreflightEndpoints:
    # test_preflight_run_all_all_pass
    #   mock PreflightChecker.run_all → all CheckResult.passed=True
    #   GET /api/preflight → 200, list of 6 passed dicts

    # test_preflight_run_all_partial_fail
    #   mock run_all → 2 pass, 1 fail
    #   GET /api/preflight → 200 (never 4xx), results include failed item

    # test_preflight_run_all_no_config
    #   CONFIG_PATH doesn't exist → GET /api/preflight → 503

    # test_preflight_check_hidraw_pass
    #   mock check_hidraw → CheckResult(passed=True, detail="present")
    #   GET /api/preflight/hidraw → 200, passed=True

    # test_preflight_check_hidraw_fail
    #   mock check_hidraw → CheckResult(passed=False, error="OTG")
    #   GET /api/preflight/hidraw → 200, passed=False, error in response

    # test_preflight_check_minidsp_pass
    # test_preflight_check_minidsp_fail
    # test_preflight_check_denon_pass
    # test_preflight_check_denon_fail
    # test_preflight_check_playback_pass
    # test_preflight_check_signal_path_pass
    # test_preflight_check_config_all_present
    # test_preflight_check_config_missing_denon_host
    # test_preflight_unknown_check_name
    #   GET /api/preflight/foobar → 404

class TestPreflightConfigCheck:
    # test_check_config_all_fields_present
    # test_check_config_denon_host_none → FAIL with field name in error
    # test_check_config_defaults_are_valid → minidsp.host='localhost' → PASS
```

**EXISTING COVERAGE (test_preflight.py):**
All 6 PreflightChecker methods are comprehensively tested already. The new tests in test_web.py only need to test the HTTP layer (route → serialization → response code). Mock PreflightChecker at the web layer, don't re-test the check logic.

**TEST AMBITION:**
- 2am Friday test: "What if minidspd is running but returns an empty device list?" → Already tested by `test_preflight_minidsp_fail`.
- Hostile QA test: "What if /api/preflight/hidraw is called when config.yaml doesn't exist?" → `test_preflight_run_all_no_config` covers this.
- Chaos test: "What if PreflightChecker.run_all() itself raises?" → `run_all()` catches BaseException internally (preflight.py:53-64) → always returns list, never raises.

**FLAKINESS RISK:** None. All checks are mocked. No real hardware, no time dependency, no random values.

### Section 4: Performance

- GET /api/preflight: asyncio.gather on 6 checks. On Pi Zero 2 W, LAN checks (Denon) dominate: ~100-500ms. Acceptable.
- Each per-check endpoint: single check, < 500ms worst case.
- Workflow HTML: added to the existing inline HTML in `index()`. Page size will grow but it's a single-page app — fine.
- No DB queries in the preflight path.

### Eng Completion Summary

| Section | Finding | Severity | Auto-decided |
|---|---|---|---|
| Architecture | Per-check parameterized endpoint + CHECK_MAP | — | P5 explicit |
| Architecture | check_config() belongs in PreflightChecker | P2 | Added |
| Architecture | CheckResult serialization via dataclasses.asdict() | P2 | Added |
| Architecture | Routes follow existing _load_config() pattern | — | Confirmed |
| Code quality | No DRY violations — wiring only | None | — |
| Tests | 15 new test cases identified | — | Added to plan |
| Performance | ~500ms run_all max — acceptable on Pi Zero 2 W | None | — |

## Test Plan (complete)

### New file: `tests/test_preflight.py` additions

```python
class TestPreflightConfigCheck:
    async def test_check_config_all_fields_present(self, config):
        """Config with denon.host set → passes."""
        result = await PreflightChecker(config).check_config()
        assert result.passed
        assert "required fields" in result.detail.lower()

    async def test_check_config_denon_host_none(self):
        """Config with denon.host=None → fails, names the missing field."""
        cfg = Config({"denon": {"host": None}, "minidsp": {}, "mic": {}})
        result = await PreflightChecker(cfg).check_config()
        assert not result.passed
        assert "denon.host" in result.error

    async def test_check_config_defaults_are_valid(self):
        """minidsp.host='localhost' (default) → passes (not flagged as missing)."""
        cfg = Config({"denon": {"host": "192.168.1.100"}, "minidsp": {}, "mic": {}})
        result = await PreflightChecker(cfg).check_config()
        assert result.passed
```

### New tests in `tests/test_web.py`

```python
class TestPreflightEndpoints:
    def test_preflight_run_all_all_pass(self, client, cfg_path):
        """GET /api/preflight → 200, all 6 results in response."""
        with patch("calibrate.web._load_config", return_value=_make_config()), \
             patch("calibrate.preflight.PreflightChecker.run_all",
                   new=AsyncMock(return_value=[
                       CheckResult("Microphone", True, "UMIK-1"),
                       CheckResult("miniDSP USB", True, "/dev/hidraw0 present"),
                       CheckResult("miniDSP", True, "minidspd ok"),
                       CheckResult("Denon AVR", True, "X3800H"),
                       CheckResult("Playback Route", True, "HDMI"),
                       CheckResult("Signal Path", True, "source=Analog"),
                       CheckResult("Config", True, "5 of 5 fields present"),
                   ])):
            r = client.get("/api/preflight")
        assert r.status_code == 200
        results = r.json()
        assert len(results) == 7
        assert all(x["passed"] for x in results)

    def test_preflight_run_all_partial_fail(self, client, cfg_path):
        """GET /api/preflight → 200 even if some checks fail."""
        with patch("calibrate.web._load_config", return_value=_make_config()), \
             patch("calibrate.preflight.PreflightChecker.run_all",
                   new=AsyncMock(return_value=[
                       CheckResult("Microphone", False, "", error="No UMIK found"),
                       CheckResult("miniDSP USB", True, "present"),
                   ])):
            r = client.get("/api/preflight")
        assert r.status_code == 200
        results = r.json()
        assert results[0]["passed"] is False
        assert results[0]["error"] == "No UMIK found"

    def test_preflight_run_all_no_config(self, client, tmp_path):
        """Missing config.yaml → 503."""
        with patch("calibrate.web.CONFIG_PATH", tmp_path / "missing.yaml"):
            r = client.get("/api/preflight")
        assert r.status_code == 503

    def test_preflight_check_hidraw_pass(self, client, cfg_path):
        with patch("calibrate.web._load_config", return_value=_make_config()), \
             patch("calibrate.preflight.PreflightChecker.check_hidraw",
                   new=AsyncMock(return_value=CheckResult("miniDSP USB", True, "present"))):
            r = client.get("/api/preflight/hidraw")
        assert r.status_code == 200
        assert r.json()["passed"] is True

    def test_preflight_check_hidraw_fail(self, client, cfg_path):
        with patch("calibrate.web._load_config", return_value=_make_config()), \
             patch("calibrate.preflight.PreflightChecker.check_hidraw",
                   new=AsyncMock(return_value=CheckResult("miniDSP USB", False, "", error="OTG adapter required"))):
            r = client.get("/api/preflight/hidraw")
        assert r.status_code == 200
        assert r.json()["passed"] is False
        assert "OTG" in r.json()["error"]

    def test_preflight_check_minidsp_pass(self, client, cfg_path): ...
    def test_preflight_check_minidsp_fail(self, client, cfg_path): ...
    def test_preflight_check_denon_pass(self, client, cfg_path): ...
    def test_preflight_check_denon_fail(self, client, cfg_path): ...
    def test_preflight_check_playback_pass(self, client, cfg_path): ...
    def test_preflight_check_signal_path_pass(self, client, cfg_path): ...

    def test_preflight_check_config_all_present(self, client, cfg_path):
        with patch("calibrate.web._load_config", return_value=_make_config()), \
             patch("calibrate.preflight.PreflightChecker.check_config",
                   new=AsyncMock(return_value=CheckResult("Config", True, "5 of 5 fields present"))):
            r = client.get("/api/preflight/config")
        assert r.status_code == 200
        assert r.json()["passed"] is True

    def test_preflight_check_config_missing_field(self, client, cfg_path):
        with patch("calibrate.web._load_config", return_value=_make_config()), \
             patch("calibrate.preflight.PreflightChecker.check_config",
                   new=AsyncMock(return_value=CheckResult("Config", False, "",
                       error="Missing required fields: denon.host"))):
            r = client.get("/api/preflight/config")
        assert r.status_code == 200
        assert r.json()["passed"] is False
        assert "denon.host" in r.json()["error"]

    def test_preflight_unknown_check_name(self, client, cfg_path):
        """GET /api/preflight/foobar → 404."""
        with patch("calibrate.web._load_config", return_value=_make_config()):
            r = client.get("/api/preflight/foobar")
        assert r.status_code == 404
```

### Frontend: Workflow navigator (no backend tests — JS-only)
- Visual state of navigator steps (active, locked, complete): manual QA
- Test tone: manual QA (confirm audible tone at each frequency)
- localStorage persistence: manual QA (refresh page, verify phase restored)
- Dependency chain (miniDSP USB fail → SKIPPED rows): manual QA

## Cross-Phase Themes

**Theme: dependency chain between checks** — flagged in Phase 1 (CEO architecture), Phase 2 (Design: dependency chain section), Phase 3 (Eng: subagent "signal-path skipped when minidsp down"). High-confidence signal — the check dependency ordering must be explicit in both the backend logic (run_all ordering) and the UI (skipped rows).

**Theme: AudioContext state management** — flagged in Phase 2 (Design: test tone edge cases) and Phase 3 (Eng subagent: AudioContext suspended). Both phases independently identified the same Web Audio API pitfall.

## NOT in Scope (Deferred)

| Item | Reason |
|---|---|
| AI parsing of free-text room description | Next PR |
| Digital twin visualization | Future feature |
| Changes to calibration algorithm | Out of scope |
| Subjective feedback collection | Phase 5 placeholder only |
| Visual room diagram | Out of scope |
| threading.Lock → asyncio.Lock refactor | Pre-existing, future PR |
| TTL cleanup sub gain restore on restart | Pre-existing, future PR |
| Full-room multi-channel measurement | TODO-R1 tracks this |

## Decision Audit Trail

| # | Phase | Decision | Classification | Principle | Rationale | Rejected |
|---|-------|----------|----------------|-----------|-----------|---------|
| 1 | CEO | Approach B (full workflow shell) over Approach A (preflight only) | Mechanical | P1+P2 | Workflow architecture is the stated goal; A defers the UX problem | Approach A, C |
| 2 | CEO | Cherry-pick: test tone frequency selector | Mechanical | P1 | In blast radius, direct debug value, <30min CC | None |
| 3 | CEO | Cherry-pick: config validation check | Mechanical | P1 | In blast radius, surfaces config gaps immediately | None |
| 4 | CEO | Cherry-pick: localStorage phase persistence | Mechanical | P5 | Trivial, explicit, good UX | None |
| 5 | CEO | Cherry-pick: debug log download | Mechanical | P1 | In blast radius, serves "no silent failures" goal | None |
| 6 | CEO | 404 for unknown /api/preflight/{name} | Mechanical | P5 | Explicit error > silent 500 | — |
| 7 | CEO | "Continue" button hidden until all checks pass | Mechanical | P5 | Prevents premature advancement | None |
| 8 | Design | Segmented buttons for frequency selector (not <select>) | Mechanical | P5 | Consistent with existing curve-row pattern | <select> |
| 9 | Design | Mobile nav collapses to "Step N of 5" | Mechanical | P1 | 5-step horizontal nav breaks <480px | None |
| 10 | Design | "Complete" step state distinct from "active" | Mechanical | P1 | User needs to know what's done vs. in-progress | None |
| 11 | Eng | Parameterized /api/preflight/{check_name} over individual routes | Mechanical | P5 | DRY, extensible, consistent | 7 individual routes |
| 12 | Eng | check_config() in PreflightChecker, not web.py | Mechanical | P5 | CLI `calibrate check` benefits too; consistent with existing pattern | web.py inline |
| 13 | Eng | dataclasses.asdict() for CheckResult serialization | Mechanical | P5 | DRY, no new Pydantic model needed | Pydantic model |
| 14 | Eng | check_playback_route(hdmi) reuses check_denon() internally | Mechanical | P1 | Prevents duplicate DenonAVR connection in run_all | Separate connection |
| 15 | Eng | audioCtx.resume() before oscillator start | Mechanical | P1 | Prevents silent tone on tab-focus-restore | None |


## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 1 | clean | 4 cherry-picks auto-approved, 3 gaps fixed |
| Eng Review | `/plan-eng-review` | Architecture & tests | 1 | issues_open | 15 decisions, 2 pre-existing issues flagged to TODOS |
| Design Review | `/plan-design-review` | UI/UX gaps | 1 | clean | 5 gaps resolved, accessibility specs added |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | N/A | unavailable (no codex binary) |

**Outside Voices:** Codex unavailable. Claude subagent [subagent-only] ran for Eng phase.
- 6 findings from subagent: 3 actioned (duplicate Denon connections, AudioContext resume, signal-path skip), 2 flagged pre-existing (threading.Lock, TTL cleanup), 1 noted non-issue (SSRF/1-user device).

**VERDICT:** READY FOR IMPLEMENTATION — run `/ship` when code is written.
