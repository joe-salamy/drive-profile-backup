"""Tests for the backup engine using mocks for Drive API."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from drive_backup.config import Config
from drive_backup.dedup import Manifest, compute_md5
from drive_backup.engine import (
    BackupEngine,
    ManifestProgressError,
    ProgressEvent,
    ProgressKind,
    _format_mtime,
)
from drive_backup.scanner import FileEntry
from tests.file_helpers import write_tree


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

        report = BackupEngine(config, dry_run=True).run()

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

        BackupEngine(config, dry_run=True).run(progress_callback=callback)

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

        report = BackupEngine(config, dry_run=True).run()

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

        report = BackupEngine(config, dry_run=True).run()

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

        report = BackupEngine(config, dry_run=True, full=True).run()

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

        report = BackupEngine(config, dry_run=True, prune=False).run()

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

        report = BackupEngine(config, dry_run=True, prune=True).run()

        assert report["files_pruned"] == 1
        assert report["pruned_files"][0]["relative_path"] == "old/moved.txt"
        assert Manifest.load(str(manifest_path)).get("old/moved.txt") is not None


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
        engine = BackupEngine(config, dry_run=False)
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
        engine = BackupEngine(config, dry_run=False)
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
        engine = BackupEngine(config, dry_run=False)
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
        engine = BackupEngine(config, dry_run=False)
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
        engine = BackupEngine(config, dry_run=False)
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

    def test_successful_prune_removal_persists_immediately(
        self, tmp_path: Path
    ) -> None:
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
        config = Config(
            profile_name="laptop-a",
            backup_root=str(backup_root),
            exclude_dirs=[],
            exclude_files=[],
            manifest_path=str(manifest_path),
        )
        engine = BackupEngine(config, dry_run=False, prune=True)
        engine.manifest = Manifest.load(str(manifest_path))
        mock_drive = MagicMock()
        mock_drive.trash_file.return_value = {"id": "drive_old"}
        engine.drive = mock_drive

        engine._prune_stale_manifest_entries()

        assert Manifest.load(str(manifest_path)).get("old/file.txt") is None

    def test_manifest_progress_save_failure_aborts_run(
        self, tmp_path: Path, monkeypatch: "pytest.MonkeyPatch"
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
            BackupEngine(config, dry_run=False).run()

        assert len(upload_calls) == 1

    def test_report_upload_error_does_not_fail_backup(
        self, tmp_path: Path, monkeypatch: "pytest.MonkeyPatch"
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

        report = BackupEngine(config, dry_run=False).run()

        assert report["files_scanned"] == 0

    def test_prune_skipped_when_backup_has_errors(
        self, tmp_path: Path, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        class FakeDrive:
            trash_calls: list[str] = []

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

        report = BackupEngine(config, dry_run=False, prune=True).run()

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
        engine = BackupEngine(config)
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
        self, tmp_path: Path, monkeypatch: "pytest.MonkeyPatch"
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

        report = BackupEngine(config, dry_run=False, prune=True).run()

        assert fake_drive.trash_calls == ["drive_old"]
        assert report["files_pruned"] == 1
        assert Manifest.load(str(manifest_path)).get("old/file.txt") is None

    def test_prune_skips_when_backup_root_is_missing(
        self, tmp_path: Path, monkeypatch: "pytest.MonkeyPatch"
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

    def test_prune_failure_keeps_manifest_entry(
        self, tmp_path: Path, monkeypatch: "pytest.MonkeyPatch"
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

        report = BackupEngine(config, dry_run=False, prune=True).run()

        assert report["files_pruned"] == 0
        assert report["files_prune_failed"] == 1
        assert report["prune_error_files"][0]["error"] == "trash failed"
        assert Manifest.load(str(manifest_path)).get("old/file.txt") is not None
