---
name: backup-pipeline-flow
description: Use when changing the drive-backup CLI, BackupEngine orchestration, upload/prune flow, or progress/report handoff between scanner, dedup, Drive API, and report generation.
---

# Backup Pipeline Flow

## Read first

Use this skill before editing `src/drive_backup/cli.py`, `engine.py`, or behavior that crosses module boundaries.

## Repo flow

- Entry point: `drive-backup = drive_backup.cli:main` in `pyproject.toml`.
- CLI loads `config.yaml`, builds `BackupEngine`, runs `engine.run()`, and prints report summaries.
- Engine order: `scanner.scan(config)` -> `dedup.needs_upload()` -> `DriveAPI` upload/update/trash -> manifest update -> `report.generate_report()` and `save_report()`.
- Dry-run must not authenticate, upload, prune, or save manifest changes for would-be operations.
- `--full` ignores manifest upload decisions; `--prune` depends on manifest state and is rejected with `--full`.
- Progress callbacks use action strings consumed by `cli.py`; update CLI display and tests together when adding actions.

## Safe change pattern

1. Start at the caller-visible contract: CLI flags, report fields, manifest side effects.
2. Keep the engine as the only orchestrator; do not move Drive concerns into scanner/dedup/report.
3. Preserve report dictionaries as plain JSON-serializable values.
4. Mock Drive behavior in tests; never require real Google credentials for unit tests.

## Verification

Run focused tests for touched flow:

```powershell
pytest tests/test_cli.py tests/test_engine.py tests/test_report.py
```

Run `drive-backup --dry-run` only when local `config.yaml` is safe for the current machine.

## Sources

- Python entry points and `pyproject.toml`: https://packaging.python.org/en/latest/guides/writing-pyproject-toml/
- Rich progress API used by `cli.py`: https://rich.readthedocs.io/en/stable/progress.html
