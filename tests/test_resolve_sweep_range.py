"""Tests for `_resolve_sweep_range` — picks the right sweep band per target.

Resolution order:
  1. Explicit freq_min / freq_max (any one of them, partial overrides supported)
  2. target → speaker config sweep_range_hz lookup
  3. None / None (engine falls back to measurement.freq_min/freq_max)
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from calibrate.mcp_server import _resolve_sweep_range


def _cfg(speakers=None, sub=None):
    """Build a tiny config-like object with .speakers and .sub."""
    return SimpleNamespace(
        speakers=speakers or [],
        sub=sub or {},
    )


# ── Explicit overrides ─────────────────────────────────────────────────


def test_explicit_min_max_pass_through() -> None:
    lo, hi, source = _resolve_sweep_range(
        _cfg(), target=None, freq_min=100, freq_max=5000,
    )
    assert lo == 100 and hi == 5000
    assert "explicit" in source


def test_explicit_partial_min_only_fills_max_from_target() -> None:
    """freq_min=300 + target='subs' → caller's 300 wins, max from sub config."""
    lo, hi, source = _resolve_sweep_range(
        _cfg(sub={"sweep_range_hz": [15, 150]}),
        target="subs",
        freq_min=300,
        freq_max=None,
    )
    assert lo == 300  # explicit wins
    assert hi == 150  # from sub config


def test_explicit_partial_max_only_fills_min_from_target() -> None:
    lo, hi, _ = _resolve_sweep_range(
        _cfg(sub={"sweep_range_hz": [15, 150]}),
        target="subs",
        freq_min=None,
        freq_max=80,
    )
    assert lo == 15
    assert hi == 80


# ── Target-based resolution ────────────────────────────────────────────


def test_target_subs_resolves_from_sub_config() -> None:
    lo, hi, source = _resolve_sweep_range(
        _cfg(sub={"sweep_range_hz": [15, 150]}),
        target="subs",
        freq_min=None,
        freq_max=None,
    )
    assert lo == 15 and hi == 150
    assert "speaker_config" in source


def test_target_sub_singular_also_resolves() -> None:
    lo, hi, _ = _resolve_sweep_range(
        _cfg(sub={"sweep_range_hz": [15, 150]}),
        target="sub",
        freq_min=None,
        freq_max=None,
    )
    assert (lo, hi) == (15, 150)


def test_target_LFE_treated_as_sub() -> None:
    lo, hi, _ = _resolve_sweep_range(
        _cfg(sub={"sweep_range_hz": [15, 150]}),
        target="LFE",
        freq_min=None,
        freq_max=None,
    )
    assert (lo, hi) == (15, 150)


def test_target_mains_resolves_from_main_speaker() -> None:
    speakers = [
        {"model": "Chane A2.4", "type": "main",
         "positions": ["L", "C", "R"], "sweep_range_hz": [60, 20000]},
        {"model": "Chane A1.4", "type": "surround",
         "positions": ["SL", "SR"], "sweep_range_hz": [80, 20000]},
    ]
    lo, hi, source = _resolve_sweep_range(
        _cfg(speakers=speakers),
        target="mains",
        freq_min=None,
        freq_max=None,
    )
    assert lo == 60 and hi == 20000


def test_target_position_FL_resolves_to_main_speaker() -> None:
    speakers = [
        {"model": "Chane A2.4", "type": "main",
         "positions": ["L", "C", "R"], "sweep_range_hz": [60, 20000]},
    ]
    # "FL" doesn't match positions exactly (we'd need "L"), but it's a common
    # alias the resolver should support if positions list it. Test with "L"
    # and "C" specifically.
    lo, hi, _ = _resolve_sweep_range(
        _cfg(speakers=speakers), target="C",
        freq_min=None, freq_max=None,
    )
    assert (lo, hi) == (60, 20000)


def test_target_atmos_resolves_to_atmos_speaker() -> None:
    speakers = [
        {"model": "Chane A2.4", "type": "main",
         "positions": ["L", "R"], "sweep_range_hz": [60, 20000]},
        {"model": "Polk VT60", "type": "atmos",
         "positions": ["TFL", "TFR"], "sweep_range_hz": [80, 20000]},
    ]
    lo, hi, _ = _resolve_sweep_range(
        _cfg(speakers=speakers), target="atmos",
        freq_min=None, freq_max=None,
    )
    assert (lo, hi) == (80, 20000)


def test_target_height_alias_for_atmos() -> None:
    speakers = [
        {"type": "atmos", "positions": ["TFL"], "sweep_range_hz": [80, 20000]},
    ]
    lo, hi, _ = _resolve_sweep_range(
        _cfg(speakers=speakers), target="heights",
        freq_min=None, freq_max=None,
    )
    assert (lo, hi) == (80, 20000)


# ── Fallback when no override + no target ─────────────────────────────


def test_no_target_no_explicit_returns_none() -> None:
    """Engine uses config defaults when both are None."""
    lo, hi, source = _resolve_sweep_range(
        _cfg(), target=None, freq_min=None, freq_max=None,
    )
    assert lo is None and hi is None
    assert "default" in source


def test_target_with_no_speakers_in_config_returns_none() -> None:
    """Unknown target → fall through to defaults rather than raising."""
    lo, hi, _ = _resolve_sweep_range(
        _cfg(), target="surround", freq_min=None, freq_max=None,
    )
    assert lo is None and hi is None


def test_target_speaker_without_sweep_range_returns_none() -> None:
    """Speaker config exists but lacks sweep_range_hz — fall through."""
    speakers = [
        {"model": "Test", "type": "main", "positions": ["L"]},
        # No sweep_range_hz
    ]
    lo, hi, _ = _resolve_sweep_range(
        _cfg(speakers=speakers), target="mains",
        freq_min=None, freq_max=None,
    )
    assert lo is None and hi is None


# ── Source string for diagnostics ──────────────────────────────────────


def test_source_describes_explicit_overrides() -> None:
    _, _, source = _resolve_sweep_range(
        _cfg(), target=None, freq_min=100, freq_max=200,
    )
    assert source == "explicit"


def test_source_describes_target_resolution() -> None:
    _, _, source = _resolve_sweep_range(
        _cfg(sub={"sweep_range_hz": [15, 150]}),
        target="subs", freq_min=None, freq_max=None,
    )
    assert "speaker_config" in source
    assert "subs" in source


def test_source_describes_combined() -> None:
    """When explicit fills one and target fills the other, source notes both."""
    _, _, source = _resolve_sweep_range(
        _cfg(sub={"sweep_range_hz": [15, 150]}),
        target="subs", freq_min=300, freq_max=None,
    )
    assert "explicit" in source
    assert "speaker_config" in source
