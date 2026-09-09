#!/usr/bin/env bash
#
# Start main.py and oi_collector.py in tmux, one dated log file per day.
#
# Runs from cron @reboot, because the instance is started by a Lambda on a
# schedule -- boot IS the start signal, and tying it to boot means the machine
# coming up for any other reason also brings the bot up.
#
# Idempotent. Running it twice does not produce two bots: tmux has-session is
# checked before every launch. That matters because @reboot and a manual run
# during debugging will collide sooner or later.
#
# Logs are per-day, not rotated by size: logs/main_2026-09-09.log. A dated file
# is trivially greppable after the fact and never truncates the morning while
# you are reading the afternoon.

set -uo pipefail

APP_DIR="${APP_DIR:-/home/ec2-user/cursor_FNO}"
LOG_DIR="$APP_DIR/logs"
export TZ="Asia/Kolkata"

# Interpreter discovery. VENV can be set explicitly; otherwise the usual places
# are tried in order. Hardcoding one path meant the script died with "no
# interpreter" and no clue which paths it had considered -- the failure told you
# it was broken but not how to fix it, which is the least useful kind.
find_python() {
  local candidates=()
  [ -n "${VENV:-}" ] && candidates+=("$VENV")
  candidates+=("$APP_DIR/venv" "$APP_DIR/.venv" "$HOME/venv" "$HOME/.venv")

  local base exe
  for base in "${candidates[@]}"; do
    for exe in "$base/bin/python3" "$base/bin/python"; do
      if [ -x "$exe" ]; then
        echo "$exe"
        return 0
      fi
    done
  done

  # Last resort: a system interpreter. Works only if dependencies were installed
  # globally, so it is reported loudly rather than used silently.
  command -v python3 2>/dev/null && return 0
  return 1
}

DAY="$(date +%F)"
BOOT_LOG="$LOG_DIR/boot_${DAY}.log"

mkdir -p "$LOG_DIR"

log() { echo "[$(date '+%F %T %Z')] $*" | tee -a "$BOOT_LOG"; }

# The instance can be up before its clock has synced or the network is usable.
# Starting the bot against a wrong clock is worse than starting it late: every
# session boundary, every bar bucket and every IST stamp would be wrong.
wait_for_network() {
  local tries=0
  until curl -sf -m 5 -o /dev/null https://margincalculator.angelbroking.com/ 2>/dev/null; do
    tries=$((tries + 1))
    if [ "$tries" -ge 30 ]; then
      log "WARN: network still unreachable after $tries tries; starting anyway"
      return 0
    fi
    sleep 10
  done
  log "network reachable after $tries retries"
}

# $1 = tmux session name, $2 = python entrypoint, $3 = log file
launch() {
  local session="$1" script="$2" logfile="$3"

  if tmux has-session -t "$session" 2>/dev/null; then
    log "SKIP $session: already running"
    return 0
  fi
  if [ ! -f "$APP_DIR/$script" ]; then
    log "ERROR $session: $APP_DIR/$script not found"
    return 1
  fi

  # `exec` so the python process IS the pane process -- otherwise the pane
  # survives a crash as an idle shell and tmux has-session reports healthy
  # while nothing is running. That failure mode is silent, which is the worst
  # kind for something you only check once a day.
  tmux new-session -d -s "$session" -c "$APP_DIR" \
    "exec '$PYTHON' -u $script >> '$logfile' 2>&1"

  sleep 2
  if tmux has-session -t "$session" 2>/dev/null; then
    log "STARTED $session -> $logfile"
  else
    log "ERROR $session died within 2s; last lines:"
    tail -n 20 "$logfile" | tee -a "$BOOT_LOG"
    return 1
  fi
}

log "=== start_bot.sh: $DAY, uptime $(cut -d' ' -f1 /proc/uptime)s ==="

PYTHON="$(find_python || true)"
if [ -z "$PYTHON" ]; then
  log "FATAL: no python interpreter found."
  log "  Looked for bin/python3 and bin/python under:"
  log "    VENV=${VENV:-<unset>}"
  log "    $APP_DIR/venv, $APP_DIR/.venv, $HOME/venv, $HOME/.venv"
  log "  and found no system python3 on PATH."
  log "  Fix: VENV=/path/to/your/venv $0"
  exit 1
fi
case "$PYTHON" in
  "$APP_DIR"/*|"$HOME"/venv/*|"$HOME"/.venv/*)
    log "interpreter: $PYTHON" ;;
  *)
    log "WARN using SYSTEM interpreter $PYTHON -- no virtualenv found."
    log "     Works only if dependencies are installed globally." ;;
esac

wait_for_network

rc=0
launch fno-main "main.py"         "$LOG_DIR/main_${DAY}.log"      || rc=1
launch fno-oi   "oi_collector.py" "$LOG_DIR/oi_${DAY}.log"        || rc=1

log "sessions now: $(tmux ls 2>/dev/null | tr '\n' ' ' || echo none)"
log "=== start_bot.sh done (rc=$rc) ==="
exit "$rc"
