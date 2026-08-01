#!/data/data/com.termux/files/usr/bin/sh
# Watchdog for chat-bridge: restarts the node process whenever it exits.
BRIDGE_DIR="/data/data/com.termux/files/home/chatgpt-web-codex-bridge"
LOG="/data/data/com.termux/files/tmp/chat-bridge.log"
export HOME=/data/data/com.termux/files/home
export PREFIX=/data/data/com.termux/files/usr
export TMPDIR=/data/data/com.termux/files/tmp
export PATH=/data/data/com.termux/files/usr/bin:/system/bin
while true; do
  node "$BRIDGE_DIR/code/src/chat-bridge.mjs" >>"$LOG" 2>&1
  echo "[chat-bridge] exited, restarting in 3s..." >>"$LOG"
  sleep 3
done
