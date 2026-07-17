"""Helpers for constructing filesystem test fixtures."""

from collections.abc import Mapping
from pathlib import Path


def write_tree(root: Path, files: Mapping[str, str | bytes]) -> None:
    """Write relative file paths and contents below root."""
    for relative_path, content in files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
