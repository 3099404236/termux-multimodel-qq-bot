#!/usr/bin/env sh
set -eu

ASTRBOT_ROOT="${ASTRBOT_PHONE_ROOT:-/root/AstrBot}"
SITE_PACKAGES="${ASTRBOT_PHONE_SITE_PACKAGES:-/root/antigravity-bridge/site-packages}"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3.12}"

export PYTHONPATH="$SITE_PACKAGES:$ASTRBOT_ROOT${PYTHONPATH:+:$PYTHONPATH}"
cd "$ASTRBOT_ROOT"

exec "$PYTHON_BIN" "$ASTRBOT_ROOT/main.py"
