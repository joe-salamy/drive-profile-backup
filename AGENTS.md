## Overview

A Windows-focused CLI tool (`drive-backup`) that incrementally backs up a user profile directory to Google Drive, targeting personal users who want automated, deduplicated cloud backups. Built on Python 3.11+ with `google-api-python-client` for Drive uploads, `pyyaml` for config, and `rich` for progress/reporting; the architecture follows a pipeline pattern where `cli.py` parses args → `engine.py` orchestrates `scanner.scan()` → `dedup.needs_upload()` (mtime+size fast path, then MD5) → `drive_api.DriveAPI` upload → `report.generate_report()`. A JSON manifest file tracks per-file MD5/mtime/Drive-file-ID state between runs, `config.yaml` controls exclusions and size limits, and OAuth credentials (`credentials.json`/`token.json`) must never be committed.

## Rules

- Treat `docs/scratchpad.md` as private scratch space. Do not read, search, open, modify, diff, summarize, or quote it. If it appears in broad file listings or git status output, ignore it.
- When plan mode is active, use the `ask` tool every time before producing a plan. Ask any clarifying questions needed, or ask the user to confirm that no clarification is needed.
- Every Markdown plan file must start with a single descriptive H1 (`# ...`) before any `##` sections. Use the H1 as a stable, filesystem-safe worktree-flow title, not a generic label like `Plan`; `worktree-flow.py` derives branch, worktree, staging, and archive names from that header.
- Before performing any edit, briefly state in chat what files or behavior you intend to change and why. Do not wait for approval.
- Be concise by default: answer with only the decision, changed files, verification, and blockers; avoid background, step-by-step narration, repeated summaries, and optional detail unless the user asks for it.
