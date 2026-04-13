---
name: cleanup-dead-code
description: Dead Code Cleanup for Python (using Vulture)
---

Vulture is installed in the project venv (not globally).

1. Run `python -m vulture . --min-confidence 60 --sort-by-size --exclude "venv,.venv,__pycache__,.git,build,dist"` and present the report:
   - Group by file, show confidence levels and size impact
   - Breakdown by type (functions, classes, variables, imports, modules), largest first
   - Flag potential false positives (entry points, CLI commands, tests, dynamic usage)
   - Suggest `--exclude` or `.vulture_whitelist.py` entries if patterns emerge

2. After I approve, remove dead code automatically (no per-edit confirmation):
   - Work file-by-file in small batches
   - Show clear diffs for each edit
   - Preserve formatting, comments, docstrings, and structure
   - Skip anything below 80% confidence unless obviously safe

3. Run `ruff check --fix --select F401,I` to clean up remaining unused imports

**Safety rules:**

- Never break public APIs, framework conventions, or tests
- Account for implicit/dynamic usage
- Suggest `git stash` before large changes
