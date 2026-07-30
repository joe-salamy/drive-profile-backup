"""Generate rich JSON metadata reports for backup runs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TypedDict

from drive_backup.machine_state import CollectorOutcome
from drive_backup.utils import human_size


class SkippedFileRow(TypedDict):
    path: str
    relative_path: str
    size_bytes: int
    size_human: str
    modified: str
    reason: str
    extension: str


class UploadedFileRow(TypedDict):
    relative_path: str
    size_bytes: int
    size_human: str
    extension: str


class ErrorFileRow(TypedDict):
    path: str
    relative_path: str
    error: str


class PrunedFileRow(TypedDict):
    relative_path: str
    drive_file_id: str
    size_bytes: int
    size_human: str


class PruneErrorRow(TypedDict):
    relative_path: str
    drive_file_id: str
    error: str


class ExtensionBreakdownRow(TypedDict):
    extension: str
    count: int
    size_bytes: int
    size_human: str


class MachineStateCollectorRow(TypedDict):
    name: str
    status: str
    output_file: str | None
    warnings: list[str]
    previous_output_retained: bool


class BackupReport(TypedDict):
    backup_timestamp: str
    duration_seconds: float
    duration_human: str
    backup_root: str
    profile_name: str
    dry_run: bool
    files_scanned: int
    files_uploaded: int
    files_skipped_dedup: int
    files_skipped_exclusion: int
    files_skipped_error: int
    total_files_eligible: int
    total_bytes_uploaded: int
    total_size_uploaded_human: str
    total_bytes_eligible: int
    total_size_eligible_human: str
    drive_parent_folder_id: str
    drive_folder_id: str
    drive_folder_url: str
    prune_enabled: bool
    files_pruned: int
    files_prune_failed: int
    total_bytes_pruned: int
    total_size_pruned_human: str
    prune_skipped_reason: str
    pruned_files: list[PrunedFileRow]
    prune_error_files: list[PruneErrorRow]
    skipped_files: list[SkippedFileRow]
    uploaded_files: list[UploadedFileRow]
    extension_breakdown: list[ExtensionBreakdownRow]
    error_files: list[ErrorFileRow]
    excluded_directories_count: int
    machine_state_refreshed: bool
    machine_state_collectors: list[MachineStateCollectorRow]


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
class UploadFile:
    """A file that was (or will be) uploaded."""

    relative_path: str
    size_bytes: int
    size_human: str
    extension: str


@dataclass
class ErrorFile:
    """A file that encountered an error during backup."""

    path: str
    relative_path: str
    error: str


@dataclass
class PrunedFile:
    """A Drive file that was (or will be) pruned."""

    relative_path: str
    drive_file_id: str
    size_bytes: int
    size_human: str


@dataclass
class PruneError:
    """A Drive prune operation that failed."""

    relative_path: str
    drive_file_id: str
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
    profile_name: str = ""
    drive_parent_folder_id: str = ""
    drive_folder_id: str = ""
    drive_folder_url: str = ""
    skipped_files: list[SkippedFile] = field(default_factory=list)
    uploaded_files: list[UploadFile] = field(default_factory=list)
    error_files: list[ErrorFile] = field(default_factory=list)
    excluded_directories: list[str] = field(default_factory=list)
    prune_enabled: bool = False
    files_pruned: int = 0
    files_prune_failed: int = 0
    bytes_pruned: int = 0
    prune_skipped_reason: str = ""
    pruned_files: list[PrunedFile] = field(default_factory=list)
    prune_error_files: list[PruneError] = field(default_factory=list)
    machine_state_refreshed: bool = False
    machine_state_collectors: list[CollectorOutcome] = field(default_factory=list)

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


def _extension_breakdown(
    uploaded_files: list[UploadFile],
) -> list[ExtensionBreakdownRow]:
    """Aggregate uploaded files by extension, sorted by total size desc."""
    by_ext: dict[str, dict[str, int]] = {}
    for uploaded_file in uploaded_files:
        extension = uploaded_file.extension or "(no extension)"
        bucket = by_ext.setdefault(extension, {"count": 0, "size_bytes": 0})
        bucket["count"] += 1
        bucket["size_bytes"] += uploaded_file.size_bytes
    rows: list[ExtensionBreakdownRow] = [
        {
            "extension": extension,
            "count": data["count"],
            "size_bytes": data["size_bytes"],
            "size_human": human_size(data["size_bytes"]),
        }
        for extension, data in by_ext.items()
    ]
    rows.sort(key=lambda row: row["size_bytes"], reverse=True)
    return rows


def generate_report(stats: BackupStats) -> BackupReport:
    """Build the full JSON report structure from backup stats."""
    return {
        "backup_timestamp": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(stats.duration_seconds, 2),
        "duration_human": stats.duration_human,
        "backup_root": stats.backup_root,
        "profile_name": stats.profile_name,
        "dry_run": stats.dry_run,
        "files_scanned": stats.files_scanned,
        "files_uploaded": stats.files_uploaded,
        "files_skipped_dedup": stats.files_skipped_dedup,
        "files_skipped_exclusion": stats.files_skipped_exclusion,
        "files_skipped_error": stats.files_skipped_error,
        "total_files_eligible": (stats.files_uploaded + stats.files_skipped_dedup),
        "total_bytes_uploaded": stats.bytes_uploaded,
        "total_size_uploaded_human": human_size(stats.bytes_uploaded),
        "total_bytes_eligible": stats.bytes_total_eligible,
        "total_size_eligible_human": human_size(stats.bytes_total_eligible),
        "drive_parent_folder_id": stats.drive_parent_folder_id,
        "drive_folder_id": stats.drive_folder_id,
        "drive_folder_url": stats.drive_folder_url,
        "prune_enabled": stats.prune_enabled,
        "files_pruned": stats.files_pruned,
        "files_prune_failed": stats.files_prune_failed,
        "total_bytes_pruned": stats.bytes_pruned,
        "total_size_pruned_human": human_size(stats.bytes_pruned),
        "prune_skipped_reason": stats.prune_skipped_reason,
        "pruned_files": [
            {
                "relative_path": pf.relative_path,
                "drive_file_id": pf.drive_file_id,
                "size_bytes": pf.size_bytes,
                "size_human": pf.size_human,
            }
            for pf in stats.pruned_files
        ],
        "prune_error_files": [
            {
                "relative_path": pe.relative_path,
                "drive_file_id": pe.drive_file_id,
                "error": pe.error,
            }
            for pe in stats.prune_error_files
        ],
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
        "uploaded_files": [
            {
                "relative_path": uf.relative_path,
                "size_bytes": uf.size_bytes,
                "size_human": uf.size_human,
                "extension": uf.extension,
            }
            for uf in stats.uploaded_files
        ],
        "extension_breakdown": _extension_breakdown(stats.uploaded_files),
        "error_files": [
            {
                "path": ef.path,
                "relative_path": ef.relative_path,
                "error": ef.error,
            }
            for ef in stats.error_files
        ],
        "excluded_directories_count": len(stats.excluded_directories),
        "machine_state_refreshed": stats.machine_state_refreshed,
        "machine_state_collectors": [
            {
                "name": outcome.name,
                "status": outcome.status.value,
                "output_file": outcome.output_file,
                "warnings": list(outcome.warnings),
                "previous_output_retained": outcome.previous_output_retained,
            }
            for outcome in stats.machine_state_collectors
        ],
    }


def save_report(report: BackupReport, path: str) -> None:
    """Write report dict to a JSON file."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
