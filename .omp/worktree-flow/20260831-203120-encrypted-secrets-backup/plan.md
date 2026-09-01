# Encrypted Secrets Backup

## Context
User wants every currently-skipped secret (docs/03-secrets: `.ssh/id_ed25519`, `.env`/`credentials.json`/`token.json`/`*.pem`/`*.key`/`id_rsa*`/`.boto`/`.claude.json*` and secrets dirs `.ssh`/`.azure`/`.gemini`/`.android`/`.aitk`/`.cisco`) to be uploaded automatically encrypted, and `drive-backup --restore` to decrypt automatically when the local key exists. Selected preferences: scope = only secrets (not caches like `.ollama`/`Splice`/`AppData`/`node_modules`), key = local key file at `~/.drive-backup/secrets.key` auto-generated, Drive layout = same relative tree with `.enc` suffix, restore = auto-decrypt when key present else leave `.enc` with warning, and key must be printed very clearly on generation for copy.

## Approach
### Step 1 — Config schema (`src/drive_backup/config.py`, `config.example.yaml`, `config.yaml`)
- Add `Config` fields: `encrypt_secrets: bool = True`, `secrets_key_path: str = "~/.drive-backup/secrets.key"`. Expand `~` in `__post_init__` like `token_path`. Normalize extensions not needed.
- Extend `_STRING_FIELDS` / validation: add `secrets_key_path` to `_STRING_FIELDS`, add `encrypt_secrets` boolean handling (new `if field_name == "encrypt_secrets": type(value) is bool else invalid`). Add to `_CONFIG_FIELDS`. `load_config` falls back to defaults when missing → backwards compatible: old manifests/configs without the keys get True + default path (enables encryption on next run; intentional per request).
- Update `config.example.yaml` with commented block explaining that when `encrypt_secrets: true` secrets are encrypted with `secrets_key_path` and uploaded as `*.enc`; key is never uploaded.

### Step 2 — Crypto module (`src/drive_backup/crypto.py` — new, no equivalent exists)
- Add dependency `cryptography>=42.0` to `pyproject.toml:dependencies`. Use `cryptography.hazmat.primitives.ciphers.aead.AESGCM`.
- Constants: `KEY_BYTES = 32`, `NONCE_BYTES = 12`, `FILE_VERSION = b"\x01"`.
- `def generate_key() -> bytes: return AESGCM.generate_key(256)` or `os.urandom(32)`.
- `def load_or_generate_key(key_path: str) -> tuple[bytes, bool]:` expanduser, `if exists: read bytes (strip whitespace, decode hex or base64 or raw) → validate 32 bytes else generate, write atomically with `os.umask`/`chmod 0o600` (POSIX) and restrictive ACL on Windows, return (key, was_generated)`. Supports reading stored raw, hex, or base64 to tolerate manual copy.
- `def format_key_display(key: bytes) -> dict:` returns `{"hex": key.hex(), "base64": base64.b64encode(key).decode(), "path": expanded_path}`.
- `def encrypt_file(plaintext_path: str, ciphertext_path: str, key: bytes) -> None:` read plaintext bytes (use `Path.read_bytes`), `nonce = os.urandom(12)`, `AESGCM(key).encrypt(nonce, plaintext, None)` (no AAD), write `FILE_VERSION + nonce + ciphertext_and_tag` to `ciphertext_path` atomically via `tempfile + os.replace`.
- `def decrypt_file(ciphertext_path: str, plaintext_path: str, key: bytes) -> None:` read file, verify first byte `0x01`, split nonce/ciphertext, `AESGCM(key).decrypt`.
- `def is_encrypted_file(path: str) -> bool:` check version byte.
- Handle large files by reading whole file (secrets <150 MB, `max_file_size_mb:150`); no streaming needed — document limit.
- Error: on decrypt failure raise `ValueError("Decryption failed — wrong key or corrupted file")`.

### Step 3 — Scanner (`src/drive_backup/scanner.py`)
- Add to `FileEntry`: `encrypted: bool = False` (means source file requires encryption before upload; not is_skipped). Keep `is_skipped/skip_reason` for true skips.
- Define module constants: `SECRETS_FILE_PATTERNS = [".env", ".env.local", ".env.*.local", "credentials.json", "token.json", "id_rsa", "id_rsa.pub", "id_ed25519", "id_ed25519.pub", "*.pem", "*.key", "*.p12", "*.pfx", ".boto", ".claude.json*"]` and `SECRETS_DIR_NAMES = {".ssh", ".azure", ".gemini", ".android", ".aitk", ".cisco"}` (`.drive-backup`, `.ollama`, `Splice`, `AppData`, `.cache`, etc. deliberately NOT in set — remain skipped).
- Helper `def _is_secret_file(filename: str) -> bool: fnmatch against SECRETS_FILE_PATTERNS`.
- Helper `def _is_secret_dir(dirname: str) -> bool: dirname in SECRETS_DIR_NAMES`.
- Change directory pruning loop (line ~118-124): `if _is_excluded_dir_with_includes(d, rel_dir, config):` → add guard: `if config.encrypt_secrets and _is_secret_dir(d): continue to allow descent` (do not increment `excluded_dir_count`, append to `filtered_dirs`). Else existing logic.
- Change file exclusion block (~162-171 `_is_excluded_file`): before yielding `is_skipped`, check `if config.encrypt_secrets and _is_secret_file(filename): yield FileEntry(..., encrypted=True)` instead of skipped. Same for `exclude_dirs` case? File inside secrets dir will not have `is_excluded_file` match but would have been excluded via dir prune; now allowed, so arrives here without dir exclusion and should be yielded as encrypted regardless of filename — add check `rel_parts = rel_path.split("/")` and if any part in `SECRETS_DIR_NAMES` and `config.encrypt_secrets`: yield encrypted.
- Ensure `exclude_specific_files` and `type_excluded`/`exceeds_size_limit` still apply after encryption check — if file exceeds size limit even encrypted, yield skipped with reason `exceeds_size_limit` (do not encrypt huge secrets dirs that blow limit). For secrets that are normally skipped via `exclude_specific_files`, still encrypt if path is secrets file — treat encryption precedence over specific_file when secret match and `encrypt_secrets` true.
- Preserve existing `include_path_patterns` override behavior.

### Step 4 — Manifest / Dedup (`src/drive_backup/dedup.py`)
- Extend `ManifestEntry`: add `encrypted: bool = False`. Update `_MANIFEST_ENTRY_FIELDS` to include `"encrypted"`, add to `_MANIFEST_ENTRY_STRING_FIELDS` exclusion (keep only bool/int fields logic). Add default handling in `_load_manifest_entries`: `encrypted = bool(data.get("encrypted", False))` for backward compat when key missing → False.
- `Manifest.set` adds `encrypted` param `encrypted: bool = False`, stores it. `asdict` automatically includes field; `save/load` roundtrip covers it.
- `needs_upload` unchanged — it computes `compute_md5` on plaintext `file.path` and compares to stored `manifestEntry.md5` (which for encrypted entries stores plaintext md5). Document invariant: `ManifestEntry.md5` is always plaintext MD5 for encrypted entries; Drive `md5Checksum` (ciphertext) is ignored for dedup. `_complete_upload` will compute `md5 = compute_md5(plaintext_path)` for encrypted files, not Drive-returned md5.

### Step 5 — Engine (`src/drive_backup/engine.py`)
- In `BackupEngine.__init__`, after `self.config = config`, add `self._secrets_key: bytes | None = None` and `self._secrets_key_was_generated: bool = False`.
- Add `def _ensure_secrets_key(self) -> bytes | None:` if not `config.encrypt_secrets`: return None; else call `crypto.load_or_generate_key(config.secrets_key_path)`; on `was_generated` set flag; store key; on failure log `logger.error` and set `stats` warning, return None → engine will fall back to skipping secrets with error reason (never upload plaintext). Called once in `run()` before scan (after manifest load, before `scan` loop) so scanner already knows `config.encrypt_secrets` but key availability does not affect scan filtering — scan always marks `encrypted=True` when `encrypt_secrets` true; engine then decides per file if key missing → treat as `files_skipped_error` with reason `encrypted_missing_key`.
- Modify `BackupEngine.run` dry-run branch: before loop call `_ensure_secrets_key()`. If `dry_run` and encrypted file and key missing, still count as `would_upload (encrypted)` with reason `new (encrypted, key will be generated)` — do not fail dry-run.
- Modify `_prepare_upload`: after existing `if file.is_skipped` early return, add check `if getattr(file, "encrypted", False):` → if `self._secrets_key is None` and not dry-run: record error skip `encrypted_missing_key` in `stats.files_skipped_error` and `stats.error_files`, callback `ProgressKind.ERROR`, return None (do not upload plaintext). Else treat as eligible: same dedup path as normal but tag reason with `" (encrypted)"` suffix, snapshot `existing` handling extended to compare plaintext md5 even when encrypted flag mismatch (if existing entry had `encrypted` differing, reason becomes `"content_changed"`). Return `UploadWork(file=file, reason=reason, existing_drive_file_id=..., is_encrypted=True)` — extend `UploadWork` dataclass with `is_encrypted: bool = False`.
- Extend `UploadWork` and `UploadResult` dataclasses (line ~65-76): `UploadWork.is_encrypted: bool = False`; `UploadResult.encrypted: bool = False` if needed.
- Modify `_execute_upload(self, work: UploadWork) -> UploadResult:` If `work.is_encrypted`:
  1. `plaintext_path = work.file.path` (strip `\\?\` prefix on Windows for reading).
  2. `tmp_dir = tempfile.mkdtemp(prefix="enc-")` or `NamedTemporaryFile(delete=False, suffix=".enc")`; `enc_path = tmp_path`.
  3. Call `crypto.encrypt_file(plaintext_path_normalized, enc_path, self._secrets_key)` — handle `ManifestProgressError`? No.
  4. Determine `parent_id` from `rel_dir = os.path.dirname(work.file.relative_path)` (original dir, no .enc).
  5. `filename_enc = os.path.basename(work.file.relative_path) + ".enc"` (if already `.enc` skip doubling).
  6. `resumable = os.path.getsize(enc_path) > self.config.resumable_threshold_bytes`.
  7. Perform `find_file_by_name_and_parent(filename_enc, parent_id)` reconcile, else `update_file`/`upload_file` using `enc_path` not `file.path`.
  8. On success, `md5_plaintext = compute_md5(plaintext_path_normalized) or ""` (use plaintext md5 for manifest), `drive_file_id = result["id"]`, `return UploadResult(md5=md5_plaintext, drive_file_id=..., drive_parent_id=parent_id, encrypted=True)`. Ensure temp file deletion in `finally`.
  Else existing logic unchanged.
- Modify `_complete_upload`: `self.manifest.set(..., md5=result.md5, ..., encrypted=work.is_encrypted)`; propagate `encrypted` flag into manifest. Mark `_manifest_dirty`.
- Dry-run path in `_prepare_upload` for encrypted files: `self.stats.files_uploaded += 1` etc. already there; ensure `_record_upload` includes encrypted hint: `UploadFile` may gain `encrypted` field or `relative_path` stays original and `extension` is original ext; for report, maybe add count `files_encrypted`. Add `BackupStats.files_encrypted`/`bytes_encrypted` (optional) or reuse existing `uploaded_files` with encrypted tag — plan chooses to add `stats.files_encrypted_uploaded` int and reflect in `report`.
- After `run` completes and before `_upload_manifest_snapshot`, if `_secrets_key_was_generated`, print key clearly: use `rich.console.Console` + `rich.panel.Panel` + `rich.text.Text`. Panel title `"[bold red]NEW SECRETS ENCRYPTION KEY — COPY AND SAVE NOW[/]"`, body lines: `Path: <expanded>`, `Hex:   <hex>`, `Base64: <b64>`, warnings: `"Without this key encrypted backups on Drive CANNOT be decrypted. Store offline (USB, 1Password, print). This is shown ONLY once at generation."`. Also log to `logger.warning` same info, and ensure `--dry-run` does not generate key.
- Ensure pruning respects encrypted suffix: `_manifest_key_exists_locally` checks original path; `_stale_manifest_entries` will correctly prune encrypted entries when local file vanished; on `prune_mode trash` it will trash the `.enc` Drive file via stored `drive_file_id`.

### Step 6 — Restore (`src/drive_backup/restore.py`)
- Add params: `def restore_backup(config: Config, output_dir: str, dry_run: bool=False, force: bool=False, decrypt: bool=True, decrypt_key_path: str | None=None) -> dict[str, Any]:`
- After loading manifest, if `decrypt` and any `entry.encrypted`:
  1. Resolve `key_path = decrypt_key_path or config.secrets_key_path` (expanded).
  2. Try `crypto.load_key(key_path)` — if not found and `decrypt` true: `logger.warning` + collect `errors` entry `"missing secrets key"` and set flag `missing_key = True`; behavior per preference: download encrypted `.enc` files as-is (with `.enc` suffix) to `output_dir`, and do not attempt decrypt.
  3. If key exists: for each `entry.encrypted` true, download to `tmp_enc = tempfile.NamedTemporaryFile(delete=False)`, `drive.download_file(entry.drive_file_id, tmp_enc.name)`, then `crypto.decrypt_file(tmp_enc.name, final_output_path, key)`, set permissions `0o600` if `rel_path` in secrets dirs and POSIX.
- For non-encrypted entries: existing download path unchanged.
- Safety: validate `relative_path` still uses unsafe-path checks; for encrypted entries the Drive filename is `basename + ".enc"` but manifest key is original — download uses stored `drive_file_id`, not name search, so no suffix confusion.
- Need to find Drive file ID for encrypted entries: already stored `drive_file_id` from upload; restore uses that, not `find_file_by_name_and_parent(filename_enc)`. Manifest already has it.
- Update error handling: decrypt failure → `files_failed +=1`, `errors.append({"relative_path": p, "error": "decryption failed"})`, leave `.enc` temp for inspection.
- For `dry_run=True` in restore: skip download/decrypt, just count `files_restored` would-be and report encrypted counts.
- Ensure `output_dir` creation respects original subdirs.

### Step 7 — CLI (`src/drive_backup/cli.py`)
- Add args: `--no-encrypt-secrets` (store_false for config override), `--decrypt/--no-decrypt` (for restore, default decrypt=True per restore preference), `--decrypt-key PATH`.
- In `main()`, after `load_config`, apply CLI overrides: `if args.no_encrypt_secrets: config.encrypt_secrets = False; config.secrets_key_path = args.decrypt_key or config.secrets_key_path` if restore.
- Key generation display is done in engine, but CLI also supports `drive-backup --generate-secrets-key` helper: if called, call `crypto.load_or_generate_key` and print panel, exit.
- Update `restore` call: `restore_backup(config, args.output, dry_run=args.dry_run, force=args.force, decrypt=not args.no_decrypt, decrypt_key_path=args.decrypt_key)`.
- Update `_print_summary` to show `files_encrypted` segment when present.

### Step 8 — Tests & verification artifacts
- Add `tests/test_crypto.py` covering `encrypt->decrypt roundtrip, wrong key fails, key file load/generate permissions`.
- Extend `tests/test_scanner.py`: test that with `encrypt_secrets=True` a `.env` and `.ssh/id_ed25519` inside secrets dir are NOT `is_skipped` but `encrypted=True` and allowed descent into `.ssh`.
- Extend `tests/test_engine.py`: mock DriveAPI, create tmp secrets files, run `BackupEngine` with `encrypt_secrets=True`, assert `manifest.get("secret/.env").encrypted is True`, `md5` equals plaintext MD5, Drive upload called with `.enc` basename, temp ciphertext not left.
- Extend `tests/test_dedup.py`: roundtrip manifest with encrypted flag.
- Extend `tests/test_restore.py`: build manifest with encrypted entries, use `FakeDrive` with ciphertext blobs, test restore decrypts to original content and respects missing key (leaves .enc).
- Update `tests/test_config.py` for new fields validation.

## Critical files & anchors
- `src/drive_backup/config.py#29-Config` — add `encrypt_secrets`/`secrets_key_path`, validation, defaults.
- `src/drive_backup/scanner.py#92-scan` — directory prune + `FileEntry.encrypted` flag, secrets pattern matching.
- `src/drive_backup/crypto.py` — new AES-256-GCM file encrypt/decrypt + key load/generate + display helper.
- `src/drive_backup/engine.py#640-_prepare_upload` and `#716-_execute_upload` — branched encrypted upload (temp `.enc`, `.enc` Drive filename, plaintext MD5 dedup, manifest `encrypted` flag, key-generation panel).
- `src/drive_backup/restore.py#20-restore_backup` — download via `drive_file_id`, decrypt with `crypto.decrypt_file`, auto-decrypt with `decrypt` flag, missing-key fallback to `.enc` with warning.

## Verification
- `pytest tests/test_crypto.py tests/test_scanner.py tests/test_engine.py tests/test_restore.py tests/test_dedup.py -q` — exercises new encryption path; all existing tests must still pass (encrypted flag defaults False).
- `python -m drive_backup.cli --dry-run --verbose` on profile `/mnt/c/Users/joesa` with `config.yaml` `encrypt_secrets: true` and `secrets_key_path: ~/.drive-backup/secrets.key` — first run prints Rich panel with hex/base64 key and path, lists `WOULD_UPLOAD ... (encrypted)` for `.ssh/id_ed25519`, `.env` hits; `~/.drive-backup/secrets.key` exists `0o600`.
- `python -m drive_backup.cli` (non-dry) — second run uploads ciphertexts; verify Drive `Laptop Backups/Inspiron 15 3535/.ssh/id_ed25519.enc` exists, manifest `~/.drive-backup/profiles/Inspiron 15 3535/fresh-2026-07-21/manifest.json` contains `"encrypted": true` for those relative paths and `"md5": "<plaintext md5>"`.
- `python -m drive_backup.cli --restore --output /tmp/restore-test` — restores `.ssh/id_ed25519` decrypted (byte-identical to source, permissions `0o600` on POSIX), non-encrypted files unchanged; then test missing key: rename key file away, rerun restore to different output → files appear as `.enc` and report warns `"missing secrets key"`.
- `pip check` / `mypy src/drive_backup/crypto.py src/drive_backup/config.py` — new `cryptography` import types pass strict mode.
- `ruff check src/drive_backup/crypto.py` clean.

## Assumptions & contingencies
- Assumption: key file path `~/.drive-backup/secrets.key` is acceptable as default and never uploaded (even when `encrypt_secrets` true we explicitly keep `.drive-backup` dir excluded from encryption) — if user prefers different path they set `secrets_key_path` in `config.yaml` or `--decrypt-key`; implementer reads that field rather than hardcoding.
- Assumption: adding `cryptography` dependency is approved (pure-Python wheel, no system OpenSSL build needed) — if environment forbids new native dep, fallback is `pip install cryptography` already satisfies Windows wheel; if truly blocked, use `PyNaCl` is worse, so fail with install error and document prerequisite.
- Assumption: secrets file size rarely exceeds `max_file_size_mb` (150 MB); if a secrets-dir file does exceed limit we skip with `exceeds_size_limit` rather than encrypt — if reality is a large secrets file (e.g. `.gemini/history` DB) exceeds limit, user must raise limit or add `no_size_limit` ext; do not silently truncate.
- If plaintext file vanishes between scan and encrypt (race), `encrypt_file` raises `FileNotFoundError` → engine records `files_skipped_error` with that error via `_record_upload_error`, does not upload partial ciphertext.
- If `cryptography` is not installed on restore host, restore fails fast with `"pip install cryptography required to decrypt"` rather than silently leaving ciphertext.

