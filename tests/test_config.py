"""Tests for calibrate/config.py — Config loading and update_config."""

from pathlib import Path

import pytest
import yaml

from calibrate.config import Config, DEFAULT_CONFIG, update_config


# ── Config.load — missing config falls back to defaults ──────────────────────

class TestConfigLoad:
    def test_load_returns_defaults_when_no_file(self, tmp_path):
        """Config.load when file doesn't exist returns default config values."""
        missing = tmp_path / "nonexistent.yaml"
        cfg = Config.load(missing)
        assert cfg.minidsp.get("host") == "localhost"
        assert cfg.minidsp.get("port") == 5380

    def test_load_with_existing_file_merges_user_values(self, tmp_path):
        """User values override defaults; missing keys fall back to defaults."""
        p = tmp_path / "config.yaml"
        p.write_text(yaml.dump({"denon": {"host": "192.168.1.50"}}))
        cfg = Config.load(p)
        assert cfg.denon.get("host") == "192.168.1.50"
        # Default minidsp keys are still present
        assert cfg.minidsp.get("port") == 5380

    def test_load_user_non_dict_value_overrides_default(self, tmp_path):
        """Non-dict user values replace default value entirely (line 127)."""
        p = tmp_path / "config.yaml"
        # 'mic' default is a dict; user overrides with a non-dict (shouldn't happen
        # in practice, but let's hit the elif branch)
        # Use a top-level key that has a non-dict default — not possible for current
        # defaults, so test the dict vs non-dict merge path with a custom key.
        # We hit line 127 when the user value for a dict key is NOT a dict.
        p.write_text("denon: some_string\n")
        cfg = Config.load(p)
        # "some_string" is not None and not a dict → stored as-is (line 127)
        assert cfg.denon == "some_string"

    def test_load_empty_yaml_falls_back_to_defaults(self, tmp_path):
        """Empty YAML file → all keys fall back to DEFAULT_CONFIG."""
        p = tmp_path / "config.yaml"
        p.write_text("")  # empty file yields {} after safe_load
        cfg = Config.load(p)
        assert cfg.mic.get("name") == "UMIK"

    def test_load_null_user_value_uses_default(self, tmp_path):
        """If user sets a top-level key to null, the default is used (line 129)."""
        p = tmp_path / "config.yaml"
        p.write_text("denon:\n")  # denon: null in YAML
        cfg = Config.load(p)
        # null user_val → else branch uses default_val ({"host": None})
        assert cfg.denon == DEFAULT_CONFIG["denon"]


# ── Config.create_template ────────────────────────────────────────────────────

class TestCreateTemplate:
    def test_create_template_writes_file(self, tmp_path):
        """create_template creates the config directory and writes the template."""
        p = tmp_path / "subdir" / "config.yaml"
        Config.create_template(p)
        assert p.exists()
        content = p.read_text()
        assert "AVR Calibration" in content

    def test_create_template_is_valid_yaml(self, tmp_path):
        """Template content is valid YAML."""
        p = tmp_path / "config.yaml"
        Config.create_template(p)
        data = yaml.safe_load(p.read_text())
        assert isinstance(data, dict)


# ── update_config ─────────────────────────────────────────────────────────────

class TestUpdateConfig:
    def test_update_creates_file_when_missing(self, tmp_path):
        """update_config creates the file if it doesn't exist."""
        p = tmp_path / "new.yaml"
        update_config({"denon": {"host": "10.0.0.1"}}, path=p)
        assert p.exists()
        data = yaml.safe_load(p.read_text())
        assert data["denon"]["host"] == "10.0.0.1"

    def test_update_merges_with_existing(self, tmp_path):
        """Existing keys not in updates are preserved."""
        p = tmp_path / "config.yaml"
        p.write_text(yaml.dump({"denon": {"host": "192.168.1.100"}, "mic": {"name": "UMIK"}}))
        update_config({"denon": {"host": "10.0.0.2"}}, path=p)
        data = yaml.safe_load(p.read_text())
        # mic is preserved; denon host is updated
        assert data["mic"]["name"] == "UMIK"
        assert data["denon"]["host"] == "10.0.0.2"

    def test_update_non_dict_value_replaces(self, tmp_path):
        """Non-dict update value replaces the existing value entirely."""
        p = tmp_path / "config.yaml"
        p.write_text(yaml.dump({"version": 1}))
        update_config({"version": 2}, path=p)
        data = yaml.safe_load(p.read_text())
        assert data["version"] == 2

    def test_update_nested_dict_shallow_merge(self, tmp_path):
        """Dict values are shallow-merged: existing sub-keys not in update are kept."""
        p = tmp_path / "config.yaml"
        p.write_text(yaml.dump({"minidsp": {"host": "localhost", "port": 5380}}))
        update_config({"minidsp": {"host": "10.0.0.3"}}, path=p)
        data = yaml.safe_load(p.read_text())
        assert data["minidsp"]["host"] == "10.0.0.3"
        assert data["minidsp"]["port"] == 5380
