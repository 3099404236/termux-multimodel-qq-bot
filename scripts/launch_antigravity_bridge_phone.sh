#!/usr/bin/env sh
set -eu

BRIDGE_ROOT="${ANTIGRAVITY_BRIDGE_ROOT:-/root/antigravity-bridge}"
PID_FILE="${ANTIGRAVITY_BRIDGE_PID_FILE:-$BRIDGE_ROOT/bridge.pid}"
LOG_FILE="${ANTIGRAVITY_BRIDGE_LOG_FILE:-$BRIDGE_ROOT/bridge.log}"

if [ -f "$PID_FILE" ]; then
  running_pid="$(cat "$PID_FILE")"
  if kill -0 "$running_pid" 2>/dev/null; then
    echo "Antigravity bridge is already running with PID $running_pid."
    exit 0
  fi
fi

nohup /bin/sh "$BRIDGE_ROOT/start_antigravity_bridge_phone.sh" \
  >>"$LOG_FILE" 2>&1 </dev/null &
bridge_pid="$!"
echo "$bridge_pid" >"$PID_FILE"

sleep 1
if ! kill -0 "$bridge_pid" 2>/dev/null; then
  echo "Antigravity bridge failed to start. Check $LOG_FILE." >&2
  exit 1
fi

echo "Antigravity bridge started with PID $bridge_pid."
