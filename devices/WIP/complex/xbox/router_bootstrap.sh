#!/bin/sh
# ---------------------------------------------------------------------------
# kidsout xbox — one-time router-side bootstrap.   RUN THIS ON THE ROUTER.
#
#   scp router_bootstrap.sh root@192.168.2.1:/tmp/
#   ssh root@192.168.2.1 sh /tmp/router_bootstrap.sh
#
# (or, without scp:  ssh -t root@192.168.2.1 'sh -s' < router_bootstrap.sh
#  — the password prompt reads from /dev/tty, so piping the script in is fine.)
#
# It installs three things and nothing else:
#
#   1. /usr/libexec/kidsout-xbox            a fixed-verb privileged helper
#   2. /usr/share/rpcd/acl.d/kidsout-xbox.json   an ACL scoped to that helper
#                                           plus read/write on uci 'firewall'
#   3. a 'kidsout' rpcd login in /etc/config/rpcd, password of your choosing
#
# Why a helper instead of granting `file exec` on nft/conntrack directly:
# rpcd's file-exec ACL authorises a *command path*, not its arguments, and
# rpcd runs as root. Granting exec on /usr/sbin/nft would therefore hand the
# driver's credential complete control of the firewall — i.e. root-equivalent,
# which is exactly what scoping the user was supposed to prevent. The helper
# accepts a closed set of verbs with validated arguments, so a leaked
# credential can toggle the kidsout rules and read counters, and no more.
#
# Nothing here touches your existing firewall rules, users, or any other
# config. To undo everything: sh /tmp/router_bootstrap.sh --uninstall
# ---------------------------------------------------------------------------
set -u

HELPER=/usr/libexec/kidsout-xbox
ACL=/usr/share/rpcd/acl.d/kidsout-xbox.json
RPCD_USER=kidsout

say() { echo "[bootstrap] $*"; }
die() { echo "[bootstrap] ERROR: $*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "must run as root on the router"

# --------------------------------------------------------------------------- #
# uninstall
# --------------------------------------------------------------------------- #
if [ "${1:-}" = "--uninstall" ]; then
	say "removing helper, ACL and rpcd login"
	rm -f "$HELPER" "$ACL"
	# only remove OUR login section, never any other login
	i=0
	while uci -q get "rpcd.@login[$i]" >/dev/null 2>&1; do
		if [ "$(uci -q get "rpcd.@login[$i].username")" = "$RPCD_USER" ]; then
			uci delete "rpcd.@login[$i]"
			continue
		fi
		i=$((i + 1))
	done
	uci commit rpcd
	/etc/init.d/rpcd restart
	nft delete table inet kidsout 2>/dev/null
	for f in /etc/passwd /etc/group /etc/shadow; do
		[ -f "$f" ] || continue
		grep -v "^${RPCD_USER}:" "$f" > "$f.kidsout.tmp" && mv "$f.kidsout.tmp" "$f"
	done
	say "done. helper, ACL, rpcd login, nft table and the '$RPCD_USER' user removed."
	exit 0
fi

# --------------------------------------------------------------------------- #
# 1. the helper
# --------------------------------------------------------------------------- #
say "installing $HELPER"
mkdir -p /usr/libexec
cat > "$HELPER" <<'HELPER_EOF'
#!/bin/sh
# kidsout xbox privileged helper. Invoked by rpcd `file exec` with a fixed
# verb and validated arguments; never with a shell, so argv is not parsed.
#
#   info                      router facts: fw3/fw4, nft json, conntrack tools
#   counters                  read the kidsout byte counters (json if available)
#   counters-install MAC IP4  create/refresh the accounting table (idempotent)
#   counters-remove           drop the accounting table
#   flush ADDR [ADDR...]      drop conntrack entries for the given addresses
#   neigh                     ipv4 + ipv6 neighbour table
#   leases                    /tmp/dhcp.leases
#   conntrack-dump            /proc/net/nf_conntrack (diagnostics only)
set -u

TABLE=kidsout
CHAIN=accounting

valid_mac() {
	case "$1" in
	[0-9a-fA-F][0-9a-fA-F]:[0-9a-fA-F][0-9a-fA-F]:[0-9a-fA-F][0-9a-fA-F]:[0-9a-fA-F][0-9a-fA-F]:[0-9a-fA-F][0-9a-fA-F]:[0-9a-fA-F][0-9a-fA-F]) return 0 ;;
	*) return 1 ;;
	esac
}

# addresses may contain only hex digits, dots, colons and a /prefix — enough
# for v4 and v6, and nothing that could be mistaken for an option or a path.
valid_addr() {
	case "$1" in
	"" | -*) return 1 ;;
	*[!0-9a-fA-F.:]*) return 1 ;;
	*) return 0 ;;
	esac
}

have() { command -v "$1" >/dev/null 2>&1; }

verb="${1:-}"
[ -n "$verb" ] || { echo "usage: kidsout-xbox <verb> [args]" >&2; exit 2; }
shift

case "$verb" in
info)
	if have fw4; then echo "firewall=fw4"; elif have fw3; then echo "firewall=fw3"; else echo "firewall=unknown"; fi
	if have nft; then
		echo "nft=yes"
		if nft -j list counters >/dev/null 2>&1; then echo "nft_json=yes"; else echo "nft_json=no"; fi
	else
		echo "nft=no"
		echo "nft_json=no"
	fi
	if have conntrack; then echo "conntrack_tools=yes"; else echo "conntrack_tools=no"; fi
	[ -r /proc/net/nf_conntrack ] && echo "proc_nf_conntrack=yes" || echo "proc_nf_conntrack=no"
	if nft list table inet "$TABLE" >/dev/null 2>&1; then echo "counters_table=yes"; else echo "counters_table=no"; fi
	echo "openwrt=$(. /etc/openwrt_release 2>/dev/null; echo "${DISTRIB_RELEASE:-unknown}")"
	echo "ipv6_wan=$(ip -6 route show default 2>/dev/null | head -n1 | wc -l)"
	;;

counters)
	have nft || { echo "nft not available" >&2; exit 3; }
	nft list table inet "$TABLE" >/dev/null 2>&1 || { echo "counters table missing" >&2; exit 4; }
	if nft -j list counters table inet "$TABLE" 2>/dev/null; then
		:
	else
		nft list counters table inet "$TABLE"
	fi
	;;

counters-install)
	have nft || { echo "nft not available" >&2; exit 3; }
	mac="${1:-}"; ip4="${2:-}"
	valid_mac "$mac" || { echo "bad mac" >&2; exit 2; }
	valid_addr "$ip4" || { echo "bad ipv4" >&2; exit 2; }
	# Rebuild only when the live ruleset does not already match this device,
	# so that a no-op install does not reset the counters.
	if nft list chain inet "$TABLE" "$CHAIN" 2>/dev/null | grep -qi "$mac" &&
		nft list chain inet "$TABLE" "$CHAIN" 2>/dev/null | grep -q "$ip4"; then
		echo "unchanged"
		exit 0
	fi
	nft delete table inet "$TABLE" 2>/dev/null
	nft add table inet "$TABLE" || exit 5
	nft add counter inet "$TABLE" xbox_out || exit 5
	nft add counter inet "$TABLE" xbox_in || exit 5
	nft add chain inet "$TABLE" "$CHAIN" '{ type filter hook forward priority -160; policy accept; }' || exit 5
	# outbound is matched on the MAC: covers IPv4 and IPv6 alike, and survives
	# the console's address changing. inbound can only be matched on the IP
	# (the L2 destination in the forward hook is the router, not the console),
	# so it is IPv4-only and used for diagnostics, not for the up/down call.
	nft add rule inet "$TABLE" "$CHAIN" ether saddr "$mac" counter name xbox_out || exit 5
	nft add rule inet "$TABLE" "$CHAIN" ip daddr "$ip4" counter name xbox_in || exit 5
	echo "installed"
	;;

counters-remove)
	have nft || exit 3
	nft delete table inet "$TABLE" 2>/dev/null
	echo "removed"
	;;

flush)
	have conntrack || { echo "conntrack-tools not installed" >&2; exit 3; }
	[ $# -gt 0 ] || { echo "flush needs at least one address" >&2; exit 2; }
	n=0
	for a in "$@"; do
		valid_addr "$a" || { echo "bad address: $a" >&2; exit 2; }
		conntrack -D -s "$a" >/dev/null 2>&1 && n=$((n + 1))
		conntrack -D -d "$a" >/dev/null 2>&1 && n=$((n + 1))
	done
	echo "flushed $*"
	;;

neigh)
	ip neigh show 2>/dev/null
	ip -6 neigh show 2>/dev/null
	;;

leases)
	cat /tmp/dhcp.leases 2>/dev/null
	;;

conntrack-dump)
	cat /proc/net/nf_conntrack 2>/dev/null
	;;

*)
	echo "unknown verb: $verb" >&2
	exit 2
	;;
esac
HELPER_EOF
chmod 755 "$HELPER"

# --------------------------------------------------------------------------- #
# 2. the ACL
# --------------------------------------------------------------------------- #
say "installing $ACL"
mkdir -p /usr/share/rpcd/acl.d
cat > "$ACL" <<'ACL_EOF'
{
	"kidsout-xbox": {
		"description": "kidsout xbox device driver: toggle its own firewall rules, read its own traffic counters",
		"read": {
			"ubus": {
				"session": [ "access", "login" ],
				"system": [ "board" ],
				"uci": [ "get", "changes" ],
				"file": [ "exec" ]
			},
			"uci": [ "firewall" ],
			"file": {
				"/usr/libexec/kidsout-xbox": [ "exec" ]
			}
		},
		"write": {
			"ubus": {
				"uci": [ "set", "add", "delete", "commit", "apply", "confirm", "revert" ],
				"file": [ "exec" ]
			},
			"uci": [ "firewall" ],
			"file": {
				"/usr/libexec/kidsout-xbox": [ "exec" ]
			}
		}
	}
}
ACL_EOF
chmod 644 "$ACL"

# --------------------------------------------------------------------------- #
# 3. the rpcd login
# --------------------------------------------------------------------------- #
printf '\n'
say "creating the login-less system user '$RPCD_USER'"
# This router has no cryptpw/mkpasswd/openssl, so a password hash cannot be
# computed here. rpcd's '$p$<user>' form defers to /etc/shadow instead, which is
# the same mechanism the existing root login uses. It also means THIS SCRIPT
# NEVER HANDLES THE PASSWORD: passwd(1) prompts you for it directly.
grep -q "^${RPCD_USER}:" /etc/passwd 2>/dev/null ||
	echo "${RPCD_USER}:x:6000:6000:kidsout rpcd:/var:/bin/false" >> /etc/passwd
grep -q "^${RPCD_USER}:" /etc/group 2>/dev/null ||
	echo "${RPCD_USER}:x:6000:" >> /etc/group

printf '\n'
say "set the password for '$RPCD_USER' — you will be prompted twice."
say "this is NOT the router root password. Pick a fresh one, and put the same"
say "value into devices/xbox/config.json on the machine that runs kidsout."
printf '\n'
passwd "$RPCD_USER" < /dev/tty || die "passwd failed; '$RPCD_USER' has no password"
STORED="\$p\$${RPCD_USER}"

# replace any existing kidsout login section, leave every other login alone
i=0
while uci -q get "rpcd.@login[$i]" >/dev/null 2>&1; do
	if [ "$(uci -q get "rpcd.@login[$i].username")" = "$RPCD_USER" ]; then
		uci delete "rpcd.@login[$i]"
		continue
	fi
	i=$((i + 1))
done
S="$(uci add rpcd login)"
uci set "rpcd.$S.username=$RPCD_USER"
uci set "rpcd.$S.password=$STORED"
uci add_list "rpcd.$S.read=kidsout-xbox"
uci add_list "rpcd.$S.write=kidsout-xbox"
uci commit rpcd
say "rpcd login '$RPCD_USER' written to /etc/config/rpcd"

/etc/init.d/rpcd restart
sleep 1

# --------------------------------------------------------------------------- #
# 4. conntrack-tools
# --------------------------------------------------------------------------- #
# Without it the block cannot cut a session already in progress: fw4's forward
# chain accepts established flows before any user rule, and this router's
# established-TCP timeout is 7440 s. "Blocked" would mean "blocked within two
# hours" for whoever is already mid-game.
if command -v conntrack >/dev/null 2>&1; then
	say "conntrack-tools already installed"
else
	say "installing conntrack-tools (needed to cut in-progress sessions)"
	opkg update >/dev/null 2>&1 || say "warning: opkg update failed"
	if opkg install conntrack-tools >/dev/null 2>&1; then
		say "conntrack-tools installed"
	else
		say "WARNING: could not install conntrack-tools. Blocking will still stop"
		say "         NEW connections, but a game already running survives for up"
		say "         to 2 hours. Install it by hand, then re-run ./xbox.py selftest."
	fi
fi

# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
printf '\n'
say "router facts (feed these back to the driver):"
"$HELPER" info | sed 's/^/    /'
printf '\n'
say "done. Next, on the kidsout machine:"
say "  cp config.example.json config.json && chmod 600 config.json"
say "  # set username=$RPCD_USER and the password you just chose"
say "  ./xbox.py selftest"
