"""Tests for calibrate.graph — signal-graph data model, legacy shim, composer."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from calibrate.config import Config, DEFAULT_CONFIG
from calibrate.graph import (
    Processor,
    SignalGraph,
    Source,
    SVS_PB12_NSD_PROFILE,
    Transducer,
    TransducerGroup,
    TransducerProfile,
)


# ── Dataclass + construction ──────────────────────────────────────────────────


def test_signal_graph_is_empty_by_default() -> None:
    g = SignalGraph()
    assert g.sources == ()
    assert g.processors == ()
    assert g.transducers == ()
    assert g.profiles == ()
    assert g.groups == ()


def test_signal_graph_from_dict_parses_full_yaml() -> None:
    data = {
        "sources": [
            {"name": "game", "type": "avr_input", "avr_input_ref": "GAME2"},
            {"name": "usb_sweep", "type": "usb_direct"},
        ],
        "processors": [
            {"name": "denon", "driver_ref": "denon", "kind": "avr"},
            {"name": "camilla", "driver_ref": "camilladsp", "kind": "dsp",
             "outputs": ["0", "1", "2"]},
        ],
        "transducer_profiles": [
            {"name": "svs", "max_boost_per_band_db": 6, "hpf": {"freq": 18, "order": 4}},
            {"name": "main", "max_boost_per_band_db": 3, "hpf": None},
        ],
        "transducers": [
            {"name": "sub_l", "role": "sub", "processor_ref": "camilla",
             "output_index": 0, "safety_profile_ref": "svs", "position": "front_left"},
            {"name": "l_main", "role": "main", "processor_ref": "camilla",
             "output_index": 2, "safety_profile_ref": "main"},
        ],
        "groups": [
            {"name": "bass", "members": ["sub_l"]},
            {"name": "front", "members": ["l_main", "sub_l"]},
        ],
    }
    g = SignalGraph.from_dict(data)
    assert len(g.sources) == 2
    assert g.source_by_name("game").avr_input_ref == "GAME2"
    assert g.processor_by_name("camilla").outputs == ("0", "1", "2")
    assert g.transducer_by_name("sub_l").position == "front_left"
    assert g.transducer_by_name("l_main").safety_profile_ref == "main"
    assert g.profile_by_name("main").hpf_freq_hz is None
    assert g.group_by_name("bass").members == ("sub_l",)


def test_signal_graph_is_frozen_so_lookups_are_stable() -> None:
    g = SignalGraph(
        sources=(Source(name="s", type="usb_direct"),),
    )
    with pytest.raises(Exception):  # FrozenInstanceError
        g.sources = ()  # type: ignore[misc]


# ── Legacy shim ───────────────────────────────────────────────────────────────


def test_from_legacy_synthesises_avr_and_dsp_from_default_config() -> None:
    cfg = Config(DEFAULT_CONFIG.copy())
    g = SignalGraph.from_legacy(cfg)
    proc_names = {(p.name, p.kind) for p in g.processors}
    # Default config has avr_driver=denon, dsp_driver=minidsp — both synthesised.
    assert ("denon", "avr") in proc_names
    assert ("minidsp", "dsp") in proc_names


def test_from_legacy_creates_sub_transducers_and_bass_group() -> None:
    cfg = Config(DEFAULT_CONFIG.copy())
    g = SignalGraph.from_legacy(cfg)
    subs = g.transducers_by_role("sub")
    assert len(subs) == 2
    assert [t.output_index for t in subs] == [0, 1]
    # All subs land in a "bass" group.
    bass = g.group_by_name("bass")
    assert bass is not None
    assert set(bass.members) == {t.name for t in subs}


def test_from_legacy_attaches_svs_profile_to_subs() -> None:
    cfg = Config(DEFAULT_CONFIG.copy())
    g = SignalGraph.from_legacy(cfg)
    sub = g.transducers_by_role("sub")[0]
    profile = g.profile_for(sub)
    assert profile.name == SVS_PB12_NSD_PROFILE.name


def test_from_legacy_honours_shaker_slot_type() -> None:
    data = DEFAULT_CONFIG.copy()
    data["minidsp"] = {**DEFAULT_CONFIG["minidsp"]}
    data["minidsp"]["output_slots"] = [
        {"index": 0, "label": "", "type": "sub"},
        {"index": 1, "label": "", "type": "sub"},
        {"index": 2, "label": "couch", "type": "shaker"},
        {"index": 3, "label": "", "type": "unused"},
    ]
    cfg = Config(data)
    g = SignalGraph.from_legacy(cfg)
    shakers = g.transducers_by_role("shaker")
    assert len(shakers) == 1
    assert shakers[0].output_index == 2


def test_config_signal_graph_property_uses_legacy_shim_when_absent() -> None:
    """Config.signal_graph returns a synthesised graph when no explicit block."""
    cfg = Config(DEFAULT_CONFIG.copy())
    g = cfg.signal_graph
    assert isinstance(g, SignalGraph)
    assert any(p.kind == "dsp" for p in g.processors)


def test_config_signal_graph_property_parses_explicit_block() -> None:
    data = DEFAULT_CONFIG.copy()
    data["signal_graph"] = {
        "processors": [
            {"name": "my_dsp", "driver_ref": "minidsp", "kind": "dsp"},
        ],
        "transducers": [
            {"name": "left_sub", "role": "sub", "processor_ref": "my_dsp",
             "output_index": 0, "safety_profile_ref": "svs_pb12_nsd"},
        ],
    }
    cfg = Config(data)
    g = cfg.signal_graph
    # Explicit block wins — processor names come from the YAML, not the legacy shim.
    assert [p.name for p in g.processors] == ["my_dsp"]
    assert g.transducer_by_name("left_sub") is not None


# ── Target resolution ────────────────────────────────────────────────────────


def _build_fixture_graph() -> SignalGraph:
    return SignalGraph(
        sources=(
            Source(name="game", type="avr_input", avr_input_ref="GAME2"),
            Source(name="usb", type="usb_direct"),
        ),
        processors=(
            Processor(name="denon", driver_ref="denon", kind="avr"),
            Processor(name="dsp", driver_ref="minidsp", kind="dsp"),
        ),
        profiles=(
            SVS_PB12_NSD_PROFILE,
            TransducerProfile(name="main_tight", max_boost_per_band_db=3.0,
                              min_boost_freq_hz=40.0, hpf_freq_hz=None,
                              max_cumulative_boost_db=5.0),
        ),
        transducers=(
            Transducer(name="sub_l", role="sub", processor_ref="dsp",
                       output_index=0, safety_profile_ref="svs_pb12_nsd"),
            Transducer(name="sub_r", role="sub", processor_ref="dsp",
                       output_index=1, safety_profile_ref="svs_pb12_nsd"),
            Transducer(name="l_main", role="main", processor_ref="dsp",
                       output_index=2, safety_profile_ref="main_tight"),
        ),
        groups=(
            TransducerGroup(name="bass", members=("sub_l", "sub_r")),
            TransducerGroup(name="all", members=("sub_l", "sub_r", "l_main")),
        ),
    )


def test_resolve_target_prefers_group_over_role() -> None:
    g = _build_fixture_graph()
    # "bass" is both a role-adjacent name (matches no role literally) and a group.
    resolved = g.resolve_target("bass")
    assert [t.name for t in resolved] == ["sub_l", "sub_r"]


def test_resolve_target_falls_through_to_transducer_name() -> None:
    g = _build_fixture_graph()
    resolved = g.resolve_target("sub_l")
    assert [t.name for t in resolved] == ["sub_l"]


def test_resolve_target_falls_through_to_role() -> None:
    g = _build_fixture_graph()
    resolved = g.resolve_target("sub")
    assert {t.name for t in resolved} == {"sub_l", "sub_r"}


def test_resolve_target_unknown_returns_empty() -> None:
    g = _build_fixture_graph()
    assert g.resolve_target("nonexistent") == ()


def test_resolve_target_group_skips_missing_members_silently() -> None:
    g = SignalGraph(
        transducers=(
            Transducer(name="t1", role="x", processor_ref="p",
                       output_index=0, safety_profile_ref="svs"),
        ),
        groups=(
            TransducerGroup(name="mixed", members=("t1", "missing")),
        ),
    )
    resolved = g.resolve_target("mixed")
    assert [t.name for t in resolved] == ["t1"]


# ── Profile lookup + strictness ──────────────────────────────────────────────


def test_profile_for_returns_default_when_profile_name_unknown() -> None:
    g = SignalGraph(
        transducers=(
            Transducer(name="orphan", role="sub", processor_ref="p",
                       output_index=0, safety_profile_ref="not_registered"),
        ),
    )
    profile = g.profile_for(g.transducer_by_name("orphan"))
    # Falls back to built-in SVS — caller always gets a usable profile.
    assert profile.name == SVS_PB12_NSD_PROFILE.name


def test_strictest_profile_picks_tightest_boost_and_highest_freq_floor() -> None:
    g = _build_fixture_graph()
    # Include both a sub (svs: 6 dB per band, 25 Hz floor) and a main (3 dB, 40 Hz).
    mixed = g.resolve_target("all")
    strict = g.strictest_profile(mixed)
    # Strictest per-band is the main's 3 dB, strictest freq floor is 40 Hz.
    assert strict.max_boost_per_band_db == 3.0
    assert strict.min_boost_freq_hz == 40.0
    # HPF collapses: one has 18 Hz, one has None → min of the non-None = 18 Hz.
    assert strict.hpf_freq_hz == 18.0


def test_strictest_profile_empty_set_returns_default() -> None:
    g = _build_fixture_graph()
    assert g.strictest_profile(()).name == SVS_PB12_NSD_PROFILE.name


# ── Sweep-context composition ────────────────────────────────────────────────


class _FakeSweepCtx:
    """Records enter/exit order for composition tests."""

    def __init__(self, name: str, events: list[str]) -> None:
        self.name = name
        self._events = events

    async def __aenter__(self):
        self._events.append(f"enter:{self.name}")
        return self

    async def __aexit__(self, *_):
        self._events.append(f"exit:{self.name}")


class _FakeDriver:
    def __init__(self, name: str, events: list[str]) -> None:
        self.name = name
        self._events = events

    def sweep_context(self, _config):
        return _FakeSweepCtx(self.name, self._events)


class _FakeRegistry:
    def __init__(self, drivers: dict) -> None:
        self._drivers = drivers

    def get(self, name: str):
        return self._drivers.get(name)


def test_processors_on_path_includes_avr_only_for_avr_source() -> None:
    g = _build_fixture_graph()
    subs = g.resolve_target("bass")
    # AVR-input source → AVR first, then DSP
    path = g.processors_on_path("game", subs)
    assert [p.name for p in path] == ["denon", "dsp"]
    # USB direct source → DSP only (AVR bypassed)
    path = g.processors_on_path("usb", subs)
    assert [p.name for p in path] == ["dsp"]


def test_sweep_context_enters_and_exits_in_source_order() -> None:
    g = _build_fixture_graph()
    subs = g.resolve_target("bass")
    events: list[str] = []
    registry = _FakeRegistry({
        "denon": _FakeDriver("denon", events),
        "dsp": _FakeDriver("dsp", events),
    })

    async def run():
        async with g.sweep_context("game", subs, None, registry):
            pass

    asyncio.run(run())
    # AVR enters first, DSP enters second; exits unwind in reverse order.
    assert events == ["enter:denon", "enter:dsp", "exit:dsp", "exit:denon"]


def test_sweep_context_skips_drivers_returning_none() -> None:
    g = _build_fixture_graph()
    subs = g.resolve_target("bass")
    events: list[str] = []

    class _NoopDriver:
        def sweep_context(self, _):
            return None  # some drivers don't need sweep-time setup

    registry = _FakeRegistry({
        "denon": _NoopDriver(),
        "dsp": _FakeDriver("dsp", events),
    })

    async def run():
        async with g.sweep_context("game", subs, None, registry):
            pass

    asyncio.run(run())
    assert events == ["enter:dsp", "exit:dsp"]


def test_sweep_context_with_empty_path_is_noop() -> None:
    g = SignalGraph()  # no processors, no transducers
    registry = _FakeRegistry({})

    async def run():
        async with g.sweep_context("nonexistent_source", (), None, registry):
            pass

    asyncio.run(run())  # must not raise


# ── Summary for MCP ──────────────────────────────────────────────────────────


def test_summary_has_all_node_types_for_llm() -> None:
    g = _build_fixture_graph()
    s = g.summary()
    assert set(s.keys()) == {
        "sources", "processors", "transducers", "profiles", "groups",
    }
    assert len(s["transducers"]) == 3
    assert len(s["groups"]) == 2
    # Transducer summary carries the fields a recipe needs to target correctly.
    t0 = s["transducers"][0]
    assert set(t0.keys()) == {
        "name", "role", "processor", "output_index", "profile", "position",
    }


# ── SafetyValidator integration with profiles ─────────────────────────────────


def test_safety_validator_honours_profile_min_boost_freq() -> None:
    """A tweeter profile with min_boost_freq_hz=3000 rejects a 50 Hz boost."""
    from calibrate.safety import FilterSpec, SafetyValidator

    profile = TransducerProfile(
        name="tweeter", min_boost_freq_hz=3000.0,
        max_boost_per_band_db=3.0, hpf_freq_hz=None,
    )
    validator = SafetyValidator(profile)
    result = validator.validate(
        [FilterSpec(freq=50.0, gain_db=2.0, q=1.0, type="peaking")]
    )
    assert not result.ok
    assert "3000 Hz" in result.error


def test_safety_validator_skips_hpf_when_profile_hpf_none() -> None:
    """A profile without an HPF requirement doesn't force one on the filter set."""
    from calibrate.safety import FilterSpec, SafetyValidator

    profile = TransducerProfile(
        name="main", hpf_freq_hz=None, max_boost_per_band_db=3.0,
    )
    validator = SafetyValidator(profile)
    # No HPF in the filter set — SVS default would reject; this profile accepts.
    result = validator.validate(
        [FilterSpec(freq=100.0, gain_db=-1.0, q=1.0, type="peaking")]
    )
    assert result.ok


def test_safety_validator_with_default_profile_matches_legacy_behaviour() -> None:
    """Bare SafetyValidator() uses SVS defaults — existing tests keep passing."""
    from calibrate.safety import FilterSpec, SafetyValidator

    validator = SafetyValidator()
    assert validator.profile.name == SVS_PB12_NSD_PROFILE.name
    # The classic mandatory-HPF rule still fires.
    result = validator.validate(
        [FilterSpec(freq=50.0, gain_db=-1.0, q=1.0, type="peaking")]
    )
    assert not result.ok
    assert "HPF" in result.error
