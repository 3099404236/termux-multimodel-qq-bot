#!/data/data/com.termux/files/usr/bin/sh
# Start the stack monitor if it is not already running.
HOME_DIR=/data/data/com.termux/files/home
PREFIX=/data/data/com.termux/files/usr
export PATH=$PREFIX/bin:/system/bin
export HOME=$HOME_DIR
export TMPDIR=$PREFIX/tmp

if ps -ef 2>/dev/null | grep -F 'stack-monitor.mjs' | grep -v 'start-stack-monitor' | grep -qv grep; then
  echo "stack-monitor already running"
  exit 0
fi

/system/bin/setsid "$PREFIX/bin/node" "$HOME_DIR/stack-monitor.mjs" \
  >>"$TMPDIR/stack-monitor.log" 2>&1 &
sleep 4
if ps -ef 2>/dev/null | grep -F 'stack-monitor.mjs' | grep -v 'start-stack-monitor' | grep -qv grep; then
  echo "stack-monitor started"
else
  echo "stack-monitor FAILED to start"
  tail -n 5 "$TMPDIR/stack-monitor.log" 2>/dev/null
  exit 1
fi
