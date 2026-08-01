#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_ROOT="${ASTRBOT_ROOT:-$ROOT_DIR/runtime/codex-prod}"
LOG_DIR="$RUNTIME_ROOT/logs"
LOG_FILE="$LOG_DIR/astrbot.log"
UNIT_NAME="${ASTRBOT_PROD_UNIT_NAME:-astrbot-prod}"

mkdir -p "$LOG_DIR"

is_running() {
  systemctl --user --quiet is-active "$UNIT_NAME"
}

listener_pids() {
  local port="$1"
  if ! command -v lsof >/dev/null 2>&1; then
    return 0
  fi

  lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | awk '!seen[$0]++'
}

process_args() {
  local pid="$1"
  ps -p "$pid" -o args= 2>/dev/null || true
}

terminate_pid() {
  local pid="$1"
  if ! kill -0 "$pid" 2>/dev/null; then
    return 0
  fi

  kill "$pid" 2>/dev/null || true
  for _ in $(seq 1 20); do
    if ! kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
    sleep 0.5
  done

  kill -9 "$pid" 2>/dev/null || true
}

cleanup_listener() {
  local port="$1"
  local expected_fragment="$2"
  local label="$3"
  local pid

  while read -r pid; do
    [[ -n "$pid" ]] || continue
    local args
    args="$(process_args "$pid")"
    if [[ "$args" == *"$expected_fragment"* ]]; then
      terminate_pid "$pid"
      continue
    fi

    echo "$label port $port is occupied by an unexpected process:"
    echo "  pid=$pid"
    echo "  cmd=${args:-unknown}"
    return 1
  done < <(listener_pids "$port")
}

cleanup_conflicts() {
  cleanup_listener 6185 "$ROOT_DIR/main.py" "astrbot" || return 1
  cleanup_listener 6199 "$ROOT_DIR/main.py" "astrbot" || return 1
  cleanup_listener 8787 "$ROOT_DIR/scripts/codex_openai_bridge.py" "legacy codex-bridge" || return 1
}

start_service() {
  if is_running; then
    echo "astrbot is already running as unit $UNIT_NAME"
    return 0
  fi

  cleanup_conflicts || return 1

  systemd-run \
    --user \
    --quiet \
    --unit="$UNIT_NAME" \
    --collect \
    --property=WorkingDirectory="$ROOT_DIR" \
    /bin/bash -lc "./scripts/start_codex_astrbot.sh >>'$LOG_FILE' 2>&1"

  for _ in $(seq 1 20); do
    if is_running; then
      echo "Started astrbot as unit $UNIT_NAME"
      return 0
    fi
    sleep 0.5
  done

  echo "Failed to start astrbot; inspect $LOG_FILE and 'systemctl --user status $UNIT_NAME'"
  return 1
}

stop_service() {
  if is_running; then
    systemctl --user stop "$UNIT_NAME"
    systemctl --user reset-failed "$UNIT_NAME" >/dev/null 2>&1 || true
  fi

  cleanup_conflicts || true
  echo "Stopped astrbot"
}

status_service() {
  if is_running; then
    echo "astrbot: running (unit $UNIT_NAME) log=$LOG_FILE"
    return 0
  fi

  local pid
  while read -r pid; do
    [[ -n "$pid" ]] || continue
    local args
    args="$(process_args "$pid")"
    if [[ "$args" == *"$ROOT_DIR/main.py"* ]]; then
      echo "astrbot: running (legacy pid $pid) log=$LOG_FILE"
      return 0
    fi
  done < <(listener_pids 6185)

  echo "astrbot: stopped"
  return 1
}

usage() {
  echo "Usage: $0 {start|stop|restart|status}"
}

case "${1:-}" in
  start)
    start_service
    ;;
  stop)
    stop_service
    ;;
  restart)
    "$0" stop
    "$0" start
    ;;
  status)
    status_service
    ;;
  *)
    usage
    exit 1
    ;;
esac
