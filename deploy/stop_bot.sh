#!/usr/bin/env bash
#
# Stop the bot cleanly, ten minutes before the Lambda stops the instance.
#
# Runs from cron at 15:35 IST. The Lambda stops the machine at 15:45. The gap
# exists so SQLite finishes its WAL checkpoint and the option-chain writer
# closes its last transaction on disk rather than mid-flight.
#
# SIGTERM first, then wait, then SIGKILL. A SIGKILL'd SQLite writer leaves a
# hot journal that the next start has to recover -- survivable, but it means
# the last few minutes of a session are the part most likely to be missing,
# which is exactly the part nobody notices is gone.

set -uo pipefail

APP_DIR="${APP_DIR:-/home/ec2-user/cursor_FNO}"
LOG_DIR="$APP_DIR/logs"
GRACE_SEC="${GRACE_SEC:-45}"
export TZ="Asia/Kolkata"

DAY="$(date +%F)"
BOOT_LOG="$LOG_DIR/boot_${DAY}.log"
mkdir -p "$LOG_DIR"

log() { echo "[$(date '+%F %T %Z')] $*" | tee -a "$BOOT_LOG"; }

# $1 = tmux session name
shutdown_session() {
  local session="$1"

  if ! tmux has-session -t "$session" 2>/dev/null; then
    log "SKIP $session: not running"
    return 0
  fi

  # Signal the python process, not the tmux server. Killing the session
  # outright is a SIGHUP to the pane and gives the process no chance to close
  # its database handle.
  local pane_pid
  pane_pid="$(tmux list-panes -t "$session" -F '#{pane_pid}' 2>/dev/null | head -1)"
  if [ -z "$pane_pid" ]; then
    log "WARN $session: no pane pid; killing session"
    tmux kill-session -t "$session" 2>/dev/null
    return 0
  fi

  log "TERM $session (pid $pane_pid), waiting up to ${GRACE_SEC}s"
  kill -TERM "$pane_pid" 2>/dev/null

  local waited=0
  while kill -0 "$pane_pid" 2>/dev/null && [ "$waited" -lt "$GRACE_SEC" ]; do
    sleep 1
    waited=$((waited + 1))
  done

  if kill -0 "$pane_pid" 2>/dev/null; then
    log "WARN $session did not exit in ${GRACE_SEC}s; SIGKILL"
    kill -KILL "$pane_pid" 2>/dev/null
    sleep 1
  else
    log "STOPPED $session cleanly after ${waited}s"
  fi

  tmux kill-session -t "$session" 2>/dev/null
}

log "=== stop_bot.sh: $DAY ==="

shutdown_session fno-main
shutdown_session fno-oi

# Force a WAL checkpoint so the .db files are complete on disk. Without this the
# -wal file carries recent writes, and an instance stop is not a clean unmount.
for db in trade_history.db oi_history.db; do
  if [ -f "$APP_DIR/$db" ]; then
    if sqlite3 "$APP_DIR/$db" "PRAGMA wal_checkpoint(TRUNCATE);" >/dev/null 2>&1; then
      log "checkpointed $db"
    else
      log "WARN could not checkpoint $db"
    fi
  fi
done

log "sessions now: $(tmux ls 2>/dev/null | tr '\n' ' ' || echo none)"
log "=== stop_bot.sh done ==="
