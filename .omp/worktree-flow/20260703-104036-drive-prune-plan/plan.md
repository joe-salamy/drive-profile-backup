## Context

The requested feature is an opt-in prune mode so Drive can be made to match the current local profile after files or folders move. Current behavior is upload/update only: `scanner.scan()` yields local files from `Config.backup_root`, `Manifest.entries` is keyed by each slash-normalized `relative_path`, `_upload_file()` updates only same-key manifest entries, and `DriveAPI` has no trash/delete method. The intended end state is a safe CLI workflow: `drive-backup --dry-run --prune` reports stale Drive files that would be pruned, `drive-backup --dry-run --verbose --prune` lists every would-prune file for verification, and `drive-backup --prune` moves those stale Drive files to Google Drive trash and removes only successfully trashed entries from the manifest.

## Approach

### Define manifest removal support

1. In `src/drive_backup/dedup.py`, add `Manifest.remove(self, relative_path: str) -> ManifestEntry | None` immediately after `Manifest.get()`.
   - Implementation: `return self.entries.pop(relative_path, None)`.
   - Keep `Manifest.set()` unchanged.
   - Add `tests/test_dedup.py::TestManifest.test_remove_entry`:
     - Create a `Manifest`, call `set(relative_path="old/file.txt", md5="abc", size=10, mtime=1.0, drive_file_id="drive_old", drive_parent_id="parent")`.
     - Assert `remove("old/file.txt")` returns a non-`None` entry with `drive_file_id == "drive_old"`.
     - Assert `manifest.get("old/file.txt") is None`.
     - Assert `remove("missing.txt") is None`.

### Add a reversible Drive trash operation

2. In `src/drive_backup/drive_api.py`, add a public method to `DriveAPI`:
   - Exact signature: `def trash_file(self, file_id: str) -> dict[str, Any]:`
   - Body shape copies `update_file()`: `return self._execute_with_retry(lambda: self._do_trash_file(file_id))`.
3. In the same class, add private helper:
   - Exact signature: `def _do_trash_file(self, file_id: str) -> dict[str, Any]:`
   - Implementation:
     - Call `self._rate_limiter.wait()` before the Drive write, matching `_do_upload()` and `_do_update()`.
     - Build `request = self.service.files().update(fileId=file_id, body={"trashed": True}, fields="id, name, trashed")`.
     - Return `request.execute()` as `dict[str, Any]`.
   - Use Drive trash, not `files().delete()`, so a mistaken prune is recoverable from Google Drive trash.
4. Add `tests/test_drive_api.py::TestDriveAPI.test_trash_file_moves_file_to_trash`:
   - Instantiate `DriveAPI(credentials_path="creds.json", token_path="token.json")`.
   - Set `api._service = mock_service` using `MagicMock`, matching existing tests.
   - Replace `api._rate_limiter = MagicMock()` so the test asserts `wait()` without sleeping.
   - Set `mock_service.files().update().execute.return_value = {"id": "file_123", "name": "stale.txt", "trashed": True}`.
   - Assert `api.trash_file("file_123")` returns that dict.
   - Assert `mock_service.files().update.assert_called_once_with(fileId="file_123", body={"trashed": True}, fields="id, name, trashed")`.
   - Assert `api._rate_limiter.wait.assert_called_once()`.

### Extend reports with prune-specific schema

5. In `src/drive_backup/report.py`, add two dataclasses near `UploadFile` and `ErrorFile`:
   - `@dataclass class PrunedFile:` with fields `relative_path: str`, `drive_file_id: str`, `size_bytes: int`, `size_human: str`.
   - `@dataclass class PruneError:` with fields `relative_path: str`, `drive_file_id: str`, `error: str`.
6. Extend `BackupStats` with these exact fields and defaults:
   - `prune_enabled: bool = False`
   - `files_pruned: int = 0`
   - `files_prune_failed: int = 0`
   - `bytes_pruned: int = 0`
   - `prune_skipped_reason: str = ""`
   - `pruned_files: list[PrunedFile] = field(default_factory=list)`
   - `prune_error_files: list[PruneError] = field(default_factory=list)`
7. Extend `generate_report(stats)` with these exact keys:
   - `"prune_enabled": stats.prune_enabled`
   - `"files_pruned": stats.files_pruned`
   - `"files_prune_failed": stats.files_prune_failed`
   - `"total_bytes_pruned": stats.bytes_pruned`
   - `"total_size_pruned_human": human_size(stats.bytes_pruned)`
   - `"prune_skipped_reason": stats.prune_skipped_reason`
   - `"pruned_files": [{"relative_path": pf.relative_path, "drive_file_id": pf.drive_file_id, "size_bytes": pf.size_bytes, "size_human": pf.size_human} for pf in stats.pruned_files]`
   - `"prune_error_files": [{"relative_path": pe.relative_path, "drive_file_id": pe.drive_file_id, "error": pe.error} for pe in stats.prune_error_files]`
   - Do not change `"total_files_eligible"`; it remains upload-oriented as `files_uploaded + files_skipped_dedup`.
8. Update `tests/test_report.py`:
   - Import `PrunedFile` and `PruneError`.
   - In `TestGenerateReport.test_report_has_required_keys`, construct `BackupStats(..., prune_enabled=True, files_pruned=2, files_prune_failed=1, bytes_pruned=2048, prune_skipped_reason="")`; assert the new keys and `"total_size_pruned_human" == "2.0 KB"`.
   - Add `test_report_includes_pruned_files` with `BackupStats(pruned_files=[PrunedFile(relative_path="old/file.txt", drive_file_id="drive_old", size_bytes=1024, size_human="1.0 KB")])`; assert one serialized row and the `drive_file_id`.
   - Add `test_report_includes_prune_errors` with `BackupStats(prune_error_files=[PruneError(relative_path="old/file.txt", drive_file_id="drive_old", error="not found")])`; assert one serialized row and the `error`.
   - In `TestSaveReport.test_saves_valid_json`, also assert `loaded["pruned_files"]` and `loaded["prune_error_files"]` are lists.

### Wire prune into the engine safely

9. In `src/drive_backup/engine.py`, update imports: add `import sys`; add `ManifestEntry` to the existing `drive_backup.dedup` import; add `PrunedFile` and `PruneError` to the existing `drive_backup.report` import; import `human_size` from `drive_backup.utils`.
10. Change `BackupEngine.__init__` to this exact signature:
    - `def __init__(self, config: Config, dry_run: bool = False, full: bool = False, prune: bool = False) -> None:`
    - Store `self.prune = prune`.
    - Pass `prune_enabled=prune` when constructing `BackupStats`.
11. Add two local-path helpers to `BackupEngine`:
    - `def _local_path_for_manifest_key(self, relative_path: str) -> str:`
      - If `os.path.isabs(relative_path)` is true, use `relative_path.replace("/", os.sep)` as the local path. This preserves the scanner's defensive absolute-path fallback.
      - Otherwise, use `os.path.join(self.config.backup_root, *relative_path.split("/"))`.
      - If `sys.platform == "win32"`, length is at least `260`, and the path does not already start with `"\\\\?\\"`, prefix `"\\\\?\\"`.
      - Return the string path.
    - `def _manifest_key_exists_locally(self, relative_path: str) -> bool:`
      - Return `os.path.isfile(self._local_path_for_manifest_key(relative_path))`.
      - This defines stale as "manifest key no longer points to a local file", not "file is currently eligible for upload"; files that still exist but are excluded, oversized, symlinks, unreadable, or under an excluded directory are not pruned just because config skipped them.
12. Add a stale-entry iterator helper:
    - Exact signature: `def _stale_manifest_entries(self) -> list[tuple[str, ManifestEntry]]:`
    - Return `[(rel_path, entry) for rel_path, entry in sorted(self.manifest.entries.items()) if not self._manifest_key_exists_locally(rel_path)]`.
13. Add a record helper:
    - Exact signature: `def _record_pruned_file(self, relative_path: str, entry: ManifestEntry) -> None:`
    - Increment `self.stats.files_pruned`.
    - Add `entry.size` to `self.stats.bytes_pruned`.
    - Append `PrunedFile(relative_path=relative_path, drive_file_id=entry.drive_file_id, size_bytes=entry.size, size_human=human_size(entry.size))`.
    - Reuse the `human_size` import added in step 9.
14. Add prune execution helper:
    - Exact signature: `def _prune_stale_manifest_entries(self) -> None:`
    - For each `(relative_path, entry)` from `_stale_manifest_entries()`:
      - If `entry.drive_file_id` is empty, increment `files_prune_failed`, append `PruneError(relative_path=relative_path, drive_file_id=entry.drive_file_id, error="missing Drive file ID")`, and continue without removing the manifest entry.
      - If `self.dry_run` is true, call `_record_pruned_file(relative_path, entry)` and continue; do not call Drive and do not mutate the manifest.
      - Otherwise assert `self.drive is not None`, call `self.drive.trash_file(entry.drive_file_id)`, call `_record_pruned_file(relative_path, entry)`, then call `self.manifest.remove(relative_path)`.
      - If `trash_file()` raises, log an error, increment `files_prune_failed`, append `PruneError(relative_path=relative_path, drive_file_id=entry.drive_file_id, error=str(e))`, keep the manifest entry, and continue to the next stale entry.
15. In `BackupEngine.run()`, invoke prune after the scan/process loop and before `self.stats.end_time = time.time()` and before manifest save:
    - Keep the existing scan loop and `_process_file()` behavior unchanged.
    - If `self.prune` is false, do nothing.
    - If `self.prune` is true and `self.full` is true, set `self.stats.prune_skipped_reason = "Skipped prune because --full ignores the manifest"` and do not prune. CLI will normally prevent this combination, but this engine guard keeps direct callers safe.
    - If `self.prune` is true, `self.dry_run` is false, and `self.stats.files_skipped_error > 0`, set `self.stats.prune_skipped_reason = "Skipped prune because backup had file or upload errors"` and do not prune. This avoids trashing old Drive copies when the replacement backup did not complete cleanly.
    - Otherwise call `_prune_stale_manifest_entries()`.
    - Existing dry-run behavior remains: no Drive authentication, no manifest save, report still saved locally.
16. Add engine tests in `tests/test_engine.py`:
    - `TestBackupEngineDryRun.test_prune_disabled_ignores_stale_manifest_entries`: create a temp backup root with no matching file for manifest key `"old/file.txt"`, seed manifest outside backup root, run `BackupEngine(config, dry_run=True, prune=False)`, assert `report["files_pruned"] == 0` and `report["pruned_files"] == []`.
    - `TestBackupEngineDryRun.test_dry_run_prune_reports_stale_entries_without_saving_manifest`: create local `"keep.txt"` plus manifest entries for `"keep.txt"` and `"old/moved.txt"`; run `BackupEngine(config, dry_run=True, prune=True)`; assert `report["files_pruned"] == 1`, `report["pruned_files"][0]["relative_path"] == "old/moved.txt"`, and reloading the manifest still contains `"old/moved.txt"`.
    - `TestBackupEngineUploadErrors.test_prune_skipped_when_backup_has_errors`: seed a stale manifest entry, create a local new file, monkeypatch `drive_backup.drive_api.DriveAPI` to a fake whose `upload_file()` raises for local uploads, run `BackupEngine(config, dry_run=False, prune=True)`, assert `report["prune_skipped_reason"] == "Skipped prune because backup had file or upload errors"` and fake `trash_file()` was not called. The fake must let `authenticate()` and `_resolve_backup_folder()` succeed.
    - Add a non-dry-run success test in a new `TestBackupEnginePrune` class: seed manifest with stale `"old/file.txt"` drive ID `"drive_old"` outside the backup root, monkeypatch `drive_backup.drive_api.DriveAPI` to a fake with `authenticate()`, `get_or_create_folder()`, `trash_file()`, and `upload_file()` for report upload; run `BackupEngine(config, dry_run=False, prune=True)`; assert fake `trash_file` received `"drive_old"`, `report["files_pruned"] == 1`, and `Manifest.load(manifest_path).get("old/file.txt") is None`.
    - Add `TestBackupEnginePrune.test_prune_failure_keeps_manifest_entry`: fake `trash_file()` raises `RuntimeError("trash failed")`; assert `files_pruned == 0`, `files_prune_failed == 1`, `prune_error_files[0]["error"] == "trash failed"`, and the manifest still contains the stale key.

### Expose prune, dry-run, and verbose verification in the CLI

17. In `src/drive_backup/cli.py`, add an argparse flag immediately after `--full`:
    - `parser.add_argument("--prune", action="store_true", help="Move Drive files missing locally to trash and remove them from the manifest")`
    - Do not add separate prune-specific dry-run or verbose flags. The existing CLI flags `--dry-run` and `--verbose` must apply to prune when combined as `--dry-run --prune` and `--dry-run --verbose --prune`.
18. After `args = parser.parse_args(argv)`, reject the unsafe/incoherent flag combination:
    - `if args.full and args.prune: parser.error("--prune cannot be combined with --full because prune needs the existing manifest")`
19. Update the banner logic:
    - If `args.dry_run and args.prune`, print exactly `[yellow]DRY RUN - no files will be uploaded or pruned[/]`.
    - Else if `args.dry_run`, keep the current `[yellow]DRY RUN - no files will be uploaded[/]`.
    - Else if `args.prune`, print exactly `[yellow]PRUNE - stale Drive files will be moved to trash[/]`.
20. Change engine construction to `BackupEngine(config, dry_run=args.dry_run, full=args.full, prune=args.prune)`.
21. Leave `progress_callback` unchanged for prune. Stale manifest entries are not `FileEntry` objects, so per-prune verification is rendered after the run from `report["pruned_files"]` instead of through scan progress callbacks.
22. Change the summary call and function signature:
    - In `main()`, change `_print_summary(console, report)` to `_print_summary(console, report, verbose=args.verbose)`.
    - Change the function signature to `def _print_summary(console: Console, report: dict[str, Any], *, verbose: bool = False) -> None:`.
23. Extend `_print_summary(...)`:
    - If `report.get("prune_enabled")` is truthy or `report.get("files_pruned", 0)` is nonzero, add rows:
      - `"Files to prune"` in dry-run else `"Files pruned"` with `str(report.get("files_pruned", 0))`.
      - `"Size to prune"` in dry-run else `"Size pruned"` with `report.get("total_size_pruned_human", "0.0 B")`.
      - `"Prune failures"` with `str(report.get("files_prune_failed", 0))`.
    - If `report.get("prune_skipped_reason")` is a non-empty string, print `f"\n[yellow]{report['prune_skipped_reason']}[/]"`.
    - If `pruned_files = report.get("pruned_files", [])` is non-empty:
      - When `verbose` is false, print a detail section titled `Top 10 biggest files to prune:` in dry-run else `Top 10 biggest files pruned:`; sort by `size_bytes` descending and display only the first 10.
      - When `verbose` is true, print a detail section titled `Files to prune:` in dry-run else `Files pruned:`; sort every row by `relative_path` ascending and display all rows. This is the required verification path for `drive-backup --dry-run --verbose --prune`.
      - Use table columns `"File"`, `"Size"`, and `"Drive ID"`; row values are `relative_path`, `size_human`, and `drive_file_id`.
    - If `prune_error_files = report.get("prune_error_files", [])` is non-empty, print `f"\n[red]{len(prune_error_files)} prune operations failed.[/]"`.
24. Update `tests/test_cli.py`:
    - Existing fake report dicts must include the new report keys: `prune_enabled`, `files_pruned`, `files_prune_failed`, `total_size_pruned_human`, `pruned_files`, `prune_skipped_reason`, and `prune_error_files`.
    - Update fake `BackupEngine.__init__` signatures to accept `*, dry_run: bool, full: bool, prune: bool`; existing tests assert `prune is False`.
    - Add `TestCliMain.test_prune_flag_is_passed_to_engine` using the fake Rich/fake engine pattern from `test_dry_run_progress_enters_before_scanning_with_indeterminate_total`; call `main(["--dry-run", "--prune"])` and assert the fake engine saw `dry_run=True`, `full=False`, `prune=True`.
    - Add `TestCliMain.test_verbose_prune_lists_all_would_prune_files`: call `_print_summary(fake_console, report, verbose=True)` with `dry_run=True`, `prune_enabled=True`, and two `pruned_files`; assert the captured prune detail table contains both relative paths and the title text is `Files to prune:`.
    - Add `TestCliMain.test_full_and_prune_are_rejected`: call `main(["--full", "--prune"])` inside `pytest.raises(SystemExit)` and assert the exit code is nonzero.
    - Add or parameterize a focused `_print_summary` table-row test; pass a dry-run report with `prune_enabled=True`, `files_pruned=2`, `total_size_pruned_human="3.0 KB"`, one `pruned_files` row, and no uploads; assert rows include `"Files to prune"` and `"Size to prune"`. Add a non-dry-run case to assert `"Files pruned"` and `"Size pruned"`.

## Critical files & anchors

- `src/drive_backup/engine.py:BackupEngine.run` — insert prune after scan/upload processing and before manifest save/report generation.
- `src/drive_backup/engine.py:BackupEngine._process_file` and `_upload_file` — preserve upload/dedup semantics; prune is a separate post-scan phase, not per-upload behavior.
- `src/drive_backup/dedup.py:Manifest` — add the only manifest deletion API, `remove()`, so successful prune can persist by removing stale keys before `save()`.
- `src/drive_backup/drive_api.py:DriveAPI.update_file` and `_do_update` — copy the existing retry/rate-limit/write style for `trash_file()`.
- `src/drive_backup/report.py:BackupStats` and `generate_report()` — define the report contract consumed by CLI and tests.

## Verification

Run from `C:/Users/joesa/Code/drive-profile-backup` in PowerShell after activating the venv:

```powershell
venv\Scripts\Activate.ps1
python -m pytest tests/test_dedup.py tests/test_drive_api.py tests/test_report.py tests/test_engine.py tests/test_cli.py
python -m mypy src tests
python -m ruff check src tests
python -m black --check src tests
```

Behavioral proof required from the new tests:

- Dry-run prune preview: a manifest containing stale `"old/moved.txt"` and live `"keep.txt"` produces `files_pruned == 1`, reports `"old/moved.txt"` in `pruned_files`, does not call Drive, and does not rewrite the manifest.
- Real prune success: a fake Drive API receives `trash_file("drive_old")`; the report shows one pruned file; reloading the manifest shows `"old/file.txt"` removed.
- Real prune failure: fake `trash_file()` raises; the report shows `files_prune_failed == 1`; `prune_error_files` contains the error; the stale manifest entry remains.
- Upload/error safety: if an upload or scanner error occurred before prune, real prune is skipped and `prune_skipped_reason` is `"Skipped prune because backup had file or upload errors"`.
- CLI safety and verification: `--full --prune` exits with an argparse error; `--dry-run --prune` passes `prune=True` to `BackupEngine` and renders “Files to prune”; `--dry-run --verbose --prune` renders every would-prune relative path, not only the top 10.

## Assumptions & contingencies

- Prune uses Google Drive trash via `files().update(..., body={"trashed": True})`, not permanent `files().delete()`. If the Drive API rejects trashing a file, keep the manifest entry and report a `PruneError`; do not fall back to permanent delete.
- A stale manifest entry means the manifest key no longer resolves to a local file path under `backup_root`; it does not mean “not eligible under current exclusions.” This prevents `--prune` from deleting Drive copies of files that still exist locally but are skipped by config.
- Real prune is skipped when previous scan/upload errors occurred in the same run. This favors preserving old remote backups over cleanup when the current backup is incomplete.
- Empty or missing `drive_file_id` is a prune failure, not a successful local manifest cleanup. Keep the manifest entry so the bad state remains visible and retryable.
- Prune has no per-file progress callback. Reporting happens through the JSON report and summary because current progress callbacks accept `FileEntry`, while stale manifest entries are not local files; the existing `--verbose` flag must make `_print_summary(..., verbose=True)` list every `pruned_files` row so dry-run prune can be audited before a real prune.
