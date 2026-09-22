#!/bin/sh
# ---------------------------------------------------------------------------
# kidsout generic OpenWrt driver — per-device router-side bootstrap.
# RUN THIS ON THE ROUTER, ONCE PER DEVICE.
#
#   sh /tmp/router_bootstrap.sh <id>                 full install (prompts for a password)
#   sh /tmp/router_bootstrap.sh <id> --helper-only   refresh helper + ACL only, no prompt
#   sh /tmp/router_bootstrap.sh <id> --uninstall     remove this device's artefacts only
#
# <id> is the `id` in the device's device.json (= its devices/<name>/ directory
# name): lowercase letters and digits, e.g. xbox, nintendoswitch2.
#
# Upload it, then run it INTERACTIVELY — two separate commands:
#
#   ssh root@<router> 'cat > /tmp/router_bootstrap.sh' < router_bootstrap.sh
#   ssh -t root@<router> sh /tmp/router_bootstrap.sh <id>
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
# It installs three things for the named device and nothing else:
#
#   1. /usr/libexec/kidsout-<id>                 a fixed-verb privileged helper
#   2. /usr/share/rpcd/acl.d/kidsout-<id>.json   an ACL scoped to that helper
#                                                plus read/write on uci 'firewall'
#   3. a 'kidsout-<id>' rpcd login in /etc/config/rpcd, password of your choosing
#
# Every artefact carries the device id, so several devices coexist on one
# router without touching each other's helper, login, ACL or accumulator.
# The helper is byte-identical for every device: it derives the id from its
# own file name, so refreshing it for one device changes nothing for another.
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
# config. To undo one device: sh /tmp/router_bootstrap.sh <id> --uninstall
# (remove its firewall rule first with './driver.sh uninstall' from kidsout).
# ---------------------------------------------------------------------------
set -u

ID="${1:-}"
MODE="${2:-}"

say() { echo "[bootstrap ${ID:-?}] $*"; }
die() { echo "[bootstrap ${ID:-?}] ERROR: $*" >&2; exit 1; }

usage() {
	echo "usage: sh $0 <id> [--helper-only|--uninstall]" >&2
	echo "       <id> = device.json 'id': lowercase letters/digits, e.g. xbox" >&2
	exit 2
}

[ -n "$ID" ] || usage
case "$ID" in
--*) usage ;;
*[!a-z0-9]* | [!a-z]*) die "bad id '$ID': lowercase letters and digits only, starting with a letter" ;;
esac
case "$MODE" in
"" | --helper-only | --uninstall) : ;;
*) usage ;;
esac

[ "$(id -u)" = "0" ] || die "must run as root on the router"

HELPER=/usr/libexec/kidsout-$ID
ACL=/usr/share/rpcd/acl.d/kidsout-$ID.json
ACL_GROUP=kidsout-$ID
RPCD_USER=kidsout-$ID
STATE=/tmp/kidsout-$ID.acct
ADDRS=/tmp/kidsout-$ID.addrs

# --------------------------------------------------------------------------- #
# uninstall — THIS device's artefacts only
# --------------------------------------------------------------------------- #
if [ "$MODE" = "--uninstall" ]; then
	say "removing helper, ACL, rpcd login, accumulator state and system user"
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
	rm -f "$STATE" "$ADDRS"
	# Edit in place via a mode-preserving copy. The obvious
	# `grep -v ... > tmp && mv tmp "$f"` gives the replacement file the
	# shell's umask, which would drop /etc/shadow from 0600 to 0644 and
	# expose every account's hash (REVIEW.2 S2-01). The ':' anchor keeps
	# 'kidsout-xbox:' from matching 'kidsout-xbox2:'.
	for f in /etc/passwd /etc/group /etc/shadow; do
		[ -f "$f" ] || continue
		cp -p "$f" "$f.$RPCD_USER.bak" || continue
		if grep -v "^${RPCD_USER}:" "$f.$RPCD_USER.bak" > "$f"; then
			rm -f "$f.$RPCD_USER.bak"
		else
			cat "$f.$RPCD_USER.bak" > "$f"; rm -f "$f.$RPCD_USER.bak"
		fi
	done
	say "done. helper, ACL, rpcd login, accumulator state and the '$RPCD_USER' user removed."
	say "NOT removed: the firewall rule kidsout_${ID}_out (run './driver.sh uninstall' from kidsout)."
	say "file modes after edit (shadow must be 600):"
	ls -l /etc/shadow 2>/dev/null | sed 's/^/    /'
	exit 0
fi

# --------------------------------------------------------------------------- #
# 1. the helper  (identical for every device; the id comes from its file name)
# --------------------------------------------------------------------------- #
say "installing $HELPER"
mkdir -p /usr/libexec
cat > "$HELPER" <<'HELPER_EOF'
#!/bin/sh
# kidsout per-device privileged helper. Invoked by rpcd `file exec` with a
# fixed verb and validated arguments; never with a shell, so argv is not
# parsed. The device id is the suffix of this file's own name
# (/usr/libexec/kidsout-<id>), so the file is byte-identical for every device
# and each one keeps its own accumulator state.
#
#   info                       router facts: fw3/fw4, offload, conntrack tools
#   counters                   monotonic byte totals for the device ("out N" / "in N")
#   counters-install ADDR...   register the device's addresses, reset totals
#   counters-remove            forget the device; drop accumulator state
#   flush ADDR [ADDR...]       drop conntrack entries for the given addresses
#   neigh                      ipv4 + ipv6 neighbour table
#   leases                     /tmp/dhcp.leases
#
# WHY CONNTRACK AND NOT AN NFT COUNTER (REVIEW.2 G-01)
# Routers running fw4 with flow_offloading_hw=1 (e.g. MediaTek PPE) forward
# offloaded flows in silicon; they never enter ANY netfilter hook, so an nft
# counter in the forward hook (or at netdev ingress) sees only each flow's
# first few packets and under-reports by ~98%. fw4 declares its flowtable
# with `counter`, so the hardware's per-flow MIB is fed back into conntrack
# instead -- verified on such a SoC: 9 of 9 offloaded flows gained bytes
# over 20s. Conntrack is therefore a byte source offload cannot blind, and
# it needs no nft table, no device names and no extra package.
set -u

me=${0##*/}
ID=${me#kidsout-}
if [ -z "$ID" ] || [ "$ID" = "$me" ]; then
	echo "helper must be installed as /usr/libexec/kidsout-<id>, got $0" >&2
	exit 2
fi
STATE=/tmp/kidsout-$ID.acct     # tmpfs: no flash wear, cleared on reboot
ADDRS=/tmp/kidsout-$ID.addrs

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
[ -n "$verb" ] || { echo "usage: $me <verb> [args]" >&2; exit 2; }
shift

case "$verb" in
info)
	echo "device=$ID"
	if have fw4; then echo "firewall=fw4"; elif have fw3; then echo "firewall=fw3"; else echo "firewall=unknown"; fi
	if have nft; then echo "nft=yes"; else echo "nft=no"; fi
	if have conntrack; then echo "conntrack_tools=yes"; else echo "conntrack_tools=no"; fi
	[ -r /proc/net/nf_conntrack ] && echo "proc_nf_conntrack=yes" || echo "proc_nf_conntrack=no"
	echo "ct_acct=$(cat /proc/sys/net/netfilter/nf_conntrack_acct 2>/dev/null || echo unknown)"
	# The facts the accounting design depends on. If a firmware upgrade ever
	# changes them, this is what makes it visible in driver.log rather than
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
	# Measured during real use it produced -510 KB/min (REVIEW.2 G-04).
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
		printf "out %d\n", tot_out
		printf "in %d\n",  tot_in
		printf "source %s\n", src
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
	have conntrack || { echo "conntrack CLI not installed (opkg install conntrack)" >&2; exit 3; }
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
	# 'ip neigh show' already covers both families.
	ip neigh show 2>/dev/null
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
# 2. the ACL  (unquoted heredoc on purpose: $ACL_GROUP/$HELPER/$ID expand)
# --------------------------------------------------------------------------- #
say "installing $ACL"
mkdir -p /usr/share/rpcd/acl.d
cat > "$ACL" <<ACL_EOF
{
	"$ACL_GROUP": {
		"description": "kidsout device '$ID': toggle its own firewall rule, read its own traffic counters",
		"read": {
			"ubus": {
				"session": [ "access", "login" ],
				"system": [ "board" ],
				"uci": [ "get", "changes" ],
				"file": [ "exec" ]
			},
			"uci": [ "firewall" ],
			"file": {
				"$HELPER": [ "exec" ]
			}
		},
		"write": {
			"ubus": {
				"uci": [ "set", "add", "delete", "commit", "apply", "confirm", "revert" ],
				"file": [ "exec" ]
			},
			"uci": [ "firewall" ],
			"file": {
				"$HELPER": [ "exec" ]
			}
		}
	}
}
ACL_EOF
chmod 644 "$ACL"

# Warn about a login that reaches this ACL under a different username (e.g.
# the single 'kidsout' user of the pre-generic Xbox install). It keeps working
# with identical powers; remove it once config.json uses $RPCD_USER.
i=0
while uci -q get "rpcd.@login[$i]" >/dev/null 2>&1; do
	u="$(uci -q get "rpcd.@login[$i].username")"
	if [ "$u" != "$RPCD_USER" ] && uci -q get "rpcd.@login[$i].read" | tr ' ' '\n' | grep -qx "$ACL_GROUP"; then
		say "NOTE: rpcd login '$u' (rpcd.@login[$i]) also grants '$ACL_GROUP'. Once config.json"
		say "      uses username=$RPCD_USER, remove it:  uci delete rpcd.@login[$i]; uci commit rpcd;"
		say "      /etc/init.d/rpcd restart; and drop user '$u' from /etc/passwd, /etc/group, /etc/shadow."
	fi
	i=$((i + 1))
done

if [ "$MODE" = "--helper-only" ]; then
	found=0
	i=0
	while uci -q get "rpcd.@login[$i]" >/dev/null 2>&1; do
		[ "$(uci -q get "rpcd.@login[$i].username")" = "$RPCD_USER" ] && found=1
		i=$((i + 1))
	done
	/etc/init.d/rpcd restart
	sleep 1
	printf '\n'
	say "helper and ACL refreshed; accumulator state in $STATE preserved."
	"$HELPER" info | sed 's/^/    /'
	if [ "$found" = 0 ]; then
		say "WARNING: no rpcd login '$RPCD_USER' exists. Either config.json uses another"
		say "         username that grants '$ACL_GROUP', or run a full bootstrap: sh $0 $ID"
	fi
	exit 0
fi

# --------------------------------------------------------------------------- #
# 3. the rpcd login
# --------------------------------------------------------------------------- #
printf '\n'
say "creating the login-less system user '$RPCD_USER'"
# Many routers have no cryptpw/mkpasswd/openssl, so a password hash cannot be
# computed here. rpcd's '$p$<user>' form defers to /etc/shadow instead, which is
# the same mechanism the existing root login uses. It also means THIS SCRIPT
# NEVER HANDLES THE PASSWORD: passwd(1) prompts you for it directly.
#
# uid/gid: the first free number from 6000, so a second device does not share
# the first one's uid.
if grep -q "^${RPCD_USER}:" /etc/passwd 2>/dev/null; then
	UID_=$(awk -F: -v u="$RPCD_USER" '$1==u{print $3}' /etc/passwd)
else
	UID_=$(awk -F: '$3>=6000 && $3<7000 {u[$3]=1} END{for(i=6000;i<7000;i++) if(!(i in u)){print i;exit}}' /etc/passwd /etc/group)
	[ -n "$UID_" ] || die "no free uid in 6000-6999"
	echo "${RPCD_USER}:x:${UID_}:${UID_}:kidsout rpcd ($ID):/var:/bin/false" >> /etc/passwd
fi
grep -q "^${RPCD_USER}:" /etc/group 2>/dev/null ||
	echo "${RPCD_USER}:x:${UID_}:" >> /etc/group

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
	cp -p /etc/shadow /etc/shadow.$RPCD_USER.bak
	awk -F: -v u="$RPCD_USER" -v h="$pwfield" 'BEGIN{OFS=":"}
		$1==u{$2=h} {print}' /etc/shadow.$RPCD_USER.bak > /etc/shadow
	rm -f /etc/shadow.$RPCD_USER.bak
	cp -p /etc/passwd /etc/passwd.$RPCD_USER.bak
	awk -F: -v u="$RPCD_USER" 'BEGIN{OFS=":"} $1==u{$2="x"} {print}' \
		/etc/passwd.$RPCD_USER.bak > /etc/passwd
	rm -f /etc/passwd.$RPCD_USER.bak
	say "NOTE: that hash was readable by anyone on the router. Set a NEW password now."
	;;
esac

printf '\n'
say "set the password for '$RPCD_USER' — you will be prompted twice."
say "this is NOT the router root password. Pick a fresh one, and put the same"
say "value into devices/$ID/generic-openwrt-driver_files/config.json on the"
say "machine that runs kidsout (username: $RPCD_USER)."
printf '\n'
passwd "$RPCD_USER" < /dev/tty || die "passwd failed; '$RPCD_USER' has no password"
STORED="\$p\$${RPCD_USER}"

# replace any existing login section for this user, leave every other login alone
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
uci add_list "rpcd.$S.read=$ACL_GROUP"
uci add_list "rpcd.$S.write=$ACL_GROUP"
uci commit rpcd
say "rpcd login '$RPCD_USER' written to /etc/config/rpcd"

/etc/init.d/rpcd restart
sleep 1

# --------------------------------------------------------------------------- #
# 4. conntrack CLI (OpenWrt package 'conntrack' — NOT 'conntrack-tools', which does not exist)
# --------------------------------------------------------------------------- #
# Without it the block cannot cut a session already in progress: fw4's forward
# chain accepts established flows before any user rule, and the established-TCP
# timeout is typically 7440 s. "Blocked" would mean "blocked within two hours"
# for whoever is already mid-session.
if command -v conntrack >/dev/null 2>&1; then
	say "conntrack CLI already installed"
else
	say "installing package conntrack (needed to cut in-progress sessions)"
	opkg update >/dev/null 2>&1 || say "warning: opkg update failed"
	if opkg install conntrack >/dev/null 2>&1; then
		say "conntrack installed"
	else
		say "WARNING: could not install package conntrack. Blocking will still stop"
		say "         NEW connections, but a session already running survives for up"
		say "         to 2 hours. Install it by hand, then re-run ./driver.sh selftest."
	fi
fi

# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
printf '\n'
say "router facts (feed these back to the driver):"
"$HELPER" info | sed 's/^/    /'
printf '\n'
say "done. Next, on the kidsout machine, in devices/$ID/generic-openwrt-driver_files/:"
say "  cp ../../../generic-openwrt-driver/config.example.json config.json && chmod 600 config.json"
say "  # set username=$RPCD_USER and the password you just chose"
say "  ./driver.sh selftest"
