"""Tests for the backup engine using mocks for Drive API."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock

import pytest

import drive_backup.engine as engine_module
from drive_backup.config import Config
from drive_backup.dedup import Manifest, compute_md5
from drive_backup.engine import (
    BackupEngine,
    ManifestProgressError,
    ProgressEvent,
    ProgressKind,
    _format_mtime,
)
from drive_backup.machine_state import CollectorOutcome, CollectorStatus
from drive_backup.report import BackupReport
from drive_backup.scanner import FileEntry
from tests.file_helpers import write_tree


class _FakeHttpError(Exception):
    """Stand-in for googleapiclient's HttpError: only resp.status matters."""

    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(message or f"HTTP {status}")
        self.resp = MagicMock(status=status)


class TestFormatMtime:
    def test_zero_returns_empty(self) -> None:
        assert _format_mtime(0) == ""

    def test_formats_timestamp(self) -> None:
        result = _format_mtime(1704067200.0)  # 2024-01-01 00:00:00 UTC
        assert "2024-01-01" in result


class TestBackupEngineDryRun:
    def test_dry_run_scans_without_uploading(self, tmp_path: Path) -> None:
        write_tree(
            tmp_path,
            {"file1.txt": "hello", "sub/file2.txt": "world"},
        )
        manifest_path = tmp_path / "manifest.json"
        config = Config(
            profile_name="laptop-a",
            backup_root=str(tmp_path),
            exclude_dirs=[],
            exclude_files=[],
            manifest_path=str(manifest_path),
        )

        report = BackupEngine(
            config, dry_run=True, collect_machine_state_snapshot=False
        ).run()

        assert report["dry_run"] is True
        assert report["files_scanned"] >= 2
        assert report["files_uploaded"] >= 2
        assert not manifest_path.exists()

    def test_dry_run_calls_progress_callback(self, tmp_path: Path) -> None:
        write_tree(tmp_path, {"file.txt": "data"})
        config = Config(
            profile_name="laptop-a",
            backup_root=str(tmp_path),
            exclude_dirs=[],
            exclude_files=[],
            manifest_path=str(tmp_path / "manifest.json"),
        )
        calls: list[tuple[str, ProgressEvent]] = []

        def callback(file: FileEntry, event: ProgressEvent) -> None:
            calls.append((file.relative_path, event))

        BackupEngine(config, dry_run=True, collect_machine_state_snapshot=False).run(
            progress_callback=callback
        )

        assert len(calls) >= 1
        assert any(
            event.kind is ProgressKind.WOULD_UPLOAD and event.reason == "new"
            for _, event in calls
        )

    def test_dry_run_skips_excluded_files(self, tmp_path: Path) -> None:
        write_tree(
            tmp_path,
            {
                "keep.txt": "keep",
                "Thumbs.db": "skip",
            },
        )
        config = Config(
            profile_name="laptop-a",
            backup_root=str(tmp_path),
            exclude_dirs=[],
            exclude_files=["Thumbs.db"],
            manifest_path=str(tmp_path / "manifest.json"),
        )

        report = BackupEngine(
            config, dry_run=True, collect_machine_state_snapshot=False
        ).run()

        assert report["files_skipped_exclusion"] == 1

    def test_dedup_skips_unchanged_files(self, tmp_path: Path) -> None:
        backup_root = tmp_path / "backup"
        state_dir = tmp_path / "state"
        write_tree(backup_root, {"file.txt": "content"})
        manifest_path = state_dir / "manifest.json"
        config = Config(
            profile_name="laptop-a",
            backup_root=str(backup_root),
            exclude_dirs=[],
            exclude_files=[],
            manifest_path=str(manifest_path),
        )
        stat = (backup_root / "file.txt").stat()
        manifest = Manifest()
        manifest.set(
            relative_path="file.txt",
            md5="abc",
            size=stat.st_size,
            mtime=stat.st_mtime,
            drive_file_id="id",
            drive_parent_id="pid",
        )
        manifest.save(str(manifest_path))

        report = BackupEngine(
            config, dry_run=True, collect_machine_state_snapshot=False
        ).run()

        assert report["files_skipped_dedup"] == 1
        assert report["files_uploaded"] == 0

    def test_full_mode_ignores_manifest(self, tmp_path: Path) -> None:
        backup_root = tmp_path / "backup"
        state_dir = tmp_path / "state"
        write_tree(backup_root, {"file.txt": "content"})
        manifest_path = state_dir / "manifest.json"
        config = Config(
            profile_name="laptop-a",
            backup_root=str(backup_root),
            exclude_dirs=[],
            exclude_files=[],
            manifest_path=str(manifest_path),
        )
        stat = (backup_root / "file.txt").stat()
        manifest = Manifest()
        manifest.set(
            relative_path="file.txt",
            md5="abc",
            size=stat.st_size,
            mtime=stat.st_mtime,
            drive_file_id="id",
            drive_parent_id="pid",
        )
        manifest.save(str(manifest_path))

        report = BackupEngine(
            config, dry_run=True, full=True, collect_machine_state_snapshot=False
        ).run()

        assert report["files_uploaded"] == 1

    def test_prune_disabled_ignores_stale_manifest_entries(
        self, tmp_path: Path
    ) -> None:
        backup_root = tmp_path / "backup"
        backup_root.mkdir()
        manifest_path = tmp_path / "state" / "manifest.json"
        manifest = Manifest()
        manifest.set(
            relative_path="old/file.txt",
            md5="abc",
            size=10,
            mtime=1.0,
            drive_file_id="drive_old",
            drive_parent_id="parent",
        )
        manifest.save(str(manifest_path))
        config = Config(
            profile_name="laptop-a",
            backup_root=str(backup_root),
            exclude_dirs=[],
            exclude_files=[],
            manifest_path=str(manifest_path),
        )

        report = BackupEngine(
            config, dry_run=True, prune=False, collect_machine_state_snapshot=False
        ).run()

        assert report["files_pruned"] == 0
        assert report["pruned_files"] == []

    def test_dry_run_prune_reports_stale_entries_without_saving_manifest(
        self, tmp_path: Path
    ) -> None:
        backup_root = tmp_path / "backup"
        write_tree(backup_root, {"keep.txt": "keep"})
        manifest_path = tmp_path / "state" / "manifest.json"
        stat = (backup_root / "keep.txt").stat()
        manifest = Manifest()
        manifest.set(
            relative_path="keep.txt",
            md5="abc",
            size=stat.st_size,
            mtime=stat.st_mtime,
            drive_file_id="drive_keep",
            drive_parent_id="parent",
        )
        manifest.set(
            relative_path="old/moved.txt",
            md5="def",
            size=20,
            mtime=1.0,
            drive_file_id="drive_old",
            drive_parent_id="parent",
        )
        manifest.save(str(manifest_path))
        config = Config(
            profile_name="laptop-a",
            backup_root=str(backup_root),
            exclude_dirs=[],
            exclude_files=[],
            manifest_path=str(manifest_path),
        )

        report = BackupEngine(
            config,
            dry_run=True,
            prune=True,
            prune_mode="trash",
            collect_machine_state_snapshot=False,
        ).run()

        assert report["files_pruned"] == 1
        assert report["pruned_files"][0]["relative_path"] == "old/moved.txt"
        assert Manifest.load(str(manifest_path)).get("old/moved.txt") is not None


class TestBackupEngineMachineState:
    def test_collection_runs_after_manifest_load_before_drive_and_scan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events: list[str] = []
        backup_root = tmp_path / "backup"
        backup_root.mkdir()
        config = Config(
            profile_name="laptop-a",
            backup_root=str(backup_root),
            exclude_dirs=[],
            exclude_files=[],
            manifest_path=str(tmp_path / "state" / "manifest.json"),
            machine_state_collectors=[],
        )
        real_load = Manifest.load

        def load_manifest(path: str) -> Manifest:
            events.append("manifest")
            return real_load(path)

        def collect(root: str, names: list[str]) -> list[CollectorOutcome]:
            events.append("collect")
            return []

        def scan_files(config: Config) -> list[FileEntry]:
            events.append("scan")
            return []

        class FakeDrive:
            def authenticate(self) -> None:
                events.append("authenticate")

            def get_or_create_folder(
                self, name: str, parent_id: str | None = None
            ) -> str:
                return f"{name}_id"

            def upload_file(
                self, local_path: str, parent_id: str, resumable: bool = False
            ) -> dict[str, str]:
                return {"id": "report_id", "md5Checksum": "report_md5"}

        monkeypatch.setattr(Manifest, "load", load_manifest)
        monkeypatch.setattr(engine_module, "collect_machine_state", collect)
        monkeypatch.setattr(engine_module, "scan", scan_files)
        monkeypatch.setattr("drive_backup.drive_api.DriveAPI", lambda **_: FakeDrive())

        BackupEngine(config).run()

        assert events[:4] == ["manifest", "collect", "authenticate", "scan"]

    def test_dry_run_refreshes_and_scans_generated_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backup_root = tmp_path / "backup"
        backup_root.mkdir()
        manifest_path = tmp_path / "state" / "manifest.json"
        config = Config(
            profile_name="laptop-a",
            backup_root=str(backup_root),
            exclude_dirs=[],
            exclude_files=[],
            manifest_path=str(manifest_path),
            machine_state_collectors=["system"],
        )

        def collect(root: str, names: list[str]) -> list[CollectorOutcome]:
            output = Path(root) / "_machine_state" / "system.json"
            output.parent.mkdir()
            output.write_text("{}", encoding="utf-8")
            return [
                CollectorOutcome(
                    name="system",
                    status=CollectorStatus.SUCCEEDED,
                    output_file="_machine_state/system.json",
                    warnings=(),
                    previous_output_retained=False,
                )
            ]

        monkeypatch.setattr(engine_module, "collect_machine_state", collect)

        report = BackupEngine(config, dry_run=True).run()

        assert report["machine_state_refreshed"] is True
        assert report["files_scanned"] == 1
        assert report["uploaded_files"][0]["relative_path"] == (
            "_machine_state/system.json"
        )
        assert not manifest_path.exists()

    def test_skip_refresh_still_scans_existing_snapshot(self, tmp_path: Path) -> None:
        backup_root = tmp_path / "backup"
        output = backup_root / "_machine_state" / "system.json"
        output.parent.mkdir(parents=True)
        output.write_text("{}", encoding="utf-8")
        config = Config(
            profile_name="laptop-a",
            backup_root=str(backup_root),
            exclude_dirs=[],
            exclude_files=[],
            manifest_path=str(tmp_path / "state" / "manifest.json"),
        )

        report = BackupEngine(
            config, dry_run=True, collect_machine_state_snapshot=False
        ).run()

        assert report["machine_state_refreshed"] is False
        assert report["machine_state_collectors"] == []
        assert report["uploaded_files"][0]["relative_path"] == (
            "_machine_state/system.json"
        )

    def test_failed_collector_is_reported_without_file_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backup_root = tmp_path / "backup"
        backup_root.mkdir()
        config = Config(
            profile_name="laptop-a",
            backup_root=str(backup_root),
            exclude_dirs=[],
            exclude_files=[],
            manifest_path=str(tmp_path / "state" / "manifest.json"),
            machine_state_collectors=["system"],
        )
        monkeypatch.setattr(
            engine_module,
            "collect_machine_state",
            lambda root, names: [
                CollectorOutcome(
                    name="system",
                    status=CollectorStatus.FAILED,
                    output_file=None,
                    warnings=("powershell unavailable",),
                    previous_output_retained=False,
                )
            ],
        )

        report = BackupEngine(config, dry_run=True).run()

        assert report["files_skipped_error"] == 0
        assert report["error_files"] == []
        assert report["machine_state_collectors"][0]["status"] == "failed"

    def test_unexpected_refresh_failure_continues_and_protects_prune(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backup_root = tmp_path / "backup"
        backup_root.mkdir()
        ordinary_file = backup_root / "ordinary.txt"
        ordinary_file.write_text("backup me", encoding="utf-8")
        manifest_path = tmp_path / "state" / "manifest.json"
        manifest = Manifest()
        manifest.set(
            relative_path="_machine_state/system.json",
            md5="abc",
            size=10,
            mtime=1.0,
            drive_file_id="system_drive",
            drive_parent_id="parent",
        )
        manifest.save(str(manifest_path))
        config = Config(
            profile_name="laptop-a",
            backup_root=str(backup_root),
            exclude_dirs=[],
            exclude_files=[],
            manifest_path=str(manifest_path),
            machine_state_collectors=["system"],
        )

        def fail_refresh(root: str, names: list[str]) -> list[CollectorOutcome]:
            raise OSError("collector subsystem failure")

        monkeypatch.setattr(engine_module, "collect_machine_state", fail_refresh)

        report = BackupEngine(config, dry_run=True, prune=True).run()

        assert report["machine_state_refreshed"] is True
        assert [row["name"] for row in report["machine_state_collectors"]] == [
            "system",
            "snapshot",
        ]
        assert all(
            row["status"] == "failed" for row in report["machine_state_collectors"]
        )
        assert report["files_scanned"] == 1
        assert report["files_pruned"] == 0

    def test_enabled_failed_collector_is_prune_protected_but_disabled_is_pruned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeDrive:
            def __init__(self) -> None:
                self.trash_calls: list[str] = []

            def authenticate(self) -> None:
                pass

            def get_or_create_folder(
                self, name: str, parent_id: str | None = None
            ) -> str:
                return f"{name}_id"

            def upload_file(
                self, local_path: str, parent_id: str, resumable: bool = False
            ) -> dict[str, str]:
                return {"id": "report_id", "md5Checksum": "report_md5"}

            def trash_file(self, file_id: str) -> dict[str, str]:
                self.trash_calls.append(file_id)
                return {"id": file_id}

        backup_root = tmp_path / "backup"
        backup_root.mkdir()
        manifest_path = tmp_path / "state" / "manifest.json"
        manifest = Manifest()
        for relative_path, drive_id in (
            ("_machine_state/system.json", "system_drive"),
            ("_machine_state/services.json", "services_drive"),
        ):
            manifest.set(
                relative_path=relative_path,
                md5="abc",
                size=10,
                mtime=1.0,
                drive_file_id=drive_id,
                drive_parent_id="parent",
            )
        manifest.save(str(manifest_path))
        config = Config(
            profile_name="laptop-a",
            backup_root=str(backup_root),
            exclude_dirs=[],
            exclude_files=[],
            manifest_path=str(manifest_path),
            machine_state_collectors=["system"],
        )
        monkeypatch.setattr(
            engine_module,
            "collect_machine_state",
            lambda root, names: [
                CollectorOutcome(
                    name="system",
                    status=CollectorStatus.FAILED,
                    output_file=None,
                    warnings=("failed",),
                    previous_output_retained=False,
                )
            ],
        )
        drive = FakeDrive()
        monkeypatch.setattr("drive_backup.drive_api.DriveAPI", lambda **_: drive)

        report = BackupEngine(config, prune=True, prune_mode="trash").run()

        assert drive.trash_calls == ["services_drive"]
        assert report["files_pruned"] == 1
        loaded = Manifest.load(str(manifest_path))
        assert loaded.get("_machine_state/system.json") is not None
        assert loaded.get("_machine_state/services.json") is None


class TestBackupEngineUploadErrors:
    def test_upload_error_is_captured(self, tmp_path: Path) -> None:
        file_path = tmp_path / "file.txt"
        file_path.write_text("test", encoding="utf-8")
        config = Config(
            profile_name="laptop-a",
            backup_root=str(tmp_path),
            exclude_dirs=[],
            exclude_files=[],
            manifest_path=str(tmp_path / "manifest.json"),
        )
        engine = BackupEngine(
            config, dry_run=False, collect_machine_state_snapshot=False
        )
        mock_drive = MagicMock()
        mock_drive.get_or_create_folder.return_value = "root_id"
        mock_drive.upload_file.side_effect = RuntimeError("Upload failed")
        mock_drive.find_file_by_name_and_parent.return_value = None
        engine.drive = mock_drive
        engine._root_folder_id = "root_id"
        stat = file_path.stat()
        entry = FileEntry(
            path=str(file_path),
            relative_path="file.txt",
            size=stat.st_size,
            mtime=stat.st_mtime,
        )

        engine._process_file(entry)

        assert engine.stats.files_skipped_error == 1
        assert len(engine.stats.error_files) == 1
        assert "Upload failed" in engine.stats.error_files[0].error

    def test_file_deleted_during_backup_is_skipped_not_failed(
        self, tmp_path: Path
    ) -> None:
        file_path = tmp_path / "part-1.parquet"
        file_path.write_text("test", encoding="utf-8")
        config = Config(
            profile_name="laptop-a",
            backup_root=str(tmp_path),
            exclude_dirs=[],
            exclude_files=[],
            manifest_path=str(tmp_path / "manifest.json"),
        )
        engine = BackupEngine(
            config, dry_run=False, collect_machine_state_snapshot=False
        )
        mock_drive = MagicMock()
        mock_drive.get_or_create_folder.return_value = "root_id"
        mock_drive.find_file_by_name_and_parent.return_value = None

        def compacted_away(*args: object, **kwargs: object) -> None:
            file_path.unlink()
            raise FileNotFoundError(2, "No such file or directory", str(file_path))

        mock_drive.upload_file.side_effect = compacted_away
        engine.drive = mock_drive
        engine._root_folder_id = "root_id"
        stat = file_path.stat()
        entry = FileEntry(
            path=str(file_path),
            relative_path="part-1.parquet",
            size=stat.st_size,
            mtime=stat.st_mtime,
        )

        engine._process_file(entry)

        assert engine.stats.files_skipped_error == 0
        assert engine.stats.error_files == []
        assert [f.reason for f in engine.stats.skipped_files] == ["vanished"]

    def test_successful_upload_persists_manifest_before_run_finishes(
        self, tmp_path: Path
    ) -> None:
        file_path = tmp_path / "file.txt"
        file_path.write_text("test", encoding="utf-8")
        manifest_path = tmp_path / "manifest.json"
        config = Config(
            profile_name="laptop-a",
            backup_root=str(tmp_path),
            exclude_dirs=[],
            exclude_files=[],
            manifest_path=str(manifest_path),
        )
        engine = BackupEngine(
            config, dry_run=False, collect_machine_state_snapshot=False
        )
        mock_drive = MagicMock()
        mock_drive.find_file_by_name_and_parent.return_value = None
        mock_drive.upload_file.return_value = {
            "id": "drive_file_id",
            "md5Checksum": compute_md5(str(file_path)),
        }
        engine.drive = mock_drive
        engine._root_folder_id = "root_id"
        stat = file_path.stat()
        entry = FileEntry(
            path=str(file_path),
            relative_path="file.txt",
            size=stat.st_size,
            mtime=stat.st_mtime,
        )

        engine._process_file(entry)

        loaded = Manifest.load(str(manifest_path))
        persisted = loaded.get("file.txt")
        assert persisted is not None
        assert persisted.drive_file_id == "drive_file_id"

    def test_orphaned_drive_file_is_updated_instead_of_duplicated(
        self, tmp_path: Path
    ) -> None:
        file_path = tmp_path / "file.txt"
        file_path.write_text("hello", encoding="utf-8")
        local_md5 = compute_md5(str(file_path))
        config = Config(
            profile_name="laptop-a",
            backup_root=str(tmp_path),
            exclude_dirs=[],
            exclude_files=[],
            manifest_path=str(tmp_path / "manifest.json"),
        )
        engine = BackupEngine(
            config, dry_run=False, collect_machine_state_snapshot=False
        )
        mock_drive = MagicMock()
        mock_drive.find_file_by_name_and_parent.return_value = {
            "id": "orphan_id",
            "name": "file.txt",
            "md5Checksum": "old",
            "size": "5",
        }
        mock_drive.update_file.return_value = {
            "id": "orphan_id",
            "md5Checksum": local_md5,
        }
        mock_drive.upload_file.side_effect = AssertionError("upload_file called")
        engine.drive = mock_drive
        engine._root_folder_id = "root_id"
        stat = file_path.stat()
        entry = FileEntry(
            path=str(file_path),
            relative_path="file.txt",
            size=stat.st_size,
            mtime=stat.st_mtime,
        )

        engine._process_file(entry)

        persisted = Manifest.load(str(tmp_path / "manifest.json")).get("file.txt")
        assert persisted is not None
        assert persisted.drive_file_id == "orphan_id"
        mock_drive.upload_file.assert_not_called()
        mock_drive.update_file.assert_called_once_with(
            "orphan_id", str(file_path), resumable=False
        )

    def test_orphan_lookup_miss_uploads_new_file(self, tmp_path: Path) -> None:
        file_path = tmp_path / "file.txt"
        file_path.write_text("hello", encoding="utf-8")
        local_md5 = compute_md5(str(file_path))
        config = Config(
            profile_name="laptop-a",
            backup_root=str(tmp_path),
            exclude_dirs=[],
            exclude_files=[],
            manifest_path=str(tmp_path / "manifest.json"),
        )
        engine = BackupEngine(
            config, dry_run=False, collect_machine_state_snapshot=False
        )
        mock_drive = MagicMock()
        mock_drive.find_file_by_name_and_parent.return_value = None
        mock_drive.upload_file.return_value = {
            "id": "new_id",
            "md5Checksum": local_md5,
        }
        engine.drive = mock_drive
        engine._root_folder_id = "root_id"
        stat = file_path.stat()
        entry = FileEntry(
            path=str(file_path),
            relative_path="file.txt",
            size=stat.st_size,
            mtime=stat.st_mtime,
        )

        engine._process_file(entry)

        persisted = Manifest.load(str(tmp_path / "manifest.json")).get("file.txt")
        assert persisted is not None
        assert persisted.drive_file_id == "new_id"
        mock_drive.upload_file.assert_called_once_with(
            str(file_path), "root_id", resumable=False
        )

    def test_orphan_lookup_failure_does_not_create_duplicate(
        self, tmp_path: Path
    ) -> None:
        file_path = tmp_path / "file.txt"
        file_path.write_text("hello", encoding="utf-8")
        config = Config(
            profile_name="laptop-a",
            backup_root=str(tmp_path),
            exclude_dirs=[],
            exclude_files=[],
            manifest_path=str(tmp_path / "manifest.json"),
        )
        engine = BackupEngine(
            config, dry_run=False, collect_machine_state_snapshot=False
        )
        mock_drive = MagicMock()
        mock_drive.find_file_by_name_and_parent.side_effect = RuntimeError(
            "lookup failed"
        )
        mock_drive.upload_file.side_effect = AssertionError("upload_file called")
        engine.drive = mock_drive
        engine._root_folder_id = "root_id"
        stat = file_path.stat()
        entry = FileEntry(
            path=str(file_path),
            relative_path="file.txt",
            size=stat.st_size,
            mtime=stat.st_mtime,
        )

        engine._process_file(entry)

        assert engine.stats.files_skipped_error == 1
        assert "lookup failed" in engine.stats.error_files[0].error
        mock_drive.upload_file.assert_not_called()
        assert Manifest.load(str(tmp_path / "manifest.json")).get("file.txt") is None

    @staticmethod
    def _engine_with_stale_drive_id(
        tmp_path: Path, update_error: Exception
    ) -> tuple[BackupEngine, MagicMock, FileEntry]:
        file_path = tmp_path / "file.txt"
        file_path.write_text("hello", encoding="utf-8")
        config = Config(
            profile_name="laptop-a",
            backup_root=str(tmp_path),
            exclude_dirs=[],
            exclude_files=[],
            manifest_path=str(tmp_path / "manifest.json"),
        )
        engine = BackupEngine(
            config, dry_run=False, collect_machine_state_snapshot=False
        )
        engine.manifest.set(
            relative_path="file.txt",
            md5="stale",
            size=1,
            mtime=0.0,
            drive_file_id="deleted_id",
            drive_parent_id="root_id",
        )
        mock_drive = MagicMock()
        mock_drive.update_file.side_effect = update_error
        mock_drive.find_file_by_name_and_parent.return_value = None
        mock_drive.upload_file.return_value = {
            "id": "new_id",
            "md5Checksum": compute_md5(str(file_path)),
        }
        engine.drive = mock_drive
        engine._root_folder_id = "root_id"
        stat = file_path.stat()
        entry = FileEntry(
            path=str(file_path),
            relative_path="file.txt",
            size=stat.st_size,
            mtime=stat.st_mtime,
        )
        return engine, mock_drive, entry

    def test_deleted_drive_file_is_uploaded_again(self, tmp_path: Path) -> None:
        engine, mock_drive, entry = self._engine_with_stale_drive_id(
            tmp_path, _FakeHttpError(404)
        )

        engine._process_file(entry)

        assert engine.stats.files_skipped_error == 0
        mock_drive.update_file.assert_called_once_with(
            "deleted_id", entry.path, resumable=False
        )
        mock_drive.upload_file.assert_called_once_with(
            entry.path, "root_id", resumable=False
        )
        persisted = Manifest.load(str(tmp_path / "manifest.json")).get("file.txt")
        assert persisted is not None
        assert persisted.drive_file_id == "new_id"

    def test_other_update_errors_are_not_retried_as_uploads(
        self, tmp_path: Path
    ) -> None:
        engine, mock_drive, entry = self._engine_with_stale_drive_id(
            tmp_path, _FakeHttpError(403)
        )

        engine._process_file(entry)

        assert engine.stats.files_skipped_error == 1
        mock_drive.upload_file.assert_not_called()

    def test_trash_prune_checkpoints_on_interval_not_per_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backup_root = tmp_path / "backup"
        state_dir = tmp_path / "state"
        backup_root.mkdir()
        state_dir.mkdir()
        manifest_path = state_dir / "manifest.json"
        manifest = Manifest()
        for name in ("a", "b", "gone"):
            manifest.set(
                relative_path=f"old/{name}.txt",
                md5="abc",
                size=10,
                mtime=1.0,
                drive_file_id=f"drive_{name}",
                drive_parent_id="parent",
            )
        manifest.save(str(manifest_path))
        config = Config(
            profile_name="laptop-a",
            backup_root=str(backup_root),
            exclude_dirs=[],
            exclude_files=[],
            manifest_path=str(manifest_path),
        )
        engine = BackupEngine(
            config,
            dry_run=False,
            prune=True,
            prune_mode="trash",
            collect_machine_state_snapshot=False,
        )
        engine.manifest = Manifest.load(str(manifest_path))
        mock_drive = MagicMock()

        def trash_file(file_id: str) -> dict[str, str]:
            # Already purged from Drive (e.g. trash emptied after a crash): done.
            if file_id == "drive_gone":
                raise _FakeHttpError(404)
            return {"id": file_id}

        mock_drive.trash_file.side_effect = trash_file
        engine.drive = mock_drive
        saves: list[int] = []
        real_save = Manifest.save

        def tracking_save(manifest_obj: Manifest, path: str) -> None:
            saves.append(len(manifest_obj.entries))
            real_save(manifest_obj, path)

        monkeypatch.setattr(Manifest, "save", tracking_save)

        engine._prune_stale_manifest_entries()

        assert saves == []
        assert engine.stats.files_pruned == 3
        assert engine.stats.files_prune_failed == 0
        engine._maybe_save_manifest_progress(force=True)
        assert saves == [0]
        assert Manifest.load(str(manifest_path)).entries == {}

    def test_prune_protected_paths_are_kept_in_trash_mode(self, tmp_path: Path) -> None:
        backup_root = tmp_path / "backup"
        state_dir = tmp_path / "state"
        backup_root.mkdir()
        state_dir.mkdir()
        manifest_path = state_dir / "manifest.json"
        manifest = Manifest()
        for name in ("old/keep.txt", "old/stale.txt", "second/keep.txt"):
            manifest.set(
                relative_path=name,
                md5="abc",
                size=10,
                mtime=1.0,
                drive_file_id=f"drive_{name}",
                drive_parent_id="parent",
            )
        manifest.save(str(manifest_path))
        config = Config(
            profile_name="laptop-a",
            backup_root=str(backup_root),
            exclude_dirs=[],
            exclude_files=[],
            manifest_path=str(manifest_path),
            prune_protected_paths=["old/keep*"],
        )
        engine = BackupEngine(
            config,
            dry_run=False,
            prune=True,
            prune_mode="trash",
            collect_machine_state_snapshot=False,
        )
        engine.manifest = Manifest.load(str(manifest_path))
        mock_drive = MagicMock()
        mock_drive.trash_file.return_value = {"id": "trashed"}
        engine.drive = mock_drive

        engine._prune_stale_manifest_entries()

        assert [row.relative_path for row in engine.stats.pruned_files] == [
            "old/stale.txt",
            "second/keep.txt",
        ]
        assert set(engine.manifest.entries) == {"old/keep.txt"}
        engine._maybe_save_manifest_progress(force=True)
        assert set(Manifest.load(str(manifest_path)).entries) == {"old/keep.txt"}

    def test_manifest_progress_save_failure_aborts_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backup_root = tmp_path / "backup"
        state_dir = tmp_path / "state"
        backup_root.mkdir()
        state_dir.mkdir()
        (backup_root / "one.txt").write_text("one", encoding="utf-8")
        (backup_root / "two.txt").write_text("two", encoding="utf-8")
        upload_calls: list[str] = []

        class FakeDrive:
            def authenticate(self) -> None:
                pass

            def get_or_create_folder(
                self, name: str, parent_id: str | None = None
            ) -> str:
                return f"{name}_id" if parent_id is None else f"{parent_id}_{name}_id"

            def ensure_folder_path(self, path_parts: list[str], root_id: str) -> str:
                return root_id

            def find_file_by_name_and_parent(
                self, name: str, parent_id: str
            ) -> dict[str, str] | None:
                return None

            def upload_file(
                self, local_path: str, parent_id: str, resumable: bool = False
            ) -> dict[str, str]:
                upload_calls.append(local_path)
                return {"id": f"drive_{len(upload_calls)}", "md5Checksum": ""}

            def update_file(
                self, file_id: str, local_path: str, resumable: bool = False
            ) -> dict[str, str]:
                upload_calls.append(local_path)
                return {"id": file_id, "md5Checksum": ""}

        def fail_save_progress(self: BackupEngine) -> None:
            raise ManifestProgressError("Could not save manifest progress")

        monkeypatch.setattr("drive_backup.drive_api.DriveAPI", lambda **_: FakeDrive())
        monkeypatch.setattr(
            BackupEngine,
            "_save_manifest_progress",
            fail_save_progress,
        )
        config = Config(
            profile_name="laptop-a",
            backup_root=str(backup_root),
            exclude_dirs=[],
            exclude_files=[],
            manifest_path=str(state_dir / "manifest.json"),
        )
        with pytest.raises(ManifestProgressError):
            BackupEngine(
                config, dry_run=False, collect_machine_state_snapshot=False
            ).run()

        # With 30s checkpoint interval and 8 workers, both files are in-flight
        # before the final forced checkpoint fails; the failure stops further
        # submission beyond in-flight calls and skips snapshot/report uploads.
        assert len(upload_calls) == 2

    def test_report_upload_error_does_not_fail_backup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeDrive:
            def authenticate(self) -> None:
                pass

            def get_or_create_folder(
                self, name: str, parent_id: str | None = None
            ) -> str:
                return "root_id" if parent_id is None else "reports_id"

            def upload_file(
                self, local_path: str, parent_id: str, resumable: bool = False
            ) -> dict[str, str]:
                raise RuntimeError("Report upload failed")

        monkeypatch.setattr("drive_backup.drive_api.DriveAPI", lambda **_: FakeDrive())
        config = Config(
            profile_name="laptop-a",
            backup_root=str(tmp_path),
            exclude_dirs=[],
            exclude_files=[],
            manifest_path=str(tmp_path / "manifest.json"),
        )

        report = BackupEngine(
            config, dry_run=False, collect_machine_state_snapshot=False
        ).run()

        assert report["files_scanned"] == 0

    def test_prune_skipped_when_backup_has_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeDrive:
            trash_calls: ClassVar[list[str]] = []

            def authenticate(self) -> None:
                pass

            def get_or_create_folder(
                self, name: str, parent_id: str | None = None
            ) -> str:
                return "folder_id"

            def find_file_by_name_and_parent(
                self, name: str, parent_id: str
            ) -> dict[str, str] | None:
                return None

            def upload_file(
                self, local_path: str, parent_id: str, resumable: bool = False
            ) -> dict[str, str]:
                raise RuntimeError("upload failed")

            def trash_file(self, file_id: str) -> dict[str, str]:
                self.trash_calls.append(file_id)
                return {"id": file_id}

        backup_root = tmp_path / "backup"
        state_dir = tmp_path / "state"
        backup_root.mkdir()
        state_dir.mkdir()
        (backup_root / "new.txt").write_text("new")
        manifest_path = state_dir / "manifest.json"
        manifest = Manifest()
        manifest.set(
            relative_path="old/file.txt",
            md5="abc",
            size=10,
            mtime=1.0,
            drive_file_id="drive_old",
            drive_parent_id="parent",
        )
        manifest.save(str(manifest_path))
        monkeypatch.setattr("drive_backup.drive_api.DriveAPI", lambda **_: FakeDrive())
        config = Config(
            profile_name="laptop-a",
            backup_root=str(backup_root),
            exclude_dirs=[],
            exclude_files=[],
            manifest_path=str(manifest_path),
        )

        report = BackupEngine(
            config,
            dry_run=False,
            prune=True,
            prune_mode="trash",
            collect_machine_state_snapshot=False,
        ).run()

        assert (
            report["prune_skipped_reason"]
            == "Skipped prune because backup had file or upload errors"
        )
        assert FakeDrive.trash_calls == []


class TestBackupEngineProfileFolders:
    def test_profile_mode_uses_parent_and_profile_folder(self) -> None:
        config = Config(
            profile_name="laptop-a",
            drive_parent_folder_name="Profile Backups",
        )
        engine = BackupEngine(config, collect_machine_state_snapshot=False)
        mock_drive = MagicMock()
        mock_drive.get_or_create_folder.side_effect = ["parent_id", "profile_id"]
        engine.drive = mock_drive

        assert engine._resolve_backup_folder() == "profile_id"
        assert engine.stats.drive_parent_folder_id == "parent_id"
        assert mock_drive.get_or_create_folder.call_args_list[0].args == (
            "Profile Backups",
        )
        assert mock_drive.get_or_create_folder.call_args_list[1].args == (
            "laptop-a",
            "parent_id",
        )


class TestBackupEnginePrune:
    def test_prune_success_trashes_drive_file_and_removes_manifest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeDrive:
            def __init__(self) -> None:
                self.trash_calls: list[str] = []

            def authenticate(self) -> None:
                pass

            def get_or_create_folder(
                self, name: str, parent_id: str | None = None
            ) -> str:
                return f"{name}_id"

            def upload_file(
                self, local_path: str, parent_id: str, resumable: bool = False
            ) -> dict[str, str]:
                return {"id": "report_id", "md5Checksum": "report_md5"}

            def trash_file(self, file_id: str) -> dict[str, str]:
                self.trash_calls.append(file_id)
                return {"id": file_id}

        backup_root = tmp_path / "backup"
        state_dir = tmp_path / "state"
        backup_root.mkdir()
        state_dir.mkdir()
        manifest_path = state_dir / "manifest.json"
        manifest = Manifest()
        manifest.set(
            relative_path="old.txt",
            md5="abc",
            size=10,
            mtime=1.0,
            drive_file_id="drive_old",
            drive_parent_id="parent",
        )
        manifest.save(str(manifest_path))
        fake_drive = FakeDrive()
        monkeypatch.setattr("drive_backup.drive_api.DriveAPI", lambda **_: fake_drive)
        config = Config(
            profile_name="laptop-a",
            backup_root=str(backup_root),
            exclude_dirs=[],
            exclude_files=[],
            manifest_path=str(manifest_path),
        )

        report = BackupEngine(
            config,
            dry_run=False,
            prune=True,
            prune_mode="trash",
            collect_machine_state_snapshot=False,
        ).run()

        assert fake_drive.trash_calls == ["drive_old"]
        assert report["files_pruned"] == 1
        assert Manifest.load(str(manifest_path)).get("old.txt") is None

    def test_prune_skips_when_backup_root_is_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeDrive:
            def __init__(self) -> None:
                self.trash_calls: list[str] = []

            def authenticate(self) -> None:
                pass

            def get_or_create_folder(
                self, name: str, parent_id: str | None = None
            ) -> str:
                return f"{name}_id"

            def upload_file(
                self, local_path: str, parent_id: str, resumable: bool = False
            ) -> dict[str, str]:
                return {"id": "report_id", "md5Checksum": "report_md5"}

            def trash_file(self, file_id: str) -> dict[str, str]:
                self.trash_calls.append(file_id)
                return {"id": file_id}

        missing_root = tmp_path / "missing-backup-root"
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        manifest_path = state_dir / "manifest.json"
        manifest = Manifest()
        manifest.set(
            relative_path="old/file.txt",
            md5="abc",
            size=10,
            mtime=1.0,
            drive_file_id="drive_old",
            drive_parent_id="parent",
        )
        manifest.save(str(manifest_path))
        fake_drive = FakeDrive()
        monkeypatch.setattr("drive_backup.drive_api.DriveAPI", lambda **_: fake_drive)
        config = Config(
            profile_name="laptop-a",
            backup_root=str(missing_root),
            exclude_dirs=[],
            exclude_files=[],
            manifest_path=str(manifest_path),
        )

        report = BackupEngine(config, dry_run=False, prune=True).run()

        assert (
            report["prune_skipped_reason"]
            == "Skipped prune because backup root is unavailable"
        )
        assert fake_drive.trash_calls == []
        assert Manifest.load(str(manifest_path)).get("old/file.txt") is not None
        assert not missing_root.exists()
        assert report["machine_state_refreshed"] is False
        assert report["machine_state_collectors"] == [
            {
                "name": "snapshot",
                "status": "failed",
                "output_file": None,
                "warnings": [
                    "Backup root is unavailable; machine-state refresh skipped"
                ],
                "previous_output_retained": False,
            }
        ]

    def test_prune_failure_keeps_manifest_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeDrive:
            def authenticate(self) -> None:
                pass

            def get_or_create_folder(
                self, name: str, parent_id: str | None = None
            ) -> str:
                return f"{name}_id"

            def upload_file(
                self, local_path: str, parent_id: str, resumable: bool = False
            ) -> dict[str, str]:
                return {"id": "report_id", "md5Checksum": "report_md5"}

            def trash_file(self, file_id: str) -> dict[str, str]:
                raise RuntimeError("trash failed")

        backup_root = tmp_path / "backup"
        state_dir = tmp_path / "state"
        backup_root.mkdir()
        state_dir.mkdir()
        manifest_path = state_dir / "manifest.json"
        manifest = Manifest()
        manifest.set(
            relative_path="old/file.txt",
            md5="abc",
            size=10,
            mtime=1.0,
            drive_file_id="drive_old",
            drive_parent_id="parent",
        )
        manifest.save(str(manifest_path))
        monkeypatch.setattr("drive_backup.drive_api.DriveAPI", lambda **_: FakeDrive())
        config = Config(
            profile_name="laptop-a",
            backup_root=str(backup_root),
            exclude_dirs=[],
            exclude_files=[],
            manifest_path=str(manifest_path),
        )

        report = BackupEngine(
            config,
            dry_run=False,
            prune=True,
            prune_mode="trash",
            collect_machine_state_snapshot=False,
        ).run()

        assert report["files_pruned"] == 0
        assert report["files_prune_failed"] == 1
        assert report["prune_error_files"][0]["error"] == "trash failed"
        assert Manifest.load(str(manifest_path)).get("old/file.txt") is not None

    def test_prune_flag_mode_marks_entry_and_keeps_manifest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeDrive:
            def __init__(self) -> None:
                self.trash_calls: list[str] = []

            def authenticate(self) -> None:
                pass

            def get_or_create_folder(
                self, name: str, parent_id: str | None = None
            ) -> str:
                return f"{name}_id"

            def find_file_by_name_and_parent(
                self, name: str, parent_id: str
            ) -> dict[str, str] | None:
                return None

            def upload_file(
                self, local_path: str, parent_id: str, resumable: bool = False
            ) -> dict[str, str]:
                return {"id": "uploaded_id", "md5Checksum": "md5"}

            def update_file(
                self, file_id: str, local_path: str, resumable: bool = False
            ) -> dict[str, str]:
                return {"id": file_id, "md5Checksum": "md5"}

            def trash_file(self, file_id: str) -> dict[str, str]:
                self.trash_calls.append(file_id)
                return {"id": file_id}

        backup_root = tmp_path / "backup"
        state_dir = tmp_path / "state"
        backup_root.mkdir()
        state_dir.mkdir()
        manifest_path = state_dir / "manifest.json"
        manifest = Manifest()
        manifest.set(
            relative_path="old/file.txt",
            md5="abc",
            size=10,
            mtime=1.0,
            drive_file_id="drive_old",
            drive_parent_id="parent",
        )
        manifest.save(str(manifest_path))
        fake_drive = FakeDrive()
        monkeypatch.setattr("drive_backup.drive_api.DriveAPI", lambda **_: fake_drive)
        config = Config(
            profile_name="laptop-a",
            backup_root=str(backup_root),
            exclude_dirs=[],
            exclude_files=[],
            manifest_path=str(manifest_path),
        )

        report = BackupEngine(
            config, dry_run=False, prune=True, collect_machine_state_snapshot=False
        ).run()

        assert fake_drive.trash_calls == []
        assert report["files_pruned"] == 1
        entry = Manifest.load(str(manifest_path)).get("old/file.txt")
        assert entry is not None
        assert entry.pruned is True

    def test_prune_flag_mode_second_run_reports_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeDrive:
            def __init__(self) -> None:
                self.trash_calls: list[str] = []

            def authenticate(self) -> None:
                pass

            def get_or_create_folder(
                self, name: str, parent_id: str | None = None
            ) -> str:
                return f"{name}_id"

            def find_file_by_name_and_parent(
                self, name: str, parent_id: str
            ) -> dict[str, str] | None:
                return None

            def upload_file(
                self, local_path: str, parent_id: str, resumable: bool = False
            ) -> dict[str, str]:
                return {"id": "uploaded_id", "md5Checksum": "md5"}

            def update_file(
                self, file_id: str, local_path: str, resumable: bool = False
            ) -> dict[str, str]:
                return {"id": file_id, "md5Checksum": "md5"}

            def trash_file(self, file_id: str) -> dict[str, str]:
                self.trash_calls.append(file_id)
                return {"id": file_id}

        backup_root = tmp_path / "backup"
        state_dir = tmp_path / "state"
        backup_root.mkdir()
        state_dir.mkdir()
        manifest_path = state_dir / "manifest.json"
        manifest = Manifest()
        manifest.set(
            relative_path="old/file.txt",
            md5="abc",
            size=10,
            mtime=1.0,
            drive_file_id="drive_old",
            drive_parent_id="parent",
            pruned=True,
        )
        manifest.save(str(manifest_path))
        fake_drive = FakeDrive()
        monkeypatch.setattr("drive_backup.drive_api.DriveAPI", lambda **_: fake_drive)
        config = Config(
            profile_name="laptop-a",
            backup_root=str(backup_root),
            exclude_dirs=[],
            exclude_files=[],
            manifest_path=str(manifest_path),
        )

        report = BackupEngine(
            config, dry_run=False, prune=True, collect_machine_state_snapshot=False
        ).run()

        assert fake_drive.trash_calls == []
        assert report["files_pruned"] == 0
        entry = Manifest.load(str(manifest_path)).get("old/file.txt")
        assert entry is not None
        assert entry.pruned is True

    def test_prune_flag_mode_restored_file_reuploads_and_clears_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeDrive:
            def __init__(self) -> None:
                self.trash_calls: list[str] = []
                self.update_calls: list[str] = []

            def authenticate(self) -> None:
                pass

            def get_or_create_folder(
                self, name: str, parent_id: str | None = None
            ) -> str:
                return f"{name}_id"

            def find_file_by_name_and_parent(
                self, name: str, parent_id: str
            ) -> dict[str, str] | None:
                return None

            def upload_file(
                self, local_path: str, parent_id: str, resumable: bool = False
            ) -> dict[str, str]:
                return {"id": "uploaded_id", "md5Checksum": "md5"}

            def update_file(
                self, file_id: str, local_path: str, resumable: bool = False
            ) -> dict[str, str]:
                self.update_calls.append(file_id)
                return {"id": file_id, "md5Checksum": "md5"}

            def trash_file(self, file_id: str) -> dict[str, str]:
                self.trash_calls.append(file_id)
                return {"id": file_id}

        backup_root = tmp_path / "backup"
        state_dir = tmp_path / "state"
        backup_root.mkdir()
        state_dir.mkdir()
        (backup_root / "file.txt").write_text("new content", encoding="utf-8")
        manifest_path = state_dir / "manifest.json"
        manifest = Manifest()
        manifest.set(
            relative_path="file.txt",
            md5="old_md5",
            size=1,
            mtime=1.0,
            drive_file_id="drive_file",
            drive_parent_id="parent",
            pruned=True,
        )
        manifest.save(str(manifest_path))
        fake_drive = FakeDrive()
        monkeypatch.setattr("drive_backup.drive_api.DriveAPI", lambda **_: fake_drive)
        config = Config(
            profile_name="laptop-a",
            backup_root=str(backup_root),
            exclude_dirs=[],
            exclude_files=[],
            manifest_path=str(manifest_path),
        )

        report = BackupEngine(
            config, dry_run=False, prune=True, collect_machine_state_snapshot=False
        ).run()

        assert fake_drive.update_calls == ["drive_file"]
        assert fake_drive.trash_calls == []
        assert report["files_pruned"] == 0
        entry = Manifest.load(str(manifest_path)).get("file.txt")
        assert entry is not None
        assert entry.pruned is False


class TestBackupEngineManifestSnapshot:
    def _config(self, tmp_path: Path, manifest_path: Path) -> Config:
        return Config(
            profile_name="laptop-a",
            backup_root=str(tmp_path / "backup"),
            exclude_dirs=[],
            exclude_files=[],
            manifest_path=str(manifest_path),
        )

    def test_upload_creates_snapshot_when_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backup_root = tmp_path / "backup"
        state_dir = tmp_path / "state"
        backup_root.mkdir()
        state_dir.mkdir()
        (backup_root / "file.txt").write_text("data", encoding="utf-8")
        manifest_path = state_dir / "manifest.json"
        manifest = Manifest()
        stat = (backup_root / "file.txt").stat()
        manifest.set(
            relative_path="file.txt",
            md5="abc",
            size=stat.st_size,
            mtime=stat.st_mtime,
            drive_file_id="drive_file",
            drive_parent_id="parent",
        )
        manifest.save(str(manifest_path))

        upload_calls: list[tuple[str, str, bool]] = []

        class FakeDrive:
            def authenticate(self) -> None:
                pass

            def get_or_create_folder(
                self, name: str, parent_id: str | None = None
            ) -> str:
                return f"{name}_id"

            def find_file_by_name_and_parent(
                self, name: str, parent_id: str
            ) -> dict[str, str] | None:
                return None

            def upload_file(
                self, local_path: str, parent_id: str, resumable: bool = False
            ) -> dict[str, str]:
                upload_calls.append((local_path, parent_id, resumable))
                return {"id": "uploaded_id", "md5Checksum": "md5"}

            def update_file(
                self, file_id: str, local_path: str, resumable: bool = False
            ) -> dict[str, str]:
                return {"id": file_id, "md5Checksum": "md5"}

        monkeypatch.setattr("drive_backup.drive_api.DriveAPI", lambda **_: FakeDrive())
        config = self._config(tmp_path, manifest_path)

        report = BackupEngine(
            config, dry_run=False, collect_machine_state_snapshot=False
        ).run()

        assert report["manifest_snapshot_uploaded"] is True
        # Resumable: Drive rejects single-request uploads above 5 MB.
        assert (str(manifest_path), "_meta_id", True) in upload_calls

    def test_upload_updates_existing_snapshot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backup_root = tmp_path / "backup"
        state_dir = tmp_path / "state"
        backup_root.mkdir()
        state_dir.mkdir()
        manifest_path = state_dir / "manifest.json"
        Manifest().save(str(manifest_path))
        update_calls: list[tuple[str, bool]] = []

        class FakeDrive:
            def authenticate(self) -> None:
                pass

            def get_or_create_folder(
                self, name: str, parent_id: str | None = None
            ) -> str:
                return f"{name}_id"

            def find_file_by_name_and_parent(
                self, name: str, parent_id: str
            ) -> dict[str, str] | None:
                return {"id": "snapshot_id"}

            def upload_file(
                self, local_path: str, parent_id: str, resumable: bool = False
            ) -> dict[str, str]:
                return {"id": "uploaded_id", "md5Checksum": "md5"}

            def update_file(
                self, file_id: str, local_path: str, resumable: bool = False
            ) -> dict[str, str]:
                update_calls.append((file_id, resumable))
                return {"id": file_id, "md5Checksum": "md5"}

        monkeypatch.setattr("drive_backup.drive_api.DriveAPI", lambda **_: FakeDrive())
        config = self._config(tmp_path, manifest_path)

        report = BackupEngine(
            config, dry_run=False, collect_machine_state_snapshot=False
        ).run()

        assert report["manifest_snapshot_uploaded"] is True
        assert update_calls == [("snapshot_id", True)]

    def test_upload_failure_is_non_fatal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backup_root = tmp_path / "backup"
        state_dir = tmp_path / "state"
        backup_root.mkdir()
        state_dir.mkdir()
        manifest_path = state_dir / "manifest.json"
        Manifest().save(str(manifest_path))

        class FakeDrive:
            def authenticate(self) -> None:
                pass

            def get_or_create_folder(
                self, name: str, parent_id: str | None = None
            ) -> str:
                return f"{name}_id"

            def find_file_by_name_and_parent(
                self, name: str, parent_id: str
            ) -> dict[str, str] | None:
                return None

            def upload_file(
                self, local_path: str, parent_id: str, resumable: bool = False
            ) -> dict[str, str]:
                if parent_id == "_meta_id":
                    raise RuntimeError("snapshot upload failed")
                return {"id": "uploaded_id", "md5Checksum": "md5"}

            def update_file(
                self, file_id: str, local_path: str, resumable: bool = False
            ) -> dict[str, str]:
                return {"id": file_id, "md5Checksum": "md5"}

        monkeypatch.setattr("drive_backup.drive_api.DriveAPI", lambda **_: FakeDrive())
        config = self._config(tmp_path, manifest_path)

        report = BackupEngine(
            config, dry_run=False, collect_machine_state_snapshot=False
        ).run()

        assert report["files_scanned"] == 0
        assert report["manifest_snapshot_uploaded"] is False
        assert "snapshot upload failed" in report["manifest_snapshot_error"]

    def test_download_restores_missing_local_manifest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backup_root = tmp_path / "backup"
        state_dir = tmp_path / "state"
        backup_root.mkdir()
        state_dir.mkdir()
        (backup_root / "file.txt").write_text("data", encoding="utf-8")
        manifest_path = state_dir / "manifest.json"
        stat = (backup_root / "file.txt").stat()
        snapshot = Manifest()
        snapshot.set(
            relative_path="file.txt",
            md5="abc",
            size=stat.st_size,
            mtime=stat.st_mtime,
            drive_file_id="drive_file",
            drive_parent_id="parent",
        )
        snapshot_path = tmp_path / "snapshot.json"
        snapshot.save(str(snapshot_path))
        snapshot_bytes = snapshot_path.read_bytes()

        class FakeDrive:
            def authenticate(self) -> None:
                pass

            def get_or_create_folder(
                self, name: str, parent_id: str | None = None
            ) -> str:
                return f"{name}_id"

            def find_file_by_name_and_parent(
                self, name: str, parent_id: str
            ) -> dict[str, str] | None:
                return {"id": "snapshot_id"}

            def upload_file(
                self, local_path: str, parent_id: str, resumable: bool = False
            ) -> dict[str, str]:
                return {"id": "uploaded_id", "md5Checksum": "md5"}

            def update_file(
                self, file_id: str, local_path: str, resumable: bool = False
            ) -> dict[str, str]:
                return {"id": file_id, "md5Checksum": "md5"}

            def download_file(self, file_id: str, local_path: str) -> None:
                with open(local_path, "wb") as f:
                    f.write(snapshot_bytes)

        monkeypatch.setattr("drive_backup.drive_api.DriveAPI", lambda **_: FakeDrive())
        config = self._config(tmp_path, manifest_path)

        report = BackupEngine(
            config, dry_run=False, collect_machine_state_snapshot=False
        ).run()

        assert report["manifest_snapshot_downloaded"] is True
        # Manifest came from the snapshot: the unchanged file is dedup-skipped.
        assert report["files_skipped_dedup"] == 1
        assert report["files_uploaded"] == 0

    def test_dry_run_performs_no_snapshot_operations(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backup_root = tmp_path / "backup"
        backup_root.mkdir()
        manifest_path = tmp_path / "state" / "manifest.json"

        class FakeDrive:
            def __init__(self) -> None:
                raise AssertionError("DriveAPI must not be built in dry-run")

        monkeypatch.setattr("drive_backup.drive_api.DriveAPI", FakeDrive)
        config = self._config(tmp_path, manifest_path)

        report = BackupEngine(
            config, dry_run=True, collect_machine_state_snapshot=False
        ).run()

        assert report["manifest_snapshot_downloaded"] is False
        assert report["manifest_snapshot_uploaded"] is False
        assert report["manifest_snapshot_error"] == ""


class _PruneExcludedDrive:
    """Fake Drive recording trash calls; uploads get deterministic IDs."""

    def __init__(self) -> None:
        self.trash_calls: list[str] = []
        self.uploaded: list[str] = []

    def authenticate(self) -> None:
        pass

    def get_or_create_folder(self, name: str, parent_id: str | None = None) -> str:
        return f"{name}_id"

    def ensure_folder_path(self, path_parts: list[str], root_id: str) -> str:
        return "/".join(path_parts) + "_folder"

    def find_file_by_name_and_parent(
        self, name: str, parent_id: str
    ) -> dict[str, str] | None:
        return None

    def upload_file(
        self, local_path: str, parent_id: str, resumable: bool = False
    ) -> dict[str, str]:
        name = Path(local_path).name
        self.uploaded.append(name)
        return {"id": f"new_{name}", "md5Checksum": ""}

    def update_file(
        self, file_id: str, local_path: str, resumable: bool = False
    ) -> dict[str, str]:
        return {"id": file_id, "md5Checksum": ""}

    def trash_file(self, file_id: str) -> dict[str, str]:
        self.trash_calls.append(file_id)
        return {"id": file_id}


class TestBackupEnginePruneExcluded:
    @staticmethod
    def _track(manifest: Manifest, root: Path, rel: str, parent_id: str) -> None:
        """Record rel in the manifest as already backed up, unchanged if local."""
        local = root / rel
        exists = local.exists()
        manifest.set(
            relative_path=rel,
            md5=(compute_md5(str(local)) or "") if exists else "gone",
            size=local.stat().st_size if exists else 4,
            mtime=local.stat().st_mtime if exists else 1.0,
            drive_file_id=f"id:{rel}",
            drive_parent_id=parent_id,
        )

    def _run(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        manifest: Manifest,
        *,
        dry_run: bool = False,
        prune_trash: bool = False,
        drive: _PruneExcludedDrive | None = None,
        **config_overrides: object,
    ) -> tuple[_PruneExcludedDrive, Manifest, BackupReport]:
        manifest_path = tmp_path / "state" / "manifest.json"
        manifest.save(str(manifest_path))
        drive = drive or _PruneExcludedDrive()
        monkeypatch.setattr("drive_backup.drive_api.DriveAPI", lambda **_: drive)
        options: dict[str, object] = {
            "exclude_dirs": [],
            "exclude_files": [],
            "secrets_key_path": str(tmp_path / "state" / "secrets.key"),
        }
        options.update(config_overrides)
        config = Config(
            profile_name="laptop-a",
            backup_root=str(tmp_path / "home"),
            manifest_path=str(manifest_path),
            **options,  # type: ignore[arg-type]
        )
        report = BackupEngine(
            config,
            dry_run=dry_run,
            prune=prune_trash,
            prune_mode="trash" if prune_trash else "flag",
            prune_excluded=not prune_trash,
            collect_machine_state_snapshot=False,
        ).run()
        return drive, Manifest.load(str(manifest_path)), report

    def test_fully_excluded_tree_is_trashed_as_one_folder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        write_tree(
            home,
            {
                "notes.txt": "keep",
                "target/app.bin": "build",
                "target/debug/dep.bin": "build",
            },
        )
        manifest = Manifest()
        self._track(manifest, home, "notes.txt", "root")
        self._track(manifest, home, "target/app.bin", "target_folder")
        self._track(manifest, home, "target/debug/dep.bin", "debug_folder")
        # Flag-pruned history copy under the same folder goes with it.
        self._track(manifest, home, "target/old.bin", "target_folder")
        manifest.entries["target/old.bin"].pruned = True

        drive, saved, report = self._run(
            tmp_path, monkeypatch, manifest, exclude_dirs=["target"]
        )

        assert drive.trash_calls == ["target_folder"]
        assert set(saved.entries) == {"notes.txt"}
        assert report["pruned_folders"] == [
            {"relative_path": "target", "drive_folder_id": "target_folder", "files": 2}
        ]
        assert {row["relative_path"] for row in report["pruned_files"]} == {
            "target/app.bin",
            "target/debug/dep.bin",
        }
        assert {row["reason"] for row in report["pruned_files"]} == {"excluded"}

    def test_folder_with_kept_or_drive_only_files_is_trashed_file_by_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        write_tree(
            home,
            {
                "mixed/keep.txt": "keep",
                "mixed/noise.log": "log",
                "shared/noise.log": "log",
            },
        )
        manifest = Manifest()
        self._track(manifest, home, "mixed/keep.txt", "mixed_folder")
        self._track(manifest, home, "mixed/noise.log", "mixed_folder")
        self._track(manifest, home, "shared/noise.log", "shared_folder")
        # Exists only on Drive (e.g. from another OS): not flag-pruned, so kept.
        self._track(manifest, home, "shared/windows-only.txt", "shared_folder")

        drive, saved, report = self._run(
            tmp_path, monkeypatch, manifest, exclude_files=["*.log"]
        )

        assert sorted(drive.trash_calls) == [
            "id:mixed/noise.log",
            "id:shared/noise.log",
        ]
        assert set(saved.entries) == {"mixed/keep.txt", "shared/windows-only.txt"}
        assert report["pruned_folders"] == []

    def test_oversized_and_missing_files_keep_their_drive_copies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        write_tree(home, {"big/session.jsonl": "grew past the limit"})
        manifest = Manifest()
        self._track(manifest, home, "big/session.jsonl", "big_folder")
        self._track(manifest, home, "gone/deleted.txt", "gone_folder")

        drive, saved, report = self._run(
            tmp_path,
            monkeypatch,
            manifest,
            size_limits_by_type={".jsonl": 0.000001},
        )

        assert drive.trash_calls == []
        assert set(saved.entries) == {"big/session.jsonl", "gone/deleted.txt"}
        assert report["files_pruned"] == 0

    def test_dry_run_reports_without_trashing_or_saving(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        write_tree(home, {"target/app.bin": "build"})
        manifest = Manifest()
        self._track(manifest, home, "target/app.bin", "target_folder")

        drive, saved, report = self._run(
            tmp_path, monkeypatch, manifest, dry_run=True, exclude_dirs=["target"]
        )

        assert drive.trash_calls == []
        assert set(saved.entries) == {"target/app.bin"}
        assert report["files_pruned"] == 1
        assert report["pruned_folders"][0]["drive_folder_id"] == "target_folder"

    def test_deleted_tree_is_trashed_as_one_folder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        write_tree(home, {"keep/notes.txt": "keep"})
        manifest = Manifest()
        self._track(manifest, home, "keep/notes.txt", "keep_folder")
        self._track(manifest, home, "gone/a.txt", "gone_folder")
        self._track(manifest, home, "gone/sub/b.txt", "sub_folder")
        self._track(manifest, home, "stray.txt", "root")

        drive, saved, report = self._run(
            tmp_path, monkeypatch, manifest, prune_trash=True
        )

        assert sorted(drive.trash_calls) == ["gone_folder", "id:stray.txt"]
        assert set(saved.entries) == {"keep/notes.txt"}
        assert report["pruned_folders"] == [
            {"relative_path": "gone", "drive_folder_id": "gone_folder", "files": 2}
        ]
        assert report["files_pruned"] == 3
        assert {row["reason"] for row in report["pruned_files"]} == {"missing"}

    def test_folder_holding_another_apps_items_is_trashed_piece_by_piece(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class ForeignChildDrive(_PruneExcludedDrive):
            """Refuses folders that hold items another OAuth client created."""

            def trash_file(self, file_id: str) -> dict[str, str]:
                self.trash_calls.append(file_id)
                if file_id in {"gone_folder", "foreign_folder"}:
                    raise _FakeHttpError(403, "reason: 'appNotAuthorizedToChild'")
                return {"id": file_id}

        home = tmp_path / "home"
        write_tree(home, {"keep/notes.txt": "keep"})
        manifest = Manifest()
        self._track(manifest, home, "keep/notes.txt", "keep_folder")
        self._track(manifest, home, "gone/a.txt", "gone_folder")
        self._track(manifest, home, "gone/sub/b.txt", "sub_folder")
        # gone/deep holds no files directly, so its Drive ID is unknown.
        self._track(manifest, home, "gone/deep/x/c.txt", "x_folder")
        self._track(manifest, home, "gone/foreign/d.txt", "foreign_folder")

        drive, saved, report = self._run(
            tmp_path,
            monkeypatch,
            manifest,
            prune_trash=True,
            drive=ForeignChildDrive(),
        )

        assert sorted(drive.trash_calls) == [
            "foreign_folder",
            "gone_folder",
            "id:gone/a.txt",
            "id:gone/foreign/d.txt",
            "sub_folder",
            "x_folder",
        ]
        assert set(saved.entries) == {"keep/notes.txt"}
        assert report["files_prune_failed"] == 0
        assert report["files_pruned"] == 4
        assert sorted(f["relative_path"] for f in report["pruned_folders"]) == [
            "gone/deep/x",
            "gone/sub",
        ]

    def test_kept_file_in_same_drive_folder_blocks_folder_trash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        write_tree(home, {"Civ Pro/sub/new.txt": "keep", "b/new.txt": "keep"})
        manifest = Manifest()
        # Drive names are case-insensitive: "Civ pro" and "Civ Pro" are one folder.
        self._track(manifest, home, "Civ pro/old.txt", "civ_folder")
        self._track(manifest, home, "Civ Pro/sub/new.txt", "civ_sub_folder")
        # Different local paths that were filed under the same Drive folder.
        self._track(manifest, home, "a/old.txt", "shared_folder")
        self._track(manifest, home, "b/new.txt", "shared_folder")

        drive, saved, report = self._run(
            tmp_path, monkeypatch, manifest, prune_trash=True
        )

        assert sorted(drive.trash_calls) == ["id:Civ pro/old.txt", "id:a/old.txt"]
        assert set(saved.entries) == {"Civ Pro/sub/new.txt", "b/new.txt"}
        assert report["pruned_folders"] == []

    def test_trash_calls_run_concurrently(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        barrier = threading.Barrier(2, timeout=5)

        class BarrierDrive(_PruneExcludedDrive):
            def trash_file(self, file_id: str) -> dict[str, str]:
                barrier.wait()  # breaks (prune failure) if calls run serially
                return super().trash_file(file_id)

        home = tmp_path / "home"
        home.mkdir()
        manifest = Manifest()
        self._track(manifest, home, "one.txt", "root")
        self._track(manifest, home, "two.txt", "root")

        drive, _, report = self._run(
            tmp_path, monkeypatch, manifest, prune_trash=True, drive=BarrierDrive()
        )

        assert report["files_prune_failed"] == 0
        assert sorted(drive.trash_calls) == ["id:one.txt", "id:two.txt"]


class TestBackupEngineEncryptionSwitch:
    def test_plaintext_copy_is_trashed_when_file_becomes_encrypted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        write_tree(home, {"app/court.env": "TOKEN=abc"})
        manifest_path = tmp_path / "state" / "manifest.json"
        manifest = Manifest()
        local = home / "app/court.env"
        manifest.set(
            relative_path="app/court.env",
            md5=compute_md5(str(local)) or "",
            size=local.stat().st_size,
            mtime=local.stat().st_mtime,
            drive_file_id="plaintext_id",
            drive_parent_id="app_folder",
            encrypted=False,
        )
        manifest.save(str(manifest_path))
        drive = _PruneExcludedDrive()
        monkeypatch.setattr("drive_backup.drive_api.DriveAPI", lambda **_: drive)
        config = Config(
            profile_name="laptop-a",
            backup_root=str(home),
            exclude_dirs=[],
            exclude_files=[],
            manifest_path=str(manifest_path),
            secrets_key_path=str(tmp_path / "state" / "secrets.key"),
        )

        report = BackupEngine(config, collect_machine_state_snapshot=False).run()

        assert "court.env.enc" in drive.uploaded
        assert drive.trash_calls == ["plaintext_id"]
        entry = Manifest.load(str(manifest_path)).get("app/court.env")
        assert entry is not None
        assert entry.encrypted is True
        assert entry.drive_file_id == "new_court.env.enc"
        assert report["files_prune_failed"] == 0
