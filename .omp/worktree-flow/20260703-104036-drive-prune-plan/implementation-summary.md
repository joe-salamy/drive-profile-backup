# Drive Prune Implementation Summary

## Plan

- Plan path: `.omp/worktree-flow/20260703-104036-drive-prune-plan/plan.md`
- Worktree path: `C:/Users/joesa/Code/drive-profile-backup-plan`
- Branch: `feature/plan`
- Commit SHA: `5979fe332dccf8d59326920de366e5e49899e438`

## Changed Files

- `src/drive_backup/dedup.py`
- `src/drive_backup/drive_api.py`
- `src/drive_backup/report.py`
- `src/drive_backup/engine.py`
- `src/drive_backup/cli.py`
- `tests/test_dedup.py`
- `tests/test_drive_api.py`
- `tests/test_report.py`
- `tests/test_engine.py`
- `tests/test_cli.py`

## Behavior Changes

- Added `Manifest.remove(relative_path)` so stale manifest entries can be removed only after successful Drive trash operations.
- Added `DriveAPI.trash_file(file_id)` backed by `files().update(fileId=..., body={"trashed": True}, fields="id, name, trashed")`; this uses Google Drive trash, not permanent delete.
- Extended report schema with prune fields: `prune_enabled`, `files_pruned`, `files_prune_failed`, `total_bytes_pruned`, `total_size_pruned_human`, `prune_skipped_reason`, `pruned_files`, and `prune_error_files`.
- Added `BackupEngine(..., prune=False)` support. When prune is enabled, the engine identifies stale manifest entries after scan/upload processing and before manifest save.
- Stale means the manifest key no longer resolves to a local file path. Existing local files are not pruned just because scanner exclusions, size limits, symlinks, or unreadable status skip them.
- Dry-run prune records would-prune entries in the report but does not authenticate to Drive, does not trash Drive files, and does not save manifest changes.
- Real prune calls `trash_file()` for stale entries with Drive IDs, records successful prunes, then removes those entries from the manifest before save.
- Real prune keeps manifest entries and records `PruneError` rows when Drive trash fails or the manifest entry has an empty Drive file ID.
- Real prune is skipped when `--full` is active, or when non-dry-run backup processing recorded file/upload errors earlier in the same run.
- Added CLI `--prune`; `--full --prune` exits via argparse because prune needs the existing manifest.
- CLI banners now distinguish dry-run prune and real prune.
- CLI summary now renders prune totals, skipped reason, top 10 pruned/would-prune rows by size, verbose full prune row listing sorted by relative path, and prune failure count.

## Tests and Checks Run

Environment note: no repository virtualenv existed in this worktree. Initial `python -m pytest ...` failed with `ModuleNotFoundError: No module named 'drive_backup'`; rerunning with `PYTHONPATH=src` exposed missing runtime dependencies (`rich`, `google-auth`, `googleapiclient`). `python -m pip install -e .[dev]` initially failed because pip required an active virtualenv, then succeeded with `PIP_REQUIRE_VIRTUALENV=false`.

Passing checks after dependencies were installed:

- `PYTHONPATH=src python -m pytest tests/test_dedup.py tests/test_drive_api.py tests/test_report.py tests/test_engine.py tests/test_cli.py`
  - Result: `64 passed in 0.58s`
- `PYTHONPATH=src python -m mypy src tests`
  - Result: `OK`
- `python -m ruff check src tests`
  - Result: `OK`
- `python -m black --check src tests`
  - Result: `17 files would be left unchanged`

Formatter action run before final checks:

- `python -m black src/drive_backup/dedup.py src/drive_backup/drive_api.py src/drive_backup/report.py src/drive_backup/engine.py src/drive_backup/cli.py tests/test_dedup.py tests/test_drive_api.py tests/test_report.py tests/test_engine.py tests/test_cli.py`
  - Result: reformatted `tests/test_drive_api.py`, `src/drive_backup/cli.py`, and `src/drive_backup/engine.py`.

## Behavioral Proof Covered by Tests

- Manifest removal returns the removed entry, clears the manifest key, and returns `None` for missing keys.
- Drive trash calls `files().update(fileId="file_123", body={"trashed": True}, fields="id, name, trashed")`, waits on the rate limiter, and returns the Drive response.
- Report generation serializes prune totals, pruned file rows, and prune error rows.
- Dry-run with prune disabled ignores stale manifest entries.
- Dry-run prune reports stale `old/moved.txt`, records one would-prune row, and leaves the manifest unchanged on disk.
- Real prune success sends `trash_file("drive_old")`, reports one pruned file, and removes `old/file.txt` from the saved manifest.
- Real prune failure records `files_prune_failed == 1`, includes error text `trash failed`, and keeps the stale manifest entry.
- Real prune is skipped after a backup upload error and does not call `trash_file()`.
- CLI passes `prune=True` to `BackupEngine` for `--dry-run --prune`.
- CLI rejects `--full --prune` with nonzero `SystemExit`.
- CLI verbose prune summary renders every would-prune relative path and the `Files to prune:` title.
- CLI prune summary renders dry-run labels `Files to prune`/`Size to prune` and real-run labels `Files pruned`/`Size pruned`.

## Skipped Checks

- No checks from the approved plan were skipped after installing dependencies. The initial non-`PYTHONPATH` pytest invocation and missing-dependency run failed due to local environment setup, then the required focused commands were rerun successfully.

## Implementation Decisions and Tradeoffs

- Drive pruning uses reversible trash updates only; no permanent delete fallback exists.
- Stale detection uses filesystem existence for the manifest key’s local path, not scanner eligibility, to avoid trashing Drive copies of files that still exist locally but are currently excluded or skipped.
- Prune runs after scan/upload processing so it can skip real trashing when upload errors make the current backup incomplete.
- Dry-run prune reuses normal report serialization instead of progress callbacks because stale manifest entries are not scanner `FileEntry` objects.
- Empty Drive file IDs are treated as prune failures and are left in the manifest so the bad state remains visible and retryable.

## Assumptions, Blockers, Risks, Follow-up

- Assumption: report consumers can tolerate additional top-level prune keys in the JSON report.
- Assumption: Drive API `files().update(..., body={"trashed": True})` is accepted under the existing `drive.file` scope for files this app owns or can access.
- Known risk: installing dependencies with `PIP_REQUIRE_VIRTUALENV=false` changed the active Python environment, not the repository tree. No dependency metadata files appeared in `git status`.
- Known risk: direct engine callers can still pass `full=True, prune=True`; the engine now guards this combination by setting `prune_skipped_reason` instead of pruning.
- Follow-up: none required by the approved plan.
