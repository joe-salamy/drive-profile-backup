"""Manifest-based deduplication to avoid re-uploading unchanged files."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from drive_backup.scanner import FileEntry
from drive_backup.utils import atomic_write_json

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
    pruned: bool = False


class ManifestLoadError(RuntimeError):
    """Raised when an existing manifest cannot be loaded safely."""


_MANIFEST_ENTRY_FIELDS = {
    "md5",
    "size",
    "mtime",
    "drive_file_id",
    "drive_parent_id",
    "last_uploaded",
}
_MANIFEST_ENTRY_STRING_FIELDS = _MANIFEST_ENTRY_FIELDS - {"size", "mtime"}


def _load_manifest_entries(data: Any) -> dict[str, ManifestEntry]:
    if not isinstance(data, Mapping):
        raise ValueError("manifest root must be a mapping")
    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise ValueError("manifest version must be the integer 1")
    files = data.get("files")
    if not isinstance(files, Mapping):
        raise ValueError("manifest files must be a mapping")

    entries: dict[str, ManifestEntry] = {}
    for relative_path, entry_data in files.items():
        if not isinstance(relative_path, str):
            raise ValueError("manifest file paths must be strings")
        if not isinstance(entry_data, Mapping):
            raise ValueError(f"entry {relative_path!r} must be a mapping")
        unknown = set(entry_data) - (_MANIFEST_ENTRY_FIELDS | {"pruned"})
        if unknown:
            raise ValueError(
                f"entry {relative_path!r} contains unknown keys: "
                f"{', '.join(sorted(unknown))}"
            )
        missing = _MANIFEST_ENTRY_FIELDS - set(entry_data)
        if missing:
            raise ValueError(
                f"entry {relative_path!r} is missing required keys: "
                f"{', '.join(sorted(missing))}"
            )
        pruned = entry_data.get("pruned", False)
        if type(pruned) is not bool:
            raise ValueError(f"entry {relative_path!r} pruned must be a boolean")
        if not all(
            isinstance(entry_data[field_name], str)
            for field_name in _MANIFEST_ENTRY_STRING_FIELDS
        ):
            raise ValueError(f"entry {relative_path!r} string fields must be strings")
        size = entry_data["size"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError(
                f"entry {relative_path!r} size must be a non-negative integer"
            )
        mtime = entry_data["mtime"]
        if (
            isinstance(mtime, bool)
            or not isinstance(mtime, (int, float))
            or not math.isfinite(float(mtime))
        ):
            raise ValueError(f"entry {relative_path!r} mtime must be a finite number")
        entries[relative_path] = ManifestEntry(
            md5=entry_data["md5"],
            size=size,
            mtime=float(mtime),
            drive_file_id=entry_data["drive_file_id"],
            drive_parent_id=entry_data["drive_parent_id"],
            last_uploaded=entry_data["last_uploaded"],
            pruned=pruned,
        )
    return entries


@dataclass
class Manifest:
    """Tracks what has been uploaded to Drive."""

    entries: dict[str, ManifestEntry] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str) -> Manifest:
        """Load a valid version-1 manifest, or return empty when it is absent."""
        path = os.path.expanduser(path)
        if not os.path.exists(path):
            return cls()

        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return cls(entries=_load_manifest_entries(data))
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as error:
            raise ManifestLoadError(
                f"Could not load manifest from {path}: {error}"
            ) from error

    def save(self, path: str) -> None:
        """Save manifest to JSON file."""
        path = os.path.expanduser(path)

        data: dict[str, object] = {
            "version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "file_count": len(self.entries),
            "files": {
                rel_path: asdict(entry) for rel_path, entry in self.entries.items()
            },
        }

        atomic_write_json(path, data)

        logger.debug("Manifest saved: %d entries -> %s", len(self.entries), path)

    def get(self, relative_path: str) -> ManifestEntry | None:
        """Look up a file by its relative path."""
        return self.entries.get(relative_path)

    def remove(self, relative_path: str) -> ManifestEntry | None:
        """Remove and return a manifest entry by its relative path."""
        return self.entries.pop(relative_path, None)

    def set(
        self,
        relative_path: str,
        md5: str,
        size: int,
        mtime: float,
        drive_file_id: str,
        drive_parent_id: str,
        *,
        pruned: bool = False,
    ) -> None:
        """Record an uploaded file in the manifest."""
        self.entries[relative_path] = ManifestEntry(
            md5=md5,
            size=size,
            mtime=mtime,
            drive_file_id=drive_file_id,
            drive_parent_id=drive_parent_id,
            last_uploaded=datetime.now(timezone.utc).isoformat(),
            pruned=pruned,
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
    Reasons: "new", "restored", "size_changed", "content_changed",
             "skipped_mtime_match", "skipped_md5_match", "md5_error".
    """
    entry = manifest.get(file.relative_path)

    # New file — not in manifest
    if entry is None:
        return True, "new"

    # Previously pruned entry — local file re-created, must re-upload
    if entry.pruned:
        return True, "restored"

    # Fast path: mtime and size unchanged → file has not been modified
    if file.mtime == entry.mtime and file.size == entry.size:
        return False, "skipped_mtime_match"

    # Size changed — definitely need to upload
    if file.size != entry.size:
        return True, "size_changed"

    # mtime changed but size same — check MD5 to confirm
    local_md5 = compute_md5(file.path)
    if local_md5 is None:
        return True, "md5_error"

    if local_md5 == entry.md5:
        return False, "skipped_md5_match"

    return True, "content_changed"
