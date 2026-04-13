"""Tests for config loading and defaults."""

import os
import tempfile
from pathlib import Path

import yaml

from drive_backup.config import Config, load_config


class TestConfigDefaults:
    def test_default_backup_root_is_home(self):
        config = Config()
        assert config.backup_root == str(Path.home())

    def test_default_excludes_appdata(self):
        config = Config()
        assert "AppData" in config.exclude_dirs

    def test_default_excludes_venv(self):
        config = Config()
        assert "venv" in config.exclude_dirs
        assert ".venv" in config.exclude_dirs

    def test_default_max_file_size(self):
        config = Config()
        assert config.max_file_size_mb == 500
        assert config.max_file_size_bytes == 500 * 1024 * 1024

    def test_media_has_no_size_limit(self):
        config = Config()
        assert config.get_size_limit_bytes(".jpg") is None
        assert config.get_size_limit_bytes(".mp4") is None
        assert config.get_size_limit_bytes(".wav") is None

    def test_exe_always_skipped(self):
        config = Config()
        assert config.get_size_limit_bytes(".exe") == 0
        assert config.get_size_limit_bytes(".iso") == 0

    def test_regular_file_gets_default_limit(self):
        config = Config()
        assert config.get_size_limit_bytes(".txt") == 500 * 1024 * 1024

    def test_extension_normalization(self):
        config = Config(no_size_limit=["jpg", ".PNG"])
        assert ".jpg" in config.no_size_limit
        assert ".PNG" in config.no_size_limit

    def test_path_expansion(self):
        config = Config(manifest_path="~/test.json")
        assert "~" not in config.manifest_path


class TestLoadConfig:
    def test_load_missing_file_returns_defaults(self):
        config = load_config("/nonexistent/path/config.yaml")
        assert config.backup_root == str(Path.home())

    def test_load_yaml_overrides(self):
        data = {
            "backup_root": "C:\\Test",
            "max_file_size_mb": 100,
            "exclude_dirs": ["custom_dir"],
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            yaml.dump(data, f)
            f.flush()
            config = load_config(f.name)

        os.unlink(f.name)

        assert config.backup_root == "C:\\Test"
        assert config.max_file_size_mb == 100
        assert config.exclude_dirs == ["custom_dir"]

    def test_load_partial_yaml_keeps_other_defaults(self):
        data = {"max_file_size_mb": 200}
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            yaml.dump(data, f)
            f.flush()
            config = load_config(f.name)

        os.unlink(f.name)

        assert config.max_file_size_mb == 200
        assert "AppData" in config.exclude_dirs  # Default preserved
