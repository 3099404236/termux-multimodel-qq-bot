#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv-codex-bridge/bin/python}"

exec "$PYTHON_BIN" "$ROOT_DIR/scripts/qq_stack_boot_listener.py"
