#!/data/data/com.termux/files/usr/bin/sh
# Start the NapCat offline watchdog if it is not already running.
HOME_DIR=/data/data/com.termux/files/home
PREFIX=/data/data/com.termux/files/usr
export PATH=$PREFIX/bin:/system/bin

if ps -ef 2>/dev/null | grep -F 'napcat-watchdog.sh' | grep -v 'start-napcat-watchdog' | grep -qv grep; then
  echo "watchdog already running"
  exit 0
fi

/system/bin/setsid "$PREFIX/bin/bash" "$HOME_DIR/napcat-watchdog.sh" >/dev/null 2>&1 &
sleep 3
if ps -ef 2>/dev/null | grep -F 'napcat-watchdog.sh' | grep -v 'start-napcat-watchdog' | grep -qv grep; then
  echo "watchdog started"
else
  echo "watchdog FAILED to start"
  exit 1
fi
