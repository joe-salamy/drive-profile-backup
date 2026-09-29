#!/usr/bin/env bash
# OMEN nightly job (drive-backup-nightly.service): snapshot live databases,
# remind about unpushed work, then back up.

source "$(dirname "$0")/lib.sh"

SNAPSHOTS="$HOME/_snapshots"

# snapshot_sqlite SRC DEST — consistent copy of a live (WAL) SQLite database
# via the backup API; a plain file copy can catch it mid-write.
snapshot_sqlite() {
  python3 - "$1" "$2" <<'PY'
import os
import sqlite3
import sys

src, dest = sys.argv[1:]
os.makedirs(os.path.dirname(dest), exist_ok=True)
tmp = dest + ".tmp"
source, target = sqlite3.connect(src), sqlite3.connect(tmp)
try:
    source.backup(target)
finally:
    target.close()
    source.close()
os.replace(tmp, dest)
PY
}

if ! err=$(snapshot_sqlite "$HOME/Code/localfin-ai/data/budget.db" \
  "$SNAPSHOTS/localfin-ai/budget.db" 2>&1); then
  log "localfin snapshot failed: $err"
  printf '# localfin snapshot failed on %s\n\n```text\n%s\n```\n\nThe backup still ran, but it holds the previous snapshot of budget.db.\n' \
    "$HOST" "$err" | notify "drive-backup" "localfin snapshot failed on $HOST"
fi

repo_check
run_backup "$REPO/config.omen.yaml" drive-backup-nightly.service
