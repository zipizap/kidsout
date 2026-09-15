#!/bin/sh
# ---------------------------------------------------------------------------
# kidsout xbox — one-time router-side bootstrap.   RUN THIS ON THE ROUTER.
#
# Upload it, then run it INTERACTIVELY — two separate commands:
#
#   ssh root@192.168.2.1 'cat > /tmp/router_bootstrap.sh' < router_bootstrap.sh
#   ssh -t root@192.168.2.1 sh /tmp/router_bootstrap.sh
#
# Do NOT pipe the script into ssh (`ssh -t ... 'sh -s' < router_bootstrap.sh`).
# ssh refuses to allocate a pseudo-terminal when its stdin is a redirect, so
# the remote side has no /dev/tty and passwd(1) cannot prompt — the run dies
# at the password step. The script must already be ON the router so that ssh's
# stdin stays a real terminal.
#
# `scp` may not work either: OpenWrt images without openssh-sftp-server have no
# /usr/libexec/sftp-server, which is why the upload above uses `cat >`.
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
	nft delete table inet kidsout 2>/dev/null   # pre-v3 accounting table, if present
	rm -f /tmp/kidsout-xbox.acct /tmp/kidsout-xbox.addrs
	# Edit in place via a mode-preserving copy. The obvious
	# `grep -v ... > tmp && mv tmp "$f"` gives the replacement file the
	# shell's umask, which would drop /etc/shadow from 0600 to 0644 and
	# expose every account's hash (REVIEW.2 S2-01).
	for f in /etc/passwd /etc/group /etc/shadow; do
		[ -f "$f" ] || continue
		cp -p "$f" "$f.kidsout.bak" || continue
		if grep -v "^${RPCD_USER}:" "$f.kidsout.bak" > "$f"; then
			rm -f "$f.kidsout.bak"
		else
			cat "$f.kidsout.bak" > "$f"; rm -f "$f.kidsout.bak"
		fi
	done
	say "done. helper, ACL, rpcd login, accounting state and the '$RPCD_USER' user removed."
	say "file modes after edit (shadow must be 600):"
	ls -l /etc/shadow 2>/dev/null | sed 's/^/    /' 
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
#   info                       router facts: fw3/fw4, offload, conntrack tools
#   counters                   monotonic byte totals for the console
#   counters-install ADDR...   register the console's addresses, reset totals
#   counters-remove            forget the console; drop accumulator state
#   flush ADDR [ADDR...]       drop conntrack entries for the given addresses
#   neigh                      ipv4 + ipv6 neighbour table
#   leases                     /tmp/dhcp.leases
#
# WHY CONNTRACK AND NOT AN NFT COUNTER (REVIEW.2 G-01)
# This router runs fw4 with flow_offloading_hw=1 on a MediaTek PPE. Offloaded
# flows are forwarded in silicon and never enter ANY netfilter hook, so an nft
# counter in the forward hook (or at netdev ingress) sees only each flow's
# first few packets and under-reports gameplay by ~98%. fw4 declares its
# flowtable with `counter`, so the hardware's per-flow MIB is fed back into
# conntrack instead -- verified on this SoC: 9 of 9 offloaded flows gained
# bytes over 20s. Conntrack is therefore a byte source offload cannot blind,
# and it needs no nft table, no device names and no extra package.
set -u

STATE=/tmp/kidsout-xbox.acct     # tmpfs: no flash wear, cleared on reboot
ADDRS=/tmp/kidsout-xbox.addrs

valid_mac() {
	case "$1" in
	[0-9a-fA-F][0-9a-fA-F]:[0-9a-fA-F][0-9a-fA-F]:[0-9a-fA-F][0-9a-fA-F]:[0-9a-fA-F][0-9a-fA-F]:[0-9a-fA-F][0-9a-fA-F]:[0-9a-fA-F][0-9a-fA-F]) return 0 ;;
	*) return 1 ;;
	esac
}

# Addresses may contain only hex digits, dots and colons -- enough for v4 and
# v6, and nothing that could be mistaken for an option or a path. Note this
# deliberately rejects a /prefix: no caller passes a CIDR (REVIEW.2 G-13).
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
	if have nft; then echo "nft=yes"; else echo "nft=no"; fi
	if have conntrack; then echo "conntrack_tools=yes"; else echo "conntrack_tools=no"; fi
	[ -r /proc/net/nf_conntrack ] && echo "proc_nf_conntrack=yes" || echo "proc_nf_conntrack=no"
	echo "ct_acct=$(cat /proc/sys/net/netfilter/nf_conntrack_acct 2>/dev/null || echo unknown)"
	# The three facts the accounting design depends on. If a firmware upgrade
	# ever changes them, this is what makes it visible in xbox.log rather than
	# showing up as a silent permanent 'down' (REVIEW.2 Q4).
	if nft list flowtable inet fw4 ft 2>/dev/null | grep -q "counter"; then
		echo "flowtable_counter=yes"
	elif nft list flowtables 2>/dev/null | grep -q flowtable; then
		echo "flowtable_counter=NO"
	else
		echo "flowtable_counter=none"
	fi
	if nft list flowtable inet fw4 ft 2>/dev/null | grep -q "flags offload"; then
		echo "flowtable_hw=yes"
	else
		echo "flowtable_hw=no"
	fi
	echo "registered_addrs=$(cat "$ADDRS" 2>/dev/null | tr '\n' ' ')"
	echo "openwrt=$(. /etc/openwrt_release 2>/dev/null; echo "${DISTRIB_RELEASE:-unknown}")"
	echo "ipv6_wan=$(ip -6 route show default 2>/dev/null | head -n1 | wc -l)"
	;;

counters)
	[ -s "$ADDRS" ] || { echo "no addresses registered" >&2; exit 4; }

	# Fold the live conntrack table into a monotonic accumulator.
	#
	# A sum over live flows is NOT a counter: entries vanish when a flow
	# expires and take their bytes with them, so a naive sum goes DOWN.
	# Measured during real gameplay it produced -510 KB/min (REVIEW.2 G-04).
	# So: for each flow still present, add only what it gained since we last
	# looked; for a flow we have not seen before, add all of it; a flow that
	# disappeared simply stops contributing. The running total never
	# decreases, which is exactly what the driver's sample_rate() expects.
	#
	# Prefer `conntrack -L -o id`: the id is unique per entry, so a reused
	# 5-tuple is correctly seen as a NEW flow rather than as a counter reset.
	# /proc/net/nf_conntrack has no id column, hence the tuple-keyed fallback.
	# Decide the source BEFORE the pipeline: `awk -v src="$src"` is expanded
	# when the pipeline is built, so assigning src inside it comes too late.
	if have conntrack && conntrack -L -o id >/dev/null 2>&1; then
		src=conntrack
	else
		src=proc
	fi
	{ if [ "$src" = conntrack ]; then
		conntrack -L -o id 2>/dev/null
	  else
		cat /proc/net/nf_conntrack 2>/dev/null
	  fi
	} | awk -v addrfile="$ADDRS" -v statefile="$STATE" -v src="$src" '
	BEGIN {
		while ((getline a < addrfile) > 0) if (a != "") addr[a] = 1
		close(addrfile)
		tot_out = 0; tot_in = 0
		while ((getline line < statefile) > 0) {
			n = split(line, f, " ")
			if (f[1] == "#total" && n >= 3) { tot_out = f[2] + 0; tot_in = f[3] + 0 }
			else if (n >= 3) { po[f[1]] = f[2] + 0; pi[f[1]] = f[3] + 0 }
		}
		close(statefile)
	}
	{
		# Match the ORIGINAL tuple only, anchored on src=/dst=, so
		# 192.168.2.17 cannot match 192.168.2.172 and a remote host NATed
		# toward us is not counted as ours.
		osrc = ""; odst = ""; osp = ""; odp = ""; id = ""
		dir = 0; o = 0; i2 = 0
		for (k = 1; k <= NF; k++) {
			if ($k ~ /^src=/) {
				split($k, a, "="); if (osrc == "") osrc = a[2]
				dir = (a[2] in addr) ? 1 : 2
			}
			else if ($k ~ /^dst=/)   { split($k, a, "="); if (odst == "") odst = a[2] }
			else if ($k ~ /^sport=/) { split($k, a, "="); if (osp  == "") osp  = a[2] }
			else if ($k ~ /^dport=/) { split($k, a, "="); if (odp  == "") odp  = a[2] }
			else if ($k ~ /^id=/)    { split($k, a, "="); id = a[2] }
			else if ($k ~ /^bytes=/) {
				split($k, a, "=")
				if (dir == 1) o += a[2]; else i2 += a[2]
			}
		}
		if (!(osrc in addr) && !(odst in addr)) next

		key = (id != "") ? "i" id : "t" osrc ":" osp ">" odst ":" odp
		if (key in po) {
			d = o - po[key];  if (d > 0) tot_out += d
			e = i2 - pi[key]; if (e > 0) tot_in  += e
		} else {
			tot_out += o; tot_in += i2
		}
		co[key] = o; ci[key] = i2; seen[key] = 1
	}
	END {
		tmp = statefile ".tmp"
		printf "#total %d %d\n", tot_out, tot_in > tmp
		for (k in seen) printf "%s %d %d\n", k, co[k], ci[k] >> tmp
		close(tmp)
		system("mv " tmp " " statefile)
		printf "xbox_out %d\n", tot_out
		printf "xbox_in %d\n",  tot_in
		printf "source %s\n",   src
	}'
	;;

counters-install)
	[ $# -gt 0 ] || { echo "counters-install needs at least one address" >&2; exit 2; }
	for a in "$@"; do
		valid_addr "$a" || { echo "bad address: $a" >&2; exit 2; }
	done
	# Idempotent: if the same address set is already registered, keep the
	# accumulator so a no-op install does not force an 'unknown' tick.
	new=$(for a in "$@"; do echo "$a"; done | sort -u)
	old=$(sort -u "$ADDRS" 2>/dev/null)
	if [ "$new" = "$old" ]; then
		# Same address set: leave the accumulator alone so a re-install does
		# not cost the driver an 'unknown' tick.
		echo "unchanged"
		exit 0
	fi
	echo "$new" > "$ADDRS" || exit 5
	rm -f "$STATE"
	echo "installed"
	;;

counters-remove)
	rm -f "$STATE" "$ADDRS"
	echo "removed"
	;;

flush)
	have conntrack || { echo "conntrack-tools not installed" >&2; exit 3; }
	[ $# -gt 0 ] || { echo "flush needs at least one address" >&2; exit 2; }
	# Count ENTRIES actually destroyed, not commands that succeeded. conntrack
	# reports "N flow entries have been deleted." on stderr; summing that is
	# the difference between "cut a live session" and "there was nothing to
	# cut", which is exactly what the operator needs to know.
	n=0
	for a in "$@"; do
		valid_addr "$a" || { echo "bad address: $a" >&2; exit 2; }
		for d in -s -d; do
			c=$(conntrack -D "$d" "$a" 2>&1 >/dev/null \
			    | sed -n 's/.*: \([0-9][0-9]*\) flow entries have been deleted.*/\1/p')
			[ -n "$c" ] && n=$((n + c))
		done
	done
	echo "flushed $n conntrack entr$([ "$n" = 1 ] && echo y || echo ies)"
	;;

neigh)
	ip neigh show 2>/dev/null
	ip -6 neigh show 2>/dev/null
	;;

leases)
	cat /tmp/dhcp.leases 2>/dev/null
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

# Create the /etc/shadow entry BEFORE calling passwd(1). Without it busybox
# passwd prints "no record of <user> in /etc/shadow, using /etc/passwd" and
# writes the hash straight into /etc/passwd -- which is world-readable (0644),
# whereas /etc/shadow is 0600. That would leave the credential's hash exposed
# to every process on the router.
if ! grep -q "^${RPCD_USER}:" /etc/shadow 2>/dev/null; then
	umask 077
	echo "${RPCD_USER}:!:$(( $(date +%s) / 86400 )):0:99999:7:::" >> /etc/shadow
	umask 022
fi
# Belt and braces: if a previous run already put a hash in /etc/passwd, move it
# out and restore the placeholder.
pwfield=$(awk -F: -v u="$RPCD_USER" '$1==u{print $2}' /etc/passwd 2>/dev/null)
case "$pwfield" in
x | "!" | "*" | "") : ;;
*)
	say "moving an exposed password hash out of world-readable /etc/passwd"
	cp -p /etc/shadow /etc/shadow.kidsout.bak
	awk -F: -v u="$RPCD_USER" -v h="$pwfield" 'BEGIN{OFS=":"}
		$1==u{$2=h} {print}' /etc/shadow.kidsout.bak > /etc/shadow
	rm -f /etc/shadow.kidsout.bak
	cp -p /etc/passwd /etc/passwd.kidsout.bak
	awk -F: -v u="$RPCD_USER" 'BEGIN{OFS=":"} $1==u{$2="x"} {print}' \
		/etc/passwd.kidsout.bak > /etc/passwd
	rm -f /etc/passwd.kidsout.bak
	say "NOTE: that hash was readable by anyone on the router. Set a NEW password now."
	;;
esac

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
	if opkg install conntrack >/dev/null 2>&1; then
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
