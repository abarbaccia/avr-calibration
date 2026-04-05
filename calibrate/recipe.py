"""Recipe parser — load calibration strategy from YAML files.

A recipe defines calibration strategy: which target curve, convergence
criteria, how aggressive to be. It does NOT contain hardware facts (PEQ slots,
sub tuning, safety limits) — those come from the hardware profile.

Usage::

    from calibrate.recipe import Recipe, load_recipe

    recipe = load_recipe("harman-bass")
    # or from a path:
    recipe = load_recipe("/path/to/custom.yaml")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

_RECIPES_DIR_DEFAULT = Path(__file__).parent.parent / "recipes"
_RECIPES_DIR_APP = Path("/app/recipes")
_RECIPES_DIR = _RECIPES_DIR_DEFAULT if _RECIPES_DIR_DEFAULT.is_dir() else _RECIPES_DIR_APP

VALID_TARGETS = {"harman", "flat"}
VALID_METRICS = {"rms_deviation"}
VALID_ANALYSIS = {"claude", "mock"}


class RecipeError(ValueError):
    """Raised when a recipe file is invalid or not found."""


@dataclass(frozen=True)
class ConvergenceCriteria:
    metric: str = "rms_deviation"
    threshold_db: float = 2.0
    max_iterations: int = 5


@dataclass(frozen=True)
class MeasurementConfig:
    retry_count: int = 2
    retry_delay_s: float = 5.0


@dataclass(frozen=True)
class Recipe:
    """Calibration strategy loaded from a YAML recipe file."""

    name: str
    description: str = ""
    target: str = "harman"
    band: tuple[float, float] = (20.0, 200.0)
    convergence: ConvergenceCriteria = field(default_factory=ConvergenceCriteria)
    analysis: str = "claude"
    measurement: MeasurementConfig = field(default_factory=MeasurementConfig)


def load_recipe(name_or_path: str) -> Recipe:
    """Load a recipe by name (from recipes/ directory) or by file path.

    If *name_or_path* is a path to an existing file, load it directly.
    Otherwise, search the recipes/ directory for ``{name}.yaml``.

    Raises RecipeError if the file is not found or contains invalid data.
    """
    path = Path(name_or_path)
    if not path.exists():
        # Try as a name in the recipes directory
        path = _RECIPES_DIR / f"{name_or_path}.yaml"
        if not path.exists():
            raise RecipeError(
                f"recipe not found: {name_or_path!r} "
                f"(searched {_RECIPES_DIR / f'{name_or_path}.yaml'})"
            )

    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise RecipeError(f"invalid YAML in {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise RecipeError(f"recipe file must contain a YAML mapping, got {type(raw).__name__}")

    return _parse_recipe(raw, source=str(path))


def _parse_recipe(raw: dict, source: str = "<unknown>") -> Recipe:
    """Parse a raw dict into a validated Recipe."""
    name = raw.get("name")
    if not name or not isinstance(name, str):
        raise RecipeError(f"recipe in {source} missing required field 'name'")

    target = raw.get("target", "harman")
    if target not in VALID_TARGETS:
        raise RecipeError(
            f"recipe {name!r}: invalid target {target!r}, must be one of {VALID_TARGETS}"
        )

    analysis = raw.get("analysis", "claude")
    if analysis not in VALID_ANALYSIS:
        raise RecipeError(
            f"recipe {name!r}: invalid analysis backend {analysis!r}, "
            f"must be one of {VALID_ANALYSIS}"
        )

    band_raw = raw.get("band", [20, 200])
    if not isinstance(band_raw, list) or len(band_raw) != 2:
        raise RecipeError(f"recipe {name!r}: 'band' must be a list of [low_hz, high_hz]")
    band_low, band_high = float(band_raw[0]), float(band_raw[1])
    if band_low >= band_high or band_low < 0:
        raise RecipeError(f"recipe {name!r}: invalid band [{band_low}, {band_high}]")

    conv_raw = raw.get("convergence", {})
    metric = conv_raw.get("metric", "rms_deviation")
    if metric not in VALID_METRICS:
        raise RecipeError(
            f"recipe {name!r}: invalid convergence metric {metric!r}, "
            f"must be one of {VALID_METRICS}"
        )
    threshold_db = float(conv_raw.get("threshold_db", 2.0))
    if threshold_db <= 0:
        raise RecipeError(f"recipe {name!r}: convergence threshold_db must be positive")
    max_iterations = int(conv_raw.get("max_iterations", 5))
    if max_iterations < 1:
        raise RecipeError(f"recipe {name!r}: max_iterations must be >= 1")

    meas_raw = raw.get("measurement", {})
    retry_count = int(meas_raw.get("retry_count", 2))
    retry_delay_s = float(meas_raw.get("retry_delay_s", 5.0))

    return Recipe(
        name=name,
        description=raw.get("description", ""),
        target=target,
        band=(band_low, band_high),
        convergence=ConvergenceCriteria(
            metric=metric,
            threshold_db=threshold_db,
            max_iterations=max_iterations,
        ),
        analysis=analysis,
        measurement=MeasurementConfig(
            retry_count=retry_count,
            retry_delay_s=retry_delay_s,
        ),
    )
