#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_ROOT="${ASTRBOT_ROOT:-$ROOT_DIR/runtime/doubao-prod}"
LOG_DIR="$RUNTIME_ROOT/logs"
PID_DIR="$RUNTIME_ROOT/pids"
BRIDGE_PID_FILE="$PID_DIR/doubao-bridge.pid"
ASTRBOT_PID_FILE="$PID_DIR/astrbot.pid"
BRIDGE_LOG_FILE="$LOG_DIR/doubao-bridge.log"
ASTRBOT_LOG_FILE="$LOG_DIR/astrbot.log"

mkdir -p "$LOG_DIR" "$PID_DIR"

is_running() {
  local pid_file="$1"
  if [[ ! -f "$pid_file" ]]; then
    return 1
  fi

  local pid
  pid="$(<"$pid_file")"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

start_service() {
  local name="$1"
  local pid_file="$2"
  local log_file="$3"
  shift 3

  if is_running "$pid_file"; then
    echo "$name is already running with pid $(<"$pid_file")"
    return 0
  fi

  setsid "$@" >>"$log_file" 2>&1 < /dev/null &
  local pid=$!
  echo "$pid" >"$pid_file"
  echo "Started $name with pid $pid"
}

stop_service() {
  local name="$1"
  local pid_file="$2"

  if ! is_running "$pid_file"; then
    rm -f "$pid_file"
    echo "$name is not running"
    return 0
  fi

  local pid
  pid="$(<"$pid_file")"
  kill "$pid"

  for _ in $(seq 1 20); do
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$pid_file"
      echo "Stopped $name"
      return 0
    fi
    sleep 0.5
  done

  kill -9 "$pid" 2>/dev/null || true
  rm -f "$pid_file"
  echo "Force stopped $name"
}

status_service() {
  local name="$1"
  local pid_file="$2"
  local log_file="$3"

  if is_running "$pid_file"; then
    echo "$name: running (pid $(<"$pid_file")) log=$log_file"
    return 0
  fi

  echo "$name: stopped"
  return 1
}

usage() {
  echo "Usage: $0 {start|stop|restart|status}"
}

case "${1:-}" in
  start)
    start_service "doubao-bridge" "$BRIDGE_PID_FILE" "$BRIDGE_LOG_FILE" \
      "$ROOT_DIR/scripts/start_doubao_bridge.sh"
    start_service "astrbot" "$ASTRBOT_PID_FILE" "$ASTRBOT_LOG_FILE" \
      "$ROOT_DIR/scripts/start_doubao_astrbot.sh"
    ;;
  stop)
    stop_service "astrbot" "$ASTRBOT_PID_FILE"
    stop_service "doubao-bridge" "$BRIDGE_PID_FILE"
    ;;
  restart)
    "$0" stop
    "$0" start
    ;;
  status)
    status_code=0
    status_service "doubao-bridge" "$BRIDGE_PID_FILE" "$BRIDGE_LOG_FILE" || status_code=1
    status_service "astrbot" "$ASTRBOT_PID_FILE" "$ASTRBOT_LOG_FILE" || status_code=1
    exit "$status_code"
    ;;
  *)
    usage
    exit 1
    ;;
esac
