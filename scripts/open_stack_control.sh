#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_ROOT="${STACK_CONTROL_ROOT:-$ROOT_DIR/runtime/stack-control}"
TOKEN_FILE="$RUNTIME_ROOT/token"
PORT="${STACK_CONTROL_PORT:-8791}"

"$ROOT_DIR/scripts/manage_stack_control_server.sh" start >/dev/null

for _ in $(seq 1 20); do
  if [[ -s "$TOKEN_FILE" ]]; then
    break
  fi
  sleep 0.5
done

if [[ ! -s "$TOKEN_FILE" ]]; then
  echo "Stack control token file was not created: $TOKEN_FILE" >&2
  exit 1
fi

TOKEN="$(<"$TOKEN_FILE")"
URL="http://127.0.0.1:$PORT/$TOKEN/"

if command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$URL" >/dev/null 2>&1 &
else
  echo "$URL"
fi
