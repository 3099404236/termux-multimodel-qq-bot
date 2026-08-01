#!/data/data/com.termux/files/usr/bin/sh
set -eu

PROOT_BIN="${ASTRBOT_PROOT_BIN:-/data/data/com.termux/files/usr/bin/proot}"
ROOTFS="${ASTRBOT_ROOTFS:-/data/data/com.termux/files/home/ubuntu-rootfs}"

if command -v termux-wake-lock >/dev/null 2>&1; then
  termux-wake-lock
fi

launch_in_ubuntu() {
  nohup "$PROOT_BIN" -0 \
    -r "$ROOTFS" \
    -b /dev \
    -b /proc \
    -w /root \
    /usr/bin/env -i \
    HOME=/root \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    LANG=C.UTF-8 \
    "$@" >/dev/null 2>&1 &
}

launch_in_ubuntu /bin/sh /root/antigravity-bridge/launch_antigravity_bridge_phone.sh
launch_in_ubuntu /bin/sh /root/AstrBot/launch_astrbot_phone.sh

echo "AstrBot and the Antigravity bridge launchers were started."
