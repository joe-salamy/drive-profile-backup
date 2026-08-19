"""Symlink-safe, bounded filesystem operations used by worktree-flow."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from pathlib import Path, PureWindowsPath
from typing import Iterator

from .models import FlowError

WORKFLOW_STATE_FILENAME = "workflow-state.json"
WORKTREE_FLOW_DIRNAME = "worktree-flow"
HANDOFF_DIRNAME = "handoff"
RUN_ID_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"
USAGE_EVENTS_FILENAME = "usage-events.jsonl"
USAGE_SUMMARY_FILENAME = "usage-summary.json"
USAGE_SOURCES_FILENAME = "usage-sources.json"
MAX_STATE_BYTES = 1 * 1024 * 1024
MAX_JSONL_LINE_BYTES = 1 * 1024 * 1024
MAX_ARTIFACT_BYTES = 10 * 1024 * 1024
MAX_DIAGNOSTIC_CHARS = 20_000

ARCHIVE_ALLOWLIST = (
    "implementation-summary.md",
    "audit-summary.md",
    "conflict-resolution-summary.md",
    "post-conflict-audit-summary.md",
    "workflow.jsonl",
    USAGE_EVENTS_FILENAME,
    USAGE_SUMMARY_FILENAME,
    USAGE_SOURCES_FILENAME,
)
OBSOLETE_GENERATED_FILES = {
    "implementation-final-response.md",
    "audit-final-response.md",
    "post-conflict-audit-final-response.md",
    "conflict-resolution-final-response.md",
    "merge-conflict-context.md",
    "resume-plan.md",
}

_IDENTIFIER_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except (ValueError, OSError, RuntimeError):
        return False


def _is_reparse_point(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse)


def _components(path: Path) -> Iterator[Path]:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        yield current


def reject_symlink_components(path: Path, *, include_final: bool = True) -> None:
    """Reject symlink/reparse components without following untrusted links."""
    components = list(_components(path))
    if not include_final and components:
        components.pop()
    for component in components:
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise FlowError(f"Cannot inspect path component: {component}") from exc
        if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info):
            raise FlowError(f"Refusing symlink or reparse-point path: {component}")


def canonical_path(path: Path, *, must_exist: bool = False) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    reject_symlink_components(candidate, include_final=True)
    try:
        resolved = candidate.resolve(strict=must_exist)
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        raise FlowError(f"Cannot resolve path: {candidate}") from exc
    reject_symlink_components(resolved, include_final=True)
    return resolved


def require_confined(root: Path, candidate: Path, *, must_exist: bool = False) -> Path:
    canonical_root = canonical_path(root, must_exist=True)
    raw = Path(candidate).expanduser()
    if not raw.is_absolute():
        raw = canonical_root / raw
    reject_symlink_components(raw, include_final=True)
    resolved = canonical_path(raw, must_exist=must_exist)
    if not is_relative_to(resolved, canonical_root):
        raise FlowError(f"Path escapes confined root {canonical_root}: {candidate}")
    return resolved


def lstat_regular(path: Path, *, label: str = "file") -> os.stat_result:
    target = Path(path)
    reject_symlink_components(target, include_final=True)
    try:
        info = target.lstat()
    except FileNotFoundError as exc:
        raise FlowError(f"Required {label} does not exist: {target}") from exc
    except OSError as exc:
        raise FlowError(f"Cannot inspect {label}: {target}") from exc
    if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info):
        raise FlowError(f"Refusing symlink or reparse-point {label}: {target}")
    if not stat.S_ISREG(info.st_mode):
        raise FlowError(f"Expected regular {label}: {target}")
    return info


def optional_regular_file(path: Path, *, label: str = "file") -> bool:
    try:
        lstat_regular(path, label=label)
    except FlowError as exc:
        try:
            path.lstat()
        except FileNotFoundError:
            return False
        except OSError as inspect_exc:
            raise FlowError(f"Cannot inspect {label}: {path}") from inspect_exc
        raise exc
    return True


def ensure_directory(path: Path, *, mode: int | None = None) -> Path:
    """Create a directory, preserving existing modes unless one is requested."""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    requested_mode = 0o700 if mode is None else mode
    reject_symlink_components(candidate, include_final=False)
    try:
        candidate.mkdir(parents=True, exist_ok=True, mode=requested_mode)
    except OSError as exc:
        raise FlowError(f"Cannot create directory: {candidate}") from exc
    reject_symlink_components(candidate, include_final=True)
    if mode is not None:
        try:
            candidate.chmod(mode)
        except OSError:
            # chmod is not available/meaningful on every supported platform.
            pass
    return candidate.resolve()


def read_bytes_bounded(path: Path, *, max_bytes: int = MAX_ARTIFACT_BYTES) -> bytes:
    info = lstat_regular(path)
    if info.st_size > max_bytes:
        raise FlowError(f"File exceeds {max_bytes} byte limit: {path}")
    chunks: list[bytes] = []
    total = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(min(1024 * 1024, max_bytes - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise FlowError(f"File exceeds {max_bytes} byte limit: {path}")
                chunks.append(chunk)
    except FlowError:
        raise
    except (OSError, UnicodeError) as exc:
        raise FlowError(f"Cannot read file: {path}") from exc
    return b"".join(chunks)


def read_text_bounded(path: Path, *, max_bytes: int = MAX_ARTIFACT_BYTES) -> str:
    try:
        return read_bytes_bounded(path, max_bytes=max_bytes).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FlowError(f"File is not valid UTF-8: {path}") from exc


def _temporary_path(parent: Path, name: str) -> tuple[int, Path]:
    ensure_directory(parent)
    for _ in range(20):
        token = uuid.uuid4().hex
        candidate = parent / f".{name}.{token}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            return os.open(candidate, flags, 0o600), candidate
        except FileExistsError:
            continue
    raise FlowError(f"Could not create an exclusive temporary file beside {parent / name}")


def atomic_write_bytes(
    path: Path,
    payload: bytes,
    *,
    max_bytes: int = MAX_ARTIFACT_BYTES,
    mode: int = 0o600,
) -> None:
    if len(payload) > max_bytes:
        raise FlowError(f"File exceeds {max_bytes} byte limit: {path}")
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    reject_symlink_components(target, include_final=False)
    ensure_directory(target.parent)
    try:
        target.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise FlowError(f"Cannot inspect destination: {target}") from exc
    else:
        lstat_regular(target, label="destination")
    fd, temporary = _temporary_path(target.parent, target.name)
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temporary.chmod(mode)
        except OSError:
            pass
        reject_symlink_components(temporary, include_final=True)
        os.replace(temporary, target)
        try:
            directory_fd = os.open(target.parent, os.O_RDONLY)
        except OSError:
            directory_fd = -1
        if directory_fd >= 0:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except OSError as exc:
        raise FlowError(f"Cannot atomically write file: {target}") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def atomic_write_text(path: Path, text: str, *, max_bytes: int = MAX_ARTIFACT_BYTES) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), max_bytes=max_bytes)


def atomic_write_json(path: Path, payload: object, *, max_bytes: int = MAX_STATE_BYTES) -> None:
    try:
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    except (TypeError, ValueError) as exc:
        raise FlowError(f"Cannot encode JSON for {path}") from exc
    atomic_write_text(path, text, max_bytes=max_bytes)


def safe_copy(source: Path, destination: Path, *, max_bytes: int = MAX_ARTIFACT_BYTES) -> None:
    payload = read_bytes_bounded(source, max_bytes=max_bytes)
    atomic_write_bytes(destination, payload, max_bytes=max_bytes)


def safe_unlink(path: Path, *, missing_ok: bool = True) -> None:
    target = Path(path)
    reject_symlink_components(target, include_final=False)
    try:
        info = target.lstat()
    except FileNotFoundError:
        if missing_ok:
            return
        raise FlowError(f"File does not exist: {target}")
    except OSError as exc:
        raise FlowError(f"Cannot inspect file: {target}") from exc
    if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info):
        raise FlowError(f"Refusing to unlink symlink or reparse point: {target}")
    if not stat.S_ISREG(info.st_mode):
        raise FlowError(f"Refusing to unlink non-regular file: {target}")
    try:
        target.unlink()
    except OSError as exc:
        raise FlowError(f"Cannot remove file: {target}") from exc


def safe_rmdir(path: Path) -> None:
    target = Path(path)
    reject_symlink_components(target, include_final=True)
    try:
        info = target.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode):
        raise FlowError(f"Refusing to remove non-directory: {target}")
    try:
        target.rmdir()
    except OSError as exc:
        raise FlowError(f"Cannot remove directory: {target}") from exc


def iter_bounded_jsonl(path: Path, *, max_line_bytes: int = MAX_JSONL_LINE_BYTES) -> Iterator[bytes]:
    lstat_regular(path, label="JSONL file")
    try:
        with path.open("rb") as handle:
            for raw in handle:
                if len(raw) > max_line_bytes:
                    raise FlowError(f"JSONL line exceeds {max_line_bytes} byte limit: {path}")
                yield raw
    except OSError as exc:
        raise FlowError(f"Cannot read JSONL file: {path}") from exc


def sha256_file(path: Path, *, max_bytes: int = MAX_ARTIFACT_BYTES) -> str:
    info = lstat_regular(path)
    if info.st_size > max_bytes:
        raise FlowError(f"File exceeds {max_bytes} byte limit: {path}")
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise FlowError(f"File exceeds {max_bytes} byte limit: {path}")
                digest.update(chunk)
    except OSError as exc:
        raise FlowError(f"Cannot hash file: {path}") from exc
    return digest.hexdigest()


def slugify(value: str, *, max_words: int = 6, max_len: int = 60) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    words = [part for part in cleaned.split("-") if part]
    slug = "-".join(words[:max_words])[:max_len].strip("-")
    return slug or "harness-plan"


def plan_title(plan_path: Path) -> str:
    text = read_text_bounded(plan_path)
    for line in text.splitlines():
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if match:
            return match.group(1).strip()
    return plan_path.stem


def derive_slug(plan_path: Path) -> str:
    return slugify(plan_title(plan_path))


def validate_identifier(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not value or _IDENTIFIER_RE.fullmatch(value) is None:
        raise FlowError(
            f"Invalid {label}: expected lowercase basename identifier matching "
            r"[a-z0-9]+(?:-[a-z0-9]+)*."
        )
    return value


def timestamped_run_id(slug: str, *, stamp: str | None = None) -> str:
    from datetime import datetime

    safe_slug = validate_identifier(slug, label="slug")
    timestamp = stamp or datetime.now().strftime(RUN_ID_TIMESTAMP_FORMAT)
    if re.fullmatch(r"\d{8}-\d{6}", timestamp) is None:
        raise FlowError(f"Invalid run timestamp: {timestamp}")
    return validate_identifier(f"{timestamp}-{safe_slug}", label="run id")


def validate_harness_dir(value: str | Path) -> Path:
    raw = str(value)
    if not raw or any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise FlowError("--harness-dir must be a nonempty safe repository-relative path.")
    if ":" in raw:
        raise FlowError("--harness-dir cannot contain a drive or alternate-data-stream colon.")
    windows = PureWindowsPath(raw.replace("/", "\\"))
    if windows.is_absolute() or windows.drive or raw.startswith(("/", "\\")):
        raise FlowError("--harness-dir must be repository-relative, not absolute or UNC.")
    if re.search(r"[/\\]{2,}", raw) or raw.endswith(("/", "\\")):
        raise FlowError("--harness-dir cannot contain empty path components.")
    parts = tuple(part for part in re.split(r"[/\\]", raw) if part)
    if not parts or any(part in {".", ".."} for part in parts):
        raise FlowError("--harness-dir cannot contain empty, dot, or parent components.")
    for part in parts:
        stem = part.split(".", 1)[0].upper()
        if stem in _WINDOWS_RESERVED:
            raise FlowError(f"--harness-dir contains reserved Windows device component: {part}")
    normalized = Path(*parts)
    if normalized.is_absolute() or normalized == Path("."):
        raise FlowError("--harness-dir must be nonempty and relative.")
    return normalized


def default_state_root(repo_root: Path, git_common_dir: Path) -> Path:
    canonical_repo = canonical_path(repo_root, must_exist=True)
    common = canonical_path(git_common_dir, must_exist=True)
    digest = hashlib.sha256(str(common).encode("utf-8")).hexdigest()[:12]
    return canonical_repo.parent / ".worktree-flow-state" / f"{canonical_repo.name}-{digest}"


def ensure_outside_roots(path: Path, roots: list[Path], *, label: str) -> Path:
    canonical = canonical_path(path, must_exist=False)
    for root in roots:
        if is_relative_to(canonical, canonical_path(root, must_exist=False)):
            raise FlowError(f"{label} must be outside {root}: {canonical}")
    return canonical


def run_artifact_root(repo: Path, harness_dir: Path) -> Path:
    return canonical_path(repo / harness_dir / WORKTREE_FLOW_DIRNAME, must_exist=False)


def handoff_root(worktree: Path, harness_dir: Path) -> Path:
    return canonical_path(worktree / harness_dir / HANDOFF_DIRNAME, must_exist=False)


def regular_directory_entries(path: Path) -> list[Path]:
    reject_symlink_components(path, include_final=True)
    try:
        entries = list(path.iterdir())
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise FlowError(f"Cannot list directory: {path}") from exc
    for entry in entries:
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info):
            raise FlowError(f"Refusing symlink or reparse-point entry: {entry}")
    return entries
def infer_default_harness_dir(entrypoint_path: Path) -> Path:
    script = canonical_path(entrypoint_path, must_exist=False)
    parent_name = script.parent.parent.name
    return Path(parent_name) if parent_name.startswith(".") else Path(".harness")


def infer_default_harness(harness_dir: Path) -> str:
    name = harness_dir.name
    return name[1:] if name.startswith(".") and len(name) > 1 else name
