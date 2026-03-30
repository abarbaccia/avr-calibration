<!-- /autoplan restore point: /home/andrew/.gstack/projects/abarbaccia-avr-calibration/feat-a1evo-parity-autoplan-restore-20260330-013328.md -->
# Plan: Calibration Advisor — Community Best-Practice Features

**Branch:** feat-calibration-advisor (new branch from main)
**Feature:** Six community-sourced calibration improvements — target curves, sub trim advisor, seat-to-seat variance visualization, phase/time alignment display, Dynamic EQ advisor, and cardioid sub helper.

## CEO Review Decisions (2026-03-30)

| Feature | Decision |
|---|---|
| F1 Extended Target Curves | Ship as planned (pure front-end) |
| F2 Sub Trim Advisor | Ship as planned (advisory card) |
| F3 Seat-to-Seat Variance | Ship as planned (backend extension) |
| F4 Phase/Time Alignment | **Redesigned: store real IR + cross-correlation** (Hilbert invalid for rooms) |
| F5 Dynamic EQ Advisor | Ship as planned (dismissable advisory card) |
| F6 Cardioid Sub Helper | **GO: full implementation** (API verified: set_output_polarity + set_output_delay exist) |

## Features

### Feature 1: Extended Target Curves

Add HT-Aggressive and Musicality curves to the existing `<select id="curveSelect">`.

**Curve math:**
```javascript
// HT-Aggressive: steeper slope below 100 Hz
'ht': freqs.map(f => f >= 100 ? refSpl : refSpl + 4 * Math.log2(100 / f))

// Musicality: Gaussian peak at 30 Hz, σ=0.7 octaves
'music': freqs.map(f => {
  const octFromPeak = Math.log2(f / 30);
  return refSpl + 4 * Math.exp(-(octFromPeak ** 2) / (2 * 0.7 ** 2));
})
```

**UI:** Curve selector row above chart: `[Harman] [HT-Aggressive] [Musicality] [Flat]` — segmented button group, teal (#2dd4bf) active state, gray inactive. Selection persisted via `localStorage.setItem('targetCurve', type)`.

**States:** Chart re-renders immediately on selection. Delta table updates below chart.

### Feature 2: Sub Trim Advisor

**Card:** "Sub Trim Advisor" — appears below plotCard in page order.

**Interaction:**
- Number input: "Audyssey Sub Trim Level (dB)" — placeholder "-10"
- Color-coded badge + one-sentence guidance updates on input change
- Badge states (via CSS class):
  - `badge-optimal` (#22c55e green): -12 to -10 dB → "Optimal — physical gain is correctly calibrated."
  - `badge-warn` (#f59e0b amber): -10 to -5 dB → "Slightly hot — consider lowering physical gain 2-3 dB."
  - `badge-danger` (#ef4444 red): > -5 dB → "Too hot — lower physical gain knob and re-run Audyssey."
  - `badge-low` (#3b82f6 blue): < -12 dB → "Too low — increase physical gain or sub output level."
  - `badge-empty` (gray): no input → "Enter your Audyssey sub trim reading above."

**Empty state:** "Enter your Audyssey sub trim reading above." in muted gray. No badge shown until input has a value.

### Feature 3: Seat-to-Seat Variance Visualization

**Backend:** Extend `POST /api/sessions/average` to also compute and return `spl_variance` (per-bin standard deviation across sessions). If only 1 session (shouldn't happen per min_length=2 validation), variance is 0.

**Frontend:** After averaging, `averageSelected()` renders two additional hidden datasets in the Chart.js instance:
- `spl_upper = averaged_spl[i] + spl_variance[i]` — transparent fill dataset, no border
- `spl_lower = averaged_spl[i] - spl_variance[i]` — fill='between' to upper dataset
- Fill color: `rgba(45, 212, 191, 0.12)` (teal, very transparent)
- Legend label: "±1σ variance band"

**States:**
- Loading: "Averaging N sessions..." spinner text while POST in flight
- No variance: single-session mode (impossible via validation, but band = 0 collapses invisibly)
- Multi-session: band visible, legend shown

### Feature 4: Phase/Time Alignment Display (REDESIGNED)

**Redesign decision:** Stores actual impulse response alongside each measurement. Uses real bandpass cross-correlation at crossover. The original Hilbert approach was invalid for non-minimum-phase room responses.

**Storage:** New `impulse_response` column in measurements table (JSON blob, float32 array). ~96KB per session (24,000 samples at 48kHz, 500ms window). Migration: existing sessions have `NULL` — UI shows "Re-measure to enable phase check."

**Measurement change:** In `measurement.py`, after `measure_sweep()` returns the deconvolved IR, store the first 24,000 samples as a list alongside the session.

**Backend endpoint:**
```
POST /api/sessions/time-align
Body: {"sub_session_id": 3, "mains_session_id": 5}
```
- Loads IR for both sessions from DB
- If either IR is NULL → 422 with `{"error": "IR_NOT_AVAILABLE", "message": "Re-measure [sub|mains] session to enable phase check."}`
- Bandpass 60–100 Hz (4th-order Butterworth, `sosfiltfilt`)
- Normalized cross-correlation
- Returns `{"offset_ms": 10.3, "offset_feet": 11.6, "sub_leads": true, "recommendation": "..."}`

**Math:**
```python
from scipy.signal import butter, sosfiltfilt, correlate

def compute_time_offset_ms(ir1, ir2, f_lo=60, f_hi=100, sample_rate=48000):
    sos = butter(4, [f_lo, f_hi], btype='band', fs=sample_rate, output='sos')
    bp1 = sosfiltfilt(sos, ir1)
    bp2 = sosfiltfilt(sos, ir2)
    corr = correlate(bp1, bp2, mode='full')
    lag_samples = np.argmax(np.abs(corr)) - (len(ir2) - 1)
    return lag_samples / sample_rate * 1000  # ms
```

**Card UI:** "Phase Check" card — below measurement card.
- Session selects: "Sub session:" `<select>` + "Mains session:" `<select>` — populated from history
- Button: "Analyze Alignment"
- **Loading state:** spinner + "Computing cross-correlation..."
- **Result state:** Colored offset badge + recommendation sentence + conversion note
- **No-IR state:** per-session amber warning "⚠ No IR data — re-measure" shown inline next to session select
- **Error state:** "Analysis failed: [message]" in red

**Offset badge colors:**
- |offset| < 1ms: green "Well-aligned (< 1ms)"
- |offset| 1-5ms: amber "Moderate offset (Xms)"  
- |offset| > 5ms: red "Large offset (X ms) — adjust sub distance"

### Feature 5: Dynamic EQ Advisor

**Card:** Dismissable callout with amber left border (4px solid #f59e0b). Shown by default.

**Content:**
- Title: "⚠ Disable Dynamic EQ"
- Body: "Audyssey Dynamic EQ re-applies a loudness curve at every volume level. This fights your calibration by adding bass boost that conflicts with the Harman target you just measured. Disable it in AVR Settings → Audyssey → Dynamic EQ, or set Reference Level Offset to -15 dB."
- Button: "Got it — I've disabled it" → sets `localStorage.setItem('dynEqDismissed', '1')`, card fades out and is removed from DOM

**Empty/dismissed state:** Card not shown. No persistence in DOM — clean on re-dismiss.

**Priority:** Shown ABOVE sub trim advisor. Users need to see this before entering trim values.

### Feature 6: Cardioid Sub Configuration Helper

**API verified:** `MinidspClient.set_output_polarity()` (PUT /output/1/polarity) and `set_output_delay()` (PUT /output/1/delay) exist and are used in alignment.py.

**Config:** New `sub_separation_m` key in config.yaml (default: 1.0). Delay = `sub_separation_m / 343 * 1000` ms (capped at 30ms hardware limit).

**Card:** "Sub Array Mode" — shown only if config has 2+ sub outputs.
- Toggle: off (Normal) / on (Cardioid)
- **Normal state:** "Both sub outputs active, standard stereo configuration."
- **Cardioid state:** Computed delay shown ("Output 2: inverted polarity, 2.9ms delay"), amber warning "Effective above ~170 Hz at 1m separation. Verify with measurement after applying."
- **Loading state (applying):** Toggle disabled, spinner, "Applying cardioid mode..."
- **Error state:** "Failed to apply — check miniDSP connection." in red

**Backend endpoint:**
```
POST /api/signal-path/cardioid
Body: {"enabled": true, "delay_ms": 2.9}
```
- `enabled=true`: calls `set_output_polarity(1, inverted=True)` + `set_output_delay(1, delay_ms=X)`
- `enabled=false`: calls `set_output_polarity(1, inverted=False)` + `set_output_delay(1, 0.0)`
- MinidspApiError(404) on polarity → 200 with `{"status": "advisory_only", "message": "Polarity inversion not supported by hardware"}` — frontend shows manual-config instructions instead
- No sub_outputs in config → 422

## UI Layout — Card Order (top to bottom)

1. Measure card (existing)
2. **Dynamic EQ Advisor** (F5 — dismissable, amber border, most urgent)
3. Plot card + curve selector (existing + F1 curve buttons)
4. Delta table (existing)
5. **Sub Trim Advisor** (F2 — new advisory card)
6. **Phase Check** (F4 — sub/mains alignment)
7. **Seat-to-Seat Variance** note (F3 — shown inline in plot card after averaging, not separate card)
8. History card (existing — with avg button and checkboxes)
9. **Sub Array Mode** (F6 — shown conditionally if 2+ sub outputs)

## What Already Exists

| Sub-problem | Existing code |
|---|---|
| Target curve rendering | `getTargetCurve()`, `renderDeltaTable()` in web.py:~430–470 |
| Curve selector UI | `<select id="curveSelect">` in web.py:~233 |
| Chart.js datasets | `renderFR()` in web.py:~470–540 |
| Session averaging | `POST /api/sessions/average` in web.py:~870–930 |
| MinidspClient polarity/delay | `set_output_polarity`, `set_output_delay` in adapters/minidsp.py |
| Sub alignment phases | `run_alignment_phases()` in alignment.py |
| Session storage | `SessionStore` in storage.py |
| PyTTa IR output | `measure_sweep()` returns IR in measurement.py |

## Files Changed

- `calibrate/web.py` — F1, F2, F3 front-end, F4 backend, F5 front-end, F6 endpoint (~280 LOC)
- `calibrate/storage.py` — F4: `impulse_response` column + migration (~30 LOC)
- `calibrate/measurement.py` — F4: save IR with session (~20 LOC)
- `calibrate/adapters/minidsp.py` — F6: minimal wiring only, existing methods (~10 LOC)
- `tests/test_web.py` — F3, F4, F6 backend tests (~140 LOC)
- `tests/test_storage.py` — F4 IR storage tests (~30 LOC)
- `tests/test_minidsp_adapter.py` — F6 cardioid tests (~20 LOC)

**Total: ~530 LOC across 6 files**

## Test Plan

### Feature 1 — Front-end only, no backend tests

### Feature 2 — Front-end only, no backend tests

### Feature 3 (Variance bands)
- `test_average_sessions_returns_variance` — `spl_variance` in response
- `test_average_sessions_variance_zero_for_identical` — identical sessions → 0.0
- `test_average_sessions_variance_two_positions` — two sessions with known spl → verify σ

### Feature 4 (Time alignment)
- `test_store_ir_with_session` — measurement save stores IR blob in DB
- `test_ir_migration_nullable` — existing sessions without IR return NULL
- `test_time_align_happy_path` — two sessions with IR → offset_ms + offset_feet returned
- `test_time_align_session_not_found` — invalid ID → 404
- `test_time_align_no_ir` — session without IR → 422 with IR_NOT_AVAILABLE
- `test_time_align_crosscorr_known_offset` — inject IR with known 10ms offset → result within 1ms
- `test_compute_time_offset_ms_unit` — unit test for `compute_time_offset_ms()`

### Feature 5 — Front-end only, no backend tests

### Feature 6 (Cardioid)
- `test_cardioid_happy_path` — polarity + delay set on output 1
- `test_cardioid_disabled` — polarity normal, delay 0
- `test_cardioid_no_sub_outputs` — config without sub_outputs → 422
- `test_cardioid_polarity_404_fallback` — MinidspApiError(404) → advisory response, not 500

## Design Decisions (from Phase 2 Review)

**Colors (consistent with existing teal theme):**
- Active/accent: #2dd4bf (teal)
- Success: #22c55e (green)
- Warning: #f59e0b (amber)
- Danger: #ef4444 (red)
- Info: #3b82f6 (blue)
- Background: #1a1a1a
- Card border: #333
- Muted text: #888

**Interaction states:** Every card has loading, empty, error, and success states specified above.

**Card priority order:** Dynamic EQ first (most disruptive if ignored), then measurement flow, then analysis cards.

**Variance band:** Rendered inline in plot card (not a separate card) — avoids card proliferation.

**Cardioid toggle:** Disabled during API call — prevents double-submission.

**localStorage keys:** `targetCurve`, `dynEqDismissed` — both survive page refresh.
