#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv-codex-bridge/bin/python}"

export ASTRBOT_ROOT="${ASTRBOT_ROOT:-$ROOT_DIR/runtime/codex-prod}"
CHAT_WORKDIR="${CODEX_BRIDGE_WORKDIR:-$ASTRBOT_ROOT/codex-chat-workdir}"

mkdir -p "$CHAT_WORKDIR"

exec "$PYTHON_BIN" \
  "$ROOT_DIR/scripts/codex_openai_bridge.py" \
  --host "${CODEX_BRIDGE_HOST:-127.0.0.1}" \
  --port "${CODEX_BRIDGE_PORT:-8787}" \
  --model "${CODEX_BRIDGE_MODEL:-gpt-5.4}" \
  --sandbox "${CODEX_BRIDGE_SANDBOX:-read-only}" \
  --timeout "${CODEX_BRIDGE_TIMEOUT:-90}" \
  --max-parallel-requests "${CODEX_BRIDGE_MAX_PARALLEL:-1}" \
  --workdir "$CHAT_WORKDIR"
