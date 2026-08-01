#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv-antigravity-bridge/bin/python}"

export ASTRBOT_ROOT="${ASTRBOT_ROOT:-$ROOT_DIR/runtime/antigravity-prod}"
CHAT_WORKDIR="${ANTIGRAVITY_BRIDGE_WORKDIR:-$ASTRBOT_ROOT/antigravity-chat-workdir}"

mkdir -p "$CHAT_WORKDIR"

command=(
  "$PYTHON_BIN"
  "$ROOT_DIR/scripts/antigravity_openai_bridge.py"
  --host "${ANTIGRAVITY_BRIDGE_HOST:-127.0.0.1}"
  --port "${ANTIGRAVITY_BRIDGE_PORT:-8791}"
  --model "${ANTIGRAVITY_BRIDGE_MODEL:-antigravity}"
  --agy-bin "${ANTIGRAVITY_BRIDGE_AGY_BIN:-agy}"
  --timeout "${ANTIGRAVITY_BRIDGE_TIMEOUT:-180}"
  --print-timeout "${ANTIGRAVITY_BRIDGE_PRINT_TIMEOUT:-150}"
  --max-parallel-requests "${ANTIGRAVITY_BRIDGE_MAX_PARALLEL:-1}"
  --workdir "$CHAT_WORKDIR"
)

if [[ -n "${ANTIGRAVITY_BRIDGE_AGY_MODEL:-}" ]]; then
  command+=(--agy-model "$ANTIGRAVITY_BRIDGE_AGY_MODEL")
fi

if [[ -n "${ANTIGRAVITY_BRIDGE_EFFORT:-}" ]]; then
  command+=(--effort "$ANTIGRAVITY_BRIDGE_EFFORT")
fi

if [[ "${ANTIGRAVITY_BRIDGE_SANDBOX:-true}" == "false" ]]; then
  command+=(--disable-sandbox)
fi

exec "${command[@]}"
