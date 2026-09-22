sh <<'KIDSOUT_CHECK_B1'
XIP=192.168.2.172
XMAC=d8:e2:df:92:a9:93
s() { echo; echo "##### $* #####"; }

s B1.1 FLOWTABLE '(the big question: does offload bypass my counters?)'
echo "-- firewall defaults --"
uci show firewall.@defaults[0] 2>/dev/null
echo "flow_offloading    = $(uci -q get firewall.@defaults[0].flow_offloading)"
echo "flow_offloading_hw = $(uci -q get firewall.@defaults[0].flow_offloading_hw)"
echo "-- flowtables --"
nft list flowtables 2>/dev/null || echo "(none)"
echo "-- flowtable detail (devices + flags; 'flags offload' means HARDWARE) --"
nft list flowtable inet fw4 ft 2>/dev/null || echo "(no flowtable named ft)"
echo "-- DECISIVE: does the flowtable carry 'counter'? --"
echo "   (if yes, offloaded flows still update conntrack byte accounting,"
echo "    which makes conntrack a viable byte source even under offload)"
nft list flowtable inet fw4 ft 2>/dev/null | grep -E "counter|flags offload|devices" \
  || echo "   (none of counter/flags/devices found)"
echo "-- the flow add rule --"
nft list chain inet fw4 forward 2>/dev/null | grep -n "flow add\|flow offload"
echo "-- MediaTek PPE / hardware offload engine present? --"
lsmod 2>/dev/null | grep -iE "mtk|ppe|flow" | head -5 || echo "(no matching modules)"
ls /sys/kernel/debug/ 2>/dev/null | grep -i "ppe\|hnat" || echo "(no ppe/hnat debugfs)"

s B1.2 ARE FLOWS ACTUALLY OFFLOADED RIGHT NOW
echo "total conntrack entries : $(wc -l < /proc/net/nf_conntrack)"
echo "marked [OFFLOAD]        : $(grep -c OFFLOAD /proc/net/nf_conntrack)"
echo "marked [HW_OFFLOAD]     : $(grep -c HW_OFFLOAD /proc/net/nf_conntrack)"
echo "-- one offloaded entry, addresses masked --"
grep OFFLOAD /proc/net/nf_conntrack 2>/dev/null | head -1 | sed -e 's/[0-9]\{1,3\}\.[0-9]\{1,3\}\.[0-9]\{1,3\}\.[0-9]\{1,3\}/<ip>/g'

s B1.3 CONNTRACK ACCOUNTING '(a possible alternative byte source)'
sysctl net.netfilter.nf_conntrack_acct 2>/dev/null || \
  echo "nf_conntrack_acct = $(cat /proc/sys/net/netfilter/nf_conntrack_acct 2>/dev/null)"
echo "-- does a conntrack line carry bytes= ? --"
head -1 /proc/net/nf_conntrack | grep -o "bytes=[0-9]*" | head -2 || echo "(no bytes= field: accounting is OFF)"

s B1.4 NETDEV INGRESS DRY RUN '(the offload-proof accounting point)'
echo "-- devices --"; ls /sys/class/net/ | tr '\n' ' '; echo
echo "-- br-lan ports --"; ls /sys/class/net/br-lan/brif/ 2>/dev/null | tr '\n' ' '; echo
echo "-- which port is the console on right now --"
XPORT=$(bridge fdb show 2>/dev/null | grep -i "$XMAC" | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1)}' | head -1)
echo "console port = ${XPORT:-(not in fdb - console idle or absent)}"

# Build the real multi-device list from the bridge's own ports. Hooking br-lan
# is wrong: netdev ingress is a per-netdevice hook, and the flowtable attaches
# to the PHYSICAL lower devices, so we must hook those to run before it.
DEVS=$(for d in $(ls /sys/class/net/br-lan/brif/ 2>/dev/null); do printf '"%s", ' "$d"; done | sed 's/, $//')
echo "device list  = { $DEVS }"

echo "-- dry run A: multi-device ingress at priority -300 (before flowtable at 0) --"
if [ -n "$DEVS" ]; then
	nft -c -f - <<NFT && echo "DRY RUN OK: multi-device netdev ingress accepted" || echo "DRY RUN FAILED"
table netdev kidsout_syntax_check {
	counter xbox_out { }
	chain ingress {
		type filter hook ingress devices = { $DEVS } priority -300; policy accept;
		ether saddr $XMAC counter name xbox_out
	}
}
NFT
else
	echo "(skipped: no bridge ports enumerated)"
fi

echo "-- dry run B: netdev EGRESS (would give a MAC-keyed INBOUND counter) --"
if [ -n "$DEVS" ]; then
	nft -c -f - <<NFT && echo "DRY RUN OK: netdev egress accepted (kernel >= 5.16)" || echo "DRY RUN FAILED: no egress hook"
table netdev kidsout_syntax_check3 {
	counter xbox_in { }
	chain egress {
		type filter hook egress devices = { $DEVS } priority -300; policy accept;
		ether daddr $XMAC counter name xbox_in
	}
}
NFT
else
	echo "(skipped: no bridge ports enumerated)"
fi

s B1.5 EARLY-DROP DRY RUN '(enforcement above fw4 established-accept)'
nft -c -f - <<'NFT' && echo "DRY RUN OK: own forward chain at priority -160 accepted" || echo "DRY RUN FAILED"
table inet kidsout_syntax_check2 {
	chain enforce {
		type filter hook forward priority -160; policy accept;
		ether saddr d8:e2:df:92:a9:93 drop
	}
}
NFT
echo "-- what currently occupies forward priority mangle (-150) --"
nft -a list ruleset 2>/dev/null | grep -B4 "hook forward priority mangle" | grep -E "^table|chain" | head -4

s B1.6 USER / SHADOW PREREQUISITES '(no contents printed)'
echo "/etc/shadow exists     : $([ -f /etc/shadow ] && echo yes || echo NO)"
echo "/etc/passwd writable   : $([ -w /etc/passwd ] && echo yes || echo NO)"
echo "user 'kidsout' exists  : $(grep -q '^kidsout:' /etc/passwd && echo YES || echo no)"
echo "uid 6000 free          : $(cut -d: -f3 /etc/passwd | grep -qx 6000 && echo NO || echo yes)"
echo "gid 6000 free          : $(cut -d: -f3 /etc/group  | grep -qx 6000 && echo NO || echo yes)"
echo "passwd applet          : $(busybox passwd --help 2>&1 | head -1)"
echo "shadow entries (count) : $(wc -l < /etc/shadow 2>/dev/null)"

s B1.7 RPCD FILE-EXEC CAPABILITY
echo "-- methods on the ubus 'file' object --"
ubus -v list file 2>/dev/null | grep -E '^\s*"?(exec|read|stat|list)' | head -10
ubus -v list file 2>/dev/null | head -20
echo "-- ACLs that grant exec (schema reference for my ACL) --"
grep -l '"exec"' /usr/share/rpcd/acl.d/*.json 2>/dev/null
for f in $(grep -l '"exec"' /usr/share/rpcd/acl.d/*.json 2>/dev/null | head -1); do
	echo "--- $f ---"; cat "$f"
done

s B1.8 PACKAGE AVAILABILITY
echo "-- is conntrack-tools in the cached package lists? --"
opkg list conntrack-tools 2>/dev/null || echo "(no cached list; 'opkg update' needed first)"
ls -la /var/opkg-lists/ 2>/dev/null | head -5
echo "-- overlay free --"; df -h /overlay | tail -1

s B1.10 WILDCARD ZONE SUPPORT '(does fw4 accept dest=*?)'
echo "-- wildcard handling in fw4 ucode --"
grep -n "'\*'" /usr/share/ucode/fw4.uc 2>/dev/null | head -12
echo "-- zone_ref / any-zone parsing --"
grep -n -E "parse_zone_ref|ANY|any_zone|wildcard" /usr/share/ucode/fw4.uc 2>/dev/null | head -12

s B1.9 UHTTPD REDIRECT
uci show uhttpd.main 2>/dev/null | grep -E "redirect|rfc1918|ubus"

s B1.11 DO OFFLOADED FLOWS STILL COUNT BYTES '(settles the whole design)'
# If conntrack byte counters keep moving for flows marked [OFFLOAD]/[HW_OFFLOAD],
# then conntrack is a byte source that offload cannot blind -- which is the
# cheapest possible fix, needing no nft table and no device names at all.
echo "-- accounting enabled? --"
echo "nf_conntrack_acct = $(cat /proc/sys/net/netfilter/nf_conntrack_acct 2>/dev/null)"
snap() {
	grep "$1" /proc/net/nf_conntrack 2>/dev/null \
	| awk '{ t=0; for (i=1;i<=NF;i++) if ($i ~ /^bytes=/) { split($i,a,"="); t+=a[2] } s+=t }
	        END { printf "%d %d\n", NR+0, s+0 }'
}
for tag in OFFLOAD HW_OFFLOAD ASSURED; do
	A=$(snap "$tag"); sleep 5; B=$(snap "$tag")
	na=$(echo "$A" | cut -d' ' -f1); ba=$(echo "$A" | cut -d' ' -f2)
	nb=$(echo "$B" | cut -d' ' -f1); bb=$(echo "$B" | cut -d' ' -f2)
	d=$((bb - ba))
	echo "[$tag] flows ${na}->${nb}  bytes delta over 5s = ${d}"
	if [ "$na" -eq 0 ] && [ "$nb" -eq 0 ]; then
		echo "        (no flows carry this tag)"
	elif [ "$d" -gt 0 ]; then
		echo "        MOVING -> conntrack accounting survives for these flows"
	else
		echo "        static -> either idle, or accounting is bypassed"
	fi
done

echo; echo "##### END OF B1 — nothing was modified #####"
KIDSOUT_CHECK_B1
