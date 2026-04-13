"""Walk the local filesystem and yield files with exclusion metadata."""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Iterator

from drive_backup.config import Config

_WIN32 = sys.platform == "win32"
_MAX_PATH = 260

logger = logging.getLogger(__name__)


@dataclass
class FileEntry:
    """A single file discovered during scanning."""

    path: str
    relative_path: str
    size: int
    mtime: float
    is_skipped: bool = False
    skip_reason: str = ""

    @property
    def extension(self) -> str:
        return Path(self.path).suffix.lower()

    @property
    def size_human(self) -> str:
        return _human_size(self.size)


def _human_size(num_bytes: int) -> str:
    """Convert bytes to human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num_bytes) < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024  # type: ignore[assignment]
    return f"{num_bytes:.1f} PB"


def _is_excluded_dir(name: str, exclude_dirs: list[str]) -> bool:
    """Check if a directory name matches any exclusion pattern."""
    for pattern in exclude_dirs:
        if fnmatch(name, pattern):
            return True
    return False


def _is_excluded_file(name: str, exclude_files: list[str]) -> bool:
    """Check if a filename matches any exclusion pattern."""
    for pattern in exclude_files:
        if fnmatch(name, pattern):
            return True
    return False


def _truncate_relative_path(rel_path: str, max_len: int = _MAX_PATH) -> str:
    """Truncate the filename stem so the full relative path fits within *max_len* chars."""
    if len(rel_path) <= max_len:
        return rel_path
    directory, filename = os.path.split(rel_path)
    stem, ext = os.path.splitext(filename)
    # How much room is left for the stem after dir + separator + ext?
    overhead = len(ext)
    if directory:
        overhead += len(directory) + 1  # +1 for the separator
    max_stem = max_len - overhead
    if max_stem < 1:
        max_stem = 1
    truncated = stem[:max_stem]
    if directory:
        return f"{directory}/{truncated}{ext}"
    return f"{truncated}{ext}"


def _is_excluded_by_path(rel_path: str, patterns: list[str]) -> bool:
    """Check if a relative path matches any path-based exclusion pattern."""
    for pattern in patterns:
        if fnmatch(rel_path, pattern):
            return True
    return False


def scan(config: Config) -> Iterator[FileEntry]:
    """Walk backup_root and yield every file, marking skipped ones with reasons.

    Yields FileEntry for every file encountered, including those that are
    skipped due to exclusion rules, size limits, or errors. This powers
    the detailed skip report.
    """
    root = config.backup_root
    if not os.path.isdir(root):
        logger.error("Backup root does not exist: %s", root)
        return

    excluded_dir_count = 0

    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        # --- Filter directories in-place (topdown=True lets us prune) ---
        filtered_dirs: list[str] = []
        for d in dirnames:
            full_dir = os.path.join(dirpath, d)

            # Skip symlinks/junctions to prevent infinite loops
            if config.exclude_symlinks and os.path.islink(full_dir):
                excluded_dir_count += 1
                logger.debug("Skipping symlink/junction: %s", full_dir)
                continue

            # Skip excluded directory names
            if _is_excluded_dir(d, config.exclude_dirs):
                excluded_dir_count += 1
                logger.debug("Skipping excluded dir: %s", full_dir)
                continue

            filtered_dirs.append(d)

        dirnames[:] = filtered_dirs

        # --- Process files ---
        for filename in filenames:
            full_path = os.path.join(dirpath, filename)
            try:
                rel_path = os.path.relpath(full_path, root).replace("\\", "/")
            except ValueError:
                # Can happen with paths on different drives
                rel_path = full_path.replace("\\", "/")

            # On Windows, paths >= 260 chars need the \\?\ prefix for I/O
            if _WIN32 and len(full_path) >= _MAX_PATH:
                full_path = "\\\\?\\" + full_path
                # Truncate the filename in the relative path so Drive
                # receives a name within the 260-char limit.
                rel_path = _truncate_relative_path(rel_path)

            # Try to stat the file
            try:
                stat = os.stat(full_path)
            except (PermissionError, OSError) as e:
                yield FileEntry(
                    path=full_path,
                    relative_path=rel_path,
                    size=0,
                    mtime=0,
                    is_skipped=True,
                    skip_reason=f"error: {e}",
                )
                continue

            size = stat.st_size
            mtime = stat.st_mtime

            # Check file name exclusions
            if _is_excluded_file(filename, config.exclude_files):
                yield FileEntry(
                    path=full_path,
                    relative_path=rel_path,
                    size=size,
                    mtime=mtime,
                    is_skipped=True,
                    skip_reason="excluded_by_pattern",
                )
                continue

            # Check path-based exclusion patterns (match against relative path)
            if _is_excluded_by_path(rel_path, config.exclude_path_patterns):
                yield FileEntry(
                    path=full_path,
                    relative_path=rel_path,
                    size=size,
                    mtime=mtime,
                    is_skipped=True,
                    skip_reason="excluded_by_path_pattern",
                )
                continue

            # Check specific file exclusions (exact relative path match)
            if rel_path in config.exclude_specific_files:
                yield FileEntry(
                    path=full_path,
                    relative_path=rel_path,
                    size=size,
                    mtime=mtime,
                    is_skipped=True,
                    skip_reason="excluded_by_specific_file",
                )
                continue

            # Check symlinks
            if config.exclude_symlinks and os.path.islink(full_path):
                yield FileEntry(
                    path=full_path,
                    relative_path=rel_path,
                    size=size,
                    mtime=mtime,
                    is_skipped=True,
                    skip_reason="symlink",
                )
                continue

            # Check size limits
            ext = Path(filename).suffix.lower()
            size_limit = config.get_size_limit_bytes(ext)
            if size_limit is not None:
                if size_limit == 0:
                    yield FileEntry(
                        path=full_path,
                        relative_path=rel_path,
                        size=size,
                        mtime=mtime,
                        is_skipped=True,
                        skip_reason=f"type_excluded ({ext})",
                    )
                    continue
                if size > size_limit:
                    limit_mb = size_limit / (1024 * 1024)
                    yield FileEntry(
                        path=full_path,
                        relative_path=rel_path,
                        size=size,
                        mtime=mtime,
                        is_skipped=True,
                        skip_reason=f"exceeds_size_limit ({_human_size(size)} > {limit_mb:.0f} MB)",
                    )
                    continue

            # File passes all checks
            yield FileEntry(
                path=full_path,
                relative_path=rel_path,
                size=size,
                mtime=mtime,
            )

    logger.info("Scan complete. Excluded %d directories.", excluded_dir_count)
