#!/usr/bin/env python3
"""The upstream kidsout script contract, exercised through the real .sh files.

Everything else in the suite calls python functions. This one runs the three
scripts kidsout actually runs, as subprocesses, from a different working
directory, against the mock — which is the only way to catch a broken shebang,
a missing exec bit, a lost `cd "$(dirname "$0")"`, or stdout pollution.

Contract, from upstream's engine.go and state.go:
  - devices/<name>/ contains getState.sh, block.sh, unblock.sh, all executable
  - they are run with cmd.Dir unset, so they must self-locate
  - getState.sh prints one word; anything that is not up/down is unknown
  - non-zero exit is also unknown
  - all three run under a 10 s context timeout
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import time

from mock_router import MockRouter
from testlib import Suite, device, tmpdir

HERE = os.path.dirname(os.path.abspath(__file__))     # the driver subdirectory
DEVICE_DIR = os.path.dirname(HERE)                    # what kidsout scans
DRIVER_SUBDIR = os.path.basename(HERE)                # "xbox-openwrt-driver"
SCRIPTS = ("getState.sh", "block.sh", "unblock.sh")
DRIVER_FILES = ("allow.sh", "install.sh", "status.sh", "xbox.py")


def deploy(tmp, mock):
    """A copy of the device directory, pointed at the mock.

    Reproduces the shipped two-level layout rather than flattening it: the
    three contract scripts at the top, the driver and everything it owns one
    level down. The split is the thing under test — a contract script that
    still cd'd to its own directory would find no xbox.py here.
    """
    d = os.path.join(tmp, "xbox")
    drv = os.path.join(d, DRIVER_SUBDIR)
    os.makedirs(drv)
    for name in SCRIPTS:
        shutil.copy2(os.path.join(DEVICE_DIR, name), os.path.join(d, name))
    for name in DRIVER_FILES:
        shutil.copy2(os.path.join(HERE, name), os.path.join(drv, name))
    with open(os.path.join(drv, "device.json"), "w") as f:
        json.dump(device(), f)
    cfg = os.path.join(drv, "config.json")
    with open(cfg, "w") as f:
        json.dump(mock.config(), f)
    os.chmod(cfg, 0o644)                 # deliberately loose: S-02 should fix it
    return d, drv


def run(script, cwd, dirname):
    """Run like upstream does: absolute path, cmd.Dir unset, 10 s ceiling."""
    t0 = time.time()
    p = subprocess.run([os.path.join(dirname, script)], cwd=cwd,
                       capture_output=True, text=True, timeout=10)
    return p, time.time() - t0


def main():
    s = Suite("upstream script contract")

    with tmpdir() as tmp, MockRouter() as mock:
        d, drv = deploy(tmp, mock)

        with s.case("the three contract scripts exist and are executable") as c:
            for name in SCRIPTS:
                p = os.path.join(d, name)
                c.check(os.path.isfile(p), f"{name} exists")
                c.check(os.stat(p).st_mode & stat.S_IXUSR, f"{name} has the exec bit")

        with s.case("install from an unrelated working directory") as c:
            p, _ = run("install.sh", "/", drv)
            c.eq(p.returncode, 0, f"exit 0 (stderr: {p.stderr.strip()[:200]})")
            c.check("ALLOWED" in p.stdout, "installs in the allowed state")
            secs = mock.state.sections()
            c.eq(sorted(secs), ["kidsout_xbox_out"], "the rule is on the router")
            c.eq(secs["kidsout_xbox_out"]["enabled"], "0", "console still online")

        with s.case("config.json permissions are tightened (S-02)") as c:
            mode = os.stat(os.path.join(drv, "config.json")).st_mode & 0o777
            c.eq(oct(mode), oct(0o600), "the credential file is owner-only")

        with s.case("getState.sh prints one contract word") as c:
            p, elapsed = run("getState.sh", "/", d)
            out = p.stdout.strip()
            c.eq(len(out.split()), 1, f"exactly one word, got {p.stdout!r}")
            c.check(out in ("up", "down", "unknown"), f"a contract word: {out!r}")
            c.check(elapsed < 10, f"inside the 10 s timeout ({elapsed:.2f}s)")

        with s.case("getState.sh reports 'up' when the console is busy") as c:
            run("getState.sh", "/", d)                    # baseline
            st = os.path.join(drv, ".state.json")
            with open(st) as f:
                data = json.load(f)
            data["counters_ts"] = time.time() - 60
            with open(st, "w") as f:
                json.dump(data, f)
            mock.state.add_traffic(out=4 * 1024 * 1024)
            p, elapsed = run("getState.sh", "/", d)
            c.eq(p.stdout.strip(), "up", "4 MB in the last minute")
            c.eq(p.returncode, 0, "exit 0")
            c.check(elapsed < 3, f"and it is quick ({elapsed:.2f}s)")

        with s.case("block.sh turns the internet off") as c:
            p, elapsed = run("block.sh", "/", d)
            c.eq(p.returncode, 0, f"exit 0 (stderr: {p.stderr.strip()[:200]})")
            r = mock.state.sections()["kidsout_xbox_out"]
            c.eq(r["target"], "REJECT", "REJECT rule")
            c.eq(r["enabled"], "1", "and it is ENABLED — internet blocked")
            c.check(mock.state.flushed, "live flows were flushed")
            c.check(elapsed < 10, f"inside the timeout ({elapsed:.2f}s)")

        with s.case("block.sh is cheap to repeat (kidsout calls it every tick)") as c:
            mock.state.calls.clear()
            p, _ = run("block.sh", "/", d)
            c.eq(p.returncode, 0, "exit 0")
            c.eq([x for x in mock.state.write_calls() if x[1] in ("set", "commit", "apply")],
                 [], "no uci write, no commit, no firewall reload")

        with s.case("unblock.sh turns the internet back on") as c:
            p, _ = run("unblock.sh", "/", d)
            c.eq(p.returncode, 0, f"exit 0 (stderr: {p.stderr.strip()[:200]})")
            c.eq(mock.state.sections()["kidsout_xbox_out"]["enabled"], "0",
                 "REJECT disabled — internet allowed")

        with s.case("unblock.sh is safe to over-call (the manual escape hatch)") as c:
            p, _ = run("unblock.sh", "/", d)
            c.eq(p.returncode, 0, "exit 0 when already allowed")

        with s.case("a dead router does not hang the tick") as c:
            mock.state.fail = "denied"
            p, elapsed = run("getState.sh", "/", d)
            c.eq(p.stdout.strip(), "unknown", "unknown, not down")
            c.check(elapsed < 5, f"and fast ({elapsed:.2f}s) — a slow device driver "
                                 f"stalls kidsout's whole evaluation tick")
            mock.state.fail = None

        with s.case("a missing config.json is reported as such, not as the router's fault") as c:
            cfg = os.path.join(drv, "config.json")
            logf = os.path.join(drv, "xbox.log")
            mark = os.path.getsize(logf)
            os.rename(cfg, cfg + ".away")
            try:
                p, elapsed = run("getState.sh", "/", d)
            finally:
                os.rename(cfg + ".away", cfg)
            c.eq(p.stdout.strip(), "unknown", "still exactly one contract word")
            c.check(elapsed < 5, f"and it fails locally, without a round trip ({elapsed:.2f}s)")
            with open(logf) as f:
                f.seek(mark)
                tail = f.read()
            c.check("config.json" in tail, "the log names the file that is missing")
            c.check("permission denied" not in tail,
                    "and does not blame rpcd for a fault on this machine")

        with s.case("the driver keeps its own log") as c:
            logf = os.path.join(drv, "xbox.log")
            c.check(os.path.exists(logf), "xbox.log was created")
            body = open(logf).read()
            c.check("state=" in body, "state decisions are recorded")
            c.check("internet BLOCKED" in body, "so are enforcement changes")

    return s.report()


if __name__ == "__main__":
    sys.exit(main())
