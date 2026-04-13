# Repo Checklist

Itemized checklist for repo-level file and configuration audits. Each item includes how to verify and its severity.

## Severity Definitions

| Severity       | Meaning                                                        |
| -------------- | -------------------------------------------------------------- |
| Blocker        | Must fix before publishing — risk of data leak or broken repo  |
| Recommendation | Should fix for quality and first impressions                   |
| Optional       | Nice to have for mature open-source projects                   |

---

## 1. Security & Privacy

### 1.1 .gitignore Completeness — Blocker

**Verify:** Read `.gitignore` and confirm it includes at minimum:

```
# Virtual environments
venv/
.venv/
env/

# Environment / secrets
.env
.env.*
*.pem
*.key

# Python
__pycache__/
*.pyc
*.pyo
*.egg-info/
dist/
build/

# Type checking / linting caches
.mypy_cache/
.ruff_cache/
.pytest_cache/

# IDE
.vscode/
.idea/
*.swp
*.swo
*~

# AI tooling
.claude/
CLAUDE.md

# OS
.DS_Store
Thumbs.db
```

**Pass:** All relevant patterns present. Missing patterns for tools not used by the project are acceptable.

### 1.2 No Tracked Secret Files — Blocker

**Verify:** Run:
```bash
git ls-files | grep -iE '\.(env|pem|key|p12|pfx|jks|keystore|credentials|secret)$'
git ls-files | grep -iE '(credentials|secrets?|tokens?)\.(json|yaml|yml|toml|ini|cfg)$'
```

**Pass:** No results, or results are clearly template/example files (e.g., `.env.example` with no real values).

### 1.3 No Secrets in Source Code — Blocker

**Verify:** Search source files for common secret patterns:
```bash
grep -rnI --include="*.py" -E '(password|secret|api_key|api_secret|token|private_key)\s*=' .
grep -rnI --include="*.py" -E '(sk-[a-zA-Z0-9]{20,}|ghp_[a-zA-Z0-9]{36}|AKIA[0-9A-Z]{16})' .
grep -rnI --include="*.py" '-----BEGIN.*PRIVATE KEY-----' .
```

**Pass:** No real credentials found. False positives to ignore: test fixtures with dummy values, documentation examples, environment variable reads (`os.getenv()`), constant names without values.

### 1.4 No Secrets in Git History — Blocker

**Verify:** Search commit diffs for secret patterns:
```bash
git log -p --all -S 'password' --diff-filter=A -- '*.py' '*.env' '*.cfg' '*.ini' '*.yaml' '*.yml' '*.json'
git log -p --all -S 'api_key' --diff-filter=A -- '*.py' '*.env' '*.cfg' '*.ini'
git log -p --all -S 'secret' --diff-filter=A -- '*.py' '*.env' '*.cfg' '*.ini'
git log -p --all -S 'BEGIN.*PRIVATE KEY' -- '*.py' '*.pem' '*.key'
```

**Pass:** No real credentials in history. If secrets were committed and removed, recommend `git filter-repo` or `BFG Repo-Cleaner` to scrub history before publishing.

### 1.5 No Hardcoded Personal Paths — Recommendation

**Verify:** Search for absolute paths with usernames:
```bash
grep -rnI --include="*.py" -E '(C:\\Users\\|/home/|/Users/)[a-zA-Z]' .
```

**Pass:** No hardcoded user-specific paths. Use `pathlib.Path.home()` or relative paths instead.

### 1.6 AI Tooling Files Excluded — Recommendation

**Verify:** Check whether Claude Code files are tracked:
```bash
git ls-files | grep -iE '(^\.claude/|^CLAUDE\.md$)'
```

**Pass:** No results, or patterns already in `.gitignore`.

**Remediation:** Add `.claude/` and `CLAUDE.md` to `.gitignore`, then remove from the index only (preserves history):
```bash
git rm --cached -r .claude/
git rm --cached CLAUDE.md
```
Do **not** scrub these from git history — they contain no secrets and serve as a record of the tooling used during development.

### 1.7 No Personal Information — Recommendation

**Verify:** Manually review README, config files, and comments for:
- Personal email addresses (non-public)
- Phone numbers
- Internal company URLs or IP addresses
- Names of colleagues or private organizations

**Pass:** Only intentionally public contact info present (e.g., author email in pyproject.toml).

---

## 2. License

### 2.1 LICENSE File Exists — Blocker

**Verify:** Check for `LICENSE`, `LICENSE.md`, or `LICENSE.txt` at repo root.

**Pass:** File exists with a recognized open-source license body (MIT, Apache-2.0, GPL-3.0, BSD-2-Clause, BSD-3-Clause, ISC, MPL-2.0, etc.).

### 2.2 License Field in Package Metadata — Recommendation

**Verify:** If `pyproject.toml` exists, check for:
```toml
[project]
license = {text = "MIT"}  # or license = "MIT" (PEP 639)
```
Or in `setup.cfg`:
```ini
[metadata]
license = MIT
```

**Pass:** License field present and matches the LICENSE file content. If no `pyproject.toml`/`setup.cfg`, skip.

---

## 3. README

### 3.1 README Exists — Blocker

**Verify:** Check for `README.md` or `README.rst` at repo root.

**Pass:** File exists and is non-empty.

### 3.2 README Required Sections — Recommendation

**Verify:** README should contain:
- **Project description** — What does this project do? (first paragraph or heading)
- **Installation** — How to install (`pip install`, clone + setup, etc.)
- **Usage** — Basic quickstart or example
- **License** — Mention of license type (can be a one-liner at the bottom)

**Pass:** All four sections present in some form. Exact headings don't matter.

### 3.3 README Freshness — Recommendation

**Verify:** Cross-check references in README against actual code:
- Module/package names referenced exist
- CLI commands shown actually work
- Function/class names mentioned exist in source
- Installation commands reference the correct package name

**Pass:** No stale references found.

---

## 4. Dependencies

### 4.1 Dependency File Exists — Recommendation

**Verify:** Check for one of:
- `requirements.txt`
- `pyproject.toml` with `[project.dependencies]`
- `setup.py` with `install_requires`
- `setup.cfg` with `install_requires`

**Pass:** At least one dependency specification exists.

### 4.2 Dependencies Match Imports — Recommendation

**Verify:** Cross-reference:
```bash
# Find all imports
grep -rn --include="*.py" -E '^(import |from [a-zA-Z])' . | grep -v venv | grep -v __pycache__
```
Compare against declared dependencies. Check for:
- Third-party imports missing from dependencies
- Declared dependencies not imported anywhere (may be indirect — verify before flagging)

**Pass:** All third-party imports have corresponding dependency declarations.

### 4.3 Python Version Constraint — Recommendation

**Verify:** Check for:
- `pyproject.toml`: `requires-python = ">=3.x"`
- `setup.cfg`: `python_requires = >=3.x`
- `.python-version` file

**Pass:** Python version constraint is declared somewhere.

### 4.4 Dev Dependencies Separated — Recommendation

**Verify:** Dev-only packages (pytest, ruff, black, mypy, pre-commit, etc.) should not be in production dependencies:
- `requirements.txt` → separate `requirements-dev.txt`
- `pyproject.toml` → `[project.optional-dependencies]` with a `dev` group

**Pass:** Dev tools are not in the main dependency list.

---

## 5. Repo Hygiene

### 5.1 No Large Binary Files — Recommendation

**Verify:**
```bash
git ls-files | while read f; do wc -c "$f"; done | sort -rn | head -20
```
Flag files over 1MB that are binary (images, compiled files, data files).

**Pass:** No large binaries tracked. If needed, use Git LFS or document why they're included.

### 5.2 No Generated Files Committed — Recommendation

**Verify:** Check that these are NOT tracked:
```bash
git ls-files | grep -E '(dist/|build/|\.egg-info/|node_modules/|\.tox/)'
```

**Pass:** No generated/build artifact directories tracked.

---

## 6. Optional Files

These are not required but are recommended for mature projects. Report as **Optional Enhancement** if missing.

| File                              | Purpose                              |
| --------------------------------- | ------------------------------------ |
| `CHANGELOG.md`                    | Track notable changes per release    |
| `CONTRIBUTING.md`                 | Guide for contributors               |
| `CODE_OF_CONDUCT.md`             | Community standards                  |
| `SECURITY.md`                     | Vulnerability reporting instructions |
| `.github/ISSUE_TEMPLATE/`        | Structured issue creation            |
| `.github/PULL_REQUEST_TEMPLATE.md`| PR guidelines                       |
| `.github/workflows/*.yml`        | CI/CD automation                     |
| `.pre-commit-config.yaml`        | Pre-commit hooks                     |
