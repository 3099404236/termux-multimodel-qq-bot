#!/data/data/com.termux/files/usr/bin/bash
# napcat-watchdog.sh
#
# Reports when the bot stops receiving QQ messages.
#
# Why this exists: on 2026-08-01 the QQ account was kicked off at 10:48:54 and it
# went unnoticed for ~20 minutes. Nothing else in the stack shows it - AstrBot
# keeps running and serving HTTP, both bridges stay healthy, and the only trace is
# a single line on NapCat's console.
#
# Signals, in order:
#   1. AstrBot's log stops gaining "[qq(aiocqhttp)]" lines  -> traffic has stopped
#      (primary: it is a plain file, so unlike NapCat's console it has no
#      scrollback limit - the console loses its status lines within minutes in a
#      busy group)
#   2. only once traffic looks stalled, the expensive checks run, to say WHY:
#        - the qq process is gone            -> down
#        - NapCat's console shows a kick / QR prompt as its last word -> offline
#        - neither                           -> stalled (could just be a quiet night)
#
# "stalled" is deliberately distinct from "offline": a quiet group at 4am looks
# the same as an outage from the traffic side, and crying wolf would make the log
# useless. Only a kicked account or a dead process is reported as an outage.
#
# Recovery is not automatic by default - a watchdog that restarts NapCat on a
# false reading is worse than the outage. NAPCAT_WATCH_AUTO_RESTART=1 enables one
# quick-login restart per cooldown.
set -uo pipefail

export PREFIX=/data/data/com.termux/files/usr
export HOME=/data/data/com.termux/files/home
export TMPDIR=$PREFIX/tmp
export PATH=$PREFIX/bin:$PREFIX/bin/applets:/system/bin
export SCREENDIR=$HOME/.screen
export TERM=xterm-256color
export LANG=C.UTF-8

INTERVAL=${NAPCAT_WATCH_INTERVAL:-60}
STALE_TICKS=${NAPCAT_WATCH_STALE_TICKS:-5}          # ticks without traffic before looking closer
AUTO_RESTART=${NAPCAT_WATCH_AUTO_RESTART:-0}
RESTART_COOLDOWN=${NAPCAT_WATCH_RESTART_COOLDOWN:-900}
UIN=${NAPCAT_UIN:-}

ASTRBOT_LOG=$HOME/ubuntu-rootfs/root/AstrBot/astrbot.log
STATUS_FILE=$TMPDIR/napcat-status.json
LOG=$TMPDIR/napcat-watchdog.log
BUF=$TMPDIR/napcat-watchdog-console.txt
QR_PNG=/data/data/com.termux/files/usr/var/lib/proot-distro/containers/napcat/rootfs/root/Napcat/opt/QQ/resources/app/app_launcher/napcat/cache/qrcode.png

# Seen for real when the account was kicked. A QR prompt belongs here too:
# NapCat only asks for a scan when it is not logged in.
OFFLINE_RE='账号状态变更为离线|KickedOffLine|请扫描下面的二维码|无法重复登录'
ACTIVITY_RE='接收 <-|发送 ->'

stamp() { date '+%F %T'; }
log() { echo "[$(stamp)] $*" >>"$LOG"; }

qq_running() {
  ps -ef 2>/dev/null | grep -F 'qq --no-sandbox' | grep -qv grep
}

# Fingerprint of the newest QQ message line; changes whenever traffic flows.
traffic_sig() {
  grep -aF '[qq(aiocqhttp)]' "$ASTRBOT_LOG" 2>/dev/null | tail -1 | md5sum 2>/dev/null | cut -d' ' -f1
}

# Whether NapCat's console ends on an offline marker rather than on traffic.
console_says_offline() {
  rm -f "$BUF"
  screen -S napcat -X hardcopy -h "$BUF" >/dev/null 2>&1 || return 1
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    [ -s "$BUF" ] && break
    sleep 0.3
  done
  [ -s "$BUF" ] || return 1
  local last
  last=$(grep -aE "$OFFLINE_RE|$ACTIVITY_RE" "$BUF" 2>/dev/null | tail -1)
  [ -n "$last" ] && echo "$last" | grep -qaE "$OFFLINE_RE"
}

write_status() {
  cat >"$STATUS_FILE" <<EOF
{
  "state": "$1",
  "since": "$2",
  "checked_at": "$(stamp)",
  "detail": "$3",
  "quiet_ticks": $4,
  "uin": "$UIN",
  "qr_png": "$QR_PNG",
  "webui": "http://127.0.0.1:6099/webui?token=<see napcat webui.json>"
}
EOF
}

restart_quick() {
  if [ -z "$UIN" ]; then
    log "AUTO-RESTART skipped: set NAPCAT_UIN before enabling automatic restart"
    return 1
  fi
  log "AUTO-RESTART: restarting NapCat with quick login"
  screen -S napcat -X quit >/dev/null 2>&1 || true
  sleep 3
  # screen quit does not take down the proot tree; clear it explicitly, or two
  # QQ instances end up fighting over the account.
  for p in $(ps -ef 2>/dev/null | grep -E 'qq --no-sandbox|Xvfb :|xvfb-run' | grep -v grep | awk '{print $2}'); do
    kill -9 "$p" 2>/dev/null || true
  done
  sleep 3
  screen -dmS napcat bash -c "proot-distro sh napcat -- bash -c \"xvfb-run -a /root/Napcat/opt/QQ/qq --no-sandbox -q $UIN\""
}

state=init
since=$(stamp)
quiet=0
last_sig=$(traffic_sig)
last_restart=0
log "watchdog started (interval=${INTERVAL}s stale_ticks=${STALE_TICKS} auto_restart=${AUTO_RESTART})"

while true; do
  sig=$(traffic_sig)
  if [ -n "$sig" ] && [ "$sig" != "$last_sig" ]; then
    last_sig=$sig
    quiet=0
    new=online
    detail="messages flowing"
  else
    quiet=$((quiet + 1))
    if [ "$quiet" -lt "$STALE_TICKS" ]; then
      new=$state
      [ "$state" = init ] && new=online
      detail="no new messages for ${quiet} tick(s)"
    elif ! qq_running; then
      new=down
      detail="qq process is not running"
    elif console_says_offline; then
      new=offline
      detail="NapCat console ends on a kick/QR prompt - the account is logged out"
    else
      new=stalled
      detail="no messages for ${quiet} tick(s); QQ looks logged in (may just be quiet)"
    fi
  fi

  if [ "$new" != "$state" ]; then
    case "$new" in
      online)
        [ "$state" != init ] && log "RECOVERED: messages flowing again (was '$state' since $since)"
        ;;
      offline|down)
        log "*** QQ IS ${new} *** $detail"
        log "    fix: scan $QR_PNG, or open the NapCat WebUI on port 6099"
        ;;
      stalled)
        log "quiet: $detail"
        ;;
    esac
    [ "$state" = init ] && log "initial state: $new ($detail)"
    state=$new
    since=$(stamp)
  fi

  write_status "$state" "$since" "$detail" "$quiet"

  if [ "$AUTO_RESTART" = 1 ] && { [ "$state" = offline ] || [ "$state" = down ]; }; then
    now=$(date +%s)
    if [ $((now - last_restart)) -ge "$RESTART_COOLDOWN" ]; then
      last_restart=$now
      restart_quick
    fi
  fi

  sleep "$INTERVAL"
done
