"""Backup orchestrator tying scanner, dedup, Drive API, and reporting together."""

from __future__ import annotations

import logging
import os
import sys
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
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


MANIFEST_CHECKPOINT_INTERVAL_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class UploadWork:
    file: FileEntry
    reason: str
    existing_drive_file_id: str | None


@dataclass(frozen=True, slots=True)
class UploadResult:
    md5: str
    drive_file_id: str
    drive_parent_id: str


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
        self._manifest_dirty: bool = False
        self._last_manifest_checkpoint_at: float = time.monotonic()

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

        # Init checkpoint tracking for this run
        self._manifest_dirty = False
        self._last_manifest_checkpoint_at = time.monotonic()

        # Scan and process files
        if self.dry_run:
            for file_entry in scan(self.config):
                self.stats.files_scanned += 1
                # _prepare_upload handles skips/dedup/dry-run accounting
                self._prepare_upload(file_entry, progress_callback)
            # prune handling for dry-run (serial, no checkpoint)
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
            # Dry-run never saves manifest or uploads snapshot/report
            report = generate_report(self.stats)
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
            return report

        # Non-dry-run: concurrent pipeline
        futures: dict[Future[UploadResult], UploadWork] = {}
        executor: ThreadPoolExecutor | None = None
        final_checkpoint_failed = False
        try:
            executor = ThreadPoolExecutor(max_workers=self.config.upload_workers)
            # Wrap scan/upload loop to handle KeyboardInterrupt and checkpoint errors
            try:
                for file_entry in scan(self.config):
                    self.stats.files_scanned += 1
                    # Prepare upload work (handles skips/dedup)
                    try:
                        work = self._prepare_upload(file_entry, progress_callback)
                    except ManifestProgressError:
                        raise
                    if work is None:
                        # Check periodic checkpoint after skip/dedup
                        try:
                            self._maybe_save_manifest_progress()
                        except ManifestProgressError:
                            # Fatal: cancel pending, wait for running, propagate
                            for f in list(futures.keys()):
                                f.cancel()
                            if futures:
                                wait(list(futures.keys()))
                            raise
                        continue

                    # Submit to worker pool
                    future = executor.submit(self._execute_upload, work)
                    futures[future] = work

                    # Non-blockingly drain completed futures after each submission
                    done_now = [f for f in list(futures.keys()) if f.done()]
                    for fut in done_now:
                        w = futures.pop(fut)
                        try:
                            result = fut.result()
                        except Exception as exc:
                            self._record_upload_error(w, exc, progress_callback)
                        else:
                            try:
                                self._complete_upload(w, result, progress_callback)
                            except ManifestProgressError:
                                for ff in list(futures.keys()):
                                    ff.cancel()
                                if futures:
                                    wait(list(futures.keys()))
                                raise
                    try:
                        self._maybe_save_manifest_progress()
                    except ManifestProgressError:
                        for f in list(futures.keys()):
                            f.cancel()
                        if futures:
                            wait(list(futures.keys()))
                        raise

                    # Enforce bound: at most 2*workers in-flight
                    while len(futures) >= 2 * self.config.upload_workers:
                        # Use remaining checkpoint interval as timeout
                        now = time.monotonic()
                        elapsed = now - self._last_manifest_checkpoint_at
                        remaining = MANIFEST_CHECKPOINT_INTERVAL_SECONDS - elapsed
                        timeout: float | None = None
                        if self._manifest_dirty:
                            timeout = max(0.0, remaining)
                        # Wait for at least one completion
                        if timeout is not None:
                            done_set, _ = wait(
                                list(futures.keys()),
                                timeout=timeout,
                                return_when=FIRST_COMPLETED,
                            )
                            if not done_set:
                                # Timeout -> flush checkpoint
                                try:
                                    self._maybe_save_manifest_progress()
                                except ManifestProgressError:
                                    for f in list(futures.keys()):
                                        f.cancel()
                                    if futures:
                                        wait(list(futures.keys()))
                                    raise
                                continue
                        else:
                            done_set, _ = wait(
                                list(futures.keys()), return_when=FIRST_COMPLETED
                            )
                        for fut in done_set:
                            w = futures.pop(fut)
                            try:
                                result = fut.result()
                            except Exception as exc:
                                self._record_upload_error(w, exc, progress_callback)
                            else:
                                try:
                                    self._complete_upload(w, result, progress_callback)
                                except ManifestProgressError:
                                    for ff in list(futures.keys()):
                                        ff.cancel()
                                    if futures:
                                        wait(list(futures.keys()))
                                    raise
                        try:
                            self._maybe_save_manifest_progress()
                        except ManifestProgressError:
                            for f in list(futures.keys()):
                                f.cancel()
                            if futures:
                                wait(list(futures.keys()))
                            raise

                # Scan finished: drain remaining futures with checkpoint timeout
                while futures:
                    now = time.monotonic()
                    elapsed = now - self._last_manifest_checkpoint_at
                    remaining = MANIFEST_CHECKPOINT_INTERVAL_SECONDS - elapsed
                    timeout = None
                    if self._manifest_dirty:
                        timeout = max(0.0, remaining)
                    if timeout is not None:
                        done_set, _ = wait(
                            list(futures.keys()),
                            timeout=timeout,
                            return_when=FIRST_COMPLETED,
                        )
                        if not done_set:
                            try:
                                self._maybe_save_manifest_progress()
                            except ManifestProgressError:
                                for f in list(futures.keys()):
                                    f.cancel()
                                if futures:
                                    wait(list(futures.keys()))
                                raise
                            continue
                    else:
                        done_set, _ = wait(
                            list(futures.keys()), return_when=FIRST_COMPLETED
                        )
                    for fut in done_set:
                        w = futures.pop(fut)
                        try:
                            result = fut.result()
                        except Exception as exc:
                            self._record_upload_error(w, exc, progress_callback)
                        else:
                            try:
                                self._complete_upload(w, result, progress_callback)
                            except ManifestProgressError:
                                for ff in list(futures.keys()):
                                    ff.cancel()
                                if futures:
                                    wait(list(futures.keys()))
                                raise
                    try:
                        self._maybe_save_manifest_progress()
                    except ManifestProgressError:
                        for f in list(futures.keys()):
                            f.cancel()
                        if futures:
                            wait(list(futures.keys()))
                        raise

            except KeyboardInterrupt:
                # Controlled interruption: flush dirty state, then propagate
                # Cancel futures that have not started, wait for running
                for f in list(futures.keys()):
                    f.cancel()
                if futures:
                    # Wait for running to finish (they may still complete remote)
                    wait(list(futures.keys()))
                    # Drain any completed that we can apply? But after interrupt we should flush what we have.
                    # Apply any that completed successfully before interrupt? We have already drained? For remaining, try to apply those that finished.
                    for fut, w in list(futures.items()):
                        if fut.done() and not fut.cancelled():
                            try:
                                result = fut.result()
                            except Exception as exc:
                                self._record_upload_error(w, exc, progress_callback)
                            else:
                                try:
                                    self._complete_upload(w, result, progress_callback)
                                except ManifestProgressError:
                                    # If flush fails during interrupt handling, we will handle in finally
                                    pass
                            futures.pop(fut, None)
                # Force final checkpoint before propagating
                try:
                    self._maybe_save_manifest_progress(force=True)
                    # If file doesn't exist but no dirty, ensure file exists
                    if not os.path.exists(self.config.manifest_path):
                        self._save_manifest_progress()
                except ManifestProgressError:
                    final_checkpoint_failed = True
                raise
            except ManifestProgressError:
                final_checkpoint_failed = True
                raise
            finally:
                # On normal exit, also ensure we attempt to flush if not already failed
                # This covers the case where KeyboardInterrupt didn't happen but we are exiting normally
                # But we also handle normal final checkpoint after prune outside this block
                # Here we just ensure executor shutdown handling later
                pass

            # Prune phase: only after every upload future settles
            if self.prune:
                if self.full:
                    self.stats.prune_skipped_reason = (
                        "Skipped prune because --full ignores the manifest"
                    )
                elif not backup_root_available:
                    self.stats.prune_skipped_reason = (
                        "Skipped prune because backup root is unavailable"
                    )
                elif self.stats.files_skipped_error > 0:
                    self.stats.prune_skipped_reason = (
                        "Skipped prune because backup had file or upload errors"
                    )
                else:
                    try:
                        self._prune_stale_manifest_entries()
                    except ManifestProgressError:
                        final_checkpoint_failed = True
                        raise

            self.stats.end_time = time.time()

            # Final local checkpoint before snapshot
            if not final_checkpoint_failed:
                try:
                    # Force flush dirty; also create file if missing even when not dirty
                    if self._manifest_dirty:
                        self._maybe_save_manifest_progress(force=True)
                    elif not os.path.exists(self.config.manifest_path):
                        self._save_manifest_progress()
                    else:
                        # Still ensure checkpoint interval flush? Force to clean state
                        self._maybe_save_manifest_progress(force=True)
                except ManifestProgressError:
                    final_checkpoint_failed = True
                    raise

            # Save manifest already handled by checkpoint; but ensure stats end_time already set
            # Upload manifest snapshot (skip if final checkpoint failed)
            if not final_checkpoint_failed:
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

            # Upload report to Drive (skip if final checkpoint failed)
            if not final_checkpoint_failed and self.drive:
                try:
                    reports_folder_id = self.drive.get_or_create_folder(
                        "_reports", self._root_folder_id
                    )
                    self.drive.upload_file(report_path, reports_folder_id)
                    logger.info("Report uploaded to Drive/_reports/")
                except Exception as e:
                    logger.warning("Report upload failed: %s", e)

            return report

        except ManifestProgressError:
            # Propagate after ensuring we don't upload snapshot/report
            # Still need to generate report locally? Plan says do not upload manifest/report after failed final checkpoint.
            # But we should still save local report and set end_time, then re-raise?
            # For now, ensure end_time set, generate report, save locally, but skip Drive uploads, then raise.
            # However plan says "Do not upload the Drive manifest snapshot or report after a failed final checkpoint."
            # It doesn't say to skip local report saving. We'll still save local report before raising.
            if not hasattr(self.stats, "end_time") or self.stats.end_time == 0:
                self.stats.end_time = time.time()
            else:
                if self.stats.end_time == 0:
                    self.stats.end_time = time.time()
            # Attempt to save local report even after checkpoint failure? Keep consistent with earlier handling.
            try:
                report = generate_report(self.stats)
                report_dir = os.path.join(
                    os.path.dirname(os.path.expanduser(self.config.manifest_path)),
                    "reports",
                )
                os.makedirs(report_dir, exist_ok=True)
                timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
                prefix = "dry-run-" if self.dry_run else ""
                report_path = os.path.join(
                    report_dir, f"{prefix}backup-{timestamp}.json"
                )
                save_report(report, report_path)
                logger.info("Report saved to %s", report_path)
            except Exception:
                pass
            raise
        except KeyboardInterrupt:
            # Ensure end_time and local report? For interrupt, we flushed, but should propagate KeyboardInterrupt
            if self.stats.end_time == 0:
                self.stats.end_time = time.time()
            raise
        finally:
            if executor is not None:
                try:
                    executor.shutdown(wait=True, cancel_futures=True)
                except TypeError:
                    executor.shutdown(wait=True)
                # If we are in normal path and haven't yet handled final checkpoint failure, the finally here already handled?
                # The outer try's final checkpoint is inside the try, so this finally just cleans executor.
                pass

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

    def _maybe_save_manifest_progress(self, *, force: bool = False) -> None:
        """Checkpoint manifest every 30s or when forced."""
        if not self._manifest_dirty:
            return
        now = time.monotonic()
        elapsed = now - self._last_manifest_checkpoint_at
        if not force and elapsed < MANIFEST_CHECKPOINT_INTERVAL_SECONDS:
            return
        self._save_manifest_progress()
        self._manifest_dirty = False
        self._last_manifest_checkpoint_at = now

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

    def _prepare_upload(
        self,
        file: FileEntry,
        progress_callback: Callable[[FileEntry, ProgressEvent], None] | None = None,
    ) -> UploadWork | None:
        """Handle skips/dedup/dry-run and return work for uploads that need Drive I/O."""
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
            return None

        # Eligible file — track total
        self.stats.bytes_total_eligible += file.size

        # Dedup check
        should_upload, reason = needs_upload(file, self.manifest)
        if not should_upload:
            self.stats.files_skipped_dedup += 1
            if progress_callback:
                progress_callback(file, ProgressEvent(ProgressKind.DEDUP, reason))
            return None

        # Upload (or simulate in dry-run)
        if self.dry_run:
            self.stats.files_uploaded += 1
            self.stats.bytes_uploaded += file.size
            self._record_upload(file)
            if progress_callback:
                progress_callback(
                    file, ProgressEvent(ProgressKind.WOULD_UPLOAD, reason)
                )
            return None

        # Snapshot existing Drive ID for direct-update path
        existing = self.manifest.get(file.relative_path)
        existing_drive_file_id: str | None = None
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
            existing_drive_file_id = existing.drive_file_id

        return UploadWork(
            file=file, reason=reason, existing_drive_file_id=existing_drive_file_id
        )

    def _execute_upload(self, work: UploadWork) -> UploadResult:
        """Perform folder resolution and Drive I/O for a single file."""
        assert self.drive is not None
        file = work.file

        # Determine the parent folder on Drive
        rel_dir = os.path.dirname(file.relative_path)
        if rel_dir:
            path_parts = rel_dir.split("/")
            parent_id = self.drive.ensure_folder_path(path_parts, self._root_folder_id)
        else:
            parent_id = self._root_folder_id

        resumable = file.size > self.config.resumable_threshold_bytes

        # Update existing file, reconcile an orphaned same-name file, or upload new one
        if work.existing_drive_file_id:
            result = self.drive.update_file(
                work.existing_drive_file_id, file.path, resumable=resumable
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

        md5 = result.get("md5Checksum", "")
        drive_file_id = str(result.get("id", ""))
        return UploadResult(
            md5=md5, drive_file_id=drive_file_id, drive_parent_id=parent_id
        )

    def _complete_upload(
        self,
        work: UploadWork,
        result: UploadResult,
        progress_callback: Callable[[FileEntry, ProgressEvent], None] | None = None,
    ) -> None:
        """Apply Drive result to manifest/stats/progress on the main thread."""
        file = work.file
        md5 = result.md5
        if not md5:
            md5 = compute_md5(file.path) or ""

        self.manifest.set(
            relative_path=file.relative_path,
            md5=md5,
            size=file.size,
            mtime=file.mtime,
            drive_file_id=result.drive_file_id,
            drive_parent_id=result.drive_parent_id,
        )
        self._manifest_dirty = True
        self.stats.files_uploaded += 1
        self.stats.bytes_uploaded += file.size
        self._record_upload(file)
        if progress_callback:
            progress_callback(file, ProgressEvent(ProgressKind.UPLOADED, work.reason))
        # Checkpoint may raise ManifestProgressError
        # Caller decides when to flush; we don't auto-flush here to allow batching,
        # but the outer loop will call _maybe_save after each completion.
        # However to keep the contract that _complete marks dirty, we leave flush to caller.

    def _record_upload_error(
        self,
        work: UploadWork,
        error: Exception,
        progress_callback: Callable[[FileEntry, ProgressEvent], None] | None = None,
    ) -> None:
        """Record a per-file upload failure."""
        if isinstance(error, ManifestProgressError):
            raise error
        logger.error("Failed to upload %s: %s", work.file.path, error)
        self.stats.files_skipped_error += 1
        self.stats.error_files.append(
            ErrorFile(
                path=work.file.path,
                relative_path=work.file.relative_path,
                error=str(error),
            )
        )
        if progress_callback:
            progress_callback(work.file, ProgressEvent(ProgressKind.ERROR))

    # Compatibility shim for old tests that call _process_file directly.
    # Preserves immediate persistence semantics for single-file tests.
    def _process_file(
        self,
        file: FileEntry,
        progress_callback: Callable[[FileEntry, ProgressEvent], None] | None = None,
    ) -> None:
        """Process a single file: check exclusions, dedup, upload. Legacy shim."""
        work = self._prepare_upload(file, progress_callback)
        if work is None:
            return
        # For legacy direct calls, we are not in concurrent mode; perform upload synchronously
        # and immediately checkpoint.
        try:
            result = self._execute_upload(work)
        except ManifestProgressError:
            raise
        except Exception as exc:
            self._record_upload_error(work, exc, progress_callback)
            return
        try:
            self._complete_upload(work, result, progress_callback)
            # Immediate persistence for legacy path
            self._manifest_dirty = True
            self._save_manifest_progress()
            self._manifest_dirty = False
            self._last_manifest_checkpoint_at = time.monotonic()
        except ManifestProgressError:
            raise
        except Exception as exc:
            self._record_upload_error(work, exc, progress_callback)

    def _upload_file(self, file: FileEntry, reason: str) -> None:
        """Legacy manifest-mutating upload path. Retained for compatibility."""
        # Reconstruct work and execute synchronously
        existing = self.manifest.get(file.relative_path)
        existing_id = None
        if (
            existing
            and existing.drive_file_id
            and reason in ("content_changed", "size_changed", "md5_error", "restored")
        ):
            existing_id = existing.drive_file_id
        work = UploadWork(file=file, reason=reason, existing_drive_file_id=existing_id)
        result = self._execute_upload(work)
        md5 = result.md5 or compute_md5(file.path) or ""
        self.manifest.set(
            relative_path=file.relative_path,
            md5=md5,
            size=file.size,
            mtime=file.mtime,
            drive_file_id=result.drive_file_id,
            drive_parent_id=result.drive_parent_id,
        )
        self._save_manifest_progress()

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
                self._manifest_dirty = True
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


def _format_mtime(mtime: float) -> str:
    """Format a modification time as ISO string."""
    if mtime == 0:
        return ""
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
