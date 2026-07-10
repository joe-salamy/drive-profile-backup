---
name: scanner-exclusion-rules
description: Use when changing profile scanning, Windows path handling, exclusions, symlink behavior, size limits, or config fields that decide whether files are eligible for backup.
---

# Scanner Exclusion Rules

## Scope

Use this skill for `src/drive_backup/scanner.py`, `config.py`, `config.example.yaml`, and tests under `tests/test_scanner.py` or `tests/test_config.py`.

## Invariants

- `backup_root` defaults to the current user's home directory when blank.
- `profile_name` is required and must stay safe for Drive folder names and local manifest paths.
- Directory exclusions prune traversal; file/path/specific-file exclusions yield skipped `FileEntry` records with metadata.
- Media extensions in `no_size_limit` bypass the default size cap.
- Per-extension size limit `0` means skip that file type.
- Symlinks and NTFS junctions stay excluded by default to avoid loops.
- On Windows, long-path handling must preserve manifest identity: `relative_path` is the real relative path, not the display-truncated path.
- `.omp`, `.claude`, `.codex`, caches, generated builds, and local state remain excluded by default.

## Change pattern

1. Update `Config` defaults and `config.example.yaml` together.
2. Keep skip reasons stable unless tests and report consumers are updated.
3. Prefer adding a narrower exclusion path/pattern over broad root-level pruning.
4. Do not read or modify `docs/scratchpad.md` while validating docs-related exclusions.

## Verification

```powershell
pytest tests/test_config.py tests/test_scanner.py
```

For manual checks, use temporary directories in tests rather than scanning a real profile.

## Sources

- PyYAML loading guidance for config parsing: https://pyyaml.org/wiki/PyYAMLDocumentation
- pytest temp-path fixture guidance: https://docs.pytest.org/en/stable/how-to/tmp_path.html
