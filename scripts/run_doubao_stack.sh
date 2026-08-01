#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SUPERVISOR_ROOT="${ASTRBOT_SUPERVISOR_ROOT:-$ROOT_DIR/runtime/foreground-doubao-stack}"
LOG_DIR="$SUPERVISOR_ROOT/logs"
BRIDGE_LOG_FILE="$LOG_DIR/doubao-bridge.log"
ASTRBOT_LOG_FILE="$LOG_DIR/astrbot.log"
BRIDGE_SCRIPT="$ROOT_DIR/scripts/start_doubao_bridge.sh"
ASTRBOT_SCRIPT="$ROOT_DIR/scripts/start_codex_astrbot.sh"

mkdir -p "$LOG_DIR"

bridge_pid=""
astrbot_pid=""
cleanup_started=0
bridge_env=()

stop_managed_services() {
  "$ROOT_DIR/scripts/manage_doubao_bridge.sh" stop >/dev/null 2>&1 || true
  "$ROOT_DIR/scripts/manage_doubao_prod.sh" stop >/dev/null 2>&1 || true
  "$ROOT_DIR/scripts/manage_codex_prod.sh" stop >/dev/null 2>&1 || true
}

wait_for_exit() {
  local pid="$1"
  for _ in $(seq 1 20); do
    if ! kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
    sleep 0.5
  done
  return 1
}

stop_process_group() {
  local pid="${1:-}"
  if [[ -z "$pid" ]]; then
    return 0
  fi
  if ! kill -0 "$pid" 2>/dev/null; then
    return 0
  fi

  kill -TERM -- "-$pid" 2>/dev/null || true
  if wait_for_exit "$pid"; then
    return 0
  fi

  kill -KILL -- "-$pid" 2>/dev/null || true
  wait_for_exit "$pid" || true
}

cleanup() {
  if [[ "$cleanup_started" -eq 1 ]]; then
    return 0
  fi
  cleanup_started=1

  echo
  echo "Stopping foreground stack..."
  stop_process_group "$astrbot_pid"
  stop_process_group "$bridge_pid"
}

trap cleanup EXIT INT TERM HUP

start_supervised() {
  local name="$1"
  local log_file="$2"
  shift 2

  : >"$log_file"
  setsid "$@" >>"$log_file" 2>&1 &
  local pid=$!

  sleep 1
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "$name exited immediately. Check $log_file" >&2
    tail -n 40 "$log_file" >&2 || true
    return 1
  fi

  echo "Started $name (pid $pid)"
  echo "  log: $log_file"
  LAST_PID="$pid"
}

wait_until_ready() {
  local url="$1"
  local label="$2"

  if ! command -v curl >/dev/null 2>&1; then
    echo "curl not found, skipping $label readiness check."
    return 0
  fi

  for _ in $(seq 1 60); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      echo "$label is ready: $url"
      return 0
    fi
    sleep 1
  done

  echo "$label did not become ready in time: $url" >&2
  return 1
}

monitor_children() {
  while true; do
    if [[ -n "$bridge_pid" ]] && ! kill -0 "$bridge_pid" 2>/dev/null; then
      echo "doubao-bridge exited. Check $BRIDGE_LOG_FILE" >&2
      return 1
    fi
    if [[ -n "$astrbot_pid" ]] && ! kill -0 "$astrbot_pid" 2>/dev/null; then
      echo "astrbot exited. Check $ASTRBOT_LOG_FILE" >&2
      return 1
    fi
    sleep 2
  done
}

if [[ ! -x "$BRIDGE_SCRIPT" ]]; then
  echo "Bridge start script not found: $BRIDGE_SCRIPT" >&2
  exit 1
fi

if [[ ! -x "$ASTRBOT_SCRIPT" ]]; then
  echo "AstrBot start script not found: $ASTRBOT_SCRIPT" >&2
  exit 1
fi

stop_managed_services

if [[ -n "${DISPLAY:-}" || -n "${WAYLAND_DISPLAY:-}" ]]; then
  bridge_env=(DOUBAO_BRIDGE_HEADLESS=false)
fi

echo "Starting Doubao foreground stack..."
start_supervised \
  "doubao-bridge" \
  "$BRIDGE_LOG_FILE" \
  env \
  ASTRBOT_ROOT="$ROOT_DIR/runtime/doubao-prod" \
  "${bridge_env[@]}" \
  "$BRIDGE_SCRIPT"
bridge_pid="$LAST_PID"

wait_until_ready "http://127.0.0.1:8790/healthz" "doubao-bridge"

start_supervised \
  "astrbot" \
  "$ASTRBOT_LOG_FILE" \
  env \
  ASTRBOT_ROOT="$ROOT_DIR/runtime/codex-prod" \
  "$ASTRBOT_SCRIPT"
astrbot_pid="$LAST_PID"

wait_until_ready "http://127.0.0.1:6185" "astrbot-webui"

cat <<EOF
Foreground stack is running.
  WebUI: http://127.0.0.1:6185
  Doubao bridge: http://127.0.0.1:8790
  AstrBot log: $ASTRBOT_LOG_FILE
  Bridge log: $BRIDGE_LOG_FILE

Keep this script running.
Press Ctrl+C or close this terminal to stop both processes.
EOF

monitor_children
