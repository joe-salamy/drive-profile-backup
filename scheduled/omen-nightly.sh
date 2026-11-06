#!/usr/bin/env bash
# OMEN nightly job (drive-backup-nightly.service): snapshot live databases,
# remind about unpushed work, then back up.

source "$(dirname "$0")/lib.sh"
stale_check 'Check `journalctl --user -u drive-backup-nightly` on omen, or run the backup by hand with:

```sh
~/Code/drive-profile-backup/scheduled/omen-nightly.sh
```'

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

# snapshot NAME SRC DEST — snapshot_sqlite, reporting a failure to the inbox.
snapshot() {
  local name=$1 err
  if ! err=$(snapshot_sqlite "$2" "$3" 2>&1); then
    log "$name snapshot failed: $err"
    printf '# %s snapshot failed on %s\n\n```text\n%s\n```\n\nThe backup still ran, but it holds the previous snapshot of %s.\n' \
      "$name" "$HOST" "$err" "$(basename "$2")" | notify "drive-backup" "$name snapshot failed on $HOST"
  fi
}

snapshot localfin "$HOME/Code/localfin-ai/data/budget.db" \
  "$SNAPSHOTS/localfin-ai/budget.db"
snapshot autotrader "$HOME/Code/autotrader/data/executor.db" \
  "$SNAPSHOTS/autotrader/executor.db"

repo_check
run_backup "$REPO/config.omen.yaml" drive-backup-nightly.service
