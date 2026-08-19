"""Public command-line parser and wrapper dispatch."""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

from .command_runner import CommandRunner
from .models import FlowConfig, FlowError
from .paths import (
    infer_default_harness,
    infer_default_harness_dir,
    validate_harness_dir,
)
from .workflow import HarnessWorktreeFlow


def positive_seconds(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def build_parser(
    *, default_harness: str, default_harness_dir: Path
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the harness plan -> worktree -> audit -> finish workflow."
    )
    parser.add_argument("--plan", required=True, help="Approved Markdown plan file.")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume an existing schema-v2 workflow state.",
    )
    parser.add_argument("--worktree", help="Explicit feature worktree for --resume.")
    parser.add_argument(
        "--repo", default=".", help="Repository root. Defaults to current directory."
    )
    parser.add_argument(
        "--base",
        help="Existing local base branch. Defaults to main, master, or current local branch.",
    )
    parser.add_argument("--state-dir", help="External workflow state root.")
    parser.add_argument("--model", help="Intentional all-phase model override.")
    parser.add_argument(
        "--implementation-model",
        help="Model selector for implementation and conflict resolution.",
    )
    parser.add_argument(
        "--review-model", help="Model selector for feature and post-conflict audits."
    )
    parser.add_argument(
        "--harness",
        default=default_harness,
        help=f"Harness executable. Defaults to {default_harness}.",
    )
    parser.add_argument(
        "--harness-dir",
        default=default_harness_dir.as_posix(),
        help=f"Repository-relative artifact directory. Defaults to {default_harness_dir}.",
    )
    parser.add_argument(
        "--merge-mode", choices=["squash", "no-ff", "stop"], default=None
    )
    parser.add_argument(
        "--keep-worktrees",
        action="store_true",
        help="Retain feature and integration worktrees after finalization.",
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
        "--dry-run",
        action="store_true",
        help="Print a deterministic happy-path preview without mutation.",
    )
    return parser


def _option_supplied(argv: list[str], option: str) -> bool:
    return any(
        argument == option or argument.startswith(option + "=") for argument in argv
    )


def flow_config_from_args(
    args: argparse.Namespace, *, entrypoint_path: Path, argv: list[str]
) -> FlowConfig:
    harness_dir = validate_harness_dir(args.harness_dir)
    return FlowConfig(
        repo=Path(args.repo).expanduser(),
        plan=Path(args.plan).expanduser(),
        base=args.base,
        harness=args.harness,
        harness_dir=harness_dir,
        state_dir=Path(args.state_dir).expanduser() if args.state_dir else None,
        model=args.model,
        implementation_model=args.implementation_model,
        review_model=args.review_model,
        merge_mode=args.merge_mode,
        keep_worktrees=args.keep_worktrees,
        verbose=args.verbose,
        command_timeout_seconds=args.command_timeout_seconds,
        dry_run=args.dry_run,
        resume=args.resume,
        entrypoint_path=entrypoint_path.resolve(),
        worktree=Path(args.worktree).expanduser() if args.worktree else None,
        harness_explicit=_option_supplied(argv, "--harness"),
        harness_dir_explicit=_option_supplied(argv, "--harness-dir"),
        keep_worktrees_explicit=_option_supplied(argv, "--keep-worktrees"),
        merge_mode_explicit=_option_supplied(argv, "--merge-mode"),
        model_explicit=_option_supplied(argv, "--model"),
        implementation_model_explicit=_option_supplied(argv, "--implementation-model"),
        review_model_explicit=_option_supplied(argv, "--review-model"),
        command_timeout_explicit=_option_supplied(argv, "--command-timeout-seconds"),
    )


def main(argv: list[str] | None = None, *, entrypoint_path: Path) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    default_dir = infer_default_harness_dir(entrypoint_path)
    parser = build_parser(
        default_harness=infer_default_harness(default_dir),
        default_harness_dir=default_dir,
    )
    args = parser.parse_args(raw_argv)
    if not args.resume and args.worktree is not None:
        parser.error("--worktree requires --resume")
    try:
        config = flow_config_from_args(
            args, entrypoint_path=entrypoint_path, argv=raw_argv
        )
        runner = CommandRunner(
            config.dry_run,
            verbose=config.verbose,
            command_timeout_seconds=config.command_timeout_seconds,
        )
        flow = HarnessWorktreeFlow(config, runner)
        if config.resume:
            flow.resume()
        else:
            flow.run()
        return 0
    except FlowError as exc:
        print(str(exc), file=sys.stderr)
        try:
            resume_command = locals().get("flow")
            if isinstance(resume_command, HarnessWorktreeFlow):
                command = resume_command.resume_command()
                if command:
                    print("\nResume command:", file=sys.stderr)
                    print(f"  {command}", file=sys.stderr)
        except (OSError, RuntimeError):
            pass
        return 1


__all__ = ["build_parser", "flow_config_from_args", "main", "positive_seconds"]
