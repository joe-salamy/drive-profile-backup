#!/usr/bin/env bash
# Install this host's backup timer: builds .venv if missing, links the units
# into ~/.config/systemd/user, and enables the timer. Safe to re-run.

set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/drive-backup-scheduled"

case "$(hostname)" in
  omen) unit=drive-backup-nightly ;;
  inspiron-3535) unit=drive-backup-evening ;;
  *) echo "no scheduled backup defined for host $(hostname)" >&2; exit 1 ;;
esac

for f in credentials.json; do
  [[ -f $REPO/$f ]] || { echo "missing $REPO/$f (copy it from another machine)" >&2; exit 1; }
done
for f in token.json secrets.key; do
  [[ -f $HOME/.drive-backup/$f ]] || { echo "missing ~/.drive-backup/$f (copy it from another machine)" >&2; exit 1; }
done

if [[ ! -x $REPO/.venv/bin/drive-backup ]]; then
  if command -v uv >/dev/null; then
    uv venv --quiet "$REPO/.venv"
    uv pip install --quiet --python "$REPO/.venv/bin/python" -e "$REPO"
  else
    python3 -m venv "$REPO/.venv"
    "$REPO/.venv/bin/pip" install --quiet -e "$REPO"
  fi
fi

mkdir -p "$STATE_DIR"
[[ -f $STATE_DIR/installed ]] || date +%s >"$STATE_DIR/installed"

systemctl --user link "$REPO/scheduled/systemd/$unit.service" "$REPO/scheduled/systemd/$unit.timer"
systemctl --user daemon-reload
systemctl --user enable --now "$unit.timer"
systemctl --user list-timers "$unit.timer" --no-pager
