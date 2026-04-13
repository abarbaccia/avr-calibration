"""Tests for recipe module — file loading, listing, and data containers."""

import pytest
from pathlib import Path

from calibrate.recipe import (
    Recipe,
    RecipeError,
    ConvergenceCriteria,
    MeasurementConfig,
    load_recipe_text,
    list_recipes,
)


# ── load_recipe_text ─────────────────────────────────────────────────────────

def test_load_bass_calibration_md() -> None:
    """Core markdown recipe loads successfully."""
    text = load_recipe_text("bass-calibration")
    assert "Bass Calibration" in text or "bass" in text.lower()
    assert len(text) > 100


def test_load_full_room_verify_md() -> None:
    text = load_recipe_text("full-room-verify")
    assert "verify" in text.lower() or "integration" in text.lower()


def test_load_by_path(tmp_path: Path) -> None:
    """Loading by direct file path works."""
    recipe_file = tmp_path / "custom.md"
    recipe_file.write_text("# Custom recipe\nDo stuff.")
    text = load_recipe_text(str(recipe_file))
    assert "Custom recipe" in text


def test_load_yaml_fallback() -> None:
    """YAML recipes in recipes/ dir still load."""
    text = load_recipe_text("bass-calibration")
    # Should find either .md or .yaml version
    assert len(text) > 0


def test_recipe_not_found() -> None:
    with pytest.raises(RecipeError, match="recipe not found"):
        load_recipe_text("nonexistent-recipe-xyz")


# ── list_recipes ─────────────────────────────────────────────────────────────

def test_list_recipes_includes_core() -> None:
    recipes = list_recipes()
    names = [r["name"] for r in recipes]
    assert "bass-calibration" in names
    assert "full-room-verify" in names


def test_list_recipes_format_field() -> None:
    recipes = list_recipes()
    for r in recipes:
        assert r["format"] in ("markdown", "yaml")
        assert "path" in r


# ── Recipe dataclass ─────────────────────────────────────────────────────────

def test_recipe_defaults() -> None:
    r = Recipe(name="test")
    assert r.target == "harman"
    assert r.band == (20.0, 80.0)
    assert r.convergence.threshold_db == 2.0
    assert r.convergence.max_iterations == 5
    assert r.analysis == "claude"
    assert r.measurement.retry_count == 2


def test_recipe_custom_values() -> None:
    r = Recipe(
        name="custom",
        target="flat",
        band=(30.0, 120.0),
        convergence=ConvergenceCriteria(threshold_db=1.5, max_iterations=3),
        analysis="mock",
        measurement=MeasurementConfig(retry_count=1, retry_delay_s=2.0),
    )
    assert r.target == "flat"
    assert r.band == (30.0, 120.0)
    assert r.convergence.threshold_db == 1.5
    assert r.measurement.retry_delay_s == 2.0
