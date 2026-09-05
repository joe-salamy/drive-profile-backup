"""Generate restore-oriented Windows and WSL machine-state inventories."""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from drive_backup.config import MACHINE_STATE_COLLECTORS
from drive_backup.utils import atomic_write_json

logger = logging.getLogger(__name__)

MACHINE_STATE_DIRECTORY = "_machine_state"
COMMAND_TIMEOUT = 120.0
LONG_COMMAND_TIMEOUT = 300.0
POWERSHELL_WRAPPER = "param([Parameter(Position=0,Mandatory=$true)][string]$Executable,[Parameter(ValueFromRemainingArguments=$true)][string[]]$ToolArgs) & $Executable @ToolArgs; exit $LASTEXITCODE"
POWERSHELL_PREFIX = (
    "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "
    "$OutputEncoding = [Console]::OutputEncoding; "
)


class CollectorStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CollectorOutcome:
    name: str
    status: CollectorStatus
    output_file: str | None
    warnings: tuple[str, ...]
    previous_output_retained: bool


class CommandRunner(Protocol):
    def __call__(
        self, argv: Sequence[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]: ...


def run_command(
    argv: Sequence[str], *, timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run one command without a shell and preserve all diagnostic output."""
    return subprocess.run(
        list(argv),
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout,
    )


def _windows_path_for_powershell(path: str) -> str:
    """Translate a WSL-mounted drive path for a Windows process."""
    normalized = path.replace("\\", "/")
    parts = normalized.split("/")
    if len(parts) >= 4 and parts[0] == "" and parts[1] == "mnt":
        drive = parts[2]
        if len(drive) == 1 and drive.isalpha():
            return f"{drive.upper()}:\\" + "\\".join(parts[3:])
    return path


def run_resolved_tool(
    executable: str,
    args: Sequence[str],
    *,
    timeout: float,
    runner: CommandRunner,
    powershell_executable: str,
    wrapper_path: str,
) -> subprocess.CompletedProcess[str]:
    """Run a resolved executable, safely routing shell shims through PowerShell."""
    suffix = Path(executable).suffix.lower()
    if suffix in {".ps1", ".cmd", ".bat"}:
        argv = [
            powershell_executable,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            _windows_path_for_powershell(wrapper_path),
            _windows_path_for_powershell(executable),
            *args,
        ]
    else:
        argv = [executable, *args]
    return runner(argv, timeout=timeout)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _warning(label: str, error: object) -> str:
    return f"{label}: {error}"


def _process_warning(label: str, result: subprocess.CompletedProcess[str]) -> str:
    details = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
    return f"{label} exited with status {result.returncode}: {details}"


def _run_catching(
    argv: Sequence[str],
    *,
    timeout: float,
    runner: CommandRunner,
) -> tuple[subprocess.CompletedProcess[str] | None, str | None]:
    try:
        return runner(argv, timeout=timeout), None
    except (OSError, subprocess.SubprocessError) as error:
        return None, str(error)


def _powershell_argv(executable: str, script: str, *arguments: str) -> list[str]:
    return [
        executable,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        POWERSHELL_PREFIX + script,
        *arguments,
    ]


def _powershell_json(
    powershell: str | None,
    script: str,
    label: str,
    runner: CommandRunner,
) -> tuple[object | None, str | None]:
    if powershell is None:
        return None, f"{label}: powershell.exe is unavailable"
    result, error = _run_catching(
        _powershell_argv(powershell, f"{script} | ConvertTo-Json -Depth 12"),
        timeout=COMMAND_TIMEOUT,
        runner=runner,
    )
    if error is not None:
        return None, _warning(label, error)
    assert result is not None
    if result.returncode != 0:
        return None, _process_warning(label, result)
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as parse_error:
        return None, _warning(f"{label} returned invalid JSON", parse_error)
    if result.stderr.strip():
        return data, _warning(f"{label} wrote to stderr", result.stderr.strip())
    return data, None


def _command_row(
    executable: str | None,
    args: Sequence[str],
    *,
    parse_json: bool,
    timeout: float,
    runner: CommandRunner,
    powershell: str | None,
    wrapper_path: str,
) -> tuple[dict[str, object], str | None]:
    if executable is None:
        return {
            "available": False,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "data": None,
        }, None
    if powershell is None and Path(executable).suffix.lower() in {
        ".ps1",
        ".cmd",
        ".bat",
    }:
        return {
            "available": True,
            "returncode": None,
            "stdout": "",
            "stderr": "powershell.exe is unavailable for command shim",
            "data": None,
        }, "powershell.exe is unavailable for command shim"
    try:
        result = run_resolved_tool(
            executable,
            args,
            timeout=timeout,
            runner=runner,
            powershell_executable=powershell or "powershell.exe",
            wrapper_path=wrapper_path,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return {
            "available": True,
            "returncode": None,
            "stdout": "",
            "stderr": str(error),
            "data": None,
        }, str(error)
    data: object | None = None
    parse_warning: str | None = None
    if parse_json and result.returncode == 0:
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            parse_warning = f"invalid JSON: {error}"
    warning = parse_warning
    if result.returncode != 0:
        warning = _process_warning(Path(executable).name, result)
    elif result.stderr.strip():
        warning = _warning(
            f"{Path(executable).name} wrote to stderr", result.stderr.strip()
        )
    return {
        "available": True,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "data": data,
    }, warning


def _status(successes: int, failures: int) -> CollectorStatus:
    if failures == 0:
        return CollectorStatus.SUCCEEDED
    if successes > 0:
        return CollectorStatus.PARTIAL
    return CollectorStatus.FAILED


CollectorResult = tuple[object, CollectorStatus, list[str]]
Collector = Callable[[str | None, CommandRunner, str], CollectorResult]


def _single_powershell_collector(script: str, label: str) -> Collector:
    def collect(
        powershell: str | None, runner: CommandRunner, wrapper_path: str
    ) -> CollectorResult:
        del wrapper_path
        data, warning = _powershell_json(powershell, script, label, runner)
        if data is None:
            return {}, CollectorStatus.FAILED, [warning or f"{label} failed"]
        if warning is not None:
            return data, CollectorStatus.PARTIAL, [warning]
        return data, CollectorStatus.SUCCEEDED, []

    return collect


_SYSTEM_SCRIPT = """[PSCustomObject]@{
ComputerInfo = Get-ComputerInfo
Culture = Get-Culture
UICulture = Get-UICulture
TimeZone = Get-TimeZone
}"""

_WINDOWS_APPS_SCRIPT = r"""$paths = @(
'Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
'Registry::HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
'Registry::HKEY_CURRENT_USER\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
'Registry::HKEY_CURRENT_USER\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*'
)
[PSCustomObject]@{
DesktopApplications = @($paths | ForEach-Object {
    Get-ItemProperty -Path $_ -ErrorAction SilentlyContinue |
        Select-Object DisplayName, DisplayVersion, Publisher, InstallDate,
            InstallLocation, UninstallString, QuietUninstallString,
            WindowsInstaller, SystemComponent
})
AppxPackages = @(Get-AppxPackage |
    Select-Object Name, PackageFullName, Version, Architecture, Publisher,
        InstallLocation, IsFramework, SignatureKind, Status, NonRemovable)
}"""
_SERVICES_SCRIPT = (
    "Get-CimInstance Win32_Service | "
    "Select-Object Name, DisplayName, State, StartMode, StartName, PathName, "
    "Description, ProcessId, ServiceType"
)
_SCHEDULED_TASKS_SCRIPT = (
    "Get-ScheduledTask | "
    "Select-Object TaskPath, TaskName, State, Author, Description, URI, Source, "
    "Actions, Triggers, Principal, Settings"
)
_DRIVERS_SCRIPT = (
    "Get-CimInstance Win32_PnPSignedDriver | "
    "Select-Object DeviceName, DeviceClass, Manufacturer, DriverProviderName, "
    "DriverVersion, DriverDate, InfName, IsSigned, Signer, Status"
)
_ENVIRONMENT_SCRIPT = """[PSCustomObject]@{
Process = [Environment]::GetEnvironmentVariables('Process')
User = [Environment]::GetEnvironmentVariables('User')
Machine = [Environment]::GetEnvironmentVariables('Machine')
}"""
_MODULES_SCRIPT = (
    "Get-Module -ListAvailable | "
    "Select-Object Name, Version, Path, ModuleType, Guid, CompanyName, Description"
)


def _collect_windows_features(
    powershell: str | None, runner: CommandRunner, wrapper_path: str
) -> CollectorResult:
    del wrapper_path
    payload: dict[str, object] = {}
    warnings: list[str] = []
    for key, script in (
        ("optional_features", "Get-WindowsOptionalFeature -Online"),
        ("capabilities", "Get-WindowsCapability -Online"),
    ):
        data, warning = _powershell_json(powershell, script, key, runner)
        payload[key] = data
        if warning is not None:
            warnings.append(warning)
    successes = sum(value is not None for value in payload.values())
    return payload, _status(successes, len(warnings)), warnings


def _collect_network(
    powershell: str | None, runner: CommandRunner, wrapper_path: str
) -> CollectorResult:
    del wrapper_path
    script = """[PSCustomObject]@{
Adapters = @(Get-NetAdapter |
    Select-Object Name, InterfaceDescription, Status, MacAddress, LinkSpeed,
        MediaType, PhysicalMediaType, Virtual)
IPConfiguration = @(Get-NetIPConfiguration -Detailed |
    Select-Object InterfaceAlias, InterfaceIndex, InterfaceDescription,
        NetProfile, IPv4Address, IPv6Address, IPv4DefaultGateway,
        IPv6DefaultGateway, DNSServer)
Routes = @(Get-NetRoute |
    Select-Object DestinationPrefix, NextHop, RouteMetric, InterfaceIndex,
        InterfaceAlias, AddressFamily, State)
DnsServers = @(Get-DnsClientServerAddress |
    Select-Object InterfaceAlias, InterfaceIndex, AddressFamily, ServerAddresses)
FirewallProfiles = @(Get-NetFirewallProfile |
    Select-Object Name, Enabled, DefaultInboundAction, DefaultOutboundAction,
        AllowInboundRules, AllowLocalFirewallRules, AllowLocalIPsecRules,
        NotifyOnListen)
}"""
    powershell_data, warning = _powershell_json(powershell, script, "network", runner)
    warnings = [warning] if warning is not None else []
    payload: dict[str, object] = {"powershell": powershell_data}
    successes = int(powershell_data is not None)
    netsh = shutil.which("netsh.exe") or shutil.which("netsh")
    for key, args in (
        ("wlan_interfaces", ["wlan", "show", "interfaces"]),
        ("wlan_profiles", ["wlan", "show", "profiles"]),
    ):
        if netsh is None:
            payload[key] = {
                "available": False,
                "returncode": None,
                "stdout": "",
                "stderr": "",
            }
            continue
        result, error = _run_catching(
            [netsh, *args], timeout=COMMAND_TIMEOUT, runner=runner
        )
        if error is not None:
            payload[key] = {
                "available": True,
                "returncode": None,
                "stdout": "",
                "stderr": error,
            }
            warnings.append(_warning(key, error))
            continue
        assert result is not None
        payload[key] = {
            "available": True,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        if result.returncode == 0:
            successes += 1
            if result.stderr.strip():
                warnings.append(
                    _warning(f"{key} wrote to stderr", result.stderr.strip())
                )
        else:
            warnings.append(_process_warning(key, result))
    return payload, _status(successes, len(warnings)), warnings


def _collect_package_managers(
    powershell: str | None, runner: CommandRunner, wrapper_path: str
) -> CollectorResult:
    payload: dict[str, object] = {}
    warnings: list[str] = []
    successes = 0

    winget = shutil.which("winget.exe") or shutil.which("winget")
    if winget is None:
        payload["winget"] = {
            "available": False,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "data": None,
        }
    else:
        export_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                suffix=".json", dir=Path(wrapper_path).parent, delete=False
            ) as temporary:
                export_path = temporary.name
            row, warning = _command_row(
                winget,
                [
                    "export",
                    "--output",
                    _windows_path_for_powershell(export_path),
                    "--include-versions",
                    "--accept-source-agreements",
                    "--disable-interactivity",
                ],
                parse_json=False,
                timeout=LONG_COMMAND_TIMEOUT,
                runner=runner,
                powershell=powershell,
                wrapper_path=wrapper_path,
            )
            if row["returncode"] == 0:
                try:
                    with open(export_path, encoding="utf-8") as export_file:
                        row["data"] = json.load(export_file)
                except (OSError, json.JSONDecodeError) as error:
                    warning = _warning("winget export JSON", error)
            payload["winget"] = row
            if warning is None:
                successes += 1
            else:
                warnings.append(warning)
        finally:
            if export_path is not None:
                try:
                    os.unlink(export_path)
                except OSError:
                    pass

    choco = shutil.which("choco.exe") or shutil.which("choco")
    row, warning = _command_row(
        choco,
        ["list", "--local-only", "--limit-output"],
        parse_json=False,
        timeout=COMMAND_TIMEOUT,
        runner=runner,
        powershell=powershell,
        wrapper_path=wrapper_path,
    )
    if choco is not None and row["returncode"] not in (0, None):
        row, warning = _command_row(
            choco,
            ["list", "--limit-output"],
            parse_json=False,
            timeout=COMMAND_TIMEOUT,
            runner=runner,
            powershell=powershell,
            wrapper_path=wrapper_path,
        )
    payload["chocolatey"] = row
    if choco is not None:
        if warning is None:
            successes += 1
        else:
            warnings.append(warning)

    scoop = (
        shutil.which("scoop.ps1") or shutil.which("scoop.cmd") or shutil.which("scoop")
    )
    row, warning = _command_row(
        scoop,
        ["export"],
        parse_json=False,
        timeout=COMMAND_TIMEOUT,
        runner=runner,
        powershell=powershell,
        wrapper_path=wrapper_path,
    )
    payload["scoop"] = row
    if scoop is not None:
        if warning is None:
            successes += 1
        else:
            warnings.append(warning)

    available_count = sum(
        bool(row["available"]) for row in payload.values() if isinstance(row, dict)
    )
    return (
        payload,
        _status(successes if available_count else 1, len(warnings)),
        warnings,
    )


_DEVELOPER_COMMANDS: tuple[tuple[str, tuple[str, ...], bool], ...] = (
    ("vscode_extensions", ("code", "--list-extensions", "--show-versions"), False),
    ("npm_global_packages", ("npm", "list", "--global", "--depth=0", "--json"), True),
    ("pipx_packages", ("pipx", "list", "--json"), True),
    ("cargo_crates", ("cargo", "install", "--list"), False),
    ("dotnet_global_tools", ("dotnet", "tool", "list", "--global"), False),
    (
        "python_user_packages",
        ("python", "-m", "pip", "list", "--user", "--format=json"),
        True,
    ),
    (
        "git_global_config",
        ("git", "config", "--global", "--list", "--show-origin"),
        False,
    ),
)


def _collect_developer_tools(
    powershell: str | None, runner: CommandRunner, wrapper_path: str
) -> CollectorResult:
    modules, module_warning = _powershell_json(
        powershell, _MODULES_SCRIPT, "powershell_modules", runner
    )
    payload: dict[str, object] = {"powershell_modules": modules, "tools": {}}
    warnings = [module_warning] if module_warning is not None else []
    successes = int(modules is not None)
    tools = payload["tools"]
    assert isinstance(tools, dict)
    for key, command, parse_json in _DEVELOPER_COMMANDS:
        executable = shutil.which(command[0])
        row, warning = _command_row(
            executable,
            command[1:],
            parse_json=parse_json,
            timeout=COMMAND_TIMEOUT,
            runner=runner,
            powershell=powershell,
            wrapper_path=wrapper_path,
        )
        tools[key] = row
        if executable is not None:
            if warning is None:
                successes += 1
            else:
                warnings.append(f"{key}: {warning}")
    return payload, _status(successes, len(warnings)), warnings


def _wsl_command_row(
    wsl: str,
    distribution: str,
    command: Sequence[str],
    runner: CommandRunner,
) -> tuple[dict[str, object], str | None]:
    result, error = _run_catching(
        [wsl, "--distribution", distribution, "--exec", *command],
        timeout=LONG_COMMAND_TIMEOUT,
        runner=runner,
    )
    if error is not None:
        return {
            "available": True,
            "returncode": None,
            "stdout": "",
            "stderr": error,
        }, error
    assert result is not None
    row: dict[str, object] = {
        "available": True,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
    if result.returncode != 0:
        return row, _process_warning(command[0], result)
    if result.stderr.strip():
        return row, _warning(f"{command[0]} wrote to stderr", result.stderr.strip())
    return row, None


_WSL_BASE_COMMANDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("os_release", ("cat", "/etc/os-release")),
    ("uname", ("uname", "-a")),
    ("environment", ("env",)),
    ("mounts", ("mount",)),
)
_WSL_OPTIONAL_COMMANDS: tuple[tuple[str, str], ...] = (
    ("snap", "snap list"),
    ("flatpak", "flatpak list"),
    ("npm", "npm list --global --depth=0 --json"),
    ("pipx", "pipx list --json"),
    ("cargo", "cargo install --list"),
    ("dotnet", "dotnet tool list --global"),
)
_WSL_PACKAGE_COMMANDS: tuple[tuple[str, str], ...] = (
    (
        "dpkg",
        "dpkg-query -W -f='${binary:Package}\\t${Version}\\t${Architecture}\\n'; apt-mark showmanual",
    ),
    ("rpm", "rpm -qa --qf '%{NAME}\\t%{VERSION}-%{RELEASE}\\t%{ARCH}\\n'"),
    ("apk", "apk info -vv"),
    ("pacman", "pacman -Q; pacman -Qqe"),
)


def _unavailable_wsl_command() -> dict[str, object]:
    return {
        "available": False,
        "returncode": None,
        "stdout": "",
        "stderr": "",
    }


def _collect_wsl(
    powershell: str | None, runner: CommandRunner, wrapper_path: str
) -> CollectorResult:
    wsl = shutil.which("wsl.exe") or shutil.which("wsl")
    if wsl is None:
        return (
            {"available": False, "distribution_list_verbose": "", "distributions": []},
            CollectorStatus.SUCCEEDED,
            [],
        )
    if powershell is None:
        return (
            {},
            CollectorStatus.FAILED,
            ["wsl enumeration: powershell.exe is unavailable"],
        )

    list_wrapper_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".ps1",
            dir=Path(wrapper_path).parent,
            delete=False,
        ) as list_wrapper:
            list_wrapper.write(
                "param([Parameter(Position=0,Mandatory=$true)][string]$Executable) "
                "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "
                "$OutputEncoding = [Console]::OutputEncoding; "
                "& $Executable --list --quiet; exit $LASTEXITCODE"
            )
            list_wrapper_path = list_wrapper.name
        quiet, quiet_error = _run_catching(
            [
                powershell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-File",
                _windows_path_for_powershell(list_wrapper_path),
                _windows_path_for_powershell(wsl),
            ],
            timeout=COMMAND_TIMEOUT,
            runner=runner,
        )
    except OSError as error:
        return {}, CollectorStatus.FAILED, [_warning("wsl list wrapper", error)]
    finally:
        if list_wrapper_path is not None:
            try:
                os.unlink(list_wrapper_path)
            except OSError:
                pass
    verbose, verbose_error = _run_catching(
        [wsl, "--list", "--verbose"], timeout=COMMAND_TIMEOUT, runner=runner
    )
    warnings: list[str] = []
    if quiet_error is not None:
        return (
            {},
            CollectorStatus.FAILED,
            [_warning("wsl distribution list", quiet_error)],
        )
    assert quiet is not None
    if quiet.returncode != 0:
        return (
            {},
            CollectorStatus.FAILED,
            [_process_warning("wsl distribution list", quiet)],
        )
    if quiet.stderr.strip():
        warnings.append(
            _warning("wsl distribution list wrote to stderr", quiet.stderr.strip())
        )
    if verbose_error is not None:
        warnings.append(_warning("wsl verbose list", verbose_error))
        verbose_stdout = ""
    else:
        assert verbose is not None
        verbose_stdout = verbose.stdout
        if verbose.returncode != 0:
            warnings.append(_process_warning("wsl verbose list", verbose))
        elif verbose.stderr.strip():
            warnings.append(
                _warning("wsl verbose list wrote to stderr", verbose.stderr.strip())
            )

    decoded_names = quiet.stdout.replace("\x00", "").lstrip("\ufeff")
    distributions = [
        line.strip().lstrip("* ").strip()
        for line in decoded_names.splitlines()
        if line.strip().lstrip("* ").strip()
    ]
    rows: list[dict[str, object]] = []
    distro_failures = 0
    for distribution in distributions:
        commands: dict[str, object] = {}
        distro_warnings: list[str] = []
        for key, command in _WSL_BASE_COMMANDS:
            row, warning = _wsl_command_row(wsl, distribution, command, runner)
            commands[key] = row
            if warning is not None:
                distro_warnings.append(f"{key}: {warning}")

        systemctl_available, _ = _wsl_command_row(
            wsl,
            distribution,
            ["sh", "-lc", "command -v systemctl"],
            runner,
        )
        if systemctl_available["returncode"] == 0:
            row, warning = _wsl_command_row(
                wsl,
                distribution,
                ["systemctl", "list-unit-files", "--state=enabled", "--no-pager"],
                runner,
            )
            commands["enabled_systemd_units"] = row
            if warning is not None:
                distro_warnings.append(f"enabled_systemd_units: {warning}")
        else:
            commands["enabled_systemd_units"] = _unavailable_wsl_command()
            if systemctl_available["returncode"] is None:
                distro_warnings.append(
                    f"systemctl probe: {systemctl_available['stderr']}"
                )

        package_script = ""
        package_key = "packages"
        for tool, script in _WSL_PACKAGE_COMMANDS:
            probe, _ = _wsl_command_row(
                wsl, distribution, ["sh", "-lc", f"command -v {tool}"], runner
            )
            if probe["returncode"] == 0:
                package_script = script
                package_key = f"{tool}_packages"
                break
            if probe["returncode"] is None:
                distro_warnings.append(f"{tool} probe: {probe['stderr']}")
        if package_script:
            row, warning = _wsl_command_row(
                wsl, distribution, ["sh", "-lc", package_script], runner
            )
            commands[package_key] = row
            if warning is not None:
                distro_warnings.append(f"{package_key}: {warning}")
        else:
            commands[package_key] = _unavailable_wsl_command()

        for tool, script in _WSL_OPTIONAL_COMMANDS:
            probe, _ = _wsl_command_row(
                wsl, distribution, ["sh", "-lc", f"command -v {tool}"], runner
            )
            if probe["returncode"] != 0:
                commands[tool] = _unavailable_wsl_command()
                if probe["returncode"] is None:
                    distro_warnings.append(f"{tool} probe: {probe['stderr']}")
                continue
            row, warning = _wsl_command_row(
                wsl, distribution, ["sh", "-lc", script], runner
            )
            commands[tool] = row
            if warning is not None:
                distro_warnings.append(f"{tool}: {warning}")

        if distro_warnings:
            distro_failures += 1
            warnings.extend(f"{distribution}: {item}" for item in distro_warnings)
        rows.append({"name": distribution, "commands": commands})

    payload = {
        "available": True,
        "distribution_list_verbose": verbose_stdout,
        "distributions": rows,
    }
    return payload, _status(1, int(bool(warnings))), warnings


_git_repos_backup_root: str | None = None


def _collect_git_repos(
    powershell: str | None, runner: CommandRunner, wrapper_path: str
) -> CollectorResult:
    """Inventory Code git repos (remote + branch) for Drive restore --with-github-clone."""
    # Backup root is injected by collect_machine_state via module global.
    backup_root = _git_repos_backup_root or str(Path.home())
    git = shutil.which("git")
    if git is None:
        return (
            {"repos": [], "repo_count": 0, "github_count": 0, "git_available": False},
            CollectorStatus.SUCCEEDED,
            ["git executable not found — git_repos inventory empty"],
        )
    code_root = Path(backup_root) / "Code"
    # Also handle case where backup_root itself is Code (unlikely) — ensure we scan Code.
    if not code_root.is_dir():
        # No Code directory — nothing to inventory, but not a failure.
        return (
            {"repos": [], "repo_count": 0, "github_count": 0, "git_available": True},
            CollectorStatus.SUCCEEDED,
            [],
        )
    repos: list[dict[str, object]] = []
    warnings: list[str] = []
    # Walk Code tree, pruning .git internals and ephemeral dirs.
    skip_dir_names = {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        ".hypothesis",
        "dist",
        "build",
        ".next",
        ".turbo",
    }
    # Components that indicate ephemeral/generated and should be skipped entirely.
    ephemeral_parts = {"logs", ".tmp", ".tmp-pytest-clean", "omp-edge-profile"}
    for dirpath, dirnames, _ in os.walk(code_root, topdown=True, followlinks=False):
        # Prune ephemeral / cache dirs early to avoid descending into huge trees.
        # Also prune hidden ephemeral .git worktree leaves will be handled via .git detection below.
        # Remove excluded dirnames in-place.
        # Filter dirnames: skip known caches and ephemeral.
        original_dirnames = list(dirnames)
        dirnames[:] = [d for d in dirnames if d not in skip_dir_names and d not in ephemeral_parts]
        # Also skip any dirname that matches "*.egg-info"
        dirnames[:] = [d for d in dirnames if not d.endswith(".egg-info")]
        # Skip nested worktree detection: if any part of relative path contains ephemeral, prune.
        try:
            rel_dir = os.path.relpath(dirpath, backup_root).replace("\\", "/")
        except ValueError:
            rel_dir = dirpath
        if any(part in ephemeral_parts for part in Path(rel_dir).parts):
            dirnames[:] = []
            continue
        # Check if this directory contains a .git dir (is a repo root)
        if ".git" in original_dirnames:
            git_dir = Path(dirpath) / ".git"
            if git_dir.is_dir() or git_dir.is_file():
                # This dirpath is a repo root.
                relative_path = os.path.relpath(dirpath, backup_root).replace("\\", "/")
                # Skip ephemeral nested repos (e.g., Code/open-law-notes/logs/omp-harness/... )
                if any(part in ephemeral_parts for part in Path(relative_path).parts):
                    continue
                remote_url = ""
                has_remote = False
                has_github = False
                branch = ""
                sha = ""
                # remote get-url origin
                result, error = _run_catching([git, "-C", dirpath, "remote", "get-url", "origin"], timeout=COMMAND_TIMEOUT, runner=runner)
                if error is None and result is not None and result.returncode == 0:
                    remote_url = result.stdout.strip()
                    has_remote = bool(remote_url)
                    has_github = "github.com" in remote_url.lower()
                elif error is not None:
                    warnings.append(f"{relative_path}: remote probe: {error}")
                # branch
                result2, error2 = _run_catching([git, "-C", dirpath, "rev-parse", "--abbrev-ref", "HEAD"], timeout=COMMAND_TIMEOUT, runner=runner)
                if error2 is None and result2 is not None and result2.returncode == 0:
                    branch = result2.stdout.strip()
                # sha
                result3, error3 = _run_catching([git, "-C", dirpath, "rev-parse", "HEAD"], timeout=COMMAND_TIMEOUT, runner=runner)
                if error3 is None and result3 is not None and result3.returncode == 0:
                    sha = result3.stdout.strip()
                repos.append(
                    {
                        "relative_path": relative_path,
                        "remote_url": remote_url,
                        "has_remote": has_remote,
                        "has_github": has_github,
                        "branch": branch,
                        "sha": sha,
                    }
                )
                # Do not descend into this repo's subdirectories for further repo detection
                # (nested .git worktrees inside logs are already pruned via ephemeral_parts).
                # Keep dirnames pruned to avoid walking .git internals (already removed).
                continue
    # Sort for determinism
    repos.sort(key=lambda r: str(r["relative_path"]))
    github_count = sum(1 for r in repos if r["has_github"])
    payload = {
        "repos": repos,
        "repo_count": len(repos),
        "github_count": github_count,
        "git_available": True,
    }
    return payload, CollectorStatus.SUCCEEDED, warnings


_COLLECTORS: dict[str, Collector] = {
    "system": _single_powershell_collector(_SYSTEM_SCRIPT, "system"),
    "windows_apps": _single_powershell_collector(_WINDOWS_APPS_SCRIPT, "windows_apps"),
    "package_managers": _collect_package_managers,
    "developer_tools": _collect_developer_tools,
    "windows_features": _collect_windows_features,
    "services": _single_powershell_collector(_SERVICES_SCRIPT, "services"),
    "scheduled_tasks": _single_powershell_collector(
        _SCHEDULED_TASKS_SCRIPT, "scheduled_tasks"
    ),
    "drivers": _single_powershell_collector(_DRIVERS_SCRIPT, "drivers"),
    "network": _collect_network,
    "environment": _single_powershell_collector(_ENVIRONMENT_SCRIPT, "environment"),
    "wsl": _collect_wsl,
    "git_repos": _collect_git_repos,
}


def _relative_output(name: str) -> str:
    return f"{MACHINE_STATE_DIRECTORY}/{name}.json"


def _failed_outcome(name: str, output_path: Path, warning: str) -> CollectorOutcome:
    retained = output_path.is_file()
    return CollectorOutcome(
        name=name,
        status=CollectorStatus.FAILED,
        output_file=_relative_output(name) if retained else None,
        warnings=(warning,),
        previous_output_retained=retained,
    )


def _outcome_row(outcome: CollectorOutcome) -> dict[str, object]:
    row = asdict(outcome)
    row["status"] = outcome.status.value
    row["warnings"] = list(outcome.warnings)
    return row


def collect_machine_state(
    backup_root: str,
    collector_names: Sequence[str],
    *,
    runner: CommandRunner = run_command,
) -> list[CollectorOutcome]:
    """Refresh selected inventories while isolating every collector failure."""
    global _git_repos_backup_root
    _git_repos_backup_root = backup_root
    started_at = _utc_now()
    output_dir = Path(backup_root) / MACHINE_STATE_DIRECTORY
    snapshot_path = output_dir / "snapshot.json"
    snapshot_existed = snapshot_path.is_file()
    outcomes: list[CollectorOutcome] = []
    reconciliation_warnings: list[str] = []

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        warning = _warning("Could not create machine-state directory", error)
        logger.warning(warning)
        _git_repos_backup_root = None
        return [
            CollectorOutcome(
                name="snapshot",
                status=CollectorStatus.FAILED,
                output_file=_relative_output("snapshot") if snapshot_existed else None,
                warnings=(warning,),
                previous_output_retained=snapshot_existed,
            )
        ]

    enabled = set(collector_names)
    for name in MACHINE_STATE_COLLECTORS:
        if name in enabled:
            continue
        path = output_dir / f"{name}.json"
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            warning = _warning(
                f"Could not remove disabled collector output {path}", error
            )
            logger.warning(warning)
            reconciliation_warnings.append(warning)

    powershell = shutil.which("powershell.exe")
    wrapper_path: str | None = None
    wrapper_warning: str | None = None
    if collector_names:
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".ps1",
                dir=output_dir,
                delete=False,
            ) as wrapper:
                wrapper.write(POWERSHELL_WRAPPER)
                wrapper_path = wrapper.name
        except OSError as error:
            wrapper_warning = _warning(
                "Could not create machine-state command wrapper", error
            )
            logger.warning(wrapper_warning)

    try:
        for name in collector_names:
            output_path = output_dir / f"{name}.json"
            if wrapper_warning is not None:
                outcomes.append(_failed_outcome(name, output_path, wrapper_warning))
                continue
            assert wrapper_path is not None
            try:
                payload, status, warnings = _COLLECTORS[name](
                    powershell, runner, wrapper_path
                )
                if status is CollectorStatus.FAILED:
                    warning = "; ".join(warnings) or f"{name} failed"
                    outcomes.append(_failed_outcome(name, output_path, warning))
                    continue
                envelope = {
                    "schema_version": 1,
                    "collector": name,
                    "collected_at": _utc_now(),
                    "data": payload,
                }
                atomic_write_json(output_path, envelope)
                outcomes.append(
                    CollectorOutcome(
                        name=name,
                        status=status,
                        output_file=_relative_output(name),
                        warnings=tuple(warnings),
                        previous_output_retained=False,
                    )
                )
            except Exception as error:
                warning = _warning(f"Unexpected {name} collector failure", error)
                logger.warning(warning)
                outcomes.append(_failed_outcome(name, output_path, warning))
    finally:
        if wrapper_path is not None:
            try:
                os.unlink(wrapper_path)
            except OSError:
                pass

    snapshot = {
        "schema_version": 1,
        "started_at": started_at,
        "completed_at": _utc_now(),
        "hostname": socket.gethostname(),
        "collectors": list(collector_names),
        "reconciliation_warnings": reconciliation_warnings,
        "outcomes": [_outcome_row(outcome) for outcome in outcomes],
    }
    try:
        atomic_write_json(snapshot_path, snapshot)
    except Exception as error:
        warning = _warning("Could not write machine-state snapshot metadata", error)
        logger.warning(warning)
        outcomes.append(
            CollectorOutcome(
                name="snapshot",
                status=CollectorStatus.FAILED,
                output_file=_relative_output("snapshot") if snapshot_existed else None,
                warnings=(warning,),
                previous_output_retained=snapshot_existed,
            )
        )
    finally:
        _git_repos_backup_root = None
    return outcomes
