#!/usr/bin/env python3
"""Save the latest OMP plan as a worktree-flow input.

The OMP plan-mode session records the current plan as a ``local://`` path in
``mode_change`` entries. This script resolves that session-local file, copies it
unchanged to ``.omp/worktree-flow/<timestamp>-<slug-from-h1>/plan.md``, verifies
the copy, and prints the command that runs ``worktree-flow.py`` with the saved plan.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

DEFAULT_HARNESS = "omp"
DEFAULT_HARNESS_DIR = Path(".omp")
DEFAULT_PLAN_NAME = "PLAN.md"
WORKTREE_FLOW_DIR = Path(".omp") / "worktree-flow"


@dataclass(frozen=True)
class PlanSource:
    path: Path
    source: str


def slugify(value: str, *, max_words: int = 6, max_len: int = 60) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    words = [part for part in cleaned.split("-") if part]
    slug = "-".join(words[:max_words]) or "harness-plan"
    return slug[:max_len].strip("-") or "harness-plan"


def plan_title(plan_path: Path) -> str:
    text = plan_path.read_bytes().decode("utf-8-sig")
    for line in text.splitlines():
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if match:
            return match.group(1).strip()
    return plan_path.stem


def timestamped_run_id(slug: str, *, stamp: str | None = None) -> str:
    timestamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{timestamp}-{slug}"


def local_url_path(value: str) -> str | None:
    parsed = urlparse(value)
    if parsed.scheme != "local":
        return None
    if parsed.netloc and parsed.path:
        relative = f"{parsed.netloc}{parsed.path}"
    else:
        relative = parsed.netloc or parsed.path.lstrip("/") or DEFAULT_PLAN_NAME
    return unquote(relative).replace("\\", "/")


def resolve_local_url(session_jsonl: Path, value: str) -> Path | None:
    relative = local_url_path(value)
    if relative is None:
        return None
    if relative.startswith("/") or re.match(r"^[A-Za-z]:/", relative):
        return Path(relative)
    return session_jsonl.with_suffix("") / "local" / relative


def wsl_windows_home_from_repo(repo: Path) -> Path | None:
    match = re.match(r"^/mnt/([A-Za-z])/Users/([^/]+)(?:/|$)", repo.as_posix())
    if not match:
        return None
    drive, user = match.groups()
    return Path("/mnt") / drive.lower() / "Users" / user


def candidate_sessions_roots(repo: Path) -> list[Path]:
    roots = [Path.home() / ".omp" / "agent" / "sessions"]
    wsl_home = wsl_windows_home_from_repo(repo.resolve())
    if wsl_home is not None:
        roots.append(wsl_home / ".omp" / "agent" / "sessions")
    return list(dict.fromkeys(roots))


def default_sessions_root(repo: Path) -> Path:
    roots = candidate_sessions_roots(repo)
    for root in roots:
        if root.exists():
            return root
    return roots[0]


def session_home_from_root(sessions_root: Path) -> Path | None:
    parts = sessions_root.parts
    if len(parts) >= 3 and parts[-3:] == (".omp", "agent", "sessions"):
        return Path(*parts[:-3])
    return None


def repo_session_group_names(repo: Path, sessions_root: Path) -> list[str]:
    resolved = repo.resolve()
    homes = [Path.home().resolve()]
    session_home = session_home_from_root(sessions_root.resolve())
    if session_home is not None:
        homes.append(session_home)

    names: list[str] = []
    for home in dict.fromkeys(homes):
        try:
            relative = resolved.relative_to(home)
        except ValueError:
            continue
        names.append("-" + "-".join(relative.parts))

    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", str(resolved)).strip("-")
    names.append(f"-{cleaned}" if cleaned else "-repo")
    return list(dict.fromkeys(names))


def session_groups(repo: Path, sessions_root: Path) -> list[Path]:
    if not sessions_root.exists():
        return []

    preferred_names = repo_session_group_names(repo, sessions_root)
    preferred_names_lowered = {name.lower() for name in preferred_names}
    groups: list[Path] = []
    for preferred_name in preferred_names:
        preferred = sessions_root / preferred_name
        if preferred.exists() and preferred not in groups:
            groups.append(preferred)
    for child in sorted(
        (path for path in sessions_root.iterdir() if path.is_dir()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    ):
        if child in groups:
            continue
        if child.name.lower() in preferred_names_lowered:
            groups.insert(0, child)
        else:
            groups.append(child)
    return groups


def iter_session_jsonls(repo: Path, sessions_root: Path) -> list[Path]:
    jsonls: list[Path] = []
    for group in session_groups(repo, sessions_root):
        jsonls.extend(path for path in group.glob("*.jsonl") if path.is_file())
    return sorted(jsonls, key=lambda path: path.stat().st_mtime, reverse=True)


def mode_change_plan_refs(session_jsonl: Path) -> list[str]:
    refs: list[str] = []
    with session_jsonl.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("type") != "mode_change" or record.get("mode") != "plan":
                continue
            data = record.get("data")
            if not isinstance(data, dict):
                continue
            for key in ("planFilePath", "planFile"):
                value = data.get(key)
                if isinstance(value, str) and value:
                    refs.append(value)
    return refs


def resolve_plan_ref(repo: Path, session_jsonl: Path, ref: str) -> Path:
    local_path = resolve_local_url(session_jsonl, ref)
    if local_path is not None:
        return local_path
    path = Path(ref)
    if not path.is_absolute():
        path = repo / path
    return path


def find_latest_omp_plan(repo: Path, sessions_root: Path) -> PlanSource:
    for session_jsonl in iter_session_jsonls(repo, sessions_root):
        for ref in reversed(mode_change_plan_refs(session_jsonl)):
            path = resolve_plan_ref(repo, session_jsonl, ref)
            if path.is_file():
                return PlanSource(path=path, source=ref)

        local_dir = session_jsonl.with_suffix("") / "local"
        if local_dir.is_dir():
            local_plans = sorted(
                (path for path in local_dir.glob("*.md") if path.is_file()),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            if local_plans:
                return PlanSource(path=local_plans[0], source=str(local_plans[0]))

    raise FileNotFoundError(
        f"No OMP plan file found under {sessions_root}. "
        "Pass --source with an explicit plan file if needed."
    )


def resolve_source_arg(repo: Path, sessions_root: Path, source: str | None) -> PlanSource:
    if source is None:
        return find_latest_omp_plan(repo, sessions_root)

    if local_url_path(source) is not None:
        for session_jsonl in iter_session_jsonls(repo, sessions_root):
            path = resolve_plan_ref(repo, session_jsonl, source)
            if path.is_file():
                return PlanSource(path=path, source=source)
        raise FileNotFoundError(f"Could not resolve {source} under {sessions_root}.")

    path = Path(source)
    if not path.is_absolute():
        path = repo / path
    if not path.is_file():
        raise FileNotFoundError(f"Plan source does not exist: {path}")
    return PlanSource(path=path, source=str(path))


def unique_worktree_flow_dir(root: Path, slug: str) -> Path:
    run_id = timestamped_run_id(slug)
    target = root / run_id
    if not target.exists():
        return target
    for suffix in range(2, 1000):
        candidate = root / f"{run_id}-{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not choose a unique worktree-flow directory for {slug}")


def copy_plan_to_worktree_flow(repo: Path, source: Path) -> Path:
    slug = slugify(plan_title(source))
    target_dir = unique_worktree_flow_dir(repo / WORKTREE_FLOW_DIR, slug)
    target = target_dir / "plan.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)

    source_bytes = source.read_bytes()
    target_bytes = target.read_bytes()
    if target_bytes != source_bytes:
        raise RuntimeError(f"Copied plan differs from source: {target}")
    return target


def command_for_plan(
    repo: Path,
    plan_path: Path,
    harness: str,
    harness_dir: Path,
    python_command: str = "python",
) -> str:
    worktree_flow = repo / harness_dir / "scripts" / "worktree-flow.py"
    if not worktree_flow.is_file():
        raise FileNotFoundError(f"Missing worktree flow script: {worktree_flow}")

    relative_plan = plan_path.relative_to(repo).as_posix()
    script_rel = harness_dir / "scripts" / "worktree-flow.py"
    script_cmd = script_rel.as_posix() if script_rel.is_absolute() else f"./{script_rel.as_posix()}"
    return (
        f"{python_command} {script_cmd} "
        f"--plan {relative_plan} "
        f"--harness {harness} "
        f"--harness-dir {harness_dir.as_posix()}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Copy the latest OMP plan to .omp/worktree-flow/<timestamp>-<slug>/plan.md."
    )
    parser.add_argument("--repo", default=".", help="Repository root. Defaults to cwd.")
    parser.add_argument(
        "--source",
        help="Optional explicit plan file or local:// plan reference. Defaults to the latest OMP plan.",
    )
    parser.add_argument(
        "--sessions-root",
        default=os.environ.get("OMP_SESSIONS_ROOT"),
        help="OMP sessions root. Defaults to the first existing candidate from the current home and WSL-mounted Windows home.",
    )
    parser.add_argument("--harness", default=DEFAULT_HARNESS)
    parser.add_argument(
        "--harness-dir",
        default=DEFAULT_HARNESS_DIR.as_posix(),
        help="Harness artifact directory passed to worktree-flow.py.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        raise FileNotFoundError(f"Repository root does not exist: {repo}")

    (repo / DEFAULT_HARNESS_DIR).mkdir(exist_ok=True)
    (repo / WORKTREE_FLOW_DIR).mkdir(parents=True, exist_ok=True)

    sessions_root = Path(args.sessions_root).resolve() if args.sessions_root else default_sessions_root(repo)
    plan_source = resolve_source_arg(repo, sessions_root, args.source)
    saved_plan = copy_plan_to_worktree_flow(repo, plan_source.path)
    print(command_for_plan(repo, saved_plan, args.harness, Path(args.harness_dir)))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - CLI should print concise failures.
        print(f"save-plan: {exc}", file=sys.stderr)
        raise SystemExit(1)
