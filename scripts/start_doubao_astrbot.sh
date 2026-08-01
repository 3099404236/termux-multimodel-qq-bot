#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv-doubao-bridge/bin/python}"

export ASTRBOT_ROOT="${ASTRBOT_ROOT:-$ROOT_DIR/runtime/doubao-prod}"

exec "$PYTHON_BIN" "$ROOT_DIR/main.py"
