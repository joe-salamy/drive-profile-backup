"""Tests for CLI entry point."""

from __future__ import annotations

import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Self

import pytest

from drive_backup.cli import _print_summary, main
from drive_backup.config import Config
from drive_backup.engine import ProgressEvent, ProgressKind
from drive_backup.report import BackupReport
from drive_backup.utils import human_size


def _minimal_report(**overrides: object) -> BackupReport:
    report = {
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
        "prune_enabled": False,
        "prune_mode": "flag",
        "files_pruned": 0,
        "files_prune_failed": 0,
        "total_size_pruned_human": "0.0 B",
        "pruned_files": [],
        "prune_skipped_reason": "",
        "prune_error_files": [],
        "manifest_snapshot_downloaded": False,
        "manifest_snapshot_uploaded": False,
        "manifest_snapshot_error": "",
        "machine_state_refreshed": False,
        "machine_state_collectors": [],
    }
    report.update(overrides)
    return report  # type: ignore[return-value]


class TestCliHumanSize:
    def test_bytes(self) -> None:
        assert human_size(100) == "100.0 B"

    def test_kilobytes(self) -> None:
        assert human_size(2048) == "2.0 KB"

    def test_megabytes(self) -> None:
        assert human_size(1024 * 1024) == "1.0 MB"


class TestCliMain:
    def test_dry_run_progress_enters_before_scanning_with_indeterminate_total(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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

            def __enter__(self) -> Self:
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
            def __init__(
                self,
                config: Config,
                *,
                dry_run: bool,
                full: bool,
                prune: bool,
                prune_mode: str,
                collect_machine_state_snapshot: bool,
            ) -> None:
                assert dry_run is True
                assert full is False
                assert prune is False
                assert prune_mode == "flag"
                assert collect_machine_state_snapshot is True

            def run(
                self,
                *,
                progress_callback: Callable[[object, ProgressEvent], None],
            ) -> BackupReport:
                assert "progress_enter" in events
                events.append("engine_run")
                progress_callback(
                    object(),
                    ProgressEvent(ProgressKind.WOULD_UPLOAD, "test.txt"),
                )
                return _minimal_report()

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
        console_module.Console = FakeConsole
        progress_module.Progress = FakeProgress
        progress_module.BarColumn = FakeColumn
        progress_module.MofNCompleteColumn = FakeColumn
        progress_module.TextColumn = FakeColumn
        progress_module.TimeElapsedColumn = FakeColumn
        table_module.Table = FakeTable
        rich_module.console = console_module
        rich_module.progress = progress_module
        rich_module.table = table_module

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
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
        main(["--dry-run", "--skip-machine-state"])

    def test_manifest_failure_is_concise(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text('{"version": 2, "files": {}}', encoding="utf-8")
        (tmp_path / "config.yaml").write_text(
            "profile_name: laptop-a\n"
            f"backup_root: {tmp_path}\n"
            "exclude_dirs: []\n"
            "exclude_files: []\n"
            f"manifest_path: {manifest_path}\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)

        with pytest.raises(SystemExit, match="1"):
            main([])

        assert "Backup failed:" in capsys.readouterr().out

    def test_verbose_dry_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
        main(["--dry-run", "--verbose", "--skip-machine-state"])

    def test_help_includes_skip_machine_state(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit, match="0"):
            main(["--help"])

        assert (
            "Do not refresh generated machine-state inventories before this backup"
            in " ".join(capsys.readouterr().out.split())
        )

    @pytest.mark.parametrize(
        ("extra_args", "expected_collection"),
        [([], True), (["--skip-machine-state"], False)],
    )
    def test_prune_flag_is_passed_to_engine(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        extra_args: list[str],
        expected_collection: bool,
    ) -> None:
        captured: dict[str, bool | str] = {}
        config = Config(
            profile_name="laptop-a",
            backup_root=str(tmp_path),
            exclude_dirs=[],
            exclude_files=[],
            manifest_path=str(tmp_path / "manifest.json"),
        )

        class FakeBackupEngine:
            def __init__(
                self,
                config: Config,
                *,
                dry_run: bool,
                full: bool,
                prune: bool,
                prune_mode: str,
                collect_machine_state_snapshot: bool,
            ) -> None:
                captured["dry_run"] = dry_run
                captured["full"] = full
                captured["prune"] = prune
                captured["prune_mode"] = prune_mode
                captured["collect_machine_state_snapshot"] = (
                    collect_machine_state_snapshot
                )

            def run(
                self,
                *,
                progress_callback: Callable[[object, ProgressEvent], None],
            ) -> BackupReport:
                return _minimal_report(dry_run=True, prune_enabled=True)

        monkeypatch.setattr("drive_backup.config.load_config", lambda path: config)
        monkeypatch.setattr("drive_backup.engine.BackupEngine", FakeBackupEngine)

        main(["--dry-run", "--prune", *extra_args])

        assert captured == {
            "dry_run": True,
            "full": False,
            "prune": True,
            "prune_mode": "flag",
            "collect_machine_state_snapshot": expected_collection,
        }

    def test_prune_trash_passes_trash_mode_to_engine(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, bool | str] = {}
        config = Config(
            profile_name="laptop-a",
            backup_root=str(tmp_path),
            exclude_dirs=[],
            exclude_files=[],
            manifest_path=str(tmp_path / "manifest.json"),
        )

        class FakeBackupEngine:
            def __init__(
                self,
                config: Config,
                *,
                dry_run: bool,
                full: bool,
                prune: bool,
                prune_mode: str,
                collect_machine_state_snapshot: bool,
            ) -> None:
                captured["dry_run"] = dry_run
                captured["full"] = full
                captured["prune"] = prune
                captured["prune_mode"] = prune_mode

            def run(
                self,
                *,
                progress_callback: Callable[[object, ProgressEvent], None],
            ) -> BackupReport:
                return _minimal_report(
                    dry_run=True, prune_enabled=True, prune_mode="trash"
                )

        monkeypatch.setattr("drive_backup.config.load_config", lambda path: config)
        monkeypatch.setattr("drive_backup.engine.BackupEngine", FakeBackupEngine)

        main(["--dry-run", "--prune-trash"])

        assert captured == {
            "dry_run": True,
            "full": False,
            "prune": True,
            "prune_mode": "trash",
        }

    def test_prune_and_prune_trash_are_mutually_exclusive(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main(["--prune", "--prune-trash"])

        assert exc_info.value.code != 0

    def test_restore_requires_output(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main(["--restore"])

        assert exc_info.value.code != 0

    def test_restore_rejects_backup_flags(self) -> None:
        for conflicting in (["--full"], ["--prune"], ["--prune-trash"]):
            with pytest.raises(SystemExit) as exc_info:
                main(["--restore", "--output", "/tmp/out", *conflicting])

            assert exc_info.value.code != 0

    def test_restore_prints_summary_without_constructing_engine(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        config = Config(
            profile_name="laptop-a",
            backup_root=str(tmp_path),
            exclude_dirs=[],
            exclude_files=[],
            manifest_path=str(tmp_path / "manifest.json"),
        )
        output_dir = tmp_path / "restored"
        result = {
            "profile_name": "laptop-a",
            "output_dir": str(output_dir),
            "files_total": 3,
            "files_restored": 1,
            "files_skipped_pruned": 1,
            "files_skipped_existing": 1,
            "files_failed": 0,
            "bytes_restored": 2048,
            "pruned_files": ["old/gone.txt"],
            "errors": [],
        }

        class FakeBackupEngine:
            def __init__(self, *args: object, **kwargs: object) -> None:
                raise AssertionError("engine must not be built during restore")

        monkeypatch.setattr("drive_backup.config.load_config", lambda path: config)
        monkeypatch.setattr("drive_backup.engine.BackupEngine", FakeBackupEngine)
        monkeypatch.setattr(
            "drive_backup.restore.restore_backup",
            lambda config, output_dir, dry_run=False, force=False: result,
        )

        main(["--restore", "--output", str(output_dir)])

        output = capsys.readouterr().out
        assert "RESTORE - downloading non-pruned files" in output
        assert "Files restored" in output
        assert "Files skipped (pruned)" in output
        assert "Files skipped (existing)" in output

    def test_verbose_prune_lists_all_would_prune_files(self) -> None:
        from rich.console import Console

        console = Console(record=True, width=120)
        report = _minimal_report(
            dry_run=True,
            prune_enabled=True,
            files_pruned=2,
            total_size_pruned_human="3.0 KB",
            pruned_files=[
                {
                    "relative_path": "z-old.txt",
                    "drive_file_id": "drive_z",
                    "size_bytes": 1024,
                    "size_human": "1.0 KB",
                },
                {
                    "relative_path": "a-old.txt",
                    "drive_file_id": "drive_a",
                    "size_bytes": 2048,
                    "size_human": "2.0 KB",
                },
            ],
        )

        _print_summary(console, report, verbose=True)
        output = console.export_text()

        assert "Files to prune:" in output
        assert "a-old.txt" in output
        assert "z-old.txt" in output

    def test_full_and_prune_are_rejected(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main(["--full", "--prune"])

        assert exc_info.value.code != 0

    @pytest.mark.parametrize(
        ("dry_run", "expected_files_label", "expected_size_label"),
        [
            (True, "Files to prune", "Size to prune"),
            (False, "Files pruned", "Size pruned"),
        ],
    )
    def test_print_summary_includes_prune_rows(
        self,
        dry_run: bool,
        expected_files_label: str,
        expected_size_label: str,
    ) -> None:
        from rich.console import Console

        console = Console(record=True, width=120)
        report = _minimal_report(
            dry_run=dry_run,
            prune_enabled=True,
            files_uploaded=0,
            files_pruned=2,
            total_size_pruned_human="3.0 KB",
            pruned_files=[
                {
                    "relative_path": "old/file.txt",
                    "drive_file_id": "drive_old",
                    "size_bytes": 3072,
                    "size_human": "3.0 KB",
                }
            ],
        )

        _print_summary(console, report)
        output = console.export_text()

        assert expected_files_label in output
        assert expected_size_label in output


def test_print_summary_includes_machine_state_counts_and_warnings() -> None:
    from rich.console import Console

    console = Console(record=True, width=120)
    report = _minimal_report(
        machine_state_refreshed=True,
        machine_state_collectors=[
            {
                "name": "system",
                "status": "succeeded",
                "output_file": "_machine_state/system.json",
                "warnings": [],
                "previous_output_retained": False,
            },
            {
                "name": "wsl",
                "status": "partial",
                "output_file": "_machine_state/wsl.json",
                "warnings": ["one distro failed"],
                "previous_output_retained": False,
            },
            {
                "name": "services",
                "status": "failed",
                "output_file": None,
                "warnings": ["access denied"],
                "previous_output_retained": False,
            },
        ],
    )

    _print_summary(console, report)
    output = console.export_text()

    assert "1 succeeded, 1 partial, 1 failed" in output
    assert "wsl: one distro failed" in output
    assert "services: access denied" in output
