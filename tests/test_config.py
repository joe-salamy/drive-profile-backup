"""Tests for config loading and defaults."""

from pathlib import Path

import pytest
import yaml

from drive_backup.config import Config, load_config


class TestConfigDefaults:
    def test_default_backup_root_is_home(self) -> None:
        config = Config(profile_name="laptop-a")
        assert config.backup_root == str(Path.home())

    def test_default_excludes_appdata(self) -> None:
        config = Config(profile_name="laptop-a")
        assert "AppData" in config.exclude_dirs

    def test_default_excludes_venv(self) -> None:
        config = Config(profile_name="laptop-a")
        assert "venv" in config.exclude_dirs
        assert ".venv" in config.exclude_dirs

    def test_default_max_file_size(self) -> None:
        config = Config(profile_name="laptop-a")
        assert config.max_file_size_mb == 500
        assert config.max_file_size_bytes == 500 * 1024 * 1024

    def test_media_has_no_size_limit(self) -> None:
        config = Config(profile_name="laptop-a")
        assert config.get_size_limit_bytes(".jpg") is None
        assert config.get_size_limit_bytes(".mp4") is None
        assert config.get_size_limit_bytes(".wav") is None

    def test_exe_always_skipped(self) -> None:
        config = Config(profile_name="laptop-a")
        assert config.get_size_limit_bytes(".exe") == 0
        assert config.get_size_limit_bytes(".iso") == 0

    def test_regular_file_gets_default_limit(self) -> None:
        config = Config(profile_name="laptop-a")
        assert config.get_size_limit_bytes(".txt") == 500 * 1024 * 1024

    def test_extension_normalization(self) -> None:
        config = Config(profile_name="laptop-a", no_size_limit=["jpg", ".PNG"])
        assert ".jpg" in config.no_size_limit
        assert ".png" in config.no_size_limit

    def test_path_expansion(self) -> None:
        config = Config(profile_name="laptop-a", manifest_path="~/test.json")
        assert "~" not in config.manifest_path

    def test_profile_derives_manifest_path(self) -> None:
        config = Config(profile_name="laptop-a")
        assert config.profile_name == "laptop-a"
        path_parts = Path(config.manifest_path).parts
        assert path_parts[-4:] == (
            ".drive-backup",
            "profiles",
            "laptop-a",
            "manifest.json",
        )

    def test_profile_preserves_custom_manifest_path(self) -> None:
        config = Config(profile_name="laptop-a", manifest_path="~/custom.json")
        assert config.manifest_path.endswith("custom.json")

    def test_rejects_invalid_profile_name(self) -> None:
        with pytest.raises(ValueError, match="profile_name"):
            Config(profile_name=" ")
        with pytest.raises(ValueError, match="slashes"):
            Config(profile_name="laptop/a")
        with pytest.raises(ValueError, match="control"):
            Config(profile_name="lap\ntop")

    def test_rejects_invalid_numeric_values(self) -> None:
        with pytest.raises(ValueError, match="writes_per_second"):
            Config(profile_name="laptop-a", writes_per_second=0)
        with pytest.raises(ValueError, match="max_retries"):
            Config(profile_name="laptop-a", max_retries=0)
        with pytest.raises(ValueError, match="max_file_size_mb"):
            Config(profile_name="laptop-a", max_file_size_mb=-1)


class TestLoadConfig:
    def test_load_missing_file_requires_profile_name(self) -> None:
        with pytest.raises(ValueError, match="profile_name"):
            load_config("/nonexistent/path/config.yaml")

    def test_load_yaml_overrides(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "profile_name": "laptop-a",
                    "backup_root": "C:\\Test",
                    "max_file_size_mb": 100,
                    "exclude_dirs": ["custom_dir"],
                }
            ),
            encoding="utf-8",
        )

        config = load_config(path)

        assert config.backup_root == "C:\\Test"
        assert config.max_file_size_mb == 100.0
        assert config.exclude_dirs == ["custom_dir"]

    def test_load_partial_yaml_keeps_other_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(
            yaml.safe_dump({"profile_name": "laptop-a", "max_file_size_mb": 200}),
            encoding="utf-8",
        )

        config = load_config(path)

        assert config.max_file_size_mb == 200.0
        assert "AppData" in config.exclude_dirs  # Default preserved

    def test_rejects_unknown_keys_in_sorted_order(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(
            "profile_name: laptop-a\nzebra: true\nexclude_file: [x]\n",
            encoding="utf-8",
        )

        with pytest.raises(
            ValueError,
            match="Unknown configuration keys: exclude_file, zebra",
        ):
            load_config(path)

    @pytest.mark.parametrize(
        ("field_name", "value", "expectation"),
        [
            ("backup_root", [], "expected a string"),
            ("exclude_dirs", "venv", "expected a list of strings"),
            ("exclude_symlinks", 1, "expected a boolean"),
            ("max_retries", True, "expected an integer"),
            ("max_file_size_mb", ".inf", "expected a finite number"),
            (
                "size_limits_by_type",
                {".zip": True},
                "expected a finite number",
            ),
        ],
    )
    def test_rejects_wrong_typed_values(
        self,
        tmp_path: Path,
        field_name: str,
        value: object,
        expectation: str,
    ) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(
            yaml.safe_dump({"profile_name": "laptop-a", field_name: value}),
            encoding="utf-8",
        )

        with pytest.raises(
            ValueError,
            match=rf"Invalid configuration value for '{field_name}': {expectation}",
        ):
            load_config(path)

    def test_rejects_non_mapping_root(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("- profile_name\n- laptop-a\n", encoding="utf-8")

        with pytest.raises(ValueError, match="Configuration root must be a mapping"):
            load_config(path)
