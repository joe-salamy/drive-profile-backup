#!/usr/bin/env bash
# ExecCondition for the 3535 evening backup: exit 0 runs it, exit 1 skips.
# The timer fires every 5 minutes all day so this can track how long the
# laptop has been continuously on AC (closing the lid shuts it down, which
# breaks the streak). A run needs: 18:00 or later, 30+ minutes on AC, no
# success yet today, fewer than 2 attempts today and an hour since the last,
# and working DNS. Also nags once a day when no backup succeeded in 3 days.

source "$(dirname "$0")/lib.sh"

MIN_STREAK=$((30 * 60))
MAX_GAP=$((12 * 60)) # a missed tick or two is fine; longer means off/asleep
START_HOUR=18
STALE_AFTER=$((3 * 24 * 3600))

now=$(date +%s)
today=$(date +%F)
skip() {
  log "skip: $*"
  exit 1
}

on_ac() {
  local supply
  for supply in /sys/class/power_supply/*; do
    [[ $(cat "$supply/type" 2>/dev/null) == Mains ]] || continue
    [[ $(cat "$supply/online" 2>/dev/null) == 1 ]] && return 0
  done
  return 1
}

# Stale-backup nag, checked before any skip so it fires even if the
# heuristic never lets a run through.
last_success=$(cat "$STATE_DIR/last-success" 2>/dev/null || cat "$STATE_DIR/installed" 2>/dev/null || echo "$now")
if ((now - last_success > STALE_AFTER)) && [[ $(cat "$STATE_DIR/stale-nag" 2>/dev/null) != "$today" ]]; then
  echo "$today" >"$STATE_DIR/stale-nag"
  printf '# No backup from %s in %s days\n\nThe evening backup needs the laptop on AC for 30+ minutes after 18:00. Run it by hand (no conditions) with:\n\n```sh\n~/Code/drive-profile-backup/scheduled/laptop-evening.sh\n```\n\nLast success: %s.\n' \
    "$HOST" "$(((now - last_success) / 86400))" "$(date -d "@$last_success" '+%F %R')" |
    notify "drive-backup" "No backup from $HOST in 3+ days"
fi

streak_file="$STATE_DIR/ac-streak"
if ! on_ac; then
  rm -f "$streak_file"
  skip "on battery"
fi
read -r streak_start last_seen 2>/dev/null <"$streak_file" || true
if [[ -z ${last_seen:-} ]] || ((now - last_seen > MAX_GAP)); then
  streak_start=$now
fi
echo "$streak_start $now" >"$streak_file"

(($(date +%-H) >= START_HOUR)) || skip "before ${START_HOUR}:00"
((now - streak_start >= MIN_STREAK)) || skip "on AC for $(((now - streak_start) / 60)) min, need $((MIN_STREAK / 60))"
if [[ -f $STATE_DIR/last-success ]] && [[ $(date -d "@$(cat "$STATE_DIR/last-success")" +%F) == "$today" ]]; then
  skip "already backed up today"
fi
read -r attempt_day attempts last_attempt 2>/dev/null <"$STATE_DIR/attempts" || true
if [[ ${attempt_day:-} == "$today" ]]; then
  ((attempts >= 2)) && skip "already attempted twice today"
  ((now - last_attempt < 3600)) && skip "last attempt under an hour ago"
fi
getent hosts www.googleapis.com >/dev/null || skip "no network"
log "conditions met"
