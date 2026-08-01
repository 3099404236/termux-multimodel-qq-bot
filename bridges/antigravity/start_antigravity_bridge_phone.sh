#!/usr/bin/env sh
set -eu

BRIDGE_ROOT="${ANTIGRAVITY_BRIDGE_ROOT:-/root/antigravity-bridge}"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3.12}"
CHAT_WORKDIR="${ANTIGRAVITY_BRIDGE_WORKDIR:-$BRIDGE_ROOT/workdir}"

mkdir -p "$CHAT_WORKDIR"
export PYTHONPATH="${PYTHONPATH:-$BRIDGE_ROOT/site-packages}"

set -- \
  "$PYTHON_BIN" \
  "$BRIDGE_ROOT/antigravity_openai_bridge.py" \
  --host "${ANTIGRAVITY_BRIDGE_HOST:-127.0.0.1}" \
  --port "${ANTIGRAVITY_BRIDGE_PORT:-8791}" \
  --model "${ANTIGRAVITY_BRIDGE_MODEL:-antigravity}" \
  --agy-bin "${ANTIGRAVITY_BRIDGE_AGY_BIN:-/usr/local/bin/agy}" \
  --timeout "${ANTIGRAVITY_BRIDGE_TIMEOUT:-300}" \
  --print-timeout "${ANTIGRAVITY_BRIDGE_PRINT_TIMEOUT:-300}" \
  --max-parallel-requests "${ANTIGRAVITY_BRIDGE_MAX_PARALLEL:-1}" \
  --session-max-turns "${ANTIGRAVITY_BRIDGE_SESSION_MAX_TURNS:-20}" \
  --session-idle-seconds "${ANTIGRAVITY_BRIDGE_SESSION_IDLE_SECONDS:-21600}" \
  --workdir "$CHAT_WORKDIR"

if [ "${ANTIGRAVITY_BRIDGE_REUSE_CONVERSATIONS:-true}" = "true" ]; then
  set -- "$@" --reuse-conversations
fi

if [ "${ANTIGRAVITY_BRIDGE_PERSISTENT_TUI:-true}" = "true" ]; then
  set -- \
    "$@" \
    --persistent-tui \
    --persistent-session-limit \
    "${ANTIGRAVITY_BRIDGE_PERSISTENT_SESSION_LIMIT:-2}" \
    --persistent-columns \
    "${ANTIGRAVITY_BRIDGE_PERSISTENT_COLUMNS:-160}" \
    --persistent-rows \
    "${ANTIGRAVITY_BRIDGE_PERSISTENT_ROWS:-50}" \
    --persistent-write-timeout \
    "${ANTIGRAVITY_BRIDGE_PERSISTENT_WRITE_TIMEOUT:-5}" \
    --persistent-submit-timeout \
    "${ANTIGRAVITY_BRIDGE_PERSISTENT_SUBMIT_TIMEOUT:-20}" \
    --persistent-progress-timeout \
    "${ANTIGRAVITY_BRIDGE_PERSISTENT_PROGRESS_TIMEOUT:-180}" \
    --persistent-buffer-screens \
    "${ANTIGRAVITY_BRIDGE_PERSISTENT_BUFFER_SCREENS:-256}"
fi

if [ -n "${ANTIGRAVITY_BRIDGE_AGY_MODEL:-}" ]; then
  set -- "$@" --agy-model "$ANTIGRAVITY_BRIDGE_AGY_MODEL"
fi

if [ -n "${ANTIGRAVITY_BRIDGE_EFFORT:-}" ]; then
  set -- "$@" --effort "$ANTIGRAVITY_BRIDGE_EFFORT"
fi

if [ "${ANTIGRAVITY_BRIDGE_SANDBOX:-true}" = "false" ]; then
  set -- "$@" --disable-sandbox
fi

exec "$@"
