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

    def test_load_preserves_measurement_profiles(self, tmp_path):
        """measurement_profiles from YAML must survive Config.load() deep-merge.

        Regression for the restart-wipe bug: measurement_profiles was missing
        from DEFAULT_CONFIG, so Config.load() never copied it into merged{} and
        every container restart silently dropped the user's USB-route override.
        """
        p = tmp_path / "config.yaml"
        p.write_text(yaml.dump({
            "measurement_profiles": {
                "sub": {"route": "usb", "playback_device": "avr_cal_sweep"},
            },
        }))
        cfg = Config.load(p)
        profiles = cfg._data.get("measurement_profiles", {})
        assert profiles.get("sub", {}).get("route") == "usb", (
            "measurement_profiles.sub.route='usb' was lost — "
            "DEFAULT_CONFIG is missing the 'measurement_profiles' key"
        )

    def test_load_passes_through_unknown_keys(self, tmp_path):
        """Keys not in DEFAULT_CONFIG must be preserved, not silently dropped.

        Any future config key added to the YAML before its DEFAULT_CONFIG entry
        should not vanish on the first Config.load() call.
        """
        p = tmp_path / "config.yaml"
        p.write_text(yaml.dump({"future_key": {"nested": 42}}))
        cfg = Config.load(p)
        assert cfg._data.get("future_key") == {"nested": 42}, (
            "unknown config key was silently dropped by Config.load()"
        )


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


# ── New config defaults ──────────────────────────────────────────────────────

class TestNewConfigDefaults:
    def test_denon_settle_ms_default_is_5000(self):
        """denon_settle_ms defaults to 5000 (HDMI HDCP needs 3-5s)."""
        cfg = Config(DEFAULT_CONFIG.copy())
        assert cfg.measurement.get("denon_settle_ms") == 5000

    def test_denon_pure_direct_default_true(self):
        """denon_pure_direct defaults to True for backward compat."""
        cfg = Config(DEFAULT_CONFIG.copy())
        assert cfg.measurement.get("denon_pure_direct") is True

    def test_mic_device_index_default_none(self):
        """mic_device_index defaults to None (find by name)."""
        cfg = Config(DEFAULT_CONFIG.copy())
        assert cfg.measurement.get("mic_device_index") is None

    def test_hdmi_device_index_default_none(self):
        """hdmi_device_index defaults to None (find by name)."""
        cfg = Config(DEFAULT_CONFIG.copy())
        assert cfg.measurement.get("hdmi_device_index") is None

    def test_usb_device_index_default_none(self):
        """usb_device_index defaults to None (find by name)."""
        cfg = Config(DEFAULT_CONFIG.copy())
        assert cfg.measurement.get("usb_device_index") is None

    def test_master_gain_hdmi_db_default_none(self):
        """master_gain_hdmi_db defaults to None (don't change)."""
        cfg = Config(DEFAULT_CONFIG.copy())
        assert cfg.measurement.get("master_gain_hdmi_db") is None


# ── HDMI channel map ─────────────────────────────────────────────────────────

class TestHdmiChannelMap:
    def test_default_cea861_mapping(self):
        """Default channel map follows CEA-861 5.1 layout."""
        cfg = Config(DEFAULT_CONFIG.copy())
        m = cfg.hdmi_channel_map
        assert m["left"] == 1
        assert m["right"] == 2
        assert m["lfe"] == 3
        assert m["center"] == 4
        assert m["surround_left"] == 5
        assert m["surround_right"] == 6

    def test_hdmi_channel_for_exact_key(self):
        """hdmi_channel_for resolves exact role names."""
        cfg = Config(DEFAULT_CONFIG.copy())
        assert cfg.hdmi_channel_for("lfe") == 3
        assert cfg.hdmi_channel_for("center") == 4
        assert cfg.hdmi_channel_for("left") == 1

    def test_hdmi_channel_for_alias(self):
        """hdmi_channel_for resolves common aliases."""
        cfg = Config(DEFAULT_CONFIG.copy())
        assert cfg.hdmi_channel_for("sub") == 3
        assert cfg.hdmi_channel_for("subwoofer") == 3
        assert cfg.hdmi_channel_for("sw") == 3
        assert cfg.hdmi_channel_for("fl") == 1
        assert cfg.hdmi_channel_for("fr") == 2
        assert cfg.hdmi_channel_for("fc") == 4
        assert cfg.hdmi_channel_for("c") == 4
        assert cfg.hdmi_channel_for("sl") == 5
        assert cfg.hdmi_channel_for("sr") == 6

    def test_hdmi_channel_for_case_insensitive(self):
        """hdmi_channel_for is case-insensitive."""
        cfg = Config(DEFAULT_CONFIG.copy())
        assert cfg.hdmi_channel_for("LFE") == 3
        assert cfg.hdmi_channel_for("Center") == 4
        assert cfg.hdmi_channel_for("FL") == 1

    def test_hdmi_channel_for_unknown_returns_none(self):
        """hdmi_channel_for returns None for unknown roles."""
        cfg = Config(DEFAULT_CONFIG.copy())
        assert cfg.hdmi_channel_for("height_left") is None
        assert cfg.hdmi_channel_for("nonexistent") is None

    def test_hdmi_channel_map_user_override(self, tmp_path):
        """User can override the channel map in config.yaml."""
        p = tmp_path / "config.yaml"
        p.write_text(yaml.dump({
            "hdmi_channel_map": {"left": 1, "right": 2, "lfe": 4, "center": 3}
        }))
        cfg = Config.load(p)
        assert cfg.hdmi_channel_for("lfe") == 4
        assert cfg.hdmi_channel_for("center") == 3


class TestActiveInput:
    """Config.active_input resolves a driver-neutral value with legacy fallback."""

    def test_prefers_top_level_key(self):
        cfg = Config({
            "dsp_driver": "camilladsp",
            "active_input": 7,
            "minidsp": {"active_input": 1},
        })
        assert cfg.active_input == 7

    def test_prefers_driver_block_over_minidsp_fallback(self):
        cfg = Config({
            "dsp_driver": "camilladsp",
            "camilladsp": {"active_input": 3},
            "minidsp": {"active_input": 1},
        })
        assert cfg.active_input == 3

    def test_legacy_minidsp_block_on_minidsp_driver_no_warning(self, caplog):
        import logging
        cfg = Config({"dsp_driver": "minidsp", "minidsp": {"active_input": 2}})
        with caplog.at_level(logging.WARNING, logger="calibrate.config"):
            assert cfg.active_input == 2
        assert not any("deprecated" in r.message.lower() for r in caplog.records)

    def test_legacy_minidsp_block_on_camilladsp_driver_warns_once(self, caplog):
        import logging
        import calibrate.config as _cfg_mod
        # Reset the warn cache so the test sees a fresh warning.
        _cfg_mod._WARNED_KEYS.clear()
        cfg = Config({"dsp_driver": "camilladsp", "minidsp": {"active_input": 0}})
        with caplog.at_level(logging.WARNING, logger="calibrate.config"):
            cfg.active_input
            cfg.active_input  # second access — should not warn again
        warnings = [r for r in caplog.records if "deprecated" in r.message.lower()]
        assert len(warnings) == 1

    def test_defaults_to_zero_when_nothing_configured(self):
        cfg = Config({"dsp_driver": "camilladsp"})
        assert cfg.active_input == 0


class TestSubOutputs:
    """sub_outputs / sub_output_labels resolve signal_graph first, then fallbacks."""

    def test_prefers_signal_graph(self):
        cfg = Config({
            "dsp_driver": "camilladsp",
            "signal_graph": {
                "processors": [{"name": "camilla", "driver_ref": "camilladsp", "kind": "dsp"}],
                "transducers": [
                    {"name": "sub_front_left", "role": "sub", "processor_ref": "camilla",
                     "output_index": 5, "safety_profile_ref": "svs"},
                    {"name": "sub_front_right", "role": "sub", "processor_ref": "camilla",
                     "output_index": 6, "safety_profile_ref": "svs"},
                ],
            },
            # Conflicting legacy values must lose to the graph:
            "minidsp": {"output_slots": [{"index": 0, "type": "sub"}]},
            "measurement": {"sub_outputs": [9]},
        })
        assert cfg.sub_outputs == [5, 6]
        assert cfg.sub_output_labels() == [(5, "sub_front_left"), (6, "sub_front_right")]

    def test_falls_back_to_output_slots(self):
        cfg = Config({
            "dsp_driver": "minidsp",
            "minidsp": {"output_slots": [
                {"index": 0, "type": "sub", "label": "Sub L"},
                {"index": 1, "type": "sub"},
                {"index": 2, "type": "unused"},
            ]},
        })
        # No signal_graph block → synthesised graph may or may not carry subs;
        # at minimum the resolved indices must match the typed slots.
        assert set(cfg.sub_outputs) >= {0, 1} or cfg.sub_outputs == [0, 1]
