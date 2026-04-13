"""Load and validate backup configuration from YAML."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Config:
    """All backup configuration, with sensible defaults."""

    backup_root: str = ""
    exclude_dirs: list[str] = field(default_factory=lambda: [
        "venv", ".venv", "env",
        ".git", "__pycache__", "node_modules",
        "AppData",
        ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
        ".claude", "scoop",
    ])
    exclude_files: list[str] = field(default_factory=lambda: [
        "NTUSER.DAT*", "ntuser.*",
        "Thumbs.db", "desktop.ini",
        "*.tmp", "*.lnk",
    ])
    exclude_path_patterns: list[str] = field(default_factory=list)
    exclude_specific_files: list[str] = field(default_factory=list)
    exclude_symlinks: bool = True
    max_file_size_mb: float = 500
    size_limits_by_type: dict[str, float] = field(default_factory=lambda: {
        ".iso": 0,
        ".exe": 0,
        ".msi": 0,
    })
    no_size_limit: list[str] = field(default_factory=lambda: [
        ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".heic", ".webp",
        ".mp4", ".mov", ".avi", ".mkv", ".wmv",
        ".mp3", ".wav", ".flac", ".aac", ".ogg",
    ])
    drive_folder_name: str = "Profile Backup"
    manifest_path: str = "~/.drive-backup/manifest.json"
    credentials_path: str = "credentials.json"
    token_path: str = "~/.drive-backup/token.json"
    resumable_threshold_mb: float = 5
    max_retries: int = 3
    writes_per_second: float = 2.5

    def __post_init__(self) -> None:
        if not self.backup_root:
            self.backup_root = str(Path.home())

        # Expand ~ in paths
        self.backup_root = os.path.expanduser(self.backup_root)
        self.manifest_path = os.path.expanduser(self.manifest_path)
        self.token_path = os.path.expanduser(self.token_path)
        self.credentials_path = os.path.expanduser(self.credentials_path)

        # Normalize extensions to lowercase with leading dot
        self.no_size_limit = [
            ext if ext.startswith(".") else f".{ext}"
            for ext in self.no_size_limit
        ]
        self.size_limits_by_type = {
            (ext if ext.startswith(".") else f".{ext}"): limit
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


def load_config(path: str | Path) -> Config:
    """Load configuration from a YAML file, falling back to defaults."""
    path = Path(path)
    if not path.exists():
        return Config()

    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    # Map YAML keys to Config fields
    kwargs: dict = {}
    for key in Config.__dataclass_fields__:
        if key in data:
            kwargs[key] = data[key]

    return Config(**kwargs)
