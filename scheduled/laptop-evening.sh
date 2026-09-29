#!/usr/bin/env bash
# 3535 evening job (drive-backup-evening.service), started only after
# laptop-gate.sh says the laptop will likely stay on: remind about unpushed
# work, then back up. Failures go to the OMEN inbox.

source "$(dirname "$0")/lib.sh"

today=$(date +%F)
read -r attempt_day attempts _ 2>/dev/null <"$STATE_DIR/attempts" || true
[[ ${attempt_day:-} == "$today" ]] || attempts=0
echo "$today $((attempts + 1)) $(date +%s)" >"$STATE_DIR/attempts"

repo_check
run_backup "$REPO/config.linux.yaml" drive-backup-evening.service
