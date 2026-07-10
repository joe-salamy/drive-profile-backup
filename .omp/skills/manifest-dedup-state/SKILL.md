---
name: manifest-dedup-state
description: Use when changing manifest schema, MD5 computation, dedup upload decisions, stale-entry pruning, or local state paths under ~/.drive-backup.
---

# Manifest Dedup State

## Scope

Use this skill for `src/drive_backup/dedup.py`, manifest-related `engine.py` paths, and tests in `tests/test_dedup.py` or manifest sections of `tests/test_engine.py`.

## Manifest contract

- Manifest keys are scanner `relative_path` values.
- Each entry stores `md5`, `size`, `mtime`, `drive_file_id`, `drive_parent_id`, and `last_uploaded`.
- Load missing or corrupted manifests as empty instead of crashing a backup.
- Save must create parent directories.
- Fast path: matching mtime and size skips upload without recomputing MD5.
- If mtime differs, compute MD5; matching MD5 skips upload, differing MD5 uploads.
- If MD5 cannot be computed, upload rather than silently losing changes.
- Pruning removes manifest entries only after Drive trash succeeds; dry-run prune reports stale entries without saving removals.

## Change pattern

1. Treat manifest schema changes as migrations: preserve old manifests or intentionally document incompatibility.
2. Keep MD5 streaming; do not read whole files into memory.
3. Do not use Drive IDs as primary manifest keys; local relative path is the identity used across scanner/report/tests.
4. Update report fields when adding new dedup/prune reasons.

## Verification

```powershell
pytest tests/test_dedup.py tests/test_engine.py
```

Add edge tests for missing files, corrupted JSON, unchanged content with changed mtime, and prune failure paths.
