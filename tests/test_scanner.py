"""Tests for the filesystem scanner."""

from pathlib import Path
from typing import TYPE_CHECKING

from drive_backup.config import Config
from drive_backup.scanner import FileEntry, scan
from tests.file_helpers import write_tree

if TYPE_CHECKING:
    import pytest


class TestScanner:

    def test_basic_scan_finds_files(self, tmp_path: Path) -> None:
        tmp = str(tmp_path)
        write_tree(
            tmp_path,
            {
                "file1.txt": "hello",
                "subdir/file2.txt": "world",
            },
        )
        config = Config(
            profile_name="laptop-a",
            backup_root=tmp,
            exclude_dirs=[],
            exclude_files=[],
        )
        entries = list(scan(config))

        paths = {e.relative_path for e in entries}
        assert "file1.txt" in paths
        assert "subdir/file2.txt" in paths
        assert all(not e.is_skipped for e in entries)

    def test_excludes_directories(self, tmp_path: Path) -> None:
        tmp = str(tmp_path)
        write_tree(
            tmp_path,
            {
                "keep.txt": "keep",
                "venv/lib/something.py": "skip",
                "__pycache__/cached.pyc": "skip",
            },
        )
        config = Config(
            profile_name="laptop-a",
            backup_root=tmp,
            exclude_dirs=["venv", "__pycache__"],
            exclude_files=[],
        )
        entries = list(scan(config))
        paths = {e.relative_path for e in entries}

        assert "keep.txt" in paths
        assert "venv/lib/something.py" not in paths
        assert "__pycache__/cached.pyc" not in paths

    def test_default_generated_directories_are_pruned(self, tmp_path: Path) -> None:
        tmp = str(tmp_path)
        write_tree(
            tmp_path,
            {
                "src/app.py": "print('kept')",
                "dist/bundle.js": "generated",
                "build/output.js": "generated",
                ".omp/state.json": "scaffolding",
                "demo.egg-info/PKG-INFO": "packaging metadata",
                "__pycache__/cached.pyc": "bytecode",
                ".cache/tool/index": "tool cache",
                "cache/archive.bin": "generic cache",
                ".npm/_cacache/index-v5/entry": "package cache",
                ".hypothesis/examples/demo": "property-test cache",
                ".turbo/run.json": "js task cache",
            },
        )
        config = Config(profile_name="laptop-a", backup_root=tmp)

        entries = list(scan(config))

        paths = {e.relative_path for e in entries}
        assert paths == {"src/app.py"}
        assert all(not e.is_skipped for e in entries)

    def test_default_generated_file_patterns_are_marked_skipped(
        self, tmp_path: Path
    ) -> None:
        tmp = str(tmp_path)
        write_tree(
            tmp_path,
            {
                "src/main.ts": "console.log('kept')",
                "frontend.tsbuildinfo": "generated",
                "module.pyc": "bytecode",
                "module.pyo": "optimized bytecode",
                ".coverage": "coverage database",
                ".coverage.integration": "coverage shard",
                "bundle.cache": "generic cache file",
                ".eslintcache": "linter cache",
                ".dmypy.json": "mypy daemon cache",
            },
        )
        config = Config(profile_name="laptop-a", backup_root=tmp)

        entries = list(scan(config))

        kept_paths = {e.relative_path for e in entries if not e.is_skipped}
        skipped_by_pattern = {
            e.relative_path
            for e in entries
            if e.is_skipped and e.skip_reason == "excluded_by_pattern"
        }
        assert kept_paths == {"src/main.ts"}
        assert skipped_by_pattern == {
            "frontend.tsbuildinfo",
            "module.pyc",
            "module.pyo",
            ".coverage",
            ".coverage.integration",
            "bundle.cache",
            ".eslintcache",
            ".dmypy.json",
        }

    def test_default_harness_html_exports_are_skipped_but_json_is_kept(
        self, tmp_path: Path
    ) -> None:
        tmp = str(tmp_path)
        write_tree(
            tmp_path,
            {
                "Code/z-archive/harness-info/index.html": "landing page",
                "Code/z-archive/harness-info/llm-call-exports/history.html": "export",
                "Code/z-archive/harness-info/llm-call-exports/source.jsonl": "source",
                "Code/z-archive/harness-info/research-results/overview.html": "overview",
                "Code/z-archive/harness-info/research-results/metadata.json": "metadata",
                "_machine_state/scheduled_tasks.json": "machine state",
            },
        )
        config = Config(profile_name="laptop-a", backup_root=tmp)

        entries = list(scan(config))

        kept_paths = {entry.relative_path for entry in entries if not entry.is_skipped}
        skipped_by_path = {
            entry.relative_path
            for entry in entries
            if entry.is_skipped and entry.skip_reason == "excluded_by_path_pattern"
        }
        assert kept_paths == {
            "Code/z-archive/harness-info/llm-call-exports/source.jsonl",
            "Code/z-archive/harness-info/research-results/metadata.json",
            "_machine_state/scheduled_tasks.json",
        }
        assert skipped_by_path == {
            "Code/z-archive/harness-info/index.html",
            "Code/z-archive/harness-info/llm-call-exports/history.html",
            "Code/z-archive/harness-info/research-results/overview.html",
        }

    def test_include_path_patterns_preserve_omp_jsonl_only(
        self, tmp_path: Path
    ) -> None:
        tmp = str(tmp_path)
        write_tree(
            tmp_path,
            {
                ".omp/agent/sessions/-Code/session.jsonl": "source",
                ".omp/agent/sessions/direct.jsonl": "direct source",
                ".omp/agent/sessions/-Code/context.md": "derived context",
                ".omp/agent/agent.db": "runtime state",
                "_machine_state/network.json": "machine state",
            },
        )
        config = Config(
            profile_name="laptop-a",
            backup_root=tmp,
            exclude_path_patterns=[".omp/*"],
            include_path_patterns=[
                ".omp/agent/sessions/*.jsonl",
                ".omp/agent/sessions/**/*.jsonl",
            ],
        )

        entries = list(scan(config))

        kept_paths = {entry.relative_path for entry in entries if not entry.is_skipped}
        skipped_by_path = {
            entry.relative_path
            for entry in entries
            if entry.is_skipped and entry.skip_reason == "excluded_by_path_pattern"
        }
        assert kept_paths == {
            ".omp/agent/sessions/-Code/session.jsonl",
            ".omp/agent/sessions/direct.jsonl",
            "_machine_state/network.json",
        }
        assert skipped_by_path == {
            ".omp/agent/agent.db",
            ".omp/agent/sessions/-Code/context.md",
        }

    def test_excludes_file_patterns(self, tmp_path: Path) -> None:
        tmp = str(tmp_path)
        write_tree(
            tmp_path,
            {
                "keep.txt": "keep",
                "Thumbs.db": "skip",
                "desktop.ini": "skip",
            },
        )
        config = Config(
            profile_name="laptop-a",
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

    def test_size_limit_skips_large_files(self, tmp_path: Path) -> None:
        tmp = str(tmp_path)
        write_tree(
            tmp_path,
            {
                "small.txt": "x",
                "big.txt": "x" * 2000,
            },
        )
        config = Config(
            profile_name="laptop-a",
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

    def test_type_excluded_files(self, tmp_path: Path) -> None:
        tmp = str(tmp_path)
        write_tree(
            tmp_path,
            {
                "app.exe": "binary",
                "doc.txt": "text",
            },
        )
        config = Config(
            profile_name="laptop-a",
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

    def test_media_files_bypass_size_limit(self, tmp_path: Path) -> None:
        tmp = str(tmp_path)
        # Create a "large" jpg that exceeds the default limit
        write_tree(
            tmp_path,
            {
                "photo.jpg": "x" * 5000,
            },
        )
        config = Config(
            profile_name="laptop-a",
            backup_root=tmp,
            exclude_dirs=[],
            exclude_files=[],
            max_file_size_mb=0.001,  # ~1 KB — but .jpg has no limit
        )
        entries = list(scan(config))

        assert len(entries) == 1
        assert not entries[0].is_skipped

    def test_skipped_files_have_metadata(self, tmp_path: Path) -> None:
        tmp = str(tmp_path)
        write_tree(tmp_path, {"Thumbs.db": "data"})
        config = Config(
            profile_name="laptop-a",
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

    def test_path_pattern_exclusion(self, tmp_path: Path) -> None:
        tmp = str(tmp_path)
        write_tree(
            tmp_path,
            {
                "keep.txt": "keep",
                "lectures/open-law-notes/rec.wav": "skip",
            },
        )
        config = Config(
            profile_name="laptop-a",
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

    def test_specific_file_exclusion(self, tmp_path: Path) -> None:
        tmp = str(tmp_path)
        write_tree(
            tmp_path,
            {
                "keep.txt": "keep",
                "docs/secret.pdf": "skip",
            },
        )
        config = Config(
            profile_name="laptop-a",
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
            profile_name="laptop-a",
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

    def test_windows_long_path_keeps_manifest_identity(
        self, tmp_path: Path, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        import drive_backup.scanner as scanner

        long_name = f"{'a' * 40}.txt"
        (tmp_path / long_name).write_text("content")
        monkeypatch.setattr(scanner, "_WIN32", True)
        monkeypatch.setattr(scanner, "_MAX_PATH", 10)

        config = Config(
            profile_name="laptop-a",
            backup_root=str(tmp_path),
            exclude_dirs=[],
            exclude_files=[],
        )
        entries = list(scan(config))

        assert entries[0].relative_path == long_name
