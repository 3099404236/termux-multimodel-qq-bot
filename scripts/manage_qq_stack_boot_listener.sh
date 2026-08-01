#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_ROOT="${STACK_BOOT_LISTENER_ROOT:-$ROOT_DIR/runtime/stack-boot-listener}"
LOG_DIR="$RUNTIME_ROOT/logs"
LOG_FILE="$LOG_DIR/qq-stack-boot-listener.log"
UNIT_NAME="${STACK_BOOT_LISTENER_UNIT_NAME:-astrbot-qq-stack-boot-listener}"

mkdir -p "$LOG_DIR"

is_running() {
  systemctl --user --quiet is-active "$UNIT_NAME"
}

start_service() {
  if is_running; then
    echo "qq-stack-boot-listener is already running as unit $UNIT_NAME"
    return 0
  fi

  systemd-run \
    --user \
    --quiet \
    --unit="$UNIT_NAME" \
    --collect \
    --property=WorkingDirectory="$ROOT_DIR" \
    /bin/bash -lc "./scripts/start_qq_stack_boot_listener.sh >>'$LOG_FILE' 2>&1"

  for _ in $(seq 1 20); do
    if is_running; then
      echo "Started qq-stack-boot-listener as unit $UNIT_NAME"
      return 0
    fi
    sleep 0.5
  done

  echo "Failed to start qq-stack-boot-listener; inspect $LOG_FILE and 'systemctl --user status $UNIT_NAME'"
  return 1
}

stop_service() {
  if ! is_running; then
    echo "qq-stack-boot-listener is not running"
    return 0
  fi

  systemctl --user stop "$UNIT_NAME"
  systemctl --user reset-failed "$UNIT_NAME" >/dev/null 2>&1 || true
  echo "Stopped qq-stack-boot-listener"
}

status_service() {
  if is_running; then
    echo "qq-stack-boot-listener: running (unit $UNIT_NAME) log=$LOG_FILE"
    return 0
  fi

  echo "qq-stack-boot-listener: stopped"
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
