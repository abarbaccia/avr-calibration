"""Signal graph — named-transducer topology for calibration scope.

A **composed prism**: the system models audio as signal passing through a stack
of layers, each with settable state. The graph names those layers and the
terminals they feed, so calibration can be expressed as "scope X on signal path
Y using source Z" rather than "apply_eq to output index 0."

Abstraction layers (from `CLAUDE.md` + this module):

- **Source** — origin of audio entering the chain (an AVR input name, a direct
  USB sweep, analog RCA, etc.). Lets the LLM reason about "which injection
  point do I use for this sweep."
- **Processor** — any node with filterable state: AVR (volume / trim /
  distance / sound-mode) or DSP (routing / PEQ / FIR / delay / gain). Owns a
  driver.
- **Transducer** — a physical driver in the room (sub, main, centre, surround,
  height, shaker). Referenced by name — ``"sub_left"`` — not by output index.
- **TransducerProfile** — hardware limits (boost ceilings, mandatory HPF,
  per-iteration change caps). Each transducer references one profile; profiles
  are shared across transducers of the same model.
- **TransducerGroup** — a named scope (``"bass"``, ``"front_soundstage"``) so
  recipes address a set of transducers with one identifier.

The graph is **loose on purpose**. Validation is best-effort — cycles aren't
detected, roles aren't enumerated. The LLM catches topological nonsense during
calibration; the graph just gives it names to reason over.

Legacy installs without a ``signal_graph:`` block get a synthesised graph via
``SignalGraph.from_legacy(config)`` so nothing downstream needs to special-case
absence.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from .config import Config


class _SweepContext:
    """Async context manager that composes zero or more processor sweep contexts.

    Enters each manager in insertion order on ``__aenter__``; exits in reverse
    order on ``__aexit__``. Empty context (no processors needed neutralisation)
    is a no-op.
    """

    def __init__(self, managers: list[Any]) -> None:
        self._managers = managers
        self._stack: AsyncExitStack | None = None

    async def __aenter__(self):
        self._stack = AsyncExitStack()
        try:
            await self._stack.__aenter__()
            for m in self._managers:
                await self._stack.enter_async_context(m)
        except Exception:
            await self._stack.aclose()
            self._stack = None
            raise
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._stack is None:
            return None
        try:
            return await self._stack.__aexit__(exc_type, exc, tb)
        finally:
            self._stack = None

    @property
    def is_empty(self) -> bool:
        return not self._managers


SourceType = Literal[
    "avr_input", "usb_direct", "toslink", "analog", "sweep_generator"
]

ProcessorKind = Literal["avr", "dsp"]


@dataclass(frozen=True)
class Source:
    """An audio origin entering the chain.

    ``avr_input_ref`` is the string name the AVR uses for the input (e.g.
    ``"GAME2"``). ``hdmi_channel_map`` lets a source declare its channel
    layout (``{"lfe": 3, "left": 1, ...}``); when absent the top-level
    ``hdmi_channel_map`` config block is used.
    """

    name: str
    type: SourceType
    avr_input_ref: str | None = None
    hdmi_channel_map: dict[str, int] | None = None


@dataclass(frozen=True)
class Processor:
    """A node with filterable state — AVR or DSP.

    ``driver_ref`` selects a class in the drivers registry (``"denon"``,
    ``"minidsp"``, ``"camilladsp"``). ``config_key`` (optional) is the
    ``config.yaml`` section the registry reads for connection parameters;
    defaults to ``driver_ref``.

    ``inputs`` / ``outputs`` are string identifiers meaningful to the
    processor's driver — on a DSP these are output channel names
    (``"0".."9"``), on an AVR they're preout labels (``"preout_sub_1"``) or
    input keys (``"GAME2"``).

    ``display_name`` (optional) is the human-readable label shown in the web
    UI and other user-facing surfaces (e.g. ``"Living Room miniDSP"``,
    ``"CamillaDSP"``). When unset, callers should fall back to
    :func:`default_display_name` which derives a sensible label from
    ``driver_ref``.
    """

    name: str
    driver_ref: str
    kind: ProcessorKind
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    config_key: str | None = None
    display_name: str | None = None


_DEFAULT_DISPLAY_NAMES: dict[str, str] = {
    "denon": "Denon AVR",
    "minidsp": "miniDSP 2x4 HD",
    "camilladsp": "CamillaDSP",
}


def default_display_name(proc: Processor) -> str:
    """Fallback human-readable label for a processor without ``display_name``.

    Looks up the driver_ref in a small table of well-known drivers; unknown
    drivers fall back to a title-cased version of the ref itself so third-party
    drivers still render reasonably.
    """
    if proc.display_name:
        return proc.display_name
    return _DEFAULT_DISPLAY_NAMES.get(proc.driver_ref, proc.driver_ref.title())


@dataclass(frozen=True)
class TransducerProfile:
    """Hardware safety limits for one transducer model.

    Defaults match the current module-level constants in ``safety.py`` (SVS
    PB12-NSD, ported sub, 22 Hz tuning). A profile without an HPF
    (``hpf_freq_hz=None``) tells ``SafetyValidator`` not to enforce one —
    appropriate for tweeters / mids / shakers that have no infrasonic issue.
    """

    name: str
    min_boost_freq_hz: float = 25.0
    max_boost_per_band_db: float = 6.0
    max_boost_above_threshold_db: float = 8.0
    freq_dependent_boost_threshold_hz: float = 30.0
    max_cumulative_boost_db: float = 9.0
    max_change_per_iter_db: float = 3.0
    max_change_simulated_db: float = 6.0
    hpf_freq_hz: float | None = 18.0
    hpf_order: int = 4
    notes: str = ""


@dataclass(frozen=True)
class Transducer:
    """A physical driver in the room.

    Role is an open string ("sub", "main", "center", "surround", "height",
    "shaker", …) — not a closed enum. The LLM can reason about roles it
    hasn't seen; enforcing an enum here is the kind of static validation the
    module docstring explicitly rejects.
    """

    name: str
    role: str
    processor_ref: str
    output_index: int
    safety_profile_ref: str
    position: str | None = None


@dataclass(frozen=True)
class TransducerGroup:
    """A named scope — ``"bass"``, ``"front_soundstage"``."""

    name: str
    members: tuple[str, ...]


# Built-in default — matches current module-level constants in safety.py.
# Kept as a module-level constant so ``SafetyValidator()`` (no args) and other
# legacy call sites that reference ``SVS_PB12_NSD_PROFILE`` directly keep
# working. The YAML copy in ``calibrate/profiles/transducers/svs_pb12_nsd.yaml``
# is the authoritative version for the shared profile library; this constant
# mirrors it verbatim.
SVS_PB12_NSD_PROFILE = TransducerProfile(
    name="svs_pb12_nsd",
    min_boost_freq_hz=25.0,
    max_boost_per_band_db=6.0,
    max_boost_above_threshold_db=8.0,
    freq_dependent_boost_threshold_hz=30.0,
    max_cumulative_boost_db=9.0,
    max_change_per_iter_db=3.0,
    max_change_simulated_db=6.0,
    hpf_freq_hz=18.0,
    hpf_order=4,
    notes="Default SVS PB12-NSD ported sub. Matches legacy safety.py constants.",
)


def _profile_from_dict(data: dict) -> TransducerProfile:
    """Construct a TransducerProfile from the YAML dict shape.

    Accepts the ``hpf: {freq, order}`` sub-block that both the user's
    inline config and the shipped profile files use; flattens it onto
    the dataclass fields ``hpf_freq_hz`` / ``hpf_order``. Missing fields
    fall back to the dataclass defaults.
    """
    hpf = data.get("hpf")
    hpf_freq: float | None
    hpf_order: int
    if hpf is None:
        hpf_freq, hpf_order = None, 4
    elif isinstance(hpf, dict):
        raw_freq = hpf.get("freq")
        hpf_freq = float(raw_freq) if raw_freq is not None else None
        hpf_order = int(hpf.get("order", 4))
    else:
        hpf_freq, hpf_order = None, 4

    return TransducerProfile(
        name=data["name"],
        min_boost_freq_hz=float(data.get("min_boost_freq_hz", 25.0)),
        max_boost_per_band_db=float(data.get("max_boost_per_band_db", 6.0)),
        max_boost_above_threshold_db=float(
            data.get("max_boost_above_threshold_db", 8.0)
        ),
        freq_dependent_boost_threshold_hz=float(
            data.get("freq_dependent_boost_threshold_hz", 30.0)
        ),
        max_cumulative_boost_db=float(data.get("max_cumulative_boost_db", 9.0)),
        max_change_per_iter_db=float(data.get("max_change_per_iter_db", 3.0)),
        max_change_simulated_db=float(data.get("max_change_simulated_db", 6.0)),
        hpf_freq_hz=hpf_freq,
        hpf_order=hpf_order,
        notes=data.get("notes", ""),
    )


def load_builtin_profiles() -> tuple[TransducerProfile, ...]:
    """Scan ``calibrate/profiles/transducers/*.yaml`` and return each as a profile.

    Profiles ship with the package so new transducers can be added by dropping
    a YAML file — no code change. The user's inline config.yaml declarations
    override shipped profiles by ``name`` when the graph merges them.
    """
    import yaml
    from pathlib import Path

    profiles: list[TransducerProfile] = []
    pkg_root = Path(__file__).resolve().parent
    transducer_dir = pkg_root / "profiles" / "transducers"
    if not transducer_dir.is_dir():
        return ()
    for yaml_path in sorted(transducer_dir.glob("*.yaml")):
        try:
            with open(yaml_path) as f:
                data = yaml.safe_load(f)
            if not isinstance(data, dict):
                continue
            profiles.append(_profile_from_dict(data))
        except Exception:
            # A malformed profile should never crash graph construction — the
            # fallback is the built-in SVS default + whatever the user declares.
            continue
    return tuple(profiles)


def _merge_profiles(
    builtin: tuple[TransducerProfile, ...],
    user: tuple[TransducerProfile, ...],
) -> tuple[TransducerProfile, ...]:
    """Return built-in profiles overlaid with user declarations.

    Name collisions resolve to the user's version — the repo library is a
    default / sensible fallback, not a lock-in.
    """
    by_name = {p.name: p for p in builtin}
    for p in user:
        by_name[p.name] = p
    return tuple(by_name.values())


@dataclass(frozen=True)
class SignalGraph:
    """Container for all graph nodes + lookup methods.

    Construction: usually via ``from_dict`` (from YAML) or ``from_legacy``
    (from a legacy Config). Direct construction is fine in tests.
    """

    sources: tuple[Source, ...] = ()
    processors: tuple[Processor, ...] = ()
    transducers: tuple[Transducer, ...] = ()
    profiles: tuple[TransducerProfile, ...] = ()
    groups: tuple[TransducerGroup, ...] = ()

    # ── lookups ──────────────────────────────────────────────────────────────

    def source_by_name(self, name: str) -> Source | None:
        return next((s for s in self.sources if s.name == name), None)

    def processor_by_name(self, name: str) -> Processor | None:
        return next((p for p in self.processors if p.name == name), None)

    def transducer_by_name(self, name: str) -> Transducer | None:
        return next((t for t in self.transducers if t.name == name), None)

    def profile_by_name(self, name: str) -> TransducerProfile | None:
        return next((p for p in self.profiles if p.name == name), None)

    def group_by_name(self, name: str) -> TransducerGroup | None:
        return next((g for g in self.groups if g.name == name), None)

    def transducers_by_role(self, role: str) -> tuple[Transducer, ...]:
        return tuple(t for t in self.transducers if t.role == role)

    def transducers_on(self, processor_name: str) -> tuple[Transducer, ...]:
        return tuple(t for t in self.transducers if t.processor_ref == processor_name)

    # ── target resolution ────────────────────────────────────────────────────

    def resolve_target(self, target: str) -> tuple[Transducer, ...]:
        """Resolve a ``target`` string to a tuple of Transducer nodes.

        Resolution order:
          1. Group name (``"bass"``) → group members
          2. Transducer name (``"sub_left"``) → single-element tuple
          3. Role name (``"sub"``) → all transducers with that role
          4. Empty tuple if nothing matches

        Callers that accept a legacy ``int`` output index should pass it
        through unchanged — index fallback lives at the MCP tool layer, not
        here. The graph speaks names.
        """
        group = self.group_by_name(target)
        if group:
            resolved = tuple(
                t for m in group.members
                if (t := self.transducer_by_name(m)) is not None
            )
            return resolved
        t = self.transducer_by_name(target)
        if t is not None:
            return (t,)
        by_role = self.transducers_by_role(target)
        if by_role:
            return by_role
        return ()

    def profile_for(self, transducer: Transducer) -> TransducerProfile:
        """Return the transducer's profile, defaulting to the built-in SVS."""
        p = self.profile_by_name(transducer.safety_profile_ref)
        return p if p is not None else SVS_PB12_NSD_PROFILE

    def strictest_profile(
        self, transducers: tuple[Transducer, ...]
    ) -> TransducerProfile:
        """Pick the most restrictive profile across a set.

        Used for input EQ where one filter applies across all downstream
        outputs: the filter must be safe for the *weakest* driver in the
        chain. Strictness axes: lowest ``max_boost_per_band_db``, lowest
        ``max_cumulative_boost_db``, highest ``min_boost_freq_hz``.
        Falls back to SVS default when the set is empty.
        """
        if not transducers:
            return SVS_PB12_NSD_PROFILE
        profiles = [self.profile_for(t) for t in transducers]
        # Strictness: take the min of each ceiling + max of the freq floor.
        return TransducerProfile(
            name="__strictest_of__" + "+".join(p.name for p in profiles),
            min_boost_freq_hz=max(p.min_boost_freq_hz for p in profiles),
            max_boost_per_band_db=min(p.max_boost_per_band_db for p in profiles),
            max_boost_above_threshold_db=min(
                p.max_boost_above_threshold_db for p in profiles
            ),
            freq_dependent_boost_threshold_hz=max(
                p.freq_dependent_boost_threshold_hz for p in profiles
            ),
            max_cumulative_boost_db=min(p.max_cumulative_boost_db for p in profiles),
            max_change_per_iter_db=min(p.max_change_per_iter_db for p in profiles),
            max_change_simulated_db=min(p.max_change_simulated_db for p in profiles),
            hpf_freq_hz=min(
                (p.hpf_freq_hz for p in profiles if p.hpf_freq_hz is not None),
                default=None,
            ),
            hpf_order=max(p.hpf_order for p in profiles),
            notes="Synthetic strictest-of profile",
        )

    def default_processor(self, kind: ProcessorKind) -> Processor | None:
        """First processor of the given kind. Used for legacy single-X dispatch."""
        return next((p for p in self.processors if p.kind == kind), None)

    # ── sweep-context composition ────────────────────────────────────────────

    def processors_on_path(
        self, source_name: str, transducers: tuple[Transducer, ...]
    ) -> tuple[Processor, ...]:
        """Return the processors the signal flows through from source to targets.

        Heuristic: if the source's type is ``"avr_input"`` or ``"toslink"`` or
        ``"analog"``, the AVR is in the path (decodes / routes before reaching
        a DSP). If the source is a direct DSP input (``"usb_direct"``,
        ``"sweep_generator"``), the AVR is bypassed. Each transducer's
        ``processor_ref`` adds that DSP to the path.

        Ordering: source-side first (AVR), then the transducer-side DSP(s).
        Duplicates removed while preserving order.
        """
        source = self.source_by_name(source_name)
        ordered: list[Processor] = []

        if source is not None and source.type in {"avr_input", "toslink", "analog"}:
            avr = self.default_processor("avr")
            if avr is not None:
                ordered.append(avr)

        seen = {p.name for p in ordered}
        for t in transducers:
            p = self.processor_by_name(t.processor_ref)
            if p is not None and p.name not in seen:
                ordered.append(p)
                seen.add(p.name)

        return tuple(ordered)

    def sweep_context_for_route(
        self,
        route: str,
        targets: tuple[Transducer, ...],
        config,
        registry,
    ) -> "_SweepContext":
        """Compose sweep contexts keyed on ``playback_route`` rather than source.

        ``"hdmi"`` includes the default AVR in the path; ``"usb"`` bypasses it.
        This is the measurement-oriented entry point — source objects on the
        graph are for reasoning about arbitrary injection points; the
        measurement layer only cares whether the signal is decoded by the AVR
        or arrives direct at the DSP.
        """
        processors: list[Processor] = []
        if route == "hdmi":
            avr = self.default_processor("avr")
            if avr is not None:
                processors.append(avr)

        seen = {p.name for p in processors}
        for t in targets:
            p = self.processor_by_name(t.processor_ref)
            if p is not None and p.name not in seen:
                processors.append(p)
                seen.add(p.name)

        return self._compose_contexts(processors, config, registry)

    def sweep_context(
        self,
        source_name: str,
        targets: tuple[Transducer, ...],
        config,
        registry,
    ) -> "_SweepContext":
        """Compose each upstream processor's neutralisation context into one stack.

        Returns an async context manager that, on ``__aenter__``, enters every
        driver's ``sweep_context(config)`` in signal order — AVR first, then
        DSP(s). On ``__aexit__`` the contexts unwind in reverse order.

        Drivers that don't need sweep-time setup return ``None`` from
        ``sweep_context`` and get skipped.

        ``registry`` is a ``DriverRegistry`` (``drivers.registry``); passed in
        rather than imported to avoid a hard dependency from graph → drivers.
        """
        processors = self.processors_on_path(source_name, targets)
        return self._compose_contexts(processors, config, registry)

    def _compose_contexts(
        self, processors: "list[Processor] | tuple[Processor, ...]", config, registry,
    ) -> "_SweepContext":
        """Collect each processor driver's sweep_context into an exit stack."""
        managers = []
        for proc in processors:
            drv = registry.get(proc.name)
            if drv is None:
                continue
            ctx = drv.sweep_context(config)
            if ctx is not None:
                managers.append(ctx)
        return _SweepContext(managers)

    # ── summary for LLM reasoning ────────────────────────────────────────────

    def summary(self) -> dict:
        """Compact dict for the ``get_signal_graph`` MCP tool.

        The LLM reads this to understand topology. Keep it terse — one dict
        per node type, fields carry the essentials only.
        """
        return {
            "sources": [
                {
                    "name": s.name,
                    "type": s.type,
                    "avr_input_ref": s.avr_input_ref,
                }
                for s in self.sources
            ],
            "processors": [
                {
                    "name": p.name,
                    "driver_ref": p.driver_ref,
                    "kind": p.kind,
                    "outputs": list(p.outputs),
                    "display_name": p.display_name or default_display_name(p),
                }
                for p in self.processors
            ],
            "transducers": [
                {
                    "name": t.name,
                    "role": t.role,
                    "processor": t.processor_ref,
                    "output_index": t.output_index,
                    "profile": t.safety_profile_ref,
                    "position": t.position,
                }
                for t in self.transducers
            ],
            "profiles": [
                {
                    "name": p.name,
                    "min_boost_freq_hz": p.min_boost_freq_hz,
                    "max_boost_per_band_db": p.max_boost_per_band_db,
                    "max_cumulative_boost_db": p.max_cumulative_boost_db,
                    "hpf_freq_hz": p.hpf_freq_hz,
                    "hpf_order": p.hpf_order,
                }
                for p in self.profiles
            ],
            "groups": [
                {"name": g.name, "members": list(g.members)} for g in self.groups
            ],
        }

    # ── construction ─────────────────────────────────────────────────────────

    @classmethod
    def from_dict(cls, data: dict) -> "SignalGraph":
        """Build a SignalGraph from a parsed YAML dict (the ``signal_graph:`` block)."""
        return cls(
            sources=tuple(
                Source(
                    name=s["name"],
                    type=s["type"],
                    avr_input_ref=s.get("avr_input_ref"),
                    hdmi_channel_map=s.get("hdmi_channel_map"),
                )
                for s in data.get("sources", [])
            ),
            processors=tuple(
                Processor(
                    name=p["name"],
                    driver_ref=p["driver_ref"],
                    kind=p["kind"],
                    inputs=tuple(p.get("inputs", [])),
                    outputs=tuple(str(o) for o in p.get("outputs", [])),
                    config_key=p.get("config_key"),
                    display_name=p.get("display_name"),
                )
                for p in data.get("processors", [])
            ),
            profiles=_merge_profiles(
                load_builtin_profiles(),
                tuple(_profile_from_dict(p) for p in data.get("transducer_profiles", [])),
            ),
            transducers=tuple(
                Transducer(
                    name=t["name"],
                    role=t["role"],
                    processor_ref=t["processor_ref"],
                    output_index=int(t["output_index"]),
                    safety_profile_ref=t["safety_profile_ref"],
                    position=t.get("position"),
                )
                for t in data.get("transducers", [])
            ),
            groups=tuple(
                TransducerGroup(name=g["name"], members=tuple(g["members"]))
                for g in data.get("groups", [])
            ),
        )

    @classmethod
    def from_legacy(cls, config: "Config") -> "SignalGraph":
        """Synthesise a graph from legacy single-DSP / sub_outputs config.

        Every install without an explicit ``signal_graph:`` block still gets a
        graph — this is what makes the migration non-breaking. The shim reads
        ``minidsp.output_slots`` + ``measurement.sub_outputs`` to name
        transducers, attaches the built-in SVS profile to subs, and creates a
        ``bass`` group.
        """
        processors: list[Processor] = []
        transducers: list[Transducer] = []
        # Start from the shipped profile library so every legacy install sees
        # the full set of known transducers (SVS, MQB-1, and anything else
        # under calibrate/profiles/transducers/). Fall back to the in-memory
        # SVS default only if the library fails to load for some reason.
        builtin_profiles = load_builtin_profiles()
        profiles: list[TransducerProfile] = (
            list(builtin_profiles) if builtin_profiles else [SVS_PB12_NSD_PROFILE]
        )
        groups: list[TransducerGroup] = []

        dsp_name = config.dsp_driver_name
        avr_name = config.avr_driver_name
        # Legacy installs always have an AVR — SSDP auto-discovery means host
        # may be unset even when the hardware exists. Omit only when the user
        # has explicitly opted out via ``avr_driver: none`` or similar.
        has_avr = avr_name and avr_name.lower() not in {"none", "null", ""}

        if has_avr:
            processors.append(Processor(
                name=avr_name, driver_ref=avr_name, kind="avr",
            ))

        # DSP processor: use driver name as processor name; outputs from slots or a
        # 4-channel default (miniDSP 2x4 HD).
        dsp_block = getattr(config, dsp_name, None) or {}
        if isinstance(dsp_block, dict):
            slots = dsp_block.get("output_slots")
        else:
            slots = None
        if not slots:
            slots = config.minidsp.get("output_slots", [
                {"index": 0, "type": "sub"},
                {"index": 1, "type": "sub"},
                {"index": 2, "type": "unused"},
                {"index": 3, "type": "unused"},
            ])

        dsp_outputs = tuple(str(s["index"]) for s in slots)
        processors.append(Processor(
            name=dsp_name, driver_ref=dsp_name, kind="dsp",
            outputs=dsp_outputs,
        ))

        # Transducers: one per non-unused slot.
        sub_counter = 0
        shaker_counter = 0
        sub_members: list[str] = []
        for slot in slots:
            stype = slot.get("type", "unused")
            if stype == "unused":
                continue
            idx = int(slot["index"])
            if stype == "sub":
                sub_counter += 1
                name = f"sub_{sub_counter}"
                sub_members.append(name)
                transducers.append(Transducer(
                    name=name, role="sub",
                    processor_ref=dsp_name,
                    output_index=idx,
                    safety_profile_ref=SVS_PB12_NSD_PROFILE.name,
                    position=slot.get("label") or None,
                ))
            elif stype == "shaker":
                shaker_counter += 1
                name = f"shaker_{shaker_counter}"
                transducers.append(Transducer(
                    name=name, role="shaker",
                    processor_ref=dsp_name,
                    output_index=idx,
                    # Shakers reuse SVS limits for now — user hasn't declared
                    # a profile; better too-restrictive than too-loose.
                    safety_profile_ref=SVS_PB12_NSD_PROFILE.name,
                    position=slot.get("label") or None,
                ))
            else:
                # Unknown type — still create a transducer so the graph is complete,
                # but flag role as the raw type so the LLM can reason about it.
                transducers.append(Transducer(
                    name=f"{stype}_{idx}", role=stype,
                    processor_ref=dsp_name,
                    output_index=idx,
                    safety_profile_ref=SVS_PB12_NSD_PROFILE.name,
                ))

        # If output_slots had no subs but measurement.sub_outputs lists some,
        # synthesise from that — covers the default-config fallback path.
        if not sub_members:
            for idx in config.measurement.get("sub_outputs", [0, 1]):
                sub_counter += 1
                name = f"sub_{sub_counter}"
                sub_members.append(name)
                transducers.append(Transducer(
                    name=name, role="sub",
                    processor_ref=dsp_name,
                    output_index=int(idx),
                    safety_profile_ref=SVS_PB12_NSD_PROFILE.name,
                ))

        if sub_members:
            groups.append(TransducerGroup(name="bass", members=tuple(sub_members)))

        return cls(
            sources=(
                Source(name="usb_sweep", type="usb_direct"),
            ),
            processors=tuple(processors),
            transducers=tuple(transducers),
            profiles=tuple(profiles),
            groups=tuple(groups),
        )
