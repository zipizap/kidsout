#!/usr/bin/env python3
"""getState semantics.

The metric, stated once so the assertions can be derived from it rather than
from whatever the code happens to do (REVIEW.1 F-07's lesson — v1's test passed
for the wrong reason and thereby certified broken parsing as correct):

    Let out(t) be the byte count of the nft counter `xbox_out`, which counts
    every forwarded frame whose ethernet source is the console's MAC.

        rate = (out(now) - out(prev)) / (now - prev) * 60      [bytes/minute]

        up       rate >  threshold_bytes_per_min
        down     rate <= threshold_bytes_per_min
        unknown  rate is not computable: no previous sample, the previous
                 sample is older than max_sample_age_s, the counter went
                 backwards (reboot/flush), the counter table is missing, or
                 the router could not be consulted at all

Only the OUTBOUND counter decides. Inbound is recorded for diagnostics but is
IPv4-only — the L2 destination of a forwarded frame is the router, not the
console, so an inbound counter cannot be keyed on the MAC.
"""
import json
import os
import sys
import time

from mock_router import MockRouter
from testlib import Suite, captured, device, load_driver, out_of, tmpdir

KB = 1024


def backdate(m, seconds):
    """Pretend the stored sample was taken `seconds` ago."""
    st = m.load_state()
    st["counters_ts"] = time.time() - seconds
    m.save_state(st)


def state_word(m, fw, dev):
    with captured() as buf:
        rc = m.cmd_state(fw, dev, None)
    return out_of(buf), rc


def main():
    s = Suite("state logic")

    with tmpdir() as tmp, MockRouter() as mock:
        m = load_driver(tmp)
        dev = device()
        fw = lambda: m.Router(mock.config(), dev)
        threshold = dev["state"]["threshold_bytes_per_min"]

        with captured():
            m.cmd_install(fw(), dev, None)

        # ------------------------------------------------------------------ #
        with s.case("first run has no baseline -> unknown") as c:
            word, rc = state_word(m, fw(), dev)
            c.eq(word, "unknown", "no previous sample")
            c.eq(rc, 0, "benign unknown is not an error exit")
            c.check(m.load_state().get("counters_ts"), "but a baseline was stored")

        with s.case("console actively used -> up") as c:
            backdate(m, 60)
            mock.state.add_traffic(out=2 * 1024 * KB, inbound=20 * 1024 * KB)
            word, rc = state_word(m, fw(), dev)
            c.eq(word, "up", "2 MB/min outbound is well above the threshold")
            c.eq(rc, 0, "exit 0")

        with s.case("console idle (standby presence traffic) -> down") as c:
            backdate(m, 60)
            mock.state.add_traffic(out=8 * KB, inbound=12 * KB)
            word, _ = state_word(m, fw(), dev)
            c.eq(word, "down", "8 KB/min is standby chatter, not play")

        with s.case("console blocked, no traffic at all -> down") as c:
            backdate(m, 60)
            word, _ = state_word(m, fw(), dev)
            c.eq(word, "down", "zero bytes")

        with s.case("threshold boundary is strict") as c:
            # Asserted against sample_rate with an explicit clock: going through
            # cmd_state would add a few milliseconds of real elapsed time, which
            # is enough to move a one-byte margin across the line. The boundary
            # is a property of the metric, so test it where the metric lives.
            base = {"counters": {"xbox_out": 0, "xbox_in": 0}, "counters_ts": 1000.0}
            mock.state.reset_counters()
            mock.state.add_traffic(out=threshold)
            st = dict(base)
            rate_out, _, reason = m.sample_rate(fw(), dev, st, now=1060.0)
            c.check(reason is None, f"a rate was computable: {reason}")
            c.eq(rate_out, float(threshold), "exactly threshold bytes in 60s")
            c.check(not (rate_out > threshold), "exactly at the threshold is not 'up'")

            mock.state.reset_counters()
            mock.state.add_traffic(out=threshold + 1)
            st = dict(base)
            rate_out, _, _ = m.sample_rate(fw(), dev, st, now=1060.0)
            c.check(rate_out > threshold, "one byte over the threshold is 'up'")

        with s.case("the rate is per minute, not per sample") as c:
            state_word(m, fw(), dev)      # re-baseline: the case above reset the counters
            # half the interval, half the bytes -> same rate
            backdate(m, 30)
            mock.state.add_traffic(out=(threshold // 2) + 1000)
            word, _ = state_word(m, fw(), dev)
            c.eq(word, "up", "threshold/2 bytes in 30s is above threshold/min")

        with s.case("a big download alone does not count as 'in use'") as c:
            backdate(m, 60)
            mock.state.add_traffic(out=1 * KB, inbound=200 * 1024 * KB)
            word, _ = state_word(m, fw(), dev)
            c.eq(word, "down", "200 MB/min inbound with no outbound is not gameplay")

        # ------------------------------------------------------------------ #
        with s.case("counter reset (router reboot) -> unknown, then recovers") as c:
            backdate(m, 60)
            mock.state.reset_counters()
            word, _ = state_word(m, fw(), dev)
            c.eq(word, "unknown", "counters went backwards")
            backdate(m, 60)
            mock.state.add_traffic(out=5 * KB)
            word, _ = state_word(m, fw(), dev)
            c.eq(word, "down", "the next tick has a fresh baseline and answers normally")

        with s.case("stale baseline is not averaged over the gap -> unknown") as c:
            backdate(m, dev["state"]["max_sample_age_s"] + 60)
            mock.state.add_traffic(out=50 * 1024 * KB)
            word, _ = state_word(m, fw(), dev)
            c.eq(word, "unknown", "an hour of bytes must not be divided into one minute")

        with s.case("missing counter table -> unknown and self-heals") as c:
            mock.state.counters_installed = False
            word, _ = state_word(m, fw(), dev)
            c.eq(word, "unknown", "cannot measure")
            c.check(mock.state.counters_installed, "the table was recreated for the next tick")

        # ------------------------------------------------------------------ #
        with s.case("stdout is always exactly one word") as c:
            for setup in (lambda: None,
                          lambda: mock.state.reset_counters(),
                          lambda: setattr(mock.state, "fail", "denied")):
                setup()
                with captured() as buf:
                    m.cmd_state(fw(), dev, None)
                words = out_of(buf).split()
                c.eq(len(words), 1, f"one word, got {words!r}")
                c.check(words[0] in ("up", "down", "unknown"),
                        f"a contract word, got {words[0]!r}")
            mock.state.fail = None

        with s.case("diagnostics land in the log, not on stdout") as c:
            backdate(m, 60)
            mock.state.add_traffic(out=3 * 1024 * KB)
            with captured() as buf:
                m.cmd_state(fw(), dev, None)
            c.eq(out_of(buf), "up", "stdout stays one word")
            logged = open(m.LOG_FILE).read()
            c.check("state=up" in logged, "the verdict is logged")
            c.check("KB/min" in logged, "with the measurement behind it")
            c.check("threshold=" in logged, "and the threshold it was compared against")

        with s.case("the rule state is audited periodically (F-10)") as c:
            st = m.load_state()
            st["rule_audit_ts"] = 0
            m.save_state(st)
            with captured():
                m.cmd_block(fw(), dev, None)
            backdate(m, 60)
            with captured():
                m.cmd_state(fw(), dev, None)
            logged = open(m.LOG_FILE).read()
            c.check("audit:" in logged, "an audit line was written")
            c.check("internet BLOCKED" in logged,
                    "recording what the router is actually enforcing")

    return s.report()


if __name__ == "__main__":
    sys.exit(main())
