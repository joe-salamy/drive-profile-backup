#!/usr/bin/env bash
# Shared helpers for the scheduled backup jobs. Sourced, not executed.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/drive-backup-scheduled"
HOST="$(hostname)"
INBOX_HOST="omen" # the one inbox; other hosts deliver over ssh
JOE_INBOX="go/bin/joe-inbox" # relative to $HOME, locally and over ssh
mkdir -p "$STATE_DIR"

log() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }

# notify FROM TITLE < body.md — deliver a message to the OMEN inbox, falling
# back to this host's ~/Inbox when OMEN can't be reached.
notify() {
  local from=$1 title=${2//[\'\\]/} body
  body=$(cat)
  if [[ $HOST == "$INBOX_HOST" ]]; then
    printf '%s\n' "$body" | "$HOME/$JOE_INBOX" send -from "$from" -title "$title" && return
  elif printf '%s\n' "$body" | ssh -o BatchMode=yes -o ConnectTimeout=10 "$INBOX_HOST" \
    "~/$JOE_INBOX send -from '$from' -title '$title'"; then
    return
  fi
  log "inbox delivery to $INBOX_HOST failed; writing to local ~/Inbox"
  deliver_local "$from" "$title" "$body"
}

# The by-hand delivery from joe-inbox's README: write aside, then mv in.
deliver_local() {
  local from=$1 title=$2 body=$3 dir="${INBOX_DIR:-$HOME/Inbox}" tmp
  mkdir -p "$dir/.tmp"
  tmp=$(mktemp "$dir/.tmp/XXXXXX")
  {
    printf -- '---\nfrom: %s\ntitle: %s\ndate: %s\n---\n\n' "$from" "$title" "$(date -Iseconds)"
    printf '%s\n\n_(%s could not reach the inbox on %s.)_\n' "$body" "$HOST" "$INBOX_HOST"
  } >"$tmp"
  mv "$tmp" "$dir/$(date +%Y-%m-%dT%H%M%S)-$from.md"
}

# repo_report [ROOT] — markdown list of repos with work not on any remote.
# Uses local remote-tracking refs only (no fetch), so it never needs network.
repo_report() {
  local root=${1:-$HOME/Code} dir name changes stashes branch count
  for dir in "$root"/*/; do
    [[ -e $dir/.git ]] || continue
    name=$(basename "$dir")
    local issues=()
    changes=$(git -C "$dir" status --porcelain 2>/dev/null | wc -l)
    ((changes)) && issues+=("$changes uncommitted/untracked files")
    if [[ -z $(git -C "$dir" remote) ]]; then
      issues+=("no remote")
    else
      while read -r branch; do
        count=$(git -C "$dir" rev-list --count "refs/heads/$branch" --not --remotes 2>/dev/null)
        ((count)) && issues+=("\`$branch\` has $count unpushed commit(s)")
      done < <(git -C "$dir" for-each-ref --format='%(refname:short)' refs/heads)
    fi
    stashes=$(git -C "$dir" stash list 2>/dev/null | wc -l)
    ((stashes)) && issues+=("$stashes stashes")
    if ((${#issues[@]})); then
      local joined
      joined=$(printf '%s; ' "${issues[@]}")
      printf -- '- **%s**: %s\n' "$name" "${joined%; }"
    fi
  done
}

# repo_check — send the report to the inbox, only when something is unpushed.
repo_check() {
  local report
  report=$(repo_report)
  if [[ -z $report ]]; then
    log "repo check: everything is pushed"
    return
  fi
  log "repo check: unpushed work found"
  printf '%s\n' "$report"
  {
    printf '# Unpushed work on %s\n\n' "$HOST"
    printf '%s\n\n' "$report"
    printf 'Commit and push, or stash/drop what you do not need. Checked %s.\n' "$(date '+%F %R')"
  } | notify "repo-check" "Unpushed work on $HOST"
}

# run_backup CONFIG UNIT — run drive-backup, record success, report failures.
# Output goes to a file, not a pipe: on shutdown SIGTERM hits every process in
# the unit, and a dead pipe reader would kill drive-backup mid-checkpoint.
run_backup() {
  local config=$1 unit=$2 out="$STATE_DIR/last-run.log" status=0
  trap 'log "SIGTERM received; waiting for drive-backup to checkpoint"' TERM
  log "running drive-backup --config $config (live output: $out)"
  COLUMNS=120 "$REPO/.venv/bin/drive-backup" --config "$config" >"$out" 2>&1 || status=$?
  trap - TERM
  cat "$out"
  case $status in
    0)
      date +%s >"$STATE_DIR/last-success"
      log "backup succeeded"
      ;;
    130 | 143)
      log "backup interrupted (exit $status); the next run resumes"
      return 0
      ;;
    *)
      local title="Backup failed on $HOST"
      ((status == 2)) && title="Backup finished with errors on $HOST"
      {
        printf '# %s\n\n' "$title"
        printf 'drive-backup exited %s at %s.\n\n' "$status" "$(date '+%F %R')"
        printf 'Last lines of the run:\n\n```text\n%s\n```\n\n' "$(tail -n 60 "$out")"
        printf 'Full output: `%s`, or `journalctl --user -u %s` on %s.\n' "$out" "$unit" "$HOST"
      } | notify "drive-backup" "$title"
      ;;
  esac
  return "$status"
}
