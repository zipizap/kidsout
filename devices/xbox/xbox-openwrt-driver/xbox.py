#!/usr/bin/env python3
"""
kidsout device driver: Xbox (two interfaces, see device.json)

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
    """The router answered and refused. `status` is the ubus status code."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


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
                            f"for {params[1]}.{params[2]}", status=status)
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
        """Trigger a service reload for the committed package.

        Tolerates ubus status 5 (NO_DATA), which is what rpcd returns when
        there is nothing staged to apply. That is the NORMAL case here:
        `uci commit` already writes the package AND fires
        rpc_uci_trigger_event() itself, so by the time apply runs the change
        set is empty. Verified on this router (OpenWrt 24.10.5, rpcd
        2025.09.01) — this settles REVIEW.1 F-09's open question: the explicit
        apply after commit is redundant, and treating its NO_DATA as an error
        made `install` exit 1 despite having done its job correctly.

        It is kept rather than deleted because other rpcd builds do stage
        changes for apply, and a missed reload is a silent enforcement
        failure — the expensive direction to be wrong in.
        """
        try:
            return self.call("uci", "apply", {"rollback": rollback, "timeout": timeout})
        except UbusError as e:
            if getattr(e, "status", None) == 5 or "no data" in str(e):
                return {}
            raise

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
MAC_RE = re.compile(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}")


def interfaces(device):
    """Every interface the console can appear on, as [{'mac','ipv4','link'}].

    A console has a SEPARATE MAC for wired and wireless, and switches between
    them whenever the cable is plugged or pulled. v2 recorded only whichever
    one happened to be live when `discover` ran, which meant the block could
    be keyed on an interface the console was not using -- matching nothing,
    while still reporting success (REVIEW.2 G-02). Both must be covered.

    This is the ONLY reader of the console's identity. The top-level
    mac/ipv4 scalars it used to fall back to are gone: they named interface
    [0] alone, so every caller that reached for them silently ignored the
    other interface -- which is the same G-02 bug wearing a different hat.
    """
    out = []
    for e in device.get("interfaces") or []:
        mac = (e.get("mac") or "").strip().lower()
        if MAC_RE.fullmatch(mac):
            out.append({"mac": mac,
                        "ipv4": (e.get("ipv4") or "").strip(),
                        "link": e.get("link") or "?"})
    return out


def require_macs(device):
    """Every configured MAC, lowercased. Exits if none is usable."""
    macs = [e["mac"] for e in interfaces(device)]
    if not macs:
        sys.exit("device.json lists no valid interface MAC. Turn the console on "
                 "and run:\n    ./xbox.py discover --write")
    return macs


def require_mac(device):
    """The primary MAC. Kept for callers that genuinely want just one."""
    return require_macs(device)[0]


def static_addresses(device):
    """Configured IPv4s, in device.json order. May be stale -- see live_addresses."""
    return [e["ipv4"] for e in interfaces(device) if e.get("ipv4")]


def get_xbox_rules(device):
    """The rule set. One rule: everything the console forwards to wan is REJECTed.

    Keyed on the MAC, not the IPv4 address, so IPv6 is covered too (F-04) and a
    DHCP change cannot silently disarm the block. v1's `_fwd_in` added nothing
    (a console that cannot send is not reachable either) and its `_input_out`
    was a forward rule despite its name and a strict subset of this one (F-08).

    ALL of the console's MACs go on the one rule. fw4 parses src_mac as a list
    (fw4.uc:2314, PARSE_LIST), so wired and wireless are covered by a single
    section and the block does not care which one the console is using today
    (REVIEW.2 G-02).
    """
    p = device["rules"]["prefix"]
    macs = require_macs(device)
    return [
        (f"{p}_out", {
            "name": "kidsout xbox: block internet",
            "src": device["network"]["zone"],
            "dest": device["network"].get("wan_zone", "wan"),
            "src_mac": macs if len(macs) > 1 else macs[0],
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


def live_addresses(fw, device):
    """Addresses the router currently associates with any of the console's MACs.

    Resolved at run time from the DHCP lease file and the neighbour table
    rather than trusting device.json's static IPv4, because only one of the
    console's two interfaces is up at a time and the other one's address is
    stale by definition. Both interfaces do now hold a static DHCP reservation
    (see device.json's comment), but a reservation only takes effect once the
    console renews, so it makes this resolution reliable rather than
    unnecessary (REVIEW.2 G-02, G-06).

    Returns (addresses, links) where links maps address -> how we found it.
    """
    macs = {e["mac"] for e in interfaces(device)}
    found, how = [], {}

    def note(addr, src):
        if addr and addr not in found:
            found.append(addr)
            how[addr] = src

    try:
        for line in fw.helper_ok("leases").splitlines():
            f = line.split()
            # <expiry> <mac> <ip> <hostname> <clientid>
            if len(f) >= 3 and f[1].lower() in macs:
                note(f[2], "lease")
    except Exception as e:
        log(f"warn: lease lookup failed: {e}")

    try:
        for line in fw.helper_ok("neigh").splitlines():
            f = line.split()
            # <addr> dev <dev> lladdr <mac> <state>
            if not f:
                continue
            low = line.lower()
            if any(m in low for m in macs) and "failed" not in low:
                note(f[0], "neigh")
    except Exception as e:
        log(f"warn: neighbour lookup failed: {e}")

    return found, how


def routable(addr):
    """Could traffic from this address reach the internet?

    Link-local (fe80::/10, 169.254/16) and unique-local (fd00::/8) addresses
    never leave the LAN, so traffic on them is the console talking to something
    in the house -- not internet use. Counting it would inflate the byte rate
    with LAN chatter and break a threshold that was calibrated on IPv4 alone.
    They are still worth FLUSHING, just not worth COUNTING.
    """
    a = addr.lower()
    if a.startswith("fe80:") or a.startswith("169.254."):
        return False
    if a.startswith("fd") or a.startswith("fc"):      # ULA, fc00::/7
        return ":" not in a                            # keep IPv4 that starts 'fd'? never
    return True


def accounting_addresses(fw, device):
    """Addresses whose bytes count toward the up/down decision."""
    return [a for a in console_addresses(fw, device) if routable(a)]


def console_addresses(fw, device):
    """Every address worth acting on: live ones first, configured ones as backup.

    The static entries are kept as a fallback so a block still targets
    something sensible when the router's tables are momentarily empty -- an
    extra address costs nothing, whereas missing the live one costs the block.
    """
    addrs, _how = live_addresses(fw, device)
    for a in static_addresses(device):
        if a and a not in addrs:
            addrs.append(a)
    return addrs


def flush_conntrack(fw, device):
    """Drop the console's existing flows so a block bites immediately.

    Returns (ok, detail). This is NOT an optimisation: with flow offloading
    enabled -- and this router offloads in hardware -- an established flow is
    forwarded by the PPE without entering any netfilter hook, so the REJECT
    rule never applies to it at all. Reloading the firewall does not help
    either: the conntrack entry survives, matches `ct state established
    : accept`, and is immediately re-offloaded. Destroying the conntrack entry
    is the ONLY thing that cuts a session already in progress (REVIEW.2 G-03).

    Once the flush lands the console cannot re-offload while blocked: `flow add
    @ft` only acts on established, bidirectionally-seen flows, and new SYNs are
    REJECTed before they get there. One successful flush is enough.
    """
    addrs = console_addresses(fw, device)
    if not addrs:
        return False, "no console addresses known — nothing could be flushed"
    try:
        detail = fw.helper_ok("flush", *addrs).strip()
        log(f"conntrack flushed for {addrs}: {detail}")
        return True, detail or f"flushed {' '.join(addrs)}"
    except UbusError as e:
        log(f"ERROR: conntrack flush failed: {e}")
        return False, (f"NOT flushed ({e}). Install conntrack-tools on the router "
                       f"('opkg install conntrack-tools' or 'apk add conntrack-tools'), "
                       f"otherwise a session already in progress survives the block.")


def cmd_block(fw, device, _args):
    _set_enabled(fw, device, ENABLED_WHEN_BLOCKED)
    # Always flush, even when the rule was already enabled: re-asserting the
    # block is exactly when a lingering flow needs killing.
    ok, detail = flush_conntrack(fw, device)
    print("conntrack:", detail)
    if not ok:
        # Exiting 0 here would tell kidsout the console was blocked while it
        # carried on playing. block.sh is re-run every tick while blocked and
        # up, so a non-zero exit costs one log line a minute and stops as soon
        # as the flush succeeds -- far better than a silent lie (REVIEW.2 G-03).
        log("block: FLUSH FAILED — an in-progress session may still be running")
        return 1


def cmd_allow(fw, device, _args):
    _set_enabled(fw, device, ENABLED_WHEN_ALLOWED)


# --------------------------------------------------------------------------- #
# traffic counters  (the state signal)
# --------------------------------------------------------------------------- #
def install_counters(fw, device):
    """Register the console's current addresses with the accounting helper.

    Re-registering the same set is a no-op on the router, so this is cheap to
    call on every self-heal; changing the set resets the accumulator, which
    costs exactly one 'unknown' tick.
    """
    addrs = accounting_addresses(fw, device)
    if not addrs:
        raise UbusError("no routable console addresses known (console offline "
                        "and device.json has no static ipv4)")
    return fw.helper_ok("counters-install", *addrs).strip()


def helper_facts(fw):
    """The helper's `info` verb as a dict. Cheap, read-only."""
    facts = {}
    for line in fw.helper_ok("info").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            facts[k.strip()] = v.strip()
    return facts


def read_counters(fw):
    """Return {'xbox_out': bytes, 'xbox_in': bytes} as MONOTONIC totals.

    The helper accumulates router-side and guarantees the totals never
    decrease, which is what sample_rate() assumes. That guarantee is the whole
    reason the accumulation happens on the router: the underlying conntrack
    byte counters are per-flow and vanish when a flow expires, so a naive sum
    goes DOWN -- measured at -510 KB/min during active gameplay (REVIEW.2
    G-04). Do not reintroduce a bare sum here.

    Raises CountersMissing (helper exit 4) when the console's addresses are not
    registered, so callers can self-heal; RouterUnreachable if the router could
    not be asked at all.
    """
    code, out, err = fw.helper("counters")
    if code == 4:
        raise CountersMissing(err.strip() or "no addresses registered")
    if code != 0:
        raise UbusError(f"helper counters failed (code {code}): {err.strip() or out.strip()}")

    vals = {}
    # v3 shape: "xbox_out <n>" / "xbox_in <n>" / "source <name>", one per line.
    for line in out.splitlines():
        f = line.split()
        if len(f) == 2 and f[0] in ("xbox_out", "xbox_in") and f[1].isdigit():
            vals[f[0]] = int(f[1])
    if vals:
        return vals

    # Pre-v3 nft shapes, kept so a router still running the old helper does not
    # hard-fail: json first, then the plain-text counter block.
    try:
        doc = json.loads(out)
        for item in doc.get("nftables", []):
            c = item.get("counter")
            if c and "name" in c:
                vals[c["name"]] = int(c.get("bytes", 0))
    except (json.JSONDecodeError, ValueError, AttributeError):
        for m in re.finditer(r"counter\s+(\S+)\s*\{[^}]*?bytes\s+(\d+)", out, re.S):
            vals[m.group(1)] = int(m.group(2))
    if not vals:
        raise UbusError(f"could not parse counters from: {out[:200]!r}")
    return vals


class CountersMissing(UbusError):
    """The console's addresses are not registered with the helper."""


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
def thresholds(cfg_state):
    """(threshold_out, threshold_in) in bytes/min from device.json -> state.

    threshold_in may be None, which means the inbound rule is off and the
    verdict is outbound-only (the pre-2026-09-21 behaviour).
    """
    return (cfg_state.get("threshold_out_bytes_per_min", 200 * 1024),
            cfg_state.get("threshold_in_bytes_per_min"))


def verdict_for(rate_out, rate_in, cfg_state):
    """Return ("up" | "down", "out" | "in" | None) for a computed rate pair.

    up when EITHER direction exceeds its threshold (strictly):

        rate_out > threshold_out_bytes_per_min      -> ("up", "out")
        rate_in  > threshold_in_bytes_per_min       -> ("up", "in")
        otherwise                                   -> ("down", None)

    Why two directions: gameplay uploads continuously (289-472 KB/min out,
    calibrated 2026-09-15), but streaming video is almost pure INBOUND -- YouTube
    measured 2026-09-21 at 60-80 KB/min out against 4.4-18 MB/min in, and read
    'down' all evening under the outbound-only rule. No outbound threshold can
    catch it: a background game download produces 93.7 KB/min out, MORE than
    streaming does, so any line low enough for YouTube also fires on patches and
    sits next to random ACK bursts. Hence a separate inbound floor.

    The accepted cost: a game download in progress reads 'up' too. That only
    happens while the console is powered on (it is in Energy-saving mode, so
    nothing downloads while off), and a false 'up' is loud -- it burns allowance
    and gets complained about -- whereas a false 'down' is silent unmetered
    viewing. See DESIGN.md §5.
    """
    thr_out, thr_in = thresholds(cfg_state)
    if rate_out > thr_out:
        return "up", "out"
    if thr_in is not None and rate_in > thr_in:
        return "up", "in"
    return "down", None


def _fmt_thresholds(cfg_state):
    thr_out, thr_in = thresholds(cfg_state)
    s = f"threshold_out={thr_out / 1024:.0f}KB/min"
    s += f" threshold_in={thr_in / 1024:.0f}KB/min" if thr_in is not None else " threshold_in=off"
    return s


def cmd_state(fw, device, _args):
    """Print exactly one of up / down / unknown.

    up      the console's outbound byte rate is above threshold_out, OR its
            inbound rate is above threshold_in (streaming) -- see verdict_for()
    down    the router answered and the console is idle (or blocked)
    unknown the router could not be consulted, or there is no usable baseline

    Never raises: kidsout parses stdout, and a traceback there would be read as
    'unknown' by accident rather than on purpose. Detail goes to xbox.log,
    because upstream discards stderr (REVIEW.1 F-02).
    """
    st = load_state()
    cfg_state = device.get("state", {})

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

        verdict, by = verdict_for(rate_out, rate_in, cfg_state)
        print(verdict)
        log(f"state={verdict}{' by=' + by if by else ''} "
            f"out={rate_out / 1024:.1f}KB/min in={rate_in / 1024:.1f}KB/min "
            f"{_fmt_thresholds(cfg_state)}")
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

    def enforcement_preconditions():
        """The two ways a block can look fine and do nothing."""
        facts = helper_facts(fw)
        problems = []
        if facts.get("conntrack_tools") != "yes":
            problems.append(
                "conntrack-tools is NOT installed. With flow offloading on, an "
                "in-progress session is forwarded in hardware and never reaches "
                "the REJECT rule, so flushing its conntrack entry is the only "
                "way to cut it. Fix: opkg update && opkg install conntrack-tools")
        if facts.get("flowtable_counter") == "NO":
            problems.append(
                "the fw4 flowtable has no 'counter' flag, so offloaded flows do "
                "not update conntrack byte counters and the state metric will "
                "under-report badly. Fix: disable flow offloading, or upgrade fw4")
        if problems:
            raise UbusError(" | ".join(problems))
        return (f"conntrack-tools present; flowtable counter="
                f"{facts.get('flowtable_counter')}, hw={facts.get('flowtable_hw')}")
    check("enforcement preconditions", enforcement_preconditions)

    def console_visible():
        addrs, how = live_addresses(fw, device)
        cfg_macs = require_macs(device)
        if not addrs:
            raise UbusError(f"none of {len(cfg_macs)} configured MAC(s) is visible "
                            f"on the router — is the console on?")
        return f"{', '.join(addrs)} ({len(cfg_macs)} MAC(s) configured)"
    check("console is visible on the LAN", console_visible)

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
        print(f"facts    : firewall={info.get('firewall')} "
              f"conntrack_tools={info.get('conntrack_tools')} "
              f"ct_acct={info.get('ct_acct')} "
              f"flowtable_counter={info.get('flowtable_counter')} "
              f"offload_hw={info.get('flowtable_hw')} "
              f"openwrt={info.get('openwrt')}")
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
        cfg_state = device.get("state", {})
        if reason:
            print(f"traffic  : no rate available — {reason}")
        else:
            verdict, by = verdict_for(rate_out, rate_in, cfg_state)
            print(f"traffic  : out={rate_out / 1024:.1f} KB/min  in={rate_in / 1024:.1f} KB/min  "
                  f"{_fmt_thresholds(cfg_state)}  -> {verdict}"
                  f"{' (by ' + by + ')' if by else ''}")
    except CountersMissing:
        print("traffic  : counters NOT INSTALLED (run './xbox.py install')")
    except Exception as e:
        print(f"traffic  : unavailable ({e})")

    try:
        addrs = console_addresses(fw, device)
        configured = ", ".join(f"{e['link']}={e['mac']}/{e['ipv4'] or '?'}"
                               for e in interfaces(device)) or "NONE"
        print(f"console  : configured {configured}")
        print(f"           addresses seen on the router: {', '.join(addrs)}")
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

    Every interface is tested, not just the first. Matching on the legacy
    scalar mac/ipv4 meant a console sitting on WiFi was reported as powered
    off, because only the ethernet MAC was ever compared (REVIEW.2 G-02).
    """
    macs = [e["mac"] for e in interfaces(device)]
    ips = static_addresses(device)
    rows = fw.helper_ok("neigh").splitlines()
    hits = [r.strip() for r in rows
            if any(m in r.lower() for m in macs)
            or any(r.startswith(ip + " ") for ip in ips)]
    if not hits:
        print(f"console: NOT present in the router's neighbour table "
              f"(macs={', '.join(macs) or 'none'} "
              f"ips={', '.join(ips) or 'none'}) — powered off, or fully asleep")
        return 1
    for h in hits:
        print(f"console: {h}")
    print("\n(REACHABLE/STALE/DELAY = the router has an address for it; "
          "FAILED/INCOMPLETE = it is not answering)")
    return 0


def cmd_discover(fw, device, args):
    """Find every interface the console has, by hostname and by known MAC.

    A console shows up under one MAC on ethernet and another on WiFi, usually
    differing only in the last octet. v2 recorded whichever was live and
    silently ignored the other (REVIEW.2 G-02), so this looks for all of them:
    any lease whose hostname looks like the console, plus anything already in
    device.json, plus anything sharing the same OUI and near-identical MAC.
    """
    known = {e["mac"]: dict(e) for e in interfaces(device)}
    found = {}

    leases = []
    try:
        leases = fw.helper_ok("leases").splitlines()
    except Exception as e:
        print(f"lease lookup failed: {e}")

    hostname_hint = (device.get("id") or "xbox").lower()
    for line in leases:
        f = line.split()
        if len(f) < 4:
            continue
        mac, ip, host = f[1].lower(), f[2], f[3]
        if not MAC_RE.fullmatch(mac):
            continue
        same_oui = any(m[:8] == mac[:8] for m in known)
        if hostname_hint in host.lower() or mac in known or same_oui:
            found[mac] = {"mac": mac, "ipv4": ip, "link": "?", "hostname": host}

    # Which of them is up right now, and on what
    live, how = live_addresses(fw, device)
    try:
        for line in fw.helper_ok("neigh").splitlines():
            low = line.lower()
            for mac in list(found):
                if mac in low and "failed" not in low:
                    found[mac]["link"] = "up"
    except Exception:
        pass

    if not found:
        print("no console interfaces found. Turn the console on, make it talk to "
              "the network, and try again.")
        return 1

    print("interfaces found:")
    for mac, e in sorted(found.items()):
        mark = "  <- live" if e.get("link") == "up" else ""
        print(f"  {mac}  {e['ipv4']:<15} {e.get('hostname','')}{mark}")
    if live:
        print(f"currently reachable: {', '.join(live)}")

    if not args.write:
        print("\nre-run with --write to record these in device.json")
        return 0

    dev = load_json(DEVICE_FILE)
    merged = dict(known)
    for mac, e in found.items():
        merged.setdefault(mac, {})
        merged[mac].update({"mac": mac, "ipv4": e["ipv4"]})
        merged[mac].setdefault("link", "?")
    dev["interfaces"] = [
        {"mac": m, "ipv4": v.get("ipv4", ""), "link": v.get("link", "?")}
        for m, v in sorted(merged.items())
    ]
    # PRESERVE the provenance comment instead of dropping it (REVIEW.2 G-12).
    dev["comment"] = (f"interfaces discovered {time.strftime('%Y-%m-%d')}: "
                      + "; ".join(f"{e['mac']}={e['ipv4']}" for e in dev["interfaces"])
                      + ". The firewall rule names every MAC, so the block holds "
                        "whether the console is on ethernet or WiFi. "
                      + (dev.get("comment", "") or ""))[:1200]
    with open(DEVICE_FILE, "w") as fh:
        json.dump(dev, fh, indent=2)
        fh.write("\n")
    print(f"\nwrote {len(dev['interfaces'])} interface(s) to {DEVICE_FILE}")
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
        # Both directions: outbound picks threshold_out_bytes_per_min (play vs
        # idle/download), inbound picks threshold_in_bytes_per_min (streaming
        # vs idle). See DESIGN.md §5.
        for key, name in (("out_kb_min", "out"), ("in_kb_min", "in ")):
            vals = sorted(r[key] for r in rows)
            print(f"\n{label} {name}: n={len(vals)}  min={vals[0]:.1f}  "
                  f"median={vals[len(vals) // 2]:.1f}  max={vals[-1]:.1f} KB/min")
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
        # Defaults MUST match config.example.json: v2 shipped 2/7 in code and
        # 1.5/4 in the example, so with no config.json the driver ran on a pair
        # documented nowhere (REVIEW.2 G-07).
        timeout = cfg.get("state_timeout", 1.5)
        deadline = Deadline(cfg.get("state_deadline", 4))
    else:
        timeout = cfg.get("timeout", 10)
        deadline = Deadline(None)

    fw = Router(cfg, device, timeout=timeout, deadline=deadline)

    # 'probe' is the only command that needs no credentials. Without a config
    # the others post session.login with an empty password and surface rpcd's
    # 'permission denied', which reads as a router-side fault when the real one
    # is a missing file on this machine.
    if not cfg and args.command != "probe":
        missing = (f"no {os.path.basename(CONFIG_FILE)} — copy config.example.json to "
                   f"config.json and fill in the rpcd credentials (see DESIGN.md §7).")
        if args.command == "state":
            # one word on stdout is the contract, local fault or not, and the
            # log is the only diagnostic channel upstream does not discard
            print("unknown")
            log(f"state=unknown error={missing}")
            print(f"xbox state unknown: {missing}", file=sys.stderr)
            sys.exit(1)
        sys.exit(missing)

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
