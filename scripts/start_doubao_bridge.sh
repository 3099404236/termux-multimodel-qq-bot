#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv-doubao-bridge/bin/python}"

export ASTRBOT_ROOT="${ASTRBOT_ROOT:-$ROOT_DIR/runtime/doubao-prod}"
PROFILE_DIR="${DOUBAO_BRIDGE_USER_DATA_DIR:-$ASTRBOT_ROOT/doubao-browser-profile}"
PROMPT_CAPTURE_PATH="${DOUBAO_BRIDGE_PROMPT_CAPTURE_PATH:-$ASTRBOT_ROOT/logs/doubao-prompts.jsonl}"
INCIDENT_CAPTURE_DIR="${DOUBAO_BRIDGE_INCIDENT_CAPTURE_DIR:-$ASTRBOT_ROOT/logs/incidents}"

mkdir -p "$PROFILE_DIR"
mkdir -p "$(dirname "$PROMPT_CAPTURE_PATH")"
mkdir -p "$INCIDENT_CAPTURE_DIR"

cmd=(
  "$PYTHON_BIN"
  "$ROOT_DIR/scripts/doubao_web_bridge.py"
  --host "${DOUBAO_BRIDGE_HOST:-127.0.0.1}"
  --port "${DOUBAO_BRIDGE_PORT:-8790}"
  --model "${DOUBAO_BRIDGE_MODEL:-doubao-web}"
  --timeout "${DOUBAO_BRIDGE_TIMEOUT:-120}"
  --ready-timeout "${DOUBAO_BRIDGE_READY_TIMEOUT:-20}"
  --max-parallel-requests "${DOUBAO_BRIDGE_MAX_PARALLEL:-1}"
  --user-data-dir "$PROFILE_DIR"
  --prompt-capture-path "$PROMPT_CAPTURE_PATH"
  --incident-capture-dir "$INCIDENT_CAPTURE_DIR"
)

if [[ -n "${DOUBAO_BRIDGE_BROWSER:-}" ]]; then
  cmd+=(--browser-executable "$DOUBAO_BRIDGE_BROWSER")
elif [[ -x "/usr/bin/google-chrome" ]]; then
  cmd+=(--browser-executable "/usr/bin/google-chrome")
fi

if [[ -n "${DOUBAO_BRIDGE_HEADLESS:-}" ]]; then
  if [[ "${DOUBAO_BRIDGE_HEADLESS}" == "true" ]]; then
    cmd+=(--headless)
  else
    cmd+=(--no-headless)
  fi
elif [[ -n "${DISPLAY:-}" || -n "${WAYLAND_DISPLAY:-}" ]]; then
  cmd+=(--no-headless)
else
  cmd+=(--headless)
fi

exec "${cmd[@]}"
