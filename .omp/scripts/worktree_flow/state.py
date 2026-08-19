"""Versioned external state storage and workflow transition graph."""

from __future__ import annotations

import json
import math
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .models import FlowError, WorkflowStage, WorkflowState, STATE_FIELDS
from .paths import (
    MAX_STATE_BYTES,
    atomic_write_json,
    canonical_path,
    ensure_directory,
    lstat_regular,
    read_bytes_bounded,
    reject_symlink_components,
    require_confined,
    validate_harness_dir,
    validate_identifier,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - exercised on POSIX
    msvcrt = None  # type: ignore[assignment]
_OID_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_HEX64_RE = re.compile(r"[0-9a-f]{64}")


def _require_branch_text(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("-")
        or value.startswith(("refs/", "origin/"))
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(char.isspace() for char in value)
    ):
        raise FlowError(f"Workflow state field {label} has the wrong value.")
    return value


def _require_oid_text(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _OID_RE.fullmatch(value) is None:
        raise FlowError(f"Workflow state field {label} has the wrong value.")
    return value


def _require_hex64(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _HEX64_RE.fullmatch(value) is None:
        raise FlowError(f"Workflow state field {label} has the wrong value.")
    return value



_ALLOWED_TRANSITIONS: dict[WorkflowStage, frozenset[WorkflowStage]] = {
    WorkflowStage.FEATURE_ALLOCATED: frozenset({WorkflowStage.FEATURE_WORKTREE_CREATED}),
    WorkflowStage.FEATURE_WORKTREE_CREATED: frozenset({WorkflowStage.PLAN_COPIED}),
    WorkflowStage.PLAN_COPIED: frozenset({WorkflowStage.IMPLEMENTATION_STARTED}),
    WorkflowStage.IMPLEMENTATION_STARTED: frozenset(
        {WorkflowStage.IMPLEMENTATION_STARTED, WorkflowStage.IMPLEMENTATION_COMPLETE}
    ),
    WorkflowStage.IMPLEMENTATION_COMPLETE: frozenset({WorkflowStage.AUDIT_STARTED}),
    WorkflowStage.AUDIT_STARTED: frozenset(
        {WorkflowStage.AUDIT_STARTED, WorkflowStage.AUDIT_COMPLETE}
    ),
    WorkflowStage.AUDIT_COMPLETE: frozenset(
        {
            WorkflowStage.AUDIT_STARTED,
            WorkflowStage.STOPPED_BEFORE_MERGE,
            WorkflowStage.INTEGRATION_ALLOCATED,
        }
    ),
    WorkflowStage.STOPPED_BEFORE_MERGE: frozenset(
        {WorkflowStage.AUDIT_STARTED, WorkflowStage.STOPPED_BEFORE_MERGE}
    ),
    WorkflowStage.INTEGRATION_ALLOCATED: frozenset(
        {WorkflowStage.INTEGRATION_WORKTREE_CREATED}
    ),
    WorkflowStage.INTEGRATION_WORKTREE_CREATED: frozenset(
        {
            WorkflowStage.INTEGRATION_MERGE_APPLIED,
            WorkflowStage.INTEGRATION_CONFLICTS_DETECTED,
        }
    ),
    WorkflowStage.INTEGRATION_MERGE_APPLIED: frozenset(
        {WorkflowStage.INTEGRATION_CHANGES_STAGED}
    ),
    WorkflowStage.INTEGRATION_CONFLICTS_DETECTED: frozenset(
        {WorkflowStage.CONFLICT_RESOLUTION_STARTED}
    ),
    WorkflowStage.CONFLICT_RESOLUTION_STARTED: frozenset(
        {
            WorkflowStage.CONFLICT_RESOLUTION_STARTED,
            WorkflowStage.CONFLICT_RESOLUTION_COMPLETE,
        }
    ),
    WorkflowStage.CONFLICT_RESOLUTION_COMPLETE: frozenset(
        {WorkflowStage.POST_CONFLICT_AUDIT_STARTED}
    ),
    WorkflowStage.POST_CONFLICT_AUDIT_STARTED: frozenset(
        {
            WorkflowStage.POST_CONFLICT_AUDIT_STARTED,
            WorkflowStage.POST_CONFLICT_AUDIT_COMPLETE,
        }
    ),
    WorkflowStage.INTEGRATION_CHANGES_STAGED: frozenset(
        {WorkflowStage.INTEGRATION_COMMITTED}
    ),
    WorkflowStage.INTEGRATION_COMMITTED: frozenset(
        {WorkflowStage.BASE_FAST_FORWARDED, WorkflowStage.INTEGRATION_REBUILD_CLEANUP}
    ),
    WorkflowStage.INTEGRATION_REBUILD_CLEANUP: frozenset({WorkflowStage.AUDIT_COMPLETE}),
    WorkflowStage.BASE_FAST_FORWARDED: frozenset({WorkflowStage.HANDOFF_ARCHIVED}),
    WorkflowStage.HANDOFF_ARCHIVED: frozenset({WorkflowStage.ARTIFACTS_COMMITTED}),
    WorkflowStage.ARTIFACTS_COMMITTED: frozenset(
        {WorkflowStage.INTEGRATION_WORKTREE_REMOVED, WorkflowStage.WORKTREES_RETAINED}
    ),
    WorkflowStage.INTEGRATION_WORKTREE_REMOVED: frozenset(
        {WorkflowStage.FEATURE_WORKTREE_REMOVED}
    ),
    WorkflowStage.FEATURE_WORKTREE_REMOVED: frozenset({WorkflowStage.WORKTREES_PRUNED}),
    WorkflowStage.WORKTREES_PRUNED: frozenset({WorkflowStage.INTEGRATION_BRANCH_REMOVED}),
    WorkflowStage.INTEGRATION_BRANCH_REMOVED: frozenset(
        {WorkflowStage.FEATURE_BRANCH_REMOVED}
    ),
    WorkflowStage.FEATURE_BRANCH_REMOVED: frozenset({WorkflowStage.CLEANUP_COMPLETE}),
    WorkflowStage.CLEANUP_COMPLETE: frozenset({WorkflowStage.COMPLETE}),
    WorkflowStage.WORKTREES_RETAINED: frozenset({WorkflowStage.COMPLETE}),
    WorkflowStage.COMPLETE: frozenset({WorkflowStage.COMPLETE}),
}


def allowed_transition(current: WorkflowStage, target: WorkflowStage) -> bool:
    return target in _ALLOWED_TRANSITIONS.get(current, frozenset())


def require_transition(current: WorkflowStage, target: WorkflowStage) -> None:
    if not allowed_transition(current, target):
        raise FlowError(f"Invalid workflow transition: {current.value} -> {target.value}")


class RunLock:
    """A process-held, nonblocking lock over one external run directory."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: object | None = None

    def acquire(self) -> None:
        ensure_directory(self.path.parent, mode=0o700)
        if self.path.is_symlink():
            raise FlowError(f"Refusing symlinked workflow lock: {self.path}")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise FlowError(f"Cannot open workflow lock: {self.path}") from exc
        handle = os.fdopen(fd, "a+b", closefd=True)
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            if fcntl is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise FlowError(
                        f"Workflow run is already active; lock is held: {self.path.parent.name}"
                    ) from exc
            elif msvcrt is not None:  # pragma: no cover - Windows only
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise FlowError(
                        f"Workflow run is already active; lock is held: {self.path.parent.name}"
                    ) from exc
            self._handle = handle
        except BaseException:
            handle.close()
            raise

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()  # type: ignore[union-attr]

    def __enter__(self) -> "RunLock":
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


class WorkflowStateStore:
    """Read/write one schema-v2 state record rooted outside all worktrees."""

    schema_version = 2

    def __init__(self, root: Path, *, repo_root: Path, git_common_dir: Path) -> None:
        self.root = canonical_path(root, must_exist=False)
        self.repo_root = canonical_path(repo_root, must_exist=True)
        self.git_common_dir = canonical_path(git_common_dir, must_exist=True)
        if self.root == self.repo_root or self.root == self.git_common_dir:
            raise FlowError("Workflow state root must be outside the selected repository and Git directory.")
        if self.root.is_relative_to(self.repo_root) or self.root.is_relative_to(self.git_common_dir):
            raise FlowError("Workflow state root must be outside the selected repository and Git directory.")

    def run_dir(self, run_id: str) -> Path:
        validate_identifier(run_id, label="run id")
        return require_confined(self.root, self.root / run_id)

    def state_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "workflow-state.json"

    def lock_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / ".workflow.lock"

    def reserve_run(self, run_id: str) -> Path:
        validate_identifier(run_id, label="run id")
        ensure_directory(self.root, mode=0o700)
        destination = self.root / run_id
        if destination.exists() or destination.is_symlink():
            raise FlowError(f"Workflow run directory already exists; use --resume: {destination}")
        try:
            destination.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise FlowError(f"Workflow run directory already exists; use --resume: {destination}") from exc
        except OSError as exc:
            raise FlowError(f"Cannot reserve workflow run directory: {destination}") from exc
        return canonical_path(destination, must_exist=True)

    @contextmanager
    def lock(self, run_id: str) -> Iterator[RunLock]:
        run_dir = self.run_dir(run_id)
        if not run_dir.exists():
            raise FlowError(f"Workflow run directory does not exist: {run_dir}")
        lock = RunLock(self.lock_path(run_id))
        with lock:
            yield lock

    @staticmethod
    def _reject_json_constant(value: str) -> object:
        raise ValueError(f"non-standard JSON constant: {value}")

    @staticmethod
    def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON object key: {key}")
            result[key] = value
        return result

    def _decode(self, path: Path) -> dict[str, object]:
        raw = read_bytes_bounded(path, max_bytes=MAX_STATE_BYTES)
        try:
            value = json.loads(
                raw.decode("utf-8"),
                parse_constant=self._reject_json_constant,
                object_pairs_hook=self._reject_duplicate_keys,
            )
        except (UnicodeDecodeError, ValueError) as exc:
            raise FlowError(f"Invalid workflow state JSON: {path}") from exc
        if not isinstance(value, dict):
            raise FlowError(f"Workflow state must be a JSON object: {path}")
        return value

    def _state_from_data(
        self,
        data: dict[str, object],
        *,
        expected_run_id: str | None = None,
    ) -> WorkflowState:
        if set(data) != set(STATE_FIELDS):
            missing = sorted(set(STATE_FIELDS) - set(data))
            unknown = sorted(set(data) - set(STATE_FIELDS))
            raise FlowError(f"Workflow state fields mismatch; missing={missing}, unknown={unknown}")
        schema_version = data["schema_version"]
        if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version != self.schema_version:
            raise FlowError("Unsupported workflow state schema version; restart with schema-v2 state.")
        run_id_value = data["run_id"]
        if not isinstance(run_id_value, str):
            raise FlowError("Workflow state field run_id has the wrong type.")
        validate_identifier(run_id_value, label="run id")
        if expected_run_id is not None and run_id_value != expected_run_id:
            raise FlowError("Workflow state run_id does not match its selected run directory.")
        identifiers = ("run_id", "slug")
        for field in identifiers:
            value = data[field]
            if not isinstance(value, str):
                raise FlowError(f"Workflow state field {field} has the wrong type.")
            validate_identifier(value, label=field)
        path_fields = ("repo_root", "git_common_dir", "plan_path", "feature_worktree")
        for field in path_fields:
            value = data[field]
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise FlowError(f"Workflow state path field {field} must be absolute.")
            canonical = canonical_path(Path(value), must_exist=False)
            if str(canonical) != value:
                raise FlowError(f"Workflow state path field {field} is not canonical.")
        if data["repo_root"] != str(self.repo_root) or data["git_common_dir"] != str(self.git_common_dir):
            raise FlowError("Workflow state repository identity does not match the selected repository.")
        base_branch = _require_branch_text(data["base_branch"], label="base_branch")
        feature_branch = _require_branch_text(data["feature_branch"], label="feature_branch")
        for field in ("harness", "harness_kind", "harness_dir", "plan_title", "merge_mode"):
            if not isinstance(data[field], str) or not data[field]:
                raise FlowError(f"Workflow state field {field} has the wrong type.")
        harness_dir = validate_harness_dir(data["harness_dir"])
        if harness_dir.as_posix() != data["harness_dir"]:
            raise FlowError("Workflow state harness_dir is not normalized.")
        if data["harness_kind"] not in {"omp", "codex", "opencode"}:
            raise FlowError("Workflow state has an unsupported harness kind.")
        if data["merge_mode"] not in {"squash", "no-ff", "stop"}:
            raise FlowError("Workflow state has an unsupported merge mode.")
        plan_sha256 = _require_hex64(data["plan_sha256"], label="plan_sha256")
        _require_oid_text(data["feature_base_commit"], label="feature_base_commit")
        nullable_string_fields = (
            "implementation_model",
            "review_model",
            "integration_branch",
            "integration_worktree",
            "integration_base_commit",
            "integration_feature_commit",
            "integration_worktree_fingerprint",
            "integration_commit",
            "archive_dir",
            "archive_commit",
            "final_state_commit",
        )
        for field in nullable_string_fields:
            value = data[field]
            if value is not None and (not isinstance(value, str) or not value):
                raise FlowError(f"Workflow state field {field} has the wrong type.")
        if data["integration_branch"] is not None:
            _require_branch_text(data["integration_branch"], label="integration_branch")
        for field in (
            "implementation_head",
            "audit_start_head",
            "audit_head",
            "integration_base_commit",
            "integration_feature_commit",
            "integration_commit",
            "archive_commit",
            "final_state_commit",
        ):
            value = data[field]
            if value is not None:
                _require_oid_text(value, label=field)
        fingerprint = data["integration_worktree_fingerprint"]
        if fingerprint is not None:
            _require_hex64(fingerprint, label="integration_worktree_fingerprint")
        timeout = data["command_timeout_seconds"]
        if timeout is not None:
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(float(timeout)) or timeout <= 0:
                raise FlowError("Workflow state command_timeout_seconds has the wrong type.")
        for field in ("integration_worktree", "archive_dir"):
            value = data[field]
            if value is not None:
                if not Path(value).is_absolute() or str(canonical_path(Path(value), must_exist=False)) != value:
                    raise FlowError(f"Workflow state path field {field} is not canonical.")
        if not isinstance(data["keep_worktrees"], bool):
            raise FlowError("Workflow state keep_worktrees must be boolean.")
        stage_value = data["stage"]
        if not isinstance(stage_value, str):
            raise FlowError("Workflow state stage must be a string.")
        try:
            stage = WorkflowStage(stage_value)
        except ValueError as exc:
            raise FlowError("Workflow state stage is invalid.") from exc
        return WorkflowState(
            schema_version=schema_version,
            run_id=run_id_value,
            slug=data["slug"],
            repo_root=data["repo_root"],
            git_common_dir=data["git_common_dir"],
            base_branch=base_branch,
            feature_base_commit=data["feature_base_commit"],
            plan_title=data["plan_title"],
            plan_path=data["plan_path"],
            plan_sha256=plan_sha256,
            feature_branch=feature_branch,
            feature_worktree=data["feature_worktree"],
            harness=data["harness"],
            harness_kind=data["harness_kind"],
            harness_dir=data["harness_dir"],
            implementation_model=data["implementation_model"],
            review_model=data["review_model"],
            merge_mode=data["merge_mode"],
            keep_worktrees=data["keep_worktrees"],
            command_timeout_seconds=timeout,
            stage=stage,
            implementation_head=data["implementation_head"],
            audit_start_head=data["audit_start_head"],
            audit_head=data["audit_head"],
            integration_branch=data["integration_branch"],
            integration_worktree=data["integration_worktree"],
            integration_base_commit=data["integration_base_commit"],
            integration_feature_commit=data["integration_feature_commit"],
            integration_worktree_fingerprint=data["integration_worktree_fingerprint"],
            integration_commit=data["integration_commit"],
            archive_dir=data["archive_dir"],
            archive_commit=data["archive_commit"],
            final_state_commit=data["final_state_commit"],
        )

    def load(self, run_id: str) -> WorkflowState:
        validate_identifier(run_id, label="run id")
        return self._state_from_data(self._decode(self.state_path(run_id)), expected_run_id=run_id)

    @staticmethod
    def _payload(state: WorkflowState) -> dict[str, object]:
        payload = {field: getattr(state, field) for field in STATE_FIELDS}
        payload["stage"] = state.stage.value
        return payload

    def save(self, state: WorkflowState) -> Path:
        if state.schema_version != self.schema_version:
            raise FlowError("Cannot write a non-schema-v2 workflow state.")
        if state.run_id != self.run_dir(state.run_id).name:
            raise FlowError("Workflow state run_id does not match its state directory.")
        if state.repo_root != str(self.repo_root) or state.git_common_dir != str(self.git_common_dir):
            raise FlowError("Cannot write workflow state for a different repository.")
        payload = self._payload(state)
        self._state_from_data(payload, expected_run_id=state.run_id)
        path = self.state_path(state.run_id)
        ensure_directory(path.parent, mode=0o700)
        atomic_write_json(path, payload, max_bytes=MAX_STATE_BYTES)
        return path

    def transition(self, state: WorkflowState, target: WorkflowStage, **changes: object) -> WorkflowState:
        require_transition(state.stage, target)
        values = {field: getattr(state, field) for field in STATE_FIELDS}
        values.update(changes)
        values["stage"] = target
        updated = WorkflowState(**values)
        self.save(updated)
        return updated

    def read_candidate_state(self, path: Path) -> WorkflowState:
        reject_symlink_components(path, include_final=True)
        canonical = canonical_path(path, must_exist=True)
        if canonical.name != "workflow-state.json":
            raise FlowError("Candidate state path is not workflow-state.json.")
        data = self._decode(canonical)
        run_id = data.get("run_id")
        if not isinstance(run_id, str):
            raise FlowError("Candidate workflow state run_id has the wrong type.")
        return self._state_from_data(data, expected_run_id=None)

