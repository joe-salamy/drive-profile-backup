"""Merge, conflict, archive, and finalization support primitives."""

from __future__ import annotations

import json
import stat
from dataclasses import replace
from pathlib import Path
from typing import Iterable

from .git_workspace import GitWorkspace
from .models import FlowError, WorkflowStage, WorkflowState
from .paths import (
    ARCHIVE_ALLOWLIST,
    OBSOLETE_GENERATED_FILES,
    atomic_write_json,
    canonical_path,
    ensure_directory,
    lstat_regular,
    optional_regular_file,
    read_bytes_bounded,
    regular_directory_entries,
    safe_copy,
    safe_unlink,
    sha256_file,
)


class IntegrationManager:
    def __init__(self, git: GitWorkspace, *, harness_dir: Path) -> None:
        self.git = git
        self.harness_dir = harness_dir
        self.handoff_rel = harness_dir / "handoff"
        self.flow_rel = harness_dir / "worktree-flow"

    def handoff(self, worktree: Path) -> Path:
        return worktree / self.handoff_rel

    def copy_context(self, feature_worktree: Path, integration_worktree: Path, plan_path: Path) -> Path:
        source = self.handoff(feature_worktree)
        destination = self.handoff(integration_worktree)
        ensure_directory(destination, mode=0o700)
        source_entries = {entry.name: entry for entry in regular_directory_entries(source)}
        for name in ARCHIVE_ALLOWLIST:
            entry = source_entries.get(name)
            if entry is not None:
                safe_copy(entry, destination / name)
        relative_plan = plan_path.relative_to(feature_worktree) if plan_path.is_relative_to(feature_worktree) else Path("docs") / "plans" / plan_path.name
        target_plan = integration_worktree / relative_plan
        ensure_directory(target_plan.parent, mode=0o700)
        safe_copy(plan_path, target_plan)
        return target_plan
    def remove_phase_outputs(self, worktree: Path, *, phase: str) -> None:
        handoff = self.handoff(worktree)
        phase_outputs = {
            "implementation": {"implementation-summary.md", "implementation-prompt.md", "implementation-diagnostics.log"},
            "audit": {"audit-summary.md", "audit-prompt.md", "audit-diagnostics.log"},
            "conflict_resolution": {"conflict-resolution-summary.md", "conflict_resolution-prompt.md", "conflict_resolution-diagnostics.log"},
            "post_conflict_audit": {"post-conflict-audit-summary.md", "post_conflict_audit-prompt.md", "post_conflict_audit-diagnostics.log"},
        }
        phases = ("implementation", "audit", "conflict_resolution", "post_conflict_audit")
        try:
            start = phases.index(phase)
        except ValueError as exc:
            raise FlowError(f"Unknown phase output set: {phase}") from exc
        names: set[str] = set()
        for later_phase in phases[start:]:
            names.update(phase_outputs[later_phase])
        for name in names:
            path = handoff / name
            if optional_regular_file(path, label="phase output"):
                safe_unlink(path)

    def allowed_archive_entries(self, archive: Path) -> set[str]:
        return {entry.name for entry in regular_directory_entries(archive)}

    def prepare_archive(self, archive: Path, *, saved_plan: Path | None = None) -> None:
        ensure_directory(archive, mode=0o700)
        entries = self.allowed_archive_entries(archive)
        allowed = set(ARCHIVE_ALLOWLIST) | {"plan.md", "workflow-state.json"}
        unknown = entries - allowed
        if unknown:
            raise FlowError(f"Archive contains unknown entries: {', '.join(sorted(unknown))}")
        if saved_plan is not None:
            plan = archive / "plan.md"
            if optional_regular_file(plan, label="saved plan"):
                if sha256_file(plan) != sha256_file(saved_plan):
                    raise FlowError(f"Archive plan differs from selected plan: {plan}")
            else:
                safe_copy(saved_plan, plan)
        for name in OBSOLETE_GENERATED_FILES:
            path = archive / name
            if optional_regular_file(path, label="archive output"):
                safe_unlink(path)

    def archive_handoff(self, source_worktree: Path, archive: Path, plan_path: Path) -> None:
        self.prepare_archive(archive, saved_plan=plan_path)
        source_entries = {
            entry.name for entry in regular_directory_entries(self.handoff(source_worktree))
        }
        source = self.handoff(source_worktree)
        for name in ARCHIVE_ALLOWLIST:
            target = archive / name
            if name in source_entries:
                safe_copy(source / name, target)
            elif optional_regular_file(target, label="archive output"):
                safe_unlink(target)
    def write_candidate_state(self, archive: Path, state: WorkflowState) -> Path:
        target = archive / "workflow-state.json"
        payload = {field: getattr(state, field) for field in state.__dataclass_fields__}
        payload["stage"] = state.stage.value
        atomic_write_json(target, payload)
        return target

    def copy_plan(self, source: Path, target: Path) -> None:
        safe_copy(source, target)

    def status_fingerprint(self, worktree: Path) -> str:
        return self.git.fingerprint(worktree)

    def require_conflict_checkpoint(self, worktree: Path, expected: str | None) -> str:
        actual = self.status_fingerprint(worktree)
        if expected is not None and actual != expected:
            raise FlowError("Integration worktree changed after the saved conflict checkpoint.")
        return actual

    def require_no_unmerged(self, worktree: Path) -> None:
        if self.git.has_unmerged(worktree):
            raise FlowError("Integration worktree still contains unmerged entries.")

    def stage_non_artifacts(self, worktree: Path) -> None:
        result = self.git._run(["git", "add", "-A", "--", "."], worktree)
        if result.returncode != 0:
            raise FlowError(f"Git staging failed for {worktree}")
        reset = self.git._run(
            ["git", "reset", "HEAD", "--", f":(literal){self.handoff_rel.as_posix()}", f":(literal){self.flow_rel.as_posix()}"],
            worktree,
            check=False,
        )
        if reset.returncode not in {0, 1}:
            raise FlowError(f"Git artifact reset failed for {worktree}")

    def staged_paths(self, worktree: Path) -> list[str]:
        result = self.git._run(["git", "diff", "--cached", "--name-only", "-z"], worktree)
        return [item for item in result.stdout.split("\0") if item]

    def require_staged_changes(self, worktree: Path) -> None:
        paths = [path for path in self.staged_paths(worktree) if not self.git.path_is_artifact(path)]
        if not paths:
            raise FlowError("No integration changes to commit.")

    def exact_manifest(self, archive: Path) -> set[str]:
        entries = self.allowed_archive_entries(archive)
        allowed = set(ARCHIVE_ALLOWLIST) | {"plan.md", "workflow-state.json"}
        unknown = entries - allowed
        if unknown:
            raise FlowError(f"Archive contains unknown entries: {', '.join(sorted(unknown))}")
        missing = {"plan.md", "workflow-state.json"} - entries
        if missing:
            raise FlowError(f"Archive is missing required entries: {', '.join(sorted(missing))}")
        return entries
