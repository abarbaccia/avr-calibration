"""Tests for the target-driven measurement chain resolver.

Sub and shaker measurements now use the HDMI → Denon → LFE pre-out → Scarlett
→ CamillaDSP path (same as listening mode). No loopback, no mode switching.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from calibrate.measurement_profiles import (
    DEFAULT_MEASUREMENT_PROFILES,
    resolve_measurement_chain,
)


def _config(playback_route="usb", profiles=None, signal_graph=None):
    cfg = MagicMock()
    cfg.measurement = {
        "playback_route": playback_route,
        "freq_min": 20,
        "freq_max": 200,
    }
    cfg.signal_graph = signal_graph
    cfg._data = {"measurement_profiles": profiles} if profiles else {}
    return cfg


def _signal_graph(transducers=None, groups=None):
    return SimpleNamespace(
        transducers=transducers or [],
        groups=groups or [],
        transducers_by_role=lambda role: [t for t in (transducers or []) if t.role == role],
    )


def _transducer(name, role, processor="camilla", overrides=None):
    return SimpleNamespace(
        name=name,
        role=role,
        processor=processor,
        measurement_overrides=overrides,
        raw={"measurement_overrides": overrides} if overrides else {},
    )


# ── Resolution paths ─────────────────────────────────────────────────────


def test_target_subs_resolves_to_hdmi_direct():
    """target='subs' → role=sub → route=hdmi, sound_mode=DIRECT, sweep_channel=LFE."""
    sub_fr = _transducer("sub_front_right", "sub")
    sub_nf = _transducer("sub_nearfield", "sub")
    grp = SimpleNamespace(name="subs", members=["sub_front_right", "sub_nearfield"])
    graph = _signal_graph(transducers=[sub_fr, sub_nf], groups=[grp])
    cfg = _config(signal_graph=graph)

    chain = resolve_measurement_chain("subs", None, cfg)

    assert chain.role == "sub"
    assert chain.route == "hdmi"
    assert chain.sound_mode == "DIRECT"
    assert chain.sweep_channel == "LFE"
    assert chain.legacy_path is False


def test_target_transducer_name_resolves_via_role():
    """target='sub_front_right' → finds transducer → role=sub → hdmi chain."""
    sub_fr = _transducer("sub_front_right", "sub")
    graph = _signal_graph(transducers=[sub_fr])
    cfg = _config(signal_graph=graph)

    chain = resolve_measurement_chain("sub_front_right", None, cfg)

    assert chain.role == "sub"
    assert chain.route == "hdmi"
    assert chain.sound_mode == "DIRECT"


def test_target_main_resolves_to_hdmi_pure_direct():
    """target='mains' → route=hdmi, sound_mode=PURE DIRECT."""
    fl = _transducer("FL", "main", processor="avr")
    grp = SimpleNamespace(name="mains", members=["FL"])
    graph = _signal_graph(transducers=[fl], groups=[grp])
    cfg = _config(signal_graph=graph)

    chain = resolve_measurement_chain("mains", None, cfg)

    assert chain.role == "main"
    assert chain.route == "hdmi"
    assert chain.sound_mode == "PURE DIRECT"


def test_target_lfe_pre_resolves_to_hdmi_direct():
    """sweep_channel=LFE → lfe_pre role → hdmi, DIRECT, LFE channel."""
    cfg = _config()
    cfg.signal_graph = None
    chain = resolve_measurement_chain(None, "LFE", cfg)
    assert chain.role == "lfe_pre"
    assert chain.route == "hdmi"
    assert chain.sound_mode == "DIRECT"
    assert chain.sweep_channel == "LFE"


def test_sweep_channel_forces_hdmi_regardless_of_target():
    """Per-channel sweep (sweep_channel='FL') always overrides to HDMI."""
    cfg = _config(playback_route="usb")
    chain = resolve_measurement_chain("subs", "FL", cfg)
    assert chain.route == "hdmi"
    assert chain.role == "main"
    assert chain.sweep_channel == "FL"


def test_sweep_channel_lfe_resolves_to_lfe_pre_role():
    """sweep_channel='LFE' resolves to lfe_pre role."""
    cfg = _config()
    chain = resolve_measurement_chain(None, "LFE", cfg)
    assert chain.role == "lfe_pre"
    assert chain.route == "hdmi"
    assert chain.sound_mode == "DIRECT"


def test_legacy_fallback_when_no_role_matches():
    """target=None, no signal_graph, no profile → legacy playback_route default."""
    cfg = _config(playback_route="usb")
    cfg.signal_graph = None
    chain = resolve_measurement_chain(None, None, cfg)
    assert chain.legacy_path is True
    assert chain.route == "usb"


def test_legacy_role_aliases():
    """Common aliases ('subs', 'bass', 'mains') resolve even without a graph."""
    cfg = _config()
    cfg.signal_graph = None
    assert resolve_measurement_chain("subs", None, cfg).role == "sub"
    assert resolve_measurement_chain("bass", None, cfg).role == "sub"
    assert resolve_measurement_chain("mains", None, cfg).role == "main"


def test_shaker_resolves_to_hdmi_direct():
    """Shaker role uses the same HDMI → LFE path as subs."""
    shaker = _transducer("shaker_mqb1", "shaker")
    graph = _signal_graph(transducers=[shaker])
    cfg = _config(signal_graph=graph)
    chain = resolve_measurement_chain("shaker_mqb1", None, cfg)
    assert chain.role == "shaker"
    assert chain.route == "hdmi"
    assert chain.sound_mode == "DIRECT"
    assert chain.sweep_channel == "LFE"


# ── User profile overrides ───────────────────────────────────────────────


def test_user_profile_deep_merges_with_defaults():
    """User can override one field of a built-in profile without restating everything else."""
    sub_t = _transducer("sub_x", "sub")
    graph = _signal_graph(transducers=[sub_t])
    user_profiles = {
        "sub": {
            "master_gain_db": -8.0,
        }
    }
    cfg = _config(signal_graph=graph, profiles=user_profiles)
    chain = resolve_measurement_chain("sub_x", None, cfg)

    assert chain.master_gain_db == -8.0
    assert chain.route == "hdmi"
    assert chain.sound_mode == "DIRECT"


def test_per_processor_sub_profile():
    """A 'by_processor' nested map lets multi-DSP setups specify different chain params."""
    sub_t = _transducer("sub_aux", "sub", processor="minidsp_aux")
    graph = _signal_graph(transducers=[sub_t])
    user_profiles = {
        "sub": {
            "by_processor": {
                "minidsp_aux": {
                    "sweep_volume_db": -25.0,
                }
            }
        }
    }
    cfg = _config(signal_graph=graph, profiles=user_profiles)
    chain = resolve_measurement_chain("sub_aux", None, cfg)
    assert chain.sweep_volume_db == -25.0
    assert chain.route == "hdmi"


def test_per_transducer_overrides_beat_role_and_processor():
    """A specific transducer's measurement_overrides win over both role default and by_processor."""
    sub_t = _transducer("sub_quiet", "sub", overrides={"master_gain_db": -5.0})
    graph = _signal_graph(transducers=[sub_t])
    cfg = _config(signal_graph=graph)
    chain = resolve_measurement_chain("sub_quiet", None, cfg)
    assert chain.master_gain_db == -5.0


# ── Built-in profile sanity ──────────────────────────────────────────────


def test_default_profiles_all_use_hdmi():
    """All default profiles use route=hdmi. No loopback paths remain."""
    for role, prof in DEFAULT_MEASUREMENT_PROFILES.items():
        assert prof["route"] == "hdmi", (
            f"Role {role}: expected route=hdmi, got {prof['route']!r}"
        )


def test_default_profiles_no_cal_mode():
    """Default profiles must not contain a cal_mode key (concept was removed)."""
    for role, prof in DEFAULT_MEASUREMENT_PROFILES.items():
        assert "cal_mode" not in prof, (
            f"Role {role}: unexpected cal_mode key"
        )


def test_sub_and_lfe_pre_share_hdmi_lfe_path():
    """sub and lfe_pre profiles both use HDMI + DIRECT + LFE sweep channel."""
    sub = DEFAULT_MEASUREMENT_PROFILES["sub"]
    lfe_pre = DEFAULT_MEASUREMENT_PROFILES["lfe_pre"]
    for prof, name in [(sub, "sub"), (lfe_pre, "lfe_pre")]:
        assert prof["route"] == "hdmi", f"{name}: wrong route"
        assert prof["sound_mode"] == "DIRECT", f"{name}: wrong sound_mode"
        assert prof["sweep_channel"] == "LFE", f"{name}: wrong sweep_channel"
