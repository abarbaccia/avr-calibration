# Recipe: Post-Room-Correction Integration Verification

## Goal

After sub calibration (DSP) and room correction (AVR auto-EQ), verify that the
complete system integrates properly. Measure the combined response at the listening
position, check sub-to-main crossover integration, and recommend adjustments.

**This recipe does not apply EQ.** It measures and recommends only. Adjustments
are made via AVR settings or by re-running the appropriate calibration recipe.

## Prerequisites

- Sub calibration completed (DSP EQ applied via `/calibrate`)
- Room correction run on the AVR
- AVR set to normal listening mode (room correction processing active, NOT bypass/direct)
- All speakers at their final physical positions
- Mic at the primary listening position (MLP)

## Filter Strategy

**No filters are written by this recipe.** All analysis is read-only.

## Pre-flight

### 0.1 System check

Call `check_system` to verify all hardware is reachable.

### 0.2 Read current configuration

Call `get_config` to understand the output layout (which outputs are subs, shakers).
Call `get_device_state` to capture the AVR's current state (volume, input, sound mode).

### 0.3 Confirm room correction is active

Verify the AVR is NOT in a bypass or direct mode — room correction processing must
be active for verification to reflect the user's actual listening experience.

If the AVR is in a bypass/direct mode, **STOP** and ask the user to switch to their
normal listening mode with room correction engaged.

### 0.4 Gather user context

Ask the user:
1. What crossover frequency is set on the AVR? (default: 80Hz)
2. What room correction curve/mode are you using?
3. Is any dynamic volume/EQ feature enabled?

Record these — they affect interpretation of the results.

## Phase 1 — Sub-Only Baseline

Measure the calibrated sub response in isolation to establish what our
calibration achieved.

### 1.1 Measure subs only

Take a measurement through the normal sub calibration path (DSP outputs).
Use label "verify-subs-only" and position "MLP".

This captures the sub response WITH our DSP calibration applied.

### 1.2 Record key metrics

From the measurement, note:
- Average SPL across 30–80Hz (sub level reference)
- RMS deviation from target curve (if a target was used during calibration)
- -3dB rolloff point (low-end extension)
- Any remaining peaks or nulls

Call `get_measurement_history(limit=1, min_hz=20, max_hz=200, format="compact")`
to retrieve the detailed FR data.

## Phase 2 — Full-System Measurement

Measure the complete system — all speakers, all processing active — as the user
would hear it during normal playback.

### 2.1 Configure for full-range measurement

This measurement requires a different signal path than sub calibration:
- The AVR must stay in its normal listening mode (room correction active, bass management active)
- The sweep must be full-range (20Hz–20kHz), not sub-only
- The AVR's bass management routes low frequencies to subs and high frequencies to mains

Call `measure` with `full_range=true` to use the full-system measurement mode.
This mode preserves the AVR's current sound mode and uses a full-range sweep.

Use label "verify-full-system" and position "MLP".

**Fallback (if full_range mode not yet available):**
Ask the user to confirm the AVR is in normal listening mode at a moderate volume.
Take the measurement with the standard `measure` tool — note the limited frequency
range in the report and focus analysis on the sub-bass and crossover region only.

### 2.2 Record full-range metrics

From the measurement:
- Overall response shape and tilt
- Average SPL in key bands:
  - Sub band: 30–60Hz (pure sub output, below crossover)
  - Crossover band: one octave centered on the user's crossover frequency
  - Main band: 200–2kHz (pure main output, above crossover)
- Response smoothness (standard deviation within each band)

## Phase 3 — Crossover Integration Analysis

The critical region where subs hand off to mains, centered on the AVR's crossover
frequency, spanning roughly one octave above and below.

### 3.1 Compare sub-only to full-system in crossover region

Examine both measurements in the crossover region:

**Level continuity at crossover:**
- At the crossover frequency, the full-system response should be approximately
  3–6dB louder than subs-only (both subs and mains contributing)
- A DIP at crossover indicates phase cancellation between subs and mains
- A large PEAK (>6dB above adjacent frequencies) indicates excessive overlap

**Slope matching:**
- The sub response should roll off above the crossover frequency
- The main response should roll off below the crossover frequency
- These slopes should complement each other for a smooth combined response

**Phase alignment at crossover:**
- A narrow null (>10dB dip) right at the crossover frequency means the subs
  and mains arrive out of phase — the #1 integration problem
- Often fixable with sub distance adjustment on the AVR

### 3.2 Rate crossover quality

| Rating | Criteria |
|--------|----------|
| **Good** | Smooth through crossover region, no dips >3dB |
| **Fair** | Small dip (3–6dB) or bump at crossover, may be audible on some content |
| **Poor** | Deep null (>6dB) or large peak at crossover — needs correction |
| **Critical** | >10dB null at crossover — subs and mains are fighting each other |

## Phase 4 — Level Balance Check

### 4.1 Sub level relative to mains

Compare average SPL in two bands from the full-system measurement:
- **Sub band**: 30–60Hz (well below crossover, pure sub output)
- **Main band**: 200–2kHz (well above crossover, pure main output)

Interpretation depends on calibration target:

| Target | Expected sub-to-main difference |
|--------|---------------------------------|
| Harman preference | Sub band +3 to +5dB above main band |
| Flat | Sub band matches main band (±1dB) |
| Custom house curve | Depends on user preference |

If the difference is outside the expected range:
- Too loud: recommend reducing sub level on AVR
- Too quiet: recommend increasing sub level on AVR
- Compute the specific dB adjustment needed

### 4.2 Dynamic processing interaction

If the AVR has a dynamic volume or dynamic EQ feature enabled:
- These features boost bass at lower listening volumes
- At reference level they typically have no effect
- At –20dB they may add 5–8dB of bass boost
- This interacts with DSP sub calibration — warn if combined boost seems excessive

## Phase 5 — Report and Recommendations

### 5.1 Integration scorecard

| Category | Metric | Rating |
|----------|--------|--------|
| Crossover integration | Smoothness through crossover region | Good/Fair/Poor/Critical |
| Sub level balance | Sub vs main level difference | Good/Fair/Poor |
| Overall tonal balance | Full-range response shape | Good/Fair/Poor |
| Bass extension | -3dB point vs sub capability | Good/Fair/Poor |

### 5.2 Recommendations

Based on the analysis, recommend adjustments in priority order:

**Crossover issues (fix first — biggest audible impact):**
- Phase cancellation → adjust sub distance on AVR (try both directions)
- Excessive overlap → raise crossover frequency or reduce sub level
- Insufficient overlap → lower crossover frequency or check AVR speaker size settings

**Level issues:**
- Subs too loud → reduce sub level trim on AVR by {X}dB
- Subs too quiet → increase sub level trim on AVR by {X}dB
- Note dynamic processing interaction at typical listening volume

**Tonal balance (room correction curve selection):**
- Bright overall → try a more rolled-off room correction curve
- Dull overall → try a flatter room correction curve
- Inconsistent → room correction may need re-running

### 5.3 When to re-run sub calibration

Recommend re-running `/calibrate` if:
- Room correction changed the sub distance significantly
- Sub level trim needed major adjustment (>3dB)
- Crossover frequency was changed (affects frequency range subs cover)

After AVR adjustments, suggest running `/verify` again to confirm improvement.

## Convergence

This recipe does not iterate. It produces a single report. If the user makes
adjustments, they should run `/verify` again to check the result.

## When issues persist

If verification shows poor integration after adjustments:
- Deep null at crossover that doesn't respond to distance changes → sub
  placement issue, consider moving the sub
- Subs and mains wildly different levels → check room correction calibration,
  may need re-running with careful mic placement
- Severe response irregularities above 200Hz → room correction issue, not sub-related

## MCP tools used

- `check_system` — pre-flight verification
- `get_config` — hardware layout and output types
- `get_device_state` — AVR state (sound mode, volume, input)
- `measure` — take frequency response measurements (sub-only and full-range)
- `get_measurement_history` — retrieve detailed FR data for comparison
- `get_fr_summary` — quick 1/3-octave overview
