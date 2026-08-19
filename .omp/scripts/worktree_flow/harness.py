"""Unattended OMP, Codex, and OpenCode adapters."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .command_runner import CommandResult, CommandRunner, format_command_failure, logged_command
from .models import FlowError, FlowConfig, HarnessKind
from .paths import MAX_DIAGNOSTIC_CHARS, atomic_write_text, ensure_directory, safe_unlink
from .usage import UsageCollector

Logger = Callable[[str, Mapping[str, object]], None]


@dataclass(frozen=True)
class HarnessFailure:
    phase: str
    diagnostic_path: Path
    result: CommandResult

    def message(self) -> str:
        status = "timed out" if self.result.timed_out else f"exited {self.result.returncode}"
        return f"Harness phase {self.phase} {status}; diagnostics: {self.diagnostic_path}"


def phase_model(
    kind: HarnessKind,
    phase: str,
    *,
    implementation_model: str | None,
    review_model: str | None,
    model: str | None,
) -> str | None:
    if phase in {"audit", "post_conflict_audit"}:
        return review_model or model or ("@slow" if kind is HarnessKind.OMP else None)
    return implementation_model or model or ("@default" if kind is HarnessKind.OMP else None)


class HarnessAdapter:
    def __init__(
        self,
        config: FlowConfig,
        runner: CommandRunner,
        *,
        logger: Logger | None = None,
        usage: UsageCollector | None = None,
        writable_roots: Callable[[Path], Sequence[Path]] | None = None,
    ) -> None:
        self.config = config
        self.runner = runner
        self.kind = HarnessKind.from_executable(config.harness)
        self.logger = logger
        self.usage = usage or UsageCollector(harness=config.harness, harness_dir=config.harness_dir, dry_run=runner.dry_run)
        self.writable_roots = writable_roots or (lambda _cwd: ())

    @property
    def harness_dir(self) -> Path:
        return self.config.harness_dir

    @property
    def is_omp(self) -> bool:
        return self.kind is HarnessKind.OMP

    def validation_command(self) -> list[str]:
        if self.kind is HarnessKind.OMP:
            return [self.config.harness, "--help"]
        if self.kind is HarnessKind.CODEX:
            return [self.config.harness, "exec", "--help"]
        return [self.config.harness, "run", "--help"]

    def validate(self, cwd: Path) -> CommandResult:
        return self.runner.run(self.validation_command(), cwd)

    def model_for(self, phase: str) -> str | None:
        return phase_model(
            self.kind,
            phase,
            implementation_model=self.config.implementation_model,
            review_model=self.config.review_model,
            model=self.config.model,
        )

    def prompt_path(self, worktree: Path, phase: str) -> Path:
        return worktree / self.harness_dir / "handoff" / f"{phase}-prompt.md"

    def diagnostic_path(self, worktree: Path, phase: str) -> Path:
        return worktree / self.harness_dir / "handoff" / f"{phase}-diagnostics.log"

    def omp_prompt_argument(self, prompt: Path) -> str:
        path = prompt
        executable = CommandRunner.resolve_executable(self.config.harness)
        if executable and executable.replace("\\", "/").lower().endswith(".exe"):
            raw = prompt.as_posix()
            if raw.startswith("/mnt/") and len(raw) > 6:
                path = Path(raw[5].upper() + ":\\" + raw[7:].replace("/", "\\"))
        return "@" + str(path)

    def omp_args(self, prompt: Path, phase: str) -> list[str]:
        args = [self.config.harness, "-p", "--no-session", "--auto-approve", "--approval-mode", "yolo"]
        model = self.model_for(phase)
        if model:
            args.extend(["--model", model])
        args.append(self.omp_prompt_argument(prompt))
        return args

    def codex_args(self, cwd: Path, phase: str) -> list[str]:
        args = [self.config.harness, "exec", "--cd", str(cwd), "--sandbox", "danger-full-access" if os.name == "nt" else "workspace-write"]
        for root in self.writable_roots(cwd):
            args.extend(["--add-dir", str(root)])
        model = self.model_for(phase)
        if model:
            args.extend(["--model", model])
        args.append("-")
        return args

    def opencode_args(self, cwd: Path, prompt: Path, phase: str) -> list[str]:
        args = [self.config.harness, "run", "--dir", str(cwd), "--dangerously-skip-permissions"]
        model = self.model_for(phase)
        if model:
            args.extend(["--model", model])
        args.extend(["--file", str(prompt), "Execute the attached worktree-flow phase prompt."])
        return args

    def command(self, cwd: Path, prompt: Path, phase: str) -> tuple[list[str], str | None]:
        if self.kind is HarnessKind.OMP:
            return self.omp_args(prompt, phase), None
        if self.kind is HarnessKind.CODEX:
            return self.codex_args(cwd, phase), None
        return self.opencode_args(cwd, prompt, phase), None

    def _log(self, event: str, **fields: object) -> None:
        if self.logger is not None:
            self.logger(event, fields)

    def _write_diagnostic(self, path: Path, result: CommandResult) -> None:
        text = (result.stdout + "\n" + result.stderr)[:MAX_DIAGNOSTIC_CHARS]
        atomic_write_text(path, text, max_bytes=MAX_DIAGNOSTIC_CHARS * 4)
        try:
            path.chmod(0o600)
        except OSError:
            pass

    def execute(self, cwd: Path, prompt_text: str, *, phase: str) -> CommandResult:
        handoff = cwd / self.harness_dir / "handoff"
        ensure_directory(handoff, mode=0o700)
        prompt = self.prompt_path(cwd, phase)
        if self.kind in {HarnessKind.OMP, HarnessKind.OPENCODE}:
            atomic_write_text(prompt, prompt_text)
        snapshot = self.usage.snapshot_sessions(cwd)
        args, _unused_input = self.command(cwd, prompt, phase)
        self._log("harness_exec_start", cwd=str(cwd), command=logged_command(args), phase=phase)
        input_text = prompt_text if self.kind is HarnessKind.CODEX else None
        result = self.runner.run(args, cwd, check=False, input_text=input_text)
        self._log(
            "harness_exec_finish",
            cwd=str(cwd),
            phase=phase,
            command=logged_command(args),
            returncode=result.returncode,
            timed_out=result.timed_out,
            started_at=result.started_at,
            finished_at=result.finished_at,
            duration_ms=result.duration_ms,
            stdout_bytes=result.stdout_bytes,
            stderr_bytes=result.stderr_bytes,
        )
        event = self.usage.collect_phase_usage(cwd, cwd, phase, snapshot, result)
        self.usage.append_event(cwd, event)
        self.usage.rewrite_artifacts(cwd)
        if result.returncode != 0 or result.timed_out:
            diagnostic = self.diagnostic_path(cwd, phase)
            self._write_diagnostic(diagnostic, result)
            self._log(
                "harness_exec_failure",
                cwd=str(cwd),
                phase=phase,
                diagnostic_path=str(diagnostic),
                returncode=result.returncode,
                timed_out=result.timed_out,
            )
            raise FlowError(HarnessFailure(phase, diagnostic, result).message())
        if self.kind in {HarnessKind.OMP, HarnessKind.OPENCODE}:
            safe_unlink(prompt)
        safe_unlink(self.diagnostic_path(cwd, phase))
        return result

    def prepare_permissions(self, roots: Sequence[Path]) -> None:
        """Grant one inheritable Modify rule only for Codex on Windows."""
        if self.kind is not HarnessKind.CODEX or os.name != "nt":
            return
        shell = CommandRunner.resolve_executable("pwsh") or CommandRunner.resolve_executable("powershell")
        if shell is None:
            raise FlowError("Codex ACL setup requires PowerShell (pwsh or powershell).")
        group = os.environ.get("CODEX_SANDBOX_GROUP", "CodexSandboxUsers")
        script = (
            "$ErrorActionPreference='Stop';"
            "$root=$args[0];$group=$args[1];"
            "$acl=Get-Acl -LiteralPath $root;"
            "$identity=New-Object System.Security.Principal.NTAccount($group);"
            "$rule=New-Object System.Security.AccessControl.FileSystemAccessRule($identity,'Modify','ContainerInherit,ObjectInherit','None','Allow');"
            "$acl.AddAccessRule($rule);Set-Acl -LiteralPath $root -AclObject $acl"
        )
        for root in roots:
            result = self.runner.run([shell, "-NoProfile", "-NonInteractive", "-Command", script, str(root), group], self.config.repo, check=False)
            if result.returncode != 0:
                raise FlowError(f"Codex ACL setup failed for {root}; correct permissions and retry.")


def implementation_prompt(worktree: Path, plan: Path, harness_dir: Path) -> str:
    rel_plan = plan.relative_to(worktree).as_posix() if plan.is_relative_to(worktree) else str(plan)
    return f"""Use the implement-worktree skill.\n\nImplement the approved plan in `{rel_plan}` inside this worktree.\n\nRequirements:\n- Do not create, switch, merge, delete, or rebase worktrees.\n- Keep edits scoped to the plan.\n- Run focused checks appropriate to the change.\n- Commit the completed implementation.\n- Write `{(harness_dir / 'handoff' / 'implementation-summary.md').as_posix()}` with plan path, worktree, commit SHA, changed files, behavior, checks, assumptions, and risks.\n- Do not commit workflow artifacts.\n"""


def audit_prompt(worktree: Path, plan: Path, harness_dir: Path, *, post_conflict: bool) -> str:
    rel_plan = plan.relative_to(worktree).as_posix() if plan.is_relative_to(worktree) else str(plan)
    summary = "post-conflict-audit-summary.md" if post_conflict else "audit-summary.md"
    finish = "Do not commit; leave integration fixes staged or unstaged." if post_conflict else "Commit audit fixes if changes are made."
    return f"""Use the audit-worktree skill.\n\nFresh audit pass in this worktree. Read `{rel_plan}` and `{(harness_dir / 'handoff' / 'implementation-summary.md').as_posix()}`.\nAudit the actual diff against the recorded base, fix confirmed issues, and run relevant checks. {finish}\nWrite `{(harness_dir / 'handoff' / summary).as_posix()}` before finishing. Do not commit workflow artifacts.\n"""


def conflict_resolution_prompt(worktree: Path, plan: Path, harness_dir: Path, base: str, feature_branch: str) -> str:
    rel_plan = plan.relative_to(worktree).as_posix() if plan.is_relative_to(worktree) else str(plan)
    return f"""Use the merge-conflict-resolver skill.\n\nResolve conflicts in this integration worktree between `{base}` and `{feature_branch}`. Read `{(harness_dir / 'handoff' / 'merge-conflict-context.md').as_posix()}` and `{rel_plan}`. Preserve latest base behavior unless the plan supersedes it, remove all conflict markers, and write `{(harness_dir / 'handoff' / 'conflict-resolution-summary.md').as_posix()}`. Do not commit.\n"""
