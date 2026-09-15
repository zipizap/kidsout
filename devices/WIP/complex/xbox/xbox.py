#!/usr/bin/env python3
"""
kidsout device driver: Xbox (xbox.infracon.ovh -> 192.168.2.172)

Controls the console through an OpenWrt router's ubus-over-HTTP JSON-RPC API
(uhttpd-mod-ubus + rpcd), using a dedicated, ACL-scoped rpcd login and a
fixed-verb helper installed on the router by router_bootstrap.sh.

Enforcement model
-----------------
One firewall rule, `kidsout_xbox_out`: REJECT everything forwarded from the
console's MAC address to the wan zone. Matching on the MAC rather than the
IPv4 address covers IPv6 as well, and survives the console's address changing.
`enabled` is the toggle:

    enabled = "1"  ->  REJECT is in force  ->  internet BLOCKED
    enabled = "0"  ->  REJECT is absent    ->  internet ALLOWED

Blocking also flushes the console's conntrack entries, because OpenWrt accepts
established flows before any user rule is evaluated — without the flush, a game
already in progress would keep running for days.

State model
-----------
`state` reports whether the console is *actually being used*, measured as the
byte rate through an nft counter keyed on its MAC. Counting flows instead would
report Instant-On standby as "in use" and silently burn the day's allowance.

Commands:
    probe        no-credential health check: which endpoint answers
    selftest     with credentials: verify every capability the driver needs
    status       console presence, router state, rule state, traffic
    state        one word for kidsout: up | down | unknown  (contract)
    block        internet OFF  (enable the REJECT rule + flush live flows)
    allow        internet ON   (disable the REJECT rule)
    install      create the firewall rule and the nft counters (idempotent)
    uninstall    remove both (destructive)
    discover     find the console's MAC on the router; --write updates device.json
    check        is the console present on the LAN right now
    counters     current byte counters and the rate since the last sample
    calibrate    sample the byte rate over time (for tuning the up/down threshold)
    pin          record the router's TLS certificate fingerprint in device.json

Examples:
    ./xbox.py selftest
    ./xbox.py block            # kid time over
    ./xbox.py allow            # play time
"""

import argparse
import http.client
import json
import os
import re
import socket
import ssl
import sys
import time
import hashlib
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DEVICE_FILE = os.path.join(HERE, "device.json")
CONFIG_FILE = os.path.join(HERE, "config.json")
STATE_FILE = os.path.join(HERE, ".state.json")
LOG_FILE = os.path.join(HERE, "xbox.log")
LOG_MAX_BYTES = 256 * 1024

HELPER = "/usr/libexec/kidsout-xbox"

# uci `enabled` values, named after what they mean rather than what they are.
# Getting these two round the wrong way is REVIEW.1 F-01, the defect that made
# v1 do the exact opposite of its purpose.
ENABLED_WHEN_BLOCKED = "1"
ENABLED_WHEN_ALLOWED = "0"


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #
class UbusError(Exception):
    """The router answered, but refused or failed the request."""


class RouterUnreachable(Exception):
    """The router could not be consulted at all — state is genuinely unknown."""


class Expired(RouterUnreachable):
    """Ran out of wall-clock budget before the router answered."""


class PinMismatch(RouterUnreachable):
    """The router's TLS certificate does not match the pinned fingerprint."""


# --------------------------------------------------------------------------- #
# logging
# --------------------------------------------------------------------------- #
def log(msg):
    """Append a line to the driver's own log.

    Upstream kidsout runs the scripts with Go's cmd.Output(), which discards
    stderr on the success path, so stderr is not a usable diagnostic channel
    (REVIEW.1 F-02). This file is.
    """
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > LOG_MAX_BYTES:
            with open(LOG_FILE, "rb") as f:
                f.seek(-LOG_MAX_BYTES // 2, os.SEEK_END)
                f.readline()          # drop the partial line
                tail = f.read()
            with open(LOG_FILE, "wb") as f:
                f.write(b"... truncated ...\n" + tail)
        with open(LOG_FILE, "a") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except Exception:
        pass                          # logging must never break enforcement


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def load_json(path, required=True):
    if not os.path.exists(path):
        if required:
            sys.exit(f"missing file: {path}")
        return None
    with open(path) as f:
        return json.load(f)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    """Merge into the state file rather than overwriting it.

    Several parts of a single run touch this file — the endpoint cache during
    probe(), the counter sample at the end of cmd_state — and a plain overwrite
    lets the later write discard the earlier one.
    """
    try:
        merged = load_state()
        merged.update(state)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(merged, f, indent=2, sort_keys=True)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log(f"warn: could not write {STATE_FILE}: {e}")


def secure_config(path):
    """config.json holds the rpcd password; keep it owner-only (REVIEW.1 S-02)."""
    try:
        mode = os.stat(path).st_mode & 0o777
        if mode & 0o077:
            os.chmod(path, 0o600)
            log(f"warn: {os.path.basename(path)} was mode {mode:o}; tightened to 600")
            print(f"warning: {path} was mode {mode:o} — tightened to 600", file=sys.stderr)
    except Exception:
        pass


class Deadline:
    """A hard wall-clock ceiling, so the driver always answers before kidsout
    kills it at 10 s and can report a truthful 'unknown' (REVIEW.1 F-03)."""

    def __init__(self, seconds):
        self.end = time.monotonic() + seconds if seconds else None

    def remaining(self):
        if self.end is None:
            return None
        return self.end - time.monotonic()

    def budget(self, per_request):
        left = self.remaining()
        if left is None:
            return per_request
        if left <= 0.05:
            raise Expired("out of time budget before the router answered")
        return max(0.05, min(per_request, left))


def human_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0


# --------------------------------------------------------------------------- #
# ubus JSON-RPC client
# --------------------------------------------------------------------------- #
class Router:
    """ubus-over-HTTP JSON-RPC client for an OpenWrt router."""

    def __init__(self, cfg, device, timeout=None, deadline=None):
        self.cfg = cfg
        self.device = device
        r = device["router"]
        self.hosts = ([cfg["host"]] if cfg.get("host")
                      else [r["primary_host"]] + list(r.get("alt_hosts") or []))
        self.https_port = cfg.get("port") or r.get("https_port", 443)
        self.http_port = cfg.get("port") or r.get("http_port", 80)
        self.prefer_https = cfg.get("https", True)
        self.allow_http_fallback = cfg.get("allow_http_fallback", True)
        self.paths = list(r.get("ubus_path_candidates") or ["/ubus"])
        self.verify_tls = cfg.get("verify_tls", False)
        self.pin = (r.get("tls_sha256") or cfg.get("tls_sha256") or "").lower().replace(":", "")
        self.timeout = timeout if timeout is not None else cfg.get("timeout", 10)
        self.session_timeout = cfg.get("session_timeout", 30)
        self.username = cfg.get("username", "kidsout")
        self.password = cfg.get("password", "")
        self.deadline = deadline or Deadline(None)
        self.url = None
        self.session = None
        self._failure = None          # memoised: do not re-sweep after a failure
        self._id = 0
        self._tried = []

    # ---- transport ---------------------------------------------------- #
    def _post(self, url, payload):
        scheme, rest = url.split("://", 1)
        hostport, path = rest.split("/", 1)
        path = "/" + path
        host, _, port = hostport.partition(":")
        port = int(port) if port else (443 if scheme == "https" else 80)
        t = self.deadline.budget(self.timeout)

        if scheme == "https":
            ctx = ssl.create_default_context()
            if not self.verify_tls:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            conn = http.client.HTTPSConnection(host, port, timeout=t, context=ctx)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=t)

        try:
            # Connect first and check the pin BEFORE the credentials go out.
            conn.connect()
            if scheme == "https" and self.pin:
                der = conn.sock.getpeercert(binary_form=True)
                got = hashlib.sha256(der).hexdigest()
                if got != self.pin:
                    raise PinMismatch(
                        f"certificate fingerprint {got} does not match the pin "
                        f"{self.pin} in device.json — refusing to send credentials")
            body = json.dumps(payload)
            conn.request("POST", path, body=body,
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            raw = resp.read(1 << 20)
            if resp.status != 200:
                raise RouterUnreachable(f"HTTP {resp.status} from {url}")
            return json.loads(raw.decode())
        except (OSError, socket.timeout, json.JSONDecodeError,
                http.client.HTTPException) as e:
            raise RouterUnreachable(f"{url}: {type(e).__name__}: {e}") from e
        finally:
            conn.close()

    def _rpc(self, url, method, params):
        self._id += 1
        resp = self._post(url, {"jsonrpc": "2.0", "id": self._id,
                                "method": method, "params": params})
        if "error" in resp:
            err = resp["error"]
            raise UbusError(f"JSON-RPC error {err.get('code')}: {err.get('message')}")
        result = resp.get("result", [])
        if not result:
            raise UbusError("empty ubus result")
        status = result[0]
        if status != 0:
            raise UbusError(f"ubus status {status} ({UBUS_STATUS.get(status, '?')}) "
                            f"for {params[1]}.{params[2]}")
        return result[1] if len(result) > 1 else {}

    # ---- endpoint discovery ------------------------------------------- #
    def candidate_urls(self):
        """Cached winner first, then https, then http (REVIEW.1 F-11, F-12)."""
        seen = []
        cached = load_state().get("endpoint")
        # Only honour the cache if it still names a configured host and port:
        # otherwise a stale entry would silently override an explicit `host` in
        # config.json, or keep talking to a router that has been renumbered.
        if cached and self._configured(cached):
            seen.append(cached)
        schemes = [("https", self.https_port)] if self.prefer_https else [("http", self.http_port)]
        if self.allow_http_fallback:
            other = ("http", self.http_port) if self.prefer_https else ("https", self.https_port)
            schemes.append(other)
        for scheme, port in schemes:
            for host in self.hosts:
                for path in self.paths:
                    url = f"{scheme}://{host}:{port}{path}"
                    if url not in seen:
                        seen.append(url)
        return seen

    def _configured(self, url):
        try:
            hostport = url.split("://", 1)[1].split("/", 1)[0]
            host, _, port = hostport.partition(":")
            return (host in self.hosts
                    and (not port or int(port) in (self.https_port, self.http_port)))
        except (IndexError, ValueError):
            return False

    def probe(self):
        """Find a working ubus endpoint without credentials.

        Any parseable JSON-RPC reply — including 'Access denied' — proves the
        endpoint is alive. A 404, a redirect or a connection error means: next.
        """
        if self.url:
            return self.url
        if self._failure:
            raise self._failure
        self._tried = []
        last = None
        for url in self.candidate_urls():
            try:
                self._post(url, {"jsonrpc": "2.0", "id": 0, "method": "call",
                                 "params": ["0" * 32, "session", "list", {}]})
            except PinMismatch:
                raise
            except Expired as e:
                self._failure = e
                raise
            except RouterUnreachable as e:
                self._tried.append(f"{url}: {e}")
                last = e
                continue
            self.url = url
            st = load_state()
            if st.get("endpoint") != url:
                st["endpoint"] = url
                save_state(st)
            return url
        self._failure = RouterUnreachable(
            "no working ubus endpoint. tried: " + "; ".join(self._tried or [str(last)]))
        raise self._failure

    # ---- session ------------------------------------------------------- #
    def login(self):
        if self.session:
            return self.session
        if self._failure:
            raise self._failure           # memoised (REVIEW.1 F-03)
        url = self.probe()
        try:
            r = self._rpc(url, "call", [
                "0" * 32, "session", "login",
                # short-lived: one process per invocation, never reused, so a
                # long timeout only leaves stale sessions in rpcd (F-15)
                {"username": self.username, "password": self.password,
                 "timeout": self.session_timeout},
            ])
        except UbusError as e:
            self._failure = RouterUnreachable(f"login as '{self.username}' failed: {e}")
            raise self._failure from e
        except RouterUnreachable as e:
            self._failure = e
            raise
        self.session = r.get("ubus_rpc_session")
        if not self.session:
            self._failure = RouterUnreachable("login returned no session token")
            raise self._failure
        return self.session

    def call(self, obj, method, params=None):
        sid = self.login()
        return self._rpc(self.url, "call", [sid, obj, method, params or {}])

    # ---- uci ----------------------------------------------------------- #
    def uci_get(self, config, type_=None):
        return self.call("uci", "get", {"config": config, **({"type": type_} if type_ else {})})

    def uci_set(self, config, section, values):
        return self.call("uci", "set", {"config": config, "section": section, "values": values})

    def uci_add(self, config, type_, values=None, name=None):
        p = {"config": config, "type": type_, "values": values or {}}
        if name:
            p["name"] = name
        return self.call("uci", "add", p)

    def uci_delete(self, config, section):
        return self.call("uci", "delete", {"config": config, "section": section})

    def uci_commit(self, config):
        return self.call("uci", "commit", {"config": config})

    def uci_apply(self, rollback=False, timeout=30):
        return self.call("uci", "apply", {"rollback": rollback, "timeout": timeout})

    # ---- the router-side helper ---------------------------------------- #
    def helper(self, verb, *args):
        """Run a verb of /usr/libexec/kidsout-xbox. Returns (code, stdout, stderr).

        Raises rather than swallowing: 'the helper said no' and 'we could not
        ask the router' must stay distinguishable (REVIEW.1 F-02).
        """
        r = self.call("file", "exec", {"command": HELPER, "params": [verb, *[str(a) for a in args]]})
        return r.get("code", -1), (r.get("stdout") or ""), (r.get("stderr") or "")

    def helper_ok(self, verb, *args):
        code, out, err = self.helper(verb, *args)
        if code != 0:
            raise UbusError(f"helper {verb} failed (code {code}): {err.strip() or out.strip()}")
        return out


UBUS_STATUS = {
    1: "invalid command", 2: "invalid argument", 3: "method not found",
    4: "not found", 5: "no data", 6: "permission denied", 7: "timeout",
    8: "not supported", 9: "unknown error", 10: "connection failed",
}


# --------------------------------------------------------------------------- #
# firewall rules
# --------------------------------------------------------------------------- #
def require_mac(device):
    mac = (device.get("mac") or "").strip()
    if not mac or not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", mac):
        sys.exit("device.json has no valid 'mac'. Turn the console on and run:\n"
                 "    ./xbox.py discover --write")
    return mac.lower()


def get_xbox_rules(device):
    """The rule set. One rule: everything the console forwards to wan is REJECTed.

    Keyed on the MAC, not the IPv4 address, so IPv6 is covered too (F-04) and a
    DHCP change cannot silently disarm the block. v1's `_fwd_in` added nothing
    (a console that cannot send is not reachable either) and its `_input_out`
    was a forward rule despite its name and a strict subset of this one (F-08).
    """
    p = device["rules"]["prefix"]
    return [
        (f"{p}_out", {
            "name": "kidsout xbox: block internet",
            "src": device["network"]["zone"],
            "dest": device["network"].get("wan_zone", "wan"),
            "src_mac": require_mac(device),
            "proto": "all",
            "target": "REJECT",
            "enabled": ENABLED_WHEN_ALLOWED,
        }),
    ]


def find_sections(fw, prefix):
    data = fw.uci_get("firewall")
    return {n: o for n, o in (data.get("values") or {}).items() if n.startswith(prefix)}


def describe(value):
    return "internet BLOCKED" if value == ENABLED_WHEN_BLOCKED else "internet ALLOWED"


def cmd_install(fw, device, args):
    rules = get_xbox_rules(device)
    wanted = dict(rules)
    existing = find_sections(fw, device["rules"]["prefix"])

    created, updated, removed = [], [], []
    for name, values in rules:
        if name in existing:
            fw.uci_set("firewall", name, values)
            updated.append(name)
        else:
            fw.uci_add("firewall", "rule", values=values, name=name)
            created.append(name)
    # migration: drop sections from an older rule set (F-08)
    for name in existing:
        if name not in wanted:
            fw.uci_delete("firewall", name)
            removed.append(name)

    fw.uci_commit("firewall")
    fw.uci_apply(rollback=False)
    print(f"firewall: created={created or '[]'} updated={updated or '[]'} removed={removed or '[]'}")
    print(f"          installed in the ALLOWED state "
          f"(REJECT rule present but enabled={ENABLED_WHEN_ALLOWED} -> {describe(ENABLED_WHEN_ALLOWED)})")

    out = install_counters(fw, device)
    print(f"counters: {out}")
    log(f"install: created={created} updated={updated} removed={removed} counters={out}")
    print("\nthe console should still be online. verify, then './xbox.py block' to test enforcement.")


def cmd_uninstall(fw, device, _args):
    existing = find_sections(fw, device["rules"]["prefix"])
    for name in existing:
        fw.uci_delete("firewall", name)
    if existing:
        fw.uci_commit("firewall")
        fw.uci_apply(rollback=False)
        print(f"firewall: removed {sorted(existing)}")
    else:
        print("firewall: no kidsout_xbox sections found")
    try:
        print("counters:", fw.helper_ok("counters-remove").strip())
    except UbusError as e:
        print(f"counters: {e}")
    log(f"uninstall: removed={sorted(existing)}")


def _set_enabled(fw, device, value):
    """Idempotent: reads before writing, so the steady state costs one read-only
    RPC and no flash write (REVIEW.1 F-09). Returns True if anything changed."""
    existing = find_sections(fw, device["rules"]["prefix"])
    if not existing:
        sys.exit("no kidsout_xbox firewall sections found — run './xbox.py install' first")
    stale = [n for n, o in existing.items() if str(o.get("enabled", "1")) != value]
    if not stale:
        print(f"already {describe(value)} (no change, nothing written)")
        return False
    for name in stale:
        fw.uci_set("firewall", name, {"enabled": value})
    fw.uci_commit("firewall")
    fw.uci_apply(rollback=False)
    print(f"{describe(value)} — {len(stale)} rule(s) updated and firewall reloaded")
    log(f"set enabled={value} ({describe(value)}) on {sorted(stale)}")
    return True


def console_addresses(fw, device):
    """The console's IPv4 plus any IPv6 addresses the router knows for its MAC."""
    addrs = [device["ipv4"]]
    mac = (device.get("mac") or "").lower()
    if not mac:
        return addrs
    try:
        for line in fw.helper_ok("neigh").splitlines():
            f = line.split()
            if len(f) >= 5 and mac in line.lower() and f[0] not in addrs:
                addrs.append(f[0])
    except Exception as e:
        log(f"warn: neighbour lookup failed: {e}")
    return addrs


def flush_conntrack(fw, device):
    """Drop the console's existing flows so a block bites immediately.

    OpenWrt accepts established/related flows before any user rule, so without
    this an in-progress game survives the block until its conntrack entry
    expires — up to five days (REVIEW.1 F-05).
    """
    addrs = console_addresses(fw, device)
    try:
        fw.helper_ok("flush", *addrs)
        log(f"conntrack flushed for {addrs}")
        return f"flushed {' '.join(addrs)}"
    except UbusError as e:
        log(f"warn: conntrack flush failed: {e}")
        return (f"NOT flushed ({e}). Install conntrack-tools on the router "
                f"('opkg install conntrack-tools' or 'apk add conntrack-tools'), "
                f"otherwise a game already in progress survives the block.")


def cmd_block(fw, device, _args):
    _set_enabled(fw, device, ENABLED_WHEN_BLOCKED)
    # Always flush, even when the rule was already enabled: re-asserting the
    # block is exactly when a lingering flow needs killing.
    print("conntrack:", flush_conntrack(fw, device))


def cmd_allow(fw, device, _args):
    _set_enabled(fw, device, ENABLED_WHEN_ALLOWED)


# --------------------------------------------------------------------------- #
# traffic counters  (the state signal)
# --------------------------------------------------------------------------- #
def install_counters(fw, device):
    return fw.helper_ok("counters-install", require_mac(device), device["ipv4"]).strip()


def read_counters(fw):
    """Return {'xbox_out': bytes, 'xbox_in': bytes}.

    Raises UbusError with code 4 semantics if the table is missing, so callers
    can self-heal; raises RouterUnreachable if the router could not be asked.
    """
    code, out, err = fw.helper("counters")
    if code == 4:
        raise CountersMissing(err.strip() or "counters table missing")
    if code != 0:
        raise UbusError(f"helper counters failed (code {code}): {err.strip() or out.strip()}")

    vals = {}
    try:
        doc = json.loads(out)
        for item in doc.get("nftables", []):
            c = item.get("counter")
            if c and "name" in c:
                vals[c["name"]] = int(c.get("bytes", 0))
    except (json.JSONDecodeError, ValueError, AttributeError):
        # plain-text fallback for images without nft json support
        for m in re.finditer(r"counter\s+(\S+)\s*\{[^}]*?bytes\s+(\d+)", out, re.S):
            vals[m.group(1)] = int(m.group(2))
    if not vals:
        raise UbusError(f"could not parse counters from: {out[:200]!r}")
    return vals


class CountersMissing(UbusError):
    """The nft accounting table is not present on the router."""


def sample_rate(fw, device, state, now=None):
    """Bytes-per-minute since the previous sample, or None if not computable.

    Returns (rate_out, rate_in, reason). reason is None when the rate is
    usable, otherwise a short explanation for the log.
    """
    now = now or time.time()
    cur = read_counters(fw)
    prev = state.get("counters")
    prev_ts = state.get("counters_ts")
    max_age = device.get("state", {}).get("max_sample_age_s", 300)

    state["counters"] = cur
    state["counters_ts"] = now

    if not prev or not prev_ts:
        return None, None, "no previous sample yet (first run after install/restart)"
    dt = now - prev_ts
    if dt <= 0:
        return None, None, "clock went backwards"
    if dt > max_age:
        return None, None, f"previous sample is {dt:.0f}s old (> {max_age}s), too stale to use"
    if any(cur.get(k, 0) < prev.get(k, 0) for k in cur):
        return None, None, "counters decreased (router reboot or nft flush)"

    per_min = 60.0 / dt
    return ((cur.get("xbox_out", 0) - prev.get("xbox_out", 0)) * per_min,
            (cur.get("xbox_in", 0) - prev.get("xbox_in", 0)) * per_min,
            None)


# --------------------------------------------------------------------------- #
# the kidsout contract: state
# --------------------------------------------------------------------------- #
def cmd_state(fw, device, _args):
    """Print exactly one of up / down / unknown.

    up      the console's outbound byte rate is above the threshold
    down    the router answered and the console is idle (or blocked)
    unknown the router could not be consulted, or there is no usable baseline

    Never raises: kidsout parses stdout, and a traceback there would be read as
    'unknown' by accident rather than on purpose. Detail goes to xbox.log,
    because upstream discards stderr (REVIEW.1 F-02).
    """
    st = load_state()
    cfg_state = device.get("state", {})
    threshold = cfg_state.get("threshold_bytes_per_min", 200 * 1024)

    try:
        try:
            rate_out, rate_in, reason = sample_rate(fw, device, st)
        except CountersMissing:
            # self-heal: recreate the accounting table, then admit we cannot
            # know this tick (F-10 — nothing else reconciles router-side state)
            install_counters(fw, device)
            save_state(st)
            print("unknown")
            log("state=unknown reason=counters table missing; reinstalled")
            return 0

        save_state(st)

        if reason:
            print("unknown")
            log(f"state=unknown reason={reason}")
            return 0

        verdict = "up" if rate_out > threshold else "down"
        print(verdict)
        log(f"state={verdict} out={rate_out / 1024:.1f}KB/min in={rate_in / 1024:.1f}KB/min "
            f"threshold={threshold / 1024:.0f}KB/min")
        _periodic_rule_audit(fw, device, st, verdict)
        return 0

    except Exception as e:
        print("unknown")
        log(f"state=unknown error={type(e).__name__}: {e}")
        print(f"xbox state unknown: {e}", file=sys.stderr)
        return 1


def _periodic_rule_audit(fw, device, st, verdict):
    """Every so often, record what the router's enforcement state actually is.

    kidsout drives block/unblock on transitions, so nothing otherwise notices if
    the router and kidsout disagree — a stuck block would be invisible until a
    human ran unblock.sh (REVIEW.1 F-10). This does not fix the divergence, it
    makes it visible in the log.
    """
    period = device.get("state", {}).get("rule_audit_period_s", 900)
    last = st.get("rule_audit_ts", 0)
    if time.time() - last < period:
        return
    try:
        secs = find_sections(fw, device["rules"]["prefix"])
        if not secs:
            log("audit: NO kidsout_xbox rules installed on the router")
        else:
            vals = {n: str(o.get("enabled", "?")) for n, o in sorted(secs.items())}
            meaning = ", ".join(f"{n}={describe(v)}" for n, v in vals.items())
            log(f"audit: {meaning}")
        st["rule_audit_ts"] = time.time()
        save_state(st)
    except Exception as e:
        log(f"audit: failed: {e}")


# --------------------------------------------------------------------------- #
# diagnostics
# --------------------------------------------------------------------------- #
def cmd_probe(fw, device, _args):
    print("trying ubus endpoints without credentials "
          "(a JSON 'Access denied' means the endpoint is alive):\n")
    found = None
    for url in fw.candidate_urls():
        try:
            r = fw._post(url, {"jsonrpc": "2.0", "id": 0, "method": "call",
                               "params": ["0" * 32, "session", "list", {}]})
            print(f"  OK    {url}  ->  {json.dumps(r)[:90]}")
            found = found or url
        except Exception as e:
            print(f"  FAIL  {url}  ->  {e}")
    print()
    if found:
        print(f"usable endpoint: {found}")
        return 0
    print("no usable endpoint. Check the router is up, that uhttpd-mod-ubus is\n"
          "installed, and that device.json's hosts/ports are right.")
    return 1


def cmd_pin(fw, device, _args):
    """Record the router's certificate fingerprint so TLS can be verified
    against a self-signed cert without trusting the whole LAN (S-03)."""
    url = fw.probe()
    if not url.startswith("https"):
        sys.exit(f"endpoint {url} is not HTTPS — nothing to pin")
    host, _, port = url.split("://", 1)[1].split("/", 1)[0].partition(":")
    port = int(port or 443)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=5) as s:
        with ctx.wrap_socket(s, server_hostname=host) as ss:
            fp = hashlib.sha256(ss.getpeercert(binary_form=True)).hexdigest()
    print(f"{host}:{port} certificate sha256 = {fp}")
    dev = load_json(DEVICE_FILE)
    dev["router"]["tls_sha256"] = fp
    with open(DEVICE_FILE, "w") as f:
        json.dump(dev, f, indent=2)
        f.write("\n")
    print(f"written to device.json (router.tls_sha256)")
    print("from now on the driver refuses to send credentials to any other certificate.")
    print("NOTE: re-run this after the router regenerates its certificate.")
    return 0


def cmd_selftest(fw, device, _args):
    """Verify every capability the driver needs, with real credentials."""
    results = []

    def check(label, fn):
        try:
            detail = fn()
            results.append((True, label, detail))
            print(f"  PASS  {label}" + (f" — {detail}" if detail else ""))
        except Exception as e:
            results.append((False, label, str(e)))
            print(f"  FAIL  {label} — {e}")

    print("kidsout xbox driver selftest\n")
    check("ubus endpoint reachable", lambda: fw.probe())
    check(f"login as '{fw.username}'", lambda: f"session {fw.login()[:8]}…")
    check("read uci firewall", lambda: f"{len(fw.uci_get('firewall').get('values') or {})} sections")
    check("router helper installed", lambda: fw.helper_ok("info").strip().replace("\n", ", "))
    check("helper: neighbour table", lambda: f"{len(fw.helper_ok('neigh').splitlines())} entries")
    check("helper: dhcp leases", lambda: f"{len(fw.helper_ok('leases').splitlines())} leases")

    def counters():
        try:
            return ", ".join(f"{k}={human_bytes(v)}" for k, v in sorted(read_counters(fw).items()))
        except CountersMissing:
            return "not installed yet (run './xbox.py install')"
    check("traffic counters", counters)

    def rules():
        secs = find_sections(fw, device["rules"]["prefix"])
        if not secs:
            return "not installed yet (run './xbox.py install')"
        return ", ".join(f"{n}: {describe(str(o.get('enabled')))}" for n, o in sorted(secs.items()))
    check("firewall rules", rules)

    check("write access to uci firewall (dry run)", lambda: _write_probe(fw))

    failed = [r for r in results if not r[0]]
    print()
    if failed:
        print(f"{len(failed)} check(s) failed. The driver will not work correctly yet.")
        return 1
    print("all checks passed.")
    return 0


def _write_probe(fw):
    """Confirm the ACL really grants uci write, without changing anything:
    add a throwaway section, then revert it before committing."""
    name = "kidsout_xbox_writetest"
    fw.uci_add("firewall", "rule", values={"name": "kidsout write test",
                                           "enabled": "0", "target": "REJECT"}, name=name)
    fw.uci_delete("firewall", name)
    fw.call("uci", "revert", {"config": "firewall"})
    return "granted (test section added and reverted, nothing committed)"


def cmd_status(fw, device, _args):
    ip = device["ipv4"]
    try:
        print(f"endpoint : {fw.probe()}")
    except Exception as e:
        print(f"endpoint : UNREACHABLE ({e})")
        return 1
    try:
        board = fw.call("system", "board")
        print(f"router   : {board.get('model')} / {board.get('release', {}).get('version')}")
    except Exception as e:
        print(f"router   : (board info unavailable: {e})")

    try:
        info = dict(l.split("=", 1) for l in fw.helper_ok("info").strip().splitlines() if "=" in l)
        print(f"facts    : firewall={info.get('firewall')} nft_json={info.get('nft_json')} "
              f"conntrack_tools={info.get('conntrack_tools')} openwrt={info.get('openwrt')}")
    except Exception as e:
        print(f"facts    : helper unavailable ({e})")

    try:
        secs = find_sections(fw, device["rules"]["prefix"])
        if secs:
            for n, o in sorted(secs.items()):
                print(f"rule     : {n}: {describe(str(o.get('enabled')))} "
                      f"(target={o.get('target')} src_mac={o.get('src_mac')} "
                      f"{o.get('src')}->{o.get('dest')})")
        else:
            print("rule     : NOT INSTALLED (run './xbox.py install')")
    except Exception as e:
        print(f"rule     : unreadable ({e})")

    st = load_state()
    try:
        rate_out, rate_in, reason = sample_rate(fw, device, st)
        save_state(st)
        threshold = device.get("state", {}).get("threshold_bytes_per_min", 200 * 1024)
        if reason:
            print(f"traffic  : no rate available — {reason}")
        else:
            verdict = "up" if rate_out > threshold else "down"
            print(f"traffic  : out={rate_out / 1024:.1f} KB/min  in={rate_in / 1024:.1f} KB/min  "
                  f"threshold={threshold / 1024:.0f} KB/min  -> {verdict}")
    except CountersMissing:
        print("traffic  : counters NOT INSTALLED (run './xbox.py install')")
    except Exception as e:
        print(f"traffic  : unavailable ({e})")

    try:
        addrs = console_addresses(fw, device)
        print(f"console  : {ip} mac={device.get('mac')} "
              f"addresses seen on the router: {', '.join(addrs)}")
        if len(addrs) > 1:
            print("           (IPv6 present — the MAC-based rule covers it)")
    except Exception as e:
        print(f"console  : ({e})")
    return 0


def cmd_check(fw, device, _args):
    """Is the console present on the LAN right now?

    v1 TCP-connected to ports 53/80/443/3074 on the console. Those are
    destination ports it talks *to*; it listens on none of them, so the answer
    was 'closed/filtered' whatever the console was doing (REVIEW.1 F-14). The
    router's neighbour table is the honest test, and works despite the console
    ignoring ICMP.
    """
    mac = (device.get("mac") or "").lower()
    ip = device["ipv4"]
    rows = fw.helper_ok("neigh").splitlines()
    hits = [r.strip() for r in rows if (mac and mac in r.lower()) or r.startswith(ip + " ")]
    if not hits:
        print(f"console: NOT present in the router's neighbour table "
              f"(ip={ip} mac={mac or 'unknown'}) — powered off, or fully asleep")
        return 1
    for h in hits:
        print(f"console: {h}")
    print("\n(REACHABLE/STALE/DELAY = the router has an address for it; "
          "FAILED/INCOMPLETE = it is not answering)")
    return 0


def cmd_discover(fw, device, args):
    ip = device["ipv4"]
    found = None
    for line in fw.helper_ok("leases").splitlines():
        f = line.split()
        if len(f) >= 4 and f[2] == ip:
            found = f[1].lower()
            print(f"dhcp lease: ip={ip} mac={found} hostname={f[3]}")
    if not found:
        print(f"dhcp lease: none for {ip} right now")
    for line in fw.helper_ok("neigh").splitlines():
        f = line.split()
        if f and f[0] == ip and "lladdr" in line:
            found = found or f[f.index("lladdr") + 1].lower()
            print(f"neighbour : {line.strip()}")
    if not found:
        print("\ncould not determine the MAC. Turn the console on, make it talk to the\n"
              "network (open a game or the store), and run this again.")
        return 1
    print(f"\nmac: {found}")
    if args.write:
        dev = load_json(DEVICE_FILE)
        dev["mac"] = found
        dev.pop("comment", None)
        with open(DEVICE_FILE, "w") as f:
            json.dump(dev, f, indent=2)
            f.write("\n")
        print("written to device.json. Now run './xbox.py install'.")
    else:
        print("re-run with --write to record it in device.json.")
    return 0


def cmd_counters(fw, device, _args):
    st = load_state()
    try:
        rate_out, rate_in, reason = sample_rate(fw, device, st)
    except CountersMissing:
        print("counters not installed — run './xbox.py install'")
        return 1
    save_state(st)
    cur = st["counters"]
    print(f"totals since install/reboot: "
          f"out={human_bytes(cur.get('xbox_out', 0))}  in={human_bytes(cur.get('xbox_in', 0))}")
    if reason:
        print(f"rate: not available — {reason}")
        print("run this again in a minute to get a rate.")
    else:
        print(f"rate since last sample:     "
              f"out={rate_out / 1024:.1f} KB/min  in={rate_in / 1024:.1f} KB/min")
    return 0


def cmd_calibrate(fw, device, args):
    """Sample the byte rate over time, to pick the up/down threshold (F-06)."""
    interval = args.interval
    end = time.time() + args.minutes * 60
    label = args.label or "sample"
    print(f"sampling every {interval}s for {args.minutes} min  (label: {label})")
    print(f"writing to {os.path.basename(args.out)}\n")
    print(f"{'time':>8}  {'out KB/min':>11}  {'in KB/min':>10}")

    rows = []
    prev = None
    prev_ts = None
    try:
        while time.time() < end:
            now = time.time()
            cur = read_counters(fw)
            if prev is not None and now > prev_ts:
                f = 60.0 / (now - prev_ts)
                out = (cur.get("xbox_out", 0) - prev.get("xbox_out", 0)) * f / 1024
                inn = (cur.get("xbox_in", 0) - prev.get("xbox_in", 0)) * f / 1024
                stamp = datetime.now().strftime("%H:%M:%S")
                print(f"{stamp:>8}  {out:>11.1f}  {inn:>10.1f}")
                rows.append({"t": stamp, "label": label, "out_kb_min": round(out, 1),
                             "in_kb_min": round(inn, 1)})
                with open(args.out, "a") as f_:
                    f_.write(json.dumps(rows[-1]) + "\n")
            prev, prev_ts = cur, now
            time.sleep(max(1, interval - (time.time() - now)))
    except KeyboardInterrupt:
        print("\ninterrupted")

    if rows:
        outs = sorted(r["out_kb_min"] for r in rows)
        print(f"\n{label}: n={len(outs)}  min={outs[0]:.1f}  "
              f"median={outs[len(outs) // 2]:.1f}  max={outs[-1]:.1f} KB/min")
    return 0


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
COMMANDS = {
    "probe": cmd_probe, "selftest": cmd_selftest, "status": cmd_status,
    "state": cmd_state, "block": cmd_block, "allow": cmd_allow,
    "install": cmd_install, "uninstall": cmd_uninstall, "discover": cmd_discover,
    "check": cmd_check, "counters": cmd_counters, "calibrate": cmd_calibrate,
    "pin": cmd_pin,
}


def main():
    ap = argparse.ArgumentParser(description="kidsout xbox device driver")
    ap.add_argument("command", choices=sorted(COMMANDS))
    ap.add_argument("--write", action="store_true",
                    help="discover: record the MAC in device.json")
    ap.add_argument("--minutes", type=float, default=10,
                    help="calibrate: how long to sample (default 10)")
    ap.add_argument("--interval", type=float, default=60,
                    help="calibrate: seconds between samples (default 60)")
    ap.add_argument("--label", default=None,
                    help="calibrate: what the console is doing (e.g. standby, gaming)")
    ap.add_argument("--out", default=os.path.join(HERE, "calibration.jsonl"),
                    help="calibrate: where to append samples")
    args = ap.parse_args()

    device = load_json(DEVICE_FILE)
    if os.path.exists(CONFIG_FILE):
        secure_config(CONFIG_FILE)
    cfg = load_json(CONFIG_FILE, required=False) or {}

    # The state path gets its own, much tighter budget than the interactive
    # commands, and a hard ceiling below kidsout's 10 s timeout (REVIEW.1 F-03).
    if args.command == "state":
        timeout = cfg.get("state_timeout", 2)
        deadline = Deadline(cfg.get("state_deadline", 7))
    else:
        timeout = cfg.get("timeout", 10)
        deadline = Deadline(None)

    fw = Router(cfg, device, timeout=timeout, deadline=deadline)

    if args.command not in ("probe", "state") and not cfg:
        sys.exit(f"no {os.path.basename(CONFIG_FILE)} — copy config.example.json to "
                 f"config.json and fill in the rpcd credentials (see README).")

    try:
        rc = COMMANDS[args.command](fw, device, args)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        if args.command == "state":
            # belt and braces: cmd_state handles its own failures, but a word on
            # stdout is the contract and a traceback there is not one.
            print("unknown")
            log(f"state=unknown error={type(e).__name__}: {e}")
            sys.exit(1)
        if isinstance(e, (UbusError, RouterUnreachable)):
            sys.exit(f"router error: {e}")
        raise
    sys.exit(rc or 0)


if __name__ == "__main__":
    main()
