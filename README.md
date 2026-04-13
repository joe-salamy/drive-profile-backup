# drive-profile-backup

Incrementally back up your Windows user profile to Google Drive. First run uploads everything; subsequent runs use a local manifest with MD5 deduplication to upload only changed files.

## Features

- **Incremental backups** — two-tier dedup (mtime+size fast path, then MD5) skips unchanged files
- **Smart exclusions** — skips AppData, venv, .git, node_modules, caches, and configurable size limits per file type
- **Rich metadata reports** — JSON reports withd full stats, skipped file details, and error logs uploaded to Drive
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

```bash
# First run: authenticate and see what would be uploaded
drive-backup --dry-run

# Full backup
drive-backup

# Re-upload everything (ignore manifest)
drive-backup --full

# Use a custom config
drive-backup --config my-config.yaml

# Verbose output (print each file)
drive-backup --verbose
```
