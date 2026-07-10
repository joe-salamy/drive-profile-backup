"""CLI entry point for drive-backup."""

from __future__ import annotations

import argparse
import logging
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from rich.console import Console

    from drive_backup.scanner import FileEntry


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
        "--verbose",
        action="store_true",
        help="Print each file as it is processed",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Ignore manifest, re-upload everything",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="Move Drive files missing locally to trash and remove them from the manifest",
    )
    args = parser.parse_args(argv)
    if args.full and args.prune:
        parser.error(
            "--prune cannot be combined with --full because prune needs the existing manifest"
        )

    # Setup logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Force UTF-8 output on Windows to avoid encoding errors with Rich
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
            sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass

    # Lazy imports — rich and engine are heavy; defer until after arg parsing
    from rich.console import Console
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        TextColumn,
        TimeElapsedColumn,
    )

    from drive_backup.config import load_config
    from drive_backup.engine import BackupEngine

    console = Console()

    # Load config
    try:
        config = load_config("config.yaml")
    except ValueError as e:
        console.print(f"[red]Configuration failed:[/] {e}")
        raise SystemExit(1) from e
    console.print(f"[bold]Backup root:[/] {config.backup_root}")
    console.print(f"[bold]Profile:[/] {config.profile_name}")

    if args.dry_run and args.prune:
        console.print("[yellow]DRY RUN - no files will be uploaded or pruned[/]")
    elif args.dry_run:
        console.print("[yellow]DRY RUN - no files will be uploaded[/]")
    elif args.prune:
        console.print("[yellow]PRUNE - stale Drive files will be moved to trash[/]")

    # Create engine
    engine = BackupEngine(
        config, dry_run=args.dry_run, full=args.full, prune=args.prune
    )

    # Progress tracking
    progress = Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("|"),
        TimeElapsedColumn(),
        console=console,
    )

    scan_task = None

    def progress_callback(file: FileEntry, action: str) -> None:
        if scan_task is not None:
            progress.advance(scan_task)
        if action.startswith("uploaded:") or action.startswith("would_upload:"):
            if args.verbose:
                tag = "UPLOAD" if action.startswith("uploaded:") else "WOULD UPLOAD"
                console.print(f"  [{tag}] {file.relative_path} ({file.size_human})")
        elif args.verbose and action == "skipped":
            console.print(f"  [SKIP] {file.relative_path} — {file.skip_reason}")

    # Run backup
    with progress:
        scan_task = progress.add_task(
            "Backing up..." if not args.dry_run else "Scanning (dry run)...",
            total=None,
        )
        report = engine.run(progress_callback=progress_callback)

    # Print summary
    console.print()
    _print_summary(console, report, verbose=args.verbose)


def _print_summary(
    console: Console, report: dict[str, Any], *, verbose: bool = False
) -> None:
    """Print a formatted summary table."""
    from rich.table import Table

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

    if report.get("prune_enabled") or report.get("files_pruned", 0):
        table.add_row(
            "Files to prune" if report["dry_run"] else "Files pruned",
            str(report.get("files_pruned", 0)),
        )
        table.add_row(
            "Size to prune" if report["dry_run"] else "Size pruned",
            report.get("total_size_pruned_human", "0.0 B"),
        )
        table.add_row("Prune failures", str(report.get("files_prune_failed", 0)))
    console.print(table)

    # Show top uploaded (or to-be-uploaded) files by size
    uploaded = report.get("uploaded_files", [])
    if uploaded:
        label = "to upload" if report["dry_run"] else "uploaded"
        console.print(f"\n[bold]Top 10 biggest files {label}:[/]")
        top_uploads = sorted(uploaded, key=lambda f: f["size_bytes"], reverse=True)[:10]
        upload_table = Table(show_header=True, header_style="dim")
        upload_table.add_column("File", max_width=60)
        upload_table.add_column("Size", justify="right")
        upload_table.add_column("Type")
        for uf in top_uploads:
            upload_table.add_row(uf["relative_path"], uf["size_human"], uf["extension"])
        console.print(upload_table)

    # Show breakdown by file type
    breakdown = report.get("extension_breakdown", [])
    if breakdown:
        label = "to upload" if report["dry_run"] else "uploaded"
        console.print(f"\n[bold]Breakdown by file type ({label}):[/]")
        bd_table = Table(show_header=True, header_style="dim")
        bd_table.add_column("Type")
        bd_table.add_column("Files", justify="right")
        bd_table.add_column("Size", justify="right")
        for row in breakdown:
            bd_table.add_row(row["extension"], str(row["count"]), row["size_human"])
        console.print(bd_table)

    pruned_files = report.get("pruned_files", [])
    if pruned_files:
        if verbose:
            title = "Files to prune:" if report["dry_run"] else "Files pruned:"
            rows = sorted(pruned_files, key=lambda f: f["relative_path"])
        else:
            title = (
                "Top 10 biggest files to prune:"
                if report["dry_run"]
                else "Top 10 biggest files pruned:"
            )
            rows = sorted(pruned_files, key=lambda f: f["size_bytes"], reverse=True)[
                :10
            ]
        console.print(f"\n[bold]{title}[/]")
        prune_table = Table(show_header=True, header_style="dim")
        prune_table.add_column("File", max_width=60)
        prune_table.add_column("Size", justify="right")
        prune_table.add_column("Drive ID")
        for pf in rows:
            prune_table.add_row(
                pf["relative_path"], pf["size_human"], pf["drive_file_id"]
            )
        console.print(prune_table)

    if report.get("prune_skipped_reason"):
        console.print(f"\n[yellow]{report['prune_skipped_reason']}[/]")

    prune_error_files = report.get("prune_error_files", [])
    if prune_error_files:
        console.print(f"\n[red]{len(prune_error_files)} prune operations failed.[/]")

    errors = report.get("error_files", [])
    if errors:
        console.print(f"\n[red]{len(errors)} files had errors.[/]")


if __name__ == "__main__":
    main()
