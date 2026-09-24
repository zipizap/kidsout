#!/usr/bin/env bash
# kidsout getState.sh — upstream kidsout (Go web app) script contract.
#
# Prints ONE word on stdout: up | down.
#   up    keyguard is unlocked: tablet is actively being used (game, video, ...)
#   down  keyguard is locked, screen is off, or the daemon is unreachable
#         (screen off drops Wi-Fi to save battery, so unreachable == asleep,
#         see devices/tablet/tools/README.md)
#
# Script must complete within <10secs (koTabletctl's own connect timeout is 5s).
set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ctl="${KIDSOUT_KOTABLETCTL:-$here/tools/koTabletctl}"

status="$("${ctl}" status 2>/dev/null)"
if echo "${status}" | grep -q 'keyguard: unlocked'; then
  echo "up"
else
  echo "down"
fi
exit 0
