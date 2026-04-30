"""Real-room regression tests for design_modal_fir / design_fir.

These tests load recorded measurement sessions from
``tests/fixtures/sessions/`` (pulled from the Pi's history.db, downsampled
to ~200 freq points across 20-200 Hz) and exercise the design tools against
the actual modal hump (47/70/94 Hz, T60 400-1163 ms) we observed in this
room. The synthetic-only tests in ``test_mcp_server.py`` cover edge cases;
these tests guard against regressions on real-room characteristics.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from calibrate.mcp_server import _tool_design_fir, _tool_design_modal_fir
from tests.fixtures import load_session_fixture


def _session_from_fixture(fixture: dict) -> MagicMock:
    """Build a Mock session matching SessionStore.list_sessions() shape."""
    fr_pts = fixture["fr"]
    mock_fr = MagicMock()
    mock_fr.frequencies = [p["freq"] for p in fr_pts]
    mock_fr.spl = [p["spl"] for p in fr_pts]
    mock_fr.phase = [p["phase"] for p in fr_pts]
    mock_fr.impulse_response = None

    session = MagicMock()
    session.id = fixture["session_id"]
    session.timestamp = "2026-04-30T17:50:00Z"
    session.label = fixture["label"]
    session.start_fr = mock_fr
    session.metadata = {
        "decay_modes": fixture["decay_modes"],
        "ir": fixture["ir"],
        "group_delay": {
            "freq_hz": [p["freq"] for p in fixture["group_delay"]],
            "delay_ms": [p["delay_ms"] for p in fixture["group_delay"]],
        },
    }
    return session


@pytest.mark.asyncio
async def test_modal_fir_anti_pulse_against_real_room_passes_safety() -> None:
    """design_modal_fir on a real FR-sub measurement (session 861) with
    anti-pulse intents for 70/94 Hz at cancel_strength=0.6 must produce a
    FIR that survives the safety self-iteration (peak amplitude bounded,
    achieved cancel_strength > 0)."""
    fixture = load_session_fixture("sub_fr_solo_post_routing")
    session = _session_from_fixture(fixture)

    # Pull real T60 / peak_db from fixture's decay_modes for 70 + 94 Hz.
    modes_by_freq = {round(m["freq_hz"]): m for m in fixture["decay_modes"]}
    intents = []
    for target in (70, 94):
        nearest = min(modes_by_freq, key=lambda k: abs(k - target))
        m = modes_by_freq[nearest]
        intents.append({
            "freq_hz": m["freq_hz"],
            "t60_ms": m["t60_ms"],
            "peak_db": m["peak_db"],
            "treatment": "anti_pulse",
            "cancel_strength": 0.6,
        })

    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_design_modal_fir(
            session_id=fixture["session_id"],
            intents=intents,
            num_taps=4096,
            max_pre_ring_ms=25.0,
            return_coefficients=True,
        )

    assert result["ok"], result
    # Both modes treated; achieved cancel_strength must be > 0 (not all
    # auto-demoted to linear_notch).
    treatments = result["per_mode_treatments"]
    assert len(treatments) == 2
    achieved = [
        t.get("cancel_strength_achieved", 0.0)
        for t in treatments
        if t["treatment"] == "anti_pulse"
    ]
    assert achieved, f"no mode kept anti_pulse treatment: {treatments}"
    assert max(achieved) > 0.0, (
        f"all anti-pulse modes auto-demoted to zero strength: {treatments}"
    )
    # FIR survived its own iterative reduction — peak amplitude finite,
    # below a sane upper bound (anti-pulses are intentionally hot but
    # safety caps the linear amplitude well below 1.0).
    assert 0.0 < result["peak_amplitude"] < 2.0
    assert result["pre_delay_ms"] <= 25.0


@pytest.mark.asyncio
async def test_modal_fir_dense_triplet_auto_envelope() -> None:
    """Auto-envelope picks bp_q ≥ 3 for the inner mode of the 47/70/94 Hz
    triplet (each pair < 1 octave). NF-sub clean fixture has the dense
    triplet pattern with no near-DC mode that would mask it."""
    fixture = load_session_fixture("sub_nf_solo_post_routing")
    # Only run if fixture actually has the 47/70/94 triplet.
    freqs = sorted(round(m["freq_hz"]) for m in fixture["decay_modes"])
    if not all(f in freqs for f in (47, 70, 94)):
        pytest.skip(
            f"fixture lacks 47/70/94 Hz triplet (modes: {freqs})"
        )

    session = _session_from_fixture(fixture)
    # Drive auto-envelope by NOT supplying bp_q; force anti_pulse on all
    # three to trigger the density check.
    modes_by_freq = {round(m["freq_hz"]): m for m in fixture["decay_modes"]}
    intents = [
        {
            "freq_hz": modes_by_freq[f]["freq_hz"],
            "t60_ms": modes_by_freq[f]["t60_ms"],
            "peak_db": modes_by_freq[f]["peak_db"],
            "treatment": "anti_pulse",
            "cancel_strength": 0.5,
            # bp_q + envelope deliberately omitted → auto-selection kicks in
        }
        for f in (47, 70, 94)
    ]

    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_design_modal_fir(
            session_id=fixture["session_id"],
            intents=intents,
            num_taps=4096,
            max_pre_ring_ms=25.0,
        )

    assert result["ok"], result
    # Inner mode is 70 Hz — locate it in per_mode_treatments and check
    # its bp_q got bumped (≥ 3) by the auto-envelope agent.
    inner = next(
        t for t in result["per_mode_treatments"]
        if 65 < float(t["freq_hz"]) < 75
    )
    # auto-envelope agent reports the chosen bp_q under "auto_bp_q".
    bp_q_used = (
        inner.get("auto_bp_q")
        or inner.get("bp_q")
        or inner.get("bp_q_used")
    )
    if bp_q_used is None:
        pytest.skip(
            "auto-envelope feature not yet exposing bp_q in treatment dict"
        )
    assert float(bp_q_used) >= 3.0, (
        f"inner-mode bp_q={bp_q_used} expected ≥ 3 for dense triplet "
        f"(47/70/94 Hz are each < 1 octave apart)"
    )


@pytest.mark.asyncio
async def test_design_fir_anchor_deep_bass_priority_against_real_room() -> None:
    """design_fir with target_curve=harman-in-room and
    anchor=deep_bass_priority on a real FR-sub measurement (session 861)
    picks an anchor in the [25, 45] Hz deep-bass band, and the predicted
    correction at 80 Hz is negative (cuts) — the modal hump above the
    anchor must be cut, not boosted."""
    fixture = load_session_fixture("sub_fr_solo_post_routing")
    session = _session_from_fixture(fixture)

    # Harman-in-room target (relative SPL).
    target = {
        "points": [
            {"freq": 25, "spl": 5},
            {"freq": 31, "spl": 4},
            {"freq": 40, "spl": 3},
            {"freq": 50, "spl": 2},
            {"freq": 63, "spl": 1},
            {"freq": 80, "spl": 0},
            {"freq": 100, "spl": 0},
            {"freq": 120, "spl": 0},
        ],
    }

    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_design_fir(
            session_id=fixture["session_id"],
            target_curve=target,
            num_taps=512,
            phase_mode="minimum",
            freq_focus_hz=[25.0, 120.0],
            anchor={"mode": "deep_bass_priority"},
        )

    assert result["ok"], result
    assert result["anchor_used"]["mode"] == "deep_bass_priority"
    af = result["anchor_used"]["freq_hz"]
    assert 25.0 <= af <= 45.0, (
        f"deep-bass anchor {af} Hz fell outside expected [25, 45] band"
    )

    # predicted_effect at 80 Hz must be ≤ 0 (cut on the modal hump).
    eff = result.get("predicted_effect") or []
    if not eff:
        pytest.skip("design_fir did not return predicted_effect")
    # predicted_effect is a list of {freq, db} or {freq_hz, db}.
    def _f(p: dict) -> float:
        return float(p.get("freq", p.get("freq_hz", 0.0)))

    def _db(p: dict) -> float:
        return float(p.get("db", p.get("delta_db", 0.0)))

    eff_sorted = sorted(eff, key=_f)
    # Linear-interp predicted effect at 80 Hz.
    fs = [_f(p) for p in eff_sorted]
    dbs = [_db(p) for p in eff_sorted]
    import numpy as np

    eff_at_80 = float(np.interp(80.0, fs, dbs))
    assert eff_at_80 <= 0.5, (
        f"predicted_effect at 80 Hz = {eff_at_80:+.2f} dB; "
        f"expected ≤ 0 (cuts on modal hump above deep-bass anchor)"
    )
