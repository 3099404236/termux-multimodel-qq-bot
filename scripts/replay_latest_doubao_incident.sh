#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
INCIDENT_DIR="${DOUBAO_BRIDGE_INCIDENT_CAPTURE_DIR:-$ROOT_DIR/runtime/doubao-prod/logs/incidents}"

latest_prompt="$(find "$INCIDENT_DIR" -maxdepth 1 -type f -name '*.prompt.txt' | sort | tail -n 1)"
if [[ -z "${latest_prompt:-}" ]]; then
  echo "No captured Doubao incident prompt found in $INCIDENT_DIR" >&2
  exit 1
fi

exec "$PYTHON_BIN" "$ROOT_DIR/scripts/doubao_captcha_experiment.py" \
  --scenario prompt-fingerprint \
  --fingerprint-prompt-file "$latest_prompt" \
  --waves "${DOUBAO_REPLAY_WAVES:-3}" \
  --fingerprint-group-cooldown-seconds "${DOUBAO_REPLAY_GROUP_COOLDOWN_SECONDS:-60}" \
  --timeout-seconds "${DOUBAO_REPLAY_TIMEOUT_SECONDS:-90}"
