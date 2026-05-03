"""Tests for per-sub modal data + simulate tools (LLM-first allocation).

These tools support the per-sub modal-FIR recipe documented in CLAUDE.md
(LLM-first design rule). The LLM decides per-sub anti-pulse strength
allocation; these tools provide DATA and SIMULATION only — no allocator.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from calibrate.mcp_server import (
    _tool_analyze_per_sub_modal_contribution,
    _tool_simulate_per_sub_fir,
)
from tests.fixtures import load_session_fixture


def _session_from_fixture(fixture: dict, override_id: int | None = None) -> MagicMock:
    """Build a Mock session matching SessionStore.list_sessions() shape."""
    fr_pts = fixture["fr"]
    mock_fr = MagicMock()
    mock_fr.frequencies = [p["freq"] for p in fr_pts]
    mock_fr.spl = [p["spl"] for p in fr_pts]
    mock_fr.phase = [p["phase"] for p in fr_pts]
    mock_fr.sample_rate = int(fixture.get("sample_rate", 48000))

    session = MagicMock()
    session.id = int(override_id) if override_id is not None else fixture["session_id"]
    session.timestamp = "2026-04-30T17:50:00Z"
    session.label = fixture["label"]
    session.start_fr = mock_fr
    session.impulse_response = None
    session.metadata = {
        "decay_modes": fixture["decay_modes"],
        "ir": fixture["ir"],
    }
    return session


def _two_sub_fixtures() -> tuple[MagicMock, MagicMock]:
    fa = load_session_fixture("sub_fr_solo_post_routing")
    fb = load_session_fixture("sub_nf_solo_post_routing")
    sa = _session_from_fixture(fa, override_id=911)
    sb = _session_from_fixture(fb, override_id=912)
    return sa, sb


def _wire_store_mock(MockStore: MagicMock, *sessions: MagicMock) -> None:
    """Configure a SessionStore mock to support both list_sessions() and
    get_session(id) — the production tool prefers the single-row fetch
    to avoid OOM on the Pi when the store has many sessions.
    """
    by_id = {int(s.id): s for s in sessions}

    def _by_id(sid: int) -> MagicMock | None:
        return by_id.get(int(sid))

    MockStore.return_value.list_sessions.return_value = list(sessions)
    MockStore.return_value.get_session.side_effect = _by_id


# ─── analyze_per_sub_modal_contribution ─────────────────────────────────────


@pytest.mark.asyncio
async def test_returns_modes_for_two_subs() -> None:
    """Sanity check: shape + per-sub data for both sessions."""
    sa, sb = _two_sub_fixtures()
    with patch("calibrate.storage.SessionStore") as MockStore:
        _wire_store_mock(MockStore, sa, sb)
        result = await _tool_analyze_per_sub_modal_contribution(
            session_ids=[911, 912],
        )

    assert result["ok"], result
    assert result["session_ids"] == [911, 912]
    assert len(result["modes"]) > 0
    for mode in result["modes"]:
        assert "freq_hz" in mode
        assert "per_sub" in mode
        assert len(mode["per_sub"]) == 2
        for entry in mode["per_sub"]:
            assert "session_id" in entry
            assert "peak_db" in entry
            assert "t60_ms" in entry
            assert "phase_rad" in entry
            assert "fixability" in entry


@pytest.mark.asyncio
async def test_modal_coupling_share_sums_to_one_per_mode() -> None:
    """modal_coupling_share is normalised to sum=1 across subs (or all-zero
    when neither sub excites the mode)."""
    sa, sb = _two_sub_fixtures()
    with patch("calibrate.storage.SessionStore") as MockStore:
        _wire_store_mock(MockStore, sa, sb)
        result = await _tool_analyze_per_sub_modal_contribution(
            session_ids=[911, 912],
        )

    for mode in result["modes"]:
        shares = mode["modal_coupling_share"]
        s = sum(shares)
        # Either ~1 (mode excited by at least one sub) or 0 (both peak_db ≤ 0).
        assert abs(s - 1.0) < 1e-2 or s == 0.0, (
            f"share sum {s} for mode {mode['freq_hz']}: {shares}"
        )


@pytest.mark.asyncio
async def test_uses_decay_modes_from_session_metadata() -> None:
    """When sessions already carry decay_modes in metadata, the tool reuses
    them (no analyze_decay call needed)."""
    sa, sb = _two_sub_fixtures()
    with patch("calibrate.storage.SessionStore") as MockStore, patch(
        "calibrate.mcp_server._tool_analyze_decay"
    ) as mock_decay:
        _wire_store_mock(MockStore, sa, sb)
        result = await _tool_analyze_per_sub_modal_contribution(
            session_ids=[911, 912],
        )

    assert result["ok"], result
    # Both sessions had decay_modes in metadata; tool must not have called
    # analyze_decay as a fallback.
    mock_decay.assert_not_called()


@pytest.mark.asyncio
async def test_handles_modes_present_in_only_one_sub() -> None:
    """Modes detected by only one sub still appear, with the other sub
    reporting peak_db=0 (didn't excite that mode)."""
    sa, sb = _two_sub_fixtures()

    # Force disjoint mode sets: sa has only a 70 Hz mode, sb has only 47 Hz.
    sa.metadata = dict(sa.metadata)
    sa.metadata["decay_modes"] = [
        {"freq_hz": 70.0, "t60_ms": 600.0, "peak_db": 6.0, "suggested_q": 5.0, "priority": 1}
    ]
    sb.metadata = dict(sb.metadata)
    sb.metadata["decay_modes"] = [
        {"freq_hz": 47.0, "t60_ms": 500.0, "peak_db": 8.0, "suggested_q": 4.0, "priority": 1}
    ]

    with patch("calibrate.storage.SessionStore") as MockStore:
        _wire_store_mock(MockStore, sa, sb)
        result = await _tool_analyze_per_sub_modal_contribution(
            session_ids=[911, 912],
        )

    assert result["ok"], result
    # 47 Hz cluster: sa peak_db=0 (sa lacks 47 Hz mode); sb peak_db=8.
    near47 = [m for m in result["modes"] if 44 <= m["freq_hz"] <= 50]
    assert near47, f"no 47 Hz cluster in modes: {[m['freq_hz'] for m in result['modes']]}"
    entries = near47[0]["per_sub"]
    sa_entry = next(e for e in entries if e["session_id"] == 911)
    sb_entry = next(e for e in entries if e["session_id"] == 912)
    assert sa_entry["peak_db"] == 0.0
    assert sb_entry["peak_db"] > 0.0


# ─── memory: per-sub tool must NOT call list_sessions() ─────────────────────


@pytest.mark.asyncio
async def test_does_not_call_list_sessions_oom_regression() -> None:
    """Regression: analyze_per_sub_modal_contribution must fetch each session
    via get_session() (single SQLite row) — never list_sessions(), which
    materialises every session's IR blob into memory and OOM-kills the
    worker on the 4 GiB Pi.

    Repro before this fix: a single call with two session_ids on a Pi with
    ~50 stored sessions caused the MCP server log to end with 'Killed' (no
    traceback) — the kernel OOM-killer fired. The fix is to load one
    session at a time, drop the heavy Session reference between iterations,
    and force a GC cycle so the IR blob is actually freed. Helper analytics
    (decay, phase) run inline against the fetched Session — never via the
    public _tool_analyze_decay/_tool_analyze_phase entrypoints, which each
    call list_sessions() internally.
    """
    sa, sb = _two_sub_fixtures()
    with patch("calibrate.storage.SessionStore") as MockStore:
        _wire_store_mock(MockStore, sa, sb)
        result = await _tool_analyze_per_sub_modal_contribution(
            session_ids=[911, 912],
        )

    assert result["ok"], result
    assert MockStore.return_value.list_sessions.call_count == 0, (
        "analyze_per_sub_modal_contribution called list_sessions() — "
        "this loads every session's IR blob into memory and OOM-kills "
        "the worker on the Pi. Use get_session(id) instead."
    )
    # And get_session must be called once per id (no redundant fetches).
    assert MockStore.return_value.get_session.call_count == 2


# ─── simulate_per_sub_fir ───────────────────────────────────────────────────


def _two_intents_for(fixture: dict) -> list[dict]:
    """Build anti_pulse intents from a fixture's loudest / longest modes."""
    modes = sorted(fixture["decay_modes"], key=lambda m: m["t60_ms"], reverse=True)[:1]
    return [
        {
            "freq_hz": m["freq_hz"],
            "t60_ms": m["t60_ms"],
            "peak_db": m["peak_db"],
            "treatment": "anti_pulse",
            "cancel_strength": 0.5,
        }
        for m in modes
    ]


@pytest.mark.asyncio
async def test_predicts_combined_fr_for_simple_synthesis() -> None:
    """Two real solo-sub fixtures, anti_pulse on the loudest mode each.
    Verify predicted_combined_fr_db has the right shape and sane values."""
    fa = load_session_fixture("sub_fr_solo_post_routing")
    fb = load_session_fixture("sub_nf_solo_post_routing")
    sa = _session_from_fixture(fa, override_id=911)
    sb = _session_from_fixture(fb, override_id=912)

    specs = [
        {
            "session_id": 911,
            "output_index": 5,
            "intents": _two_intents_for(fa),
            "num_taps": 4096,
            "max_pre_ring_ms": 25.0,
            "samplerate": 48000,
        },
        {
            "session_id": 912,
            "output_index": 6,
            "intents": _two_intents_for(fb),
            "num_taps": 4096,
            "max_pre_ring_ms": 25.0,
            "samplerate": 48000,
        },
    ]

    with patch("calibrate.storage.SessionStore") as MockStore:
        _wire_store_mock(MockStore, sa, sb)
        result = await _tool_simulate_per_sub_fir(per_sub_specs=specs)

    assert result["ok"], result
    fr = result["predicted_combined_fr_db"]
    assert len(fr) > 10
    for pt in fr:
        assert "freq_hz" in pt and "spl_db" in pt
    # All predicted SPL values are finite numbers in a sensible range.
    spls = [pt["spl_db"] for pt in fr]
    assert all(-200 < s < 200 for s in spls), spls

    modes = result["predicted_per_mode_t60_reduction_pct"]
    assert len(modes) >= 1
    for m in modes:
        assert "freq_hz" in m
        assert "per_sub_strength" in m
        assert len(m["per_sub_strength"]) == 2
        assert "predicted_combined_t60_reduction_pct" in m


@pytest.mark.asyncio
async def test_per_sub_intents_pass_through() -> None:
    """LLM-supplied intents are honoured: cancel_strength surfaces in the
    per_sub_strength array of the predicted mode."""
    fa = load_session_fixture("sub_fr_solo_post_routing")
    fb = load_session_fixture("sub_nf_solo_post_routing")
    sa = _session_from_fixture(fa, override_id=911)
    sb = _session_from_fixture(fb, override_id=912)

    # Pick a shared frequency that exists in both fixtures (70 Hz region).
    shared_freq = 70.3
    shared_t60 = 500.0
    shared_peak = 8.0

    specs = [
        {
            "session_id": 911,
            "output_index": 5,
            "intents": [{
                "freq_hz": shared_freq, "t60_ms": shared_t60, "peak_db": shared_peak,
                "treatment": "anti_pulse", "cancel_strength": 0.7,
            }],
            "num_taps": 4096, "samplerate": 48000,
        },
        {
            "session_id": 912,
            "output_index": 6,
            "intents": [{
                "freq_hz": shared_freq, "t60_ms": shared_t60, "peak_db": shared_peak,
                "treatment": "anti_pulse", "cancel_strength": 0.3,
            }],
            "num_taps": 4096, "samplerate": 48000,
        },
    ]

    with patch("calibrate.storage.SessionStore") as MockStore:
        _wire_store_mock(MockStore, sa, sb)
        result = await _tool_simulate_per_sub_fir(per_sub_specs=specs)

    assert result["ok"], result
    near = [m for m in result["predicted_per_mode_t60_reduction_pct"]
            if 65 <= m["freq_hz"] <= 75]
    assert near, result["predicted_per_mode_t60_reduction_pct"]
    strengths = near[0]["per_sub_strength"]
    # Order matches per_sub_specs order.
    assert strengths[0] == pytest.approx(0.7, abs=1e-3)
    assert strengths[1] == pytest.approx(0.3, abs=1e-3)


@pytest.mark.asyncio
async def test_does_not_apply_to_hardware() -> None:
    """simulate must NOT touch hardware. Mock dsp + apply_fir + set_output_gain
    and assert nothing was called."""
    fa = load_session_fixture("sub_fr_solo_post_routing")
    fb = load_session_fixture("sub_nf_solo_post_routing")
    sa = _session_from_fixture(fa, override_id=911)
    sb = _session_from_fixture(fb, override_id=912)

    specs = [
        {
            "session_id": 911, "output_index": 5,
            "intents": _two_intents_for(fa),
            "num_taps": 4096, "samplerate": 48000,
        },
        {
            "session_id": 912, "output_index": 6,
            "intents": _two_intents_for(fb),
            "num_taps": 4096, "samplerate": 48000,
        },
    ]

    with (
        patch("calibrate.storage.SessionStore") as MockStore,
        patch("calibrate.mcp_server._tool_apply_fir") as mock_apply_fir,
        patch("calibrate.mcp_server._tool_set_output_gain") as mock_set_gain,
        patch("calibrate.mcp_server._tool_apply_eq") as mock_apply_eq,
    ):
        _wire_store_mock(MockStore, sa, sb)
        result = await _tool_simulate_per_sub_fir(per_sub_specs=specs)

    assert result["ok"], result
    mock_apply_fir.assert_not_called()
    mock_set_gain.assert_not_called()
    mock_apply_eq.assert_not_called()


@pytest.mark.asyncio
async def test_reuses_modal_aware_fir_designer() -> None:
    """No parallel implementation: simulate routes through
    ModalAwareFIRDesigner.design()."""
    fa = load_session_fixture("sub_fr_solo_post_routing")
    fb = load_session_fixture("sub_nf_solo_post_routing")
    sa = _session_from_fixture(fa, override_id=911)
    sb = _session_from_fixture(fb, override_id=912)

    specs = [
        {
            "session_id": 911, "output_index": 5,
            "intents": _two_intents_for(fa),
            "num_taps": 4096, "samplerate": 48000,
        },
        {
            "session_id": 912, "output_index": 6,
            "intents": _two_intents_for(fb),
            "num_taps": 4096, "samplerate": 48000,
        },
    ]

    with (
        patch("calibrate.storage.SessionStore") as MockStore,
        patch(
            "calibrate.modal_fir.ModalAwareFIRDesigner.design",
            autospec=True,
        ) as mock_design,
    ):
        _wire_store_mock(MockStore, sa, sb)
        # Build a realistic return: passthrough impulse + summary.
        from calibrate.modal_fir import DesignSummary
        coeffs = [0.0] * 4096
        coeffs[0] = 1.0
        summary = DesignSummary(
            total_taps=4096, sample_rate=48000, pre_delay_ms=0.0,
            pre_delay_samples=0, peak_amplitude=1.0,
        )
        mock_design.return_value = (coeffs, summary)
        result = await _tool_simulate_per_sub_fir(per_sub_specs=specs)

    assert result["ok"], result
    # One design call per sub.
    assert mock_design.call_count == 2
