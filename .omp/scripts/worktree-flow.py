#!/usr/bin/env python3
"""Stable executable wrapper for the packaged worktree-flow runtime."""

from __future__ import annotations

from pathlib import Path

from worktree_flow.cli import main

if __name__ == "__main__":
    raise SystemExit(main(entrypoint_path=Path(__file__).resolve()))
