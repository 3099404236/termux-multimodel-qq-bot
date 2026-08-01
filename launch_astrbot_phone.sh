#!/usr/bin/env sh
set -eu

ASTRBOT_ROOT="${ASTRBOT_PHONE_ROOT:-/root/AstrBot}"
PID_FILE="${ASTRBOT_PHONE_PID_FILE:-$ASTRBOT_ROOT/astrbot.pid}"
LOG_FILE="${ASTRBOT_PHONE_LOG_FILE:-$ASTRBOT_ROOT/astrbot.log}"

if [ -f "$PID_FILE" ]; then
  running_pid="$(cat "$PID_FILE")"
  if kill -0 "$running_pid" 2>/dev/null; then
    echo "AstrBot is already running with PID $running_pid."
    exit 0
  fi
fi

nohup /bin/sh "$ASTRBOT_ROOT/start_astrbot_phone.sh" \
  >>"$LOG_FILE" 2>&1 </dev/null &
astrbot_pid="$!"
echo "$astrbot_pid" >"$PID_FILE"

sleep 3
if ! kill -0 "$astrbot_pid" 2>/dev/null; then
  echo "AstrBot failed to start. Check $LOG_FILE." >&2
  exit 1
fi

echo "AstrBot started with PID $astrbot_pid."
