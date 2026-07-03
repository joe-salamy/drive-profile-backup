"""Tests for CLI entry point."""

from __future__ import annotations

import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

from drive_backup.cli import main
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
    def test_dry_run_progress_enters_before_scanning_with_indeterminate_total(
        self, tmp_path: Path, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        events: list[object] = []

        config = Config(
            profile_name="laptop-a",
            backup_root=str(tmp_path),
            exclude_dirs=[],
            exclude_files=[],
            manifest_path=str(tmp_path / "manifest.json"),
        )

        class FakeProgress:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def __enter__(self) -> "FakeProgress":
                events.append("progress_enter")
                return self

            def __exit__(
                self,
                exc_type: object,
                exc: object,
                traceback: object,
            ) -> None:
                events.append("progress_exit")

            def add_task(self, description: str, total: int | None = None) -> int:
                assert "progress_enter" in events
                events.append(("add_task", description, total))
                return 1

            def advance(self, task_id: int) -> None:
                events.append(("advance", task_id))

        class FakeBackupEngine:
            def __init__(self, config: Config, *, dry_run: bool, full: bool) -> None:
                assert dry_run is True
                assert full is False

            def run(
                self, *, progress_callback: Callable[[object, str], None]
            ) -> dict[str, object]:
                assert "progress_enter" in events
                events.append("engine_run")
                progress_callback(object(), "would_upload:test.txt")
                return {
                    "dry_run": True,
                    "duration_human": "0s",
                    "files_scanned": 1,
                    "files_uploaded": 1,
                    "files_skipped_dedup": 0,
                    "files_skipped_exclusion": 0,
                    "files_skipped_error": 0,
                    "total_size_uploaded_human": "5.0 B",
                    "total_size_eligible_human": "5.0 B",
                    "uploaded_files": [],
                    "extension_breakdown": [],
                    "error_files": [],
                }

        def fail_if_scanned_before_progress(config: Config) -> object:
            events.append("scan_started")
            assert "progress_enter" in events, "scan started before progress"
            return iter(())

        class FakeConsole:
            def print(self, *args: object, **kwargs: object) -> None:
                pass

        class FakeTable:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def add_column(self, *args: object, **kwargs: object) -> None:
                pass

            def add_row(self, *args: object, **kwargs: object) -> None:
                pass

        class FakeColumn:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

        rich_module = types.ModuleType("rich")
        rich_module.__path__ = []
        console_module = types.ModuleType("rich.console")
        progress_module = types.ModuleType("rich.progress")
        table_module = types.ModuleType("rich.table")
        setattr(console_module, "Console", FakeConsole)
        setattr(progress_module, "Progress", FakeProgress)
        setattr(progress_module, "BarColumn", FakeColumn)
        setattr(progress_module, "MofNCompleteColumn", FakeColumn)
        setattr(progress_module, "TextColumn", FakeColumn)
        setattr(progress_module, "TimeElapsedColumn", FakeColumn)
        setattr(table_module, "Table", FakeTable)
        setattr(rich_module, "console", console_module)
        setattr(rich_module, "progress", progress_module)
        setattr(rich_module, "table", table_module)

        monkeypatch.setitem(sys.modules, "rich", rich_module)
        monkeypatch.setitem(sys.modules, "rich.console", console_module)
        monkeypatch.setitem(sys.modules, "rich.progress", progress_module)
        monkeypatch.setitem(sys.modules, "rich.table", table_module)
        monkeypatch.setattr("drive_backup.config.load_config", lambda path: config)
        monkeypatch.setattr("drive_backup.engine.BackupEngine", FakeBackupEngine)
        monkeypatch.setattr(
            "drive_backup.scanner.scan", fail_if_scanned_before_progress
        )

        main(["--dry-run"])

        assert events == [
            "progress_enter",
            ("add_task", "Scanning (dry run)...", None),
            "engine_run",
            ("advance", 1),
            "progress_exit",
        ]

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
