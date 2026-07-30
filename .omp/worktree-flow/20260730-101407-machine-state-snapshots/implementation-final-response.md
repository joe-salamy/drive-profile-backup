Implemented and committed machine-state snapshots.

- **Commit:** `27a8d47c268c029ad603cbb7292c6719a06ecc88`
- **Branch:** `feature/machine-state-snapshots`
- **Handoff:** `.omp/handoff/implementation-summary.md` — untracked, not committed
- **Changed:** atomic JSON utility, collector configuration, Windows/WSL collectors, engine orchestration, prune protection, reporting, CLI flag/summary, documentation, and tests.

Verification:

- Focused suite: **95 passed**
- Full suite: **144 passed**
- Ruff: passed
- Mypy: passed
- Black check: passed
- Windows PowerShell smoke:
  - Refresh created and scanned `environment.json`, `wsl.json`, and `snapshot.json`
  - Both registered WSL distributions were inventoried
  - `--skip-machine-state` reported `Not refreshed`, scanned existing snapshots, and preserved all timestamps

No plan-required checks skipped. Pyright only reported pre-existing unresolved optional Google client imports in its environment.
