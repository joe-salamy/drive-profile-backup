#!/usr/bin/env python3
"""Run a plan -> implement -> audit -> finish harness worktree workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Sequence

WORKFLOW_STATE_FILENAME = "workflow-state.json"
MAX_LOG_OUTPUT_CHARS = 20_000
DEFAULT_BASE_CANDIDATES = ("main", "master")
WORKTREE_FLOW_DIRNAME = "worktree-flow"
RUN_ID_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"
USAGE_EVENTS_FILENAME = "usage-events.jsonl"
USAGE_SUMMARY_FILENAME = "usage-summary.json"
USAGE_SOURCES_FILENAME = "usage-sources.json"
SESSION_SCAN_MAX_FILES = 2000



def decode_subprocess_output(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def truncate_log_text(text: str) -> dict[str, object]:
    original_chars = len(text)
    return {
        "text": text[:MAX_LOG_OUTPUT_CHARS],
        "truncated": original_chars > MAX_LOG_OUTPUT_CHARS,
        "original_chars": original_chars,
    }


def logged_command(args: Sequence[str]) -> list[str]:
    command = list(args)
    if command and command[-1] == "-":
        command.pop()
    return command

def shell_command(args: Sequence[str]) -> str:
    command = [str(arg) for arg in args]
    if os.name == "nt":
        return subprocess.list2cmdline(command)
    return shlex.join(command)


def wsl_drive_mount_to_windows_path(path: Path) -> str | None:
    raw = path.as_posix()
    match = re.fullmatch(r"/mnt/([A-Za-z])(?:/(.*))?", raw)
    if match is None:
        return None
    drive = match.group(1).upper()
    tail = match.group(2)
    if not tail:
        return f"{drive}:\\"
    windows_tail = tail.replace("/", "\\")
    return f"{drive}:\\{windows_tail}"


def is_windows_executable_path(executable: str | None) -> bool:
    if executable is None:
        return False
    return executable.replace("\\", "/").lower().endswith(".exe")


def positive_seconds(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def infer_default_harness_dir(script_path: Path | None = None) -> Path:
    script = script_path or Path(__file__)
    parent = script.resolve().parent.parent.name
    if parent.startswith("."):
        return Path(parent)
    return Path(".harness")


def infer_default_harness(harness_dir: Path) -> str:
    name = harness_dir.name
    return name[1:] if name.startswith(".") and len(name) > 1 else name


HARNESS_DIR = infer_default_harness_dir()
DEFAULT_HARNESS = infer_default_harness(HARNESS_DIR)
HANDOFF_DIR = HARNESS_DIR / "handoff"


class FlowError(RuntimeError):
    """A recoverable workflow error with a user-facing message."""


class CommandFailureError(FlowError):
    """A command failure that carries the structured command result."""

    def __init__(self, result: "CommandResult") -> None:
        self.result = result
        super().__init__(format_command_failure(result))


@dataclass(frozen=True)
class CommandResult:
    args: tuple[str, ...]
    cwd: Path
    returncode: int
    stdout: str = ""
    stderr: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration_ms: int | None = None
    timed_out: bool = False


@dataclass(frozen=True)
class SessionSnapshot:
    files: dict[str, int]


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



class CommandRunner:
    def __init__(
        self,
        dry_run: bool = False,
        *,
        verbose: bool = False,
        command_timeout_seconds: float | None = None,
    ) -> None:
        self.dry_run = dry_run
        self.verbose = verbose
        self.command_timeout_seconds = command_timeout_seconds

    @staticmethod
    def resolve_executable(command: str) -> str | None:
        executable = shutil.which(command)
        if executable is not None:
            return executable
        if Path(command).suffix.lower() == ".exe":
            return None
        return shutil.which(f"{command}.exe")

    @staticmethod
    def executable_not_found_message(command: str) -> str:
        if Path(command).suffix.lower() == ".exe":
            return f"Executable not found on PATH: {command}"
        return f"Executable not found on PATH: {command} (also tried {command}.exe)"

    def run(
        self,
        args: Sequence[str],
        cwd: Path,
        *,
        check: bool = True,
        capture: bool = True,
        input_text: str | None = None,
    ) -> CommandResult:
        display = " ".join(args)
        if self.verbose or self.dry_run:
            print(f"+ ({cwd}) {display}")
        started_at = now_iso()
        start = time.perf_counter()
        if self.dry_run:
            return CommandResult(
                tuple(args),
                cwd,
                0,
                started_at=started_at,
                finished_at=now_iso(),
                duration_ms=0,
            )

        executable = self.resolve_executable(args[0])
        if executable is None:
            raise FlowError(self.executable_not_found_message(args[0]))
        resolved_args = [executable, *args[1:]]
        try:
            completed = subprocess.run(
                resolved_args,
                cwd=cwd,
                check=False,
                capture_output=capture,
                text=True,
                input=input_text,
                timeout=self.command_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            result = CommandResult(
                tuple(args),
                cwd,
                -9,
                decode_subprocess_output(exc.stdout),
                decode_subprocess_output(exc.stderr),
                started_at=started_at,
                finished_at=now_iso(),
                duration_ms=int((time.perf_counter() - start) * 1000),
                timed_out=True,
            )
            if check:
                raise CommandFailureError(result) from exc
            return result
        except OSError as exc:
            raise FlowError(
                f"Failed to run command: {display}\ncwd: {cwd}\n{exc}"
            ) from exc
        result = CommandResult(
            tuple(args),
            cwd,
            completed.returncode,
            completed.stdout or "",
            completed.stderr or "",
            started_at=started_at,
            finished_at=now_iso(),
            duration_ms=int((time.perf_counter() - start) * 1000),
        )
        if check and result.returncode != 0:
            raise CommandFailureError(result)
        return result


def format_command_failure(result: CommandResult) -> str:
    status = (
        f"Command timed out: {' '.join(result.args)}"
        if result.timed_out
        else f"Command failed with exit code {result.returncode}: {' '.join(result.args)}"
    )
    parts = [
        status,
        f"cwd: {result.cwd}",
    ]
    if result.stdout.strip():
        parts.append("stdout:\n" + result.stdout.strip())
    if result.stderr.strip():
        parts.append("stderr:\n" + result.stderr.strip())
    return "\n".join(parts)


def slugify(value: str, *, max_words: int = 6, max_len: int = 60) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    words = [part for part in cleaned.split("-") if part]
    slug = "-".join(words[:max_words]) or "harness-plan"
    return slug[:max_len].strip("-") or "harness-plan"


def plan_title(plan_path: Path) -> str:
    text = plan_path.read_text(encoding="utf-8")
    for line in text.splitlines():
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if match:
            return match.group(1).strip()
    return plan_path.stem


def derive_slug(plan_path: Path) -> str:
    return slugify(plan_title(plan_path))


def timestamped_run_id(slug: str, *, stamp: str | None = None) -> str:
    timestamp = stamp or datetime.now().strftime(RUN_ID_TIMESTAMP_FORMAT)
    return f"{timestamp}-{slug}"


def slug_from_branch(branch: str) -> str:
    if branch.startswith("feature/"):
        return branch.removeprefix("feature/")
    return slugify(branch)


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_text(path: Path, text: str) -> None:
    ensure_dir(path.parent)
    path.write_text(text, encoding="utf-8", newline="\n")


@dataclass(frozen=True)
class GitWorktree:
    path: Path
    branch: str | None


@dataclass(frozen=True)
class Names:
    slug: str
    branch: str
    worktree: Path
    run_id: str


@dataclass(frozen=True)
class WorkflowState:
    run_id: str
    slug: str
    base: str
    plan_title: str
    feature_branch: str
    feature_worktree: str
    merge_mode: str
    plan_path: str | None = None
    integration_branch: str | None = None
    integration_worktree: str | None = None
    audit_head_before: str | None = None
    completed_stage: str = "feature_worktree_created"


@dataclass(frozen=True)
class FlowConfig:
    repo: Path
    plan: Path
    base: str | None
    model: str | None
    harness: str
    harness_dir: Path
    merge_mode: str
    keep_worktrees: bool
    verbose: bool = False

    command_timeout_seconds: float | None = None


class HarnessWorktreeFlow:
    def __init__(self, config: FlowConfig, runner: CommandRunner) -> None:
        self.config = config
        self.runner = runner
        self.log_file: Path | None = None
        self._last_state: WorkflowState | None = None
        self._base = config.base

    @property
    def base(self) -> str:
        if self._base is None:
            raise FlowError("Base ref has not been resolved.")
        return self._base

    @property
    def harness_dir(self) -> Path:
        return self.config.harness_dir

    @property
    def worktree_flow_dir(self) -> Path:
        return self.config.harness_dir / WORKTREE_FLOW_DIRNAME

    def run_id_from_plan(self, repo: Path, plan: Path) -> str | None:
        try:
            source_rel = plan.resolve().relative_to(repo.resolve())
        except ValueError:
            return None
        if source_rel.name != "plan.md":
            return None
        worktree_flow_dir = self.worktree_flow_dir
        if worktree_flow_dir.is_absolute():
            try:
                worktree_flow_dir = worktree_flow_dir.resolve().relative_to(repo.resolve())
            except ValueError:
                return None
        if source_rel.parent.parent == worktree_flow_dir:
            return source_rel.parent.name
        return None

    @property
    def handoff_dir(self) -> Path:
        return self.config.harness_dir / "handoff"

    @staticmethod
    def print_checkpoint(
        status: str, title: str, details: Sequence[tuple[str, object | None]] = ()
    ) -> None:
        print(f"[{status}] {title}")
        for label, value in details:
            if value is not None and value != "":
                print(f"  {label}: {value}")

    def run(self) -> None:
        repo = self.git_root(self.config.repo.resolve())
        plan = self.config.plan.resolve()
        self.validate(repo, plan)

        names = self.unique_feature_names(
            repo, derive_slug(plan), self.run_id_from_plan(repo, plan)
        )
        self.prepare_harness_permissions(repo / self.harness_dir)
        self.prepare_git_permissions(repo)
        # Keep workflow logs inside the script-created worktree. Writing them in
        # the primary checkout makes the checkout dirty before the final merge.
        self.print_checkpoint(
            "start",
            "Worktree flow",
            (
                ("plan", plan),
                ("base", self.base),
                ("merge mode", self.config.merge_mode),
            ),
        )
        self.print_checkpoint(
            "ready",
            "Feature target",
            (
                ("branch", names.branch),
                ("worktree", names.worktree),
            ),
        )

        try:
            self.create_feature_worktree(repo, names)
            self.print_checkpoint(
                "done", "Feature worktree", (("worktree", names.worktree),)
            )
            self.start_log(names.worktree, names.run_id)
            self.log_event(
                "feature_worktree_created",
                branch=names.branch,
                worktree=str(names.worktree),
            )
            state = WorkflowState(
                run_id=names.run_id,
                slug=names.slug,
                base=self.base,
                plan_title=plan_title(plan),
                feature_branch=names.branch,
                feature_worktree=str(names.worktree),
                merge_mode=self.config.merge_mode,
                completed_stage="feature_worktree_created",
            )
            self.save_workflow_state(state)
            state, plan_in_worktree = self.run_feature_phases(repo, plan, state, names)
            if self.config.merge_mode == "stop":
                state = self.stop_before_merge(repo, state, names, plan_in_worktree)
                return

            self.require_ready_for_integration(names.worktree, names.branch)
            self.finish(repo, state, names, plan_in_worktree)
        except CommandFailureError as exc:
            self.log_command_result(
                "command_failure",
                exc.result,
                phase="workflow",
                step="checked_command",
            )
            raise

    def workflow_state_file(self, worktree: Path) -> Path:
        return worktree / self.handoff_dir / WORKFLOW_STATE_FILENAME

    def read_workflow_state_file(self, path: Path) -> WorkflowState | None:
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return WorkflowState(**data)
        except (json.JSONDecodeError, TypeError) as exc:
            raise FlowError(f"Invalid workflow state file: {path}") from exc

    def save_workflow_state_file(self, path: Path, state: WorkflowState) -> None:
        self.ensure_dir(path.parent)
        self._last_state = state
        self.write_text(
            path, json.dumps(asdict(state), indent=2, sort_keys=True) + "\n"
        )

    def save_workflow_state(
        self, state: WorkflowState, *, worktree: Path | None = None
    ) -> None:
        target_worktree = worktree or Path(state.feature_worktree)
        self.save_workflow_state_file(self.workflow_state_file(target_worktree), state)

    def load_workflow_state(self, worktree: Path) -> WorkflowState | None:
        state = self.read_workflow_state_file(self.workflow_state_file(worktree))
        self._last_state = state
        return state

    def resume_command_args(self) -> list[str] | None:
        state = self._last_state
        if state is None:
            return None
        args = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--resume",
            "--plan",
            str(self.resume_plan_path(state)),
            "--worktree",
            state.feature_worktree,
            "--repo",
            str(self.config.repo),
            "--base",
            state.base,
            "--branch",
            state.feature_branch,
            "--run-id",
            state.run_id,
            "--harness",
            self.config.harness,
            "--harness-dir",
            str(self.harness_dir),
            "--merge-mode",
            state.merge_mode,
        ]
        if self.config.model:
            args.extend(["--model", self.config.model])
        if self.config.keep_worktrees:
            args.append("--keep-worktrees")
        if self.config.command_timeout_seconds is not None:
            args.extend(
                ["--command-timeout-seconds", str(self.config.command_timeout_seconds)]
            )
        if state.integration_worktree is not None:
            args.extend(["--integration-worktree", state.integration_worktree])
        if state.integration_branch is not None:
            args.extend(["--integration-branch", state.integration_branch])
        return args

    def resume_command(self) -> str | None:
        args = self.resume_command_args()
        if args is None:
            return None
        return shell_command(args)

    def resume_plan_path(self, state: WorkflowState) -> Path:
        if state.plan_path is not None:
            saved_plan = Path(state.plan_path)
            if saved_plan.exists():
                return saved_plan
        return self.config.plan

    def recover_resume_plan(self, requested_plan: Path, worktree: Path) -> Path:
        if requested_plan.exists():
            return requested_plan
        state = self.read_workflow_state_file(self.workflow_state_file(worktree))
        if state is not None:
            candidates = [
                Path(state.plan_path) if state.plan_path is not None else None,
                worktree / self.worktree_flow_dir / state.run_id / "plan.md",
                worktree / self.handoff_dir / "resume-plan.md",
            ]
            for candidate in candidates:
                if candidate is not None and candidate.exists():
                    self.print_checkpoint(
                        "recover",
                        "Resume plan",
                        (("missing plan", requested_plan), ("saved plan", candidate)),
                    )
                    return candidate.resolve()
        raise FlowError(f"Plan file does not exist: {requested_plan}")

    def update_workflow_state(
        self, state: WorkflowState, **changes: object
    ) -> WorkflowState:
        updated = replace(state, **changes)
        self.save_workflow_state(updated)
        self.log_event(
            "workflow_state_updated", completed_stage=updated.completed_stage
        )
        return updated

    def validate(self, repo: Path, plan: Path) -> None:
        if not plan.exists():
            raise FlowError(f"Plan file does not exist: {plan}")
        self.runner.run(["git", "fetch", "--all", "--prune"], repo)
        self._base = self.resolve_base(repo)
        self.runner.run(self.harness_validation_command(), repo)

    def resolve_base(self, repo: Path) -> str:
        if self._base:
            if self.ref_exists(repo, self._base):
                return self._base
            raise FlowError(
                f"Base ref does not exist: {self._base}. Pass --base <branch> "
                "or create the branch before running the workflow."
            )

        for candidate in DEFAULT_BASE_CANDIDATES:
            if self.ref_exists(repo, candidate):
                return candidate

        current = self.current_branch(repo)
        if current:
            return current

        raise FlowError(
            "Could not infer a base branch. Pass --base <branch> explicitly."
        )

    def ref_exists(self, repo: Path, ref: str) -> bool:
        return (
            self.runner.run(
                ["git", "rev-parse", "--verify", "--quiet", ref],
                repo,
                check=False,
            ).returncode
            == 0
        )

    def current_branch(self, repo: Path) -> str:
        result = self.runner.run(
            ["git", "branch", "--show-current"],
            repo,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    def git_root(self, start: Path) -> Path:
        result = self.runner.run(["git", "rev-parse", "--show-toplevel"], start)
        root = result.stdout.strip()
        return Path(root).resolve() if root else start

    def git_worktrees(self, repo: Path) -> list[GitWorktree]:
        result = self.runner.run(["git", "worktree", "list", "--porcelain"], repo)
        entries: list[GitWorktree] = []
        path: Path | None = None
        branch: str | None = None

        def add_entry() -> None:
            nonlocal path, branch
            if path is not None:
                entries.append(GitWorktree(path.resolve(), branch))
            path = None
            branch = None

        for line in result.stdout.splitlines():
            if not line:
                add_entry()
                continue
            key, _, value = line.partition(" ")
            if key == "worktree":
                add_entry()
                path = Path(value).expanduser()
            elif key == "branch":
                branch = value.removeprefix("refs/heads/")
        add_entry()
        return entries

    def matching_resume_worktrees(
        self, worktrees: Sequence[GitWorktree], run_id: str
    ) -> list[Path]:
        matches: list[Path] = []
        for worktree in worktrees:
            state = self.read_workflow_state_file(
                self.workflow_state_file(worktree.path)
            )
            if state is not None and state.run_id == run_id:
                matches.append(worktree.path)
        return matches

    def infer_resume_worktree(self, repo: Path, plan: Path) -> Path:
        worktrees = self.git_worktrees(repo)
        run_id = self.run_id_from_plan(repo, plan)
        if run_id is not None:
            matches = self.matching_resume_worktrees(worktrees, run_id)
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise FlowError(
                    f"Multiple worktrees have workflow state for run id {run_id}; "
                    "pass --worktree explicitly."
                )

        slug = derive_slug(plan)
        expected = (repo.parent / f"{repo.name}-{slug}").resolve()
        expected_matches = [
            worktree.path for worktree in worktrees if worktree.path == expected
        ]
        if len(expected_matches) == 1:
            return expected_matches[0]

        branch = f"feature/{slug}"
        branch_matches = [
            worktree.path for worktree in worktrees if worktree.branch == branch
        ]
        if len(branch_matches) == 1:
            return branch_matches[0]
        if len(branch_matches) > 1:
            raise FlowError(
                f"Multiple worktrees use branch {branch}; pass --worktree explicitly."
            )

        raise FlowError(
            "Could not infer the feature worktree for --resume; pass --worktree."
        )

    def unique_feature_names(
        self, repo: Path, slug: str, run_id: str | None = None
    ) -> Names:
        repo_name = repo.name
        parent = repo.parent
        suffix = 1
        stamp = datetime.now().strftime(RUN_ID_TIMESTAMP_FORMAT)
        while True:
            candidate_slug = slug if suffix == 1 else f"{slug}-{suffix}"
            branch = f"feature/{candidate_slug}"
            worktree = parent / f"{repo_name}-{candidate_slug}"
            branch_exists = self.runner.run(
                ["git", "branch", "--list", branch], repo
            ).stdout.strip()
            if not branch_exists and not worktree.exists():
                candidate_run_id = run_id or timestamped_run_id(
                    candidate_slug, stamp=stamp
                )
                return Names(candidate_slug, branch, worktree, candidate_run_id)
            suffix += 1

    def create_feature_worktree(self, repo: Path, names: Names) -> None:
        self.runner.run(
            [
                "git",
                "worktree",
                "add",
                str(names.worktree),
                "-b",
                names.branch,
                self.base,
            ],
            repo,
        )
        self.prepare_new_worktree(names.worktree)

    def prepare_existing_worktree(self, worktree: Path) -> None:
        self.prepare_harness_permissions(worktree / self.harness_dir)
        self.ensure_dir(worktree / self.handoff_dir)
        self.prepare_git_permissions(worktree)

    def prepare_new_worktree(self, worktree: Path) -> None:
        self.prepare_harness_permissions(worktree / self.harness_dir)
        self.ensure_dir(worktree / self.handoff_dir)
        self.prepare_harness_permissions(worktree / self.harness_dir)
        self.prepare_git_permissions(worktree)

    def ensure_plan_in_worktree(
        self, repo: Path, plan: Path, worktree: Path, names: Names
    ) -> Path:
        rel = self.worktree_flow_dir / names.run_id / "plan.md"
        if is_relative_to(plan, repo):
            source_rel = plan.relative_to(repo)
            if source_rel == rel:
                target = worktree / rel
                if target.exists():
                    return target
        target = worktree / rel

        self.ensure_dir(target.parent)
        self.copy_file(plan, target)
        return target

    def run_id_from_handoff(self, worktree: Path, fallback_slug: str) -> str:
        log_file = worktree / self.handoff_dir / "workflow.jsonl"
        if log_file.exists():
            for line in log_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                run_id = record.get("run_id")
                if isinstance(run_id, str) and run_id:
                    return run_id
        return timestamped_run_id(fallback_slug)

    def tracked_path_exists(self, worktree: Path, path: Path) -> bool:
        try:
            rel = path.relative_to(worktree)
        except ValueError:
            return False
        result = self.runner.run(
            ["git", "ls-files", "--error-unmatch", "--", rel.as_posix()],
            worktree,
            check=False,
        )
        return result.returncode == 0

    def remove_untracked_workflow_plan(
        self, worktree: Path, slug: str, run_id: str | None = None
    ) -> None:
        candidates = [run_id] if run_id else []
        candidates.append(slug)
        workflow_plans = tuple(
            worktree / self.worktree_flow_dir / candidate / "plan.md"
            for candidate in dict.fromkeys(candidates)
        ) + (
            worktree / self.worktree_flow_dir / slug / ".plan.md",
            worktree / self.worktree_flow_dir / f"{slug}.md",
        )
        for workflow_plan in workflow_plans:
            if workflow_plan.exists() and not self.tracked_path_exists(
                worktree, workflow_plan
            ):
                workflow_plan.unlink()

    def copy_plan_to_handoff(self, plan: Path, worktree: Path) -> Path:
        target = worktree / self.handoff_dir / "resume-plan.md"
        self.copy_file(plan, target)
        return target

    def resume(
        self,
        *,
        repo: Path,
        plan: Path,
        worktree: Path,
        branch: str | None,
        run_id: str | None,
        integration_worktree: Path | None,
        integration_branch: str | None,
    ) -> None:
        self.runner.run(["git", "worktree", "repair", str(worktree)], repo, check=False)
        self.prepare_harness_permissions(worktree / self.harness_dir)
        self.prepare_git_permissions(worktree)

        state = self.load_workflow_state(worktree)
        if state is None:
            resolved_branch = branch or self.current_branch(worktree)
            if not resolved_branch:
                raise FlowError(
                    "Could not infer the feature branch from the worktree; pass --branch."
                )
            slug = slug_from_branch(resolved_branch)
            state = WorkflowState(
                run_id=run_id or self.run_id_from_handoff(worktree, slug),
                slug=slug,
                base=self.base,
                plan_title=plan_title(plan),
                feature_branch=resolved_branch,
                feature_worktree=str(worktree),
                merge_mode=self.config.merge_mode,
                integration_branch=integration_branch,
                integration_worktree=(
                    str(integration_worktree) if integration_worktree else None
                ),
                completed_stage="legacy_state_inferred",
            )
            self.save_workflow_state(state, worktree=worktree)
        else:
            changes: dict[str, object] = {}
            if state.integration_worktree is None and integration_worktree is not None:
                changes["integration_worktree"] = str(integration_worktree)
            if state.integration_branch is None and integration_branch is not None:
                changes["integration_branch"] = integration_branch
            if changes:
                state = self.update_workflow_state(state, **changes)
            if not self.ref_exists(repo, state.base):
                raise FlowError(
                    f"Base ref from workflow state does not exist: {state.base}."
                )
            self._base = state.base

        self.continue_log(worktree, state.run_id)
        names = Names(
            state.slug,
            state.feature_branch,
            Path(state.feature_worktree),
            state.run_id,
        )
        self.print_checkpoint(
            "resume",
            "Worktree flow",
            (
                ("plan", plan),
                ("base", self.base),
                ("branch", names.branch),
                ("worktree", names.worktree),
                ("merge mode", state.merge_mode),
            ),
        )
        state, plan_in_worktree = self.run_feature_phases(repo, plan, state, names)
        if state.merge_mode == "stop":
            state = self.stop_before_merge(repo, state, names, plan_in_worktree)
            return

        self.require_ready_for_integration(names.worktree, names.branch)
        self.finish(repo, state, names, plan_in_worktree)

    def stop_before_merge(
        self, repo: Path, state: WorkflowState, names: Names, plan_in_worktree: Path
    ) -> WorkflowState:
        archive_dir = self.archive_handoff(repo, names.worktree, names.run_id)
        self.archive_plan(archive_dir, plan_in_worktree)
        state = self.update_workflow_state(
            state, completed_stage="stopped_before_merge"
        )
        self.print_checkpoint(
            "stop",
            "Stopped before merge",
            (
                ("handoff archive", archive_dir),
                ("plan", plan_in_worktree),
                ("worktree", names.worktree),
                ("branch", names.branch),
            ),
        )
        return state

    def run_feature_phases(
        self, repo: Path, plan: Path, state: WorkflowState, names: Names
    ) -> tuple[WorkflowState, Path]:
        state, plan_in_worktree = self.ensure_plan_stage(repo, plan, state, names)
        state = self.ensure_implementation_complete(state, names, plan_in_worktree)
        state = self.ensure_audit_complete(state, names, plan_in_worktree)
        return state, plan_in_worktree

    def ensure_plan_stage(
        self, repo: Path, plan: Path, state: WorkflowState, names: Names
    ) -> tuple[WorkflowState, Path]:
        if state.plan_path and Path(state.plan_path).exists():
            plan_in_worktree = Path(state.plan_path)
            self.print_checkpoint(
                "skip", "Plan staging", (("plan", plan_in_worktree),)
            )
        elif state.completed_stage == "legacy_state_inferred":
            plan_in_worktree = self.copy_plan_to_handoff(plan, names.worktree)
            self.remove_untracked_workflow_plan(
                names.worktree, names.slug, names.run_id
            )
            state = self.update_workflow_state(
                state,
                plan_path=str(plan_in_worktree),
                completed_stage="plan_copied",
            )
            self.print_checkpoint(
                "done", "Plan staging", (("plan", plan_in_worktree),)
            )
        else:
            plan_in_worktree = self.ensure_plan_in_worktree(
                repo, plan, names.worktree, names
            )
            state = self.update_workflow_state(
                state,
                plan_path=str(plan_in_worktree),
                completed_stage="plan_copied",
            )
            self.print_checkpoint(
                "done", "Plan staging", (("plan", plan_in_worktree),)
            )
        return state, plan_in_worktree

    def ensure_implementation_complete(
        self, state: WorkflowState, names: Names, plan_in_worktree: Path
    ) -> WorkflowState:
        implementation_summary = (
            names.worktree / self.handoff_dir / "implementation-summary.md"
        )
        if not implementation_summary.exists():
            self.print_checkpoint(
                "start",
                "Implementation",
                (("worktree", names.worktree), ("plan", plan_in_worktree)),
            )
            self.run_implementation(names.worktree, plan_in_worktree)
            self.require_file(implementation_summary)
            self.require_no_tracked_handoff_artifacts(names.worktree, names.branch)
            self.require_implementation_invariants(names.worktree, names.branch)
            state = self.update_workflow_state(
                state, completed_stage="implementation_complete"
            )
            self.print_checkpoint(
                "done", "Implementation", (("summary", implementation_summary),)
            )
        else:
            self.require_commits_since_base(
                names.worktree, names.branch, "Implementation"
            )
            self.require_branch_changed_since_base(names.worktree, names.branch)
            self.print_checkpoint(
                "skip", "Implementation", (("summary", implementation_summary),)
            )
        return state

    def ensure_audit_complete(
        self, state: WorkflowState, names: Names, plan_in_worktree: Path
    ) -> WorkflowState:
        audit_summary = names.worktree / self.handoff_dir / "audit-summary.md"
        if not audit_summary.exists():
            self.print_checkpoint(
                "start",
                "Audit",
                (("worktree", names.worktree), ("plan", plan_in_worktree)),
            )
            audit_head_before = self.head_rev(names.worktree)
            state = self.update_workflow_state(
                state, audit_head_before=audit_head_before
            )
            self.run_audit(names.worktree, plan_in_worktree)
            self.require_file(audit_summary)
            self.require_no_tracked_handoff_artifacts(names.worktree, names.branch)
            self.require_audit_invariants(
                names.worktree, names.branch, audit_head_before
            )
            state = self.update_workflow_state(state, completed_stage="audit_complete")
            self.print_checkpoint("done", "Audit", (("summary", audit_summary),))
        else:
            self.require_no_tracked_handoff_artifacts(names.worktree, names.branch)
            self.require_clean_except_handoff(names.worktree, "Audit")
            self.require_branch_changed_since_base(names.worktree, names.branch)
            self.print_checkpoint("skip", "Audit", (("summary", audit_summary),))
        return state

    def usage_events_file(self, worktree: Path) -> Path:
        return worktree / self.handoff_dir / USAGE_EVENTS_FILENAME

    def usage_summary_file(self, worktree: Path) -> Path:
        return worktree / self.handoff_dir / USAGE_SUMMARY_FILENAME

    def usage_sources_file(self, worktree: Path) -> Path:
        return worktree / self.handoff_dir / USAGE_SOURCES_FILENAME

    @staticmethod
    def wsl_windows_home_from_repo(repo: Path) -> Path | None:
        match = re.match(r"^/mnt/([A-Za-z])/Users/([^/]+)(?:/|$)", repo.as_posix())
        if not match:
            return None
        drive, user = match.groups()
        return Path("/mnt") / drive.lower() / "Users" / user

    def omp_sessions_roots(self, repo: Path) -> list[Path]:
        roots = [Path.home() / ".omp" / "agent" / "sessions"]
        wsl_home = self.wsl_windows_home_from_repo(repo.resolve())
        if wsl_home is not None:
            roots.append(wsl_home / ".omp" / "agent" / "sessions")
        return [root for root in dict.fromkeys(roots) if root.exists()]

    def snapshot_omp_sessions(self, repo: Path) -> SessionSnapshot:
        files = self.newest_session_files(repo)
        return SessionSnapshot(
            {str(path.resolve()): self.safe_mtime_ns(path) for path in files}
        )

    def changed_session_files(self, repo: Path, snapshot: SessionSnapshot) -> list[Path]:
        changed: list[tuple[int, Path]] = []
        for path in self.newest_session_files(repo):
            try:
                resolved = str(path.resolve())
            except OSError:
                continue
            mtime_ns = self.safe_mtime_ns(path)
            previous = snapshot.files.get(resolved)
            if previous is None or mtime_ns > previous:
                changed.append((mtime_ns, path))
        changed.sort(key=lambda item: item[0], reverse=True)
        return [path for _mtime, path in changed[:SESSION_SCAN_MAX_FILES]]

    def newest_session_files(self, repo: Path) -> list[Path]:
        files: list[tuple[int, Path]] = []
        for root in self.omp_sessions_roots(repo):
            try:
                candidates = root.rglob("*.jsonl")
                for path in candidates:
                    if not path.is_file():
                        continue
                    files.append((self.safe_mtime_ns(path), path))
            except OSError:
                continue
        files.sort(key=lambda item: item[0], reverse=True)
        return [path for _mtime, path in files[:SESSION_SCAN_MAX_FILES]]

    @staticmethod
    def safe_mtime_ns(path: Path) -> int:
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return 0

    def session_cwd(self, session_file: Path) -> str | None:
        for record in self.read_jsonl_records(session_file):
            if record.get("type") not in {"session", "session_init"}:
                continue
            cwd = self.string_at(record, ("cwd",))
            if cwd is None:
                cwd = self.string_at(record, ("data", "cwd"))
            if cwd is not None:
                return cwd
        return None

    def path_matches_worktree(self, raw_cwd: str, worktree: Path) -> bool:
        raw = raw_cwd.replace("\\", "/").rstrip("/")
        candidates = {raw}
        try:
            candidates.add(Path(raw_cwd).expanduser().resolve().as_posix().rstrip("/"))
        except (OSError, RuntimeError):
            pass
        try:
            resolved_worktree = worktree.resolve()
        except (OSError, RuntimeError):
            resolved_worktree = worktree
        worktree_strings = {
            worktree.as_posix().rstrip("/"),
            resolved_worktree.as_posix().rstrip("/"),
        }
        windows_worktree = wsl_drive_mount_to_windows_path(resolved_worktree)
        if windows_worktree is not None:
            worktree_strings.add(windows_worktree.replace("\\", "/").rstrip("/"))
        normalized_candidates = {self.casefold_path_text(value) for value in candidates}
        normalized_worktrees = {
            self.casefold_path_text(value) for value in worktree_strings
        }
        return bool(normalized_candidates & normalized_worktrees)

    @staticmethod
    def casefold_path_text(value: str) -> str:
        return value.lower() if re.match(r"^[A-Za-z]:/", value) else value

    def collect_phase_usage(
        self,
        repo: Path,
        worktree: Path,
        phase: str,
        snapshot: SessionSnapshot,
        command_result: CommandResult,
    ) -> dict[str, object]:
        event = self.base_usage_event(phase, worktree, command_result)
        if not self.is_omp_harness():
            event["status"] = "unavailable"
            event["reason"] = "non_omp_harness"
            return event

        selected = [
            path
            for path in self.changed_session_files(repo, snapshot)
            if (cwd := self.session_cwd(path)) is not None
            and self.path_matches_worktree(cwd, worktree)
        ]
        if not selected:
            event["status"] = "unavailable"
            event["reason"] = "no_matching_session_files"
            return event

        aggregate = self.empty_usage_aggregate()
        sources: list[dict[str, object]] = []
        for index, session_file in enumerate(selected, start=1):
            source_id = f"session-{index}"
            source_data = self.collect_session_usage(session_file, source_id)
            self.merge_usage_aggregate(aggregate, source_data["aggregate"])
            sources.append(asdict(source_data["source"]))

        event.update(
            {
                "status": "collected",
                "totals": self.compact_mapping(aggregate["totals"]),
                "nested_response_usage": self.compact_mapping(
                    aggregate["nested_response_usage"]
                ),
                "models": aggregate["models"],
                "tools": aggregate["tools"],
                "context": self.compact_mapping(aggregate["context"]),
                "timings": self.compact_mapping(aggregate["timings"]),
                "event_counts": aggregate["event_counts"],
                "sources": sources,
            }
        )
        return event

    def base_usage_event(
        self, phase: str, worktree: Path, command_result: CommandResult
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "timestamp": now_iso(),
            "run_id": self.current_run_id(worktree),
            "harness": self.config.harness,
            "harness_dir": self.harness_dir.as_posix(),
            "phase": phase,
            "status": "collected",
            "command_returncode": command_result.returncode,
            "command_timed_out": command_result.timed_out,
            "command_duration_ms": command_result.duration_ms,
            "command_started_at": command_result.started_at,
            "command_finished_at": command_result.finished_at,
            "totals": {},
            "nested_response_usage": {},
            "models": {},
            "tools": {},
            "context": {},
            "timings": {},
            "event_counts": {},
            "sources": [],
        }

    def current_run_id(self, worktree: Path) -> str | None:
        if self._last_state is not None:
            return self._last_state.run_id
        log_file = self.workflow_log_file(worktree)
        if not log_file.exists():
            return None
        for line in log_file.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            run_id = record.get("run_id")
            if isinstance(run_id, str) and run_id:
                return run_id
        return None

    def collect_session_usage(
        self, session_file: Path, source_id: str
    ) -> dict[str, object]:
        aggregate = self.empty_usage_aggregate()
        event_counts: dict[str, int] = {}
        record_ids: list[str] = []
        session_id: str | None = None
        records_read = 0
        for records_read, record in enumerate(
            self.read_jsonl_records(session_file), start=1
        ):
            contributed = False
            session_id = session_id or self.session_id_from_record(record)
            for key in ("type", "customType"):
                value = record.get(key)
                if isinstance(value, str) and value:
                    event_counts[value] = event_counts.get(value, 0) + 1
                    aggregate["event_counts"][value] = (
                        aggregate["event_counts"].get(value, 0) + 1
                    )

            if self.collect_config_record(record, aggregate):
                contributed = True
            if self.collect_message_record(record, aggregate):
                contributed = True
            if self.collect_tool_start_record(record, aggregate):
                contributed = True

            if contributed and len(record_ids) < 50:
                record_id = self.safe_record_id(record)
                if record_id is not None:
                    record_ids.append(record_id)

        path_hash = hashlib.sha256(str(session_file.resolve()).encode()).hexdigest()[:16]
        return {
            "aggregate": aggregate,
            "source": UsageSource(
                source_id=source_id,
                session_id=session_id,
                file_name=session_file.name,
                path_hash=path_hash,
                records_read=records_read,
                event_counts=event_counts,
                record_ids=record_ids,
            ),
        }

    def read_jsonl_records(self, path: Path) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return records
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
        return records

    @staticmethod
    def empty_usage_aggregate() -> dict[str, dict[str, object]]:
        return {
            "totals": asdict(UsageTotals()),
            "nested_response_usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            },
            "models": {},
            "tools": {},
            "context": {
                "max_prompt_tokens": 0,
                "last_prompt_tokens": 0,
                "max_non_message_tokens": 0,
                "last_non_message_tokens": 0,
            },
            "timings": {
                "message_duration_ms": 0,
                "message_ttft_ms": 0,
                "message_count": 0,
            },
            "event_counts": {},
        }

    def collect_config_record(
        self, record: dict[str, object], aggregate: dict[str, dict[str, object]]
    ) -> bool:
        contributed = False
        model = self.string_at(record, ("model_change", "model")) or self.string_at(
            record, ("data", "model")
        )
        if record.get("type") == "model_change" and model is not None:
            self.increment_nested(aggregate["models"], model, "config_selections", 1)
            contributed = True
        thinking_level = self.string_at(
            record, ("thinking_level_change", "thinkingLevel")
        ) or self.string_at(record, ("data", "thinkingLevel"))
        if record.get("type") == "thinking_level_change" and thinking_level is not None:
            self.increment_nested(
                aggregate["models"], thinking_level, "thinking_level_selections", 1
            )
            contributed = True
        service_tier = self.string_at(
            record, ("service_tier_change", "serviceTier")
        ) or self.string_at(record, ("data", "serviceTier"))
        if record.get("type") == "service_tier_change" and service_tier is not None:
            self.increment_nested(
                aggregate["models"], service_tier, "service_tier_selections", 1
            )
            contributed = True
        return contributed

    def collect_message_record(
        self, record: dict[str, object], aggregate: dict[str, dict[str, object]]
    ) -> bool:
        message = record.get("message")
        if not isinstance(message, dict):
            return False
        contributed = False
        if self.collect_assistant_usage(message, aggregate):
            contributed = True
        if self.collect_context_snapshot(message, aggregate):
            contributed = True
        if self.collect_nested_response_usage(message, aggregate):
            contributed = True
        if self.collect_tool_result_usage(message, aggregate):
            contributed = True
        return contributed

    def collect_assistant_usage(
        self, message: dict[str, object], aggregate: dict[str, dict[str, object]]
    ) -> bool:
        if message.get("role") != "assistant":
            return False
        contributed = False
        usage = message.get("usage")
        if isinstance(usage, dict):
            mappings = {
                "input": "input_tokens",
                "output": "output_tokens",
                "cacheRead": "cache_read_tokens",
                "cacheWrite": "cache_write_tokens",
                "reasoningTokens": "reasoning_tokens",
                "totalTokens": "total_tokens",
            }
            for source_key, dest_key in mappings.items():
                value = self.numeric_at(usage, (source_key,))
                if value is not None:
                    aggregate["totals"][dest_key] += int(value)
                    contributed = True
            cost = usage.get("cost")
            if isinstance(cost, dict):
                cost_mappings = {
                    "input": "cost_input",
                    "output": "cost_output",
                    "cacheRead": "cost_cache_read",
                    "cacheWrite": "cost_cache_write",
                    "total": "cost_total",
                }
                for source_key, dest_key in cost_mappings.items():
                    value = self.numeric_at(cost, (source_key,))
                    if value is not None:
                        aggregate["totals"][dest_key] += float(value)
                        contributed = True
        model = self.string_at(message, ("model",))
        if model is not None:
            self.increment_nested(aggregate["models"], model, "assistant_messages", 1)
            for key, dest in (
                ("provider", "providers"),
                ("api", "apis"),
                ("stopReason", "stop_reasons"),
            ):
                value = self.string_at(message, (key,))
                if value is not None:
                    self.increment_nested(
                        aggregate["models"][model].setdefault(dest, {}), value, "count", 1
                    )
            contributed = True
        duration = self.numeric_at(message, ("duration",))
        if duration is not None:
            aggregate["timings"]["message_duration_ms"] += int(float(duration) * 1000)
            aggregate["timings"]["message_count"] += 1
            contributed = True
        ttft = self.numeric_at(message, ("ttft",))
        if ttft is not None:
            aggregate["timings"]["message_ttft_ms"] += int(float(ttft) * 1000)
            contributed = True
        return contributed

    def collect_context_snapshot(
        self, message: dict[str, object], aggregate: dict[str, dict[str, object]]
    ) -> bool:
        snapshot = message.get("contextSnapshot")
        if not isinstance(snapshot, dict):
            return False
        contributed = False
        prompt_tokens = self.numeric_at(snapshot, ("promptTokens",))
        if prompt_tokens is not None:
            value = int(prompt_tokens)
            aggregate["context"]["last_prompt_tokens"] = value
            aggregate["context"]["max_prompt_tokens"] = max(
                int(aggregate["context"]["max_prompt_tokens"]), value
            )
            contributed = True
        non_message_tokens = self.numeric_at(snapshot, ("nonMessageTokens",))
        if non_message_tokens is not None:
            value = int(non_message_tokens)
            aggregate["context"]["last_non_message_tokens"] = value
            aggregate["context"]["max_non_message_tokens"] = max(
                int(aggregate["context"]["max_non_message_tokens"]), value
            )
            contributed = True
        return contributed

    def collect_nested_response_usage(
        self, message: dict[str, object], aggregate: dict[str, dict[str, object]]
    ) -> bool:
        usage = self.mapping_at(message, ("details", "response", "usage"))
        if usage is None:
            return False
        mappings = {
            "inputTokens": "input_tokens",
            "outputTokens": "output_tokens",
            "totalTokens": "total_tokens",
        }
        contributed = False
        for source_key, dest_key in mappings.items():
            value = self.numeric_at(usage, (source_key,))
            if value is not None:
                aggregate["nested_response_usage"][dest_key] += int(value)
                contributed = True
        return contributed

    def collect_tool_start_record(
        self, record: dict[str, object], aggregate: dict[str, dict[str, object]]
    ) -> bool:
        if record.get("customType") != "tool_execution_start":
            return False
        tool_name = self.string_at(record, ("data", "toolName"))
        if tool_name is None:
            return False
        tool = self.tool_bucket(aggregate["tools"], tool_name)
        tool["calls"] += 1
        return True

    def collect_tool_result_usage(
        self, message: dict[str, object], aggregate: dict[str, dict[str, object]]
    ) -> bool:
        if message.get("role") != "toolResult":
            return False
        tool_name = self.string_at(message, ("toolName",))
        if tool_name is None:
            return False
        tool = self.tool_bucket(aggregate["tools"], tool_name)
        tool["results"] += 1
        if message.get("isError") is True:
            tool["errors"] += 1
        details = message.get("details")
        if isinstance(details, dict):
            for source_key, dest_key in (
                ("wallTimeMs", "wall_time_ms"),
                ("exitCode", "exit_code_total"),
                ("timeoutSeconds", "timeout_seconds"),
                ("fileCount", "file_count"),
                ("matchCount", "match_count"),
            ):
                value = self.numeric_at(details, (source_key,))
                if value is not None:
                    tool[dest_key] = tool.get(dest_key, 0) + int(value)
            for source_key, dest_key in (
                ("fileLimitReached", "file_limit_reached"),
                ("resultLimitReached", "result_limit_reached"),
            ):
                if details.get(source_key) is True:
                    tool[dest_key] = tool.get(dest_key, 0) + 1
            for key, value in details.items():
                if not isinstance(value, int | float) or isinstance(value, bool):
                    continue
                normalized = self.safe_metric_name(key)
                if "trunc" in normalized and (
                    "byte" in normalized or "line" in normalized
                ):
                    tool[normalized] = tool.get(normalized, 0) + int(value)
        return True

    @staticmethod
    def tool_bucket(tools: dict[str, object], tool_name: str) -> dict[str, int]:
        bucket = tools.setdefault(
            tool_name, {"calls": 0, "results": 0, "errors": 0}
        )
        if not isinstance(bucket, dict):
            raise FlowError(f"Invalid tool bucket for {tool_name}")
        return bucket

    @staticmethod
    def safe_metric_name(value: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
        return normalized or "metric"

    def merge_usage_aggregate(
        self, target: dict[str, dict[str, object]], source: dict[str, dict[str, object]]
    ) -> None:
        for key in ("totals", "nested_response_usage", "timings", "event_counts"):
            self.merge_numeric_map(target[key], source[key])
        self.merge_context(target["context"], source["context"])
        self.merge_models(target["models"], source["models"])
        self.merge_tools(target["tools"], source["tools"])

    def merge_context(self, target: dict[str, object], source: dict[str, object]) -> None:
        for key in ("max_prompt_tokens", "max_non_message_tokens"):
            target[key] = max(int(target.get(key, 0)), int(source.get(key, 0)))
        for key in ("last_prompt_tokens", "last_non_message_tokens"):
            value = int(source.get(key, 0))
            if value:
                target[key] = value

    def merge_models(self, target: dict[str, object], source: dict[str, object]) -> None:
        for model, source_stats in source.items():
            if not isinstance(source_stats, dict):
                continue
            target_stats = target.setdefault(model, {})
            if not isinstance(target_stats, dict):
                continue
            for key, value in source_stats.items():
                if isinstance(value, int | float) and not isinstance(value, bool):
                    target_stats[key] = target_stats.get(key, 0) + value
                elif isinstance(value, dict):
                    nested = target_stats.setdefault(key, {})
                    if isinstance(nested, dict):
                        for nested_key, nested_value in value.items():
                            if isinstance(nested_value, dict):
                                amount = nested_value.get("count", 0)
                            else:
                                amount = nested_value
                            if isinstance(amount, int | float) and not isinstance(
                                amount, bool
                            ):
                                current = nested.setdefault(nested_key, {"count": 0})
                                if isinstance(current, dict):
                                    current["count"] = current.get("count", 0) + amount

    def merge_tools(self, target: dict[str, object], source: dict[str, object]) -> None:
        for tool_name, source_stats in source.items():
            if not isinstance(source_stats, dict):
                continue
            target_stats = self.tool_bucket(target, tool_name)
            self.merge_numeric_map(target_stats, source_stats)

    @staticmethod
    def merge_numeric_map(target: dict[str, object], source: dict[str, object]) -> None:
        for key, value in source.items():
            if isinstance(value, int | float) and not isinstance(value, bool):
                target[key] = target.get(key, 0) + value

    @staticmethod
    def increment_nested(
        target: dict[str, object], key: str, metric: str, amount: int
    ) -> None:
        bucket = target.setdefault(key, {})
        if isinstance(bucket, dict):
            bucket[metric] = bucket.get(metric, 0) + amount

    @staticmethod
    def numeric_at(data: dict[str, object], path: Sequence[str]) -> int | float | None:
        value: object = data
        for key in path:
            if not isinstance(value, dict):
                return None
            value = value.get(key)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        return value

    @staticmethod
    def string_at(data: dict[str, object], path: Sequence[str]) -> str | None:
        value: object = data
        for key in path:
            if not isinstance(value, dict):
                return None
            value = value.get(key)
        return value if isinstance(value, str) and value else None

    @staticmethod
    def mapping_at(
        data: dict[str, object], path: Sequence[str]
    ) -> dict[str, object] | None:
        value: object = data
        for key in path:
            if not isinstance(value, dict):
                return None
            value = value.get(key)
        return value if isinstance(value, dict) else None

    @staticmethod
    def session_id_from_record(record: dict[str, object]) -> str | None:
        for key in ("session_id", "sessionId", "sessionID", "id"):
            value = record.get(key)
            if isinstance(value, str) and value:
                return value
        data = record.get("data")
        if isinstance(data, dict):
            for key in ("session_id", "sessionId", "sessionID", "id"):
                value = data.get(key)
                if isinstance(value, str) and value:
                    return value
        return None

    @staticmethod
    def safe_record_id(record: dict[str, object]) -> str | None:
        for key in ("id", "messageId", "recordId"):
            value = record.get(key)
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value):
                return value
        message = record.get("message")
        if isinstance(message, dict):
            value = message.get("id")
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value):
                return value
        return None

    @staticmethod
    def compact_mapping(data: dict[str, object]) -> dict[str, object]:
        compact: dict[str, object] = {}
        for key, value in data.items():
            if isinstance(value, float):
                if value != 0.0:
                    compact[key] = value
            elif isinstance(value, int):
                if value != 0:
                    compact[key] = value
            elif value:
                compact[key] = value
        return compact

    def append_usage_event(self, worktree: Path, event: dict[str, object]) -> None:
        path = self.usage_events_file(worktree)
        if self.runner.dry_run:
            print(f"+ write {path}")
            return
        ensure_dir(path.parent)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, sort_keys=True, ensure_ascii=False) + "\n")

    def rewrite_usage_summary(self, worktree: Path) -> None:
        events = self.read_usage_events(worktree)
        summary = self.build_usage_summary(events)
        self.write_json_file(self.usage_summary_file(worktree), summary)

    def rewrite_usage_sources(self, worktree: Path) -> None:
        events = self.read_usage_events(worktree)
        sources: list[dict[str, object]] = []
        for event in events:
            for source in event.get("sources", []):
                if not isinstance(source, dict):
                    continue
                sources.append(source)
        payload = {
            "schema_version": 1,
            "generated_at": now_iso(),
            "run_id": self._last_state.run_id if self._last_state is not None else None,
            "harness": self.config.harness,
            "harness_dir": self.harness_dir.as_posix(),
            "sources": sources,
        }
        self.write_json_file(self.usage_sources_file(worktree), payload)

    def read_usage_events(self, worktree: Path) -> list[dict[str, object]]:
        path = self.usage_events_file(worktree)
        if not path.exists():
            return []
        return self.read_jsonl_records(path)

    def build_usage_summary(
        self, events: Sequence[dict[str, object]]
    ) -> dict[str, object]:
        phase_names = (
            "implementation",
            "audit",
            "conflict_resolution",
            "post_conflict_audit",
        )
        phases: dict[str, object] = {phase: {} for phase in phase_names}
        totals: dict[str, object] = {}
        nested_response_usage: dict[str, object] = {}
        models: dict[str, object] = {}
        tools: dict[str, object] = {}
        path_hashes: set[str] = set()
        run_id: str | None = None
        for event in events:
            if run_id is None and isinstance(event.get("run_id"), str):
                run_id = event["run_id"]
            phase = event.get("phase")
            if not isinstance(phase, str):
                continue
            phase_summary = phases.setdefault(
                phase,
                {
                    "runs": 0,
                    "status_counts": {},
                    "totals": {},
                    "nested_response_usage": {},
                    "models": {},
                    "tools": {},
                },
            )
            if not phase_summary:
                phase_summary.update(
                    {
                        "runs": 0,
                        "status_counts": {},
                        "totals": {},
                        "nested_response_usage": {},
                        "models": {},
                        "tools": {},
                    }
                )
            phase_summary["runs"] += 1
            status = event.get("status")
            if isinstance(status, str):
                self.increment_nested(
                    phase_summary["status_counts"], status, "count", 1
                )
            event_totals = event.get("totals")
            if isinstance(event_totals, dict):
                self.merge_numeric_map(phase_summary["totals"], event_totals)
                self.merge_numeric_map(totals, event_totals)
            event_nested = event.get("nested_response_usage")
            if isinstance(event_nested, dict):
                self.merge_numeric_map(
                    phase_summary["nested_response_usage"], event_nested
                )
                self.merge_numeric_map(nested_response_usage, event_nested)
            event_models = event.get("models")
            if isinstance(event_models, dict):
                self.merge_models(phase_summary["models"], event_models)
                self.merge_models(models, event_models)
            event_tools = event.get("tools")
            if isinstance(event_tools, dict):
                self.merge_tools(phase_summary["tools"], event_tools)
                self.merge_tools(tools, event_tools)
            for source in event.get("sources", []):
                if isinstance(source, dict) and isinstance(source.get("path_hash"), str):
                    path_hashes.add(source["path_hash"])
        for phase, phase_summary in list(phases.items()):
            if not isinstance(phase_summary, dict) or not phase_summary:
                continue
            status_counts = phase_summary.get("status_counts")
            if isinstance(status_counts, dict):
                phase_summary["status_counts"] = {
                    key: value.get("count", value) if isinstance(value, dict) else value
                    for key, value in status_counts.items()
                }
            for key in ("totals", "nested_response_usage"):
                value = phase_summary.get(key)
                if isinstance(value, dict):
                    phase_summary[key] = self.compact_mapping(value)
        return {
            "schema_version": 1,
            "generated_at": now_iso(),
            "run_id": run_id,
            "harness": self.config.harness,
            "harness_dir": self.harness_dir.as_posix(),
            "phases": phases,
            "totals": self.compact_mapping(totals),
            "nested_response_usage": self.compact_mapping(nested_response_usage),
            "models": models,
            "tools": tools,
            "sources": {"count": len(path_hashes), "path_hashes": sorted(path_hashes)},
            "privacy": {
                "prompt_text_logged": False,
                "response_text_logged": False,
                "tool_argument_values_logged": False,
                "session_paths_logged": False,
            },
        }

    def write_json_file(self, path: Path, payload: dict[str, object]) -> None:
        if self.runner.dry_run:
            print(f"+ write {path}")
            return
        self.write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")

    def implementation_prompt(self, worktree: Path, plan_path: Path) -> str:
        return f"""Use the implement-worktree skill.

Implement the approved plan in `{self.rel(worktree, plan_path)}` inside this worktree.

Requirements:
- Do not create, switch, merge, delete, or rebase worktrees.
- Keep edits scoped to the plan.
- Run focused tests or checks appropriate to the change.
- Commit the completed implementation.
- Write `{self.handoff_dir.as_posix()}/implementation-summary.md` with plan path, branch/worktree, changed files, behavior changes, tests run, skipped checks, assumptions, and known risks.
- Do not commit files under `{self.handoff_dir.as_posix()}/`; they are workflow artifacts and must remain untracked.
"""

    def run_implementation(self, worktree: Path, plan_path: Path) -> None:
        prompt = self.implementation_prompt(worktree, plan_path)
        output = worktree / self.handoff_dir / "implementation-final-response.md"
        self.harness_exec(worktree, prompt, output, phase="implementation")

    def audit_prompt(
        self, worktree: Path, plan_path: Path, *, post_conflict: bool
    ) -> str:
        summary = self.handoff_dir / (
            "post-conflict-audit-summary.md" if post_conflict else "audit-summary.md"
        )
        audit_finish_instruction = (
            "Do not commit. Leave all resolved merge state and audit fixes staged or unstaged for the workflow script to finalize."
            if post_conflict
            else "Commit audit fixes if changes are made."
        )
        return f"""Use the audit-worktree skill.

Fresh audit pass in this worktree.

Read:
- `{self.rel(worktree, plan_path)}`
- `{self.handoff_dir.as_posix()}/implementation-summary.md`
{f"- `{self.handoff_dir.as_posix()}/conflict-resolution-summary.md`" if post_conflict else ""}

Audit the actual diff against `{self.base}`. Fix confirmed issues and run relevant tests.
{audit_finish_instruction}
Do not commit files under `{self.handoff_dir.as_posix()}/`; they are workflow artifacts and must remain untracked.
Write `{summary.as_posix()}` before finishing.
"""

    def run_audit(
        self, worktree: Path, plan_path: Path, *, post_conflict: bool = False
    ) -> None:
        prompt = self.audit_prompt(worktree, plan_path, post_conflict=post_conflict)
        output = (
            worktree
            / self.handoff_dir
            / (
                "post-conflict-audit-final-response.md"
                if post_conflict
                else "audit-final-response.md"
            )
        )
        self.harness_exec(
            worktree,
            prompt,
            output,
            phase="post_conflict_audit" if post_conflict else "audit",
        )

    def harness_sandbox_mode(self) -> str:
        if os.name == "nt":
            return "danger-full-access"
        return "workspace-write"

    def is_omp_harness(self) -> bool:
        return Path(self.config.harness).name.lower().split(".", 1)[0] == "omp"

    def harness_validation_command(self) -> list[str]:
        if self.is_omp_harness():
            return [self.config.harness, "--help"]
        return [self.config.harness, "exec", "--help"]

    def omp_prompt_file(self, output_file: Path) -> Path:
        return output_file.with_name(f"{output_file.stem}-prompt.md")

    def omp_prompt_file_argument(self, prompt_file: Path) -> str:
        prompt_path = str(prompt_file)
        executable = CommandRunner.resolve_executable(self.config.harness)
        if is_windows_executable_path(executable):
            windows_path = wsl_drive_mount_to_windows_path(prompt_file)
            if windows_path is not None:
                prompt_path = windows_path
        return f"@{prompt_path}"

    def omp_exec_args(self, prompt_file: Path) -> list[str]:
        args = [
            self.config.harness,
            "-p",
            "--no-session",
            "--auto-approve",
            "--approval-mode",
            "yolo",
        ]
        if self.config.model:
            args.extend(["--model", self.config.model])
        args.append(self.omp_prompt_file_argument(prompt_file))
        return args

    def codex_exec_args(self, cwd: Path, output_file: Path) -> list[str]:
        args = [
            self.config.harness,
            "exec",
            "--cd",
            str(cwd),
            "--sandbox",
            self.harness_sandbox_mode(),
        ]
        for writable_root in self.extra_writable_roots(cwd):
            args.extend(["--add-dir", str(writable_root)])
        if self.config.model:
            args.extend(["--model", self.config.model])
        args.extend(["--output-last-message", str(output_file), "-"])
        return args

    def harness_exec(
        self, cwd: Path, prompt: str, output_file: Path, *, phase: str
    ) -> None:
        self.ensure_dir(output_file.parent)
        prompt_file: Path | None = None
        input_text: str | None = prompt
        if self.is_omp_harness():
            prompt_file = self.omp_prompt_file(output_file)
            self.write_text(prompt_file, prompt)
            args = self.omp_exec_args(prompt_file)
            input_text = None
        else:
            args = self.codex_exec_args(cwd, output_file)
        self.log_event(
            "harness_exec_start",
            cwd=str(cwd),
            output_file=str(output_file),
            command=logged_command(args),
        )
        session_snapshot = self.snapshot_omp_sessions(cwd)
        result = self.runner.run(args, cwd, check=False, input_text=input_text)
        if self.is_omp_harness():
            self.write_text(output_file, result.stdout)
            if (
                prompt_file is not None
                and result.returncode == 0
                and not result.timed_out
            ):
                prompt_file.unlink(missing_ok=True)
        output_fields = {
            "output_file": str(output_file),
            "output_file_exists": output_file.exists(),
        }
        self.log_command_result(
            "harness_exec_finish",
            result,
            **output_fields,
        )
        usage_event = self.collect_phase_usage(
            cwd, cwd, phase, session_snapshot, result
        )
        self.append_usage_event(cwd, usage_event)
        self.rewrite_usage_summary(cwd)
        self.rewrite_usage_sources(cwd)
        if result.returncode != 0 or result.timed_out:
            self.log_command_result(
                "harness_exec_failure",
                result,
                **output_fields,
            )
            raise FlowError(format_command_failure(result))

    def ensure_integration_context(
        self, feature_worktree: Path, integration_worktree: Path, plan_path: Path
    ) -> None:
        if not (
            integration_worktree / self.handoff_dir / "implementation-summary.md"
        ).exists():
            self.copy_integration_context(
                feature_worktree, integration_worktree, plan_path
            )

    def ensure_integration_worktree(
        self, repo: Path, state: WorkflowState, names: Names, plan_path: Path
    ) -> tuple[WorkflowState, Path]:
        resumed = self.resume_recorded_integration_worktree(
            repo, state, names, plan_path
        )
        if resumed is not None:
            return resumed
        restored = self.restore_recorded_integration_branch(
            repo, state, names, plan_path
        )
        if restored is not None:
            return restored
        return self.create_integration_worktree(repo, state, names, plan_path)

    def resume_recorded_integration_worktree(
        self, repo: Path, state: WorkflowState, names: Names, plan_path: Path
    ) -> tuple[WorkflowState, Path] | None:
        if state.integration_worktree is None:
            return None
        integration_worktree = Path(state.integration_worktree)
        if not integration_worktree.exists():
            return None
        self.runner.run(
            ["git", "worktree", "repair", str(integration_worktree)],
            repo,
            check=False,
        )
        self.prepare_existing_worktree(integration_worktree)
        if state.integration_branch is None:
            branch = self.current_branch(integration_worktree)
            if branch:
                state = self.update_workflow_state(state, integration_branch=branch)
        self.ensure_integration_context(names.worktree, integration_worktree, plan_path)
        self.continue_log(integration_worktree, state.run_id)
        return state, integration_worktree

    def restore_recorded_integration_branch(
        self, repo: Path, state: WorkflowState, names: Names, plan_path: Path
    ) -> tuple[WorkflowState, Path] | None:
        if state.integration_branch is None or not self.ref_exists(
            repo, state.integration_branch
        ):
            return None
        integration_branch = state.integration_branch
        integration_worktree = (
            Path(state.integration_worktree)
            if state.integration_worktree is not None
            else repo.parent / f"{repo.name}-integrate-{state.slug}"
        )
        if integration_worktree.exists():
            result = self.runner.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                integration_worktree,
                check=False,
            )
            if result.returncode != 0:
                raise FlowError(
                    f"Integration worktree path already exists: {integration_worktree}"
                )
            self.runner.run(
                ["git", "worktree", "repair", str(integration_worktree)],
                repo,
                check=False,
            )
        else:
            self.runner.run(
                [
                    "git",
                    "worktree",
                    "add",
                    str(integration_worktree),
                    integration_branch,
                ],
                repo,
            )
        self.prepare_existing_worktree(integration_worktree)
        self.ensure_integration_context(names.worktree, integration_worktree, plan_path)
        self.continue_log(integration_worktree, state.run_id)
        state = self.update_workflow_state(
            state,
            integration_branch=integration_branch,
            integration_worktree=str(integration_worktree),
            completed_stage="integration_worktree_created",
        )
        return state, integration_worktree

    def create_integration_worktree(
        self, repo: Path, state: WorkflowState, names: Names, plan_path: Path
    ) -> tuple[WorkflowState, Path]:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        integration_branch = f"integration/{names.slug}-{stamp}"
        integration_worktree = (
            repo.parent / f"{repo.name}-integrate-{names.slug}-{stamp}"
        )
        self.runner.run(
            [
                "git",
                "worktree",
                "add",
                str(integration_worktree),
                "-b",
                integration_branch,
                self.base,
            ],
            repo,
        )
        self.prepare_new_worktree(integration_worktree)
        self.copy_integration_context(names.worktree, integration_worktree, plan_path)
        self.continue_log(integration_worktree, state.run_id)
        state = self.update_workflow_state(
            state,
            integration_branch=integration_branch,
            integration_worktree=str(integration_worktree),
            completed_stage="integration_worktree_created",
        )
        return state, integration_worktree

    def refresh_committed_integration_for_advanced_base(
        self,
        state: WorkflowState,
        integration_worktree: Path,
        integration_branch: str,
    ) -> WorkflowState:
        if state.completed_stage != "integration_committed":
            return state
        base_is_ancestor = self.runner.run(
            ["git", "merge-base", "--is-ancestor", self.base, integration_branch],
            integration_worktree,
            check=False,
        )
        if base_is_ancestor.returncode == 0:
            return state
        if self.current_branch(integration_worktree) != integration_branch:
            raise FlowError(
                "Cannot refresh the recorded integration branch because its "
                "worktree is on a different branch."
            )
        self.require_clean_except_handoff(
            integration_worktree, "Recorded integration"
        )
        subject = self.runner.run(
            ["git", "log", "-1", "--format=%s", integration_branch],
            integration_worktree,
        ).stdout.strip()
        expected_subject = f"Harness: {state.plan_title}"
        if subject != expected_subject:
            raise FlowError(
                "Cannot refresh the recorded integration branch because its tip "
                f"is not the generated integration commit {expected_subject!r}."
            )
        self.runner.run(["git", "reset", "--hard", self.base], integration_worktree)
        state = self.update_workflow_state(
            state, completed_stage="integration_worktree_created"
        )
        self.print_checkpoint(
            "recover",
            "Integration base",
            (
                ("base", self.base),
                ("integration branch", integration_branch),
            ),
        )
        return state

    def finish(
        self, repo: Path, state: WorkflowState, names: Names, plan_path: Path
    ) -> WorkflowState:
        self.require_no_tracked_handoff_artifacts(repo, names.branch)
        self.require_ready_for_integration(names.worktree, names.branch)
        self.print_checkpoint(
            "start",
            "Integration",
            (
                ("mode", state.merge_mode),
                ("feature branch", names.branch),
            ),
        )
        self.runner.run(["git", "fetch", "--all", "--prune"], repo)
        state, integration_worktree = self.ensure_integration_worktree(
            repo, state, names, plan_path
        )
        self.print_checkpoint(
            "ready",
            "Integration worktree",
            (
                ("branch", state.integration_branch),
                ("worktree", integration_worktree),
            ),
        )
        if state.integration_branch is None:
            raise FlowError("Integration branch is missing from workflow state.")
        state = self.refresh_committed_integration_for_advanced_base(
            state, integration_worktree, state.integration_branch
        )
        integration_branch = state.integration_branch
        integration_plan = self.integration_plan_path(
            names.worktree, integration_worktree, plan_path
        )

        integrated = False
        archive_dir: Path | None = None
        try:
            integration_has_commits = self.branch_has_commits_since_base(
                integration_worktree, integration_branch
            )
            if self.has_unmerged_paths(integration_worktree):
                pass
            elif (
                self.has_non_handoff_changes(integration_worktree)
                and not integration_has_commits
            ):
                pass
            elif integration_has_commits:
                pass
            elif state.merge_mode == "squash":
                self.print_checkpoint(
                    "start", "Squash merge", (("feature branch", names.branch),)
                )
                merge = self.runner.run(
                    ["git", "merge", "--squash", names.branch],
                    integration_worktree,
                    check=False,
                )
                self.handle_merge_failure(
                    merge, integration_worktree, repo, "squash_merge"
                )
                self.print_checkpoint(
                    "done", "Squash merge", (("feature branch", names.branch),)
                )
            else:
                self.print_checkpoint(
                    "start", "No-ff merge", (("feature branch", names.branch),)
                )
                merge = self.runner.run(
                    ["git", "merge", "--no-ff", "--no-commit", names.branch],
                    integration_worktree,
                    check=False,
                )
                self.handle_merge_failure(
                    merge, integration_worktree, repo, "no_ff_merge"
                )
                self.print_checkpoint(
                    "done", "No-ff merge", (("feature branch", names.branch),)
                )

            conflict_summary = (
                integration_worktree
                / self.handoff_dir
                / "conflict-resolution-summary.md"
            )
            post_conflict_summary = (
                integration_worktree
                / self.handoff_dir
                / "post-conflict-audit-summary.md"
            )
            self.resolve_merge_conflicts(
                integration_worktree, names, integration_plan, conflict_summary
            )
            self.run_post_conflict_audit_if_needed(
                integration_worktree,
                integration_plan,
                conflict_summary,
                post_conflict_summary,
            )


            state = self.commit_integration_if_needed(
                state,
                names,
                integration_worktree,
                integration_branch,
            )
            state = self.fast_forward_base_if_needed(
                repo, state, names, integration_branch
            )
            state, archive_dir = self.archive_successful_handoff(
                repo, state, integration_worktree, integration_plan
            )
            integrated = True
        finally:
            if (
                integrated
                and not self.config.keep_worktrees
                and archive_dir is not None
            ):
                state = self.cleanup_successful_worktrees(
                    repo,
                    state,
                    integration_worktree,
                    integration_branch,
                    names,
                    archive_dir,
                )
        if archive_dir is not None:
            self.commit_worktree_flow_artifacts_if_needed(repo, state.plan_title)
        return state

    def handle_merge_failure(
        self, result: CommandResult, integration_worktree: Path, repo: Path, step: str
    ) -> bool:
        if result.returncode == 0:
            return False
        if result.timed_out:
            self.log_command_result(
                "command_failure", result, phase="finish", step=step
            )
            raise FlowError(format_command_failure(result))
        unmerged = self.unmerged_paths(integration_worktree)
        if unmerged:
            return True
        self.log_command_result("command_failure", result, phase="finish", step=step)
        raise FlowError(format_command_failure(result))

    def resolve_merge_conflicts(
        self,
        integration_worktree: Path,
        names: Names,
        integration_plan: Path,
        conflict_summary: Path,
    ) -> None:
        if self.has_unmerged_paths(integration_worktree):
            if not conflict_summary.exists():
                self.run_conflict_resolution(
                    integration_worktree, names, integration_plan
                )
            if self.has_unmerged_paths(integration_worktree):
                raise FlowError("Merge conflicts remain after conflict resolution.")

    def run_post_conflict_audit_if_needed(
        self,
        integration_worktree: Path,
        integration_plan: Path,
        conflict_summary: Path,
        post_conflict_summary: Path,
    ) -> None:
        if conflict_summary.exists() and not post_conflict_summary.exists():
            self.run_post_conflict_audit(integration_worktree, integration_plan)

    def commit_integration_if_needed(
        self,
        state: WorkflowState,
        names: Names,
        integration_worktree: Path,
        integration_branch: str,
    ) -> WorkflowState:
        if not self.branch_has_commits_since_base(
            integration_worktree, integration_branch
        ):
            self.print_checkpoint(
                "start",
                "Integration commit",
                (("branch", integration_branch), ("worktree", integration_worktree)),
            )
            self.stage_integration_changes(integration_worktree)
            state = self.update_workflow_state(
                state, completed_stage="integration_changes_staged"
            )
            if not self.has_staged_non_handoff_changes(integration_worktree):
                raise FlowError("No integration changes to commit.")
            self.runner.run(
                ["git", "commit", "-m", f"Harness: {state.plan_title}"],
                integration_worktree,
            )
            state = self.update_workflow_state(
                state, completed_stage="integration_committed"
            )
            self.print_checkpoint(
                "done", "Integration commit", (("branch", integration_branch),)
            )
        else:
            self.print_checkpoint(
                "skip", "Integration commit", (("branch", integration_branch),)
            )
        return state

    def fast_forward_base_if_needed(
        self, repo: Path, state: WorkflowState, names: Names, integration_branch: str
    ) -> WorkflowState:
        if not self.base_contains_branch(repo, integration_branch):
            self.print_checkpoint(
                "start",
                "Fast-forward base",
                (("base", self.base), ("integration branch", integration_branch)),
            )
            self.prepare_primary_for_fast_forward(
                repo, integration_branch, names.run_id
            )
            self.runner.run(["git", "switch", self.base], repo)
            fast_forward = self.runner.run(
                ["git", "merge", "--ff-only", integration_branch],
                repo,
                check=False,
            )
            if fast_forward.returncode != 0:
                self.log_command_result(
                    "command_failure",
                    fast_forward,
                    phase="finish",
                    step="fast_forward_merge",
                )
                raise FlowError(format_command_failure(fast_forward))
            state = self.update_workflow_state(
                state, completed_stage="base_fast_forwarded"
            )
            self.print_checkpoint(
                "done",
                "Fast-forward base",
                (("base", self.base), ("integration branch", integration_branch)),
            )
        return state

    def archive_successful_handoff(
        self,
        repo: Path,
        state: WorkflowState,
        integration_worktree: Path,
        plan_path: Path,
    ) -> tuple[WorkflowState, Path]:
        archive_dir = self.archive_handoff(repo, integration_worktree, state.run_id)
        self.archive_plan(archive_dir, plan_path)
        state = self.update_workflow_state(state, completed_stage="handoff_archived")
        self.print_checkpoint(
            "done", "Handoff archived", (("handoff archive", archive_dir),)
        )
        return state, archive_dir

    def commit_worktree_flow_artifacts_if_needed(
        self, repo: Path, plan_title: str
    ) -> None:
        self.runner.run(["git", "switch", self.base], repo)
        rel = self.worktree_flow_dir.as_posix()
        self.runner.run(["git", "add", "-A", "--", rel], repo)
        diff = self.runner.run(
            ["git", "diff", "--cached", "--quiet", "--", rel],
            repo,
            check=False,
        )
        if diff.returncode == 0:
            return
        if diff.returncode != 1:
            raise FlowError(format_command_failure(diff))
        self.runner.run(
            ["git", "commit", "-m", f"Harness artifacts: {plan_title}", "--", rel],
            repo,
        )

    def cleanup_successful_worktrees(
        self,
        repo: Path,
        state: WorkflowState,
        integration_worktree: Path,
        integration_branch: str,
        names: Names,
        archive_dir: Path,
    ) -> WorkflowState:
        self.cleanup(repo, integration_worktree, integration_branch, names)
        state = replace(state, completed_stage="cleanup_complete")
        self.save_workflow_state_file(archive_dir / WORKFLOW_STATE_FILENAME, state)
        self.print_checkpoint(
            "done",
            "Worktree cleanup",
            (("archive", archive_dir),),
        )
        self.log_file = archive_dir / "workflow.jsonl"
        self.log_event(
            "workflow_state_updated",
            completed_stage=state.completed_stage,
        )
        return state

    def run_conflict_resolution(
        self, integration_worktree: Path, names: Names, plan_path: Path
    ) -> None:
        context_path = (
            integration_worktree / self.handoff_dir / "merge-conflict-context.md"
        )
        self.write_text(
            context_path, self.conflict_context(integration_worktree, names, plan_path)
        )
        prompt = self.conflict_resolution_prompt(integration_worktree, names, plan_path)
        self.harness_exec(
            integration_worktree,
            prompt,
            integration_worktree
            / self.handoff_dir
            / "conflict-resolution-final-response.md",
            phase="conflict_resolution",
        )
        self.require_file(
            integration_worktree / self.handoff_dir / "conflict-resolution-summary.md"
        )

    def conflict_resolution_prompt(
        self, integration_worktree: Path, names: Names, plan_path: Path
    ) -> str:
        return f"""Use the merge-conflict-resolver skill.

Resolve merge conflicts in this integration worktree.

Read:
- `{self.handoff_dir.as_posix()}/merge-conflict-context.md`
- `{self.rel(integration_worktree, plan_path)}`
- `{self.handoff_dir.as_posix()}/implementation-summary.md`
- `{self.handoff_dir.as_posix()}/audit-summary.md`

Preserve latest `{self.base}` behavior unless the approved plan explicitly supersedes it. Keep the resolution narrow, remove all conflict markers, run focused checks if possible, and write `{self.handoff_dir.as_posix()}/conflict-resolution-summary.md`.
Do not commit.
"""

    def run_post_conflict_audit(
        self, integration_worktree: Path, plan_path: Path
    ) -> None:
        self.run_audit(integration_worktree, plan_path, post_conflict=True)
        self.require_file(
            integration_worktree / self.handoff_dir / "post-conflict-audit-summary.md"
        )

    def resolve_conflict(
        self, integration_worktree: Path, names: Names, plan_path: Path
    ) -> None:
        self.run_conflict_resolution(integration_worktree, names, plan_path)
        self.run_post_conflict_audit(integration_worktree, plan_path)

    def integration_plan_path(
        self, feature_worktree: Path, integration_worktree: Path, plan_path: Path
    ) -> Path:
        try:
            rel_plan = plan_path.resolve().relative_to(feature_worktree.resolve())
        except ValueError:
            rel_plan = Path("docs") / "plans" / plan_path.name
        return integration_worktree / rel_plan

    def copy_integration_context(
        self, feature_worktree: Path, integration_worktree: Path, plan_path: Path
    ) -> Path:
        source_handoff = feature_worktree / self.handoff_dir
        dest_handoff = integration_worktree / self.handoff_dir
        self.ensure_dir(dest_handoff)
        if source_handoff.exists():
            for item in source_handoff.iterdir():
                if item.is_file():
                    self.copy_file(item, dest_handoff / item.name)

        dest_plan = self.integration_plan_path(
            feature_worktree, integration_worktree, plan_path
        )
        self.ensure_dir(dest_plan.parent)
        if plan_path.exists():
            self.copy_file(plan_path, dest_plan)
        return dest_plan

    def archive_handoff(self, repo: Path, worktree: Path, run_id: str) -> Path:
        archive_dir = repo / self.worktree_flow_dir / run_id
        self.ensure_dir(archive_dir)
        source = worktree / self.handoff_dir
        if source.exists():
            for item in source.iterdir():
                if item.is_file():
                    self.copy_file(item, archive_dir / item.name)
        return archive_dir

    def archive_plan(self, archive_dir: Path, plan_path: Path) -> None:
        if plan_path.exists():
            self.ensure_dir(archive_dir)
            self.copy_file(plan_path, archive_dir / "plan.md")

    def stage_integration_changes(self, worktree: Path) -> None:
        self.runner.run(["git", "add", "-A"], worktree)
        self.runner.run(
            ["git", "reset", "HEAD", "--", self.handoff_dir.as_posix()],
            worktree,
            check=False,
        )








    def prepare_primary_for_fast_forward(
        self, repo: Path, integration_branch: str, run_id: str
    ) -> None:
        for rel in self.untracked_handoff_paths(repo):
            if not self.tree_has_path(repo, integration_branch, rel):
                continue
            path = repo / rel
            if self.path_matches_tree_blob(repo, integration_branch, rel):
                if not self.runner.dry_run:
                    path.unlink(missing_ok=True)
                self.log_event(
                    "primary_untracked_handoff_removed",
                    path=rel,
                    integration_branch=integration_branch,
                )
                continue
            archive_path = self.unique_untracked_archive_path(repo, run_id, rel)
            if not self.runner.dry_run:
                self.ensure_dir(archive_path.parent)
                shutil.move(str(path), str(archive_path))
            self.log_event(
                "primary_untracked_handoff_archived",
                path=rel,
                archive_path=str(archive_path),
                integration_branch=integration_branch,
            )

    def untracked_handoff_paths(self, worktree: Path) -> list[str]:
        result = self.runner.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            worktree,
        )
        paths: list[str] = []
        for entry in result.stdout.split("\0"):
            if not entry or entry[:2] != "??":
                continue
            rel = entry[3:].replace("\\", "/")
            if self.path_is_handoff(rel):
                paths.append(rel)
        return paths

    def tree_has_path(self, worktree: Path, treeish: str, rel: str) -> bool:
        return (
            self.runner.run(
                ["git", "cat-file", "-e", f"{treeish}:{rel}"],
                worktree,
                check=False,
            ).returncode
            == 0
        )

    def path_matches_tree_blob(self, worktree: Path, treeish: str, rel: str) -> bool:
        tree_hash = self.runner.run(
            ["git", "rev-parse", f"{treeish}:{rel}"],
            worktree,
            check=False,
        )
        if tree_hash.returncode != 0:
            return False
        worktree_hash = self.runner.run(
            ["git", "hash-object", "--", rel],
            worktree,
            check=False,
        )
        return (
            worktree_hash.returncode == 0
            and worktree_hash.stdout.strip() == tree_hash.stdout.strip()
        )

    def unique_untracked_archive_path(self, repo: Path, run_id: str, rel: str) -> Path:
        dest = repo / self.handoff_dir / "pre-fast-forward-untracked" / run_id / rel
        if not dest.exists():
            return dest
        for suffix in range(1, 1000):
            candidate = dest.with_name(f"{dest.name}.{suffix}")
            if not candidate.exists():
                return candidate
        raise FlowError(
            f"Could not choose archive path for untracked workflow file: {rel}"
        )

    def has_unmerged_paths(self, worktree: Path) -> bool:
        return bool(self.unmerged_paths(worktree))

    def unmerged_paths(self, worktree: Path) -> list[str]:
        result = self.runner.run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            worktree,
            check=False,
        )
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]


    def require_no_tracked_handoff_artifacts(
        self, worktree: Path, treeish: str
    ) -> None:
        result = self.runner.run(
            [
                "git",
                "ls-tree",
                "-r",
                "--name-only",
                treeish,
                "--",
                self.handoff_dir.as_posix(),
            ],
            worktree,
            check=False,
        )
        tracked = result.stdout.strip()
        if result.returncode != 0 or not tracked:
            return
        print(
            f"Warning: workflow handoff artifacts are tracked in {treeish}. "
            f"They should usually remain untracked:\n{tracked}",
            file=sys.stderr,
        )

    def head_rev(self, worktree: Path) -> str:
        result = self.runner.run(["git", "rev-parse", "HEAD"], worktree)
        return result.stdout.strip()

    def commit_count_since_base(self, worktree: Path, branch: str) -> int:
        result = self.runner.run(
            ["git", "rev-list", "--count", f"{self.base}..{branch}"],
            worktree,
        )
        raw = result.stdout.strip()
        return int(raw) if raw else 0

    def branch_has_commits_since_base(self, worktree: Path, branch: str) -> bool:
        return self.commit_count_since_base(worktree, branch) > 0

    def base_contains_branch(self, repo: Path, branch: str) -> bool:
        result = self.runner.run(
            ["git", "merge-base", "--is-ancestor", branch, self.base],
            repo,
            check=False,
        )
        return result.returncode == 0

    def require_commits_since_base(
        self, worktree: Path, branch: str, phase_name: str
    ) -> None:
        count = self.commit_count_since_base(worktree, branch)
        if count <= 0:
            raise FlowError(
                f"{phase_name} did not create any commits on {branch} after "
                f"{self.base}. Commit the completed implementation before "
                "continuing."
            )

    def require_branch_changed_since_base(self, worktree: Path, branch: str) -> None:
        result = self.runner.run(
            ["git", "diff", "--quiet", f"{self.base}...{branch}", "--", "."],
            worktree,
            check=False,
        )
        if result.returncode == 1:
            return
        if result.returncode == 0:
            raise FlowError(
                f"{branch} has no file changes compared with {self.base}. "
                "The workflow cannot integrate a no-op implementation."
            )
        raise FlowError(format_command_failure(result))

    def non_handoff_status(self, worktree: Path) -> list[str]:
        result = self.runner.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            worktree,
        )
        return [
            line
            for line in result.stdout.splitlines()
            if line.strip() and not self.status_line_is_handoff(line)
        ]

    def has_non_handoff_changes(self, worktree: Path) -> bool:
        return bool(self.non_handoff_status(worktree))

    def has_staged_non_handoff_changes(self, worktree: Path) -> bool:
        result = self.runner.run(
            ["git", "diff", "--cached", "--name-only"],
            worktree,
        )
        return any(
            line.strip() and not self.path_is_handoff(line.strip())
            for line in result.stdout.splitlines()
        )

    def status_line_is_handoff(self, line: str) -> bool:
        path_text = line[3:].strip()
        paths = [part.strip() for part in path_text.split(" -> ")]
        return all(self.path_is_handoff(path) for path in paths if path)

    def path_is_handoff(self, path: str) -> bool:
        normalized = path.replace("\\", "/").strip('"')
        handoff = self.handoff_dir.as_posix().rstrip("/")
        worktree_flow = self.worktree_flow_dir.as_posix().rstrip("/")
        if normalized == handoff or normalized.startswith(f"{handoff}/"):
            return True
        if normalized == worktree_flow or normalized.startswith(f"{worktree_flow}/"):
            return True
        return False

    def require_clean_except_handoff(self, worktree: Path, phase_name: str) -> None:
        status = self.non_handoff_status(worktree)
        if not status:
            return
        raise FlowError(
            f"{phase_name} left pending non-handoff changes in {worktree}:\n"
            + "\n".join(status)
        )

    def require_implementation_invariants(self, worktree: Path, branch: str) -> None:
        self.require_commits_since_base(worktree, branch, "Implementation")
        self.require_branch_changed_since_base(worktree, branch)
        self.require_clean_except_handoff(worktree, "Implementation")

    def require_audit_invariants(
        self, worktree: Path, branch: str, head_before: str
    ) -> None:
        self.require_clean_except_handoff(worktree, "Audit")
        head_after = self.head_rev(worktree)
        if head_after != head_before:
            self.require_branch_changed_since_base(worktree, branch)

    def require_ready_for_integration(self, worktree: Path, branch: str) -> None:
        self.require_no_tracked_handoff_artifacts(worktree, branch)
        self.require_clean_except_handoff(worktree, "Pre-integration")
        self.require_branch_changed_since_base(worktree, branch)

    def git_common_dir(self, worktree: Path) -> Path | None:
        result = self.runner.run(
            ["git", "rev-parse", "--git-common-dir"],
            worktree,
            check=False,
        )
        raw = result.stdout.strip()
        if result.returncode != 0 or not raw:
            return None
        path = Path(raw)
        if not path.is_absolute():
            path = worktree / path
        return path.resolve()

    def extra_writable_roots(self, worktree: Path) -> list[Path]:
        roots: list[Path] = []
        harness_dir = worktree / self.harness_dir
        if harness_dir.exists():
            roots.append(harness_dir.resolve())

        common_dir = self.git_common_dir(worktree)
        if common_dir is not None and not is_relative_to(common_dir, worktree):
            roots.append(common_dir)
        return roots

    def prepare_git_permissions(self, worktree: Path) -> None:
        common_dir = self.git_common_dir(worktree)
        if common_dir is not None and not is_relative_to(common_dir, worktree):
            self.prepare_harness_permissions(common_dir)

    def ensure_dir(self, path: Path) -> None:
        if self.runner.dry_run:
            print(f"+ mkdir -p {path}")
            return
        ensure_dir(path)

    def copy_file(self, source: Path, dest: Path) -> None:
        if self.runner.dry_run:
            print(f"+ copy {source} {dest}")
            return
        shutil.copy2(source, dest)

    def write_text(self, path: Path, text: str) -> None:
        if self.runner.dry_run:
            print(f"+ write {path}")
            return
        write_text(path, text)

    def prepare_harness_permissions(self, harness_dir: Path) -> None:
        if self.runner.dry_run or os.name != "nt" or not harness_dir.exists():
            return
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if shell is None:
            print(
                f"Warning: could not grant sandbox write permissions for {harness_dir}: "
                "PowerShell was not found.",
                file=sys.stderr,
            )
            return
        group = os.environ.get("CODEX_SANDBOX_GROUP", "CodexSandboxUsers")
        script = r"""
$Root = $env:CODEX_PERMISSION_ROOT
$Group = $env:CODEX_PERMISSION_GROUP
$ErrorActionPreference = 'Stop'
$identity = New-Object System.Security.Principal.NTAccount($Group)
$rights = [System.Security.AccessControl.FileSystemRights]::Modify
$propagate = [System.Security.AccessControl.PropagationFlags]::None
$items = @((Get-Item -LiteralPath $Root -Force))
$items += @(Get-ChildItem -LiteralPath $Root -Force -Recurse)
foreach ($item in $items) {
    $acl = Get-Acl -LiteralPath $item.FullName
    foreach ($rule in @($acl.Access)) {
        if ($rule.AccessControlType -eq [System.Security.AccessControl.AccessControlType]::Deny) {
            [void]$acl.RemoveAccessRuleSpecific($rule)
        }
    }
    if ($item.PSIsContainer) {
        $inherit = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'
    } else {
        $inherit = [System.Security.AccessControl.InheritanceFlags]::None
    }
    $allow = New-Object System.Security.AccessControl.FileSystemAccessRule(
        $identity,
        $rights,
        $inherit,
        $propagate,
        [System.Security.AccessControl.AccessControlType]::Allow
    )
    $acl.SetAccessRule($allow)
    Set-Acl -LiteralPath $item.FullName -AclObject $acl
}
"""
        completed = subprocess.run(
            [
                shell,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            check=False,
            capture_output=True,
            env={
                **os.environ,
                "CODEX_PERMISSION_ROOT": str(harness_dir),
                "CODEX_PERMISSION_GROUP": group,
            },
            text=True,
        )
        if completed.returncode != 0:
            print(
                "Warning: could not grant sandbox write permissions for "
                f"{harness_dir}: {(completed.stderr or completed.stdout).strip()}",
                file=sys.stderr,
            )

    def conflict_context(
        self, integration_worktree: Path, names: Names, plan_path: Path
    ) -> str:
        status = self.runner.run(
            ["git", "status", "--short"], integration_worktree, check=False
        ).stdout
        conflicted = self.runner.run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            integration_worktree,
            check=False,
        ).stdout
        merge_base = self.runner.run(
            ["git", "merge-base", self.base, names.branch],
            integration_worktree,
            check=False,
        ).stdout.strip()
        base_log = ""
        feature_log = ""
        if merge_base:
            base_log = self.runner.run(
                ["git", "log", "--oneline", f"{merge_base}..{self.base}"],
                integration_worktree,
                check=False,
            ).stdout
            feature_log = self.runner.run(
                ["git", "log", "--oneline", f"{merge_base}..{names.branch}"],
                integration_worktree,
                check=False,
            ).stdout

        return f"""# Merge Conflict Context

## Branches
- Base branch: {self.base}
- Feature branch: {names.branch}

## Plan
- Path: {self.rel(integration_worktree, plan_path)}

## Merge base
{merge_base or "unknown"}

## Conflicted files
{conflicted.strip() or "unknown"}

## Status
```text
{status.strip()}
```

## Base commits since merge base
```text
{base_log.strip()}
```

## Feature commits since merge base
```text
{feature_log.strip()}
```

## Resolution rules
1. Latest base behavior is presumed correct unless the approved plan explicitly supersedes it.
2. Feature intent comes from the approved plan and implementation summary.
3. Preserve audited feature behavior when compatible with latest base.
4. Prefer the smallest conflict-only edit.
5. Remove all conflict markers.
"""

    def cleanup(
        self,
        repo: Path,
        integration_worktree: Path,
        integration_branch: str,
        names: Names,
    ) -> None:
        repo_root = repo.resolve()
        for worktree in (integration_worktree, names.worktree):
            self.runner.run(
                ["git", "worktree", "remove", "--force", str(worktree)],
                repo,
                check=False,
            )
            if worktree.exists():
                if worktree.resolve() == repo_root:
                    raise FlowError(
                        "Refusing to remove repository root during cleanup."
                    )
                shutil.rmtree(worktree)
        self.runner.run(["git", "worktree", "prune"], repo, check=False)
        self.runner.run(["git", "branch", "-d", integration_branch], repo, check=False)
        # Squash merges do not mark the feature branch as merged, so force-delete
        # only after the integration branch has fast-forwarded successfully.
        self.runner.run(["git", "branch", "-D", names.branch], repo, check=False)

    def workflow_log_file(self, worktree: Path) -> Path:
        return worktree / self.handoff_dir / "workflow.jsonl"

    def start_log(self, worktree: Path, run_id: str) -> None:
        if self.runner.dry_run:
            return
        self.log_file = self.workflow_log_file(worktree)
        ensure_dir(self.log_file.parent)
        self.log_event(
            "workflow_log_started",
            log_file=str(self.log_file),
            run_id=run_id,
        )

    def continue_log(self, worktree: Path, run_id: str) -> None:
        if self.runner.dry_run:
            return
        previous = self.log_file
        self.log_file = self.workflow_log_file(worktree)
        ensure_dir(self.log_file.parent)
        self.log_event(
            "workflow_log_continued",
            log_file=str(self.log_file),
            previous_log_file=str(previous) if previous is not None else None,
            run_id=run_id,
        )

    def log_event(self, event: str, **fields: object) -> None:
        if self.log_file is None:
            return
        record = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "event": event,
            **fields,
        }
        with self.log_file.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def log_command_result(
        self, event: str, result: CommandResult, **fields: object
    ) -> None:
        self.log_event(
            event,
            cwd=str(result.cwd),
            command=logged_command(result.args),
            returncode=result.returncode,
            timed_out=result.timed_out,
            started_at=result.started_at,
            finished_at=result.finished_at,
            duration_ms=result.duration_ms,
            stdout=truncate_log_text(result.stdout),
            stderr=truncate_log_text(result.stderr),
            **fields,
        )

    def require_file(self, path: Path) -> None:
        if not path.exists() and not self.runner.dry_run:
            raise FlowError(f"Required output file was not created: {path}")

    @staticmethod
    def rel(root: Path, path: Path) -> str:
        try:
            return path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return str(path)


def build_parser(
    *,
    default_harness: str = DEFAULT_HARNESS,
    default_harness_dir: Path = HARNESS_DIR,
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the harness plan -> worktree -> audit -> finish workflow."
    )
    parser.add_argument("--plan", required=True, help="Approved Markdown plan file.")
    parser.add_argument(
        "--resume", action="store_true", help="Resume an existing worktree-flow run."
    )
    parser.add_argument(
        "--worktree",
        help=(
            "Existing feature worktree to resume. Defaults to the saved plan's "
            "existing worktree with --resume."
        ),
    )
    parser.add_argument(
        "--branch", help="Feature branch for --resume. Defaults to the worktree branch."
    )
    parser.add_argument(
        "--run-id",
        help="Workflow run id for legacy resumes without workflow-state.json.",
    )
    parser.add_argument(
        "--integration-worktree",
        help="Existing integration worktree for legacy resume after integration started.",
    )
    parser.add_argument(
        "--integration-branch",
        help="Existing integration branch for legacy resume after integration started.",
    )
    parser.add_argument(
        "--repo", default=".", help="Repository root. Defaults to current directory."
    )
    parser.add_argument(
        "--base",
        help=(
            "Base branch/ref. Defaults to the first existing branch among main, "
            "master, then the current branch."
        ),
    )
    parser.add_argument("--model", help="Optional harness model override.")
    parser.add_argument(
        "--harness",
        default=default_harness,
        help=f"Harness CLI executable. Defaults to {default_harness}.",
    )
    parser.add_argument(
        "--harness-dir",
        default=default_harness_dir.as_posix(),
        help=f"Harness artifact directory. Defaults to {default_harness_dir.as_posix()}.",
    )
    parser.add_argument(
        "--merge-mode", choices=["squash", "no-ff", "stop"], default="squash"
    )
    parser.add_argument(
        "--keep-worktrees",
        action="store_true",
        help="Do not remove feature/integration worktrees.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print each subprocess command before running it.",
    )
    parser.add_argument(
        "--command-timeout-seconds",
        type=positive_seconds,
        help="Optional timeout for each subprocess command.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print commands without running them."
    )
    return parser


def resume_only_values(args: argparse.Namespace) -> tuple[object, ...]:
    return (
        args.worktree,
        args.branch,
        args.run_id,
        args.integration_worktree,
        args.integration_branch,
    )


def flow_config_from_args(args: argparse.Namespace) -> FlowConfig:
    harness_dir = Path(args.harness_dir)
    return FlowConfig(
        repo=Path(args.repo).expanduser().resolve(),
        plan=Path(args.plan).expanduser().resolve(),
        base=args.base,
        harness=args.harness,
        command_timeout_seconds=args.command_timeout_seconds,
        harness_dir=harness_dir,
        model=args.model,
        merge_mode=args.merge_mode,
        keep_worktrees=args.keep_worktrees,
        verbose=args.verbose,
    )


def integration_worktree_arg(value: str | None) -> Path | None:
    if not value:
        return None
    return Path(value).expanduser().resolve()


def main(
    argv: list[str] | None = None,
    *,
    default_harness: str = DEFAULT_HARNESS,
    default_harness_dir: Path = HARNESS_DIR,
) -> int:
    parser = build_parser(
        default_harness=default_harness,
        default_harness_dir=default_harness_dir,
    )
    args = parser.parse_args(argv)
    resume_worktree_arg = (
        Path(args.worktree).expanduser().resolve() if args.worktree else None
    )
    resume_only_args = resume_only_values(args)
    if not args.resume and any(value is not None for value in resume_only_args):
        parser.error("resume-only arguments require --resume")

    config = flow_config_from_args(args)
    flow: HarnessWorktreeFlow | None = None
    try:
        flow = HarnessWorktreeFlow(
            config,
            CommandRunner(
                args.dry_run,
                verbose=config.verbose,
                command_timeout_seconds=config.command_timeout_seconds,
            ),
        )
        if args.resume:
            repo = flow.git_root(config.repo.resolve())
            requested_plan = config.plan.resolve()
            worktree = (
                resume_worktree_arg
                or flow.infer_resume_worktree(repo, requested_plan)
            )
            plan = flow.recover_resume_plan(requested_plan, worktree)
            flow.validate(repo, plan)
            flow.resume(
                repo=repo,
                plan=plan,
                worktree=worktree,
                branch=args.branch,
                run_id=args.run_id,
                integration_worktree=integration_worktree_arg(
                    args.integration_worktree
                ),
                integration_branch=args.integration_branch,
            )
        else:
            flow.run()
    except FlowError as exc:
        print(str(exc), file=sys.stderr)
        if flow is not None:
            resume_command = flow.resume_command()
            if resume_command is not None:
                print("\nResume command:", file=sys.stderr)
                print(f"  {resume_command}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
