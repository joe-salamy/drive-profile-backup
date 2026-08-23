"""Tests for bounded profile summary aggregation and rendering."""

from datetime import date
from io import StringIO
from tempfile import SpooledTemporaryFile
from typing import TextIO, cast

import pytest

from drive_backup.config import Config
from drive_backup.scanner import FileEntry
from scripts import generate_summary as summary_script


def _entry(
    relative_path: str,
    size: int,
    *,
    skipped: bool = False,
    reason: str = "",
) -> FileEntry:
    return FileEntry(
        path=relative_path,
        relative_path=relative_path,
        size=size,
        mtime=1.0,
        is_skipped=skipped,
        skip_reason=reason,
    )


def _config() -> Config:
    return Config(profile_name="laptop-a", backup_root="/profile")


def test_collects_aggregates_and_renders_current_sections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = [
        _entry("docs/a.txt", 100),
        _entry("photos/b.jpg", 300),
        _entry("cache/c.bin", 50, skipped=True, reason="size_limit 10 MB"),
        _entry("locked.dat", 0, skipped=True, reason="stat_error denied"),
    ]
    monkeypatch.setattr(summary_script, "scan", lambda config: iter(entries))
    errors = StringIO()

    stats = summary_script._collect_summary(_config(), errors)
    output = StringIO()
    summary_script._write_summary(
        output,
        _config(),
        stats,
        errors,
        date(2026, 7, 17),
    )
    report = output.getvalue()

    assert stats.eligible_count == 2
    assert stats.eligible_size == 400
    assert stats.folder_stats["docs"] == {"count": 1, "size": 100}
    assert stats.extension_stats[".jpg"] == {"count": 1, "size": 300}
    assert stats.skip_reasons["size_limit"] == {"count": 1, "size": 50}
    assert "# Profile Backup Summary (2026-07-17)" in report
    assert report.index("## Breakdown by Root Folder") < report.index(
        "## Breakdown by File Type"
    )
    assert report.index("## Top 25 Largest Files") < report.index("## Skipped Files")
    assert "- `locked.dat`: stat_error denied" in report
    assert report.endswith("- `locked.dat`: stat_error denied\n")


def test_largest_samples_are_deterministic_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eligible = [_entry(f"eligible/{index:02}.txt", index) for index in range(30)]
    skipped = [
        _entry(
            f"skipped/{index:02}.bin",
            index,
            skipped=True,
            reason="size_limit exceeded",
        )
        for index in range(15)
    ]
    monkeypatch.setattr(
        summary_script,
        "scan",
        lambda config: iter([*eligible, *skipped]),
    )

    stats = summary_script._collect_summary(_config(), StringIO())

    assert len(stats.largest_eligible) == 25
    assert {path for _, path, _ in stats.largest_eligible} == {
        f"eligible/{index:02}.txt" for index in range(5, 30)
    }
    assert len(stats.largest_skipped) == 10
    assert {path for _, path, _ in stats.largest_skipped} == {
        f"skipped/{index:02}.bin" for index in range(5, 15)
    }


def test_empty_scan_renders_zero_totals(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(summary_script, "scan", lambda config: iter(()))
    errors = StringIO()

    stats = summary_script._collect_summary(_config(), errors)
    output = StringIO()
    summary_script._write_summary(
        output,
        _config(),
        stats,
        errors,
        date(2026, 7, 17),
    )

    assert stats.eligible_count == 0
    assert "across 0 files" in output.getvalue()
    assert output.getvalue().endswith("## Errors (0 files)\n\nNo errors.\n")


def test_error_spool_rolls_and_preserves_every_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = [
        _entry(
            f"locked/{index:03}.dat",
            0,
            skipped=True,
            reason=f"stat_error {'x' * 6000}",
        )
        for index in range(200)
    ]
    monkeypatch.setattr(summary_script, "scan", lambda config: iter(entries))

    with SpooledTemporaryFile(
        mode="w+",
        max_size=1_048_576,
        encoding="utf-8",
    ) as errors:
        error_stream = cast(TextIO, errors)
        stats = summary_script._collect_summary(_config(), error_stream)
        assert errors._rolled is True  # type: ignore[attr-defined]
        output = StringIO()
        summary_script._write_summary(
            output,
            _config(),
            stats,
            error_stream,
            date(2026, 7, 17),
        )

    report = output.getvalue()
    assert stats.error_count == len(entries)
    assert report.count("stat_error") == len(entries)
    assert "locked/000.dat" in report
    assert "locked/199.dat" in report
