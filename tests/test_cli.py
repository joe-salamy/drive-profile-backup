"""Tests for CLI entry point."""

from __future__ import annotations

import tempfile
import os

from drive_backup.cli import main
from drive_backup.utils import human_size


class TestCliHumanSize:
    def test_bytes(self) -> None:
        assert human_size(100) == "100.0 B"

    def test_kilobytes(self) -> None:
        assert human_size(2048) == "2.0 KB"

    def test_megabytes(self) -> None:
        assert human_size(1024 * 1024) == "1.0 MB"


class TestCliMain:
    def test_dry_run_completes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # Create a file to scan
            with open(os.path.join(tmp, "test.txt"), "w") as f:
                f.write("hello")

            # Create a config file
            config_path = os.path.join(tmp, "config.yaml")
            with open(config_path, "w") as f:
                f.write(
                    f"backup_root: {tmp}\n"
                    "exclude_dirs: []\n"
                    "exclude_files: []\n"
                    f"manifest_path: {os.path.join(tmp, 'manifest.json')}\n"
                )

            main(["--dry-run", "--config", config_path])

    def test_verbose_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "test.txt"), "w") as f:
                f.write("hello")

            config_path = os.path.join(tmp, "config.yaml")
            with open(config_path, "w") as f:
                f.write(
                    f"backup_root: {tmp}\n"
                    "exclude_dirs: []\n"
                    "exclude_files: []\n"
                    f"manifest_path: {os.path.join(tmp, 'manifest.json')}\n"
                )

            main(["--dry-run", "--verbose", "--config", config_path])
