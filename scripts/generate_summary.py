"""Generate a profile-summary-[date].md from a live dry-run scan.

Usage:
    python scripts/generate_summary.py [--config config.yaml] [--out docs/]
    python scripts/generate_summary.py --full-profile [--include-appdata]
"""

from __future__ import annotations

import argparse
import heapq
import os
import shutil
import sys
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import TextIO, cast

from drive_backup.config import Config, load_config
from drive_backup.scanner import scan
from drive_backup.utils import human_size

_WIN32 = sys.platform == "win32"
_MAX_PATH = 260
CountSize = dict[str, int]


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
class SummaryStats:
    """Bounded file samples and aggregate profile scan statistics."""

    eligible_count: int = 0
    eligible_size: int = 0
    skipped_count: int = 0
    skipped_size: int = 0
    error_count: int = 0
    folder_stats: dict[str, CountSize] = field(default_factory=dict)
    extension_stats: dict[str, CountSize] = field(default_factory=dict)
    skip_reasons: dict[str, CountSize] = field(default_factory=dict)
    largest_eligible: list[tuple[int, str, str]] = field(default_factory=list)
    largest_skipped: list[tuple[int, str, str]] = field(default_factory=list)


@dataclass
class ProfileStats:
    """Accumulated stats from an unrestricted profile scan."""

    total_files: int = 0
    total_size: int = 0
    total_errors: int = 0
    folder_stats: dict[str, CountSize] = field(
        default_factory=lambda: defaultdict(lambda: {"count": 0, "size": 0})
    )
    ext_stats: dict[str, CountSize] = field(
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

    folder_rows = sorted(stats.folder_stats.items(), key=lambda x: -x[1]["size"])
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
                f"| Coverage | 100% | {backup_pct:.1f}% | " f"{100 - backup_pct:.1f}% |"
            )
        lines.append("")
        lines.append("---")
        lines.append("")

    # --- Errors ---
    if stats.total_errors > 0:
        lines.append(f"## Errors ({stats.total_errors:,} files could not be read)")
        lines.append("")
        lines.append("These files were inaccessible due to permission or OS errors.")
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


def _add_aggregate(aggregates: dict[str, CountSize], key: str, size: int) -> None:
    aggregate = aggregates.setdefault(key, {"count": 0, "size": 0})
    aggregate["count"] += 1
    aggregate["size"] += size


def _push_largest(
    heap: list[tuple[int, str, str]],
    item: tuple[int, str, str],
    limit: int,
) -> None:
    if len(heap) < limit:
        heapq.heappush(heap, item)
    elif item > heap[0]:
        heapq.heapreplace(heap, item)


def _collect_summary(config: Config, error_output: TextIO) -> SummaryStats:
    stats = SummaryStats()
    for entry in scan(config):
        if entry.is_skipped:
            if "error" in entry.skip_reason:
                stats.error_count += 1
                error_output.write(f"- `{entry.relative_path}`: {entry.skip_reason}\n")
                continue
            stats.skipped_count += 1
            stats.skipped_size += entry.size
            reason = entry.skip_reason.split(" ")[0]
            _add_aggregate(stats.skip_reasons, reason, entry.size)
            _push_largest(
                stats.largest_skipped,
                (entry.size, entry.relative_path, entry.skip_reason),
                10,
            )
            continue

        stats.eligible_count += 1
        stats.eligible_size += entry.size
        _add_aggregate(
            stats.folder_stats,
            _root_folder(entry.relative_path),
            entry.size,
        )
        _add_aggregate(
            stats.extension_stats,
            entry.extension or "(no ext)",
            entry.size,
        )
        _push_largest(
            stats.largest_eligible,
            (entry.size, entry.relative_path, ""),
            25,
        )
    return stats


def _write_line(output: TextIO, line: str = "") -> None:
    output.write(f"{line}\n")


def _write_summary(
    output: TextIO,
    config: Config,
    stats: SummaryStats,
    error_input: TextIO,
    report_date: date,
) -> None:
    today = report_date.isoformat()
    folder_rows = sorted(stats.folder_stats.items(), key=lambda item: -item[1]["size"])
    extension_rows = sorted(
        stats.extension_stats.items(), key=lambda item: -item[1]["size"]
    )
    top_eligible = sorted(
        stats.largest_eligible,
        key=lambda item: (-item[0], item[1], item[2]),
    )
    top_skipped = sorted(
        stats.largest_skipped,
        key=lambda item: (-item[0], item[1], item[2]),
    )

    _write_line(output, f"# Profile Backup Summary ({today})")
    _write_line(output)
    _write_line(output, f"**Date:** {today}  ")
    _write_line(output, f"**Backup root:** `{config.backup_root}`  ")
    _write_line(
        output,
        f"**Total eligible for upload:** {human_size(stats.eligible_size)} "
        f"across {stats.eligible_count:,} files  ",
    )
    _write_line(
        output,
        f"**Skipped by rules:** {stats.skipped_count:,} files "
        f"({human_size(stats.skipped_size)})  ",
    )
    _write_line(output, f"**Skipped by errors:** {stats.error_count} files")
    _write_line(output)
    _write_line(output, "---")
    _write_line(output)

    _write_line(output, "## Breakdown by Root Folder")
    _write_line(output)
    _write_line(output, "| Folder | Files | Size | % of Total |")
    _write_line(output, "| ------ | ----: | ---: | ---------: |")
    shown_folders = folder_rows[:10]
    other_folders = folder_rows[10:]
    for folder, aggregate in shown_folders:
        percentage = (
            aggregate["size"] / stats.eligible_size * 100 if stats.eligible_size else 0
        )
        percentage_text = f"{percentage:.1f}%" if percentage >= 0.1 else "<0.1%"
        _write_line(
            output,
            f"| {folder} | {aggregate['count']:,} | "
            f"{human_size(aggregate['size'])} | {percentage_text} |",
        )
    if other_folders:
        other_count = sum(item["count"] for _, item in other_folders)
        other_size = sum(item["size"] for _, item in other_folders)
        percentage = (
            other_size / stats.eligible_size * 100 if stats.eligible_size else 0
        )
        percentage_text = f"{percentage:.1f}%" if percentage >= 0.1 else "<0.1%"
        _write_line(
            output,
            f"| All other ({len(other_folders)} folders) | {other_count:,} "
            f"| {human_size(other_size)} | {percentage_text} |",
        )
    _write_line(output)
    _write_line(output, "---")
    _write_line(output)

    _write_line(output, "## Breakdown by File Type")
    _write_line(output)
    _write_line(output, "| Extension | Files | Size | % of Total |")
    _write_line(output, "| --------- | ----: | ---: | ---------: |")
    shown_extensions = extension_rows[:17]
    other_extensions = extension_rows[17:]
    for extension, aggregate in shown_extensions:
        percentage = (
            aggregate["size"] / stats.eligible_size * 100 if stats.eligible_size else 0
        )
        percentage_text = f"{percentage:.1f}%" if percentage >= 0.1 else "<0.1%"
        _write_line(
            output,
            f"| {extension} | {aggregate['count']:,} | "
            f"{human_size(aggregate['size'])} | {percentage_text} |",
        )
    if other_extensions:
        other_count = sum(item["count"] for _, item in other_extensions)
        other_size = sum(item["size"] for _, item in other_extensions)
        percentage = (
            other_size / stats.eligible_size * 100 if stats.eligible_size else 0
        )
        percentage_text = f"~{percentage:.1f}%" if percentage >= 0.1 else "<0.1%"
        _write_line(
            output,
            f"| All other | ~{other_count:,} | ~{human_size(other_size)} "
            f"| {percentage_text} |",
        )
    _write_line(output)
    _write_line(output, "---")
    _write_line(output)

    _write_line(output, "## Top 25 Largest Files")
    _write_line(output)
    _write_line(output, "| # | Size | File |")
    _write_line(output, "| --: | -------: | ---- |")
    for index, (size, relative_path, _) in enumerate(top_eligible, 1):
        _write_line(
            output,
            f"| {index} | {human_size(size)} | {_shorten_path(relative_path)} |",
        )
    _write_line(output)
    _write_line(output, "---")
    _write_line(output)

    _write_line(
        output,
        f"## Skipped Files ({stats.skipped_count} files, "
        f"{human_size(stats.skipped_size)})",
    )
    _write_line(output)
    if stats.skip_reasons:
        _write_line(output, "| Reason | Files | Size |")
        _write_line(output, "| ------ | ----: | ---: |")
        for reason, aggregate in sorted(
            stats.skip_reasons.items(),
            key=lambda item: -item[1]["size"],
        ):
            _write_line(
                output,
                f"| {reason} | {aggregate['count']:,} | "
                f"{human_size(aggregate['size'])} |",
            )
        _write_line(output)
    if top_skipped:
        _write_line(output, "**Top 10 skipped by size:**")
        _write_line(output)
        _write_line(output, "| File | Size | Reason |")
        _write_line(output, "| ---- | ---: | ------ |")
        for size, relative_path, reason in top_skipped:
            _write_line(
                output,
                f"| {_shorten_path(relative_path, 60)} | "
                f"{human_size(size)} | {reason} |",
            )
        _write_line(output)
    _write_line(output, "---")
    _write_line(output)

    _write_line(output, f"## Errors ({stats.error_count} files)")
    _write_line(output)
    if stats.error_count:
        error_input.seek(0)
        shutil.copyfileobj(error_input, output, length=65_536)
    else:
        _write_line(output, "No errors.")


def generate_summary(config_path: str, out_dir: str) -> SummaryResult:
    config = load_config(config_path)
    today = date.today()
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"profile-summary-{today.isoformat()}.md")

    with tempfile.SpooledTemporaryFile(
        mode="w+",
        max_size=1_048_576,
        encoding="utf-8",
    ) as errors:
        error_stream = cast(TextIO, errors)
        stats = _collect_summary(config, error_stream)
        with open(out_path, "w", encoding="utf-8") as output:
            _write_summary(output, config, stats, error_stream, today)

    return SummaryResult(
        out_path=out_path,
        eligible_count=stats.eligible_count,
        eligible_size=stats.eligible_size,
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
