#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_ROOT="${ASTRBOT_ROOT:-$ROOT_DIR/runtime/doubao-prod}"
LOG_DIR="$RUNTIME_ROOT/logs"
LOG_FILE="$LOG_DIR/doubao-bridge.log"
UNIT_NAME="${DOUBAO_BRIDGE_UNIT_NAME:-astrbot-doubao-bridge}"

mkdir -p "$LOG_DIR"

is_running() {
  systemctl --user --quiet is-active "$UNIT_NAME"
}

start_service() {
  if is_running; then
    echo "doubao-bridge is already running as unit $UNIT_NAME"
    return 0
  fi

  systemd-run \
    --user \
    --quiet \
    --unit="$UNIT_NAME" \
    --collect \
    --property=WorkingDirectory="$ROOT_DIR" \
    /bin/bash -lc "./scripts/start_doubao_bridge.sh >>'$LOG_FILE' 2>&1"

  for _ in $(seq 1 20); do
    if is_running; then
      echo "Started doubao-bridge as unit $UNIT_NAME"
      return 0
    fi
    sleep 0.5
  done

  echo "Failed to start doubao-bridge; inspect $LOG_FILE and 'systemctl --user status $UNIT_NAME'"
  return 1
}

stop_service() {
  if ! is_running; then
    echo "doubao-bridge is not running"
    return 0
  fi

  systemctl --user stop "$UNIT_NAME"
  systemctl --user reset-failed "$UNIT_NAME" >/dev/null 2>&1 || true
  echo "Stopped doubao-bridge"
}

status_service() {
  if is_running; then
    echo "doubao-bridge: running (unit $UNIT_NAME) log=$LOG_FILE"
    return 0
  fi

  echo "doubao-bridge: stopped"
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
