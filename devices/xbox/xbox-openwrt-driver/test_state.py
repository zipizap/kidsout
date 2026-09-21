#!/usr/bin/env python3
"""getState semantics.

The metric, stated once so the assertions can be derived from it rather than
from whatever the code happens to do (REVIEW.1 F-07's lesson — v1's test passed
for the wrong reason and thereby certified broken parsing as correct):

    Let out(t) / in(t) be the router-side accumulator totals `xbox_out` /
    `xbox_in`: conntrack byte counters of every flow whose original tuple
    has the console as source, folded into monotonic totals (DESIGN.md §4).

        rate_out = (out(now) - out(prev)) / (now - prev) * 60   [bytes/minute]
        rate_in  = (in(now)  - in(prev))  / (now - prev) * 60

        up       rate_out > threshold_out_bytes_per_min          (gameplay)
              OR rate_in  > threshold_in_bytes_per_min           (streaming)
        down     neither
        unknown  rate is not computable: no previous sample, the previous
                 sample is older than max_sample_age_s, the counter went
                 backwards (reboot/flush), the counter table is missing, or
                 the router could not be consulted at all

Both thresholds are strict. threshold_in_bytes_per_min may be absent/null, in
which case only outbound decides (the rule before 2026-09-21). The inbound rule
exists because streaming video is almost pure inbound and read 'down' for a
whole evening; its accepted cost is that a game download in progress also reads
'up' (see the "download" cases below).
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


def fresh_state():
    """An empty driver state dict — no baseline, no cached endpoint."""
    return {}


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
        threshold = dev["state"]["threshold_out_bytes_per_min"]
        threshold_in = dev["state"]["threshold_in_bytes_per_min"]

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

        with s.case("heavy inbound alone counts as 'in use' (streaming)") as c:
            # Inverted 2026-09-21. This used to assert 'down' ("a download is
            # not gameplay"), and that is exactly why YouTube read 'down' all
            # evening: streaming and downloading are indistinguishable by bytes
            # (huge in, trivial out). The inbound rule accepts that a download
            # in progress reads 'up' in exchange for metering video.
            backdate(m, 60)
            mock.state.add_traffic(out=1 * KB, inbound=200 * 1024 * KB)
            word, _ = state_word(m, fw(), dev)
            c.eq(word, "up", "200 MB/min inbound is someone pulling media")
            logged = open(m.LOG_FILE).read().rstrip().splitlines()[-1]
            c.check("by=in" in logged, f"the log names the inbound rule: {logged!r}")

        with s.case("streaming video (calibrated 2026-09-21) -> up via inbound") as c:
            # Measured on the real console watching YouTube over 60 s ticks:
            # out 60-80 KB/min (UNDER the outbound line), in 4.4-18 MB/min.
            backdate(m, 60)
            mock.state.add_traffic(out=70 * KB, inbound=9 * 1024 * KB)
            word, _ = state_word(m, fw(), dev)
            c.eq(word, "up", "70 KB/min out + 9 MB/min in is a video stream")
            verdict, by = m.verdict_for(70 * KB, 9 * 1024 * KB, dev["state"])
            c.eq((verdict, by), ("up", "in"), "and it is the inbound rule that fires")

        with s.case("idle dashboard (calibrated) -> down under both rules") as c:
            # idle / dashboard: 20.8 KB/min out, 23 KB/min in. The inbound
            # floor must sit far above dashboard tile chatter or the console
            # would read 'up' whenever it is merely switched on.
            backdate(m, 60)
            mock.state.add_traffic(out=int(20.8 * KB), inbound=23 * KB)
            word, _ = state_word(m, fw(), dev)
            c.eq(word, "down", "dashboard chatter is not use")
            c.check(threshold_in >= 20 * 23 * KB,
                    f"inbound floor {threshold_in/1024:.0f} KB/min keeps >=20x margin over idle")

        with s.case("inbound threshold boundary is strict") as c:
            v, by = m.verdict_for(0, threshold_in, dev["state"])
            c.eq(v, "down", "exactly at the inbound threshold is not 'up'")
            v, by = m.verdict_for(0, threshold_in + 1, dev["state"])
            c.eq((v, by), ("up", "in"), "one byte over the inbound threshold is 'up'")
            v, by = m.verdict_for(threshold + 1, threshold_in + 1, dev["state"])
            c.eq(by, "out", "when both fire, outbound (gameplay) is the reported reason")

        with s.case("no inbound threshold -> legacy outbound-only rule") as c:
            legacy = dict(dev["state"])
            del legacy["threshold_in_bytes_per_min"]
            c.eq(m.verdict_for(1 * KB, 200 * 1024 * KB, legacy), ("down", None),
                 "without the key, inbound is ignored")
            legacy["threshold_in_bytes_per_min"] = None
            c.eq(m.verdict_for(1 * KB, 200 * 1024 * KB, legacy), ("down", None),
                 "null disables it too")
            c.eq(m.verdict_for(threshold + 1, 0, legacy), ("up", "out"),
                 "outbound still decides")

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
            c.check("threshold_out=" in logged and "threshold_in=" in logged,
                    "and both thresholds it was compared against")

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

        # ---- REVIEW.2 G-04: monotonicity is a guarantee, not an assumption - #
        with s.case("counters going backwards -> unknown, then re-baseline (G-04)") as c:
            # The helper accumulates router-side precisely so this cannot
            # happen: a naive conntrack sum DOES go backwards when flows expire
            # (measured at -510 KB/min during real gameplay). If the guarantee
            # is ever violated the driver must say 'unknown', not compute a
            # negative rate and call it 'down'.
            st = fresh_state()
            mock.state.reset_counters()
            mock.state.counters_installed = True
            mock.state.counter_addrs = ["192.168.2.172"]
            mock.state.add_traffic(out=5_000_000)
            with captured():
                m.sample_rate(fw(), dev, st, now=1000.0)      # baseline
            mock.state.reset_counters()                        # totals collapse
            mock.state.add_traffic(out=1_000)
            rate_out, _rin, reason = m.sample_rate(fw(), dev, st, now=1060.0)
            c.check(rate_out is None, "no rate computed from a decrease")
            c.check(reason is not None and "decreas" in reason.lower(),
                    f"reason names the decrease: {reason!r}")
            # and the very next tick works again off the new baseline
            mock.state.add_traffic(out=4_000_000)
            rate2, _i, reason2 = m.sample_rate(fw(), dev, st, now=1120.0)
            c.check(reason2 is None, f"re-baselined cleanly: {reason2!r}")
            c.check(rate2 is not None and rate2 > 0, f"positive rate again: {rate2}")

        with s.case("the v3 key/value counters shape is parsed (G-01)") as c:
            mock.state.counters_installed = True
            mock.state.counter_addrs = ["192.168.2.172"]
            mock.state.reset_counters()
            mock.state.add_traffic(out=12345, inbound=678)
            vals = m.read_counters(fw())
            c.check(vals.get("xbox_out") == 12345, f"xbox_out parsed: {vals}")
            c.check(vals.get("xbox_in") == 678, f"xbox_in parsed: {vals}")

        with s.case("a download reads 'up' via the inbound rule, not outbound (calibrated)") as c:
            # Measured on the real console: a 12 MB/min download produced only
            # 93.7 KB/min OUTBOUND, while gameplay produced 289.5 KB/min. The
            # outbound threshold sits between them, and that margin is still
            # asserted here. Since 2026-09-21 the download nevertheless reads
            # 'up' -- through the INBOUND rule -- because it is byte-for-byte
            # indistinguishable from streaming video. Accepted trade-off.
            st = fresh_state()
            mock.state.reset_counters()
            mock.state.counters_installed = True
            mock.state.counter_addrs = ["192.168.2.172"]
            with captured():
                m.sample_rate(fw(), dev, st, now=2000.0)
            mock.state.add_traffic(out=int(93.7 * 1024), inbound=12 * 1024 * 1024)
            rate_out, rate_in, reason = m.sample_rate(fw(), dev, st, now=2060.0)
            c.check(reason is None, f"rate computed: {reason!r}")
            thr = dev["state"]["threshold_out_bytes_per_min"]
            c.check(rate_out < thr,
                    f"download outbound {rate_out/1024:.1f} KB/min stays under "
                    f"{thr/1024:.0f} KB/min (outbound alone would say down)")
            c.check(rate_in > thr * 10, "…even though inbound is enormous")
            c.eq(m.verdict_for(rate_out, rate_in, dev["state"]), ("up", "in"),
                 "so the verdict is 'up', and only because of inbound")

    return s.report()


if __name__ == "__main__":
    sys.exit(main())
