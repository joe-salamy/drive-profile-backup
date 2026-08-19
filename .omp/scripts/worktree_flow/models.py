"""Shared data contracts for the worktree-flow runtime."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class FlowError(RuntimeError):
    """A recoverable workflow error with a safe user-facing message."""


class WorkflowStage(StrEnum):
    FEATURE_ALLOCATED = "feature_allocated"
    FEATURE_WORKTREE_CREATED = "feature_worktree_created"
    PLAN_COPIED = "plan_copied"
    IMPLEMENTATION_STARTED = "implementation_started"
    IMPLEMENTATION_COMPLETE = "implementation_complete"
    AUDIT_STARTED = "audit_started"
    AUDIT_COMPLETE = "audit_complete"
    STOPPED_BEFORE_MERGE = "stopped_before_merge"
    INTEGRATION_ALLOCATED = "integration_allocated"
    INTEGRATION_WORKTREE_CREATED = "integration_worktree_created"
    INTEGRATION_MERGE_APPLIED = "integration_merge_applied"
    INTEGRATION_CONFLICTS_DETECTED = "integration_conflicts_detected"
    CONFLICT_RESOLUTION_STARTED = "conflict_resolution_started"
    CONFLICT_RESOLUTION_COMPLETE = "conflict_resolution_complete"
    POST_CONFLICT_AUDIT_STARTED = "post_conflict_audit_started"
    POST_CONFLICT_AUDIT_COMPLETE = "post_conflict_audit_complete"
    INTEGRATION_CHANGES_STAGED = "integration_changes_staged"
    INTEGRATION_COMMITTED = "integration_committed"
    INTEGRATION_REBUILD_CLEANUP = "integration_rebuild_cleanup"
    BASE_FAST_FORWARDED = "base_fast_forwarded"
    HANDOFF_ARCHIVED = "handoff_archived"
    ARTIFACTS_COMMITTED = "artifacts_committed"
    INTEGRATION_WORKTREE_REMOVED = "integration_worktree_removed"
    FEATURE_WORKTREE_REMOVED = "feature_worktree_removed"
    WORKTREES_RETAINED = "worktrees_retained"
    WORKTREES_PRUNED = "worktrees_pruned"
    INTEGRATION_BRANCH_REMOVED = "integration_branch_removed"
    FEATURE_BRANCH_REMOVED = "feature_branch_removed"
    CLEANUP_COMPLETE = "cleanup_complete"
    COMPLETE = "complete"


class HarnessKind(StrEnum):
    OMP = "omp"
    CODEX = "codex"
    OPENCODE = "opencode"

    @classmethod
    def from_executable(cls, executable: str) -> "HarnessKind":
        name = Path(executable).name.lower()
        if name.endswith(".exe"):
            name = name[:-4]
        try:
            return cls(name)
        except ValueError as exc:
            raise FlowError(
                f"Unsupported harness executable {executable!r}; use omp, codex, or opencode."
            ) from exc


@dataclass(frozen=True)
class CommandTiming:
    started_at: str
    finished_at: str
    duration_ms: int


@dataclass(frozen=True)
class GitWorktree:
    path: Path
    branch: str | None
    head: str | None = None
    bare: bool = False


@dataclass(frozen=True)
class Names:
    slug: str
    feature_branch: str
    feature_worktree: Path
    run_id: str
    integration_branch: str | None = None
    integration_worktree: Path | None = None



@dataclass(frozen=True)
class StatusRecord:
    index_status: str
    worktree_status: str
    path: str
    original_path: str | None = None

    @property
    def code(self) -> str:
        return f"{self.index_status}{self.worktree_status}"


@dataclass(frozen=True)
class SessionSnapshot:
    files: dict[str, int]


@dataclass(frozen=True)
class UsageTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    cost_input: float = 0.0
    cost_output: float = 0.0
    cost_cache_read: float = 0.0
    cost_cache_write: float = 0.0
    cost_total: float = 0.0


@dataclass(frozen=True)
class UsageSource:
    source_id: str
    session_id: str | None
    file_name: str
    path_hash: str
    records_read: int
    event_counts: dict[str, int]
    record_ids: list[str]


@dataclass(frozen=True)
class FlowConfig:
    repo: Path
    plan: Path
    base: str | None
    harness: str
    harness_dir: Path
    state_dir: Path | None = None
    model: str | None = None
    implementation_model: str | None = None
    review_model: str | None = None
    merge_mode: str | None = None
    keep_worktrees: bool = False
    verbose: bool = False
    command_timeout_seconds: float | None = None
    dry_run: bool = False
    resume: bool = False
    entrypoint_path: Path = Path("worktree-flow.py")
    worktree: Path | None = None
    harness_explicit: bool = False
    harness_dir_explicit: bool = False
    keep_worktrees_explicit: bool = False
    merge_mode_explicit: bool = False
    model_explicit: bool = False
    implementation_model_explicit: bool = False
    review_model_explicit: bool = False
    command_timeout_explicit: bool = False


@dataclass(frozen=True)
class WorkflowState:
    schema_version: int
    run_id: str
    slug: str
    repo_root: str
    git_common_dir: str
    base_branch: str
    feature_base_commit: str
    plan_title: str
    plan_path: str
    plan_sha256: str
    feature_branch: str
    feature_worktree: str
    harness: str
    harness_kind: str
    harness_dir: str
    implementation_model: str | None
    review_model: str | None
    merge_mode: str
    keep_worktrees: bool
    command_timeout_seconds: float | None
    stage: WorkflowStage
    implementation_head: str | None = None
    audit_start_head: str | None = None
    audit_head: str | None = None
    integration_branch: str | None = None
    integration_worktree: str | None = None
    integration_base_commit: str | None = None
    integration_feature_commit: str | None = None
    integration_worktree_fingerprint: str | None = None
    integration_commit: str | None = None
    archive_dir: str | None = None
    archive_commit: str | None = None
    final_state_commit: str | None = None


STATE_FIELDS: tuple[str, ...] = (
    "schema_version",
    "run_id",
    "slug",
    "repo_root",
    "git_common_dir",
    "base_branch",
    "feature_base_commit",
    "plan_title",
    "plan_path",
    "plan_sha256",
    "feature_branch",
    "feature_worktree",
    "harness",
    "harness_kind",
    "harness_dir",
    "implementation_model",
    "review_model",
    "merge_mode",
    "keep_worktrees",
    "command_timeout_seconds",
    "stage",
    "implementation_head",
    "audit_start_head",
    "audit_head",
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
