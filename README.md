# drive-profile-backup

Windows-focused Python CLI for incremental Google Drive backups of a user profile. `drive-backup` scans a configured profile root, applies exclusions and size limits, skips unchanged files with a manifest-backed mtime/size/MD5 check, uploads changed files to Google Drive, and writes JSON backup reports.

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

## Run

```powershell
drive-backup --dry-run
drive-backup
drive-backup --full
drive-backup --prune
drive-backup --verbose
```

Flags:

- `--dry-run`: scan and report without uploading or pruning.
- `--full`: ignore the manifest and upload every eligible file.
- `--prune`: move stale Drive files to trash when their manifest entries no longer exist locally.
- `--verbose`: print per-file actions and DEBUG logs.

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
