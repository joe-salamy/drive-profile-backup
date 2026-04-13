"""Generate rich JSON metadata reports for backup runs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class SkippedFile:
    """A file that was skipped during backup."""

    path: str
    relative_path: str
    size_bytes: int
    size_human: str
    modified: str
    reason: str
    extension: str


@dataclass
class ErrorFile:
    """A file that encountered an error during backup."""

    path: str
    relative_path: str
    error: str


@dataclass
class BackupStats:
    """Accumulated statistics for a backup run."""

    backup_root: str = ""
    dry_run: bool = False
    files_scanned: int = 0
    files_uploaded: int = 0
    files_skipped_dedup: int = 0
    files_skipped_exclusion: int = 0
    files_skipped_error: int = 0
    bytes_uploaded: int = 0
    bytes_total_eligible: int = 0
    start_time: float = 0
    end_time: float = 0
    drive_folder_id: str = ""
    drive_folder_url: str = ""
    skipped_files: list[SkippedFile] = field(default_factory=list)
    error_files: list[ErrorFile] = field(default_factory=list)
    excluded_directories: list[str] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        return self.end_time - self.start_time

    @property
    def duration_human(self) -> str:
        total = int(self.duration_seconds)
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        parts = []
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}m")
        parts.append(f"{seconds}s")
        return " ".join(parts)


def _human_size(num_bytes: int) -> str:
    """Convert bytes to human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num_bytes) < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024  # type: ignore[assignment]
    return f"{num_bytes:.1f} PB"


def generate_report(stats: BackupStats) -> dict:
    """Build the full JSON report structure from backup stats."""
    return {
        "backup_timestamp": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(stats.duration_seconds, 2),
        "duration_human": stats.duration_human,
        "backup_root": stats.backup_root,
        "dry_run": stats.dry_run,
        "files_scanned": stats.files_scanned,
        "files_uploaded": stats.files_uploaded,
        "files_skipped_dedup": stats.files_skipped_dedup,
        "files_skipped_exclusion": stats.files_skipped_exclusion,
        "files_skipped_error": stats.files_skipped_error,
        "total_files_eligible": (
            stats.files_uploaded + stats.files_skipped_dedup
        ),
        "total_bytes_uploaded": stats.bytes_uploaded,
        "total_size_uploaded_human": _human_size(stats.bytes_uploaded),
        "total_bytes_eligible": stats.bytes_total_eligible,
        "total_size_eligible_human": _human_size(stats.bytes_total_eligible),
        "drive_folder_id": stats.drive_folder_id,
        "drive_folder_url": stats.drive_folder_url,
        "skipped_files": [
            {
                "path": sf.path,
                "relative_path": sf.relative_path,
                "size_bytes": sf.size_bytes,
                "size_human": sf.size_human,
                "modified": sf.modified,
                "reason": sf.reason,
                "extension": sf.extension,
            }
            for sf in stats.skipped_files
        ],
        "error_files": [
            {
                "path": ef.path,
                "relative_path": ef.relative_path,
                "error": ef.error,
            }
            for ef in stats.error_files
        ],
        "excluded_directories_count": len(stats.excluded_directories),
    }


def save_report(report: dict, path: str) -> None:
    """Write report dict to a JSON file."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
