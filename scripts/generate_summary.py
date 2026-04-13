"""Generate a profile-summary-[date].md from a live dry-run scan.

Usage:
    python scripts/generate_summary.py [--config config.yaml] [--out docs/]
"""

from __future__ import annotations

import argparse
import os
from collections import defaultdict
from datetime import date
from pathlib import Path

from drive_backup.config import load_config
from drive_backup.scanner import FileEntry, scan
from drive_backup.utils import human_size


def _root_folder(rel_path: str) -> str:
    """Return the first path component, or '(root)' for top-level files."""
    parts = rel_path.split("/")
    if len(parts) == 1:
        return "(root)"
    return parts[0]


def _shorten_path(rel_path: str, max_display: int = 90) -> str:
    """Shorten a long relative path for display by collapsing middle dirs."""
    if len(rel_path) <= max_display:
        return rel_path
    parts = rel_path.split("/")
    if len(parts) <= 2:
        return rel_path
    # Keep first two dirs + filename, collapse middle
    filename = parts[-1]
    prefix = "/".join(parts[:2])
    shortened = f"{prefix}/.../{filename}"
    if len(shortened) <= max_display:
        return shortened
    # Just first dir + filename
    return f"{parts[0]}/.../{filename}"


def generate_summary(config_path: str, out_dir: str) -> str:
    config = load_config(config_path)

    eligible: list[FileEntry] = []
    skipped: list[FileEntry] = []
    errors: list[FileEntry] = []
    for entry in scan(config):
        if entry.is_skipped:
            if "error" in entry.skip_reason:
                errors.append(entry)
            else:
                skipped.append(entry)
        else:
            eligible.append(entry)

    total_size = sum(f.size for f in eligible)
    skipped_size = sum(f.size for f in skipped)

    # --- Breakdown by root folder ---
    folder_stats: dict[str, dict] = defaultdict(lambda: {"count": 0, "size": 0})
    for f in eligible:
        folder = _root_folder(f.relative_path)
        folder_stats[folder]["count"] += 1
        folder_stats[folder]["size"] += f.size

    folder_rows = sorted(folder_stats.items(), key=lambda x: -x[1]["size"])

    # --- Breakdown by extension ---
    ext_stats: dict[str, dict] = defaultdict(lambda: {"count": 0, "size": 0})
    for f in eligible:
        ext = f.extension or "(no ext)"
        ext_stats[ext]["count"] += 1
        ext_stats[ext]["size"] += f.size

    ext_rows = sorted(ext_stats.items(), key=lambda x: -x[1]["size"])

    # --- Top 25 largest ---
    top25 = sorted(eligible, key=lambda f: -f.size)[:25]

    # --- Skipped breakdown ---
    skip_reasons: dict[str, dict] = defaultdict(lambda: {"count": 0, "size": 0})
    for f in skipped:
        reason = f.skip_reason.split(" ")[0]
        skip_reasons[reason]["count"] += 1
        skip_reasons[reason]["size"] += f.size

    # --- Build markdown ---
    today = date.today().isoformat()
    lines: list[str] = []

    lines.append(f"# Profile Backup Summary ({today})")
    lines.append("")
    lines.append(f"**Date:** {today}  ")
    lines.append(f"**Backup root:** `{config.backup_root}`  ")
    lines.append(
        f"**Total eligible for upload:** {human_size(total_size)} "
        f"across {len(eligible):,} files  "
    )
    lines.append(
        f"**Skipped by rules:** {len(skipped):,} files "
        f"({human_size(skipped_size)})  "
    )
    lines.append(f"**Skipped by errors:** {len(errors)} files")
    lines.append("")
    lines.append("---")
    lines.append("")

    # --- Breakdown by root folder ---
    lines.append("## Breakdown by Root Folder")
    lines.append("")
    lines.append("| Folder | Files | Size | % of Total |")
    lines.append("| ------ | ----: | ---: | ---------: |")

    TOP_FOLDERS = 10
    shown_folders = folder_rows[:TOP_FOLDERS]
    other_folders = folder_rows[TOP_FOLDERS:]

    for folder, data in shown_folders:
        pct = (data["size"] / total_size * 100) if total_size else 0
        pct_str = f"{pct:.1f}%" if pct >= 0.1 else "<0.1%"
        lines.append(
            f"| {folder} | {data['count']:,} | {human_size(data['size'])} | {pct_str} |"
        )

    if other_folders:
        other_count = sum(d["count"] for _, d in other_folders)
        other_size = sum(d["size"] for _, d in other_folders)
        pct = (other_size / total_size * 100) if total_size else 0
        pct_str = f"{pct:.1f}%" if pct >= 0.1 else "<0.1%"
        lines.append(
            f"| All other ({len(other_folders)} folders) | {other_count:,} "
            f"| {human_size(other_size)} | {pct_str} |"
        )

    lines.append("")
    lines.append("---")
    lines.append("")

    # --- Breakdown by file type ---
    lines.append("## Breakdown by File Type")
    lines.append("")
    lines.append("| Extension | Files | Size | % of Total |")
    lines.append("| --------- | ----: | ---: | ---------: |")

    TOP_EXTS = 17
    shown_exts = ext_rows[:TOP_EXTS]
    other_exts = ext_rows[TOP_EXTS:]

    for ext, data in shown_exts:
        pct = (data["size"] / total_size * 100) if total_size else 0
        pct_str = f"{pct:.1f}%" if pct >= 0.1 else "<0.1%"
        lines.append(
            f"| {ext} | {data['count']:,} | {human_size(data['size'])} | {pct_str} |"
        )

    if other_exts:
        other_count = sum(d["count"] for _, d in other_exts)
        other_size = sum(d["size"] for _, d in other_exts)
        pct = (other_size / total_size * 100) if total_size else 0
        pct_str = f"~{pct:.1f}%" if pct >= 0.1 else "<0.1%"
        lines.append(
            f"| All other | ~{other_count:,} | ~{human_size(other_size)} | {pct_str} |"
        )

    lines.append("")
    lines.append("---")
    lines.append("")

    # --- Top 25 largest files ---
    lines.append("## Top 25 Largest Files")
    lines.append("")
    lines.append("| # | Size | File |")
    lines.append("| --: | -------: | ---- |")

    for i, f in enumerate(top25, 1):
        display = _shorten_path(f.relative_path)
        lines.append(f"| {i} | {human_size(f.size)} | {display} |")

    lines.append("")
    lines.append("---")
    lines.append("")

    # --- Skipped files ---
    lines.append(f"## Skipped Files ({len(skipped)} files, {human_size(skipped_size)})")
    lines.append("")

    if skip_reasons:
        lines.append("| Reason | Files | Size |")
        lines.append("| ------ | ----: | ---: |")
        for reason, data in sorted(skip_reasons.items(), key=lambda x: -x[1]["size"]):
            lines.append(
                f"| {reason} | {data['count']:,} | {human_size(data['size'])} |"
            )
        lines.append("")

    # Top 10 skipped by size
    if skipped:
        lines.append("**Top 10 skipped by size:**")
        lines.append("")
        lines.append("| File | Size | Reason |")
        lines.append("| ---- | ---: | ------ |")
        top_skipped = sorted(skipped, key=lambda f: -f.size)[:10]
        for f in top_skipped:
            display = _shorten_path(f.relative_path, 60)
            lines.append(f"| {display} | {human_size(f.size)} | {f.skip_reason} |")
        lines.append("")

    lines.append("---")
    lines.append("")

    # --- Errors ---
    lines.append(f"## Errors ({len(errors)} files)")
    lines.append("")
    if errors:
        for f in errors:
            lines.append(f"- `{f.relative_path}`: {f.skip_reason}")
    else:
        lines.append("No errors.")
    lines.append("")

    # --- Write file ---
    md_content = "\n".join(lines)
    os.makedirs(out_dir, exist_ok=True)
    filename = f"profile-summary-{today}.md"
    out_path = os.path.join(out_dir, filename)
    with open(out_path, "w", encoding="utf-8") as fout:
        fout.write(md_content)

    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a profile backup summary markdown file"
    )
    parser.add_argument(
        "--config", default="config.yaml", help="Path to config.yaml"
    )
    parser.add_argument(
        "--out", default="docs", help="Output directory (default: docs/)"
    )
    args = parser.parse_args()

    out_path = generate_summary(args.config, args.out)
    print(f"Summary written to {out_path}")


if __name__ == "__main__":
    main()
