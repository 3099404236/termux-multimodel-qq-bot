#!/data/data/com.termux/files/usr/bin/sh
# Relaunch only the antigravity bridge, the same way 10-astrbot-stack does it.
# The start script lives inside the proot rootfs and does mkdir /root/..., so it
# has to run under proot - from the termux view /root is a read-only system dir.
set -eu

PROOT_BIN=/data/data/com.termux/files/usr/bin/proot
ROOTFS=/data/data/com.termux/files/home/ubuntu-rootfs

rm -f "$ROOTFS/root/antigravity-bridge/bridge.pid"

nohup "$PROOT_BIN" -0 \
  -r "$ROOTFS" \
  -b /dev \
  -b /proc \
  -w /root \
  /usr/bin/env -i \
  HOME=/root \
  PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  LANG=C.UTF-8 \
  /bin/sh /root/antigravity-bridge/launch_antigravity_bridge_phone.sh \
  >/dev/null 2>&1 &

echo "launcher fired, waiting for the port..."
i=0
while [ "$i" -lt 40 ]; do
  if /system/bin/ps -ef | grep -q '[a]ntigravity_openai_bridge'; then
    echo "process up after ${i}s"
    break
  fi
  sleep 1
  i=$((i + 1))
done

/system/bin/ps -ef | grep '[a]ntigravity_openai_bridge' | head -n 1 | cut -c1-60
