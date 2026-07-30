"""Load and validate backup configuration from YAML."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_MANIFEST_PATH = "~/.drive-backup/manifest.json"
MACHINE_STATE_COLLECTORS = (
    "system",
    "windows_apps",
    "package_managers",
    "developer_tools",
    "windows_features",
    "services",
    "scheduled_tasks",
    "drivers",
    "network",
    "environment",
    "wsl",
)


@dataclass
class Config:
    """All backup configuration, with sensible defaults."""

    backup_root: str = ""
    exclude_dirs: list[str] = field(
        default_factory=lambda: [
            "venv",
            ".venv",
            "env",
            ".git",
            "__pycache__",
            "node_modules",
            ".next",
            ".turbo",
            ".parcel-cache",
            ".svelte-kit",
            ".nuxt",
            ".vite",
            ".angular",
            ".serverless",
            ".webpack",
            ".docusaurus",
            ".sass-cache",
            ".jekyll-cache",
            "dist",
            "build",
            "coverage",
            "htmlcov",
            "*.egg-info",
            "AppData",
            ".local",
            ".cache",
            "cache",
            ".npm",
            ".pnpm-store",
            ".yarn",
            ".gradle",
            ".hypothesis",
            ".ipynb_checkpoints",
            ".eggs",
            ".pyre",
            ".pytype",
            ".vscode",
            ".tox",
            ".nox",
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
            ".claude",
            ".codex",
            ".omp",
            "scoop",
        ]
    )
    exclude_files: list[str] = field(
        default_factory=lambda: [
            "NTUSER.DAT*",
            "ntuser.*",
            "Thumbs.db",
            "desktop.ini",
            "*.tmp",
            "*.lnk",
            "*.pyc",
            "*.pyo",
            "*.tsbuildinfo",
            ".coverage",
            ".coverage.*",
            "*.cache",
            ".eslintcache",
            ".stylelintcache",
            ".dmypy.json",
            "coverage.xml",
        ]
    )
    exclude_path_patterns: list[str] = field(default_factory=list)
    exclude_specific_files: list[str] = field(default_factory=list)
    exclude_symlinks: bool = True
    max_file_size_mb: float = 500
    size_limits_by_type: dict[str, float] = field(
        default_factory=lambda: {
            ".iso": 0,
            ".exe": 0,
            ".msi": 0,
        }
    )
    no_size_limit: list[str] = field(
        default_factory=lambda: [
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".bmp",
            ".heic",
            ".webp",
            ".mp4",
            ".mov",
            ".avi",
            ".mkv",
            ".wmv",
            ".mp3",
            ".wav",
            ".flac",
            ".aac",
            ".ogg",
        ]
    )
    profile_name: str = ""
    drive_parent_folder_name: str = "Profile Backups"
    manifest_path: str = DEFAULT_MANIFEST_PATH
    credentials_path: str = "credentials.json"
    token_path: str = "~/.drive-backup/token.json"
    resumable_threshold_mb: float = 5
    max_retries: int = 3
    writes_per_second: float = 2.5
    machine_state_collectors: list[str] = field(
        default_factory=lambda: list(MACHINE_STATE_COLLECTORS)
    )

    def __post_init__(self) -> None:
        if not self.backup_root:
            self.backup_root = str(Path.home())

        self.profile_name = self.profile_name.strip()
        _validate_profile_name(self.profile_name)
        if self.manifest_path == DEFAULT_MANIFEST_PATH:
            self.manifest_path = (
                f"~/.drive-backup/profiles/{self.profile_name}/manifest.json"
            )

        if self.max_file_size_mb < 0:
            raise ValueError("max_file_size_mb must be non-negative")
        if self.resumable_threshold_mb < 0:
            raise ValueError("resumable_threshold_mb must be non-negative")
        if self.max_retries < 1:
            raise ValueError("max_retries must be at least 1")
        if self.writes_per_second <= 0:
            raise ValueError("writes_per_second must be greater than 0")
        for extension, limit in self.size_limits_by_type.items():
            if limit < 0:
                raise ValueError(f"size limit for {extension!r} must be non-negative")

        # Expand ~ in paths
        self.backup_root = os.path.expanduser(self.backup_root)
        self.manifest_path = os.path.expanduser(self.manifest_path)
        self.token_path = os.path.expanduser(self.token_path)
        self.credentials_path = os.path.expanduser(self.credentials_path)

        # Normalize extensions to lowercase with leading dot
        self.no_size_limit = [
            (ext if ext.startswith(".") else f".{ext}").lower()
            for ext in self.no_size_limit
        ]
        self.size_limits_by_type = {
            (ext if ext.startswith(".") else f".{ext}").lower(): limit
            for ext, limit in self.size_limits_by_type.items()
        }

    @property
    def max_file_size_bytes(self) -> int:
        return int(self.max_file_size_mb * 1024 * 1024)

    @property
    def resumable_threshold_bytes(self) -> int:
        return int(self.resumable_threshold_mb * 1024 * 1024)

    def get_size_limit_bytes(self, extension: str) -> int | None:
        """Return the size limit in bytes for a given file extension.

        Returns None if the file type has no size limit (media files).
        Returns 0 if the file type should always be skipped.
        """
        ext = extension.lower()
        if ext in self.no_size_limit:
            return None  # No limit
        if ext in self.size_limits_by_type:
            limit_mb = self.size_limits_by_type[ext]
            if limit_mb == 0:
                return 0  # Skip entirely
            return int(limit_mb * 1024 * 1024)
        return self.max_file_size_bytes


def _validate_profile_name(profile_name: str) -> None:
    """Validate a profile name for Drive and local state paths."""
    if not profile_name:
        raise ValueError("profile_name must not be empty")
    if "/" in profile_name or "\\" in profile_name:
        raise ValueError("profile_name must not contain slashes")
    if any(ord(char) < 32 for char in profile_name):
        raise ValueError("profile_name must not contain control characters")


_STRING_FIELDS = {
    "backup_root",
    "profile_name",
    "drive_parent_folder_name",
    "manifest_path",
    "credentials_path",
    "token_path",
}
_STRING_LIST_FIELDS = {
    "exclude_dirs",
    "exclude_files",
    "exclude_path_patterns",
    "exclude_specific_files",
    "no_size_limit",
    "machine_state_collectors",
}
_NUMBER_FIELDS = {
    "max_file_size_mb",
    "resumable_threshold_mb",
    "writes_per_second",
}
_CONFIG_FIELDS = (
    _STRING_FIELDS
    | _STRING_LIST_FIELDS
    | _NUMBER_FIELDS
    | {"exclude_symlinks", "max_retries", "size_limits_by_type"}
)


def _invalid_value(field_name: str, expectation: str) -> ValueError:
    return ValueError(f"Invalid configuration value for '{field_name}': {expectation}")


def _finite_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _invalid_value(field_name, "expected a finite number")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise _invalid_value(field_name, "expected a finite number")
    return normalized


def _validate_config_values(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("Configuration root must be a mapping")

    unknown_keys = sorted((str(key) for key in data if key not in _CONFIG_FIELDS))
    if unknown_keys:
        raise ValueError(f"Unknown configuration keys: {', '.join(unknown_keys)}")

    values: dict[str, Any] = {}
    for field_name, value in data.items():
        if field_name in _STRING_FIELDS:
            if not isinstance(value, str):
                raise _invalid_value(field_name, "expected a string")
            values[field_name] = value
        elif field_name in _STRING_LIST_FIELDS:
            if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value
            ):
                raise _invalid_value(field_name, "expected a list of strings")
            values[field_name] = value
        elif field_name == "exclude_symlinks":
            if type(value) is not bool:
                raise _invalid_value(field_name, "expected a boolean")
            values[field_name] = value
        elif field_name == "max_retries":
            if isinstance(value, bool) or not isinstance(value, int):
                raise _invalid_value(field_name, "expected an integer")
            values[field_name] = value
        elif field_name in _NUMBER_FIELDS:
            values[field_name] = _finite_number(value, field_name)
        else:
            if not isinstance(value, dict) or not all(
                isinstance(key, str) for key in value
            ):
                raise _invalid_value(
                    field_name, "expected a mapping of strings to finite numbers"
                )
            values[field_name] = {
                key: _finite_number(limit, field_name) for key, limit in value.items()
            }
    collectors = values.get("machine_state_collectors")
    if collectors is not None:
        unknown = sorted(set(collectors) - set(MACHINE_STATE_COLLECTORS))
        duplicates = sorted(
            name for name in set(collectors) if collectors.count(name) > 1
        )
        if unknown or duplicates:
            problems = []
            if unknown:
                problems.append(f"unknown names: {', '.join(unknown)}")
            if duplicates:
                problems.append(f"duplicate names: {', '.join(duplicates)}")
            valid = ", ".join(MACHINE_STATE_COLLECTORS)
            raise ValueError(
                "Invalid configuration value for 'machine_state_collectors': "
                f"{'; '.join(problems)}; valid collectors: {valid}"
            )
    return values


def load_config(path: str | Path) -> Config:
    """Load configuration from a YAML file, falling back to defaults."""
    path = Path(path)
    if not path.exists():
        return Config()

    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    return Config(**_validate_config_values({} if data is None else data))
