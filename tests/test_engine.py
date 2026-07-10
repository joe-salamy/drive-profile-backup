"""Tests for the backup engine using mocks for Drive API."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from drive_backup.config import Config
from drive_backup.dedup import Manifest
from drive_backup.engine import BackupEngine, _format_mtime
from drive_backup.scanner import FileEntry

if TYPE_CHECKING:
    import pytest


class TestFormatMtime:
    def test_zero_returns_empty(self) -> None:
        assert _format_mtime(0) == ""

    def test_formats_timestamp(self) -> None:
        result = _format_mtime(1704067200.0)  # 2024-01-01 00:00:00 UTC
        assert "2024-01-01" in result


class TestBackupEngineDryRun:
    def _make_tree(self, tmp: str, files: dict[str, str]) -> None:
        for rel_path, content in files.items():
            full = os.path.join(tmp, rel_path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write(content)

    def test_dry_run_scans_without_uploading(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._make_tree(tmp, {"file1.txt": "hello", "sub/file2.txt": "world"})
            manifest_path = os.path.join(tmp, "manifest.json")

            config = Config(
                profile_name="laptop-a",
                backup_root=tmp,
                exclude_dirs=[],
                exclude_files=[],
                manifest_path=manifest_path,
            )
            engine = BackupEngine(config, dry_run=True)
            report = engine.run()

            assert report["dry_run"] is True
            assert report["files_scanned"] >= 2
            assert report["files_uploaded"] >= 2
            # No manifest should be saved in dry-run
            assert not os.path.exists(manifest_path)

    def test_dry_run_calls_progress_callback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._make_tree(tmp, {"file.txt": "data"})
            config = Config(
                profile_name="laptop-a",
                backup_root=tmp,
                exclude_dirs=[],
                exclude_files=[],
                manifest_path=os.path.join(tmp, "manifest.json"),
            )
            engine = BackupEngine(config, dry_run=True)
            calls: list[tuple[str, str]] = []

            def callback(file: FileEntry, action: str) -> None:
                calls.append((file.relative_path, action))

            engine.run(progress_callback=callback)

            assert len(calls) >= 1
            assert any("would_upload" in action for _, action in calls)

    def test_dry_run_skips_excluded_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._make_tree(
                tmp,
                {
                    "keep.txt": "keep",
                    "Thumbs.db": "skip",
                },
            )
            config = Config(
                profile_name="laptop-a",
                backup_root=tmp,
                exclude_dirs=[],
                exclude_files=["Thumbs.db"],
                manifest_path=os.path.join(tmp, "manifest.json"),
            )
            engine = BackupEngine(config, dry_run=True)
            report = engine.run()

            assert report["files_skipped_exclusion"] == 1

    def test_dedup_skips_unchanged_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._make_tree(tmp, {"file.txt": "content"})
            # Put manifest outside the backup root to avoid scanning it
            manifest_dir = tempfile.mkdtemp()
            manifest_path = os.path.join(manifest_dir, "manifest.json")
            config = Config(
                profile_name="laptop-a",
                backup_root=tmp,
                exclude_dirs=[],
                exclude_files=[],
                manifest_path=manifest_path,
            )

            # Pre-populate manifest with matching entry
            file_path = os.path.join(tmp, "file.txt")
            stat = os.stat(file_path)
            manifest = Manifest()
            manifest.set(
                relative_path="file.txt",
                md5="abc",
                size=stat.st_size,
                mtime=stat.st_mtime,
                drive_file_id="id",
                drive_parent_id="pid",
            )
            manifest.save(manifest_path)

            engine = BackupEngine(config, dry_run=True)
            report = engine.run()

            assert report["files_skipped_dedup"] == 1
            assert report["files_uploaded"] == 0

    def test_full_mode_ignores_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._make_tree(tmp, {"file.txt": "content"})
            # Put manifest outside the backup root
            manifest_dir = tempfile.mkdtemp()
            manifest_path = os.path.join(manifest_dir, "manifest.json")
            config = Config(
                profile_name="laptop-a",
                backup_root=tmp,
                exclude_dirs=[],
                exclude_files=[],
                manifest_path=manifest_path,
            )

            # Pre-populate manifest
            file_path = os.path.join(tmp, "file.txt")
            stat = os.stat(file_path)
            manifest = Manifest()
            manifest.set(
                relative_path="file.txt",
                md5="abc",
                size=stat.st_size,
                mtime=stat.st_mtime,
                drive_file_id="id",
                drive_parent_id="pid",
            )
            manifest.save(manifest_path)

            engine = BackupEngine(config, dry_run=True, full=True)
            report = engine.run()

            # Full mode should re-upload even if manifest matches
            assert report["files_uploaded"] == 1

    def test_prune_disabled_ignores_stale_manifest_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backup_root = os.path.join(tmp, "backup")
            manifest_dir = os.path.join(tmp, "state")
            os.makedirs(backup_root)
            os.makedirs(manifest_dir)
            manifest_path = os.path.join(manifest_dir, "manifest.json")
            manifest = Manifest()
            manifest.set(
                relative_path="old/file.txt",
                md5="abc",
                size=10,
                mtime=1.0,
                drive_file_id="drive_old",
                drive_parent_id="parent",
            )
            manifest.save(manifest_path)
            config = Config(
                profile_name="laptop-a",
                backup_root=backup_root,
                exclude_dirs=[],
                exclude_files=[],
                manifest_path=manifest_path,
            )

            report = BackupEngine(config, dry_run=True, prune=False).run()

            assert report["files_pruned"] == 0
            assert report["pruned_files"] == []

    def test_dry_run_prune_reports_stale_entries_without_saving_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backup_root = os.path.join(tmp, "backup")
            manifest_dir = os.path.join(tmp, "state")
            os.makedirs(backup_root)
            os.makedirs(manifest_dir)
            self._make_tree(backup_root, {"keep.txt": "keep"})
            manifest_path = os.path.join(manifest_dir, "manifest.json")
            keep_path = os.path.join(backup_root, "keep.txt")
            stat = os.stat(keep_path)
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
            manifest.save(manifest_path)
            config = Config(
                profile_name="laptop-a",
                backup_root=backup_root,
                exclude_dirs=[],
                exclude_files=[],
                manifest_path=manifest_path,
            )

            report = BackupEngine(config, dry_run=True, prune=True).run()

            assert report["files_pruned"] == 1
            assert report["pruned_files"][0]["relative_path"] == "old/moved.txt"
            assert Manifest.load(manifest_path).get("old/moved.txt") is not None


class TestBackupEngineUploadErrors:
    def test_upload_error_is_captured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            file_path = os.path.join(tmp, "file.txt")
            with open(file_path, "w") as f:
                f.write("test")

            config = Config(
                profile_name="laptop-a",
                backup_root=tmp,
                exclude_dirs=[],
                exclude_files=[],
                manifest_path=os.path.join(tmp, "manifest.json"),
            )
            engine = BackupEngine(config, dry_run=False)

            # Mock the drive to raise on upload
            mock_drive = MagicMock()
            mock_drive.get_or_create_folder.return_value = "root_id"
            mock_drive.upload_file.side_effect = RuntimeError("Upload failed")
            engine.drive = mock_drive
            engine._root_folder_id = "root_id"

            # Process a single file manually
            stat = os.stat(file_path)
            entry = FileEntry(
                path=file_path,
                relative_path="file.txt",
                size=stat.st_size,
                mtime=stat.st_mtime,
            )
            engine._process_file(entry)

            assert engine.stats.files_skipped_error == 1
            assert len(engine.stats.error_files) == 1
            assert "Upload failed" in engine.stats.error_files[0].error

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
        engine = BackupEngine(config, dry_run=False)

        report = engine.run()

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
