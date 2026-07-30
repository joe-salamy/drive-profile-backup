"""Tests for generated Windows and WSL machine-state inventories."""

from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

import drive_backup.machine_state as machine_state
from drive_backup.machine_state import (
    CollectorStatus,
    collect_machine_state,
    run_resolved_tool,
)
from drive_backup.utils import atomic_write_json


class FakeRunner:
    def __init__(
        self,
        handler: Callable[[Sequence[str], float], subprocess.CompletedProcess[str]],
    ) -> None:
        self.handler = handler
        self.calls: list[tuple[list[str], float]] = []

    def __call__(
        self, argv: Sequence[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), timeout))
        return self.handler(argv, timeout)


def completed(
    argv: Sequence[str],
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(argv), returncode, stdout, stderr)


def powershell_only(executable: str) -> str | None:
    if executable == "powershell.exe":
        return "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    return None


def test_environment_writes_atomic_unredacted_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("drive_backup.machine_state.shutil.which", powershell_only)
    secret = "api-key-not-redacted"
    runner = FakeRunner(
        lambda argv, timeout: completed(
            argv, stdout=json.dumps({"Process": {"TOKEN": secret}})
        )
    )

    outcomes = collect_machine_state(str(tmp_path), ["environment"], runner=runner)

    assert outcomes == [
        machine_state.CollectorOutcome(
            name="environment",
            status=CollectorStatus.SUCCEEDED,
            output_file="_machine_state/environment.json",
            warnings=(),
            previous_output_retained=False,
        )
    ]
    output = json.loads(
        (tmp_path / "_machine_state" / "environment.json").read_text(encoding="utf-8")
    )
    assert output["schema_version"] == 1
    assert output["collector"] == "environment"
    assert output["data"]["Process"]["TOKEN"] == secret
    assert output["collected_at"].endswith("Z")
    assert not list((tmp_path / "_machine_state").glob("*.tmp"))


def test_optional_tool_failure_is_partial_and_later_collectors_continue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def which(executable: str) -> str | None:
        if executable == "powershell.exe":
            return "powershell.exe"
        if executable == "git":
            return "git.exe"
        return None

    monkeypatch.setattr("drive_backup.machine_state.shutil.which", which)

    def handle(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        if argv[0] == "git.exe":
            return completed(argv, returncode=1, stderr="bad global config")
        return completed(argv, stdout="{}")

    outcomes = collect_machine_state(
        str(tmp_path), ["developer_tools", "system"], runner=FakeRunner(handle)
    )

    assert [outcome.status for outcome in outcomes] == [
        CollectorStatus.PARTIAL,
        CollectorStatus.SUCCEEDED,
    ]
    assert "bad global config" in outcomes[0].warnings[0]
    developer = json.loads(
        (tmp_path / "_machine_state" / "developer_tools.json").read_text()
    )
    assert developer["data"]["tools"]["git_global_config"]["stderr"] == (
        "bad global config"
    )


def test_failed_collector_retains_previous_output_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_dir = tmp_path / "_machine_state"
    output_dir.mkdir()
    previous = output_dir / "system.json"
    previous.write_text('{"old": true}', encoding="utf-8")
    monkeypatch.setattr("drive_backup.machine_state.shutil.which", powershell_only)
    calls = 0

    def handle(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise subprocess.TimeoutExpired(list(argv), timeout)
        return completed(argv, stdout="{}")

    outcomes = collect_machine_state(
        str(tmp_path), ["system", "environment"], runner=FakeRunner(handle)
    )

    assert outcomes[0].status is CollectorStatus.FAILED
    assert outcomes[0].previous_output_retained is True
    assert outcomes[0].output_file == "_machine_state/system.json"
    assert previous.read_text(encoding="utf-8") == '{"old": true}'
    assert outcomes[1].status is CollectorStatus.SUCCEEDED


def test_snapshot_write_failure_is_non_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("drive_backup.machine_state.shutil.which", powershell_only)
    real_atomic_write = atomic_write_json

    def fail_snapshot(path: str | Path, data: object) -> None:
        if Path(path).name == "snapshot.json":
            raise OSError("metadata disk failure")
        real_atomic_write(path, data)

    monkeypatch.setattr("drive_backup.machine_state.atomic_write_json", fail_snapshot)
    runner = FakeRunner(lambda argv, timeout: completed(argv, stdout="{}"))

    outcomes = collect_machine_state(str(tmp_path), ["system"], runner=runner)

    assert outcomes[0].status is CollectorStatus.SUCCEEDED
    assert outcomes[1].name == "snapshot"
    assert outcomes[1].status is CollectorStatus.FAILED
    assert "metadata disk failure" in outcomes[1].warnings[0]


def test_disabled_known_outputs_are_deleted_but_unknown_files_remain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_dir = tmp_path / "_machine_state"
    output_dir.mkdir()
    (output_dir / "services.json").write_text("old", encoding="utf-8")
    (output_dir / "user.json").write_text("keep", encoding="utf-8")
    monkeypatch.setattr("drive_backup.machine_state.shutil.which", powershell_only)

    collect_machine_state(
        str(tmp_path), [], runner=FakeRunner(lambda a, t: completed(a))
    )

    assert not (output_dir / "services.json").exists()
    assert (output_dir / "user.json").read_text(encoding="utf-8") == "keep"
    snapshot = json.loads((output_dir / "snapshot.json").read_text())
    assert snapshot["collectors"] == []


def test_empty_selection_writes_metadata_without_creating_command_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_named_temporary_file = tempfile.NamedTemporaryFile

    def reject_wrapper(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("suffix") == ".ps1":
            raise AssertionError("empty selection must not create a command wrapper")
        return real_named_temporary_file(*args, **kwargs)

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", reject_wrapper)

    outcomes = collect_machine_state(str(tmp_path), [])

    assert outcomes == []
    snapshot = json.loads(
        (tmp_path / "_machine_state" / "snapshot.json").read_text(encoding="utf-8")
    )
    assert snapshot["collectors"] == []


def test_command_wrapper_failure_is_non_fatal_and_retains_previous_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_dir = tmp_path / "_machine_state"
    output_dir.mkdir()
    previous = output_dir / "system.json"
    previous.write_text('{"old": true}', encoding="utf-8")
    real_named_temporary_file = tempfile.NamedTemporaryFile

    def fail_wrapper(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("suffix") == ".ps1":
            raise OSError("wrapper storage failure")
        return real_named_temporary_file(*args, **kwargs)

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", fail_wrapper)

    outcomes = collect_machine_state(str(tmp_path), ["system"])

    assert outcomes[0].status is CollectorStatus.FAILED
    assert outcomes[0].previous_output_retained is True
    assert "wrapper storage failure" in outcomes[0].warnings[0]
    assert previous.read_text(encoding="utf-8") == '{"old": true}'
    snapshot = json.loads((output_dir / "snapshot.json").read_text(encoding="utf-8"))
    assert snapshot["outcomes"][0]["status"] == "failed"


def test_successful_command_stderr_is_retained_as_partial_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("drive_backup.machine_state.shutil.which", powershell_only)
    runner = FakeRunner(
        lambda argv, timeout: completed(
            argv,
            stdout=json.dumps({"Process": {"PATH": "value"}}),
            stderr="non-terminating PowerShell error",
        )
    )

    outcomes = collect_machine_state(str(tmp_path), ["environment"], runner=runner)

    assert outcomes[0].status is CollectorStatus.PARTIAL
    assert "non-terminating PowerShell error" in outcomes[0].warnings[0]
    output = json.loads(
        (tmp_path / "_machine_state" / "environment.json").read_text(encoding="utf-8")
    )
    assert output["data"]["Process"]["PATH"] == "value"


def test_wsl_probe_exception_marks_collector_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def which(executable: str) -> str | None:
        if executable in {"powershell.exe", "wsl.exe"}:
            return executable
        return None

    monkeypatch.setattr("drive_backup.machine_state.shutil.which", which)

    def handle(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        if argv[0] == "powershell.exe":
            return completed(argv, stdout="Ubuntu\n")
        if list(argv[1:]) == ["--list", "--verbose"]:
            return completed(argv, stdout="Ubuntu Running")
        if argv[-1] == "command -v snap":
            raise subprocess.TimeoutExpired(list(argv), timeout)
        if isinstance(argv[-1], str) and argv[-1].startswith("command -v "):
            return completed(argv, returncode=1)
        return completed(argv, stdout="ok")

    outcomes = collect_machine_state(str(tmp_path), ["wsl"], runner=FakeRunner(handle))

    assert outcomes[0].status is CollectorStatus.PARTIAL
    assert any("snap probe" in warning for warning in outcomes[0].warnings)


def test_wsl_iterates_every_distro_with_names_as_separate_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    distro_names = ["Ubuntu Work", "evil; $(touch nope)"]

    def which(executable: str) -> str | None:
        if executable == "powershell.exe":
            return "powershell.exe"
        if executable == "wsl.exe":
            return "wsl.exe"
        return None

    monkeypatch.setattr("drive_backup.machine_state.shutil.which", which)

    def handle(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        if argv[0] == "powershell.exe":
            return completed(argv, stdout="\x00".join("\n".join(distro_names)) + "\x00")
        if list(argv[1:]) == ["--list", "--verbose"]:
            return completed(argv, stdout="verbose inventory")
        if argv[-1] == "command -v snap":
            return completed(argv, returncode=1)
        return completed(argv, stdout="ok")

    runner = FakeRunner(handle)
    outcomes = collect_machine_state(str(tmp_path), ["wsl"], runner=runner)

    assert outcomes[0].status is CollectorStatus.SUCCEEDED
    distro_calls = [
        argv
        for argv, _ in runner.calls
        if "--distribution" in argv and "--exec" in argv
    ]
    for distro in distro_names:
        assert any(
            call[call.index("--distribution") + 1] == distro for call in distro_calls
        )
        assert all(
            distro not in item
            for call in distro_calls
            for item in call
            if item != distro
        )
    payload = json.loads((tmp_path / "_machine_state" / "wsl.json").read_text())
    assert [row["name"] for row in payload["data"]["distributions"]] == distro_names
    for distribution in payload["data"]["distributions"]:
        assert distribution["commands"]["snap"] == {
            "available": False,
            "returncode": None,
            "stdout": "",
            "stderr": "",
        }


def test_resolved_shim_uses_fixed_powershell_wrapper_and_raw_arguments() -> None:
    runner = FakeRunner(lambda argv, timeout: completed(argv))
    executable = "C:/Tools & Stuff/npm.cmd"
    arguments = ["arg&one", "%PATH%", 'space and "quote"']

    run_resolved_tool(
        executable,
        arguments,
        timeout=120,
        runner=runner,
        powershell_executable="powershell.exe",
        wrapper_path="C:/Temp/fixed wrapper.ps1",
    )

    argv, timeout = runner.calls[0]
    assert argv == [
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-File",
        "C:/Temp/fixed wrapper.ps1",
        executable,
        *arguments,
    ]
    assert timeout == 120


def test_resolved_shim_translates_wsl_wrapper_path_for_powershell() -> None:
    runner = FakeRunner(lambda argv, timeout: completed(argv))

    run_resolved_tool(
        "/mnt/c/Tools/npm.cmd",
        [],
        timeout=120,
        runner=runner,
        powershell_executable="powershell.exe",
        wrapper_path="/mnt/c/Users/test/_machine_state/wrapper.ps1",
    )

    argv, _ = runner.calls[0]
    assert argv[5] == "C:\\Users\\test\\_machine_state\\wrapper.ps1"
    assert argv[6] == "C:\\Tools\\npm.cmd"
