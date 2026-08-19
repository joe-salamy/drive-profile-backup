"""Composition of worktree-flow validation, harness, integration, and cleanup stages."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from .command_runner import CommandFailureError, CommandResult, CommandRunner, format_command_failure, logged_command, shell_command
from .git_workspace import GitWorkspace
from .harness import HarnessAdapter, audit_prompt, conflict_resolution_prompt, implementation_prompt
from .integration import IntegrationManager
from .models import FlowConfig, FlowError, HarnessKind, Names, WorkflowStage, WorkflowState
from .paths import (
    ARCHIVE_ALLOWLIST,
    HANDOFF_DIRNAME,
    MAX_ARTIFACT_BYTES,
    MAX_JSONL_LINE_BYTES,
    OBSOLETE_GENERATED_FILES,
    WORKFLOW_STATE_FILENAME,
    WORKTREE_FLOW_DIRNAME,
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
    canonical_path,
    default_state_root,
    derive_slug,
    ensure_directory,
    ensure_outside_roots,
    handoff_root,
    is_relative_to,
    lstat_regular,
    optional_regular_file,
    read_bytes_bounded,
    regular_directory_entries,
    require_confined,
    safe_copy,
    safe_unlink,
    sha256_file,
    timestamped_run_id,
    validate_harness_dir,
    validate_identifier,
)
from .state import WorkflowStateStore
from .usage import UsageCollector


class HarnessWorktreeFlow:
    """Run one stateful flow. All mutation paths are explicit and checkpointed."""

    def __init__(self, config: FlowConfig, runner: CommandRunner) -> None:
        self.config = config
        self.runner = runner
        self.repo = canonical_path(config.repo, must_exist=False)
        self.git: GitWorkspace | None = None
        self.state_store: WorkflowStateStore | None = None
        self.integration: IntegrationManager | None = None
        self.adapter: HarnessAdapter | None = None
        self.usage: UsageCollector | None = None
        self.log_file: Path | None = None
        self._last_state: WorkflowState | None = None
        self._base: str | None = config.base
        self._plan_sha256: str | None = None

    @property
    def base(self) -> str:
        if self._base is None:
            raise FlowError("Base branch has not been resolved.")
        return self._base

    @property
    def harness_dir(self) -> Path:
        return self.config.harness_dir

    @property
    def handoff_dir(self) -> Path:
        return self.config.harness_dir / HANDOFF_DIRNAME

    @property
    def worktree_flow_dir(self) -> Path:
        return self.config.harness_dir / WORKTREE_FLOW_DIRNAME

    @property
    def state(self) -> WorkflowState:
        if self._last_state is None:
            raise FlowError("Workflow state is not loaded.")
        return self._last_state

    @staticmethod
    def print_checkpoint(status: str, title: str, details: Sequence[tuple[str, object | None]] = ()) -> None:
        print(f"[{status}] {title}")
        for label, value in details:
            if value is not None and value != "":
                print(f"  {label}: {value}")

    def _bind_runtime(self, repo: Path, common_dir: Path | None = None) -> None:
        self.repo = canonical_path(repo, must_exist=True)
        self.config = replace(self.config, repo=self.repo)
        self.git = GitWorkspace(self.repo, self.runner, harness_dir=self.harness_dir)
        if common_dir is None:
            common_dir = self.git.git_common_dir(self.repo)
        self.state_store = self._make_state_store(common_dir)
        self.integration = IntegrationManager(self.git, harness_dir=self.harness_dir)
        self.usage = UsageCollector(harness=self.config.harness, harness_dir=self.harness_dir, dry_run=self.runner.dry_run)
        self.adapter = HarnessAdapter(
            self.config,
            self.runner,
            logger=self._log_mapping,
            usage=self.usage,
            writable_roots=self.git.extra_writable_roots,
        )

    def _make_state_store(self, common_dir: Path) -> WorkflowStateStore:
        if self.config.state_dir is None:
            root = default_state_root(self.repo, common_dir)
        else:
            root = canonical_path(self.config.state_dir, must_exist=False)
        roots = [self.repo, common_dir, self.repo / self.harness_dir]
        if self.git is not None:
            roots.extend(entry.path for entry in self.git.worktrees())
        root = ensure_outside_roots(root, roots, label="Workflow state root")
        return WorkflowStateStore(root, repo_root=self.repo, git_common_dir=common_dir)

    def _validate_static_plan(self) -> tuple[Path, str, str]:
        plan = canonical_path(self.config.plan, must_exist=True)
        lstat_regular(plan, label="plan")
        digest = sha256_file(plan, max_bytes=MAX_ARTIFACT_BYTES)
        title = self._plan_title(plan)
        self._plan_sha256 = digest
        return plan, digest, title

    @staticmethod
    def _plan_title(plan: Path) -> str:
        raw = read_bytes_bounded(plan, max_bytes=MAX_ARTIFACT_BYTES).decode("utf-8")
        for line in raw.splitlines():
            match = re.match(r"^#\s+(.+?)\s*$", line)
            if match:
                return match.group(1).strip()
        return plan.stem

    def _validate_runtime(self, *, resume: bool = False) -> tuple[Path, Path, str, str]:
        if validate_harness_dir(self.harness_dir) != self.harness_dir:
            raise FlowError("--harness-dir is not normalized.")
        if self.runner.dry_run:
            raise FlowError("Internal error: dry-run uses the preview path.")
        raw_root = GitWorkspace.git_root(self.config.repo, self.runner)
        self._bind_runtime(raw_root)
        assert self.git is not None and self.state_store is not None and self.adapter is not None
        plan, digest, title = self._validate_static_plan()
        self._base = self.git.require_base(self.config.base)
        self.git.require_primary_ready(self.base)
        self.adapter.validate(self.repo)
        self.adapter.prepare_permissions([self.repo / self.harness_dir, self.git.git_common_dir(self.repo)])
        if not resume:
            ensure_directory(self.state_store.root, mode=0o700)
        return plan, digest, title, self.base

    def _plan_run_id(self, plan: Path) -> str | None:
        try:
            relative = plan.relative_to(self.repo)
        except ValueError:
            return None
        expected_root = self.worktree_flow_dir
        if relative.parts[: len(expected_root.parts)] != expected_root.parts:
            return None
        remaining = relative.parts[len(expected_root.parts) :]
        if len(remaining) != 2 or remaining[-1] != "plan.md":
            return None
        return validate_identifier(remaining[0], label="saved plan run id")

    def _validate_saved_plan_reservation(self, plan: Path, run_id: str | None) -> None:
        if run_id is None:
            return
        expected = canonical_path(self._archive_candidate(run_id), must_exist=False)
        if canonical_path(plan.parent, must_exist=True) != expected:
            raise FlowError("Saved plan is not in its generated workflow reservation directory.")
        entries = regular_directory_entries(expected)
        if {entry.name for entry in entries} != {"plan.md"}:
            raise FlowError("Saved plan reservation contains unexpected or unsafe entries.")
        lstat_regular(plan, label="saved plan")

    def _archive_candidate(self, run_id: str) -> Path:
        return self.repo / self.worktree_flow_dir / run_id

    def _choose_names(
        self,
        slug: str,
        saved_run_id: str | None = None,
        *,
        plan_digest: str | None = None,
    ) -> Names:
        assert self.git is not None and self.state_store is not None
        slug = validate_identifier(slug, label="slug")
        if plan_digest is not None and self.state_store.root.exists():
            for candidate_dir in regular_directory_entries(self.state_store.root):
                if not candidate_dir.is_dir():
                    continue
                state_path = candidate_dir / WORKFLOW_STATE_FILENAME
                if not optional_regular_file(state_path, label="workflow state"):
                    continue
                candidate = self.state_store.load(candidate_dir.name)
                if candidate.plan_sha256 == plan_digest and candidate.stage is WorkflowStage.COMPLETE:
                    raise FlowError(
                        f"Completed workflow run already owns this plan; use --resume: {candidate.run_id}"
                    )
        existing_worktrees = {entry.path for entry in self.git.worktrees()}
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        for suffix in range(1, 1000):
            candidate_slug = slug if suffix == 1 else f"{slug}-{suffix}"
            branch = f"feature/{candidate_slug}"
            worktree = (self.repo.parent / f"{self.repo.name}-{candidate_slug}").resolve()
            run_id = (
                saved_run_id
                if suffix == 1 and saved_run_id
                else timestamped_run_id(candidate_slug, stamp=stamp)
            )
            branch_exists = self.git.branch_exists(branch)
            archive = self._archive_candidate(run_id)
            state_dir = self.state_store.run_dir(run_id)
            if branch_exists or worktree in existing_worktrees or worktree.exists():
                continue
            if state_dir.exists():
                if saved_run_id != run_id:
                    continue
                raise FlowError(f"Workflow state already exists; use --resume: {state_dir}")
            if archive.exists() and saved_run_id != run_id:
                continue
            return Names(candidate_slug, branch, worktree, run_id)
        raise FlowError(f"Could not allocate unique names for plan slug {slug}.")

    def _new_state(self, names: Names, plan: Path, title: str, feature_base_commit: str) -> WorkflowState:
        assert self.git is not None and self.adapter is not None
        return WorkflowState(
            schema_version=2,
            run_id=names.run_id,
            slug=names.slug,
            repo_root=str(self.repo),
            git_common_dir=str(self.git.git_common_dir(self.repo)),
            base_branch=self.base,
            feature_base_commit=feature_base_commit,
            plan_title=title,
            plan_path=str(plan),
            plan_sha256=self._plan_sha256 or sha256_file(plan),
            feature_branch=names.feature_branch,
            feature_worktree=str(names.feature_worktree),
            harness=self.config.harness,
            harness_kind=self.adapter.kind.value,
            harness_dir=self.harness_dir.as_posix(),
            implementation_model=self.adapter.model_for("implementation"),
            review_model=self.adapter.model_for("audit"),
            merge_mode=self.config.merge_mode or "squash",
            keep_worktrees=self.config.keep_worktrees,
            command_timeout_seconds=self.config.command_timeout_seconds,
            stage=WorkflowStage.FEATURE_ALLOCATED,
        )

    def _save(self, state: WorkflowState) -> WorkflowState:
        assert self.state_store is not None
        self.state_store.save(state)
        self._last_state = state
        return state

    def _transition(self, state: WorkflowState, stage: WorkflowStage, **changes: object) -> WorkflowState:
        assert self.state_store is not None
        updated = self.state_store.transition(state, stage, **changes)
        self._last_state = updated
        self.log_event("workflow_state_updated", stage=stage.value)
        return updated

    def _change(self, state: WorkflowState, **changes: object) -> WorkflowState:
        updated = replace(state, **changes)
        return self._save(updated)

    def workflow_log_file(self, worktree: Path) -> Path:
        return worktree / self.handoff_dir / "workflow.jsonl"

    def start_log(self, worktree: Path, run_id: str) -> None:
        if self.runner.dry_run:
            return
        self.log_file = self.workflow_log_file(worktree)
        ensure_directory(self.log_file.parent, mode=0o700)
        self.log_event("workflow_log_started", log_file=str(self.log_file), run_id=run_id)

    def continue_log(self, worktree: Path, run_id: str) -> None:
        if self.runner.dry_run:
            return
        self.log_file = self.workflow_log_file(worktree)
        ensure_directory(self.log_file.parent, mode=0o700)
        self.log_event("workflow_log_continued", log_file=str(self.log_file), run_id=run_id)

    def log_event(self, event: str, **fields: object) -> None:
        if self.log_file is None or self.runner.dry_run:
            return
        record: dict[str, object] = {"timestamp": datetime.now().isoformat(timespec="milliseconds"), "event": event, **fields}
        line = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        previous = read_bytes_bounded(self.log_file, max_bytes=10 * 1024 * 1024) if optional_regular_file(self.log_file, label="workflow log") else b""
        if len(line) > MAX_JSONL_LINE_BYTES:
            raise FlowError("Workflow event exceeds the JSONL line limit.")
        atomic_write_bytes(self.log_file, previous + line, max_bytes=10 * 1024 * 1024)
    
    def _log_mapping(self, event: str, fields: Mapping[str, object]) -> None:
        self.log_event(event, **dict(fields))

    def log_command_result(self, event: str, result: CommandResult, **fields: object) -> None:
        self.log_event(
            event,
            command=logged_command(result.args),
            cwd=str(result.cwd),
            returncode=result.returncode,
            timed_out=result.timed_out,
            started_at=result.started_at,
            finished_at=result.finished_at,
            duration_ms=result.duration_ms,
            stdout_bytes=result.stdout_bytes,
            stderr_bytes=result.stderr_bytes,
            **fields,
        )

    def _prepare_worktree(self, path: Path) -> None:
        ensure_directory(path / self.handoff_dir, mode=0o700)
        ensure_directory(path / self.worktree_flow_dir, mode=0o700)

    def _allocate_feature(self, state: WorkflowState, names: Names) -> WorkflowState:
        assert self.git is not None
        path = names.feature_worktree
        entries = [entry for entry in self.git.worktrees() if entry.path == path]
        branch_entries = [entry for entry in self.git.worktrees() if entry.branch == names.feature_branch]
        if state.stage is WorkflowStage.FEATURE_ALLOCATED:
            self.git.require_primary_ready(state.base_branch)
            if entries or branch_entries:
                if not entries or branch_entries != entries or entries[0].branch != names.feature_branch:
                    raise FlowError("Feature allocation is partial or mismatched; refusing to repair it.")
            else:
                self.runner.run(["git", "worktree", "add", "-b", names.feature_branch, "--", str(path), state.feature_base_commit], self.repo)
            self.git.require_feature_worktree(path, names.feature_branch)
            if self.git.head(path) != state.feature_base_commit:
                raise FlowError("Allocated feature worktree does not start at the recorded base commit.")
            self._prepare_worktree(path)
            state = self._transition(state, WorkflowStage.FEATURE_WORKTREE_CREATED)
            self.start_log(path, state.run_id)
            self.log_event("feature_worktree_created", branch=names.feature_branch, worktree=str(path))
        else:
            self._validate_feature_identity(state)
            self._prepare_worktree(path)
            if self.log_file is None:
                self.continue_log(path, state.run_id)
        return state

    def _validate_feature_identity(self, state: WorkflowState) -> None:
        assert self.git is not None
        path = canonical_path(Path(state.feature_worktree), must_exist=False)
        if path != Path(state.feature_worktree):
            raise FlowError("Serialized feature worktree path is not canonical.")
        self.git.require_feature_worktree(path, state.feature_branch)
        self.git.require_commit_oid(state.feature_base_commit, label="feature base")

    def _validate_pre_copy_plan(self, state: WorkflowState) -> None:
        target = canonical_path(
            Path(state.feature_worktree) / self.worktree_flow_dir / state.run_id / "plan.md",
            must_exist=False,
        )
        if optional_regular_file(target, label="copied plan"):
            if sha256_file(target) != state.plan_sha256:
                raise FlowError("Copied plan digest differs from recorded plan_sha256.")
            return
        source = Path(state.plan_path)
        lstat_regular(source, label="source plan")
        if sha256_file(source) != state.plan_sha256:
            raise FlowError("Source plan digest differs from workflow state; refusing to continue.")

    def _copy_plan_stage(self, state: WorkflowState, plan: Path) -> tuple[WorkflowState, Path]:
        assert self.integration is not None
        target = Path(state.feature_worktree) / self.worktree_flow_dir / state.run_id / "plan.md"
        target = canonical_path(target, must_exist=False)
        if state.stage is WorkflowStage.FEATURE_WORKTREE_CREATED:
            if optional_regular_file(target, label="copied plan"):
                if sha256_file(target) != state.plan_sha256:
                    raise FlowError("Copied plan digest differs from recorded plan_sha256.")
            else:
                safe_copy(plan, target)
            if sha256_file(target) != state.plan_sha256:
                raise FlowError("Copied plan digest differs from recorded plan_sha256.")
            state = self._transition(state, WorkflowStage.PLAN_COPIED, plan_path=str(target))
        elif state.plan_path != str(target):
            raise FlowError("Workflow state plan path does not match the generated feature plan path.")
        self._verify_plan(state)
        return state, target

    def _verify_plan(self, state: WorkflowState) -> None:
        path = Path(state.plan_path)
        lstat_regular(path, label="copied plan")
        if sha256_file(path) != state.plan_sha256:
            raise FlowError("Copied plan digest differs from workflow state; refusing to continue.")

    def _remove_phase_outputs(self, worktree: Path, phase: str) -> None:
        assert self.integration is not None
        self.integration.remove_phase_outputs(worktree, phase=phase)
        handoff = worktree / self.handoff_dir
        for name in OBSOLETE_GENERATED_FILES:
            if name == "merge-conflict-context.md" and phase == "conflict_resolution":
                continue
            path = handoff / name
            if optional_regular_file(path, label="generated output"):
                safe_unlink(path)


    def _require_summary(self, worktree: Path, name: str) -> Path:
        path = worktree / self.handoff_dir / name
        if not optional_regular_file(path, label="phase summary"):
            raise FlowError(f"Required output file was not created: {path}")
        sha256_file(path)
        return path

    def _run_implementation(self, state: WorkflowState, plan: Path) -> WorkflowState:
        assert self.adapter is not None and self.git is not None
        worktree = Path(state.feature_worktree)
        if state.stage is WorkflowStage.PLAN_COPIED:
            state = self._transition(state, WorkflowStage.IMPLEMENTATION_STARTED)
        elif state.stage is not WorkflowStage.IMPLEMENTATION_STARTED:
            return state
        self._remove_phase_outputs(worktree, "implementation")
        self.print_checkpoint("start", "Implementation", (("worktree", worktree), ("plan", plan)))
        self.adapter.execute(worktree, implementation_prompt(worktree, plan, self.harness_dir), phase="implementation")
        self._verify_plan(state)
        summary = self._require_summary(worktree, "implementation-summary.md")
        self.git.require_feature_worktree(worktree, state.feature_branch)
        if self.git.count_commits_since(state.feature_base_commit, state.feature_branch, worktree) <= 0:
            raise FlowError("Implementation did not create a commit after feature_base_commit.")
        self.git.require_changed_since(state.base_branch, state.feature_branch, worktree)
        self.git.require_clean_except_artifacts(worktree, phase="Implementation")
        head = self.git.head(worktree)
        state = self._transition(state, WorkflowStage.IMPLEMENTATION_COMPLETE, implementation_head=head)
        self.print_checkpoint("done", "Implementation", (("summary", summary),))
        return state
    def _run_audit(self, state: WorkflowState, plan: Path) -> WorkflowState:
        assert self.adapter is not None and self.git is not None
        worktree = Path(state.feature_worktree)
        if state.stage is WorkflowStage.IMPLEMENTATION_COMPLETE:
            start_head = self.git.head(worktree)
            state = self._transition(state, WorkflowStage.AUDIT_STARTED, audit_start_head=start_head)
        elif state.stage is WorkflowStage.AUDIT_COMPLETE:
            self._verify_plan(state)
            current = self.git.head(worktree)
            if state.audit_head != current:
                state = self._transition(state, WorkflowStage.AUDIT_STARTED, audit_start_head=current)
            else:
                self.git.require_clean_except_artifacts(worktree, phase="Completed audit")
                self._require_summary(worktree, "audit-summary.md")
                return state
        elif state.stage is not WorkflowStage.AUDIT_STARTED:
            return state
        self._remove_phase_outputs(worktree, "audit")
        self._verify_plan(state)
        self.print_checkpoint("start", "Audit", (("worktree", worktree), ("plan", plan)))
        self.adapter.execute(worktree, audit_prompt(worktree, plan, self.harness_dir, post_conflict=False), phase="audit")
        self._verify_plan(state)
        summary = self._require_summary(worktree, "audit-summary.md")
        self.git.require_feature_worktree(worktree, state.feature_branch)
        self.git.require_clean_except_artifacts(worktree, phase="Audit")
        head = self.git.head(worktree)
        if head != state.audit_start_head:
            self.git.require_changed_since(state.base_branch, state.feature_branch, worktree)
        state = self._transition(state, WorkflowStage.AUDIT_COMPLETE, audit_head=head)
        self.print_checkpoint("done", "Audit", (("summary", summary),))
        return state


    def _run_feature_stages(self, state: WorkflowState, plan: Path) -> tuple[WorkflowState, Path]:
        state, copied_plan = self._copy_plan_stage(state, plan)
        state = self._run_implementation(state, copied_plan)
        state = self._run_audit(state, copied_plan)
        return state, copied_plan

    def _adopt_stop_archive(self, state: WorkflowState) -> WorkflowState:
        if state.archive_commit is not None or state.archive_dir is None:
            return state
        assert self.git is not None
        archive = Path(state.archive_dir)
        if not archive.exists():
            return state
        head = self.git.head(self.repo)
        subject = f"Harness: stop {state.plan_title}"
        if self.git.commit_subject(head, self.repo) != subject:
            return state
        parents = self.git.commit_parents(head, self.repo)
        if len(parents) != 1:
            return state
        relative = archive.relative_to(self.repo).as_posix()
        self._archive_receipt(head, archive, relative, parents[0])
        return self._change(state, archive_commit=head)

    def _merge_mode_resume(self, state: WorkflowState) -> WorkflowState | None:
        requested = self.config.merge_mode
        if state.stage is WorkflowStage.STOPPED_BEFORE_MERGE:
            state = self._adopt_stop_archive(state)
        if state.stage is WorkflowStage.STOPPED_BEFORE_MERGE:
            if requested is None:
                self.print_checkpoint("stop", "Stopped before merge", (("resume", self.resume_command()),))
                return None
            if requested not in {"squash", "no-ff"}:
                raise FlowError("A stopped resume requires --merge-mode squash or no-ff.")
            state = self._transition(state, WorkflowStage.AUDIT_STARTED, merge_mode=requested, archive_commit=None)
            return state
        if requested is not None and requested != state.merge_mode:
            raise FlowError("Persisted merge mode is authoritative on resume.")
        return state

    def _allocate_integration(self, state: WorkflowState) -> tuple[WorkflowState, Path]:
        assert self.git is not None
        if state.stage is WorkflowStage.AUDIT_COMPLETE:
            self.git.require_primary_ready(state.base_branch)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            branch = f"integration/{state.slug}-{stamp}"
            path = (self.repo.parent / f"{self.repo.name}-integrate-{state.slug}-{stamp}").resolve()
            for suffix in range(1, 1000):
                if self.git.branch_exists(branch) or path.exists() or any(entry.path == path for entry in self.git.worktrees()):
                    branch = f"integration/{state.slug}-{stamp}-{suffix}"
                    path = (self.repo.parent / f"{self.repo.name}-integrate-{state.slug}-{stamp}-{suffix}").resolve()
                    continue
                break
            base_commit = self.git.branch_tip(state.base_branch)
            feature_commit = self.git.head(Path(state.feature_worktree))
            if state.audit_head is None or feature_commit != state.audit_head:
                raise FlowError("Feature HEAD changed before integration allocation.")
            state = self._transition(
                state,
                WorkflowStage.INTEGRATION_ALLOCATED,
                integration_branch=branch,
                integration_worktree=str(path),
                integration_base_commit=base_commit,
                integration_feature_commit=feature_commit,
                integration_worktree_fingerprint=None,
                integration_commit=None,
            )
        if state.integration_branch is None or state.integration_worktree is None:
            raise FlowError("Integration allocation is missing its branch or worktree.")
        branch = state.integration_branch
        path = Path(state.integration_worktree)
        if state.stage is WorkflowStage.INTEGRATION_ALLOCATED:
            self.git.require_primary_ready(state.base_branch)
            entries = [entry for entry in self.git.worktrees() if entry.path == path]
            if entries:
                if entries[0].branch != branch:
                    raise FlowError("Recorded integration allocation is mismatched.")
            elif self.git.worktree_for_branch(branch) is not None:
                raise FlowError("Recorded integration branch is registered at a different path.")
            else:
                self.runner.run(["git", "worktree", "add", "-b", branch, "--", str(path), state.integration_base_commit or state.base_branch], self.repo)
            self.git.require_integration_worktree(path, branch)
            self._prepare_worktree(path)
            state = self._transition(state, WorkflowStage.INTEGRATION_WORKTREE_CREATED)
        else:
            self.git.require_integration_worktree(path, branch)
            self._prepare_worktree(path)
        return state, path

    def _integration_plan(self, state: WorkflowState, feature_plan: Path, integration: Path) -> Path:
        assert self.integration is not None
        self._verify_plan(state)
        target = integration / feature_plan.relative_to(Path(state.feature_worktree))
        if optional_regular_file(target, label="integration plan"):
            if sha256_file(target) != state.plan_sha256:
                raise FlowError("Integration plan digest differs from state.")
        else:
            self.integration.copy_context(Path(state.feature_worktree), integration, feature_plan)
        return canonical_path(target, must_exist=True)

    def _write_conflict_context(self, state: WorkflowState, integration: Path) -> None:
        assert self.git is not None
        context = integration / self.handoff_dir / "merge-conflict-context.md"
        status = "\n".join(record.code + " " + record.path for record in self.git.status_records(integration))
        conflicted = "\n".join(self.git.unmerged_paths(integration))
        text = f"""# Merge Conflict Context\n\n## Base\n{state.base_branch}\n\n## Feature\n{state.feature_branch}\n\n## Plan\n{state.plan_path}\n\n## Conflicted files\n{conflicted or 'unknown'}\n\n## Status\n```text\n{status or 'clean'}\n```\n\nResolve only the conflict and preserve the approved plan.\n"""
        atomic_write_text(context, text)

    def _merge_integration(self, state: WorkflowState, integration: Path) -> WorkflowState:
        assert self.git is not None
        if state.integration_branch is None:
            raise FlowError("Integration branch is missing.")
        if state.stage is WorkflowStage.INTEGRATION_WORKTREE_CREATED:
            if state.integration_feature_commit is None or self.git.branch_tip(state.feature_branch) != state.integration_feature_commit:
                raise FlowError("Feature branch changed before integration merge.")
            self.git.require_clean_except_artifacts(integration, phase="Pre-merge integration")
            args = ["git", "merge", "--squash", state.feature_branch] if state.merge_mode == "squash" else ["git", "merge", "--no-ff", "--no-commit", state.feature_branch]
            result = self.runner.run(args, integration, check=False)
            if result.returncode != 0:
                if result.timed_out:
                    raise FlowError(format_command_failure(result))
                if not self.git.has_unmerged(integration):
                    raise FlowError(format_command_failure(result))
                self._write_conflict_context(state, integration)
                fingerprint = self.git.fingerprint(integration)
                state = self._transition(state, WorkflowStage.INTEGRATION_CONFLICTS_DETECTED, integration_worktree_fingerprint=fingerprint)
            else:
                fingerprint = self.git.fingerprint(integration)
                state = self._transition(state, WorkflowStage.INTEGRATION_MERGE_APPLIED, integration_worktree_fingerprint=fingerprint)
        elif state.stage in {WorkflowStage.INTEGRATION_CONFLICTS_DETECTED, WorkflowStage.CONFLICT_RESOLUTION_STARTED, WorkflowStage.POST_CONFLICT_AUDIT_STARTED}:
            self._verify_integration_checkpoint(state, integration)
        return state

    def _verify_integration_checkpoint(self, state: WorkflowState, integration: Path) -> None:
        assert self.git is not None
        actual = self.git.fingerprint(integration)
        if state.integration_worktree_fingerprint is not None and actual != state.integration_worktree_fingerprint:
            raise FlowError("Integration worktree changed after the recorded checkpoint.")

    def _resolve_conflicts(self, state: WorkflowState, plan: Path, integration: Path) -> WorkflowState:
        assert self.adapter is not None and self.git is not None
        if state.integration_branch is None:
            raise FlowError("Integration branch is missing during conflict resolution.")
        if state.stage is WorkflowStage.INTEGRATION_CONFLICTS_DETECTED:
            state = self._transition(state, WorkflowStage.CONFLICT_RESOLUTION_STARTED)
        if state.stage is WorkflowStage.CONFLICT_RESOLUTION_STARTED:
            self.git.require_integration_worktree(integration, state.integration_branch)
            self._verify_integration_checkpoint(state, integration)
            self._remove_phase_outputs(integration, "conflict_resolution")
            head_before = self.git.head(integration)
            try:
                self.adapter.execute(integration, conflict_resolution_prompt(integration, plan, self.harness_dir, state.base_branch, state.feature_branch), phase="conflict_resolution")
            except FlowError:
                self.git.require_integration_worktree(integration, state.integration_branch)
                state = self._change(state, integration_worktree_fingerprint=self.git.fingerprint(integration))
                raise
            self.git.require_integration_worktree(integration, state.integration_branch)
            if self.git.head(integration) != head_before or self.git.has_unmerged(integration):
                raise FlowError("Conflict resolution must clear unmerged entries without moving integration HEAD.")
            self._require_summary(integration, "conflict-resolution-summary.md")
            state = self._transition(state, WorkflowStage.CONFLICT_RESOLUTION_COMPLETE, integration_worktree_fingerprint=self.git.fingerprint(integration))
        if state.stage is WorkflowStage.CONFLICT_RESOLUTION_COMPLETE:
            state = self._transition(state, WorkflowStage.POST_CONFLICT_AUDIT_STARTED)
        if state.stage is WorkflowStage.POST_CONFLICT_AUDIT_STARTED:
            self.git.require_integration_worktree(integration, state.integration_branch)
            self._verify_integration_checkpoint(state, integration)
            self._remove_phase_outputs(integration, "post_conflict_audit")
            head_before = self.git.head(integration)
            try:
                self.adapter.execute(integration, audit_prompt(integration, plan, self.harness_dir, post_conflict=True), phase="post_conflict_audit")
            except FlowError:
                self.git.require_integration_worktree(integration, state.integration_branch)
                state = self._change(state, integration_worktree_fingerprint=self.git.fingerprint(integration))
                raise
            self.git.require_integration_worktree(integration, state.integration_branch)
            if self.git.head(integration) != head_before or self.git.has_unmerged(integration):
                raise FlowError("Post-conflict audit must not move integration HEAD or leave conflicts.")
            self._require_summary(integration, "post-conflict-audit-summary.md")
            state = self._transition(state, WorkflowStage.POST_CONFLICT_AUDIT_COMPLETE, integration_worktree_fingerprint=self.git.fingerprint(integration))
        return state

    def _stage_integration(self, state: WorkflowState, integration: Path) -> WorkflowState:
        assert self.integration is not None and self.git is not None
        if state.stage is WorkflowStage.INTEGRATION_MERGE_APPLIED:
            self._verify_integration_checkpoint(state, integration)
            self.integration.stage_non_artifacts(integration)
            self.integration.require_staged_changes(integration)
            state = self._transition(state, WorkflowStage.INTEGRATION_CHANGES_STAGED, integration_worktree_fingerprint=self.git.fingerprint(integration))
        elif state.stage is WorkflowStage.POST_CONFLICT_AUDIT_COMPLETE:
            self._verify_integration_checkpoint(state, integration)
            self.integration.stage_non_artifacts(integration)
            self.integration.require_staged_changes(integration)
            state = self._transition(state, WorkflowStage.INTEGRATION_CHANGES_STAGED, integration_worktree_fingerprint=self.git.fingerprint(integration))
        elif state.stage is WorkflowStage.INTEGRATION_CHANGES_STAGED:
            self._verify_integration_checkpoint(state, integration)
            self.integration.require_staged_changes(integration)
        return state

    def _commit_integration(self, state: WorkflowState, integration: Path) -> WorkflowState:
        assert self.git is not None
        assert self.integration is not None
        if state.stage is WorkflowStage.INTEGRATION_CHANGES_STAGED:
            subject = f"Harness: {state.plan_title}"
            head = self.git.head(integration)
            parents = self.git.commit_parents(head, integration)
            generated = (
                self.git.commit_subject(head, integration) == subject
                and state.integration_base_commit is not None
                and parents
                and parents[0] == state.integration_base_commit
                and (
                    state.merge_mode == "squash"
                    and len(parents) == 1
                    or state.merge_mode == "no-ff"
                    and len(parents) == 2
                    and state.integration_feature_commit in parents[1:]
                )
            )
            if generated:
                self.git.require_clean_except_artifacts(integration, phase="Completed integration commit")
                state = self._transition(state, WorkflowStage.INTEGRATION_COMMITTED, integration_commit=head)
                return state
            self._verify_integration_checkpoint(state, integration)
            self.git.require_no_unstaged_non_artifact(integration, phase="Integration before commit")
            self.integration.require_staged_changes(integration)
            self.runner.run(["git", "commit", "-m", subject], integration)
            commit = self.git.head(integration)
            parents = self.git.commit_parents(commit, integration)
            if (
                self.git.commit_subject(commit, integration) != subject
                or state.integration_base_commit is None
                or not parents
                or parents[0] != state.integration_base_commit
                or (
                    state.merge_mode == "squash"
                    and len(parents) != 1
                    or state.merge_mode == "no-ff"
                    and (
                        len(parents) != 2
                        or state.integration_feature_commit not in parents[1:]
                    )
                )
            ):
                raise FlowError("Integration commit receipt does not match the required parent shape.")
            state = self._transition(state, WorkflowStage.INTEGRATION_COMMITTED, integration_commit=commit)
        elif state.stage is WorkflowStage.INTEGRATION_COMMITTED:
            if state.integration_commit is None:
                raise FlowError("Integration commit receipt is missing.")
            self.git.require_commit_oid(state.integration_commit, label="integration commit")
            if self.git.head(integration) != state.integration_commit:
                raise FlowError("Integration HEAD differs from integration_commit receipt.")
        return state

    def _fast_forward(self, state: WorkflowState, integration: Path) -> WorkflowState:
        assert self.git is not None
        self.git.require_primary_ready(state.base_branch)
        if state.integration_branch is None or state.integration_commit is None or state.integration_base_commit is None:
            raise FlowError("Integration state is incomplete before fast-forward.")
        feature_worktree = Path(state.feature_worktree)
        self.git.require_feature_worktree(feature_worktree, state.feature_branch)
        self.git.require_clean_except_artifacts(feature_worktree, phase="Feature before fast-forward")
        self.git.require_integration_worktree(integration, state.integration_branch)
        if self.git.head(integration) != state.integration_commit or self.git.branch_tip(state.integration_branch) != state.integration_commit:
            raise FlowError("Integration HEAD or branch differs from integration_commit receipt.")
        self.git.require_clean_except_artifacts(integration, phase="Integration before fast-forward")
        self._verify_plan(state)
        feature_head = self.git.head(feature_worktree)
        if (
            state.audit_head is None
            or state.integration_feature_commit is None
            or feature_head != state.audit_head
            or feature_head != state.integration_feature_commit
        ):
            raise FlowError("Feature HEAD changed after the recorded integration receipt.")
        if self.git.commit_subject(state.integration_commit, integration) != f"Harness: {state.plan_title}":
            raise FlowError("Integration commit subject is not generated by this workflow.")
        parents = self.git.commit_parents(state.integration_commit, integration)
        if state.merge_mode == "squash":
            expected_parents = (state.integration_base_commit,)
        elif state.merge_mode == "no-ff" and state.integration_feature_commit is not None:
            expected_parents = (state.integration_base_commit, state.integration_feature_commit)
        else:
            raise FlowError("Integration merge mode or feature receipt is invalid.")
        if parents != expected_parents:
            raise FlowError("Integration commit parents differ from the recorded merge receipt.")
        current_base = self.git.branch_tip(state.base_branch)
        if current_base != state.integration_base_commit:
            if self.git.merge_base_is_ancestor(state.integration_base_commit, current_base):
                state = self._transition(state, WorkflowStage.INTEGRATION_REBUILD_CLEANUP)
                self._cleanup_generated_integration(state, integration)
                cleared = replace(
                    state,
                    stage=WorkflowStage.AUDIT_COMPLETE,
                    integration_branch=None,
                    integration_worktree=None,
                    integration_base_commit=None,
                    integration_feature_commit=None,
                    integration_worktree_fingerprint=None,
                    integration_commit=None,
                )
                return self._save(cleared)
            raise FlowError("Base branch diverged from the recorded integration base.")
        if state.stage is WorkflowStage.INTEGRATION_COMMITTED:
            if current_base != state.integration_commit:
                self.git.require_primary_ready(state.base_branch)
                self.runner.run(["git", "merge", "--ff-only", "--end-of-options", state.integration_branch], self.repo)
            if not self.git.base_contains(state.integration_commit, state.base_branch):
                raise FlowError("Primary base does not contain the integration commit after fast-forward.")
            state = self._transition(state, WorkflowStage.BASE_FAST_FORWARDED)
        return state

    def _cleanup_generated_integration(self, state: WorkflowState, integration: Path) -> None:
        assert self.git is not None
        self.git.require_primary_ready(state.base_branch)
        if state.integration_branch is None or state.integration_commit is None:
            raise FlowError("Integration receipt is incomplete during rebuild cleanup.")
        assert self.git is not None
        target = canonical_path(integration, must_exist=False)
        entries = [entry for entry in self.git.worktrees() if entry.path == target]
        if entries:
            self.git.require_integration_worktree(target, state.integration_branch)
            self.git.require_clean_except_artifacts(target, phase="Integration rebuild cleanup")
            if self.git.head(target) != state.integration_commit:
                raise FlowError("Integration HEAD changed before rebuild cleanup.")
        elif target.exists():
            raise FlowError(f"Generated integration worktree is not registered at the recorded path: {target}")
        if self.git.branch_exists(state.integration_branch):
            if self.git.branch_tip(state.integration_branch) != state.integration_commit:
                raise FlowError("Integration branch changed before rebuild cleanup.")
        self._remove_or_adopt_worktree(target, state.integration_branch, allow_adopt=True, branch_may_be_absent=True)
        if self.git.branch_exists(state.integration_branch):
            self.git.delete_branch(state.integration_branch, force=True)
    def _archive(self, state: WorkflowState, source: Path, plan: Path) -> tuple[WorkflowState, Path]:
        assert self.git is not None and self.integration is not None and self.state_store is not None
        if state.integration_branch is None or state.integration_commit is None:
            raise FlowError("Integration state is incomplete before archive.")
        self._verify_plan(state)
        feature_worktree = Path(state.feature_worktree)
        self.git.require_feature_worktree(feature_worktree, state.feature_branch)
        self.git.require_clean_except_artifacts(feature_worktree, phase="Feature before archive")
        if state.audit_head is None or self.git.head(feature_worktree) != state.audit_head:
            raise FlowError("Feature HEAD changed before archive.")
        self.git.require_integration_worktree(source, state.integration_branch)
        if self.git.head(source) != state.integration_commit or self.git.branch_tip(state.integration_branch) != state.integration_commit:
            raise FlowError("Integration HEAD or branch differs from integration_commit receipt.")
        self.git.require_clean_except_artifacts(source, phase="Integration before archive")
        archive = self._archive_candidate(state.run_id)
        self.integration.archive_handoff(source, archive, plan)
        state = self._transition(state, WorkflowStage.HANDOFF_ARCHIVED, archive_dir=str(canonical_path(archive, must_exist=True)))
        self.integration.write_candidate_state(archive, state)
        return state, archive

    def _archive_receipt(self, commit: str, archive: Path, relative: str, expected_parent: str) -> None:
        assert self.git is not None and self.integration is not None
        manifest = self.integration.exact_manifest(archive)
        expected_paths = tuple(sorted(f"{relative}/{name}" for name in manifest))
        actual_paths = tuple(sorted(self.git.commit_paths(commit, relative, self.repo)))
        if self.git.commit_parents(commit, self.repo) != (expected_parent,):
            raise FlowError("Archive commit receipt does not have the expected parent.")
        if actual_paths != expected_paths:
            raise FlowError("Archive commit receipt does not match the exact archive manifest.")
        if not self.git.commit_touches(commit, relative, self.repo):
            raise FlowError("Archive commit receipt does not touch the generated archive.")

    def _commit_archive(self, state: WorkflowState, archive: Path) -> WorkflowState:
        assert self.git is not None and self.integration is not None
        self.git.require_primary_ready(state.base_branch)
        if state.integration_commit is None:
            raise FlowError("Integration commit receipt is missing before archive commit.")
        relative = archive.relative_to(self.repo).as_posix()
        subject = f"Harness: archive {state.plan_title}"
        literal_relative = f":(literal){relative}"
        head = self.git.head(self.repo)
        if state.stage is WorkflowStage.HANDOFF_ARCHIVED and self.git.commit_subject(head, self.repo) == subject:
            self._archive_receipt(head, archive, relative, state.integration_commit)
            return self._transition(state, WorkflowStage.ARTIFACTS_COMMITTED, archive_commit=head)
        self.integration.exact_manifest(archive)
        self.runner.run(["git", "add", "-A", "--", literal_relative], self.repo)
        diff = self.runner.run(["git", "diff", "--cached", "--quiet", "--", literal_relative], self.repo, check=False)
        if diff.returncode == 1:
            self.runner.run(["git", "commit", "-m", subject, "--", literal_relative], self.repo)
            commit = self.git.head(self.repo)
            if self.git.commit_subject(commit, self.repo) != subject:
                raise FlowError("Archive commit subject does not match the generated archive.")
            self._archive_receipt(commit, archive, relative, state.integration_commit)
            state = self._transition(state, WorkflowStage.ARTIFACTS_COMMITTED, archive_commit=commit)
        elif diff.returncode == 0:
            raise FlowError("Archive contains no staged changes to commit.")
        else:
            raise FlowError(format_command_failure(diff))
        return state

    def _remove_or_adopt_worktree(
        self,
        path: Path,
        branch: str,
        *,
        allow_adopt: bool,
        branch_may_be_absent: bool = False,
    ) -> None:
        assert self.git is not None
        target = canonical_path(path, must_exist=False)
        entries = [entry for entry in self.git.worktrees() if entry.path == target]
        if entries:
            self.git.remove_registered_worktree(target, branch)
            return
        if allow_adopt and not target.exists() and (branch_may_be_absent or self.git.branch_exists(branch)):
            return
        raise FlowError(f"Generated worktree is not registered at the recorded path: {target}")

    def _cleanup_stage(self, state: WorkflowState, integration: Path) -> WorkflowState:
        assert self.git is not None
        if state.keep_worktrees:
            if state.stage is WorkflowStage.ARTIFACTS_COMMITTED:
                state = self._transition(state, WorkflowStage.WORKTREES_RETAINED)
            return state
        self.git.require_primary_ready(state.base_branch)
        if state.stage is WorkflowStage.ARTIFACTS_COMMITTED:
            if state.integration_branch is None or state.integration_commit is None:
                raise FlowError("Integration receipt is incomplete during cleanup.")
            if self.git.branch_exists(state.integration_branch) and self.git.branch_tip(state.integration_branch) != state.integration_commit:
                raise FlowError("Integration branch changed before cleanup.")
            if any(entry.path == integration for entry in self.git.worktrees()):
                self.git.require_integration_worktree(integration, state.integration_branch)
                self.git.require_clean_except_artifacts(integration, phase="Integration before cleanup")
            self._remove_or_adopt_worktree(integration, state.integration_branch, allow_adopt=True)
            state = self._transition(state, WorkflowStage.INTEGRATION_WORKTREE_REMOVED)
        if state.stage is WorkflowStage.INTEGRATION_WORKTREE_REMOVED:
            feature = Path(state.feature_worktree)
            if state.audit_head is None:
                raise FlowError("Feature audit receipt is missing during cleanup.")
            if self.git.branch_exists(state.feature_branch) and self.git.branch_tip(state.feature_branch) != state.audit_head:
                raise FlowError("Feature branch changed before cleanup.")
            if any(entry.path == feature for entry in self.git.worktrees()):
                self.git.require_feature_worktree(feature, state.feature_branch)
                self.git.require_clean_except_artifacts(feature, phase="Feature before cleanup")
            self._remove_or_adopt_worktree(feature, state.feature_branch, allow_adopt=True)
            state = self._transition(state, WorkflowStage.FEATURE_WORKTREE_REMOVED)
        if state.stage is WorkflowStage.FEATURE_WORKTREE_REMOVED:
            self.git.prune()
            state = self._transition(state, WorkflowStage.WORKTREES_PRUNED)
        if state.stage is WorkflowStage.WORKTREES_PRUNED:
            if state.integration_branch and self.git.branch_exists(state.integration_branch):
                if state.integration_commit is None or self.git.branch_tip(state.integration_branch) != state.integration_commit:
                    raise FlowError("Integration branch changed before cleanup.")
                self.git.delete_branch(state.integration_branch, force=True)
            state = self._transition(state, WorkflowStage.INTEGRATION_BRANCH_REMOVED)
        if state.stage is WorkflowStage.INTEGRATION_BRANCH_REMOVED:
            if self.git.branch_exists(state.feature_branch):
                if state.audit_head is None or self.git.branch_tip(state.feature_branch) != state.audit_head:
                    raise FlowError("Feature branch changed before cleanup.")
                self.git.delete_branch(state.feature_branch, force=True)
            state = self._transition(state, WorkflowStage.FEATURE_BRANCH_REMOVED)
        if state.stage is WorkflowStage.FEATURE_BRANCH_REMOVED:
            state = self._transition(state, WorkflowStage.CLEANUP_COMPLETE)
        return state

    def _finalize(self, state: WorkflowState, archive: Path) -> WorkflowState:
        assert self.git is not None and self.integration is not None and self.state_store is not None
        if state.stage not in {WorkflowStage.CLEANUP_COMPLETE, WorkflowStage.WORKTREES_RETAINED}:
            return state
        if state.archive_commit is None:
            raise FlowError("Archive commit receipt is missing before finalization.")
        relative = archive.relative_to(self.repo).as_posix()
        literal_relative = f":(literal){relative}"
        subject = f"Harness: finalize {state.plan_title}"
        head = self.git.head(self.repo)
        if self.git.commit_subject(head, self.repo) == subject:
            try:
                candidate = self.state_store.read_candidate_state(archive / WORKFLOW_STATE_FILENAME)
            except FlowError:
                candidate = None
            if candidate is not None and candidate.run_id == state.run_id and candidate.stage is WorkflowStage.COMPLETE:
                self._archive_receipt(head, archive, relative, state.archive_commit)
                state = self._change(state, final_state_commit=head)
                return self._transition(state, WorkflowStage.COMPLETE, final_state_commit=head)
        self.integration.write_candidate_state(archive, replace(state, stage=WorkflowStage.COMPLETE, final_state_commit=None))
        self.git.require_primary_ready(state.base_branch)
        self.runner.run(["git", "add", "-A", "--", literal_relative], self.repo)
        self.runner.run(["git", "commit", "-m", subject, "--", literal_relative], self.repo)
        commit = self.git.head(self.repo)
        if self.git.commit_subject(commit, self.repo) != subject:
            raise FlowError("Final-state commit subject does not match the generated archive.")
        self._archive_receipt(commit, archive, relative, state.archive_commit)
        state = self._change(state, final_state_commit=commit)
        state = self._transition(state, WorkflowStage.COMPLETE, final_state_commit=commit)
        return state

    def _finish(self, state: WorkflowState, feature_plan: Path) -> WorkflowState:
        assert self.git is not None
        self.git.require_primary_ready(state.base_branch)
        if state.stage is WorkflowStage.COMPLETE:
            return state
        if state.stage is WorkflowStage.WORKTREES_RETAINED:
            archive = Path(state.archive_dir or self._archive_candidate(state.run_id))
            return self._finalize(state, archive)
        allocation_stages = {
            WorkflowStage.AUDIT_COMPLETE,
            WorkflowStage.INTEGRATION_ALLOCATED,
            WorkflowStage.INTEGRATION_WORKTREE_CREATED,
            WorkflowStage.INTEGRATION_CONFLICTS_DETECTED,
            WorkflowStage.CONFLICT_RESOLUTION_STARTED,
            WorkflowStage.CONFLICT_RESOLUTION_COMPLETE,
            WorkflowStage.POST_CONFLICT_AUDIT_STARTED,
            WorkflowStage.POST_CONFLICT_AUDIT_COMPLETE,
            WorkflowStage.INTEGRATION_MERGE_APPLIED,
            WorkflowStage.INTEGRATION_CHANGES_STAGED,
            WorkflowStage.INTEGRATION_COMMITTED,
        }
        cleanup_stages = {
            WorkflowStage.ARTIFACTS_COMMITTED,
            WorkflowStage.INTEGRATION_WORKTREE_REMOVED,
            WorkflowStage.FEATURE_WORKTREE_REMOVED,
            WorkflowStage.WORKTREES_PRUNED,
            WorkflowStage.INTEGRATION_BRANCH_REMOVED,
            WorkflowStage.FEATURE_BRANCH_REMOVED,
        }
        while state.stage is not WorkflowStage.COMPLETE:
            if state.stage is WorkflowStage.INTEGRATION_REBUILD_CLEANUP:
                if state.integration_base_commit is None or state.integration_commit is None:
                    raise FlowError("Integration receipt is incomplete during rebuild cleanup.")
                current_base = self.git.branch_tip(state.base_branch)
                if current_base == state.integration_base_commit or not self.git.merge_base_is_ancestor(
                    state.integration_base_commit, current_base
                ):
                    raise FlowError("Primary base changed incompatibly before integration rebuild cleanup.")
                integration = Path(state.integration_worktree or "")
                self._cleanup_generated_integration(state, integration)
                state = self._save(
                    replace(
                        state,
                        stage=WorkflowStage.AUDIT_COMPLETE,
                        integration_branch=None,
                        integration_worktree=None,
                        integration_base_commit=None,
                        integration_feature_commit=None,
                        integration_worktree_fingerprint=None,
                        integration_commit=None,
                    )
                )
                continue
            if state.stage in allocation_stages:
                state, integration = self._allocate_integration(state)
            else:
                integration = Path(state.integration_worktree or "")
            if state.stage is WorkflowStage.INTEGRATION_WORKTREE_CREATED:
                integration_plan = self._integration_plan(state, feature_plan, integration)
                state = self._merge_integration(state, integration)
                if state.stage in {
                    WorkflowStage.INTEGRATION_CONFLICTS_DETECTED,
                    WorkflowStage.CONFLICT_RESOLUTION_STARTED,
                    WorkflowStage.CONFLICT_RESOLUTION_COMPLETE,
                    WorkflowStage.POST_CONFLICT_AUDIT_STARTED,
                }:
                    state = self._resolve_conflicts(state, integration_plan, integration)
                state = self._stage_integration(state, integration)
                state = self._commit_integration(state, integration)
            elif state.stage in {
                WorkflowStage.INTEGRATION_CONFLICTS_DETECTED,
                WorkflowStage.CONFLICT_RESOLUTION_STARTED,
                WorkflowStage.CONFLICT_RESOLUTION_COMPLETE,
                WorkflowStage.POST_CONFLICT_AUDIT_STARTED,
            }:
                integration_plan = self._integration_plan(state, feature_plan, integration)
                state = self._resolve_conflicts(state, integration_plan, integration)
                state = self._stage_integration(state, integration)
                state = self._commit_integration(state, integration)
            elif state.stage in {WorkflowStage.INTEGRATION_MERGE_APPLIED, WorkflowStage.POST_CONFLICT_AUDIT_COMPLETE}:
                state = self._stage_integration(state, integration)
                state = self._commit_integration(state, integration)
            elif state.stage is WorkflowStage.INTEGRATION_CHANGES_STAGED:
                state = self._commit_integration(state, integration)
            elif state.stage is WorkflowStage.INTEGRATION_COMMITTED:
                state = self._fast_forward(state, integration)
                if state.stage is WorkflowStage.AUDIT_COMPLETE:
                    continue
            elif state.stage is WorkflowStage.BASE_FAST_FORWARDED:
                state, archive = self._archive(state, integration, feature_plan)
                state = self._commit_archive(state, archive)
            elif state.stage is WorkflowStage.HANDOFF_ARCHIVED:
                archive = Path(state.archive_dir or self._archive_candidate(state.run_id))
                state = self._commit_archive(state, archive)
            elif state.stage in cleanup_stages:
                archive = Path(state.archive_dir or self._archive_candidate(state.run_id))
                state = self._cleanup_stage(state, integration)
                if state.stage in {WorkflowStage.CLEANUP_COMPLETE, WorkflowStage.WORKTREES_RETAINED}:
                    return self._finalize(state, archive)
            elif state.stage is WorkflowStage.CLEANUP_COMPLETE:
                archive = Path(state.archive_dir or self._archive_candidate(state.run_id))
                return self._finalize(state, archive)
            else:
                raise FlowError(f"Cannot dispatch integration stage {state.stage.value}.")
        return state

    def _commit_stop_archive(self, state: WorkflowState, archive: Path) -> WorkflowState:
        assert self.git is not None and self.integration is not None
        self.git.require_primary_ready(state.base_branch)
        relative = archive.relative_to(self.repo).as_posix()
        literal_relative = f":(literal){relative}"
        subject = f"Harness: stop {state.plan_title}"
        head = self.git.head(self.repo)
        if self.git.commit_subject(head, self.repo) == subject:
            parents = self.git.commit_parents(head, self.repo)
            if len(parents) == 1:
                self._archive_receipt(head, archive, relative, parents[0])
                return self._change(state, archive_commit=head)
        parent = head
        self.runner.run(["git", "add", "-A", "--", literal_relative], self.repo)
        diff = self.runner.run(["git", "diff", "--cached", "--quiet", "--", literal_relative], self.repo, check=False)
        if diff.returncode == 0:
            raise FlowError("Stopped archive contains no staged changes to commit.")
        if diff.returncode != 1:
            raise FlowError(format_command_failure(diff))
        self.runner.run(["git", "commit", "-m", subject, "--", literal_relative], self.repo)
        commit = self.git.head(self.repo)
        if self.git.commit_subject(commit, self.repo) != subject:
            raise FlowError("Stopped archive commit subject does not match the generated archive.")
        self._archive_receipt(commit, archive, relative, parent)
        return self._change(state, archive_commit=commit)

    def run(self) -> None:
        if self.config.dry_run:
            self.preview()
            return
        plan, digest, title, _base = self._validate_runtime()
        assert self.git is not None and self.state_store is not None
        slug = derive_slug(plan)
        saved_run_id = self._plan_run_id(plan)
        self._validate_saved_plan_reservation(plan, saved_run_id)
        while True:
            names = self._choose_names(slug, saved_run_id, plan_digest=digest)
            try:
                run_dir = self.state_store.reserve_run(names.run_id)
            except FlowError as exc:
                if saved_run_id is not None or "already exists" not in str(exc):
                    raise
                continue
            break
        with self.state_store.lock(names.run_id):
            state = self._new_state(names, plan, title, self.git.branch_tip(self.base))
            if self.usage is not None:
                self.usage.set_run_id(state.run_id)
            self._save(state)
            self.print_checkpoint("start", "Worktree flow", (("plan", plan), ("base", self.base), ("merge mode", state.merge_mode)))
            self.print_checkpoint("ready", "Feature target", (("branch", names.feature_branch), ("worktree", names.feature_worktree), ("state", run_dir)))
            state = self._allocate_feature(state, names)
            state, plan_in_worktree = self._run_feature_stages(state, plan)
            if state.merge_mode == "stop":
                state = self._transition(state, WorkflowStage.STOPPED_BEFORE_MERGE)
                archive = self._archive_candidate(state.run_id)
                assert self.integration is not None
                self.integration.archive_handoff(Path(state.feature_worktree), archive, plan_in_worktree)
                state = self._change(state, archive_dir=str(canonical_path(archive, must_exist=True)))
                self.integration.write_candidate_state(archive, state)
                state = self._commit_stop_archive(state, archive)
                self.print_checkpoint("stop", "Stopped before merge", (("archive", archive), ("worktree", state.feature_worktree)))
                return
            state = self._finish(state, plan_in_worktree)
            self.print_checkpoint("done", "Worktree flow", (("stage", state.stage.value),))

    def _validate_complete_receipts(self, state: WorkflowState) -> None:
        assert self.git is not None and self.integration is not None
        if (
            state.archive_dir is None
            or state.archive_commit is None
            or state.integration_commit is None
            or state.final_state_commit is None
        ):
            raise FlowError("Completed workflow state is missing terminal receipts.")
        archive = canonical_path(Path(state.archive_dir), must_exist=True)
        relative = archive.relative_to(self.repo).as_posix()
        archive_subject = f"Harness: archive {state.plan_title}"
        if self.git.commit_subject(state.archive_commit, self.repo) != archive_subject:
            raise FlowError("Archive commit receipt subject does not match the generated archive.")
        if self.git.commit_parents(state.archive_commit, self.repo) != (state.integration_commit,):
            raise FlowError("Archive commit receipt parent does not match integration_commit.")
        self._archive_receipt(state.archive_commit, archive, relative, state.integration_commit)
        final_subject = f"Harness: finalize {state.plan_title}"
        if self.git.commit_subject(state.final_state_commit, self.repo) != final_subject:
            raise FlowError("Final-state commit receipt subject does not match the generated archive.")
        if self.git.commit_parents(state.final_state_commit, self.repo) != (state.archive_commit,):
            raise FlowError("Final-state commit receipt parent does not match archive_commit.")
        self._archive_receipt(state.final_state_commit, archive, relative, state.archive_commit)

    def _validate_resume_state(self, state: WorkflowState, requested_worktree: Path | None) -> None:
        assert self.git is not None and self.state_store is not None
        if requested_worktree is not None and canonical_path(requested_worktree, must_exist=False) != Path(state.feature_worktree):
            raise FlowError("Explicit --worktree does not match the recorded feature worktree.")
        self.git.require_local_branch(state.base_branch)
        self.git.validate_local_branch_name(state.feature_branch)
        if state.integration_branch is not None:
            self.git.validate_local_branch_name(state.integration_branch)
        self.git.require_commit_oid(state.feature_base_commit, label="feature base")
        for value, label in (
            (state.implementation_head, "implementation head"),
            (state.audit_start_head, "audit start head"),
            (state.audit_head, "audit head"),
            (state.integration_base_commit, "integration base"),
            (state.integration_feature_commit, "integration feature"),
            (state.integration_commit, "integration commit"),
            (state.archive_commit, "archive commit"),
            (state.final_state_commit, "final state commit"),
        ):
            if value is not None:
                self.git.require_commit_oid(value, label=label)
        if state.stage is WorkflowStage.COMPLETE:
            expected_archive = self._archive_candidate(state.run_id)
            if state.archive_dir is None or canonical_path(Path(state.archive_dir), must_exist=False) != canonical_path(expected_archive, must_exist=False):
                raise FlowError("Completed workflow archive path does not match its run id.")
            self._validate_complete_receipts(state)
            return
        feature_removed_stages = {
            WorkflowStage.INTEGRATION_WORKTREE_REMOVED,
            WorkflowStage.FEATURE_WORKTREE_REMOVED,
            WorkflowStage.WORKTREES_PRUNED,
            WorkflowStage.INTEGRATION_BRANCH_REMOVED,
            WorkflowStage.FEATURE_BRANCH_REMOVED,
            WorkflowStage.CLEANUP_COMPLETE,
        }
        feature_path = canonical_path(Path(state.feature_worktree), must_exist=False)
        expected_feature = (self.repo.parent / f"{self.repo.name}-{state.slug}").resolve()
        if feature_path != expected_feature:
            raise FlowError("Recorded feature worktree path is not generated for this run.")
        if state.feature_branch != f"feature/{state.slug}":
            raise FlowError("Recorded feature branch is not generated for this run.")
        if state.archive_dir is not None and canonical_path(Path(state.archive_dir), must_exist=False) != self._archive_candidate(state.run_id):
            raise FlowError("Recorded archive path does not match its run id.")
        if state.stage is not WorkflowStage.FEATURE_ALLOCATED and state.stage in feature_removed_stages:
            entries = [entry for entry in self.git.worktrees() if entry.path == feature_path]
            if entries:
                if entries[0].branch != state.feature_branch:
                    raise FlowError("Recorded feature allocation is mismatched.")
            elif feature_path.exists():
                raise FlowError("Recorded feature path exists without its registered worktree.")
            elif state.stage not in {
                WorkflowStage.INTEGRATION_BRANCH_REMOVED,
                WorkflowStage.FEATURE_BRANCH_REMOVED,
                WorkflowStage.CLEANUP_COMPLETE,
            } and not self.git.branch_exists(state.feature_branch):
                raise FlowError("Recorded feature worktree disappeared before its checkpoint.")
        elif state.stage is not WorkflowStage.FEATURE_ALLOCATED:
            self._validate_feature_identity(state)
        if state.stage in {WorkflowStage.FEATURE_ALLOCATED, WorkflowStage.FEATURE_WORKTREE_CREATED}:
            self._validate_pre_copy_plan(state)
        if state.stage not in {WorkflowStage.FEATURE_ALLOCATED, WorkflowStage.FEATURE_WORKTREE_CREATED, *feature_removed_stages}:
            expected_plan = Path(state.feature_worktree) / self.worktree_flow_dir / state.run_id / "plan.md"
            if canonical_path(expected_plan, must_exist=False) != Path(state.plan_path):
                raise FlowError("Recorded plan path does not match the generated feature plan path.")
            self._verify_plan(state)
        if state.integration_worktree is not None or state.integration_branch is not None:
            if state.integration_worktree is None or state.integration_branch is None:
                raise FlowError("Integration state has only one of branch/worktree.")
            if re.fullmatch(rf"integration/{re.escape(state.slug)}-\d{{8}}-\d{{6}}(?:-\d+)?", state.integration_branch) is None:
                raise FlowError("Recorded integration branch is not generated for this run.")
            integration_path = canonical_path(Path(state.integration_worktree), must_exist=False)
            if integration_path.parent != self.repo.parent or re.fullmatch(
                rf"{re.escape(self.repo.name)}-integrate-{re.escape(state.slug)}-\d{{8}}-\d{{6}}(?:-\d+)?",
                integration_path.name,
            ) is None:
                raise FlowError("Recorded integration worktree path is not generated for this run.")
            integration_missing_stages = {
                WorkflowStage.INTEGRATION_REBUILD_CLEANUP,
                WorkflowStage.ARTIFACTS_COMMITTED,
                WorkflowStage.INTEGRATION_WORKTREE_REMOVED,
                WorkflowStage.FEATURE_WORKTREE_REMOVED,
                WorkflowStage.WORKTREES_PRUNED,
                WorkflowStage.INTEGRATION_BRANCH_REMOVED,
                WorkflowStage.FEATURE_BRANCH_REMOVED,
                WorkflowStage.CLEANUP_COMPLETE,
            }
            if state.stage not in integration_missing_stages:
                self.git.require_integration_worktree(integration_path, state.integration_branch)
            else:
                entries = [entry for entry in self.git.worktrees() if entry.path == integration_path]
                if entries:
                    if entries[0].branch != state.integration_branch:
                        raise FlowError("Recorded integration allocation is mismatched.")
                elif integration_path.exists():
                    raise FlowError("Recorded integration path exists without its registered worktree.")
                elif state.stage not in {
                    WorkflowStage.INTEGRATION_REBUILD_CLEANUP,
                    WorkflowStage.WORKTREES_PRUNED,
                    WorkflowStage.INTEGRATION_BRANCH_REMOVED,
                    WorkflowStage.FEATURE_BRANCH_REMOVED,
                    WorkflowStage.CLEANUP_COMPLETE,
                } and not self.git.branch_exists(state.integration_branch):
                    raise FlowError("Recorded integration worktree disappeared before its checkpoint.")
    def _find_resume_run(self, plan: Path, requested_worktree: Path | None) -> tuple[str, WorkflowState]:
        assert self.state_store is not None
        candidates: list[tuple[str, WorkflowState]] = []
        if requested_worktree is not None:
            target = canonical_path(requested_worktree, must_exist=False)
        else:
            target = None
        if not self.state_store.root.exists():
            raise FlowError("No external workflow state exists; start a new run.")
        for run_dir in regular_directory_entries(self.state_store.root):
            if not run_dir.is_dir():
                continue
            state_path = run_dir / WORKFLOW_STATE_FILENAME
            if not optional_regular_file(state_path, label="workflow state"):
                continue
            try:
                state = self.state_store.load(run_dir.name)
            except FlowError:
                continue
            if target is not None and Path(state.feature_worktree) != target:
                continue
            if plan.exists():
                try:
                    if sha256_file(plan) != state.plan_sha256:
                        continue
                except FlowError:
                    continue
            candidates.append((state.run_id, state))
        if len(candidates) != 1:
            raise FlowError("Implicit resume requires exactly one matching schema-v2 workflow state; pass --worktree.")
        return candidates[0]


    def _apply_resume_configuration(self, state: WorkflowState) -> WorkflowState:
        if self.config.harness_explicit and self.config.harness != state.harness:
            raise FlowError("Persisted harness is authoritative on resume.")
        if self.config.harness_dir_explicit and self.harness_dir.as_posix() != state.harness_dir:
            raise FlowError("Persisted harness directory is authoritative on resume.")
        if self.config.keep_worktrees_explicit and self.config.keep_worktrees != state.keep_worktrees:
            raise FlowError("Persisted keep-worktrees setting is authoritative on resume.")
        harness = state.harness
        harness_dir = Path(state.harness_dir)
        implementation_model = state.implementation_model
        review_model = state.review_model
        model = None
        if self.config.model_explicit:
            model = self.config.model
            implementation_model = self.config.implementation_model if self.config.implementation_model_explicit else model
            review_model = self.config.review_model if self.config.review_model_explicit else model
        else:
            if self.config.implementation_model_explicit:
                implementation_model = self.config.implementation_model
            if self.config.review_model_explicit:
                review_model = self.config.review_model
        timeout = (
            self.config.command_timeout_seconds
            if self.config.command_timeout_explicit
            else state.command_timeout_seconds
        )
        self.config = replace(
            self.config,
            repo=self.repo,
            base=state.base_branch,
            harness=harness,
            harness_dir=harness_dir,
            model=model,
            implementation_model=implementation_model,
            review_model=review_model,
            command_timeout_seconds=timeout,
            keep_worktrees=state.keep_worktrees,
        )
        self.runner.command_timeout_seconds = timeout
        self._bind_runtime(self.repo, Path(state.git_common_dir))
        assert self.adapter is not None and self.git is not None
        if self.adapter.kind.value != state.harness_kind:
            raise FlowError("Persisted harness kind does not match its executable.")
        self.adapter.validate(self.repo)
        self.adapter.prepare_permissions([self.repo / self.harness_dir, self.git.git_common_dir(self.repo)])
        if self.usage is not None:
            self.usage.set_run_id(state.run_id)
        if state.stage is WorkflowStage.COMPLETE:
            return state
        changes: dict[str, object] = {}
        if implementation_model != state.implementation_model:
            changes["implementation_model"] = implementation_model
        if review_model != state.review_model:
            changes["review_model"] = review_model
        if timeout != state.command_timeout_seconds:
            changes["command_timeout_seconds"] = timeout
        return self._change(state, **changes) if changes else state
    def resume(self) -> None:
        if self.config.dry_run:
            self.preview()
            return
        requested_plan = self.config.plan
        raw_root = GitWorkspace.git_root(self.config.repo, self.runner)
        self._bind_runtime(raw_root)
        assert self.state_store is not None and self.git is not None
        run_id, _candidate = self._find_resume_run(requested_plan, self.config.worktree)
        with self.state_store.lock(run_id):
            state = self.state_store.load(run_id)
            self._validate_resume_state(state, self.config.worktree)
            state = self._apply_resume_configuration(state)
            self._last_state = state
            if state.stage is WorkflowStage.COMPLETE:
                self.print_checkpoint(
                    "complete",
                    "Worktree flow already complete",
                    (("run id", state.run_id), ("final state commit", state.final_state_commit)),
                )
                return
            state = self._merge_mode_resume(state)
            if state is None:
                return
            resumable_stages = {
                WorkflowStage.FEATURE_ALLOCATED,
                WorkflowStage.FEATURE_WORKTREE_CREATED,
                WorkflowStage.PLAN_COPIED,
                WorkflowStage.IMPLEMENTATION_STARTED,
                WorkflowStage.IMPLEMENTATION_COMPLETE,
                WorkflowStage.AUDIT_STARTED,
                WorkflowStage.AUDIT_COMPLETE,
                WorkflowStage.STOPPED_BEFORE_MERGE,
            }
            if state.stage in resumable_stages:
                if state.stage is WorkflowStage.FEATURE_ALLOCATED:
                    names = Names(state.slug, state.feature_branch, Path(state.feature_worktree), state.run_id)
                    state = self._allocate_feature(state, names)
                copied_plan = Path(state.plan_path)
                if state.stage is not WorkflowStage.FEATURE_ALLOCATED:
                    self._verify_plan(state)
                self.continue_log(Path(state.feature_worktree), state.run_id)
                state, copied_plan = self._run_feature_stages(state, copied_plan)
            else:
                copied_plan = Path(state.plan_path)
                if state.stage not in {
                    WorkflowStage.INTEGRATION_REBUILD_CLEANUP,
                    WorkflowStage.INTEGRATION_WORKTREE_REMOVED,
                    WorkflowStage.FEATURE_WORKTREE_REMOVED,
                    WorkflowStage.WORKTREES_PRUNED,
                    WorkflowStage.INTEGRATION_BRANCH_REMOVED,
                    WorkflowStage.FEATURE_BRANCH_REMOVED,
                    WorkflowStage.CLEANUP_COMPLETE,
                }:
                    self._verify_plan(state)
            if state.merge_mode == "stop":
                return
            self.print_checkpoint("resume", "Worktree flow", (("plan", copied_plan), ("base", state.base_branch), ("stage", state.stage.value)))
            state = self._finish(state, copied_plan)

    def resume_command_args(self) -> list[str] | None:
        if self._last_state is None:
            return None
        state = self._last_state
        args = [sys.executable, str(self.config.entrypoint_path.resolve()), "--resume", "--plan", state.plan_path, "--repo", state.repo_root, "--worktree", state.feature_worktree, "--base", state.base_branch, "--state-dir", str(self.state_store.root if self.state_store else "")]
        if state.merge_mode != "stop":
            args.extend(["--merge-mode", state.merge_mode])
        if state.implementation_model:
            args.extend(["--implementation-model", state.implementation_model])
        if state.review_model:
            args.extend(["--review-model", state.review_model])
        if state.command_timeout_seconds is not None:
            args.extend(["--command-timeout-seconds", str(state.command_timeout_seconds)])
        if state.keep_worktrees:
            args.append("--keep-worktrees")
        return args

    def resume_command(self) -> str | None:
        args = self.resume_command_args()
        return shell_command(args) if args else None

    def preview(self) -> None:
        plan = canonical_path(self.config.plan, must_exist=True)
        title = self._plan_title(plan)
        slug = derive_slug(plan)
        base = self.config.base or "main"
        kind = HarnessKind.from_executable(self.config.harness)
        implementation_selector = phase_model_for_preview(kind, "implementation", self.config)
        audit_selector = phase_model_for_preview(kind, "audit", self.config)
        feature = self.repo.parent / f"{self.repo.name}-{slug}"
        integration = self.repo.parent / f"{self.repo.name}-integrate-{slug}"
        integration_branch = f"integration/{slug}-<timestamp>"
        print(f"+ implementation: {self._preview_harness(feature, 'implementation', implementation_selector)}")
        print(f"+ audit: {self._preview_harness(feature, 'audit', audit_selector)}")
        print(f"+ git worktree add -b {integration_branch} {integration} {base}")
        print(f"+ merge ({self.config.merge_mode or 'squash'}): git merge")
        print(f"+ conflict resolution: {self._preview_harness(integration, 'conflict_resolution', implementation_selector)}")
        print(f"+ post-conflict audit: {self._preview_harness(integration, 'post_conflict_audit', audit_selector)}")
        print(f"+ integration commit: git commit -m 'Harness: {title}'")
        print(f"+ fast-forward: git merge --ff-only {integration_branch}")
        print(f"+ archive: copy allowlisted handoff artifacts to {self.repo / self.worktree_flow_dir / '<run-id>'}")
        print(f"+ cleanup: git worktree remove --force {integration}; git worktree remove --force {feature}; git worktree prune; git branch -D")

    def _preview_harness(self, cwd: Path, phase: str, selector: str | None) -> str:
        prompt = cwd / self.handoff_dir / f"{phase}-prompt.md"
        kind = HarnessKind.from_executable(self.config.harness)
        if kind is HarnessKind.OMP:
            args = [self.config.harness, "-p", "--no-session", "--auto-approve", "--approval-mode", "yolo"]
            if selector:
                args.extend(["--model", selector])
            args.append("@" + str(prompt))
        elif kind is HarnessKind.CODEX:
            args = [self.config.harness, "exec", "--cd", str(cwd), "--sandbox", "workspace-write", "-"]
            if selector:
                args[-1:-1] = ["--model", selector]
        else:
            args = [self.config.harness, "run", "--dir", str(cwd), "--dangerously-skip-permissions"]
            if selector:
                args.extend(["--model", selector])
            args.extend(["--file", str(prompt), "Execute the attached worktree-flow phase prompt."])
        return shell_command(args)


def phase_model_for_preview(kind: HarnessKind, phase: str, config: FlowConfig) -> str | None:
    if phase in {"audit", "post_conflict_audit"}:
        return config.review_model or config.model or ("@slow" if kind is HarnessKind.OMP else None)
    return config.implementation_model or config.model or ("@default" if kind is HarnessKind.OMP else None)
