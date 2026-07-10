---
name: python-quality-gates
description: Use before broad Python refactors, dependency changes, typing changes, formatter/linter configuration changes, or final verification of source/test edits.
---

# Python Quality Gates

## Repo toolchain

- Python package metadata and tool config live in `pyproject.toml`.
- Runtime package is under `src/drive_backup/`.
- Tests live under `tests/` and use pytest with mocks and temporary directories.
- Dev extra installs `black`, `mypy`, `pytest`, `pytest-cov`, `ruff`, and `types-PyYAML`.
- Mypy is strict: `strict = true`, `disallow_untyped_defs = true`, and Python target is 3.11.
- Ruff and Black target Python 3.11.

## Commands

```powershell
pytest
ruff check .
mypy src tests scripts
black --check src tests scripts
```

Format only when needed:

```powershell
black src tests scripts
```

## Testing guidance

- Prefer real temporary files/directories via `tmp_path` or `tempfile`; do not create repo-local fixtures unless durable.
- Mock Drive API and OAuth boundaries; do not hit Google in tests.
- Assert behavior and state transitions: manifest saved/not saved, report fields, skip reasons, Drive calls, CLI exit errors.
- Keep helper fakes typed enough to satisfy strict mypy.

## Sources

- pytest `tmp_path`: https://docs.pytest.org/en/stable/how-to/tmp_path.html
- pytest monkeypatch: https://docs.pytest.org/en/stable/how-to/monkeypatch.html
- mypy configuration: https://mypy.readthedocs.io/en/stable/config_file.html
- Ruff configuration: https://docs.astral.sh/ruff/configuration/
- Black configuration: https://black.readthedocs.io/en/stable/usage_and_configuration/the_basics.html
