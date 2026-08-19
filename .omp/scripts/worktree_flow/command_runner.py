"""Subprocess execution, timing, and safe failure formatting."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence

from .models import FlowError


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

    @property
    def stdout_bytes(self) -> int:
        return len(self.stdout.encode("utf-8", errors="replace"))

    @property
    def stderr_bytes(self) -> int:
        return len(self.stderr.encode("utf-8", errors="replace"))


class CommandFailureError(FlowError):
    """A checked command failure with output retained only in the result object."""

    def __init__(self, result: CommandResult) -> None:
        self.result = result
        super().__init__(format_command_failure(result))


def now_iso() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def logged_command(args: Sequence[str]) -> list[str]:
    command = [str(arg) for arg in args]
    if command and command[-1] == "-":
        command.pop()
    return command


def shell_command(args: Sequence[str]) -> str:
    command = [str(arg) for arg in args]
    if os.name == "nt":
        return subprocess.list2cmdline(command)
    return shlex.join(command)


def decode_subprocess_output(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def format_command_failure(result: CommandResult) -> str:
    if result.timed_out:
        status = f"Command timed out: {shell_command(result.args)}"
    else:
        status = (
            f"Command failed with exit code {result.returncode}: "
            f"{shell_command(result.args)}"
        )
    return f"{status}\ncwd: {result.cwd}"


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
        normalized = tuple(str(arg) for arg in args)
        if not normalized:
            raise FlowError("Cannot run an empty command.")
        display = shell_command(normalized)
        if self.verbose or self.dry_run:
            print(f"+ ({cwd}) {display}")
        started_at = now_iso()
        started = time.perf_counter()
        if self.dry_run:
            return CommandResult(
                normalized,
                Path(cwd),
                0,
                started_at=started_at,
                finished_at=now_iso(),
                duration_ms=0,
            )

        executable = self.resolve_executable(normalized[0])
        if executable is None:
            raise FlowError(self.executable_not_found_message(normalized[0]))
        resolved_args = [executable, *normalized[1:]]
        try:
            completed = subprocess.run(
                resolved_args,
                cwd=Path(cwd),
                check=False,
                capture_output=capture,
                text=True,
                input=input_text,
                timeout=self.command_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            result = CommandResult(
                normalized,
                Path(cwd),
                -9,
                decode_subprocess_output(exc.stdout),
                decode_subprocess_output(exc.stderr),
                started_at,
                now_iso(),
                int((time.perf_counter() - started) * 1000),
                True,
            )
            if check:
                raise CommandFailureError(result) from exc
            return result
        except OSError as exc:
            raise FlowError(f"Failed to run command: {display}\ncwd: {cwd}\n{exc}") from exc

        result = CommandResult(
            normalized,
            Path(cwd),
            completed.returncode,
            completed.stdout or "",
            completed.stderr or "",
            started_at,
            now_iso(),
            int((time.perf_counter() - started) * 1000),
            False,
        )
        if check and result.returncode != 0:
            raise CommandFailureError(result)
        return result
