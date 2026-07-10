"""Tests for deduplication and manifest management."""

import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

from drive_backup.dedup import Manifest, compute_md5, needs_upload
from drive_backup.scanner import FileEntry


class TestComputeMD5:
    def test_computes_correct_md5(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
            f.write("hello world")
            f.flush()
            md5 = compute_md5(f.name)
        os.unlink(f.name)
        # MD5 of "hello world"
        assert md5 == "5eb63bbbe01eeed093cb22bb8f5acdc3"

    def test_returns_none_for_missing_file(self) -> None:
        result = compute_md5("/nonexistent/file.txt")
        assert result is None


class TestManifest:
    def test_load_empty(self) -> None:
        manifest = Manifest.load("/nonexistent/manifest.json")
        assert len(manifest.entries) == 0

    def test_save_and_load_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "manifest.json")
            manifest = Manifest()
            manifest.set(
                relative_path="test/file.txt",
                md5="abc123",
                size=100,
                mtime=1000.0,
                drive_file_id="drive_123",
                drive_parent_id="parent_456",
            )
            manifest.save(path)

            loaded = Manifest.load(path)
            assert len(loaded.entries) == 1
            entry = loaded.get("test/file.txt")
            assert entry is not None
            assert entry.md5 == "abc123"
            assert entry.size == 100
            assert entry.drive_file_id == "drive_123"

    def test_save_creates_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nested", "dir", "manifest.json")
            manifest = Manifest()
            manifest.save(path)
            assert os.path.exists(path)

    def test_save_keeps_existing_manifest_when_replace_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "manifest.json"
        original = Manifest()
        original.set(
            relative_path="old.txt",
            md5="old_md5",
            size=3,
            mtime=1.0,
            drive_file_id="old_drive_id",
            drive_parent_id="parent",
        )
        original.save(str(path))

        def fail_replace(src: str, dst: str) -> None:
            raise OSError("replace failed")

        monkeypatch.setattr("drive_backup.dedup.os.replace", fail_replace)
        replacement = Manifest()
        replacement.set(
            relative_path="new.txt",
            md5="new_md5",
            size=3,
            mtime=2.0,
            drive_file_id="new_drive_id",
            drive_parent_id="parent",
        )

        with pytest.raises(OSError, match="replace failed"):
            replacement.save(str(path))

        loaded = Manifest.load(str(path))
        assert loaded.get("old.txt") is not None
        assert loaded.get("new.txt") is None
        assert list(tmp_path.glob(".manifest.json.*.tmp")) == []

    def test_save_replaces_manifest_atomically_on_success(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.json"
        original = Manifest()
        original.set(
            relative_path="old.txt",
            md5="old_md5",
            size=3,
            mtime=1.0,
            drive_file_id="old_drive_id",
            drive_parent_id="parent",
        )
        original.save(str(path))

        replacement = Manifest()
        replacement.set(
            relative_path="new.txt",
            md5="new_md5",
            size=3,
            mtime=2.0,
            drive_file_id="new_drive_id",
            drive_parent_id="parent",
        )
        replacement.save(str(path))

        loaded = Manifest.load(str(path))
        assert loaded.get("old.txt") is None
        assert loaded.get("new.txt") is not None

    def test_load_ignores_corrupted_sibling_temp_file(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.json"
        manifest = Manifest()
        manifest.set(
            relative_path="valid.txt",
            md5="valid_md5",
            size=5,
            mtime=1.0,
            drive_file_id="valid_drive_id",
            drive_parent_id="parent",
        )
        manifest.save(str(path))
        (tmp_path / ".manifest.json.orphan.tmp").write_text(
            "not json",
            encoding="utf-8",
        )

        loaded = Manifest.load(str(path))

        assert loaded.get("valid.txt") is not None

    def test_remove_entry(self) -> None:
        manifest = Manifest()
        manifest.set(
            relative_path="old/file.txt",
            md5="abc",
            size=10,
            mtime=1.0,
            drive_file_id="drive_old",
            drive_parent_id="parent",
        )

        removed = manifest.remove("old/file.txt")

        assert removed is not None
        assert removed.drive_file_id == "drive_old"
        assert manifest.get("old/file.txt") is None
        assert manifest.remove("missing.txt") is None


class TestNeedsUpload:
    def _make_file_entry(self, **kwargs: Any) -> FileEntry:
        defaults: dict[str, Any] = {
            "path": "/test/file.txt",
            "relative_path": "file.txt",
            "size": 100,
            "mtime": 1000.0,
        }
        defaults.update(kwargs)
        return FileEntry(**defaults)

    def test_new_file_needs_upload(self) -> None:
        manifest = Manifest()
        file = self._make_file_entry()
        result, reason = needs_upload(file, manifest)
        assert result is True
        assert reason == "new"

    def test_unchanged_file_skipped(self) -> None:
        manifest = Manifest()
        manifest.set(
            relative_path="file.txt",
            md5="abc",
            size=100,
            mtime=1000.0,
            drive_file_id="id",
            drive_parent_id="pid",
        )
        file = self._make_file_entry(size=100, mtime=1000.0)
        result, reason = needs_upload(file, manifest)
        assert result is False
        assert reason == "skipped_mtime_match"

    def test_size_changed_needs_upload(self) -> None:
        manifest = Manifest()
        manifest.set(
            relative_path="file.txt",
            md5="abc",
            size=100,
            mtime=1000.0,
            drive_file_id="id",
            drive_parent_id="pid",
        )
        file = self._make_file_entry(size=200, mtime=1000.0)
        result, reason = needs_upload(file, manifest)
        assert result is True
        assert reason == "size_changed"

    def test_mtime_changed_content_same_skips(self) -> None:
        """When mtime changes but MD5 matches, skip upload."""
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
            f.write("same content")
            f.flush()
            md5 = compute_md5(f.name)

        manifest = Manifest()
        manifest.set(
            relative_path="file.txt",
            md5=md5 or "",
            size=os.path.getsize(f.name),
            mtime=999.0,  # Old mtime
            drive_file_id="id",
            drive_parent_id="pid",
        )
        file = self._make_file_entry(
            path=f.name,
            size=os.path.getsize(f.name),
            mtime=1000.0,  # New mtime
        )
        result, reason = needs_upload(file, manifest)
        os.unlink(f.name)
        assert result is False
        assert reason == "skipped_md5_match"

    def test_content_changed_needs_upload(self) -> None:
        """When mtime changes and MD5 differs, upload."""
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
            f.write("new content")
            f.flush()

        manifest = Manifest()
        manifest.set(
            relative_path="file.txt",
            md5="old_md5_hash",
            size=os.path.getsize(f.name),
            mtime=999.0,
            drive_file_id="id",
            drive_parent_id="pid",
        )
        file = self._make_file_entry(
            path=f.name,
            size=os.path.getsize(f.name),
            mtime=1000.0,
        )
        result, reason = needs_upload(file, manifest)
        os.unlink(f.name)
        assert result is True
        assert reason == "content_changed"

    def test_md5_error_needs_upload(self) -> None:
        """When MD5 can't be computed, attempt upload instead of silently skipping."""
        manifest = Manifest()
        manifest.set(
            relative_path="file.txt",
            md5="abc",
            size=100,
            mtime=999.0,
            drive_file_id="id",
            drive_parent_id="pid",
        )
        file = self._make_file_entry(
            path="/nonexistent/file.txt",
            size=100,
            mtime=1000.0,
        )
        result, reason = needs_upload(file, manifest)
        assert result is True
        assert reason == "md5_error"


class TestManifestCorrupted:
    def test_load_corrupted_json(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json") as f:
            f.write("not valid json{{{")
            f.flush()
            manifest = Manifest.load(f.name)
        os.unlink(f.name)
        assert len(manifest.entries) == 0

    def test_load_malformed_entry(self) -> None:
        import json

        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json") as f:
            json.dump(
                {"files": {"test.txt": {"bad_key": "value"}}},
                f,
            )
            f.flush()
            manifest = Manifest.load(f.name)
        os.unlink(f.name)
        assert len(manifest.entries) == 0
