"""Recorded measurement-session fixtures for regression testing.

These JSON snapshots are pulled from the Pi's ``history.db`` and downsampled
to ~200 frequency points across 20-200 Hz. They preserve real room behaviour
(modal hump at 47/70/94 Hz, T60 400-1163 ms) so future tool changes can be
verified against actual measurements rather than only synthetic curves.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_FIXTURES_DIR = Path(__file__).parent / "sessions"


def load_session_fixture(name: str) -> dict[str, Any]:
    """Load a recorded measurement session for regression testing.

    Args:
        name: fixture base-name without extension, e.g.
            ``"sub_fr_solo_clean"``, ``"sub_fr_solo_post_routing"``.

    Returns:
        dict with shape ``{session_id, label, sample_rate, fr: [...],
        ir: {...}, decay_modes: [...], group_delay: [...]}``.
    """
    path = _FIXTURES_DIR / f"{name}.json"
    if not path.exists():
        available = sorted(p.stem for p in _FIXTURES_DIR.glob("*.json"))
        raise FileNotFoundError(
            f"session fixture {name!r} not found at {path}. "
            f"Available: {available}"
        )
    return json.loads(path.read_text())
