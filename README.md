# drive-profile-backup

Windows-focused Python CLI for incremental Google Drive backups of a user profile. `drive-backup` refreshes restore-oriented Windows and WSL machine-state inventories, scans the configured profile root, applies exclusions and size limits, skips unchanged files with a manifest-backed mtime/size/MD5 check, uploads changed files to Google Drive, and writes JSON backup reports.

## Prerequisites

- Windows PowerShell.
- Python 3.11 or newer.
- Google Cloud project with the Google Drive API enabled.
- OAuth desktop-app credentials saved locally as `credentials.json`.

`credentials.json`, `token.json`, `config.yaml`, manifests, caches, and generated reports are local state and must not be committed.

## Setup

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy config.example.yaml config.yaml
```

Edit `config.yaml` before running a backup:

- `backup_root`: profile directory to scan; blank means the current user's home directory.
- `profile_name`: required per-machine name used for Drive folders and default manifest paths.
- `drive_parent_folder_name`: parent folder in Google Drive.
- `credentials_path`: local OAuth client JSON path; default is `credentials.json`.
- `token_path`: local OAuth token path; default is `~/.drive-backup/token.json`.
- `secrets_key_path`: path to AES-256-GCM key for encrypted secrets; default is `~/.drive-backup/secrets.key` (never uploaded, see `drive-backup --generate-secrets-key`). `encrypt_secrets: true` (default) encrypts `.env`, `credentials.json`, `token.json`, `id_rsa*`, `*.pem`, `.boto`, `.claude.json*` and files under `.ssh`/`.azure`/`.gemini`/`.android`/`.aitk`/`.cisco` as `*.enc` on Drive.
- `machine_state_collectors`: ordered list of generated Windows/WSL inventories to refresh; the example lists every supported collector.
- `upload_workers`: parallel Drive upload workers; defaults to 8 (`config.example.yaml`) and 16 in the workstation `config.yaml`.
- `max_retries`: retry budget for rate-limited or transient Drive errors (`429`/`500`/`503` and rate-limit `403`); defaults to 8.
- `writes_per_second`: client write throttle; `0` disables client throttling (unlimited) and relies on server-directed retries, while a positive value imposes a process-wide writes/second ceiling shared by all upload workers.
- `resumable_threshold_mb`: files larger than this use resumable uploads; defaults to 5 MB.

Encrypted secrets: first backup with `encrypt_secrets: true` auto-generates `~/.drive-backup/secrets.key` (`0o600`, 32 bytes, also accepted as hex/base64). **Save it offline** — `drive-backup --generate-secrets-key` prints Hex/Base64 once; without it `*.enc` files on Drive cannot be decrypted. The key file and `~/.drive-backup/` are never uploaded. Restore decrypts by default (`--decrypt`, `--decrypt-key PATH`, `--no-decrypt` to keep `.enc`, `--no-encrypt-secrets` to skip secrets on backup). See `docs/03-secrets.md`.

Performance: uploads use a bounded worker pool (`upload_workers` with at most `2 * upload_workers` in-flight tasks). The local manifest checkpoints every ~30 seconds and on clean completion or `Ctrl+C`; a hard stop may repeat at most ~30 seconds of completed uploads, reconciled by name/parent on the next run. For NTFS backup roots, prefer native Windows Python/PowerShell; reading `/mnt/c` through WSL adds filesystem-bridge overhead that concurrency cannot remove.

## Run

```powershell
drive-backup --dry-run
drive-backup
drive-backup --full
drive-backup --prune
drive-backup --verbose
drive-backup --skip-machine-state
drive-backup --generate-secrets-key
drive-backup --no-encrypt-secrets   # skip secrets (old behavior)
drive-backup --restore --output $HOME\restore   # decrypts *.enc by default
```

Flags:

- `--dry-run`: refresh local machine-state inventories, then scan and report without uploading or pruning. Combine with `--skip-machine-state` for a no-refresh preview. Report includes `files_encrypted_uploaded` / `total_size_encrypted_human` when secrets are present.
- `--full`: ignore the manifest and upload every eligible file.
- `--prune`: move stale Drive files to trash when their manifest entries no longer exist locally.
- `--verbose`: print per-file actions and DEBUG logs.
- `--skip-machine-state`: do not refresh generated inventories. Existing snapshots remain eligible for ordinary scan, deduplication, and upload.
- `--generate-secrets-key`: generate and display the AES-256-GCM secrets key (`~/.drive-backup/secrets.key`) and exit — red panel if new (shown once), green if existing.
- `--no-encrypt-secrets`: do not encrypt secret files; they will be skipped (old "never in Drive" behavior).
- `--restore --output PATH [--decrypt|--no-decrypt] [--decrypt-key PATH] [--force]`: download non-pruned files from Drive's `manifest.json` snapshot under `_meta/`; decrypts `*.enc` by default using `secrets_key_path` or `--decrypt-key`; `--no-decrypt` keeps `.enc`.
Machine-state refresh is default-on. Generated JSON files live under `<backup_root>/_machine_state/`, are scanned and uploaded under the Drive profile like ordinary files, and obey all configured scanner exclusions and size limits. Collector failures warn and continue the ordinary backup; a failed collector retains its prior local output when possible and protects its prior remote manifest entry from pruning. WSL collection may start every registered distribution, including stopped distributions.

Machine-state outputs intentionally contain unredacted diagnostics. Environment values can include API keys and other secrets; inventories can also contain network addresses, usernames, domains, serial numbers, package sources, service accounts/paths, and scheduled-task arguments. The feature inventories state only and does not copy arbitrary credential or configuration files from outside `backup_root`.

Files upload under `drive_parent_folder_name/profile_name/` and reports go under that Drive profile folder's `_reports` folder. Local profile-mode state defaults to `~/.drive-backup/profiles/<profile_name>/manifest.json` unless `manifest_path` is set.

## Summary reports

```powershell
python scripts/generate_summary.py
python scripts/generate_summary.py --full-profile
python scripts/generate_summary.py --full-profile --include-appdata
```

The summary script writes markdown reports under `docs/` by default. `--include-appdata` can be slow because it scans AppData.

## Checks

```powershell
pytest
ruff check .
mypy src tests scripts
black --check src tests scripts
```

Format with:

```powershell
black src tests scripts
```

## Environment variables

No required environment variables were found. Runtime configuration is file-based through `config.yaml`, `credentials.json`, and the OAuth token file.

## OMP notes

- Read the relevant `skill://<skill-name>` before making changes covered by a repo skill.
- Prefer commands from this README or `pyproject.toml`.
- Do not read or modify `docs/scratchpad.md`.
- Do not commit secrets, local config, generated reports, manifests, or cache artifacts.
- Use `skill://optimize-repo-skills` when the repo's `.omp/skills/` set needs review, pruning, or expansion.

## License

MIT
