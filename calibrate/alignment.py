"""Sub-alignment algorithm — MSO-inspired IR phase alignment for multiple subs.

Algorithm overview (Phases 1-4; Phase 3.5 APF deferred to TODO-10):

  Phase 1 — Measure each sub independently (Pi mutes all others, plays sweep)
  Phase 2 — Compute travel-time delays from IR peak positions
  Phase 3 — Detect and correct polarity (wiring errors)
  Phase 4 — Level-match subs to the loudest reference

Each sub measurement yields a SubIRResult containing the IR peak time, sign,
SPL, and whether polarity was corrected.  Phase 2 outputs per-sub delay offsets
(ms) written to miniDSP.  Phase 4 outputs gain trims (dB) written to miniDSP.

Signal-chain context:

  Pi (sweep) → miniDSP 2x4 HD (output 0 or 1) → sub
                                                     ↓
  UMIK-1 → browser (getUserMedia) → POST /api/align-subs/record → IR extraction

References:
  Welti & Devantier, "In-room low-frequency optimization" (AES 2006)
  MSO (Multi-Sub Optimizer) methodology
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .drivers.dsp_driver import DSPDriver
    from .measurement import MeasurementEngine

log = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

MUTE_GAIN_DB: float = -127.0
"""Gain value used to silence a sub output during per-sub measurement."""

DEFAULT_IR_SEARCH_WINDOW_MS: float = 50.0
"""Maximum time after sweep start to search for the IR peak (ms).

50 ms = 17.5 m at 343 m/s — more than enough for any realistic room.
A hardcoded 5 ms window would miss subs placed >1.7 m from the mic.
"""


# ── Data types ─────────────────────────────────────────────────────────────────

@dataclass
class SubIRResult:
    """Result of a single sub impulse-response measurement.

    sub_index        — index into config.measurement.sub_outputs
    peak_time_s      — IR peak position in seconds (travel time from sub to mic)
    peak_sign        — +1 or -1 (polarity of the IR peak)
    polarity_inverted — whether polarity was already corrected in Phase 3
    spl_db           — peak SPL estimate (20·log10(|max(IR)|))
    """

    sub_index: int
    peak_time_s: float
    peak_sign: int          # +1 or -1
    polarity_inverted: bool
    spl_db: float


# ── Phase helpers ──────────────────────────────────────────────────────────────

def extract_ir(
    sweep_samples: list[float],
    recording_samples: list[float],
    sample_rate: int,
    ir_search_window_ms: float = DEFAULT_IR_SEARCH_WINDOW_MS,
) -> tuple[object, int, int]:
    """Deconvolve recording with sweep to get the impulse response.

    Returns (ir_array, peak_idx, peak_sign) where:
        ir_array    — numpy ndarray of the full IR
        peak_idx    — sample index of the absolute-max peak within the search window
        peak_sign   — +1 if ir[peak_idx] >= 0, else -1
    """
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("numpy is required") from exc

    sweep_arr = np.array(sweep_samples, dtype=np.float64)
    rec_arr = np.array(recording_samples, dtype=np.float64)

    # Zero-pad both to the same length before FFT
    n = max(len(sweep_arr), len(rec_arr))
    X = np.fft.rfft(sweep_arr, n=n)
    Y = np.fft.rfft(rec_arr, n=n)

    # Wiener-style deconvolution: H = Y·conj(X) / (|X|² + ε)
    # Epsilon is proportional to peak signal power (not a fixed constant)
    # to avoid amplifying noise where the sweep has little energy.
    X_power = np.abs(X) ** 2
    epsilon = max(float(np.max(X_power)) * 1e-6, 1e-20)
    H = Y * np.conj(X) / (X_power + epsilon)
    ir = np.fft.irfft(H, n=n)

    # Onset detection within the search window: find the first sample within
    # 20 dB of the absolute peak rather than the loudest peak itself.  In rooms
    # with strong bass modes, a late resonance can exceed the direct sound.
    search_samples = max(1, int(ir_search_window_ms / 1000.0 * sample_rate))
    search_window = ir[:search_samples]
    abs_window = np.abs(search_window)
    max_idx = int(np.argmax(abs_window))
    onset_threshold = abs_window[max_idx] * 0.1  # -20 dB
    peak_idx = int(np.argmax(abs_window > onset_threshold))
    peak_sign = 1 if ir[peak_idx] >= 0 else -1

    return ir, peak_idx, peak_sign


def compute_delay_offsets(ir_results: list[SubIRResult]) -> list[float]:
    """Compute per-sub delay offsets (ms) to time-align all subs.

    Sub with the latest peak is the reference (delay = 0).  All other subs
    receive a positive delay to bring them into alignment.

    Returns a list[float] of the same length as ir_results.  Index i gives
    the delay (ms) that should be applied to ir_results[i].sub_index.
    """
    if not ir_results:
        return []
    t_ref = max(r.peak_time_s for r in ir_results)
    return [(t_ref - r.peak_time_s) * 1000.0 for r in ir_results]


async def measure_sub_ir(
    engine: "MeasurementEngine",
    recording_samples: list[float],
    sweep_samples: list[float],
    sample_rate: int,
    sub_index: int,
    ir_search_window_ms: float = DEFAULT_IR_SEARCH_WINDOW_MS,
) -> SubIRResult:
    """Extract IR from a recording and return a SubIRResult.

    The caller is responsible for having already muted all subs except the one
    being measured.  This function is pure signal-processing — it does NOT
    touch any hardware.

    Quality validation (sweep capture + SNR checks) is run first via
    MeasurementEngine.validate_recording(); MeasurementQualityError is
    propagated to the caller on failure.
    """
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("numpy is required") from exc

    sweep_arr = np.array(sweep_samples, dtype=np.float64)
    rec_arr = np.array(recording_samples, dtype=np.float64)

    # Reuse the existing quality gate (sweep capture + SNR checks)
    engine.validate_recording(np, sweep_arr, rec_arr, sample_rate)

    ir, peak_idx, peak_sign = extract_ir(
        sweep_samples, recording_samples, sample_rate, ir_search_window_ms
    )

    peak_time_s = peak_idx / sample_rate
    spl_db = 20.0 * float(np.log10(abs(float(ir[peak_idx])) + 1e-12))

    return SubIRResult(
        sub_index=sub_index,
        peak_time_s=peak_time_s,
        peak_sign=peak_sign,
        polarity_inverted=False,  # Phase 3 sets this if correction was needed
        spl_db=spl_db,
    )


async def detect_and_correct_polarity(
    ir_results: list[SubIRResult],
    sub_outputs: list[int],
    driver: "DSPDriver",
) -> list[SubIRResult]:
    """Phase 3 — correct polarity of any sub whose IR peak sign differs from sub 0.

    Sub 0 (ir_results[0]) is the polarity reference.  Any sub with an
    opposite peak sign gets set_output_polarity(inverted=True) and has its
    polarity_inverted flag set to True.

    Returns a new list of SubIRResults with polarity_inverted updated.
    """
    if not ir_results:
        return ir_results

    ref_sign = ir_results[0].peak_sign
    updated: list[SubIRResult] = []

    for result in ir_results:
        output_idx = sub_outputs[result.sub_index]
        needs_inversion = (result.peak_sign != ref_sign)
        if needs_inversion:
            try:
                await driver.set_output_polarity(output_idx, inverted=True)
                log.info(
                    "Polarity corrected for output %d (sub_index=%d)",
                    output_idx,
                    result.sub_index,
                )
                updated.append(
                    SubIRResult(
                        sub_index=result.sub_index,
                        peak_time_s=result.peak_time_s,
                        peak_sign=result.peak_sign,
                        polarity_inverted=True,
                        spl_db=result.spl_db,
                    )
                )
            except Exception as exc:
                log.warning(
                    "set_output_polarity failed for output %d: %s — skipping",
                    output_idx,
                    exc,
                )
                updated.append(result)
        else:
            updated.append(result)

    return updated


async def level_match_subs(
    ir_results: list[SubIRResult],
    sub_outputs: list[int],
    driver: "DSPDriver",
) -> list[float]:
    """Phase 4 — write gain trims so all subs have equal SPL at the mic.

    The loudest sub is the reference (gain trim = 0).  Quieter subs receive a
    positive gain trim to match.

    Returns the list of gain_trim_db values (one per ir_result, same order).
    """
    if not ir_results:
        return []

    ref_spl = max(r.spl_db for r in ir_results)
    trims: list[float] = []

    for result in ir_results:
        trim = ref_spl - result.spl_db  # positive → boost the quieter sub
        trims.append(trim)
        output_idx = sub_outputs[result.sub_index]
        try:
            await driver.set_output_gain(output_idx, trim)
        except Exception as exc:
            log.warning(
                "level_match: set_output_gain(%d, %.2f) failed: %s",
                output_idx,
                trim,
                exc,
            )

    return trims


async def apply_delays(
    delay_offsets_ms: list[float],
    ir_results: list[SubIRResult],
    sub_outputs: list[int],
    driver: "DSPDriver",
) -> None:
    """Phase 2 — write delay offsets to DSP outputs."""
    from .adapters.minidsp import MAX_DELAY_MS

    for i, (delay_ms, result) in enumerate(zip(delay_offsets_ms, ir_results)):
        output_idx = sub_outputs[result.sub_index]
        if delay_ms > MAX_DELAY_MS:
            log.warning(
                "Computed delay %.2f ms for output %d exceeds hardware max %s ms — clamping",
                delay_ms,
                output_idx,
                MAX_DELAY_MS,
            )
            delay_ms = MAX_DELAY_MS
        if delay_ms <= 0.0:
            continue  # reference sub — no delay needed
        try:
            await driver.set_output_delay(output_idx, delay_ms)
        except Exception as exc:
            log.warning(
                "apply_delays: set_output_delay(%d, %.2f) failed: %s",
                output_idx,
                delay_ms,
                exc,
            )


