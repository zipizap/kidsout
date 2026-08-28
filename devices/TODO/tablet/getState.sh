#!/usr/bin/env bash
# Demo stub: reports device state via a local marker file (real impl would probe the network/device).
dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "${dir}/.blocked" ]]; then
  echo "down"
else
  echo "up"
fi
exit 0
