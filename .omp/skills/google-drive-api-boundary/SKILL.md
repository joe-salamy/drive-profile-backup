---
name: google-drive-api-boundary
description: Use when changing Google Drive authentication, Drive folder lookup/creation, upload/update/trash calls, retry behavior, OAuth token paths, or Drive API scopes.
---

# Google Drive API Boundary

## Scope

Use this skill for `src/drive_backup/drive_api.py`, Drive-facing `engine.py` code, OAuth config fields, and `tests/test_drive_api.py`.

## Invariants

- OAuth scope is `https://www.googleapis.com/auth/drive.file`; do not broaden it without explicit need.
- `credentials.json` and token files are local secrets/state and must not be committed.
- Authentication is lazy enough that dry-run backup paths do not require Google credentials.
- Folder cache key is `(name, parent_id)` so same-named folders under different parents do not collide.
- Upload/update/trash operations go through `RateLimiter.wait()` and retry handling.
- Large files use resumable upload when over `resumable_threshold_mb`.
- Tests mock Google API clients; unit tests must not perform network calls.

## Change pattern

1. Keep raw Google client details inside `DriveAPI`; engine should call wrapper methods only.
2. Preserve `drive_parent_folder_name/profile_name` folder nesting from the README.
3. Return Drive metadata dictionaries with IDs/checksums needed by manifest/report code.
4. On retry changes, test retryable and non-retryable failures separately.

## Verification

```powershell
pytest tests/test_drive_api.py tests/test_engine.py
```

Manual OAuth checks require a real Google Cloud desktop OAuth client and are not required for unit verification.

## Sources

- Drive Python quickstart and OAuth setup: https://developers.google.com/workspace/drive/api/quickstart/python
- Drive upload guidance: https://developers.google.com/workspace/drive/api/guides/manage-uploads
- Drive scopes guidance: https://developers.google.com/workspace/drive/api/guides/api-specific-auth
