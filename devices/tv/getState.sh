#!/usr/bin/env bash

# # Demo stub: reports device state via a local marker file (real impl would probe the network/device).
# dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# if [[ -f "${dir}/.blocked" ]]; then
#   echo "down"
# else
#   echo "up"
# fi
# exit 0


# Get state by pinging device
# Script must complete within <10secs
if ping -c1 -W1 192.168.2.237 >/dev/null 2>&1; then
  echo "up"
else
  echo "down"
fi
exit 0