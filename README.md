# drive-profile-backup

Incrementally back up your Windows user profile to Google Drive. First run uploads everything; subsequent runs use a local manifest with MD5 deduplication to upload only changed files.

## Features

- **Incremental backups** — two-tier dedup (mtime+size fast path, then MD5) skips unchanged files
- **Smart exclusions** — skips AppData, venv, .git, node_modules, caches, and configurable size limits per file type
- **Rich metadata reports** — JSON reports with full stats, skipped file details, and error logs uploaded to Drive
- **Dry-run mode** — scan and report without uploading anything
- **Progress bar** — live upload progress via `rich`
- **Resumable uploads** — large files use resumable uploads with retry logic

## Setup

### 1. Google Cloud Project

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or select existing)
3. Enable the **Google Drive API**
4. Go to **Credentials** → **Create Credentials** → **OAuth 2.0 Client ID**
5. Select **Desktop app** as the application type
6. Download the credentials file and save it as `credentials.json` in this project's root directory

### 2. Install

```bash
python -m venv venv
venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

### 3. Configure

Edit `config.yaml` to set your backup root, exclusions, and size limits.

## Usage

### Backup (`drive-backup`)

```bash
drive-backup [--dry-run] [--full] [--verbose]
```

| Flag        | Description                                                                                                                         |
| ----------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `--dry-run` | Scan and report only — no files are uploaded. Shows what _would_ be uploaded and saves a JSON report to `~/.drive-backup/reports/`. |
| `--full`    | Ignore the local manifest and re-upload every eligible file, even if it hasn't changed since the last run.                          |
| `--verbose` | Print each file as it is processed, including skip reasons and upload actions. Also sets log level to DEBUG.                        |

Examples:

```bash
# First run: authenticate and see what would be uploaded
drive-backup --dry-run

# Full backup
drive-backup

# Re-upload everything (ignore manifest)
drive-backup --full
```

### Summary report (`scripts/generate_summary.py`)

Scans the profile and writes a markdown summary to `docs/profile-summary-YYYY-MM-DD.md` with breakdowns by root folder, file type, top 25 largest files, skipped file reasons, and errors.

```bash
python scripts/generate_summary.py [--out DIR] [--full-profile] [--include-appdata]
```

| Flag                | Description                                                                                                                                              |
| ------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--out DIR`         | Output directory for the markdown file. Defaults to `docs/`.                                                                                             |
| `--full-profile`    | Also generate unrestricted full-profile scan reports showing everything in the user profile, with a comparison table showing what the backup is excluding. |
| `--include-appdata` | Include AppData in the full-profile scan. Opt-in because AppData can take 5-10+ minutes to scan.                                                         |

Examples:

```bash
# Backup-filtered summary only
python scripts/generate_summary.py

# Backup summary + full profile (without AppData)
python scripts/generate_summary.py --full-profile

# Backup summary + full profile with and without AppData
python scripts/generate_summary.py --full-profile --include-appdata
```

The `--full-profile` flag produces additional reports:
- `profile-full-no-appdata-YYYY-MM-DD.md` — full profile scan excluding AppData
- `profile-full-YYYY-MM-DD.md` — full profile scan including AppData (only with `--include-appdata`)
