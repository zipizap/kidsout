#!/usr/bin/env bash
# kidsout xbox: allow internet (play time). Operator convenience; identical to
# unblock.sh, which is the name upstream kidsout calls.
set -euo pipefail
cd "$(dirname "$0")"
exec python3 xbox.py allow "$@"
