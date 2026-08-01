#!/data/data/com.termux/files/usr/bin/bash
set -e

UIN=${NAPCAT_UIN:?Set NAPCAT_UIN before restarting NapCat}

screen -S napcat -X quit >/dev/null 2>&1 || true
sleep 2
screen -dmS napcat bash -c "proot-distro sh napcat -- bash -c \"xvfb-run -a /root/Napcat/opt/QQ/qq --no-sandbox -q $UIN\""
