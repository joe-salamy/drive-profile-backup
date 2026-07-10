# Crash Safe Upload Plan

## Context

User asked to strengthen `drive-backup` so a backup run is much safer if the process is cut midway through an upload. The plan optimizes for duplicate prevention while preserving backward compatibility with existing manifests: local state writes become crash-safe, completed upload progress is persisted during the run, and reruns reconcile likely orphaned Drive files before creating new files.

## Findings

- `src/drive_backup/engine.py:96-121`: `BackupEngine.run()` scans and processes every file before saving `self.manifest` once at the end. A kill after successful uploads but before line 121 loses those manifest updates on disk.
- `src/drive_backup/engine.py:215-233`: `_process_file()` catches ordinary upload exceptions, records `files_skipped_error`, and continues; it does not catch process termination, `SIGKILL`, or a hard cut.
- `src/drive_backup/engine.py:323-367`: `_upload_file()` resolves the Drive parent, chooses resumable upload by `config.resumable_threshold_bytes`, updates an existing Drive ID for `content_changed`/`size_changed`/`md5_error`, creates a new Drive file otherwise, then writes the returned Drive ID/checksum into the in-memory manifest.
- `src/drive_backup/dedup.py:19-28`: `ManifestEntry` has exactly `md5`, `size`, `mtime`, `drive_file_id`, `drive_parent_id`, and `last_uploaded`; adding required fields would break old manifests because `Manifest.load()` constructs `ManifestEntry(**entry_data)`.
- `src/drive_backup/dedup.py:61-76`: `Manifest.save()` creates parent directories but writes JSON directly to the final path. `src/drive_backup/dedup.py:47-49` treats corrupted JSON as an empty manifest, which can trigger broad re-upload after a cut during save.
- `src/drive_backup/drive_api.py:97-129`: `DriveAPI.get_or_create_folder()` already uses an escaped Drive query and caches by `(name, parent_id)`. No existing file-level lookup wrapper exists.
- `src/drive_backup/drive_api.py:138-224`: `upload_file()` uses Drive `files().create()`, `update_file()` uses `files().update()`, both return `id, name, md5Checksum, size`, and resumable uploads are not resumable across process restarts because no session URL is persisted.
- `src/drive_backup/report.py:215-218`: report JSON uses the same direct `open(path, "w")` pattern as the manifest; a cut during report save can corrupt only a generated report, not backup identity state.

## Approach

### 1. Make manifest writes atomic without changing the manifest schema

Edit `src/drive_backup/dedup.py`.

- Keep `ManifestEntry` unchanged: `md5: str`, `size: int`, `mtime: float`, `drive_file_id: str`, `drive_parent_id: str`, `last_uploaded: str`.
- Keep `Manifest.save(path: str) -> None` as the only public save method; change its implementation to write the same JSON shape as today (`version: 1`, `updated_at`, `file_count`, `files`) to a temporary file in the same directory, then atomically replace the target.
- Add a private helper in `dedup.py`:
  - Exact signature: `_atomic_write_json(path: str, data: dict[str, object]) -> None`.
  - Expand `~` before calling it, and treat an empty parent directory as `"."` so `Manifest.save("manifest.json")` remains valid.
  - Create the parent directory before writing.
  - Use `tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=parent, prefix=f".{basename}.", suffix=".tmp", delete=False)` to create the temp file on the same filesystem as the target.
  - `json.dump(data, tmp, indent=2)`, then `tmp.flush()`, then `os.fsync(tmp.fileno())`, then close the temp file, then `os.replace(temp_path, path)`.
  - On any exception before successful `os.replace`, including an `os.replace` failure, best-effort delete `temp_path` and re-raise; never open/truncate the existing target path directly.
- Do not change `Manifest.load()` behavior for corrupted target manifests: invalid target JSON still logs and returns an empty manifest. Do not add new required entry fields or top-level state.

Dependency: this step must land before per-file persistence so every new checkpoint is crash-safe.

### 2. Persist manifest progress after each successful Drive side effect

Edit `src/drive_backup/engine.py`.

- Add a private exception near the `logger` declaration:
  - Exact class: `class ManifestProgressError(RuntimeError):`
  - Purpose: manifest checkpoint failures are fatal safety errors, not ordinary per-file upload errors that the engine should swallow and continue past.
- Add a small private method on `BackupEngine`:
  - Exact signature: `def _save_manifest_progress(self) -> None:`
  - Body: call `self.manifest.save(self.config.manifest_path)`.
  - Wrap any exception from `Manifest.save()` as `ManifestProgressError("Could not save manifest progress")` using exception chaining.
  - Do not call this method from dry-run paths.
- In `_process_file()`, add `except ManifestProgressError: raise` immediately before the existing broad `except Exception as e:` upload-error block. This stops the run instead of continuing to create more Drive side effects when durable local state is unavailable.
- In `_upload_file()`, after `self.manifest.set(...)` succeeds, immediately call `self._save_manifest_progress()` before returning to `_process_file()`.
  - This makes a completed upload/update durable before the next file starts.
  - If the save raises, the run aborts through `ManifestProgressError`. The just-uploaded Drive object may be unrecorded on disk, but atomic manifest save preserves the previous manifest and Step 3 reconciliation prevents a blind duplicate on the next run.
- In `_prune_stale_manifest_entries()`, after `trash_file()` succeeds for a non-dry stale entry, remove the entry from the in-memory manifest, call `self._save_manifest_progress()`, then call `_record_pruned_file(relative_path, entry)`.
  - If `_save_manifest_progress()` raises after `trash_file()` succeeds, restore the removed entry to `self.manifest.entries[relative_path] = entry` and re-raise `ManifestProgressError`; do not count that item as pruned in the report.
  - This aborts the run with the on-disk manifest still valid. A later run may retry trash for that entry, which is safer than losing or corrupting the whole manifest.
- Keep the final `self.manifest.save(self.config.manifest_path)` in `run()` unchanged. With atomic save it is harmless and keeps the end-of-run manifest timestamp current.
- Do not add checkpoint files, upload journals, or new manifest sections. The durable state is still the existing manifest entries.

Dependency: requires Step 1. Independent of Step 3 except that both touch `_upload_file()`.

### 3. Reconcile existing Drive files before creating a new Drive file

Edit `src/drive_backup/drive_api.py` and `src/drive_backup/engine.py`.

- In `drive_api.py`, add a private query-escape helper near `FOLDER_MIME`/`DriveAPI`:
  - Exact signature: `def _escape_drive_query_value(value: str) -> str:`
  - Exact behavior: return `value.replace("\\\\", "\\\\\\\\").replace("'", "\\\\'")`.
- Update `DriveAPI.get_or_create_folder()` to use `_escape_drive_query_value(name)` instead of its inline `safe_name = ...` expression. The resulting folder query must be byte-for-byte equivalent for existing names.
- Add a new Drive wrapper method on `DriveAPI`:
  - Exact signature:
    ```python
    def find_file_by_name_and_parent(
        self,
        name: str,
        parent_id: str,
    ) -> dict[str, Any] | None:
    ```
  - Query exact shape after escaping `name`: `name='<safe_name>' and trashed=false and '<parent_id>' in parents and mimeType!='application/vnd.google-apps.folder'`.
  - Call exact Google client shape: `self.service.files().list(q=query, spaces="drive", fields="files(id, name, md5Checksum, size)").execute()`.
  - Return `None` when `results.get("files", [])` is empty; otherwise return the first metadata dict. Do not create, update, rate-limit, or mutate Drive in this method.
  - Let list exceptions propagate. The engine will treat lookup failure as an upload error rather than blindly creating a possible duplicate.
- In `BackupEngine._upload_file()`, keep the existing manifest-ID update branch first:
  - If `existing and existing.drive_file_id and reason in ("content_changed", "size_changed", "md5_error")`, continue to call `self.drive.update_file(existing.drive_file_id, file.path, resumable=resumable)`.
- Replace the current blind create branch with lookup-then-update-or-create:
  - Compute `filename = os.path.basename(file.path)`.
  - Call `found = self.drive.find_file_by_name_and_parent(filename, parent_id)`.
  - If `found is not None`, call `self.drive.update_file(found["id"], file.path, resumable=resumable)` and use that result for `Manifest.set(...)`.
  - If `found is None`, call `self.drive.upload_file(file.path, parent_id, resumable=resumable)` as today.
  - This applies to ordinary `reason == "new"` uploads and to `--full` runs, because `--full` intentionally starts with an empty manifest. The new behavior updates an existing same-name Drive file in the backup folder instead of creating another duplicate.
- Preserve existing MD5 fallback after the Drive result: use `result.get("md5Checksum", "")`, and if missing compute `compute_md5(file.path) or ""`.
- Preserve existing parent-folder resolution and resumable-threshold behavior.
- Do not add a duplicate cleanup pass. If multiple duplicates already exist, this plan updates the first non-trashed file returned by Drive and leaves cleanup to the user.

Dependency: Step 3 can be implemented after Step 1 or in parallel with Step 1; final `_upload_file()` must include both reconciliation and immediate manifest persistence from Step 2.

### 4. Add focused behavioral tests

Edit tests only after the implementation shape above is in place.

- In `tests/test_dedup.py`, add `import pytest` and tests for atomic manifest writes:
  - `test_save_keeps_existing_manifest_when_replace_fails`: save an initial manifest, monkeypatch `os.replace` in `drive_backup.dedup` to raise `OSError("replace failed")`, attempt to save a different manifest, assert `pytest.raises(OSError, match="replace failed")`, then assert `Manifest.load(path)` still contains only the original entry. Also assert no sibling temp files matching `".manifest.json.*.tmp"` remain.
  - `test_save_replaces_manifest_atomically_on_success`: save an initial manifest, save a different manifest to the same path, then assert the loaded manifest contains the new entry and not the old one.
  - `test_load_ignores_corrupted_sibling_temp_file`: save a valid manifest, create a sibling file whose name matches the temp prefix/suffix and contains invalid JSON, then assert `Manifest.load(path)` still loads the valid target manifest.
- In `tests/test_drive_api.py`, add tests for the new Drive lookup wrapper:
  - `test_find_file_by_name_and_parent_returns_first_match`: mock `files().list().execute()` to return one file, assert the returned dict and exact `files().list(q=..., spaces="drive", fields="files(id, name, md5Checksum, size)")` call.
  - `test_find_file_by_name_and_parent_returns_none_when_absent`: mock `{"files": []}`, assert `None`, and assert no `files().create`/`files().update` call and no rate-limiter wait.
  - `test_find_file_by_name_and_parent_escapes_query_name`: use a name containing both a backslash and apostrophe; assert the query uses the same escaping as `_escape_drive_query_value`.
- In `tests/test_engine.py`, add engine safety tests using existing `tmp_path`, `monkeypatch`, and inline `FakeDrive` conventions:
  - `test_successful_upload_persists_manifest_before_run_finishes`: construct `BackupEngine(config, dry_run=False)`, inject a fake drive and `_root_folder_id`, call `_process_file()` for one real tmp file, and assert `Manifest.load(manifest_path)` already contains that file's Drive ID without calling `run()` finalization.
  - `test_orphaned_drive_file_is_updated_instead_of_duplicated`: fake `find_file_by_name_and_parent()` to return `{"id": "orphan_id", "name": "file.txt", "md5Checksum": "old", "size": "5"}`, fake `update_file()` to return `{"id": "orphan_id", "md5Checksum": <local md5>}`, make `upload_file()` raise if called, call `_process_file()`, then assert persisted manifest `drive_file_id == "orphan_id"` and `upload_file` was not called.
  - `test_orphan_lookup_miss_uploads_new_file`: fake lookup returns `None`, fake upload returns `{"id": "new_id", "md5Checksum": <local md5>}`, assert manifest records `new_id`.
  - `test_orphan_lookup_failure_does_not_create_duplicate`: fake lookup raises `RuntimeError("lookup failed")`, fake upload raises if called, call `_process_file()`, assert `files_skipped_error == 1`, the error includes `"lookup failed"`, and no manifest entry is written.
  - `test_successful_prune_removal_persists_immediately`: pre-populate a manifest with one stale entry, inject fake drive whose `trash_file()` succeeds, call `_prune_stale_manifest_entries()` directly, and assert `Manifest.load(manifest_path).get(stale_path) is None` without relying on `run()` final save.
  - `test_manifest_progress_save_failure_aborts_run`: create two tmp files, patch `DriveAPI` with a fake that records upload/update calls, monkeypatch `_save_manifest_progress` or `Manifest.save` so the first checkpoint raises `ManifestProgressError`, run `BackupEngine(config, dry_run=False).run()` under `pytest.raises(ManifestProgressError)`, and assert only the first file reached Drive. This guards the safety rule that the engine must not continue creating Drive side effects when manifest checkpoints cannot be persisted.
- Do not add tests that require real Google credentials or network access.

### 5. Keep user-visible CLI/report behavior stable

- Do not add CLI flags.
- Do not change `BackupStats`, report JSON fields, or CLI progress action strings. A reconciled orphan is updated through `DriveAPI.update_file()`, so the existing uploaded counters and `uploaded:<reason>` progress remain accurate enough for this safety change: bytes were sent to Drive and no duplicate was created.
- Do not change `--dry-run`: it must still avoid authentication, Drive lookup/upload/trash, and manifest saves.
- Do not change Drive OAuth scopes.

## Critical files & anchors

- `src/drive_backup/dedup.py` — `Manifest.save()` and the new private `_atomic_write_json()` helper; this is the only durable identity-state write.
- `src/drive_backup/engine.py` — `_upload_file()`, `_process_file()`, and `_prune_stale_manifest_entries()`; this is where Drive side effects become manifest checkpoints.
- `src/drive_backup/drive_api.py` — `get_or_create_folder()` query escaping and the new `find_file_by_name_and_parent()` method; raw Google Drive queries stay behind this boundary.
- `tests/test_dedup.py` — manifest atomicity, corrupted temp-file, and backward-compatible load coverage.
- `tests/test_engine.py` and `tests/test_drive_api.py` — fake-Drive behavior proving duplicate prevention without real Google credentials.

## Verification

Run from repository root `/mnt/c/Users/joesa/code/drive-profile-backup`.

Focused behavioral tests:

```bash
pytest tests/test_dedup.py tests/test_drive_api.py tests/test_engine.py
```

Expected observable coverage:

- Atomic manifest save: a forced `os.replace` failure leaves the previous `manifest.json` valid and loadable.
- Mid-run upload persistence: after a single `_process_file()` success, `manifest.json` already contains that file's `drive_file_id` before `BackupEngine.run()` final save.
- Fatal checkpoint failure: if manifest progress cannot be saved after the first Drive side effect, the run raises `ManifestProgressError` and no later files are uploaded.
- Duplicate prevention: when Drive lookup finds an existing same-name file in the resolved parent, `DriveAPI.update_file()` is called and `DriveAPI.upload_file()` is not called.
- Safe lookup failure: when Drive lookup raises, no create/upload occurs and the file is reported through `files_skipped_error`.
- Prune checkpointing: after a successful trash/remove, the manifest entry is already absent on disk before final run save.

Quality gates after focused tests pass:

```bash
ruff check .
mypy src tests scripts
black --check src tests scripts
```

No manual OAuth run is required for verification. All new Drive behavior must be covered with mocked services or fake Drive objects.

## Assumptions & contingencies

- Safety target: prevent a cut midway through upload from corrupting the manifest or blindly creating duplicate Drive files on rerun. The plan intentionally does not implement persisted Google resumable-upload sessions; if a large upload is cut before Drive finalizes it, the next run starts that file again.
- Existing manifests remain valid. If implementation discovers a need for new manifest metadata, store it as optional top-level data or optional entry fields and update loading to ignore unknown fields; never make existing six-field entries invalid.
- Same-name Drive file policy: the backup folder is treated as tool-owned. When there is no usable manifest ID but Drive already has a non-trashed file with the same name in the resolved parent, update that Drive file and record it instead of creating a duplicate. If Drive lookup itself fails, do not upload; record an error and let the next run retry.
- Existing duplicate policy: do not attempt to clean up duplicates that already exist. The new lookup updates the first returned non-folder file and leaves other same-name files untouched.
- If strict type checking rejects fake test classes because the new `find_file_by_name_and_parent()` method is missing from older fakes, update only the fakes used in non-dry upload paths. Dry-run fakes should still fail if Drive is constructed or called.
