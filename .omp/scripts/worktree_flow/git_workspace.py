"""Live Git identity, status, worktree, and cleanup primitives."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path
from typing import Iterable, Sequence

from .command_runner import CommandResult, format_command_failure
from .models import FlowError, GitWorktree, StatusRecord
from .paths import canonical_path, is_relative_to

_DEFAULT_BASE_CANDIDATES = ("main", "master")
_COMMIT_RE = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")


class GitWorkspace:
    def __init__(self, repo: Path, runner: object, *, harness_dir: Path) -> None:
        self.repo = canonical_path(repo, must_exist=True)
        self.runner = runner
        self.harness_dir = harness_dir
        self.handoff_rel = (harness_dir / "handoff").as_posix()
        self.worktree_flow_rel = (harness_dir / "worktree-flow").as_posix()

    def _run(
        self,
        args: Sequence[str],
        cwd: Path | None = None,
        *,
        check: bool = True,
    ) -> CommandResult:
        result = self.runner.run(list(args), cwd or self.repo, check=check)  # type: ignore[attr-defined]
        return result

    @staticmethod
    def git_root(start: Path, runner: object) -> Path:
        result = runner.run(["git", "rev-parse", "--show-toplevel"], start)  # type: ignore[attr-defined]
        raw = result.stdout.strip()
        if not raw:
            raise FlowError(f"Git did not return a repository root for {start}")
        return canonical_path(Path(raw), must_exist=True)

    def git_common_dir(self, worktree: Path | None = None) -> Path:
        cwd = canonical_path(worktree or self.repo, must_exist=True)
        result = self._run(["git", "rev-parse", "--git-common-dir"], cwd, check=False)
        if result.returncode != 0 or not result.stdout.strip():
            raise FlowError(f"Cannot resolve Git common directory for {cwd}")
        value = Path(result.stdout.strip())
        if not value.is_absolute():
            value = cwd / value
        return canonical_path(value, must_exist=True)

    def worktrees(self) -> list[GitWorktree]:
        result = self._run(["git", "worktree", "list", "--porcelain"], self.repo)
        entries: list[GitWorktree] = []
        path: Path | None = None
        branch: str | None = None
        head: str | None = None
        bare = False

        def append() -> None:
            nonlocal path, branch, head, bare
            if path is not None:
                entries.append(GitWorktree(path, branch, head, bare))
            path = None
            branch = None
            head = None
            bare = False

        for line in result.stdout.splitlines():
            if not line:
                append()
                continue
            key, _, value = line.partition(" ")
            if key == "worktree":
                append()
                path = canonical_path(Path(value), must_exist=False)
            elif key == "HEAD":
                head = value.strip()
            elif key == "branch":
                branch = value.removeprefix("refs/heads/")
            elif key == "bare":
                bare = True
        append()
        return entries

    def require_registered_worktree(self, path: Path, expected_branch: str) -> GitWorktree:
        target = canonical_path(path, must_exist=False)
        common = self.git_common_dir(self.repo)
        for entry in self.worktrees():
            if entry.path != target:
                continue
            if entry.branch != expected_branch:
                raise FlowError(
                    f"Registered worktree {target} is on {entry.branch!r}, expected {expected_branch!r}."
                )
            if target.exists() and self.git_common_dir(target) != common:
                raise FlowError(f"Worktree {target} belongs to a different Git repository.")
            return entry
        raise FlowError(f"Worktree is not registered in the selected repository: {target}")

    def current_branch(self, worktree: Path | None = None) -> str:
        result = self._run(["git", "branch", "--show-current"], worktree or self.repo, check=False)
        if result.returncode != 0:
            raise FlowError(f"Cannot determine current branch for {worktree or self.repo}")
        return result.stdout.strip()

    def head(self, worktree: Path | None = None) -> str:
        result = self._run(["git", "rev-parse", "--verify", "HEAD^{commit}"], worktree or self.repo)
        value = result.stdout.strip()
        if not _COMMIT_RE.fullmatch(value):
            raise FlowError(f"Git returned an invalid HEAD for {worktree or self.repo}")
        return value.lower()

    def require_commit_oid(self, value: str, *, label: str = "commit") -> str:
        if not isinstance(value, str) or _COMMIT_RE.fullmatch(value) is None or value.lower() != value:
            raise FlowError(f"Invalid {label} receipt; expected a canonical full commit OID.")
        result = self._run(
            ["git", "rev-parse", "--verify", "--quiet", "--end-of-options", f"{value}^{{commit}}"],
            self.repo,
            check=False,
        )
        if result.returncode != 0:
            raise FlowError(f"{label} receipt does not name an existing commit: {value}")
        canonical = result.stdout.strip().lower()
        if canonical != value:
            raise FlowError(f"{label} receipt is not the repository's canonical full OID: {value}")
        return canonical
    def validate_local_branch_name(self, name: str) -> str:
        if not isinstance(name, str) or not name or name.startswith("-") or name.startswith(("refs/", "origin/")):
            raise FlowError(f"Invalid local branch name: {name!r}")
        check = self._run(["git", "check-ref-format", "--branch", name], self.repo, check=False)
        if check.returncode != 0:
            raise FlowError(f"Invalid local branch name: {name}")
        return name

    def require_local_branch(self, name: str) -> str:
        self.validate_local_branch_name(name)
        result = self._run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{name}"], self.repo, check=False)
        if result.returncode != 0:
            raise FlowError(f"Local branch does not exist: {name}")
        return name

    def require_base(self, name: str | None) -> str:
        if name is not None:
            return self.require_local_branch(name)
        for candidate in _DEFAULT_BASE_CANDIDATES:
            result = self._run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{candidate}"], self.repo, check=False)
            if result.returncode == 0:
                return candidate
        current = self.current_branch()
        if current:
            return self.require_local_branch(current)
        raise FlowError("Could not infer a local base branch; pass --base <branch>.")

    def primary_entry(self) -> GitWorktree:
        entries = self.worktrees()
        if not entries:
            raise FlowError("Selected repository has no registered Git worktree.")
        primary = entries[0]
        if primary.path != self.repo or primary.bare:
            raise FlowError("Selected repository is not the primary Git worktree entry.")
        return primary

    def status_records(self, worktree: Path | None = None) -> list[StatusRecord]:
        result = self._run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            worktree or self.repo,
        )
        fields = result.stdout.split("\0")
        records: list[StatusRecord] = []
        index = 0
        while index < len(fields):
            raw = fields[index]
            index += 1
            if not raw:
                continue
            if len(raw) < 3 or raw[2] != " ":
                raise FlowError("Git returned malformed porcelain-v1 status output.")
            original: str | None = None
            code = raw[:2]
            path = raw[3:]
            if code[0] in {"R", "C"} or code[1] in {"R", "C"}:
                if index >= len(fields) or not fields[index]:
                    raise FlowError("Git returned an incomplete rename status record.")
                original = fields[index]
                index += 1
            records.append(StatusRecord(code[0], code[1], path, original))
        return records
    def path_is_artifact(self, path: str) -> bool:
        normalized = path.replace("\\", "/").strip('"')
        if os.name == "nt":
            normalized = normalized.casefold()
            handoff = self.handoff_rel.casefold()
            flow = self.worktree_flow_rel.casefold()
        else:
            handoff = self.handoff_rel
            flow = self.worktree_flow_rel
        return normalized == handoff or normalized.startswith(f"{handoff}/") or normalized == flow or normalized.startswith(f"{flow}/")

    def tracked_handoff_paths(self, worktree: Path | None = None, treeish: str = "HEAD") -> list[str]:
        result = self._run(
            ["git", "ls-tree", "-r", "--name-only", "--end-of-options", treeish, "--", f":(literal){self.handoff_rel}"],
            worktree or self.repo,
            check=False,
        )
        if result.returncode != 0:
            raise FlowError(format_command_failure(result))
        return [line for line in result.stdout.splitlines() if line]

    def require_no_tracked_handoff(self, worktree: Path | None = None, treeish: str = "HEAD") -> None:
        tracked = self.tracked_handoff_paths(worktree, treeish)
        if tracked:
            raise FlowError("Tracked handoff artifacts are not allowed: " + ", ".join(tracked))

    def non_artifact_status(self, worktree: Path | None = None) -> list[StatusRecord]:
        return [record for record in self.status_records(worktree) if not self.path_is_artifact(record.path) and not (record.original_path and self.path_is_artifact(record.original_path))]

    def require_clean_except_artifacts(self, worktree: Path | None = None, *, phase: str = "Workflow") -> None:
        self.require_no_tracked_handoff(worktree)
        records = self.non_artifact_status(worktree)
        if records:
            details = [record.code + " " + record.path for record in records]
            raise FlowError(f"{phase} left non-artifact changes in {worktree or self.repo}:\n" + "\n".join(details))
    def require_no_unstaged_non_artifact(self, worktree: Path, *, phase: str) -> None:
        self.require_no_tracked_handoff(worktree)
        records = [
            record
            for record in self.non_artifact_status(worktree)
            if record.worktree_status != " " or record.code == "??"
        ]
        if records:
            details = [record.code + " " + record.path for record in records]
            raise FlowError(
                f"{phase} left unstaged non-artifact changes in {worktree}:\n"
                + "\n".join(details)
            )

    def require_primary_ready(self, base_branch: str) -> None:
        self.primary_entry()
        if self.current_branch() != base_branch:
            raise FlowError(f"Primary repository must already be checked out on {base_branch}.")
        self.require_clean_except_artifacts(self.repo, phase="Primary repository")

    def require_registered_branch(self, name: str) -> None:
        self.require_local_branch(name)

    def remove_registered_worktree(self, path: Path, expected_branch: str) -> None:
        target = canonical_path(path, must_exist=False)
        self.require_registered_worktree(target, expected_branch)
        result = self._run(["git", "worktree", "remove", "--force", "--", str(target)], self.repo, check=False)
        if result.returncode != 0:
            raise FlowError(format_command_failure(result))
        remaining = [entry for entry in self.worktrees() if entry.path == target]
        if remaining or target.exists():
            raise FlowError(f"Git did not remove registered worktree {target}")

    def prune(self) -> None:
        result = self._run(["git", "worktree", "prune"], self.repo, check=False)
        if result.returncode != 0:
            raise FlowError(format_command_failure(result))

    def delete_branch(self, name: str, *, force: bool = False) -> None:
        self.require_local_branch(name)
        flag = "-D" if force else "-d"
        result = self._run(["git", "branch", flag, "--", name], self.repo, check=False)
        if result.returncode != 0:
            raise FlowError(format_command_failure(result))
        check = self._run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{name}"], self.repo, check=False)
        if check.returncode == 0:
            raise FlowError(f"Git did not delete branch {name}")

    def branch_exists(self, name: str) -> bool:
        result = self._run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{name}"], self.repo, check=False)
        return result.returncode == 0

    def branch_tip(self, branch: str) -> str:
        result = self._run(["git", "rev-parse", "--verify", "--end-of-options", f"{branch}^{{commit}}"], self.repo)
        return self.require_commit_oid(result.stdout.strip(), label=f"branch {branch}")

    def count_commits_since(self, base: str, branch: str, worktree: Path | None = None) -> int:
        result = self._run(["git", "rev-list", "--count", "--end-of-options", f"{base}..{branch}"], worktree or self.repo)
        try:
            return int(result.stdout.strip())
        except ValueError as exc:
            raise FlowError("Git returned an invalid commit count.") from exc

    def require_changed_since(self, base: str, branch: str, worktree: Path | None = None) -> None:
        result = self._run(["git", "diff", "--quiet", "--end-of-options", f"{base}...{branch}", "--", "."], worktree or self.repo, check=False)
        if result.returncode == 0:
            raise FlowError(f"{branch} has no file changes compared with {base}.")
        if result.returncode != 1:
            raise FlowError(format_command_failure(result))

    def merge_base_is_ancestor(self, ancestor: str, descendant: str, worktree: Path | None = None) -> bool:
        result = self._run(["git", "merge-base", "--is-ancestor", "--end-of-options", ancestor, descendant], worktree or self.repo, check=False)
        if result.returncode not in {0, 1}:
            raise FlowError(format_command_failure(result))
        return result.returncode == 0

    def base_contains(self, branch: str, base: str) -> bool:
        return self.merge_base_is_ancestor(branch, base)

    def has_unmerged(self, worktree: Path) -> bool:
        result = self._run(["git", "ls-files", "-u", "-z"], worktree, check=False)
        if result.returncode != 0:
            raise FlowError(format_command_failure(result))
        return bool(result.stdout)

    def unmerged_paths(self, worktree: Path) -> list[str]:
        result = self._run(["git", "ls-files", "-u", "-z"], worktree, check=False)
        if result.returncode != 0:
            raise FlowError(format_command_failure(result))
        paths = {field.split("\t", 1)[-1] for field in result.stdout.split("\0") if field}
        return sorted(paths)

    def fingerprint(self, worktree: Path) -> str:
        parts: list[bytes] = []
        commands: tuple[tuple[str, ...], ...] = (
            ("git", "status", "--porcelain=v1", "-z", "--untracked-files=all"),
            ("git", "diff", "--binary"),
            ("git", "diff", "--cached", "--binary"),
            ("git", "ls-files", "-u", "-z"),
        )
        labels = (b"status", b"diff", b"cached", b"unmerged")
        for label, command in zip(labels, commands):
            result = self._run(command, worktree, check=False)
            if result.returncode != 0:
                raise FlowError(format_command_failure(result))
            payload = result.stdout.encode("utf-8", errors="surrogateescape")
            parts.extend((label, b"\0", payload, b"\0"))
        return hashlib.sha256(b"".join(parts)).hexdigest()

    def commit_subject(self, commit: str, worktree: Path | None = None) -> str:
        result = self._run(["git", "show", "-s", "--format=%s", "--end-of-options", commit], worktree or self.repo)
        return result.stdout.strip()
    def commit_paths(self, commit: str, relative_path: str, worktree: Path | None = None) -> tuple[str, ...]:
        if Path(relative_path).is_absolute() or any(part in {"", ".", ".."} for part in Path(relative_path).parts):
            raise FlowError("Commit path must be a normalized repository-relative path.")
        result = self._run(
            [
                "git",
                "ls-tree",
                "-r",
                "--name-only",
                "--full-tree",
                "--end-of-options",
                commit,
                "--",
                f":(literal){relative_path}",
            ],
            worktree or self.repo,
        )
        prefix = relative_path + "/"
        return tuple(
            line
            for line in result.stdout.splitlines()
            if line == relative_path or line.startswith(prefix)
        )

    def commit_touches(self, commit: str, relative_path: str, worktree: Path | None = None) -> bool:
        if Path(relative_path).is_absolute() or any(part in {"", ".", ".."} for part in Path(relative_path).parts):
            raise FlowError("Commit path must be a normalized repository-relative path.")
        result = self._run(
            [
                "git",
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "-r",
                "--root",
                "--end-of-options",
                commit,
                "--",
                f":(literal){relative_path}",
            ],
            worktree or self.repo,
        )
        return any(line == relative_path or line.startswith(relative_path + "/") for line in result.stdout.splitlines())

    def commit_parents(self, commit: str, worktree: Path | None = None) -> tuple[str, ...]:
        result = self._run(["git", "show", "-s", "--format=%P", "--end-of-options", commit], worktree or self.repo)
        return tuple(value for value in result.stdout.strip().split() if value)

    def commit_tree(self, commit: str, worktree: Path | None = None) -> str:
        result = self._run(["git", "show", "-s", "--format=%T", "--end-of-options", commit], worktree or self.repo)
        return result.stdout.strip()

    def extra_writable_roots(self, worktree: Path) -> list[Path]:
        roots: list[Path] = []
        harness = worktree / self.harness_dir
        try:
            info = harness.lstat()
        except FileNotFoundError:
            info = None
        except OSError as exc:
            raise FlowError(f"Cannot inspect harness root: {harness}") from exc
        if info is not None:
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise FlowError(f"Refusing non-directory harness root: {harness}")
            roots.append(canonical_path(harness, must_exist=True))
        common = self.git_common_dir(worktree)
        if not is_relative_to(common, worktree):
            roots.append(common)
        return roots

    def worktree_for_branch(self, branch: str) -> GitWorktree | None:
        for entry in self.worktrees():
            if entry.branch == branch:
                return entry
        return None

    def require_feature_worktree(self, path: Path, branch: str) -> GitWorktree:
        entry = self.require_registered_worktree(path, branch)
        if self.current_branch(entry.path) != branch:
            raise FlowError(f"Feature worktree is not checked out on {branch}: {entry.path}")
        return entry

    def require_integration_worktree(self, path: Path, branch: str) -> GitWorktree:
        return self.require_feature_worktree(path, branch)

    def parse_untracked_artifacts(self, worktree: Path) -> list[str]:
        return [record.path for record in self.status_records(worktree) if record.code == "??" and self.path_is_artifact(record.path)]

    def require_no_unexpected_changes(self, worktree: Path, *, phase: str) -> None:
        self.require_clean_except_artifacts(worktree, phase=phase)

    def list_refs(self, prefix: str) -> list[str]:
        result = self._run(["git", "for-each-ref", "--format=%(refname)", prefix], self.repo)
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def require_same_repository(self, path: Path) -> None:
        if self.git_common_dir(path) != self.git_common_dir(self.repo):
            raise FlowError(f"Path is not registered in the selected Git repository: {path}")

    def assert_absent_worktree(self, path: Path) -> None:
        target = canonical_path(path, must_exist=False)
        if any(entry.path == target for entry in self.worktrees()):
            raise FlowError(f"Git still registers worktree path: {target}")
        if target.exists():
            raise FlowError(f"Worktree path still exists after Git removal: {target}")

    def assert_branch_tip(self, branch: str, expected: str) -> None:
        actual = self.branch_tip(branch)
        if actual != expected:
            raise FlowError(f"Branch {branch} tip changed unexpectedly: {actual} != {expected}")
