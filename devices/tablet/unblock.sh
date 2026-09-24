#!/usr/bin/env bash
# kidsout unblock.sh — upstream kidsout (Go web app) script contract.
#
# koTabletctl only exposes a remote lock, not a remote unlock (the daemon
# doesn't have one either) — the tablet unlocks the normal way, by whoever
# holds it entering the passcode. So there is nothing for this script to do.
exit 0
