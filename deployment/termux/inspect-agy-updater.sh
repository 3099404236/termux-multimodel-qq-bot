#!/data/data/com.termux/files/usr/bin/bash
set -o pipefail

binary="$HOME/ubuntu-rootfs/usr/local/bin/agy"

printf '%s\n' '--- candidate flags and environment variables ---'
strings "$binary" \
  | grep -Eio -- 'AGY_[A-Z0-9_]{1,100}|ANTIGRAVITY_[A-Z0-9_]{1,100}|[A-Z][A-Z0-9_]{2,100}(UPDATE|UPDATER)[A-Z0-9_]{0,60}|--[a-z0-9-]*(update|updater)[a-z0-9-]*' \
  | sort -u \
  | head -n 400

printf '%s\n' '--- short updater-related strings ---'
strings "$binary" \
  | awk 'length($0) <= 240 && tolower($0) ~ /(bg-updater|background updater|auto.?update|disable.*update|update.*disable|skip.*update|no.?update)/ { print }' \
  | sort -u \
  | head -n 400
