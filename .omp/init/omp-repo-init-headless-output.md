Done. Report written: `.omp/init/reports/20260703-110747-init-report.md`.

Changed:
- `README.md` rewritten as concise contributor runbook.
- `AGENTS.md` updated with OMP skill/command/secret guidance.
- `.omp/skills/*/SKILL.md` curated to 10 skills:
  - Created: `backup-pipeline-flow`, `scanner-exclusion-rules`, `manifest-dedup-state`, `google-drive-api-boundary`, `python-quality-gates`, `summary-reporting`.
  - Updated: `audit-worktree`, `implement-worktree`, `merge-conflict-resolver`, `optimize-repo-skills`.
- No `.omp/lsp.json` or `.omp/config.yml` created.

LSP:
- Kept OMP defaults.
- `ruff` language server is configured/resolved by OMP.
- `lsp diagnostics src/**/*.py`: no issues.

Verification run:
- `venv\Scripts\python.exe --version` ΓåÆ Python 3.13.5.
- `venv\Scripts\python.exe -m pytest` ΓåÆ 98 passed.
- `venv\Scripts\python.exe -m ruff check .` ΓåÆ passed.
- `venv\Scripts\python.exe -m mypy src tests scripts` ΓåÆ passed.
- `venv\Scripts\python.exe -m black --check src tests scripts` ΓåÆ passed.
- Skill/frontmatter review ΓåÆ 10 skills, 0 invalid.
- Secret-marker check on written files ΓåÆ no hits.

Not run:
- Real Google Drive OAuth/upload smoke test; requires configured OAuth client, browser authorization, and intentional Drive access.
- `copy config.example.yaml config.yaml`; existing ignored local `config.yaml` was preserved.
- Package install/update; dependency files were unchanged.
