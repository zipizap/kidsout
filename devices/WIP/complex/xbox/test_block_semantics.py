#!/usr/bin/env python3
"""Block/allow polarity and rule-set semantics.

This is the test whose absence let REVIEW.1 F-01 ship: v1's block.sh set
enabled=0 on three REJECT rules, which *removes* them from the ruleset, so
"block" gave the console internet and "allow" took it away. Nothing in the
suite asserted what the router ended up with, so the inversion was invisible.

The rule here is simple and worth stating: assert the state of the router, not
the return value of the function that was supposed to change it.
"""
import sys

from mock_router import MockRouter
from testlib import Suite, captured, device, load_driver, tmpdir


def main():
    s = Suite("block semantics")

    with tmpdir() as tmp, MockRouter() as mock:
        m = load_driver(tmp)
        dev = device()
        fw = lambda: m.Router(mock.config(), dev)

        # ------------------------------------------------------------------ #
        with s.case("install leaves the console ONLINE") as c:
            with captured():
                m.cmd_install(fw(), dev, None)
            secs = mock.state.sections()
            c.eq(sorted(secs), ["kidsout_xbox_out"], "exactly one rule installed")
            r = secs["kidsout_xbox_out"]
            c.eq(r["target"], "REJECT", "rule rejects")
            c.eq(r["enabled"], "0", "REJECT is present but DISABLED -> internet allowed")
            c.eq(r["src_mac"], "aa:bb:cc:dd:ee:ff", "rule is keyed on the MAC, not the IP")
            c.check("src_ip" not in r, "rule does not key on the IPv4 address (F-04)")
            c.eq(r["dest"], "wan", "forwarded traffic to wan")

        with s.case("block ENABLES the REJECT rules") as c:
            with captured():
                m.cmd_block(fw(), dev, None)
            for name, r in mock.state.sections().items():
                c.eq(r["target"], "REJECT", f"{name} target")
                c.eq(r["enabled"], "1", f"{name} REJECT in force -> internet blocked")

        with s.case("block flushes the console's live flows (F-05)") as c:
            c.check(mock.state.flushed, "conntrack flush was requested")
            flushed = mock.state.flushed[-1]
            c.check("192.168.2.172" in flushed, "IPv4 flushed")
            c.check(any(":" in a for a in flushed),
                    f"IPv6 address discovered from the neighbour table and flushed: {flushed}")

        with s.case("allow DISABLES the REJECT rules") as c:
            with captured():
                m.cmd_allow(fw(), dev, None)
            for name, r in mock.state.sections().items():
                c.eq(r["enabled"], "0", f"{name} REJECT absent -> internet allowed")

        # ------------------------------------------------------------------ #
        with s.case("re-blocking writes nothing (F-09)") as c:
            with captured():
                m.cmd_block(fw(), dev, None)
            mock.state.calls.clear()
            with captured() as buf:
                m.cmd_block(fw(), dev, None)
            writes = mock.state.write_calls()
            c.eq(writes, [], "second block issues no uci set/commit/apply")
            c.check("already" in buf.getvalue(), "and says so")
            c.check(("uci", "get") in mock.state.calls, "it does still read the rule state")

        with s.case("re-allowing writes nothing (F-09)") as c:
            with captured():
                m.cmd_allow(fw(), dev, None)
            mock.state.calls.clear()
            with captured():
                m.cmd_allow(fw(), dev, None)
            c.eq(mock.state.write_calls(), [], "second allow issues no writes")

        # ------------------------------------------------------------------ #
        with s.case("install migrates away from the v1 rule set (F-08)") as c:
            for legacy in ("kidsout_xbox_fwd_out", "kidsout_xbox_fwd_in",
                           "kidsout_xbox_input_out"):
                mock.state.firewall[legacy] = {".type": "rule", ".name": legacy,
                                               "target": "REJECT", "enabled": "1"}
            with captured() as buf:
                m.cmd_install(fw(), dev, None)
            secs = mock.state.sections()
            c.eq(sorted(secs), ["kidsout_xbox_out"], "obsolete sections removed")
            c.check("removed=" in buf.getvalue(), "and the removal is reported")

        with s.case("toggling without an install is refused, not silently ignored") as c:
            for name in list(mock.state.firewall):
                if name.startswith("kidsout_xbox"):
                    del mock.state.firewall[name]
            try:
                with captured():
                    m.cmd_block(fw(), dev, None)
                c.check(False, "should have exited")
            except SystemExit as e:
                c.check("install" in str(e), f"points at install: {e}")

        with s.case("install without a MAC is refused (F-04)") as c:
            nomac = device(mac=None)
            try:
                with captured():
                    m.cmd_install(fw(), nomac, None)
                c.check(False, "should have exited")
            except SystemExit as e:
                c.check("discover" in str(e), f"points at discover: {e}")

    return s.report()


if __name__ == "__main__":
    sys.exit(main())
