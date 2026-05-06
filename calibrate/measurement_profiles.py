"""Target-driven measurement chain resolution.

Maps a measurement *target* (a transducer name, group name, or role) to a
fully-resolved chain spec: route (usb/hdmi), cal_mode requirements, sound_mode,
sweep_channel, sweep volume, frequency range, validation checks.

The signal_graph + measurement_profiles in config.yaml are authoritative.
Hardware specifics live in config; the resolver code is hardware-agnostic.

This eliminates the 2026-05-06 class of bug where a global ``playback_route``
config sent sub measurements through HDMI to mains, silently producing
"successful" measurements of the wrong device.

Resolution order, highest priority first:
  1. ``sweep_channel`` set → force route='hdmi' (per-channel mains sweep)
  2. ``target`` resolves via signal_graph to a transducer or group → use
     the role's profile in ``measurement_profiles``
  3. Per-transducer ``measurement_overrides`` are deep-merged on top
  4. Per-processor sub-profiles (``by_processor``) override role defaults
  5. Fall back to legacy ``measurement.playback_route`` global

The output is a :class:`ChainSpec` consumed by the measurement engine.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


# Role profiles built into the codebase as the system default. Users can override
# any of these in config.yaml ``measurement_profiles:``. Absence of a role here
# means the resolver falls back to legacy ``measurement.playback_route``.
DEFAULT_MEASUREMENT_PROFILES: dict[str, dict] = {
    "sub": {
        "route": "usb",
        "cal_mode": {
            "enabled": "required",
            "capture_device": "hw:Loopback,1,0",
            "playback_device": "hw:Loopback,0,0",
            "channels": 2,
        },
        "sound_mode": None,
        "sweep_channel": None,
        "sweep_freq_min_hz": 15,
        "sweep_freq_max_hz": 200,
    },
    "main": {
        # Pure mains via PURE DIRECT: Pi → HDMI → AVR → speaker outs. The
        # DSP/Focusrite chain is not involved, so cal_mode is irrelevant.
        "route": "hdmi",
        "cal_mode": None,
        "sound_mode": "PURE DIRECT",
        "sweep_channel": "from_position",
        "sweep_freq_min_hz": 60,
        "sweep_freq_max_hz": 20000,
    },
    "atmos": {
        "route": "hdmi",
        "cal_mode": None,
        "sound_mode": "DOLBY SURROUND",
        "sweep_channel": "from_position",
        "sweep_freq_min_hz": 80,
        "sweep_freq_max_hz": 20000,
    },
    "surround": {
        "route": "hdmi",
        "cal_mode": None,
        "sound_mode": "DIRECT",
        "sweep_channel": "from_position",
        "sweep_freq_min_hz": 80,
        "sweep_freq_max_hz": 20000,
    },
    "lfe_pre": {
        # Mains-bass-mgmt path through AVR: Pi → HDMI → AVR → AVR sub-pre-out
        # → Focusrite IN3 → CamillaDSP → Focusrite OUT 5/6 → subs. This chain
        # GOES THROUGH the DSP, so the DSP must be in live mode (capturing
        # Focusrite directly, not snd-aloop). Hence cal_mode is forbidden.
        "route": "hdmi",
        "cal_mode": {"enabled": "forbidden"},
        "sound_mode": "DIRECT",
        "sweep_channel": "LFE",
        "sweep_freq_min_hz": 15,
        "sweep_freq_max_hz": 200,
    },
    "shaker": {
        # Tactile transducers; same chain as subs (Pi → cal-mode → DSP → shaker)
        "route": "usb",
        "cal_mode": {
            "enabled": "required",
            "capture_device": "hw:Loopback,1,0",
            "playback_device": "hw:Loopback,0,0",
            "channels": 2,
        },
        "sweep_freq_min_hz": 8,
        "sweep_freq_max_hz": 80,
    },
}


@dataclass
class ChainSpec:
    """Resolved measurement chain for a single sweep.

    Hardware-agnostic. Consumed by the measurement engine which translates
    each field into the appropriate driver actions.
    """

    role: str | None
    """The role used to resolve the profile (sub, main, atmos, surround,
    lfe_pre, shaker, or None when resolution fell back to legacy)."""

    route: str
    """Sweep injection path: 'usb' (Pi → snd-aloop → DSP) or 'hdmi'
    (Pi → AVR HDMI → mains/bass-mgmt). Fully determines the upstream chain."""

    cal_mode: dict | None
    """Cal-mode (input-side loopback) configuration. ``None`` means no
    cal-mode interaction (e.g., HDMI route). When set:
      ``enabled: required|forbidden|optional``,
      ``capture_device``: ALSA capture device the DSP should switch to,
      ``playback_device``: ALSA playback device measure() writes to,
      ``channels``: number of channels in the loopback subdevice,
      ``samplerate``: optional samplerate override."""

    sound_mode: str | None
    """AVR sound mode for the sweep: 'PURE DIRECT', 'DIRECT',
    'MULTI CH STEREO', 'DOLBY SURROUND', etc. ``None`` means don't touch
    the AVR (cal-mode/USB route)."""

    sweep_channel: str | int | None
    """HDMI channel for the sweep. String 'from_position' = derive from
    transducer's position field (FL→1, C→3, etc.). ``None`` = no HDMI
    channel (USB route)."""

    sweep_freq_min_hz: int | None
    """Lower frequency bound for the sweep."""

    sweep_freq_max_hz: int | None
    """Upper frequency bound for the sweep."""

    master_gain_db: float | None = None
    """CamillaDSP master gain to apply during measurement. ``None`` =
    don't touch."""

    sweep_volume_db: float | None = None
    """AVR volume override for the sweep. ``None`` = use config default."""

    pre_sweep_validation: list[dict] = field(default_factory=list)
    """Ordered list of validation checks the engine must pass before the
    sweep runs. Each entry: ``{check: <name>, ...check-specific args}``.
    Empty list = no validation gate (legacy behaviour)."""

    reference_loopback: dict | None = None
    """Optional output-side loopback (electrical IR-alignment anchor).
    ``None`` = mic-only IR alignment. When set: ``device``, ``pick_channel``,
    ``enabled: when_available|required|disabled``."""

    legacy_path: bool = False
    """True when resolver fell back to legacy ``measurement.playback_route``
    config (no profile matched). Engine should log a deprecation warning."""


def resolve_measurement_chain(
    target: str | None,
    sweep_channel_arg: str | None,
    config,
) -> ChainSpec:
    """Resolve a target + optional sweep_channel argument to a ChainSpec.

    See module docstring for resolution order. ``config`` is the parsed
    config object (with ``.measurement``, ``.signal_graph``,
    ``.measurement_profiles`` accessors).
    """
    profiles_user = _get_user_profiles(config)
    profiles = _merged_profiles(profiles_user)

    # Priority 1: explicit sweep_channel forces HDMI route (per-channel mains).
    # We still try to resolve a role for sweep_freq + sound_mode, falling back
    # to defaults if no role found.
    if sweep_channel_arg is not None:
        role = _role_for_sweep_channel(sweep_channel_arg)
        prof = profiles.get(role) or profiles.get("main") or {}
        return _profile_to_chain(
            prof, role=role, sweep_channel_override=sweep_channel_arg
        )

    # Priority 2: target → role via signal_graph
    role, transducer_overrides, processor_name = _resolve_role(target, config)

    if role and role in profiles:
        prof = dict(profiles[role])  # shallow copy

        # Priority 3: per-processor sub-profile overrides
        by_processor = prof.pop("by_processor", None) or {}
        if processor_name and processor_name in by_processor:
            prof = _deep_merge(prof, by_processor[processor_name])

        # Priority 4: per-transducer overrides (highest priority within the role)
        if transducer_overrides:
            prof = _deep_merge(prof, transducer_overrides)

        return _profile_to_chain(prof, role=role)

    # Priority 5: legacy fallback
    legacy_route = (config.measurement.get("playback_route") or "usb").lower()
    return ChainSpec(
        role=None,
        route=legacy_route,
        cal_mode=None,
        sound_mode=None,
        sweep_channel=None,
        sweep_freq_min_hz=config.measurement.get("freq_min"),
        sweep_freq_max_hz=config.measurement.get("freq_max"),
        legacy_path=True,
    )


def _get_user_profiles(config) -> dict:
    """Read ``measurement_profiles`` from config (may be missing)."""
    raw = getattr(config, "_data", {}).get("measurement_profiles") or {}
    if not isinstance(raw, dict):
        return {}
    return raw


def _merged_profiles(user_profiles: dict) -> dict:
    """Deep-merge user profiles over built-in defaults."""
    out = {}
    for role, default_prof in DEFAULT_MEASUREMENT_PROFILES.items():
        merged = dict(default_prof)
        if role in user_profiles:
            merged = _deep_merge(merged, user_profiles[role])
        out[role] = merged
    # Roles defined only by user (not in defaults) pass through verbatim
    for role, user_prof in user_profiles.items():
        if role not in out:
            out[role] = dict(user_prof)
    return out


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Deep-merge overlay into a copy of base. Overlay wins on conflicts."""
    out = dict(base)
    for key, val in overlay.items():
        if key in out and isinstance(out[key], dict) and isinstance(val, dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def _resolve_role(
    target: str | None, config
) -> tuple[str | None, dict | None, str | None]:
    """Resolve a target string to (role, transducer_overrides, processor_name).

    Looks up signal_graph for transducers and groups. A group resolves to its
    members' shared role (errors if mixed roles). A transducer resolves
    directly to its role + any per-transducer measurement_overrides.
    """
    if not target:
        return None, None, None

    graph = getattr(config, "signal_graph", None)
    if not graph:
        # Legacy fallback: accept role names directly
        if target in DEFAULT_MEASUREMENT_PROFILES:
            return target, None, None
        if target in {"subs", "bass"}:
            return "sub", None, None
        if target in {"mains"}:
            return "main", None, None
        return None, None, None

    # Try transducer name match
    for t in graph.transducers:
        if t.name == target:
            overrides = getattr(t, "measurement_overrides", None) or t.raw.get(
                "measurement_overrides"
            ) if hasattr(t, "raw") else None
            return t.role, overrides, t.processor

    # Try group name match → resolve to common role of members
    for group in graph.groups:
        if group.name == target:
            roles = {
                t.role
                for t in graph.transducers
                if t.name in group.members
            }
            if len(roles) == 1:
                role = next(iter(roles))
                # Group-level: don't apply per-transducer overrides; use role default
                return role, None, None
            # Mixed-role group → use the first role
            if roles:
                return next(iter(roles)), None, None

    # Try role name directly (e.g., target="sub")
    role_aliases = {
        "subs": "sub",
        "bass": "sub",
        "mains": "main",
        "shakers": "shaker",
        "tactile": "shaker",
        "atmos": "atmos",
        "surrounds": "surround",
    }
    canon = role_aliases.get(target, target)
    if canon in DEFAULT_MEASUREMENT_PROFILES:
        return canon, None, None

    return None, None, None


def _role_for_sweep_channel(sweep_channel: str) -> str | None:
    """Map a sweep_channel argument (FL, C, LFE, etc.) to a role."""
    sc_upper = (sweep_channel or "").upper()
    if sc_upper in {"FL", "FR", "L", "R", "C", "CENTER"}:
        return "main"
    if sc_upper in {"SL", "SR", "LS", "RS"}:
        return "surround"
    if sc_upper in {"TFL", "TFR", "TRL", "TRR"}:
        return "atmos"
    if sc_upper == "LFE":
        return "lfe_pre"
    return None


def _profile_to_chain(
    prof: dict,
    role: str | None,
    sweep_channel_override: str | None = None,
) -> ChainSpec:
    """Convert a resolved profile dict into a typed ChainSpec."""
    return ChainSpec(
        role=role,
        route=prof.get("route", "usb"),
        cal_mode=prof.get("cal_mode"),
        sound_mode=prof.get("sound_mode"),
        sweep_channel=sweep_channel_override or prof.get("sweep_channel"),
        sweep_freq_min_hz=prof.get("sweep_freq_min_hz"),
        sweep_freq_max_hz=prof.get("sweep_freq_max_hz"),
        master_gain_db=prof.get("master_gain_db"),
        sweep_volume_db=prof.get("sweep_volume_db"),
        pre_sweep_validation=prof.get("pre_sweep_validation") or [],
        reference_loopback=prof.get("reference_loopback"),
    )


def chain_requires_cal_mode(chain: ChainSpec) -> bool:
    """True if the chain spec requires cal_mode to be active before the sweep."""
    if not chain.cal_mode:
        return False
    return chain.cal_mode.get("enabled") == "required"


def chain_forbids_cal_mode(chain: ChainSpec) -> bool:
    """True if the chain spec requires cal_mode to be OFF before the sweep."""
    if not chain.cal_mode:
        return False
    return chain.cal_mode.get("enabled") == "forbidden"
