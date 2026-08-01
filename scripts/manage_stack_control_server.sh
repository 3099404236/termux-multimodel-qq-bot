#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_ROOT="${STACK_CONTROL_ROOT:-$ROOT_DIR/runtime/stack-control}"
LOG_DIR="$RUNTIME_ROOT/logs"
LOG_FILE="$LOG_DIR/stack-control.log"
UNIT_NAME="${STACK_CONTROL_UNIT_NAME:-astrbot-stack-control}"

mkdir -p "$LOG_DIR"

is_running() {
  systemctl --user --quiet is-active "$UNIT_NAME"
}

start_service() {
  if is_running; then
    echo "stack-control is already running as unit $UNIT_NAME"
    return 0
  fi

  systemd-run \
    --user \
    --quiet \
    --unit="$UNIT_NAME" \
    --collect \
    --property=WorkingDirectory="$ROOT_DIR" \
    /bin/bash -lc "./scripts/start_stack_control_server.sh >>'$LOG_FILE' 2>&1"

  for _ in $(seq 1 20); do
    if is_running; then
      echo "Started stack-control as unit $UNIT_NAME"
      return 0
    fi
    sleep 0.5
  done

  echo "Failed to start stack-control; inspect $LOG_FILE and 'systemctl --user status $UNIT_NAME'"
  return 1
}

stop_service() {
  if ! is_running; then
    echo "stack-control is not running"
    return 0
  fi

  systemctl --user stop "$UNIT_NAME"
  systemctl --user reset-failed "$UNIT_NAME" >/dev/null 2>&1 || true
  echo "Stopped stack-control"
}

status_service() {
  if is_running; then
    echo "stack-control: running (unit $UNIT_NAME) log=$LOG_FILE"
    return 0
  fi

  echo "stack-control: stopped"
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
