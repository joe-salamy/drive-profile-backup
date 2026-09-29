# Scheduled backups

systemd user timers that run `drive-backup` unattended and report to the
[joe-inbox](https://github.com/joe-salamy/joe-inbox) on OMEN. Each run first
checks every repo in `~/Code` for uncommitted or untracked files, commits on
no remote, and stashes, and sends one "Unpushed work" message if it finds
any. Messages are sent only when there is something to act on.

| Host          | Unit                   | When                                                  | Config              |
|---------------|------------------------|-------------------------------------------------------|---------------------|
| omen          | `drive-backup-nightly` | 03:00 Pacific (±10 min), catches up after downtime    | `config.omen.yaml`  |
| inspiron-3535 | `drive-backup-evening` | after 18:00, once on AC for 30+ min (see below)       | `config.linux.yaml` |

## Install

Once per host, from the repo root:

```sh
scheduled/install.sh
```

It needs `credentials.json` in the repo root and `~/.drive-backup/token.json`
and `~/.drive-backup/secrets.key`, all copied from a machine that has them
(use the same `secrets.key` everywhere). It builds `.venv` if missing, links
the units into `~/.config/systemd/user`, and enables the timer.

## The 3535's evening run

Closing the lid shuts the laptop down, so the run waits until the laptop is
likely to stay on. The timer fires every 5 minutes all day, and
`laptop-gate.sh` (the unit's `ExecCondition`) lets a run start only when:

- it is 18:00 or later,
- the laptop has been on AC continuously for 30+ minutes (a gap of more than
  12 minutes between ticks, meaning it was off, resets the count),
- no backup has succeeded today,
- there were fewer than 2 attempts today, the last one over an hour ago,
- DNS works.

Being interrupted is safe: `drive-backup` treats SIGTERM like Ctrl+C,
checkpoints its manifest, and the next run resumes. If no backup has
succeeded in 3 days, the gate sends one reminder a day.

The unit is capped at 2 CPU threads and runs at idle I/O priority, to keep
the laptop cool.

## Inbox delivery

On OMEN, messages go straight to `~/go/bin/joe-inbox send`. The 3535 sends
them over `ssh omen`, which needs a key without a passphrase or a running
agent. If OMEN can't be reached, the message is written to the 3535's own
`~/Inbox` with a note saying so.

What gets sent:

- **Unpushed work on HOST**: the repo check found something.
- **Backup finished with errors on HOST**: `drive-backup` exited 2 (some files,
  prunes, or the manifest snapshot failed). The message includes the last 60
  lines of output.
- **Backup failed on HOST**: any other non-zero exit.
- **localfin snapshot failed on omen**.
- **No backup from HOST in 3+ days** (3535 only).

## OMEN specifics

`localfin-ai/data/budget.db` exists only on OMEN and is a live WAL database.
`omen-nightly.sh` copies it with SQLite's backup API to
`~/_snapshots/localfin-ai/budget.db`, and `config.omen.yaml` excludes the live
file. `Documents/` and `Downloads/` are excluded because they mirror the
3535's copies, and `models/` because model weights can be downloaded again.

## Operating

```sh
systemctl --user list-timers 'drive-backup-*'
systemctl --user start drive-backup-nightly.service   # OMEN: run now
scheduled/laptop-evening.sh                           # 3535: run now, skipping the gate
journalctl --user -u drive-backup-evening -n 50        # gate decisions and run output
tail -f ~/.local/state/drive-backup-scheduled/last-run.log
```

State lives in `~/.local/state/drive-backup-scheduled/`: `last-success`,
`attempts`, `ac-streak`, `stale-nag`, `installed`, and `last-run.log`.
