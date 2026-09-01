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
from drive_backup.utils import human_size

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
    encrypted: bool = False

    @property
    def extension(self) -> str:
        return Path(self.path).suffix.lower()

    @property
    def size_human(self) -> str:
        return human_size(self.size)


SECRETS_FILE_PATTERNS: list[str] = [
    ".env",
    ".env.local",
    ".env.*.local",
    "credentials.json",
    "token.json",
    "id_rsa",
    "id_rsa.pub",
    "id_ed25519",
    "id_ed25519.pub",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    ".boto",
    ".claude.json*",
]

SECRETS_DIR_NAMES: set[str] = {
    ".ssh",
    ".azure",
    ".gemini",
    ".android",
    ".aitk",
    ".cisco",
}


def _is_secret_file(filename: str) -> bool:
    """Check if filename matches any secrets file pattern."""
    return any(fnmatch(filename, pat) for pat in SECRETS_FILE_PATTERNS)


def _is_secret_dir(dirname: str) -> bool:
    """Check if directory name is a secrets directory."""
    return dirname in SECRETS_DIR_NAMES


def _is_in_secret_dir(rel_path: str) -> bool:
    """Check if any directory component of rel_path is a secrets dir."""
    parts = rel_path.split("/")
    # All components except the final filename
    return any(part in SECRETS_DIR_NAMES for part in parts[:-1])


def _is_included_path(rel_path: str, patterns: list[str]) -> bool:
    """Check if a relative path matches any explicit include pattern."""
    return any(fnmatch(rel_path, pattern) for pattern in patterns)


def _has_included_descendant(rel_dir: str, patterns: list[str]) -> bool:
    """Check if an included path may exist below a directory."""
    prefix = f"{rel_dir.rstrip('/')}/"
    return any(
        fnmatch(rel_dir, pattern) or pattern.startswith(prefix) for pattern in patterns
    )


def _relative_path(path: str, root: str) -> str:
    try:
        return os.path.relpath(path, root).replace("\\", "/")
    except ValueError:
        return path.replace("\\", "/")


def _is_excluded_dir_with_includes(name: str, rel_dir: str, config: Config) -> bool:
    return _is_excluded_dir(name, config.exclude_dirs) and not _has_included_descendant(
        rel_dir, config.include_path_patterns
    )


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

            # Always exclude the local state directory — it must never be backed up
            # (manifest, token, reports, and especially the encryption key).
            if d == ".drive-backup":
                excluded_dir_count += 1
                logger.debug("Skipping excluded dir: %s", full_dir)
                continue

            # Skip excluded directory names unless an explicit include descends
            # through the directory.
            rel_dir = _relative_path(full_dir, root)
            if _is_excluded_dir_with_includes(d, rel_dir, config):
                # Secrets dirs are allowed when encryption is enabled — even
                # if they appear in exclude_dirs (e.g. .ssh in config.wsl.yaml).
                if config.encrypt_secrets and _is_secret_dir(d):
                    filtered_dirs.append(d)
                    continue
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

            # On Windows, paths >= 260 chars need the \\?\ prefix for I/O.
            # Keep relative_path intact: it is the Drive path and manifest key.
            if _WIN32 and len(full_path) >= _MAX_PATH:
                full_path = "\\\\?\\" + full_path

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

            # Always skip the local state directory, even if dir filtering missed it
            # (covers edge where .drive-backup is nested or not pruned).
            if rel_path == ".drive-backup" or rel_path.startswith(".drive-backup/"):
                yield FileEntry(
                    path=full_path,
                    relative_path=rel_path,
                    size=size,
                    mtime=mtime,
                    is_skipped=True,
                    skip_reason="excluded_by_pattern",
                )
                continue

            # Never back up the encryption key file itself, wherever it lives.
            # Compare absolute normalized paths (handle Windows \\?\ prefix and case).
            try:
                secrets_key_expanded = config.secrets_key_path  # already expanduser'd
                normalized_full = full_path[4:] if full_path.startswith("\\\\?\\") else full_path
                if os.path.abspath(os.path.normcase(normalized_full)) == os.path.abspath(
                    os.path.normcase(secrets_key_expanded)
                ):
                    yield FileEntry(
                        path=full_path,
                        relative_path=rel_path,
                        size=size,
                        mtime=mtime,
                        is_skipped=True,
                        skip_reason="excluded_by_specific_file",
                    )
                    continue
            except Exception:
                # If path comparison fails, fall through to normal handling
                pass

            # Determine if this file is a secret that should be encrypted
            is_secret = False
            if config.encrypt_secrets:
                if _is_secret_file(filename) or _is_in_secret_dir(rel_path):
                    is_secret = True

            # Check file name exclusions — overridden for secrets when encryption enabled
            if _is_excluded_file(filename, config.exclude_files):
                if is_secret:
                    # Fall through to size checks and encrypted yield
                    pass
                else:
                    yield FileEntry(
                        path=full_path,
                        relative_path=rel_path,
                        size=size,
                        mtime=mtime,
                        is_skipped=True,
                        skip_reason="excluded_by_pattern",
                    )
                    continue

            if _is_excluded_by_path(
                rel_path, config.exclude_path_patterns
            ) and not _is_included_path(rel_path, config.include_path_patterns):
                if is_secret:
                    pass
                else:
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
            # For secrets, encryption takes precedence over specific-file exclusion
            if rel_path in config.exclude_specific_files:
                if is_secret:
                    pass
                else:
                    yield FileEntry(
                        path=full_path,
                        relative_path=rel_path,
                        size=size,
                        mtime=mtime,
                        is_skipped=True,
                        skip_reason="excluded_by_specific_file",
                    )
                    continue

            # Check symlinks — still skipped even for secrets
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

            # Check size limits — still apply to secrets
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
                        skip_reason=f"exceeds_size_limit ({human_size(size)} > {limit_mb:.0f} MB)",
                    )
                    continue

            # File passes all checks — mark encrypted if secret
            if is_secret:
                yield FileEntry(
                    path=full_path,
                    relative_path=rel_path,
                    size=size,
                    mtime=mtime,
                    encrypted=True,
                )
            else:
                yield FileEntry(
                    path=full_path,
                    relative_path=rel_path,
                    size=size,
                    mtime=mtime,
                )

    logger.info("Scan complete. Excluded %d directories.", excluded_dir_count)
