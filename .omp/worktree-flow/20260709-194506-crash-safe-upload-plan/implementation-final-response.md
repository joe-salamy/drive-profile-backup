Implemented and committed the approved crash-safe upload plan.

Decision:
- Completed implementation in current worktree only.
- Commit created: `e73e8fa0eb0cd1f290f9998ff40e67081913d2fc`
- Branch: `feature/crash-safe-upload-plan`
- Worktree: `/mnt/c/Users/joesa/code/drive-profile-backup-crash-safe-upload-plan`

Changed files committed:
- `src/drive_backup/dedup.py`
  - Added atomic manifest JSON writes via same-directory temp file, `fsync`, and `os.replace`.
  - Preserved existing manifest schema and load behavior.
- `src/drive_backup/engine.py`
  - Added `ManifestProgressError`.
  - Persisted manifest immediately after successful upload/update.
  - Persisted manifest immediately after successful prune removal.
  - Aborts on manifest checkpoint failure instead of continuing Drive side effects.
  - Reconciles same-name Drive files before creating new uploads.
- `src/drive_backup/drive_api.py`
  - Added Drive query value escaping helper.
  - Added `find_file_by_name_and_parent()`.
  - Reused helper in folder lookup.
- `tests/test_dedup.py`
  - Added atomic save failure/success and corrupted temp sibling coverage.
- `tests/test_drive_api.py`
  - Added Drive lookup wrapper coverage, absent lookup behavior, and escaping coverage.
- `tests/test_engine.py`
  - Added mid-run manifest persistence, orphan update, lookup miss upload, lookup failure no-duplicate, prune checkpoint, and fatal checkpoint failure coverage.

Verification run:
- `/tmp/drive-backup-venv/bin/python -m pytest tests/test_dedup.py tests/test_drive_api.py tests/test_engine.py`
  - `50 passed in 1.41s`
- `/tmp/drive-backup-venv/bin/ruff check .`
  - `OK`
- `/tmp/drive-backup-venv/bin/mypy src tests scripts`
  - `OK`
- `/tmp/drive-backup-venv/bin/black --check src tests scripts`
  - `18 files would be left unchanged`

Handoff:
- Wrote `.omp/handoff/implementation-summary.md`.
- It remains untracked, as required.

Final status:
- Committed implementation files are clean.
- Remaining untracked workflow artifacts:
  - `.omp/handoff/`
  - `.omp/worktree-flow/20260709-194506-crash-safe-upload-plan/`

Skipped:
- No real Google OAuth/network Drive run, per approved plan.
- No full `pytest` suite outside the focused plan targets; quality gates were run.
