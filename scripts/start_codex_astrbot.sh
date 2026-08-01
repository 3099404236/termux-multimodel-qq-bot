#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv-codex-bridge/bin/python}"

export ASTRBOT_ROOT="${ASTRBOT_ROOT:-$ROOT_DIR/runtime/codex-prod}"
unset http_proxy https_proxy all_proxy no_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY

exec "$PYTHON_BIN" "$ROOT_DIR/main.py"
