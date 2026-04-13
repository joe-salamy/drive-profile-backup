"""Manifest-based deduplication to avoid re-uploading unchanged files."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from drive_backup.scanner import FileEntry

logger = logging.getLogger(__name__)

CHUNK_SIZE = 8192


@dataclass
class ManifestEntry:
    """Record of a previously uploaded file."""

    md5: str
    size: int
    mtime: float
    drive_file_id: str
    drive_parent_id: str
    last_uploaded: str  # ISO timestamp


@dataclass
class Manifest:
    """Tracks what has been uploaded to Drive."""

    entries: dict[str, ManifestEntry] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str) -> Manifest:
        """Load manifest from JSON file, or return empty manifest."""
        path = os.path.expanduser(path)
        if not os.path.exists(path):
            return cls()

        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Could not load manifest from %s: %s", path, e)
            return cls()

        entries = {}
        for rel_path, entry_data in data.get("files", {}).items():
            try:
                entries[rel_path] = ManifestEntry(**entry_data)
            except TypeError:
                logger.debug("Skipping malformed manifest entry: %s", rel_path)
                continue

        return cls(entries=entries)

    def save(self, path: str) -> None:
        """Save manifest to JSON file."""
        path = os.path.expanduser(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)

        data = {
            "version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "file_count": len(self.entries),
            "files": {
                rel_path: asdict(entry) for rel_path, entry in self.entries.items()
            },
        }

        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        logger.info("Manifest saved: %d entries -> %s", len(self.entries), path)

    def get(self, relative_path: str) -> ManifestEntry | None:
        """Look up a file by its relative path."""
        return self.entries.get(relative_path)

    def set(
        self,
        relative_path: str,
        md5: str,
        size: int,
        mtime: float,
        drive_file_id: str,
        drive_parent_id: str,
    ) -> None:
        """Record an uploaded file in the manifest."""
        self.entries[relative_path] = ManifestEntry(
            md5=md5,
            size=size,
            mtime=mtime,
            drive_file_id=drive_file_id,
            drive_parent_id=drive_parent_id,
            last_uploaded=datetime.now(timezone.utc).isoformat(),
        )


def compute_md5(path: str) -> str | None:
    """Compute MD5 hex digest of a file, streaming in chunks.

    Returns None if the file cannot be read (locked, permissions, etc.).
    """
    h = hashlib.md5()
    try:
        with open(path, "rb") as f:
            while chunk := f.read(CHUNK_SIZE):
                h.update(chunk)
    except (PermissionError, OSError) as e:
        logger.debug("Cannot compute MD5 for %s: %s", path, e)
        return None
    return h.hexdigest()


def needs_upload(file: FileEntry, manifest: Manifest) -> tuple[bool, str]:
    """Determine if a file needs uploading using two-tier dedup.

    Returns (needs_upload: bool, reason: str).
    Reasons: "new", "size_changed", "content_changed", "skipped_mtime_match",
             "skipped_md5_match", "md5_error".
    """
    entry = manifest.get(file.relative_path)

    # New file — not in manifest
    if entry is None:
        return True, "new"

    # Fast path: mtime and size unchanged → file has not been modified
    if file.mtime == entry.mtime and file.size == entry.size:
        return False, "skipped_mtime_match"

    # Size changed — definitely need to upload
    if file.size != entry.size:
        return True, "size_changed"

    # mtime changed but size same — check MD5 to confirm
    local_md5 = compute_md5(file.path)
    if local_md5 is None:
        return False, "md5_error"

    if local_md5 == entry.md5:
        return False, "skipped_md5_match"

    return True, "content_changed"
