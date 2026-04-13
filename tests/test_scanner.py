"""Tests for the filesystem scanner."""

import os
import tempfile

from drive_backup.config import Config
from drive_backup.scanner import FileEntry, _truncate_relative_path, scan


class TestScanner:
    def _make_tree(self, tmp: str, files: dict[str, bytes | str]) -> None:
        """Create a file tree under tmp from a dict of relative_path -> content."""
        for rel_path, content in files.items():
            full = os.path.join(tmp, rel_path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            mode = "wb" if isinstance(content, bytes) else "w"
            with open(full, mode) as f:
                f.write(content)

    def test_basic_scan_finds_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._make_tree(
                tmp,
                {
                    "file1.txt": "hello",
                    "subdir/file2.txt": "world",
                },
            )
            config = Config(backup_root=tmp, exclude_dirs=[], exclude_files=[])
            entries = list(scan(config))

            paths = {e.relative_path for e in entries}
            assert "file1.txt" in paths
            assert "subdir/file2.txt" in paths
            assert all(not e.is_skipped for e in entries)

    def test_excludes_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._make_tree(
                tmp,
                {
                    "keep.txt": "keep",
                    "venv/lib/something.py": "skip",
                    "__pycache__/cached.pyc": "skip",
                },
            )
            config = Config(
                backup_root=tmp,
                exclude_dirs=["venv", "__pycache__"],
                exclude_files=[],
            )
            entries = list(scan(config))
            paths = {e.relative_path for e in entries}

            assert "keep.txt" in paths
            assert "venv/lib/something.py" not in paths
            assert "__pycache__/cached.pyc" not in paths

    def test_excludes_file_patterns(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._make_tree(
                tmp,
                {
                    "keep.txt": "keep",
                    "Thumbs.db": "skip",
                    "desktop.ini": "skip",
                },
            )
            config = Config(
                backup_root=tmp,
                exclude_dirs=[],
                exclude_files=["Thumbs.db", "desktop.ini"],
            )
            entries = list(scan(config))

            kept = [e for e in entries if not e.is_skipped]
            skipped = [e for e in entries if e.is_skipped]

            assert len(kept) == 1
            assert kept[0].relative_path == "keep.txt"
            assert len(skipped) == 2
            assert all("excluded_by_pattern" in s.skip_reason for s in skipped)

    def test_size_limit_skips_large_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._make_tree(
                tmp,
                {
                    "small.txt": "x",
                    "big.txt": "x" * 2000,
                },
            )
            config = Config(
                backup_root=tmp,
                exclude_dirs=[],
                exclude_files=[],
                max_file_size_mb=0.001,  # ~1 KB
                no_size_limit=[],
            )
            entries = list(scan(config))
            skipped = [e for e in entries if e.is_skipped]

            assert len(skipped) == 1
            assert "exceeds_size_limit" in skipped[0].skip_reason

    def test_type_excluded_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._make_tree(
                tmp,
                {
                    "app.exe": "binary",
                    "doc.txt": "text",
                },
            )
            config = Config(
                backup_root=tmp,
                exclude_dirs=[],
                exclude_files=[],
                size_limits_by_type={".exe": 0},
                no_size_limit=[],
            )
            entries = list(scan(config))
            skipped = [e for e in entries if e.is_skipped]

            assert len(skipped) == 1
            assert skipped[0].extension == ".exe"
            assert "type_excluded" in skipped[0].skip_reason

    def test_media_files_bypass_size_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Create a "large" jpg that exceeds the default limit
            self._make_tree(
                tmp,
                {
                    "photo.jpg": "x" * 5000,
                },
            )
            config = Config(
                backup_root=tmp,
                exclude_dirs=[],
                exclude_files=[],
                max_file_size_mb=0.001,  # ~1 KB — but .jpg has no limit
            )
            entries = list(scan(config))

            assert len(entries) == 1
            assert not entries[0].is_skipped

    def test_skipped_files_have_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._make_tree(tmp, {"Thumbs.db": "data"})
            config = Config(
                backup_root=tmp,
                exclude_dirs=[],
                exclude_files=["Thumbs.db"],
            )
            entries = list(scan(config))

            assert len(entries) == 1
            entry = entries[0]
            assert entry.is_skipped
            assert entry.size > 0
            assert entry.mtime > 0
            assert entry.path.endswith("Thumbs.db")

    def test_path_pattern_exclusion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._make_tree(
                tmp,
                {
                    "keep.txt": "keep",
                    "lectures/open-law-notes/rec.wav": "skip",
                },
            )
            config = Config(
                backup_root=tmp,
                exclude_dirs=[],
                exclude_files=[],
                exclude_path_patterns=["*/open-law-notes/*.wav"],
            )
            entries = list(scan(config))
            skipped = [e for e in entries if e.is_skipped]
            kept = [e for e in entries if not e.is_skipped]

            assert len(kept) == 1
            assert len(skipped) == 1
            assert "excluded_by_path_pattern" in skipped[0].skip_reason

    def test_specific_file_exclusion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._make_tree(
                tmp,
                {
                    "keep.txt": "keep",
                    "docs/secret.pdf": "skip",
                },
            )
            config = Config(
                backup_root=tmp,
                exclude_dirs=[],
                exclude_files=[],
                exclude_specific_files=["docs/secret.pdf"],
            )
            entries = list(scan(config))
            skipped = [e for e in entries if e.is_skipped]
            kept = [e for e in entries if not e.is_skipped]

            assert len(kept) == 1
            assert len(skipped) == 1
            assert "excluded_by_specific_file" in skipped[0].skip_reason

    def test_nonexistent_backup_root_yields_nothing(self) -> None:
        config = Config(
            backup_root="/nonexistent/path/that/does/not/exist",
            exclude_dirs=[],
            exclude_files=[],
        )
        entries = list(scan(config))
        assert len(entries) == 0

    def test_file_entry_properties(self) -> None:
        entry = FileEntry(
            path="/test/photo.JPG",
            relative_path="photo.JPG",
            size=2048,
            mtime=1000.0,
        )
        assert entry.extension == ".jpg"
        assert "KB" in entry.size_human


class TestTruncateRelativePath:
    def test_short_path_unchanged(self) -> None:
        assert _truncate_relative_path("short.txt", max_len=260) == "short.txt"

    def test_long_path_truncated(self) -> None:
        long_stem = "a" * 300
        result = _truncate_relative_path(f"{long_stem}.txt", max_len=20)
        assert len(result) <= 20
        assert result.endswith(".txt")

    def test_long_path_with_directory(self) -> None:
        result = _truncate_relative_path(f"dir/{'a' * 300}.txt", max_len=20)
        assert len(result) <= 20
        assert result.startswith("dir/")
        assert result.endswith(".txt")
