#!/usr/bin/env bash

# # Demo stub: blocks the device (real impl would e.g. add a firewall rule).
# dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# touch "${dir}/.blocked"
# exit 0


# Block by calling the TV's webOS API to turn it off, using an additional tool
dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${dir}"
cd tools
./lg-webos-ssap \
  -addr 192.168.2.237:3000 \
  -key-file "${dir}/.lg-webos-ssap.key" \
  -cmd turn-off
