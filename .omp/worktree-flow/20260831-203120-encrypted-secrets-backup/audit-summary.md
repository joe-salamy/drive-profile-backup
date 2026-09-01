# Audit Summary — Encrypted Secrets Backup

## Worktree
- **Path:** `/mnt/c/Users/joesa/code/drive-profile-backup-encrypted-secrets-backup`
- **Branch:** `feature/encrypted-secrets-backup`
- **Base:** `main` @ `7805e6489586fcf824c60464d6b38e19751bfad1` (merge-base `main...HEAD`)
- **HEAD before audit:** `9f80238 Implement encrypted secrets backup`
- **HEAD after audit:** `d2003ce Fix audit findings`

## Implementation Summary (prior)
Feature adds client-side AES-256-GCM encryption for secrets. `crypto.py` (new) provides `generate_key`/`load_or_generate_key`/`encrypt_file`/`decrypt_file` (atomic, `0o600`). `Config` gains `encrypt_secrets: bool=True`, `secrets_key_path="~/.drive-backup/secrets.key"` (expanduser, bool validation). `scanner` adds `FileEntry.encrypted`, `SECRETS_FILE_PATTERNS`/`SECRETS_DIR_NAMES`, `_is_secret_file`/`_is_secret_dir`/`_is_in_secret_dir`; directory prune allows secrets dirs even when in `exclude_dirs`; file exclusion bypasses `exclude_files`/`exclude_path_patterns`/`exclude_specific_files` for secrets but retains symlink/size limits. `dedup` adds `ManifestEntry.encrypted` optional bool (backward compat). `engine` adds `UploadWork.is_encrypted`/`UploadResult.encrypted`, `_secrets_key`/`_ensure_secrets_key` (dry-run does not generate), `_prepare_upload` tags `(encrypted)`/`(encrypted, key will be generated)`, forces `content_changed` on `encrypted` flag mismatch, dry-run + real `files_encrypted_uploaded` accounting, `_execute_upload` encrypts via `mkdtemp` + correct `*.enc` basename, uses plaintext MD5, `_maybe_print_generated_key` Rich panel. `restore` adds `decrypt`/`decrypt_key_path`, auto-decrypts when key present else leaves `.enc` with warning + synthetic `<secrets-key>` error, `0o600` for secrets dirs, `dry_run`/`--no-decrypt` handling. `cli` adds `--no-encrypt-secrets`, `--decrypt`/`--no-decrypt`, `--decrypt-key`, `--generate-secrets-key`, config override, report summary. `report` adds `files_encrypted_uploaded`/`bytes_encrypted_uploaded`. Verified via 163 pytest passes (minus `drive_api` missing `googleapiclient`), manual crypto/scanner/engine/restore integrations.

## Skills Loaded
- `audit-worktree` (this audit workflow)
- Implicit project rules from `AGENTS.md` (pipeline pattern, config/manifest behavior)
- Reviewed `scanner-exclusion-rules` relevant areas (secret precedence, `.drive-backup` handling, size/symlink retention), `manifest-dedup-state` (plaintext MD5 invariant, `encrypted` flag), `backup-pipeline-flow` (engine upload/prune/progress), `python-quality-gates` (compile/tests) via direct code inspection. No separate `skill://` file reads were required beyond `AGENTS.md`; focused diff inspection covered contracts.

## Issues Found and Fixes Applied
| # | Severity | Finding | Fix | Files |
|---|----------|---------|-----|-------|
| 1 | Low (cleanup) | `src/drive_backup/scanner.py:_is_in_secret_dir` contained dead duplicate `@property extension`/`size_human` after `return` (unreachable code inside function, left from merge). No runtime failure but confusing, would break if refactored. | Removed dead `86-92` block. | `scanner.py` |
| 2 | **High (security)** | `.drive-backup` not in default `Config.exclude_dirs` (`config.py`) and not hard-excluded in `scanner`. Default `backup_root=~` would scan `~/.drive-backup/secrets.key` (matches `*.key`), `token.json`/`manifest.json`. Implementation relied on `config.wsl.yaml` having `.drive-backup` but plain `config.yaml` / defaults did not. Key could be uploaded encrypted (`secrets.key.enc`) — leak. | Added `".drive-backup"` to `Config` default `exclude_dirs`; added hard directory prune `if d == ".drive-backup"` in `scan()`; added file-level guard `rel_path == ".drive-backup" or rel_path.startswith(".drive-backup/")` → `is_skipped`. Also synced `config.example.yaml` to include `".nox"`, `".pytest_cache"`, `".ruff_cache"`, `".omp"`, `".drive-backup"` matching `Config` default order. | `config.py`, `scanner.py`, `config.example.yaml` |
| 3 | **High (security)** | `secrets_key_path` file itself not excluded from scan. `secrets.key` matches `*.key` secret pattern → would be marked `encrypted=True` and uploaded as `secrets.key.enc` (self-encrypted). Even with `.drive-backup` prune, custom key paths outside `.drive-backup` (e.g., `~/my.key`) would be uploaded. | Added file-level guard in `scanner.scan()` comparing absolute normalized `full_path` (stripping `\\?\`) to `config.secrets_key_path` (already `expanduser`'d) via `abspath(normcase(...))`; if equal → `is_skipped` `excluded_by_specific_file` before secret determination. Verified with `tmp/my.key` test → correctly skipped. | `scanner.py` |
| 4 | Medium (correctness) | `engine._prepare_upload` only forced re-upload when `is_encrypted` true and `existing.encrypted != is_encrypted`. Transition `encrypted=True → False` (user disables `encrypt_secrets` or key file removed) would incorrectly dedup-skip (same plaintext MD5), leaving old `.enc` on Drive. | Changed to unconditional `existing_entry = manifest.get(...); if existing_entry is not None and existing_entry.encrypted != is_encrypted: should_upload=True; reason="content_changed"` — both directions. | `engine.py` |
| 5 | Medium (correctness) | `engine._prepare_upload` reused `existing_drive_file_id` even when `encrypted` flag mismatched. Drive filename differs (`*.enc` vs plaintext). Updating existing ID would keep old name (plaintext file updated with ciphertext or vice-versa). | Added condition `existing.encrypted == is_encrypted` to ID-reuse guard. Old file now becomes orphan (new upload creates correct name); prune not yet handling cross-name leak — noted as residual. | `engine.py` |
| 6 | Low (consistency) | `config.example.yaml` drifted from `Config` default (missing `".nox"`, `".pytest_cache"`, `".ruff_cache"`, `".omp"`). Users copying example would get different defaults. | Synced example list to exact `Config` default order including added `".drive-backup"`. | `config.example.yaml` |

No other blocking issues found. All changed lines verified via `git diff main...HEAD` (12 files, 936 insertions post-audit).

## Files Changed by Audit
- `config.example.yaml` — sync `exclude_dirs` (`+ .nox/.pytest_cache/.ruff_cache/.omp/.drive-backup`)
- `src/drive_backup/config.py` — add `".drive-backup"` to default `exclude_dirs`
- `src/drive_backup/engine.py` — dedup mismatch both directions + prevent Drive ID reuse on encrypted flag change (2 hunks, includes comment)
- `src/drive_backup/scanner.py` — remove dead properties, hard-exclude `.drive-backup` dir + file-level `.drive-backup/*` guard + secrets-key absolute-path guard

Commit: `d2003ce Fix audit findings` (4 files, 54 insertions, 16 deletions). No workflow artifacts committed.

## Tests / Checks Run
- `python3 -m py_compile src/drive_backup/scanner.py src/drive_backup/config.py src/drive_backup/engine.py` — ok
- `PYTHONPATH=src python3 -m pytest --ignore=tests/test_drive_api.py -q` — **163 passed** (pre-audit also 163)
- `PYTHONPATH=src python3 -m pytest -q` — 1 error `ModuleNotFoundError: googleapiclient` in `test_drive_api.py` (same as pre-audit, env missing `google-api-python-client`)
- Manual evals:
  - `scanner` edge: `tmp/.drive-backup/secrets.key` → 0 entries, `exclude_dirs` contains `.drive-backup` true, `.ssh/*.env` correctly `encrypted=True`, custom `my.key` → `excluded_by_specific_file` true
  - `engine` dedup mismatch: `encrypted True→False` and `False→True` both force `content_changed` and `existing_drive_file_id=None`; same-flag same-content dedup-skips correctly; `content_changed` reuses ID correctly
  - `crypto` roundtrip: `encrypt→decrypt` byte-identical, wrong key raises `Decryption failed`, `load_or_generate_key` creates `0o600`, second call `was_generated=False`, hex 64 / b64 44
- `ruff`/`mypy` — not installed in WSL env, skipped (same as pre-audit, `py_compile` covers syntax)
- Real Drive dry-run/restore on Windows host — skipped (no credentials; FakeDrive paths exercised)

## Residual Risks / Follow-up
- **Orphaned Drive files on encrypted-flag flip:** Fix now correctly creates new Drive file with correct name (e.g., `foo` vs `foo.enc`), but old Drive file remains under old name (no automatic trash). Manifest now points to new ID. Recommend manual `prune --trash` after toggling `encrypt_secrets`, or future enhancement to trash mismatched-name file on successful re-upload.
- **Key file outside `.drive-backup`:** File-level absolute-path guard covers single configured `secrets_key_path`. If user changes key path, old key file path remains on disk and could be considered secret if it matches `*.key` and lies inside `backup_root` under old location; ensure old path is cleaned or added to `exclude_specific_files`.
- **Large secrets:** `crypto.encrypt_file` reads whole file (`secrets <150 MB` assumption). A large secrets dir file (e.g., `.gemini/history` DB >500 MB) is currently `exceeds_size_limit` skipped — by design; user must raise `max_file_size_mb` or add `no_size_limit` for `.db`.
- **No dedicated `tests/test_crypto.py` / scanner encryption tests / engine encryption tests:** New behavior covered only by manual integration and existing 163 tests (which exercise non-encrypted paths). Plan's `tests/test_crypto.py`, `test_scanner` encrypted cases, `test_engine` encrypted manifest/Drive-name assertions, `test_restore` decrypt/missing-key cases remain as follow-up to formalize coverage.
- **Windows ACL:** Key file `chmod 0o600` is best-effort on Windows (no `win32security` ACL); restore `chmod` for secrets dirs also POSIX-only. Documented as acceptable.
- **Config default change:** Adding `".drive-backup"` to default `exclude_dirs` is a safe, additive default; custom `config.yaml` with `exclude_dirs: ["custom"]` overrides entirely — scanner hard-exclude still protects `.drive-backup` even when overridden, which is intentional.
