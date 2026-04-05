"""Tests for recipe parser — YAML loading, defaults, and validation."""

import pytest
import yaml
from pathlib import Path

from calibrate.recipe import Recipe, RecipeError, load_recipe


# ── Load harman-bass ──────────────────────────────────────────────────────────

def test_load_harman_bass() -> None:
    recipe = load_recipe("harman-bass")
    assert recipe.name == "harman-bass"
    assert recipe.target == "harman"
    assert recipe.band == (20.0, 200.0)
    assert recipe.convergence.metric == "rms_deviation"
    assert recipe.convergence.threshold_db == 2.0
    assert recipe.convergence.max_iterations == 5
    assert recipe.analysis == "claude"
    assert recipe.measurement.retry_count == 2
    assert recipe.measurement.retry_delay_s == 5.0


# ── Defaults ──────────────────────────────────────────────────────────────────

def test_recipe_defaults(tmp_path: Path) -> None:
    """Missing optional fields get sensible defaults."""
    recipe_file = tmp_path / "minimal.yaml"
    recipe_file.write_text(yaml.dump({"name": "minimal"}))

    recipe = load_recipe(str(recipe_file))
    assert recipe.name == "minimal"
    assert recipe.target == "harman"
    assert recipe.band == (20.0, 200.0)
    assert recipe.convergence.threshold_db == 2.0
    assert recipe.convergence.max_iterations == 5
    assert recipe.analysis == "claude"
    assert recipe.measurement.retry_count == 2


# ── Validation errors ─────────────────────────────────────────────────────────

def test_recipe_missing_name(tmp_path: Path) -> None:
    recipe_file = tmp_path / "bad.yaml"
    recipe_file.write_text(yaml.dump({"target": "harman"}))
    with pytest.raises(RecipeError, match="missing required field 'name'"):
        load_recipe(str(recipe_file))


def test_recipe_invalid_band(tmp_path: Path) -> None:
    recipe_file = tmp_path / "bad.yaml"
    recipe_file.write_text(yaml.dump({"name": "test", "band": [200, 20]}))
    with pytest.raises(RecipeError, match="invalid band"):
        load_recipe(str(recipe_file))


def test_recipe_negative_threshold(tmp_path: Path) -> None:
    recipe_file = tmp_path / "bad.yaml"
    recipe_file.write_text(yaml.dump({
        "name": "test",
        "convergence": {"threshold_db": -1.0},
    }))
    with pytest.raises(RecipeError, match="threshold_db must be positive"):
        load_recipe(str(recipe_file))


def test_recipe_unknown_analysis_backend(tmp_path: Path) -> None:
    recipe_file = tmp_path / "bad.yaml"
    recipe_file.write_text(yaml.dump({"name": "test", "analysis": "gpt4"}))
    with pytest.raises(RecipeError, match="invalid analysis backend"):
        load_recipe(str(recipe_file))


def test_recipe_unknown_target(tmp_path: Path) -> None:
    recipe_file = tmp_path / "bad.yaml"
    recipe_file.write_text(yaml.dump({"name": "test", "target": "pink_floyd"}))
    with pytest.raises(RecipeError, match="invalid target"):
        load_recipe(str(recipe_file))


def test_recipe_not_found() -> None:
    with pytest.raises(RecipeError, match="recipe not found"):
        load_recipe("nonexistent-recipe-xyz")
