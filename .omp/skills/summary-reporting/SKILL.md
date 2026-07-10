---
name: summary-reporting
description: Use when changing JSON backup reports, CLI summary output, report upload/save behavior, or scripts/generate_summary.py profile markdown reports.
---

# Summary Reporting

## Scope

Use this skill for `src/drive_backup/report.py`, CLI summary rendering, engine report statistics, and `scripts/generate_summary.py`.

## JSON report invariants

- Reports are JSON-serializable plain dictionaries.
- Uploaded, skipped, error, pruned, and prune-error files keep enough path/size/reason data for user audit.
- Duration and human-size fields are presentation helpers; numeric totals remain available for calculations.
- Dry-run reports must clearly distinguish would-upload and would-prune behavior from completed writes.
- Backup reports upload under the Drive profile folder's `_reports` subfolder during non-dry-run backup.

## Markdown summary script invariants

- `scripts/generate_summary.py` reads backup config, scans with backup filters, and writes markdown under `docs/` by default.
- `--full-profile` produces unrestricted profile comparisons.
- `--include-appdata` is explicit because AppData scans can be slow.
- Do not read, summarize, or modify `docs/scratchpad.md`.

## Verification

```powershell
pytest tests/test_report.py tests/test_cli.py tests/test_engine.py
```

For script changes, run a temporary-output smoke check when safe:

```powershell
python scripts/generate_summary.py --out <temp-dir>
```

## Sources

- Rich console/progress behavior for CLI display: https://rich.readthedocs.io/en/stable/progress.html
