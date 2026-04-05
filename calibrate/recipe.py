"""Recipe module — load recipes and provide typed containers for calibration params.

Recipes are human-readable markdown files in recipes/core/ that Claude reads
and executes by calling MCP tools. This module provides:

1. load_recipe_text() — find and return recipe markdown for Claude to read
2. list_recipes() — list available recipe files
3. Recipe dataclass — typed container for calibration parameters used by
   analysis and other primitives (target, band, convergence, etc.)

Usage::

    from calibrate.recipe import load_recipe_text, list_recipes

    text = load_recipe_text("harman-bass-aligned")
    recipes = list_recipes()
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

_RECIPES_DIR_DEFAULT = Path(__file__).parent.parent / "recipes"
_RECIPES_DIR_APP = Path("/app/recipes")
_RECIPES_DIR = _RECIPES_DIR_DEFAULT if _RECIPES_DIR_DEFAULT.is_dir() else _RECIPES_DIR_APP


class RecipeError(ValueError):
    """Raised when a recipe file is not found."""


# ── Typed containers (used by analysis engine and MCP tools) ──────────────────

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
    """Typed calibration parameters — used by analysis primitives.

    This is NOT the recipe file. The recipe file is a markdown document that
    Claude reads and follows. This dataclass holds the structured parameters
    that Python primitives (analysis, safety, etc.) need.
    """
    name: str
    description: str = ""
    target: str = "harman"
    band: tuple[float, float] = (20.0, 80.0)
    convergence: ConvergenceCriteria = field(default_factory=ConvergenceCriteria)
    analysis: str = "claude"
    measurement: MeasurementConfig = field(default_factory=MeasurementConfig)


# ── Recipe file loading ───────────────────────────────────────────────────────

def load_recipe_text(name_or_path: str) -> str:
    """Load a recipe by name and return its full text content.

    Searches for markdown recipes in recipes/core/ first, then YAML in recipes/.
    If *name_or_path* is a path to an existing file, load it directly.

    Returns the recipe file content as a string for Claude to read.
    Raises RecipeError if the file is not found.
    """
    path = Path(name_or_path)
    if path.exists():
        return path.read_text()

    # Try markdown in recipes/core/
    md_path = _RECIPES_DIR / "core" / f"{name_or_path}.md"
    if md_path.exists():
        return md_path.read_text()

    # Try YAML in recipes/ (backwards compat)
    yaml_path = _RECIPES_DIR / f"{name_or_path}.yaml"
    if yaml_path.exists():
        return yaml_path.read_text()

    raise RecipeError(
        f"recipe not found: {name_or_path!r} "
        f"(searched {md_path} and {yaml_path})"
    )


def list_recipes() -> list[dict[str, str]]:
    """List all available recipes with name and path."""
    recipes = []

    # Markdown recipes in core/
    core_dir = _RECIPES_DIR / "core"
    if core_dir.is_dir():
        for p in sorted(core_dir.glob("*.md")):
            recipes.append({"name": p.stem, "path": str(p), "format": "markdown"})

    # YAML recipes (legacy)
    for p in sorted(_RECIPES_DIR.glob("*.yaml")):
        recipes.append({"name": p.stem, "path": str(p), "format": "yaml"})

    return recipes
