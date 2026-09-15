#!/usr/bin/env bash
# kidsout xbox: getState.sh — upstream kidsout (Go web app) script contract.
#
# Prints ONE word on stdout: up | down | unknown.
#   up       the console's outbound byte rate is above the threshold in
#            device.json — someone is actually using it
#   down     the router answered and the console is idle (or blocked)
#   unknown  the router could not be consulted, or there is no usable baseline
#            (first run after a restart, a stale sample, a counter reset)
#
# Why bytes and not connections: the console keeps Xbox Live connections alive
# in Instant-On standby, so counting flows reports 'up' around the clock and
# burns the whole daily allowance while nobody is playing. See REVIEW.1 F-06.
#
# Runtime: bounded by state_deadline in config.json (default 7s), inside
# kidsout's 10s limit. Typical healthy run is well under a second; the driver
# returns a truthful 'unknown' rather than being SIGKILLed.
#
# stderr is NOT the diagnostic channel: upstream runs this with Go's
# cmd.Output(), which discards stderr on success. Detail goes to xbox.log.
set -uo pipefail
cd "$(dirname "$0")"
exec python3 xbox.py state
