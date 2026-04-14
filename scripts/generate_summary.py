"""Generate a profile-summary-[date].md from a live dry-run scan.

Usage:
    python scripts/generate_summary.py [--config config.yaml] [--out docs/]
    python scripts/generate_summary.py --full-profile [--include-appdata]
"""

from __future__ import annotations

import argparse
import heapq
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from drive_backup.config import load_config
from drive_backup.scanner import FileEntry, scan
from drive_backup.utils import human_size

_WIN32 = sys.platform == "win32"
_MAX_PATH = 260


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


@dataclass
class SummaryResult:
    """Return value from generate_summary with totals for comparison."""

    out_path: str
    eligible_count: int
    eligible_size: int


@dataclass
class ProfileStats:
    """Accumulated stats from an unrestricted profile scan."""

    total_files: int = 0
    total_size: int = 0
    total_errors: int = 0
    folder_stats: dict[str, dict] = field(
        default_factory=lambda: defaultdict(lambda: {"count": 0, "size": 0})
    )
    ext_stats: dict[str, dict] = field(
        default_factory=lambda: defaultdict(lambda: {"count": 0, "size": 0})
    )
    top_files: list[tuple[int, str]] = field(default_factory=list)  # min-heap
    elapsed: float = 0.0


def _safe_stat(path: str) -> os.stat_result | None:
    """stat() with long-path support on Windows."""
    try:
        if _WIN32 and len(path) >= _MAX_PATH and not path.startswith("\\\\?\\"):
            path = "\\\\?\\" + os.path.abspath(path)
        return os.stat(path)
    except (OSError, PermissionError):
        return None


def _unrestricted_scan(
    root: str,
    skip_dirs: set[str] | None = None,
    top_n: int = 25,
) -> ProfileStats:
    """Walk the entire profile without backup exclusions.

    Args:
        root: Profile root directory.
        skip_dirs: Directory names to skip (e.g. {"AppData"}).
        top_n: Number of largest files to track.
    """
    stats = ProfileStats()
    start = time.perf_counter()
    file_count = 0

    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        # Prune only explicitly skipped dirs
        if skip_dirs:
            dirnames[:] = [d for d in dirnames if d not in skip_dirs]

        # Skip symlink directories to avoid infinite loops
        dirnames[:] = [
            d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))
        ]

        for fname in filenames:
            full_path = os.path.join(dirpath, fname)

            # Skip symlink files
            if os.path.islink(full_path):
                continue

            st = _safe_stat(full_path)
            if st is None:
                stats.total_errors += 1
                continue

            size = st.st_size
            rel_path = os.path.relpath(full_path, root).replace("\\", "/")

            stats.total_files += 1
            stats.total_size += size

            # Top-level folder
            folder = _root_folder(rel_path)
            stats.folder_stats[folder]["count"] += 1
            stats.folder_stats[folder]["size"] += size

            # Extension
            ext = os.path.splitext(fname)[1].lower() or "(no ext)"
            stats.ext_stats[ext]["count"] += 1
            stats.ext_stats[ext]["size"] += size

            # Top N largest files (min-heap)
            if len(stats.top_files) < top_n:
                heapq.heappush(stats.top_files, (size, rel_path))
            elif size > stats.top_files[0][0]:
                heapq.heapreplace(stats.top_files, (size, rel_path))

            # Progress feedback
            file_count += 1
            if file_count % 50_000 == 0:
                elapsed = time.perf_counter() - start
                print(
                    f"  Scanning... {file_count:,} files [{elapsed:.0f}s]",
                    file=sys.stderr,
                )

    stats.elapsed = time.perf_counter() - start
    # Sort top files descending
    stats.top_files = sorted(stats.top_files, key=lambda x: -x[0])
    return stats


def _render_full_profile_report(
    stats: ProfileStats,
    root: str,
    appdata_included: bool,
    backup_file_count: int = 0,
    backup_total_size: int = 0,
) -> str:
    """Render an unrestricted profile scan as markdown."""
    today = date.today().isoformat()
    mins, secs = divmod(int(stats.elapsed), 60)
    lines: list[str] = []

    lines.append(f"# Full Profile Scan ({today})")
    lines.append("")
    lines.append(f"**Profile root:** `{root}`  ")
    lines.append(
        f"**Total:** {human_size(stats.total_size)} "
        f"across {stats.total_files:,} files  "
    )
    lines.append(f"**Scan time:** {mins}m {secs:02d}s  ")
    lines.append(f"**AppData included:** {'Yes' if appdata_included else 'No'}  ")
    lines.append(f"**Errors (permission/OS):** {stats.total_errors:,}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # --- Breakdown by top-level folder (show all) ---
    lines.append("## Breakdown by Top-Level Folder")
    lines.append("")
    lines.append("| Folder | Files | Size | % of Total |")
    lines.append("| ------ | ----: | ---: | ---------: |")

    folder_rows = sorted(
        stats.folder_stats.items(), key=lambda x: -x[1]["size"]
    )
    for folder, data in folder_rows:
        pct = (data["size"] / stats.total_size * 100) if stats.total_size else 0
        pct_str = f"{pct:.1f}%" if pct >= 0.1 else "<0.1%"
        lines.append(
            f"| {folder} | {data['count']:,} | "
            f"{human_size(data['size'])} | {pct_str} |"
        )

    lines.append("")
    lines.append("---")
    lines.append("")

    # --- Breakdown by file type (top 25 + other) ---
    lines.append("## Breakdown by File Type")
    lines.append("")
    lines.append("| Extension | Files | Size | % of Total |")
    lines.append("| --------- | ----: | ---: | ---------: |")

    ext_rows = sorted(stats.ext_stats.items(), key=lambda x: -x[1]["size"])
    TOP_EXTS = 25
    shown_exts = ext_rows[:TOP_EXTS]
    other_exts = ext_rows[TOP_EXTS:]

    for ext, data in shown_exts:
        pct = (data["size"] / stats.total_size * 100) if stats.total_size else 0
        pct_str = f"{pct:.1f}%" if pct >= 0.1 else "<0.1%"
        lines.append(
            f"| {ext} | {data['count']:,} | "
            f"{human_size(data['size'])} | {pct_str} |"
        )

    if other_exts:
        other_count = sum(d["count"] for _, d in other_exts)
        other_size = sum(d["size"] for _, d in other_exts)
        pct = (other_size / stats.total_size * 100) if stats.total_size else 0
        pct_str = f"~{pct:.1f}%" if pct >= 0.1 else "<0.1%"
        lines.append(
            f"| All other ({len(other_exts)} types) | ~{other_count:,} | "
            f"~{human_size(other_size)} | {pct_str} |"
        )

    lines.append("")
    lines.append("---")
    lines.append("")

    # --- Top 25 largest files ---
    lines.append("## Top 25 Largest Files")
    lines.append("")
    lines.append("| # | Size | File |")
    lines.append("| --: | -------: | ---- |")

    for i, (size, rel_path) in enumerate(stats.top_files, 1):
        display = _shorten_path(rel_path)
        lines.append(f"| {i} | {human_size(size)} | {display} |")

    lines.append("")
    lines.append("---")
    lines.append("")

    # --- Comparison with backup ---
    if backup_file_count > 0 or backup_total_size > 0:
        excluded_files = stats.total_files - backup_file_count
        excluded_size = stats.total_size - backup_total_size
        lines.append("## Comparison with Backup")
        lines.append("")
        lines.append("| Metric | Full Profile | Backup Eligible | Excluded |")
        lines.append("| ------ | -----------: | --------------: | -------: |")
        lines.append(
            f"| Files | {stats.total_files:,} | "
            f"{backup_file_count:,} | {excluded_files:,} |"
        )
        lines.append(
            f"| Size | {human_size(stats.total_size)} | "
            f"{human_size(backup_total_size)} | "
            f"{human_size(excluded_size)} |"
        )
        if stats.total_size > 0:
            backup_pct = backup_total_size / stats.total_size * 100
            lines.append(
                f"| Coverage | 100% | {backup_pct:.1f}% | "
                f"{100 - backup_pct:.1f}% |"
            )
        lines.append("")
        lines.append("---")
        lines.append("")

    # --- Errors ---
    if stats.total_errors > 0:
        lines.append(
            f"## Errors ({stats.total_errors:,} files could not be read)"
        )
        lines.append("")
        lines.append(
            "These files were inaccessible due to permission or OS errors."
        )
        lines.append("")

    return "\n".join(lines)


def generate_full_profile_reports(
    backup_root: str,
    out_dir: str,
    include_appdata: bool,
    backup_file_count: int,
    backup_total_size: int,
) -> list[str]:
    """Generate unrestricted full-profile scan reports.

    Returns list of output file paths created.
    """
    today = date.today().isoformat()
    os.makedirs(out_dir, exist_ok=True)
    outputs: list[str] = []

    # --- No-AppData scan (fast) ---
    print("Scanning full profile (excluding AppData)...", file=sys.stderr)
    stats_no_appdata = _unrestricted_scan(backup_root, skip_dirs={"AppData"})
    md = _render_full_profile_report(
        stats_no_appdata,
        backup_root,
        appdata_included=False,
        backup_file_count=backup_file_count,
        backup_total_size=backup_total_size,
    )
    fname = f"profile-full-no-appdata-{today}.md"
    path = os.path.join(out_dir, fname)
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"Full profile (no AppData) written to {path}")
    outputs.append(path)

    # --- Full scan with AppData (slow, opt-in) ---
    if include_appdata:
        print(
            "Scanning full profile (including AppData, this may take a while)...",
            file=sys.stderr,
        )
        stats_full = _unrestricted_scan(backup_root, skip_dirs=None)
        md = _render_full_profile_report(
            stats_full,
            backup_root,
            appdata_included=True,
            backup_file_count=backup_file_count,
            backup_total_size=backup_total_size,
        )
        fname = f"profile-full-{today}.md"
        path = os.path.join(out_dir, fname)
        with open(path, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"Full profile (with AppData) written to {path}")
        outputs.append(path)

    return outputs


def generate_summary(config_path: str, out_dir: str) -> SummaryResult:
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

    return SummaryResult(
        out_path=out_path,
        eligible_count=len(eligible),
        eligible_size=total_size,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a profile backup summary markdown file"
    )
    parser.add_argument(
        "--out", default="docs", help="Output directory (default: docs/)"
    )
    parser.add_argument(
        "--full-profile",
        action="store_true",
        help="Also generate unrestricted full-profile scan reports",
    )
    parser.add_argument(
        "--include-appdata",
        action="store_true",
        help="Include AppData in the full-profile scan (slow, opt-in)",
    )
    args = parser.parse_args()

    result = generate_summary("config.yaml", args.out)
    print(f"Summary written to {result.out_path}")

    if args.full_profile:
        config = load_config("config.yaml")
        generate_full_profile_reports(
            backup_root=config.backup_root,
            out_dir=args.out,
            include_appdata=args.include_appdata,
            backup_file_count=result.eligible_count,
            backup_total_size=result.eligible_size,
        )


if __name__ == "__main__":
    main()
