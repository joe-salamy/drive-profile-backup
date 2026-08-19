"""Backup orchestrator tying scanner, dedup, Drive API, and reporting together."""

from __future__ import annotations

import logging
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import TYPE_CHECKING

from drive_backup.config import Config
from drive_backup.dedup import Manifest, ManifestEntry, compute_md5, needs_upload
from drive_backup.machine_state import (
    MACHINE_STATE_DIRECTORY,
    CollectorOutcome,
    CollectorStatus,
    collect_machine_state,
)
from drive_backup.report import (
    BackupReport,
    BackupStats,
    ErrorFile,
    PruneError,
    PrunedFile,
    SkippedFile,
    UploadFile,
    generate_report,
    save_report,
)
from drive_backup.scanner import FileEntry, scan
from drive_backup.utils import human_size

if TYPE_CHECKING:
    from drive_backup.drive_api import DriveAPI

logger = logging.getLogger(__name__)


class ProgressKind(StrEnum):
    SKIPPED = "skipped"
    DEDUP = "dedup"
    WOULD_UPLOAD = "would_upload"
    UPLOADED = "uploaded"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    kind: ProgressKind
    reason: str = ""


class ManifestProgressError(RuntimeError):
    """Raised when manifest progress cannot be saved durably."""


class BackupEngine:
    """Orchestrates the full backup flow."""

    def __init__(
        self,
        config: Config,
        dry_run: bool = False,
        full: bool = False,
        prune: bool = False,
        prune_mode: str = "flag",
        collect_machine_state_snapshot: bool = True,
    ) -> None:
        if prune_mode not in ("flag", "trash"):
            raise ValueError("prune_mode must be 'flag' or 'trash'")
        self.config = config
        self.dry_run = dry_run
        self.full = full  # Ignore manifest, re-upload everything
        self.prune = prune
        self.prune_mode = prune_mode
        self.collect_machine_state_snapshot = collect_machine_state_snapshot
        self.stats = BackupStats(
            backup_root=config.backup_root,
            dry_run=dry_run,
            profile_name=config.profile_name,
            prune_enabled=prune,
            prune_mode=prune_mode,
        )
        self.manifest = Manifest()
        self.drive: DriveAPI | None = None  # Lazy: imported only when needed
        self._root_folder_id: str = ""
        self._prune_protected_paths: set[str] = set()

    def run(
        self,
        progress_callback: Callable[[FileEntry, ProgressEvent], None] | None = None,
    ) -> BackupReport:
        """Execute the full backup and return the report dict.

        Args:
            progress_callback: Optional callable(file: FileEntry, event: ProgressEvent)
                called for each file processed. Used by CLI for progress display.
        """
        self.stats.start_time = time.time()
        backup_root_available = os.path.isdir(self.config.backup_root)

        # Load manifest (unless --full forces re-upload)
        if not self.full:
            self.manifest = Manifest.load(self.config.manifest_path)
            logger.info(
                "Loaded manifest: %d existing entries", len(self.manifest.entries)
            )
        else:
            logger.info("Full mode: ignoring manifest, will re-upload everything")

        if self.collect_machine_state_snapshot and backup_root_available:
            self.stats.machine_state_refreshed = True
            try:
                self.stats.machine_state_collectors = collect_machine_state(
                    self.config.backup_root, self.config.machine_state_collectors
                )
            except Exception as error:
                warning = f"Unexpected machine-state refresh failure: {error}"
                logger.warning(warning)
                failed_names = [*self.config.machine_state_collectors, "snapshot"]
                self.stats.machine_state_collectors = []
                for name in failed_names:
                    filename = "snapshot.json" if name == "snapshot" else f"{name}.json"
                    relative_path = f"{MACHINE_STATE_DIRECTORY}/{filename}"
                    retained = os.path.isfile(
                        os.path.join(self.config.backup_root, *relative_path.split("/"))
                    )
                    self.stats.machine_state_collectors.append(
                        CollectorOutcome(
                            name=name,
                            status=CollectorStatus.FAILED,
                            output_file=relative_path if retained else None,
                            warnings=(warning,),
                            previous_output_retained=retained,
                        )
                    )
        elif self.collect_machine_state_snapshot:
            self.stats.machine_state_collectors = [
                CollectorOutcome(
                    name="snapshot",
                    status=CollectorStatus.FAILED,
                    output_file=None,
                    warnings=(
                        "Backup root is unavailable; machine-state refresh skipped",
                    ),
                    previous_output_retained=False,
                )
            ]

        for outcome in self.stats.machine_state_collectors:
            if outcome.status is CollectorStatus.FAILED:
                filename = (
                    "snapshot.json"
                    if outcome.name == "snapshot"
                    else f"{outcome.name}.json"
                )
                self._prune_protected_paths.add(f"{MACHINE_STATE_DIRECTORY}/{filename}")

        # Authenticate to Drive (unless dry-run)
        if not self.dry_run:
            from drive_backup.drive_api import DriveAPI

            self.drive = DriveAPI(
                credentials_path=self.config.credentials_path,
                token_path=self.config.token_path,
                writes_per_second=self.config.writes_per_second,
                max_retries=self.config.max_retries,
            )
            self.drive.authenticate()
            self._root_folder_id = self._resolve_backup_folder()
            self.stats.drive_folder_id = self._root_folder_id
            self.stats.drive_folder_url = (
                f"https://drive.google.com/drive/folders/{self._root_folder_id}"
            )
            self._maybe_download_manifest_snapshot()

        # Scan and process files
        for file_entry in scan(self.config):
            self.stats.files_scanned += 1
            self._process_file(file_entry, progress_callback)

        if self.prune:
            if self.full:
                self.stats.prune_skipped_reason = (
                    "Skipped prune because --full ignores the manifest"
                )
            elif not backup_root_available:
                self.stats.prune_skipped_reason = (
                    "Skipped prune because backup root is unavailable"
                )
            elif not self.dry_run and self.stats.files_skipped_error > 0:
                self.stats.prune_skipped_reason = (
                    "Skipped prune because backup had file or upload errors"
                )
            else:
                self._prune_stale_manifest_entries()

        self.stats.end_time = time.time()

        # Save manifest
        if not self.dry_run:
            self.manifest.save(self.config.manifest_path)
            self._upload_manifest_snapshot()

        # Generate report
        report = generate_report(self.stats)

        # Save report locally
        report_dir = os.path.join(
            os.path.dirname(os.path.expanduser(self.config.manifest_path)),
            "reports",
        )
        os.makedirs(report_dir, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
        prefix = "dry-run-" if self.dry_run else ""
        report_path = os.path.join(report_dir, f"{prefix}backup-{timestamp}.json")
        save_report(report, report_path)
        logger.info("Report saved to %s", report_path)

        # Upload report to Drive
        if not self.dry_run and self.drive:
            try:
                reports_folder_id = self.drive.get_or_create_folder(
                    "_reports", self._root_folder_id
                )
                self.drive.upload_file(report_path, reports_folder_id)
                logger.info("Report uploaded to Drive/_reports/")
            except Exception as e:
                logger.warning("Report upload failed: %s", e)

        return report

    def _resolve_backup_folder(self) -> str:
        """Return the Drive folder ID used as the backup root."""
        assert self.drive is not None

        parent_id = self.drive.get_or_create_folder(
            self.config.drive_parent_folder_name
        )
        self.stats.drive_parent_folder_id = parent_id
        return self.drive.get_or_create_folder(self.config.profile_name, parent_id)

    def _save_manifest_progress(self) -> None:
        """Persist manifest progress after a completed Drive side effect."""
        try:
            self.manifest.save(self.config.manifest_path)
        except Exception as e:
            raise ManifestProgressError("Could not save manifest progress") from e

    def _upload_manifest_snapshot(self) -> None:
        """Upload the local manifest as a snapshot to Drive/_meta/manifest.json."""
        if self.drive is None:
            return
        try:
            meta_id = self.drive.get_or_create_folder("_meta", self._root_folder_id)
            found = self.drive.find_file_by_name_and_parent("manifest.json", meta_id)
            if found is not None:
                self.drive.update_file(found["id"], self.config.manifest_path)
            else:
                self.drive.upload_file(self.config.manifest_path, meta_id)
            self.stats.manifest_snapshot_uploaded = True
        except Exception as e:
            self.stats.manifest_snapshot_error = str(e)
            logger.warning("Manifest snapshot upload failed: %s", e)

    def _maybe_download_manifest_snapshot(self) -> None:
        """Restore a fresh device's manifest from the Drive snapshot."""
        if self.drive is None or self.full or os.path.exists(self.config.manifest_path):
            return
        try:
            meta_id = self.drive.get_or_create_folder("_meta", self._root_folder_id)
            found = self.drive.find_file_by_name_and_parent("manifest.json", meta_id)
            if found is None:
                return
            manifest_path = os.path.expanduser(self.config.manifest_path)
            os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
            tmp_path = manifest_path + ".snapshot.tmp"
            try:
                self.drive.download_file(found["id"], tmp_path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
            os.replace(tmp_path, manifest_path)
            self.manifest = Manifest.load(manifest_path)
            self.stats.manifest_snapshot_downloaded = True
            logger.info(
                "Downloaded manifest snapshot: %d entries",
                len(self.manifest.entries),
            )
        except Exception as e:
            logger.warning("Manifest snapshot download failed: %s", e)

    def _process_file(
        self,
        file: FileEntry,
        progress_callback: Callable[[FileEntry, ProgressEvent], None] | None = None,
    ) -> None:
        """Process a single file: check exclusions, dedup, upload."""
        # Skipped by scanner (exclusion or error)
        if file.is_skipped:
            if "error" in file.skip_reason:
                self.stats.files_skipped_error += 1
                self.stats.error_files.append(
                    ErrorFile(
                        path=file.path,
                        relative_path=file.relative_path,
                        error=file.skip_reason,
                    )
                )
            else:
                self.stats.files_skipped_exclusion += 1
                self.stats.skipped_files.append(
                    SkippedFile(
                        path=file.path,
                        relative_path=file.relative_path,
                        size_bytes=file.size,
                        size_human=file.size_human,
                        modified=_format_mtime(file.mtime),
                        reason=file.skip_reason,
                        extension=file.extension,
                    )
                )
            if progress_callback:
                progress_callback(file, ProgressEvent(ProgressKind.SKIPPED))
            return

        # Eligible file — track total
        self.stats.bytes_total_eligible += file.size

        # Dedup check
        should_upload, reason = needs_upload(file, self.manifest)
        if not should_upload:
            self.stats.files_skipped_dedup += 1
            if progress_callback:
                progress_callback(file, ProgressEvent(ProgressKind.DEDUP, reason))
            return

        # Upload (or simulate in dry-run)
        if self.dry_run:
            self.stats.files_uploaded += 1
            self.stats.bytes_uploaded += file.size
            self._record_upload(file)
            if progress_callback:
                progress_callback(
                    file, ProgressEvent(ProgressKind.WOULD_UPLOAD, reason)
                )
            return

        try:
            self._upload_file(file, reason)
            self.stats.files_uploaded += 1
            self.stats.bytes_uploaded += file.size
            self._record_upload(file)
            if progress_callback:
                progress_callback(file, ProgressEvent(ProgressKind.UPLOADED, reason))
        except ManifestProgressError:
            raise
        except Exception as e:
            logger.error("Failed to upload %s: %s", file.path, e)
            self.stats.files_skipped_error += 1
            self.stats.error_files.append(
                ErrorFile(
                    path=file.path,
                    relative_path=file.relative_path,
                    error=str(e),
                )
            )
            if progress_callback:
                progress_callback(file, ProgressEvent(ProgressKind.ERROR))

    def _record_upload(self, file: FileEntry) -> None:
        """Track an uploaded (or would-be-uploaded) file for reporting."""
        self.stats.uploaded_files.append(
            UploadFile(
                relative_path=file.relative_path,
                size_bytes=file.size,
                size_human=file.size_human,
                extension=file.extension or "(no extension)",
            )
        )

    def _local_path_for_manifest_key(self, relative_path: str) -> str:
        """Return the local filesystem path represented by a manifest key."""
        if os.path.isabs(relative_path):
            local_path = relative_path.replace("/", os.sep)
        else:
            local_path = os.path.join(
                self.config.backup_root, *relative_path.split("/")
            )

        if (
            sys.platform == "win32"
            and len(local_path) >= 260
            and not local_path.startswith("\\\\?\\")
        ):
            local_path = "\\\\?\\" + local_path

        return local_path

    def _manifest_key_exists_locally(self, relative_path: str) -> bool:
        """Return whether a manifest key still points to a local file."""
        return os.path.isfile(self._local_path_for_manifest_key(relative_path))

    def _stale_manifest_entries(
        self, include_pruned: bool = True
    ) -> list[tuple[str, ManifestEntry]]:
        """Return manifest entries whose local files no longer exist."""
        return [
            (rel_path, entry)
            for rel_path, entry in sorted(self.manifest.entries.items())
            if rel_path not in self._prune_protected_paths
            and not self._manifest_key_exists_locally(rel_path)
            and (include_pruned or not entry.pruned)
        ]

    def _record_pruned_file(self, relative_path: str, entry: ManifestEntry) -> None:
        """Track a pruned (or would-be-pruned) Drive file for reporting."""
        self.stats.files_pruned += 1
        self.stats.bytes_pruned += entry.size
        self.stats.pruned_files.append(
            PrunedFile(
                relative_path=relative_path,
                drive_file_id=entry.drive_file_id,
                size_bytes=entry.size,
                size_human=human_size(entry.size),
            )
        )

    def _prune_stale_manifest_entries(self) -> None:
        """Mark stale Drive files as pruned, or trash them in trash mode."""
        include_pruned = self.prune_mode == "trash"
        for relative_path, entry in self._stale_manifest_entries(
            include_pruned=include_pruned
        ):
            if not entry.drive_file_id:
                self.stats.files_prune_failed += 1
                self.stats.prune_error_files.append(
                    PruneError(
                        relative_path=relative_path,
                        drive_file_id=entry.drive_file_id,
                        error="missing Drive file ID",
                    )
                )
                continue

            if self.dry_run:
                self._record_pruned_file(relative_path, entry)
                continue

            if self.prune_mode == "flag":
                entry.pruned = True
                try:
                    self._save_manifest_progress()
                except ManifestProgressError:
                    entry.pruned = False
                    raise
                self._record_pruned_file(relative_path, entry)
                continue

            try:
                assert self.drive is not None
                self.drive.trash_file(entry.drive_file_id)
                removed = self.manifest.remove(relative_path)
                try:
                    self._save_manifest_progress()
                except ManifestProgressError:
                    self.manifest.entries[relative_path] = removed or entry
                    raise
                self._record_pruned_file(relative_path, entry)
            except ManifestProgressError:
                raise
            except Exception as e:
                logger.error("Failed to prune %s: %s", relative_path, e)
                self.stats.files_prune_failed += 1
                self.stats.prune_error_files.append(
                    PruneError(
                        relative_path=relative_path,
                        drive_file_id=entry.drive_file_id,
                        error=str(e),
                    )
                )

    def _upload_file(self, file: FileEntry, reason: str) -> None:
        """Upload a single file to Drive and update the manifest."""
        assert self.drive is not None

        # Determine the parent folder on Drive
        rel_dir = os.path.dirname(file.relative_path)
        if rel_dir:
            path_parts = rel_dir.split("/")
            parent_id = self.drive.ensure_folder_path(path_parts, self._root_folder_id)
        else:
            parent_id = self._root_folder_id

        resumable = file.size > self.config.resumable_threshold_bytes

        # Update existing file, reconcile an orphaned same-name file, or upload new one
        existing = self.manifest.get(file.relative_path)
        if (
            existing
            and existing.drive_file_id
            and reason
            in (
                "content_changed",
                "size_changed",
                "md5_error",
                "restored",
            )
        ):
            result = self.drive.update_file(
                existing.drive_file_id, file.path, resumable=resumable
            )
        else:
            filename = os.path.basename(file.path)
            found = self.drive.find_file_by_name_and_parent(filename, parent_id)
            if found is not None:
                result = self.drive.update_file(
                    found["id"], file.path, resumable=resumable
                )
            else:
                result = self.drive.upload_file(
                    file.path, parent_id, resumable=resumable
                )

        # Update manifest with Drive's response
        md5 = result.get("md5Checksum", "")
        if not md5:
            md5 = compute_md5(file.path) or ""

        self.manifest.set(
            relative_path=file.relative_path,
            md5=md5,
            size=file.size,
            mtime=file.mtime,
            drive_file_id=result["id"],
            drive_parent_id=parent_id,
        )
        self._save_manifest_progress()


def _format_mtime(mtime: float) -> str:
    """Format a modification time as ISO string."""
    if mtime == 0:
        return ""
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
