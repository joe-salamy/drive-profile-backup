## Overview

A Windows-focused CLI tool (`drive-backup`) that incrementally backs up a user profile directory to Google Drive, targeting personal users who want automated, deduplicated cloud backups. Built on Python 3.11+ with `google-api-python-client` for Drive uploads, `pyyaml` for config, and `rich` for progress/reporting; the architecture follows a pipeline pattern where `cli.py` parses args → `engine.py` orchestrates `scanner.scan()` → `dedup.needs_upload()` (mtime+size fast path, then MD5) → `drive_api.DriveAPI` upload → `report.generate_report()`. A JSON manifest file tracks per-file MD5/mtime/Drive-file-ID state between runs, `config.yaml` controls exclusions and size limits, and OAuth credentials (`credentials.json`/`token.json`) must never be committed.

## Environment

- Activate venv before any pip/python commands: `venv\Scripts\Activate.ps1`
- Never pip install into the global or user environment — always use the venv.

## Git & Commits

- Read `.gitignore` before running any git commit to know what files to exclude.

## Off-Limits Files

- Never read from, write to, or git diff `scratchpad.md`.
- When running `/code-reviewer` or `/python-pro`, exclude diffs of files in `.claude/` and `docs/` — these are settings/prose, not reviewable code.

## Plan Mode

- When asking clarifying questions in plan mode, be liberal; when in doubt, ask more rather than fewer.

## Documentation

- Keep READMEs concise.
