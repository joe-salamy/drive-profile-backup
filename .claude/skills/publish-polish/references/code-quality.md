# Code Quality Checks

Code-level checks to run during the polish step. These focus on the source code itself rather than repo scaffolding.

---

## 1. Formatting & Linting

### Check for Existing Config

Look for formatter/linter configuration in:
- `pyproject.toml` → `[tool.ruff]`, `[tool.black]`, `[tool.isort]`
- `ruff.toml` or `.ruff.toml`
- `setup.cfg` → `[flake8]`, `[isort]`

### If Configured

Run the configured tools:
```bash
ruff check .          # or: flake8 .
ruff format --check . # or: black --check .
```

Report any violations as **Recommendation** items.

### If Not Configured

Note the absence as a **Recommendation**: suggest adding `[tool.ruff]` to `pyproject.toml` with a minimal config:
```toml
[tool.ruff]
target-version = "py311"  # match project's minimum Python
line-length = 88

[tool.ruff.lint]
select = ["E", "F", "I", "W"]
```

Do not block on this — many personal projects ship without a formatter configured.

---

## 2. Docstrings

### What to Check

Focus on **public API** — functions, classes, and modules that users of the project would interact with:
- Module-level docstrings in `__init__.py` and main entry points
- Public class docstrings (classes without leading `_`)
- Public function/method docstrings (functions without leading `_`)

### How to Check

```bash
# Find public functions/classes missing docstrings
# Look for def/class lines and check if the next non-empty line is a docstring
grep -n "def [a-z]" *.py **/*.py | grep -v "def _"
grep -n "class [A-Z]" *.py **/*.py | grep -v "class _"
```

Or if ruff is available:
```bash
ruff check --select D --ignore D100,D104 .  # pydocstyle rules
```

### Severity

- Missing docstrings on public API: **Recommendation**
- Missing docstrings on internal helpers: Skip — not worth flagging for personal projects

### Style

If docstrings exist, check they follow a consistent style (Google, NumPy, or Sphinx). Don't flag style inconsistency as a blocker — just note the recommendation.

---

## 3. Type Hints

### What to Check

Focus on **public function signatures**:
- Parameter types
- Return types
- No need to check internal variable annotations

### How to Check

```bash
# Find public functions without return type annotations
grep -n "def [a-z][a-zA-Z_]*(" **/*.py | grep -v "def _" | grep -v " -> "
```

Or if mypy is configured:
```bash
mypy . --ignore-missing-imports
```

### Severity

- Missing type hints on public API: **Recommendation**
- Strict mypy compliance: **Optional** — do not require for personal projects
- If mypy is configured and has errors: **Recommendation** to fix or adjust config

---

## 4. TODO / FIXME / HACK Audit

### How to Check

```bash
grep -rnI --include="*.py" -E '\b(TODO|FIXME|HACK|XXX|NOCOMMIT|TEMP)\b' .
```

### For Each Match, Decide

- **Resolved but not cleaned up** → Recommendation to remove the comment
- **Still relevant** → Recommendation to convert to a GitHub issue and reference it: `# TODO(#42): description`
- **Intentional / acceptable** → Note in report but don't flag as action item
- **NOCOMMIT** → **Blocker** — these should never ship

### Severity

- NOCOMMIT tags: **Blocker**
- Unresolved TODO/FIXME: **Recommendation**
- HACK with no explanation: **Recommendation** to either explain or refactor

---

## 5. Tests

### Existence Check

Look for test files:
```bash
# Common test locations
ls -d tests/ test/ 2>/dev/null
find . -name "test_*.py" -o -name "*_test.py" | grep -v venv | grep -v __pycache__ | head -20
```

### If Tests Exist

Run them if possible:
```bash
python -m pytest --tb=short -q 2>&1 | tail -20
```

Report:
- All passing: note in checklist as pass
- Failures: **Recommendation** to fix before publishing
- If pytest not installed: note as skip, recommend adding to dev dependencies

### If No Tests Exist

Report as **Recommendation** — having some tests improves credibility but is not a blocker for personal projects.

### Coverage (Optional)

If `pytest-cov` is available:
```bash
python -m pytest --cov=. --cov-report=term-missing -q 2>&1 | tail -10
```

Report coverage percentage as informational. Do not set a minimum threshold.

---

## 6. Secrets in Source Code

### Patterns to Search

```bash
# Hardcoded credentials
grep -rnI --include="*.py" -E '(password|passwd|secret|api_key|api_secret|access_key|private_key)\s*=\s*["\x27][^"\x27]{8,}' .

# Known API key formats
grep -rnI --include="*.py" -E '(sk-[a-zA-Z0-9]{20,}|ghp_[a-zA-Z0-9]{36}|AKIA[0-9A-Z]{16}|xox[bpoa]-[0-9a-zA-Z-]+)' .

# Private keys
grep -rnI --include="*.py" '-----BEGIN.*PRIVATE KEY-----' .

# Connection strings with credentials
grep -rnI --include="*.py" -E '(mysql|postgres|mongodb|redis)://[^:]+:[^@]+@' .
```

### False Positives to Ignore

- `os.getenv("API_KEY")` or `os.environ["SECRET"]` — reading from environment is fine
- `password = None` or `password = ""` — empty/null assignments
- Test fixtures with obviously fake values (`password = "test123"`, `api_key = "fake-key"`)
- Constants that are names, not values (`PASSWORD_FIELD = "password"`)
- Documentation strings explaining what a variable is for

### Severity

- Real credentials found: **Blocker**
- Suspicious patterns that need manual review: **Recommendation** to verify
