"""Tests for the backup engine using mocks for Drive API."""

from __future__ import annotations

import os
import tempfile
from unittest.mock import MagicMock

from drive_backup.config import Config
from drive_backup.dedup import Manifest
from drive_backup.engine import BackupEngine, _format_mtime
from drive_backup.scanner import FileEntry


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


class TestBackupEngineUploadErrors:
    def test_upload_error_is_captured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            file_path = os.path.join(tmp, "file.txt")
            with open(file_path, "w") as f:
                f.write("test")

            config = Config(
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
