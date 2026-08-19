"""Restore backed-up files from a Drive manifest snapshot."""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from drive_backup.config import Config
from drive_backup.dedup import Manifest

logger = logging.getLogger(__name__)

_RESTORE_ERROR_UNSAFE_PATH = "unsafe path"
_RESTORE_ERROR_MISSING_FILE_ID = "missing Drive file ID"


def restore_backup(
    config: Config,
    output_dir: str,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Download non-pruned files from the Drive manifest snapshot.

    Returns a result dict with counts and per-file errors. The snapshot on
    Drive is the source of truth; the local manifest is never used.
    """
    from drive_backup.drive_api import DriveAPI

    drive = DriveAPI(
        credentials_path=config.credentials_path,
        token_path=config.token_path,
        writes_per_second=config.writes_per_second,
        max_retries=config.max_retries,
    )
    drive.authenticate()

    # Mirror the engine's folder resolution: parent folder -> profile folder
    parent_id = drive.get_or_create_folder(config.drive_parent_folder_name)
    root_id = drive.get_or_create_folder(config.profile_name, parent_id)
    meta_id = drive.get_or_create_folder("_meta", root_id)
    found = drive.find_file_by_name_and_parent("manifest.json", meta_id)
    if found is None:
        raise RuntimeError("No manifest snapshot found on Drive; nothing to restore")

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as temp_file:
        temp_path = temp_file.name
    try:
        drive.download_file(found["id"], temp_path)
        manifest = Manifest.load(temp_path)
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass

    files_total = len(manifest.entries)
    files_restored = 0
    files_skipped_pruned = 0
    files_skipped_existing = 0
    files_failed = 0
    bytes_restored = 0
    pruned_files: list[str] = []
    errors: list[dict[str, str]] = []

    for relative_path, entry in sorted(manifest.entries.items()):
        if entry.pruned:
            files_skipped_pruned += 1
            pruned_files.append(relative_path)
            continue

        if os.path.isabs(relative_path) or ".." in Path(relative_path).parts:
            files_failed += 1
            errors.append(
                {
                    "relative_path": relative_path,
                    "error": _RESTORE_ERROR_UNSAFE_PATH,
                }
            )
            continue

        if not entry.drive_file_id:
            files_failed += 1
            errors.append(
                {
                    "relative_path": relative_path,
                    "error": _RESTORE_ERROR_MISSING_FILE_ID,
                }
            )
            continue

        target = Path(output_dir) / relative_path
        part_path = str(target) + ".part"

        if target.exists() and not force:
            files_skipped_existing += 1
            continue

        if dry_run:
            files_restored += 1
            bytes_restored += entry.size
            continue

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            drive.download_file(entry.drive_file_id, part_path)
            os.replace(part_path, str(target))
        except Exception as e:
            try:
                os.unlink(part_path)
            except OSError:
                pass
            files_failed += 1
            errors.append({"relative_path": relative_path, "error": str(e)})
            continue

        files_restored += 1
        bytes_restored += entry.size

    logger.info(
        "Restore finished: %d restored, %d pruned skipped, %d existing skipped, "
        "%d failed",
        files_restored,
        files_skipped_pruned,
        files_skipped_existing,
        files_failed,
    )

    return {
        "profile_name": config.profile_name,
        "output_dir": output_dir,
        "files_total": files_total,
        "files_restored": files_restored,
        "files_skipped_pruned": files_skipped_pruned,
        "files_skipped_existing": files_skipped_existing,
        "files_failed": files_failed,
        "bytes_restored": bytes_restored,
        "pruned_files": pruned_files,
        "errors": errors,
    }
