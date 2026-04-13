"""Tests for report generation and saving."""

from __future__ import annotations

import json
import os
import tempfile

from drive_backup.report import (
    BackupStats,
    ErrorFile,
    SkippedFile,
    generate_report,
    save_report,
)
from drive_backup.utils import human_size


class TestHumanSize:
    def test_bytes(self) -> None:
        assert human_size(500) == "500.0 B"

    def test_kilobytes(self) -> None:
        assert human_size(2048) == "2.0 KB"

    def test_megabytes(self) -> None:
        assert human_size(5 * 1024 * 1024) == "5.0 MB"

    def test_gigabytes(self) -> None:
        assert human_size(3 * 1024**3) == "3.0 GB"

    def test_zero(self) -> None:
        assert human_size(0) == "0.0 B"

    def test_very_large(self) -> None:
        result = human_size(5 * 1024**5)
        assert "PB" in result


class TestBackupStats:
    def test_duration_seconds(self) -> None:
        stats = BackupStats(start_time=100.0, end_time=150.0)
        assert stats.duration_seconds == 50.0

    def test_duration_human_seconds_only(self) -> None:
        stats = BackupStats(start_time=0, end_time=45)
        assert stats.duration_human == "45s"

    def test_duration_human_with_minutes(self) -> None:
        stats = BackupStats(start_time=0, end_time=125)
        assert stats.duration_human == "2m 5s"

    def test_duration_human_with_hours(self) -> None:
        stats = BackupStats(start_time=0, end_time=3665)
        assert stats.duration_human == "1h 1m 5s"


class TestGenerateReport:
    def test_report_has_required_keys(self) -> None:
        stats = BackupStats(
            backup_root="C:\\Users\\test",
            dry_run=True,
            files_scanned=100,
            files_uploaded=10,
            files_skipped_dedup=80,
            files_skipped_exclusion=5,
            files_skipped_error=2,
            bytes_uploaded=1024,
            bytes_total_eligible=8192,
            start_time=0,
            end_time=10,
        )
        report = generate_report(stats)
        assert report["backup_root"] == "C:\\Users\\test"
        assert report["dry_run"] is True
        assert report["files_scanned"] == 100
        assert report["files_uploaded"] == 10
        assert report["total_files_eligible"] == 90
        assert report["total_bytes_uploaded"] == 1024

    def test_report_includes_skipped_files(self) -> None:
        stats = BackupStats(
            skipped_files=[
                SkippedFile(
                    path="/test/file.exe",
                    relative_path="file.exe",
                    size_bytes=1024,
                    size_human="1.0 KB",
                    modified="2024-01-01",
                    reason="type_excluded",
                    extension=".exe",
                )
            ],
        )
        report = generate_report(stats)
        assert len(report["skipped_files"]) == 1  # type: ignore[arg-type]
        assert report["skipped_files"][0]["reason"] == "type_excluded"  # type: ignore[index]

    def test_report_includes_error_files(self) -> None:
        stats = BackupStats(
            error_files=[
                ErrorFile(
                    path="/test/locked.dat",
                    relative_path="locked.dat",
                    error="Permission denied",
                )
            ],
        )
        report = generate_report(stats)
        assert len(report["error_files"]) == 1  # type: ignore[arg-type]
        assert report["error_files"][0]["error"] == "Permission denied"  # type: ignore[index]


class TestSaveReport:
    def test_saves_valid_json(self) -> None:
        report = generate_report(BackupStats(start_time=0, end_time=1))
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            path = f.name

        save_report(report, path)

        with open(path, encoding="utf-8") as f:
            loaded = json.load(f)

        os.unlink(path)

        assert loaded["duration_seconds"] == 1.0
        assert isinstance(loaded["skipped_files"], list)
