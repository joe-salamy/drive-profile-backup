"""CLI entry point for drive-backup."""

from __future__ import annotations

import argparse
import logging
import os
import sys

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
    TransferSpeedColumn,
)
from rich.table import Table

from drive_backup.config import load_config
from drive_backup.engine import BackupEngine
from drive_backup.scanner import FileEntry


def _human_size(num_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num_bytes) < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024  # type: ignore[assignment]
    return f"{num_bytes:.1f} PB"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="drive-backup",
        description="Incrementally back up your user profile to Google Drive",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and report only, no uploads",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml (default: ./config.yaml)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print each file as it is processed",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Ignore manifest, re-upload everything",
    )
    args = parser.parse_args(argv)

    # Setup logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Force UTF-8 output on Windows to avoid encoding errors with Rich
    if sys.platform == "win32":
        os.environ.setdefault("PYTHONIOENCODING", "utf-8")
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass

    console = Console(force_terminal=True)

    # Load config
    config = load_config(args.config)
    console.print(f"[bold]Backup root:[/] {config.backup_root}")
    if args.dry_run:
        console.print("[yellow]DRY RUN - no files will be uploaded[/]")

    # Create engine
    engine = BackupEngine(config, dry_run=args.dry_run, full=args.full)

    # Progress tracking
    progress = Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("|"),
        TransferSpeedColumn(),
        TextColumn("|"),
        TimeElapsedColumn(),
        console=console,
    )

    scan_task = None
    uploaded_bytes = 0

    def progress_callback(file: FileEntry, action: str) -> None:
        nonlocal uploaded_bytes
        if scan_task is not None:
            progress.advance(scan_task)
        if action.startswith("uploaded:") or action.startswith("would_upload:"):
            uploaded_bytes += file.size
            if args.verbose:
                tag = "UPLOAD" if action.startswith("uploaded:") else "WOULD UPLOAD"
                console.print(
                    f"  [{tag}] {file.relative_path} ({file.size_human})"
                )
        elif args.verbose and action == "skipped":
            console.print(
                f"  [SKIP] {file.relative_path} — {file.skip_reason}"
            )

    # Run backup
    with progress:
        scan_task = progress.add_task(
            "Backing up..." if not args.dry_run else "Scanning (dry run)...",
            total=None,  # Unknown total until scan completes
        )
        report = engine.run(progress_callback=progress_callback)

    # Print summary
    console.print()
    _print_summary(console, report)


def _print_summary(console: Console, report: dict) -> None:
    """Print a formatted summary table."""
    table = Table(title="Backup Summary", show_header=False)
    table.add_column("Key", style="bold")
    table.add_column("Value")

    if report["dry_run"]:
        table.add_row("Mode", "[yellow]DRY RUN[/]")

    table.add_row("Duration", report["duration_human"])
    table.add_row("Files scanned", str(report["files_scanned"]))
    table.add_row(
        "Files uploaded" if not report["dry_run"] else "Files to upload",
        str(report["files_uploaded"]),
    )
    table.add_row("Files skipped (dedup)", str(report["files_skipped_dedup"]))
    table.add_row("Files skipped (exclusion)", str(report["files_skipped_exclusion"]))
    table.add_row("Files skipped (error)", str(report["files_skipped_error"]))
    table.add_row(
        "Size uploaded" if not report["dry_run"] else "Size to upload",
        report["total_size_uploaded_human"],
    )
    table.add_row("Total eligible size", report["total_size_eligible_human"])

    if report.get("drive_folder_url"):
        table.add_row("Drive folder", report["drive_folder_url"])

    console.print(table)

    # Show top skipped files by size if there are any
    skipped = report.get("skipped_files", [])
    if skipped:
        console.print(
            f"\n[dim]{len(skipped)} files skipped. "
            f"Top 10 by size:[/]"
        )
        top_skipped = sorted(skipped, key=lambda f: f["size_bytes"], reverse=True)[:10]
        skip_table = Table(show_header=True, header_style="dim")
        skip_table.add_column("File", max_width=60)
        skip_table.add_column("Size", justify="right")
        skip_table.add_column("Reason")
        for sf in top_skipped:
            skip_table.add_row(
                sf["relative_path"], sf["size_human"], sf["reason"]
            )
        console.print(skip_table)

    errors = report.get("error_files", [])
    if errors:
        console.print(f"\n[red]{len(errors)} files had errors.[/]")


if __name__ == "__main__":
    main()
