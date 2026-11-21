#!/bin/bash
set -u
cd "$(dirname "$0")" || exit 1
MESSAGES_FILE="$PWD/launcher/start_messages.txt"
msg() {
  local text
  text="$(/usr/bin/awk -v key="$1" 'index($0, key "=") == 1 { print substr($0, length(key) + 2); exit }' "$MESSAGES_FILE" 2>/dev/null)"
  if [ -z "$text" ]; then printf '[start_messages.txt: %s] %s' "$1" "${2:-}"; return 0; fi
  printf '%s' "${text//\{0\}/${2:-}}"
}

if [ "${1:-}" = --gui-worker ]; then
  exec ./runtime/python-macos-arm64/bin/python3 -I -B -u ./launcher/standalone.py gui-worker --target macos-arm64
fi

if [ "$(/usr/sbin/sysctl -n hw.optional.arm64 2>/dev/null)" != 1 ]; then
  echo "$(msg not_arm_mac)" >&2
  exit 1
fi
macos="$(/usr/bin/sw_vers -productVersion 2>/dev/null | /usr/bin/cut -d. -f1)"
case "$macos" in ''|*[!0-9]*) macos=0 ;; esac
if [ "$macos" -lt 15 ]; then
  echo "$(msg old_macos)" >&2
  exit 1
fi
/usr/bin/xattr -dr com.apple.quarantine "$PWD" 2>/dev/null || :
if [ ! -d "$PWD/Start EZR.app" ]; then
  echo "$(msg no_app)" >&2
  exit 1
fi
exec /usr/bin/open "$PWD/Start EZR.app"
