# Machine State Snapshots Implementation Summary

## Plan

- Approved plan: `.omp/worktree-flow/20260730-101407-machine-state-snapshots/plan.md`

## Worktree and branch

- Worktree: `/mnt/c/Users/joesa/code/drive-profile-backup-machine-state-snapshots`
- Branch: `feature/machine-state-snapshots`
- Commit: `27a8d47c268c029ad603cbb7292c6719a06ecc88` (`Add machine state snapshots`)
- Workflow artifacts under `.omp/handoff/` and `.omp/worktree-flow/` remain untracked and were not committed.

## Changed files

- `README.md`
- `config.example.yaml`
- `src/drive_backup/cli.py`
- `src/drive_backup/config.py`
- `src/drive_backup/dedup.py`
- `src/drive_backup/engine.py`
- `src/drive_backup/machine_state.py` (new)
- `src/drive_backup/report.py`
- `src/drive_backup/utils.py`
- `tests/test_cli.py`
- `tests/test_config.py`
- `tests/test_dedup.py`
- `tests/test_engine.py`
- `tests/test_machine_state.py` (new)
- `tests/test_report.py`

## Behavior changes

- Promoted atomic JSON replacement to `drive_backup.utils.atomic_write_json(path, data)` and migrated manifest saving to it.
- Added the ordered `MACHINE_STATE_COLLECTORS` contract and validated `machine_state_collectors` YAML values for type, unknown names, duplicates, order preservation, and empty selection.
- Added default-on generation under `<backup_root>/_machine_state/` for system, Windows applications, package managers, developer tools, Windows features, services, scheduled tasks, drivers, network, environment, WSL, and snapshot metadata.
- Added typed collector outcomes and statuses, shell-free command execution, fixed PowerShell wrappers for command shims and WSL enumeration, per-command timeouts, independent optional-tool resolution, atomic output replacement, prior-output retention, disabled-output reconciliation, and per-collector failure isolation.
- Added broad unredacted Windows/WSL diagnostic payloads without copying arbitrary external credential or configuration files.
- Refresh now occurs after manifest load and before Drive authentication/scan, including dry runs. An unavailable backup root is not created and produces a non-fatal synthetic snapshot failure.
- Added path-specific prune protection for failed enabled collectors; known disabled collector outputs remain pruneable after reconciliation.
- Added `--skip-machine-state`; skipping refresh leaves existing generated files eligible for normal scan/dedup/upload/prune evaluation.
- Added machine-state status rows and unconditional partial/failed warnings to CLI and JSON reports without counting collector warnings as file errors.
- Documented default refresh, configuration, dry-run/skip semantics, scanner precedence, WSL startup behavior, continuation on failure, and the unredacted-secret risk.

## Tests and checks

- Focused suite: `python.exe -m pytest tests/test_machine_state.py tests/test_config.py tests/test_cli.py tests/test_engine.py tests/test_report.py` — **95 passed**.
- Full suite: `python.exe -m pytest` — **144 passed**.
- Ruff: `python.exe -m ruff check .` — **passed**.
- Mypy: `python.exe -m mypy src tests scripts` — **passed; no issues in 23 source files**.
- Black: `python.exe -m black --check src tests scripts` — **passed; 23 files unchanged**.
- Real Windows PowerShell smoke, isolated temporary `config.yaml`, collectors `environment` and `wsl`, `drive-backup --dry-run` — **exit 0**; created and scanned `environment.json`, `wsl.json`, and `snapshot.json`; retained a non-empty process `PATH`; inventoried both registered WSL distributions with command mappings.
- Follow-up `drive-backup --dry-run --skip-machine-state` — **exit 0**; summary reported `Not refreshed`, all three files remained scanned, and every `LastWriteTimeUtc` was unchanged.
- Temporary smoke directory was removed.

## Skipped checks

- No plan-required test or quality gate was skipped.
- Workspace Pyright diagnostics were also inspected. It reported three pre-existing unresolved optional Google client imports in `src/drive_backup/drive_api.py` because that language-server environment lacks those packages; the repository mypy gate passed and the changed modules had no diagnostics.

## Decisions and tradeoffs

- Kept collectors in one typed module because the plan establishes one subprocess/inventory boundary and there was no existing abstraction to reuse.
- Used fixed source scripts and separate argv values for resolved executables, shim paths, distro names, and tool arguments. Windows PowerShell 5.1 required a fixed temporary `.ps1` wrapper for WSL quiet-list enumeration; passing values after `-Command` was not reliable in the real smoke test.
- Normalized NUL characters from `wsl.exe --list --quiet` before deriving distro names. Raw verbose list output remains unmodified in the payload.
- Missing optional tools are successful inventory facts. Failed invoked components yield partial status when another current payload component succeeded; no current payload yields failed status and preserves prior output.
- Updated two manifest tests to regex-escape Windows temporary paths because pytest 9 interprets `match=` as a regular expression; this was required for the full Windows suite and does not change product behavior.

## Assumptions and residual risks

- Supported host remains Windows PowerShell with Python 3.11+; no PowerShell 7 or non-Windows collector implementation was added.
- Machine-state JSON is intentionally unredacted and can include environment secrets/API keys, network identifiers, usernames, domains, service/task details, serial numbers, Git configuration origins, and package sources.
- WSL collection can start every registered distro and runs multiple inventory commands per distro; slow or unhealthy distributions can consume the configured 300-second per-command timeout before returning partial results.
- Scanner exclusions and size rules still take precedence over generated artifacts; excluded snapshots are generated locally but are not uploaded.
- No Drive API behavior or dependency set changed.
