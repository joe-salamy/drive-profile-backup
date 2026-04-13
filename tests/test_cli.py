"""Tests for CLI entry point."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

from drive_backup.cli import main
from drive_backup.utils import human_size


class TestCliHumanSize:
    def test_bytes(self) -> None:
        assert human_size(100) == "100.0 B"

    def test_kilobytes(self) -> None:
        assert human_size(2048) == "2.0 KB"

    def test_megabytes(self) -> None:
        assert human_size(1024 * 1024) == "1.0 MB"


class TestCliMain:
    def test_dry_run_completes(
        self, tmp_path: Path, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        (tmp_path / "test.txt").write_text("hello")
        (tmp_path / "config.yaml").write_text(
            f"backup_root: {tmp_path}\n"
            "exclude_dirs: []\n"
            "exclude_files: []\n"
            f"manifest_path: {tmp_path / 'manifest.json'}\n"
        )

        monkeypatch.chdir(tmp_path)
        main(["--dry-run"])

    def test_verbose_dry_run(
        self, tmp_path: Path, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        (tmp_path / "test.txt").write_text("hello")
        (tmp_path / "config.yaml").write_text(
            f"backup_root: {tmp_path}\n"
            "exclude_dirs: []\n"
            "exclude_files: []\n"
            f"manifest_path: {tmp_path / 'manifest.json'}\n"
        )

        monkeypatch.chdir(tmp_path)
        main(["--dry-run", "--verbose"])
