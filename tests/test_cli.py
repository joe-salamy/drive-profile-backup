"""Tests for CLI entry point."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

from drive_backup.cli import _count_scan_entries, main
from drive_backup.config import Config
from drive_backup.utils import human_size


class TestCliHumanSize:
    def test_bytes(self) -> None:
        assert human_size(100) == "100.0 B"

    def test_kilobytes(self) -> None:
        assert human_size(2048) == "2.0 KB"

    def test_megabytes(self) -> None:
        assert human_size(1024 * 1024) == "1.0 MB"


class TestCliMain:
    def test_count_scan_entries_includes_skipped_files(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "keep.txt").write_text("hello")
        (tmp_path / "Thumbs.db").write_text("skip")

        config = Config(
            profile_name="laptop-a",
            backup_root=str(tmp_path),
            exclude_dirs=[],
            exclude_files=["Thumbs.db"],
            manifest_path=str(tmp_path / "manifest.json"),
        )

        assert _count_scan_entries(config) == 2

    def test_progress_uses_file_count_total(
        self, tmp_path: Path, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        class FakeProgress:
            task_total: int | None = None

            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def __enter__(self) -> "FakeProgress":
                return self

            def __exit__(
                self,
                exc_type: object,
                exc: object,
                traceback: object,
            ) -> None:
                pass

            def add_task(self, description: str, total: int | None = None) -> int:
                self.__class__.task_total = total
                return 1

            def advance(self, task_id: int) -> None:
                pass

        (tmp_path / "test.txt").write_text("hello")
        (tmp_path / "config.yaml").write_text(
            "profile_name: laptop-a\n"
            f"backup_root: {tmp_path}\n"
            "exclude_dirs: []\n"
            "exclude_files: []\n"
            f"manifest_path: {tmp_path / 'manifest.json'}\n"
        )

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("rich.progress.Progress", FakeProgress)

        main(["--dry-run"])

        assert FakeProgress.task_total == 2

    def test_dry_run_completes(
        self, tmp_path: Path, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        (tmp_path / "test.txt").write_text("hello")
        (tmp_path / "config.yaml").write_text(
            "profile_name: laptop-a\n"
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
            "profile_name: laptop-a\n"
            f"backup_root: {tmp_path}\n"
            "exclude_dirs: []\n"
            "exclude_files: []\n"
            f"manifest_path: {tmp_path / 'manifest.json'}\n"
        )

        monkeypatch.chdir(tmp_path)
        main(["--dry-run", "--verbose"])
