#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv-codex-bridge/bin/python}"

export STACK_CONTROL_ROOT="${STACK_CONTROL_ROOT:-$ROOT_DIR/runtime/stack-control}"

exec "$PYTHON_BIN" \
  "$ROOT_DIR/scripts/stack_control_server.py" \
  --host "${STACK_CONTROL_HOST:-0.0.0.0}" \
  --port "${STACK_CONTROL_PORT:-8791}" \
  --runtime-root "$STACK_CONTROL_ROOT"
