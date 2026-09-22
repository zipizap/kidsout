#!/usr/bin/env python3
"""Block/allow polarity and rule-set semantics.

This is the test whose absence let REVIEW.1 F-01 ship: v1's block.sh set
enabled=0 on three REJECT rules, which *removes* them from the ruleset, so
"block" gave the device internet and "allow" took it away. Nothing in the
suite asserted what the router ended up with, so the inversion was invisible.

The rule here is simple and worth stating: assert the state of the router, not
the return value of the function that was supposed to change it.
"""
import sys

from mock_router import MockRouter
from testlib import TEST_ID, Suite, captured, device, load_driver, out_of, tmpdir

# Fixture MACs: obviously fake, one per interface.
ETH = "02:00:00:00:00:01"
WIFI = "02:00:00:00:00:02"


def main():
    s = Suite("block semantics")

    with tmpdir() as tmp, MockRouter(device_id=TEST_ID) as mock:
        m = load_driver(tmp)
        n = m.Names(TEST_ID)
        dev = device()
        fw = lambda: m.Router(mock.config(), dev)

        # Another device's rule, present from the start. Nothing this device
        # does may ever touch it -- including a device whose id is a PREFIX
        # of the other's, which a bare startswith() would have claimed.
        sibling = f"kidsout_{TEST_ID}2_out"
        other = "kidsout_other_out"
        for foreign in (sibling, other):
            mock.state.firewall[foreign] = {".type": "rule", ".name": foreign,
                                            "target": "REJECT", "enabled": "1",
                                            "src_mac": "02:00:00:00:00:99"}

        def foreign_untouched(c, when):
            for foreign in (sibling, other):
                r = mock.state.firewall.get(foreign)
                c.check(r is not None, f"{when}: {foreign} still exists")
                c.eq((r or {}).get("enabled"), "1", f"{when}: {foreign} still enabled")

        # ------------------------------------------------------------------ #
        with s.case("install leaves the device ONLINE") as c:
            with captured():
                m.cmd_install(fw(), dev, None)
            secs = mock.state.sections()
            c.eq(sorted(secs), [n.rule], "exactly one rule installed, named after the id")
            r = secs[n.rule]
            c.eq(r["target"], "REJECT", "rule rejects")
            c.eq(r["enabled"], "0", "REJECT is present but DISABLED -> internet allowed")
            c.eq(r["src_mac"], "aa:bb:cc:dd:ee:ff", "rule is keyed on the MAC, not the IP")
            c.check("src_ip" not in r, "rule does not key on the IPv4 address (F-04)")
            c.eq(r["dest"], "wan", "forwarded traffic to wan")
            c.check(TEST_ID in r["name"], f"the LuCI-visible name carries the id: {r['name']}")
            foreign_untouched(c, "after install")

        with s.case("block ENABLES the REJECT rules") as c:
            with captured():
                m.cmd_block(fw(), dev, None)
            for name, r in mock.state.sections().items():
                c.eq(r["target"], "REJECT", f"{name} target")
                c.eq(r["enabled"], "1", f"{name} REJECT in force -> internet blocked")

        with s.case("block flushes the device's live flows (F-05)") as c:
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
            foreign_untouched(c, "after block+allow")

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
            for legacy in (f"{n.prefix}_fwd_out", f"{n.prefix}_fwd_in",
                           f"{n.prefix}_input_out"):
                mock.state.firewall[legacy] = {".type": "rule", ".name": legacy,
                                               "target": "REJECT", "enabled": "1"}
            with captured() as buf:
                m.cmd_install(fw(), dev, None)
            secs = mock.state.sections()
            c.eq(sorted(secs), [n.rule], "obsolete sections removed")
            c.check("removed=" in buf.getvalue(), "and the removal is reported")
            foreign_untouched(c, "after migration")

        with s.case("uninstall removes ONLY this device's sections") as c:
            with captured():
                m.cmd_uninstall(fw(), dev, None)
            c.eq(mock.state.sections(), {}, "own sections gone")
            foreign_untouched(c, "after uninstall")
            with captured():
                m.cmd_install(fw(), dev, None)       # restore for the cases below

        with s.case("toggling without an install is refused, not silently ignored") as c:
            for name in list(mock.state.sections()):
                del mock.state.firewall[name]
            try:
                with captured():
                    m.cmd_block(fw(), dev, None)
                c.check(False, "should have exited")
            except SystemExit as e:
                c.check("install" in str(e), f"points at install: {e}")
            foreign_untouched(c, "a sibling rule does not count as 'installed'")

        with s.case("install without a MAC is refused (F-04)") as c:
            nomac = device(mac=None)
            nomac["interfaces"] = []
            try:
                with captured():
                    m.cmd_install(fw(), nomac, None)
                c.check(False, "should have exited")
            except SystemExit as e:
                c.check("discover" in str(e), f"points at discover: {e}")

        # ---- REVIEW.2 G-02: the device has two interfaces ----------------- #
        with s.case("one rule names EVERY device MAC (G-02)") as c:
            both = device(macs=[(ETH, "192.168.2.172", "ethernet"),
                                (WIFI, "192.168.2.169", "wifi")])
            for name in list(mock.state.sections()):
                del mock.state.firewall[name]
            with captured():
                m.cmd_install(fw(), both, None)
            secs = mock.state.sections()
            c.check(len(secs) == 1, f"still exactly one section: {list(secs)}")
            sec = next(iter(secs.values()))
            got = sec.get("src_mac")
            flat = got if isinstance(got, list) else [got]
            flat = [x.lower() for x in flat]
            c.check(ETH in flat, f"ethernet MAC present: {got}")
            c.check(WIFI in flat, f"wifi MAC present: {got}")
            c.check("src_ip" not in sec, "still keyed on MAC, not IP")

        with s.case("a block covers the device on either interface (G-02)") as c:
            both = device(macs=[(ETH, "192.168.2.172", "ethernet"),
                                (WIFI, "192.168.2.169", "wifi")])
            # router sees only the WiFi interface right now
            mock.state.leases = (f"1690000000 {WIFI} 192.168.2.169 {TEST_ID.upper()} 01:x\n")
            mock.state.neigh = (f"192.168.2.169 dev br-lan lladdr {WIFI} REACHABLE\n"
                                "192.168.2.172 dev br-lan  FAILED\n")
            mock.state.flushed.clear()
            with captured():
                m.cmd_block(fw(), both, None)
            sec = next(iter(mock.state.sections().values()))
            c.check(str(sec.get("enabled")) == "1", "rule enabled")
            flushed = mock.state.flushed[-1] if mock.state.flushed else []
            c.check("192.168.2.169" in flushed,
                    f"flushed the LIVE wifi address, not just the static one: {flushed}")

        # ---- G-02 again, in the diagnostics ------------------------------- #
        with s.case("`check` finds the device on EITHER interface (G-02)") as c:
            both = device(macs=[(ETH, "192.168.2.172", "ethernet"),
                                (WIFI, "192.168.2.169", "wifi")])
            mock.state.neigh = (f"192.168.2.169 dev br-lan lladdr {WIFI} REACHABLE\n")
            with captured() as buf:
                rc = m.cmd_check(fw(), both, None)
            text = out_of(buf)
            c.eq(rc, 0, "device on WiFi is PRESENT, not 'powered off'")
            c.check(WIFI in text, f"reported the live interface: {text}")

        with s.case("`check` reports absent only when NO interface is seen") as c:
            both = device(macs=[(ETH, "192.168.2.172", "ethernet"),
                                (WIFI, "192.168.2.169", "wifi")])
            mock.state.neigh = ("192.168.2.50 dev br-lan lladdr 11:22:33:44:55:66 STALE\n")
            with captured() as buf:
                rc = m.cmd_check(fw(), both, None)
            text = out_of(buf)
            c.eq(rc, 1, "genuinely absent -> exit 1")
            c.check(ETH in text and WIFI in text, f"names every MAC it looked for: {text}")

        with s.case("`status` lists every configured interface") as c:
            both = device(macs=[(ETH, "192.168.2.172", "ethernet"),
                                (WIFI, "192.168.2.169", "wifi")])
            mock.state.neigh = (f"192.168.2.169 dev br-lan lladdr {WIFI} REACHABLE\n")
            with captured() as buf:
                rc = m.cmd_status(fw(), both, None)
            text = out_of(buf)
            c.eq(rc, 0, "status ran")
            c.check(ETH in text, f"ethernet interface shown: {text}")
            c.check(WIFI in text, f"wifi interface shown: {text}")
            c.check(TEST_ID in text.splitlines()[0], "the first line names the device")

        # ---- REVIEW.2 G-03: a failed flush must not report success -------- #
        with s.case("block reports FAILURE when the conntrack flush fails (G-03)") as c:
            mock.state.fail = "noconntrack"
            try:
                with captured() as out:
                    rc = m.cmd_block(fw(), dev, None)
                c.check(rc == 1, f"cmd_block returned non-zero, got {rc!r}")
                c.check("NOT flushed" in out.getvalue(),
                        "says plainly that it did not flush")
            finally:
                mock.state.fail = None

        with s.case("the rule is still enabled even when the flush fails") as c:
            # Half a block is better than none: new connections must still be
            # refused even though an in-progress session may survive.
            sec = next(iter(mock.state.sections().values()))
            c.check(str(sec.get("enabled")) == "1", "REJECT left in force")

        # ---- discover uses the configured hostname hints ------------------ #
        with s.case("`discover` matches the device by hostname hint, not by id") as c:
            hinted = device(mac="02:00:00:00:00:aa", ipv4="192.168.2.10",
                            discover={"hostname_hints": ["Switch-Living"]})
            hinted["interfaces"] = []
            mock.state.leases = ("1690000000 02:00:00:00:00:bb 192.168.2.11 switch-livingroom 01:x\n"
                                 "1690000000 02:00:00:00:00:cc 192.168.2.12 laptop 01:y\n")
            with captured() as buf:
                rc = m.cmd_discover(fw(), hinted, type("A", (), {"write": False})())
            text = out_of(buf)
            c.eq(rc, 0, "found something")
            c.check("02:00:00:00:00:bb" in text, f"the hinted host is listed: {text}")
            c.check("02:00:00:00:00:cc" not in text, "the unrelated host is not")

    return s.report()


if __name__ == "__main__":
    sys.exit(main())
