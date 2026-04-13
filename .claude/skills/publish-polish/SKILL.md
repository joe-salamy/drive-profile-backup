---
name: publish-polish
description: Final audit and polish workflow for converting a personal Python project into a publication-ready open-source GitHub repository. Checks security and privacy (including git history), code quality, documentation, packaging metadata, and repo hygiene, then produces a structured readiness report with prioritized findings. Invoke before making a private repo public or before a first release.
license: MIT
metadata:
  version: "1.0.0"
  domain: quality
  triggers: publish, open source, release, go public, publish polish, make public, publication ready
  role: specialist
  scope: audit
  output-format: report
  related-skills: code-reviewer, python-pro, cleanup-dead-code
---

# Publish Polish

Final audit for making a personal Python project ready for open-source publication. Produces a structured readiness report with a clear verdict.

## When to Use This Skill

- Before making a private GitHub repo public
- Before tagging a first release or v1.0
- When preparing a personal project for community contributions
- Before submitting a project as a portfolio piece

## Core Workflow

1. **Privacy & Security Sweep** — Scan for secrets, credentials, API keys, hardcoded paths, personal information, and private data in the working tree. Search git history for accidentally committed secrets. Review `.gitignore` for completeness against canonical Python patterns. Load `references/repo-checklist.md` for scanning patterns. **This step is non-negotiable — mistakes here cannot be undone after push.**

2. **License & Legal** — Verify a LICENSE file exists at repo root with a recognized open-source license. Check that `pyproject.toml` or `setup.cfg` license field matches if present. If no license exists, recommend one (MIT for permissive, Apache 2.0 if patent grant matters, GPL if copyleft desired).

3. **Code Polish** — Run formatting and lint checks. Audit TODO/FIXME/HACK comments. Check docstrings on public functions/classes. Check type hint coverage on public API. Verify tests exist and pass. Load `references/code-quality.md` for detailed criteria.

4. **Documentation** — Verify README.md exists and contains: project description, installation instructions, basic usage/quickstart, and license mention. The README should be concise — keep it short and focused, avoiding unnecessary verbosity. Check that it is current relative to the actual code (imports, CLI commands, function names referenced actually exist).

5. **Packaging & Dependencies** — Detect which dependency approach the project uses and check accordingly:
   - **requirements.txt**: exists, matches actual imports, dev deps separated (e.g., `requirements-dev.txt`)
   - **pyproject.toml**: `[project.dependencies]` populated, `requires-python` set, `[project.optional-dependencies]` for dev deps
   - Cross-reference declared dependencies against actual `import` statements in source
   - Confirm no pinned dev-only dependencies leak into production deps

6. **Report** — Produce a structured readiness report using `references/report-template.md`. Categorize findings as Blockers, Recommendations, and Optional Enhancements. Provide a clear verdict: **Ready** / **Almost Ready** / **Needs Work**.

## Reference Guide

| Topic          | Reference                       | Load When                                  |
| -------------- | ------------------------------- | ------------------------------------------ |
| Repo Checklist | `references/repo-checklist.md`  | Steps 1-2, 4-5: repo-level file audit      |
| Code Quality   | `references/code-quality.md`    | Step 3: code polish and test checks        |
| Report Format  | `references/report-template.md` | Step 6: writing the final readiness report |

## Constraints

### MUST DO

- Run the privacy/security sweep FIRST, before any other step
- Check git history (not just working tree) for leaked secrets
- Verify `.gitignore` covers: `venv/`, `.env`, `__pycache__/`, `*.pyc`, `.mypy_cache/`, `dist/`, `build/`, `*.egg-info/`, IDE configs (`.vscode/`, `.idea/`), AI tooling (`.claude/`, `CLAUDE.md`)
- Confirm LICENSE file exists and is a recognized OSS license
- Verify README has at minimum: description, install, usage — and keep it concise
- Check that requirements/dependencies match actual imports
- Produce the structured report at the end

### MUST NOT DO

- Skip the security sweep or deprioritize it
- Require enterprise-grade scaffolding (CONTRIBUTING.md, CODE_OF_CONDUCT.md, CI/CD, SECURITY.md are optional recommendations, not blockers)
- Demand 100% test coverage or strict mypy for personal projects
- Modify any files without explicit user approval — this skill audits only
- Push to remote

### NICE-TO-HAVE (report as Optional Enhancements)

- CHANGELOG.md
- CONTRIBUTING.md
- GitHub Actions CI workflow
- Issue/PR templates
- Code of Conduct
- SECURITY.md
- Pre-commit hooks configuration
- Badges in README (build status, coverage, license)

## Output Template

The readiness report must follow this structure:

1. **Project** — Name, description, Python version
2. **Verdict** — Ready / Almost Ready / Needs Work
3. **Blockers** — Must fix before publishing (secrets, missing license, broken code, private data exposure)
4. **Recommendations** — Should fix for a good first impression (README gaps, missing docstrings, stale TODOs, unpinned dependencies)
5. **Optional Enhancements** — Nice to have (CI, CONTRIBUTING.md, badges, etc.)
6. **Checklist Summary** — Table of all checks with pass/fail/skip status

## Knowledge Reference

OWASP secrets management, SPDX license identifiers, Python packaging (PEP 517/518/621), .gitignore patterns, semantic versioning
