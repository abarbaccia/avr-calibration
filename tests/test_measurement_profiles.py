"""Tests for the target-driven measurement chain resolver.

The resolver is the structural fix for the 2026-05-06 'wrong route' bug —
it makes target authoritative so sub measurements always use cal-mode/USB
and mains measurements always use HDMI/AVR, regardless of any global
config defaults.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from calibrate.measurement_profiles import (
    DEFAULT_MEASUREMENT_PROFILES,
    chain_forbids_cal_mode,
    chain_requires_cal_mode,
    resolve_measurement_chain,
)


def _config(playback_route="usb", profiles=None, signal_graph=None):
    """Minimal config stub. Mirrors the .measurement / .signal_graph /
    ._data accessors the resolver reads."""
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
    """Minimal signal graph stub. Transducers each need .name, .role,
    .processor, .raw (or measurement_overrides attr)."""
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


def test_target_subs_role_resolves_to_usb_cal_mode():
    """target='subs' (group name) → role=sub → route=usb, cal_mode=required."""
    sub_fr = _transducer("sub_front_right", "sub")
    sub_nf = _transducer("sub_nearfield", "sub")
    grp = SimpleNamespace(name="subs", members=["sub_front_right", "sub_nearfield"])
    graph = _signal_graph(transducers=[sub_fr, sub_nf], groups=[grp])
    cfg = _config(playback_route="hdmi", signal_graph=graph)  # global default WRONG

    chain = resolve_measurement_chain("subs", None, cfg)

    assert chain.role == "sub"
    assert chain.route == "usb"
    assert chain.cal_mode is not None
    assert chain.cal_mode["enabled"] == "required"
    assert chain_requires_cal_mode(chain) is True
    assert chain_forbids_cal_mode(chain) is False
    assert chain.legacy_path is False


def test_target_transducer_name_resolves_via_role():
    """target='sub_front_right' → finds transducer → role=sub → usb chain."""
    sub_fr = _transducer("sub_front_right", "sub")
    graph = _signal_graph(transducers=[sub_fr])
    cfg = _config(signal_graph=graph)

    chain = resolve_measurement_chain("sub_front_right", None, cfg)

    assert chain.role == "sub"
    assert chain.route == "usb"
    assert chain_requires_cal_mode(chain) is True


def test_target_main_resolves_to_hdmi_pure_direct():
    """target='mains' → route=hdmi, sound_mode=PURE DIRECT. cal_mode is
    irrelevant for pure mains (DSP not in chain)."""
    fl = _transducer("FL", "main", processor="avr")
    grp = SimpleNamespace(name="mains", members=["FL"])
    graph = _signal_graph(transducers=[fl], groups=[grp])
    cfg = _config(signal_graph=graph)

    chain = resolve_measurement_chain("mains", None, cfg)

    assert chain.role == "main"
    assert chain.route == "hdmi"
    assert chain.sound_mode == "PURE DIRECT"
    assert chain_requires_cal_mode(chain) is False
    assert chain_forbids_cal_mode(chain) is False  # cal_mode irrelevant for PURE DIRECT


def test_target_lfe_pre_forbids_cal_mode():
    """lfe_pre route goes through CamillaDSP (AVR LFE → Focusrite → DSP),
    so DSP must be in live mode. cal_mode=forbidden."""
    cfg = _config()
    cfg.signal_graph = None
    chain = resolve_measurement_chain(None, "LFE", cfg)
    assert chain.role == "lfe_pre"
    assert chain_forbids_cal_mode(chain) is True


def test_sweep_channel_forces_hdmi_regardless_of_target():
    """Per-channel sweep (sweep_channel='FL') always overrides to HDMI,
    even if target would have implied USB. Mirrors the legacy
    auto-route foot-gun fix."""
    cfg = _config(playback_route="usb")  # default would be USB
    chain = resolve_measurement_chain("subs", "FL", cfg)
    assert chain.route == "hdmi"
    assert chain.role == "main"  # sweep_channel='FL' is a main
    assert chain.sweep_channel == "FL"


def test_sweep_channel_lfe_resolves_to_lfe_pre_role():
    """sweep_channel='LFE' resolves to lfe_pre role (bass-mgmt-via-AVR)."""
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
    assert chain.cal_mode is None


def test_legacy_role_aliases():
    """Common aliases ('subs', 'bass', 'mains') resolve even without a graph."""
    cfg = _config()
    cfg.signal_graph = None
    assert resolve_measurement_chain("subs", None, cfg).role == "sub"
    assert resolve_measurement_chain("bass", None, cfg).role == "sub"
    assert resolve_measurement_chain("mains", None, cfg).role == "main"


# ── User profile overrides ───────────────────────────────────────────────


def test_user_profile_deep_merges_with_defaults():
    """User can override one field of a built-in profile without restating
    everything else."""
    sub_t = _transducer("sub_x", "sub")
    graph = _signal_graph(transducers=[sub_t])
    user_profiles = {
        "sub": {
            "master_gain_db": -8.0,  # only override this
            # cal_mode not set → defaults preserved
        }
    }
    cfg = _config(signal_graph=graph, profiles=user_profiles)
    chain = resolve_measurement_chain("sub_x", None, cfg)

    assert chain.master_gain_db == -8.0
    # Default still applied:
    assert chain.cal_mode["enabled"] == "required"
    assert chain.route == "usb"


def test_per_processor_sub_profile():
    """A 'by_processor' nested map lets multi-DSP setups specify different
    chain params per processor."""
    sub_t = _transducer("sub_aux", "sub", processor="minidsp_aux")
    graph = _signal_graph(transducers=[sub_t])
    user_profiles = {
        "sub": {
            "by_processor": {
                "minidsp_aux": {
                    "cal_mode": {"enabled": "optional"},
                    "sweep_volume_db": -25.0,
                }
            }
        }
    }
    cfg = _config(signal_graph=graph, profiles=user_profiles)
    chain = resolve_measurement_chain("sub_aux", None, cfg)
    assert chain.cal_mode["enabled"] == "optional"
    assert chain.sweep_volume_db == -25.0


def test_per_transducer_overrides_beat_role_and_processor():
    """A specific transducer's measurement_overrides win over both the role
    default and any by_processor sub-profile."""
    sub_t = _transducer(
        "sub_quiet", "sub", overrides={"master_gain_db": -5.0}
    )
    graph = _signal_graph(transducers=[sub_t])
    cfg = _config(signal_graph=graph)
    chain = resolve_measurement_chain("sub_quiet", None, cfg)
    assert chain.master_gain_db == -5.0


# ── Helper API ───────────────────────────────────────────────────────────


def test_chain_requires_cal_mode_returns_false_for_main():
    """Mains measurement must NOT require cal_mode."""
    cfg = _config()
    cfg.signal_graph = None
    chain = resolve_measurement_chain("mains", None, cfg)
    assert chain_requires_cal_mode(chain) is False


def test_chain_helpers_handle_none_cal_mode():
    """Profiles with cal_mode=None (mains, atmos, surround) return False from
    both helper predicates. cal_mode is irrelevant for those chains."""
    cfg = _config()
    cfg.signal_graph = None
    chain = resolve_measurement_chain("mains", None, cfg)
    assert chain.cal_mode is None
    assert chain_requires_cal_mode(chain) is False
    assert chain_forbids_cal_mode(chain) is False


# ── Built-in profile sanity ──────────────────────────────────────────────


def test_default_profiles_have_consistent_cal_mode_and_route():
    """Every default profile that says route=usb requires cal_mode. Roles
    with route=hdmi either set cal_mode=None (DSP not in chain) or
    cal_mode.enabled='forbidden' (DSP must be in live mode for the chain
    to work)."""
    for role, prof in DEFAULT_MEASUREMENT_PROFILES.items():
        cal = prof.get("cal_mode")
        if prof["route"] == "usb":
            assert cal is not None and cal.get("enabled") in ("required", "optional"), (
                f"Role {role}: route=usb but cal_mode={cal!r}"
            )
        elif prof["route"] == "hdmi":
            assert cal is None or cal.get("enabled") == "forbidden", (
                f"Role {role}: route=hdmi but cal_mode={cal!r}"
            )
