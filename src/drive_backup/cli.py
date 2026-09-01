"""CLI entry point for drive-backup."""

from __future__ import annotations

import argparse
import logging
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rich.console import Console

    from drive_backup.engine import ProgressEvent
    from drive_backup.report import BackupReport
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
        help="Mark Drive files missing locally as pruned in the manifest",
    )
    parser.add_argument(
        "--prune-trash",
        action="store_true",
        help="Move stale Drive files to trash and remove them from the manifest "
        "(default: --prune marks them as pruned)",
    )
    parser.add_argument(
        "--restore",
        action="store_true",
        help="Download the latest backup from Drive into --output, "
        "skipping pruned files",
    )
    parser.add_argument(
        "--output",
        metavar="PATH",
        help="Target directory for --restore",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing files during --restore",
    )
    parser.add_argument(
        "--skip-machine-state",
        action="store_true",
        help="Do not refresh generated machine-state inventories before this backup",
    )
    parser.add_argument(
        "--no-encrypt-secrets",
        action="store_true",
        help="Do not encrypt secret files; they will be skipped",
    )
    parser.add_argument(
        "--decrypt",
        dest="decrypt",
        action="store_true",
        help="Decrypt encrypted files on restore (default)",
    )
    parser.add_argument(
        "--no-decrypt",
        dest="decrypt",
        action="store_false",
        help="Do not decrypt on restore; keep .enc files",
    )
    parser.set_defaults(decrypt=True)
    parser.add_argument(
        "--decrypt-key",
        metavar="PATH",
        dest="decrypt_key",
        default=None,
        help="Path to secrets key for restore decryption (default: secrets_key_path from config)",
    )
    parser.add_argument(
        "--generate-secrets-key",
        action="store_true",
        help="Generate and display a new secrets encryption key and exit",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        metavar="PATH",
        help="Path to config YAML (default: config.yaml)",
    )
    args = parser.parse_args(argv)
    if args.full and (args.prune or args.prune_trash):
        parser.error(
            "--prune cannot be combined with --full because prune needs the existing manifest"
        )
    if args.prune and args.prune_trash:
        parser.error("--prune and --prune-trash are mutually exclusive")
    if args.restore and not args.output:
        parser.error("--restore requires --output")
    if args.restore and (args.full or args.prune or args.prune_trash):
        parser.error(
            "--restore cannot be combined with --full, --prune, or --prune-trash"
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
    from drive_backup.dedup import ManifestLoadError
    from drive_backup.engine import BackupEngine, ProgressKind

    console = Console()
    # Load config
    try:
        config = load_config(args.config)
    except ValueError as e:
        console.print(f"[red]Configuration failed:[/] {e}")
        raise SystemExit(1) from e

    # Apply CLI overrides for encryption
    if args.no_encrypt_secrets:
        config.encrypt_secrets = False

    # Handle key generation helper
    if args.generate_secrets_key:
        from drive_backup import crypto
        from rich.panel import Panel
        from rich.text import Text

        key_path = args.decrypt_key or config.secrets_key_path
        try:
            key, was_generated = crypto.load_or_generate_key(key_path)
            display = crypto.format_key_display(key, key_path)
            body = Text()
            body.append(f"Path: {display['path']}\n", style="bold")
            body.append(f"Hex:   {display['hex']}\n", style="cyan")
            body.append(f"Base64: {display['base64']}\n", style="green")
            body.append("\n")
            body.append(
                "Without this key encrypted backups on Drive CANNOT be decrypted.\n",
                style="bold red",
            )
            body.append(
                "Store offline (USB, 1Password, print). This is shown ONLY once at generation.",
                style="yellow",
            )
            title = (
                "[bold red]NEW SECRETS ENCRYPTION KEY — COPY AND SAVE NOW[/]"
                if was_generated
                else "[bold]Existing secrets key[/]"
            )
            panel = Panel(
                body,
                title=title,
                border_style="red" if was_generated else "green",
                expand=False,
            )
            console.print(panel)
            if was_generated:
                console.print("[green]New key generated and saved.[/]")
            else:
                console.print("[yellow]Existing key displayed (no new key generated).[/]")
        except Exception as e:
            console.print(f"[red]Failed to generate/load secrets key:[/] {e}")
            raise SystemExit(1) from e
        return

    console.print(f"[bold]Backup root:[/] {config.backup_root}")
    console.print(f"[bold]Profile:[/] {config.profile_name}")

    if args.restore:
        from rich.table import Table

        from drive_backup.restore import restore_backup
        from drive_backup.utils import human_size

        console.print(
            f"[bold]RESTORE - downloading non-pruned files to {args.output}[/]"
        )
        try:
            result = restore_backup(
                config,
                args.output,
                dry_run=args.dry_run,
                force=args.force,
                decrypt=args.decrypt,
                decrypt_key_path=args.decrypt_key,
            )
        except RuntimeError as error:
            console.print(f"[red]Restore failed:[/] {error}")
            raise SystemExit(1) from error

        table = Table(title="Restore Summary", show_header=False)
        table.add_column("Key", style="bold")
        table.add_column("Value")
        if args.dry_run:
            table.add_row("Mode", "[yellow]DRY RUN[/]")
        table.add_row("Profile", result["profile_name"])
        table.add_row("Output directory", result["output_dir"])
        table.add_row(
            "Files to restore" if args.dry_run else "Files restored",
            str(result["files_restored"]),
        )
        table.add_row(
            "Size to restore" if args.dry_run else "Size restored",
            human_size(result["bytes_restored"]),
        )
        table.add_row("Files skipped (pruned)", str(result["files_skipped_pruned"]))
        table.add_row("Files skipped (existing)", str(result["files_skipped_existing"]))
        table.add_row("Files failed", str(result["files_failed"]))
        console.print(table)
        if args.verbose and result["errors"]:
            console.print(f"\n[red]{len(result['errors'])} files failed:[/]")
            for restore_error in result["errors"]:
                console.print(
                    f"  [red]{restore_error['relative_path']}: "
                    f"{restore_error['error']}[/]"
                )
        return

    prune_enabled = args.prune or args.prune_trash
    prune_mode = "trash" if args.prune_trash else "flag"

    if args.dry_run and prune_enabled:
        console.print("[yellow]DRY RUN - no files will be uploaded or pruned[/]")
    elif args.dry_run:
        console.print("[yellow]DRY RUN - no files will be uploaded[/]")
    elif prune_mode == "trash":
        console.print("[yellow]PRUNE - stale Drive files will be moved to trash[/]")
    elif prune_enabled:
        console.print(
            "[yellow]PRUNE - stale Drive files will be marked as pruned "
            "(use --prune-trash to delete)[/]"
        )

    # Create engine
    engine = BackupEngine(
        config,
        dry_run=args.dry_run,
        full=args.full,
        prune=prune_enabled,
        prune_mode=prune_mode,
        collect_machine_state_snapshot=not args.skip_machine_state,
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

    def progress_callback(file: FileEntry, event: ProgressEvent) -> None:
        if scan_task is not None:
            progress.advance(scan_task)
        if event.kind in (ProgressKind.UPLOADED, ProgressKind.WOULD_UPLOAD):
            if args.verbose:
                tag = (
                    "UPLOAD" if event.kind is ProgressKind.UPLOADED else "WOULD UPLOAD"
                )
                console.print(f"  [{tag}] {file.relative_path} ({file.size_human})")
        elif args.verbose and event.kind is ProgressKind.SKIPPED:
            console.print(f"  [SKIP] {file.relative_path} — {file.skip_reason}")

    try:
        with progress:
            scan_task = progress.add_task(
                "Backing up..." if not args.dry_run else "Scanning (dry run)...",
                total=None,
            )
            report = engine.run(progress_callback=progress_callback)
    except ManifestLoadError as error:
        console.print(f"[red]Backup failed:[/] {error}")
        raise SystemExit(1) from error

    # Print summary
    console.print()
    _print_summary(console, report, verbose=args.verbose)


def _print_summary(
    console: Console, report: BackupReport, *, verbose: bool = False
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
    if report.get("files_encrypted_uploaded"):
        table.add_row(
            "Files encrypted" if not report["dry_run"] else "Files to encrypt",
            str(report["files_encrypted_uploaded"]),
        )
        table.add_row(
            "Size encrypted" if not report["dry_run"] else "Size to encrypt",
            report.get("total_size_encrypted_human", "0.0 B"),
        )
    table.add_row(
        "Size uploaded" if not report["dry_run"] else "Size to upload",
        report["total_size_uploaded_human"],
    )
    table.add_row("Total eligible size", report["total_size_eligible_human"])
    collector_rows = report["machine_state_collectors"]
    if report["machine_state_refreshed"]:
        counts = {
            status: sum(row["status"] == status for row in collector_rows)
            for status in ("succeeded", "partial", "failed")
        }
        machine_state_summary = (
            f"{counts['succeeded']} succeeded, {counts['partial']} partial, "
            f"{counts['failed']} failed"
        )
    else:
        machine_state_summary = "Not refreshed"
    table.add_row("Machine state", machine_state_summary)

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

    if not report["dry_run"]:
        if report["manifest_snapshot_error"]:
            snapshot_text = f"failed: {report['manifest_snapshot_error']}"
        elif report["manifest_snapshot_uploaded"]:
            snapshot_text = "uploaded"
        elif report["manifest_snapshot_downloaded"]:
            snapshot_text = "downloaded"
        else:
            snapshot_text = "not synced"
        table.add_row("Manifest snapshot", snapshot_text)
    console.print(table)
    for collector in collector_rows:
        if collector["status"] not in ("partial", "failed"):
            continue
        for warning in collector["warnings"]:
            console.print(f"[yellow]{collector['name']}: {warning}[/]")

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
