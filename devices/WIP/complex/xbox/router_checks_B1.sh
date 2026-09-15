sh <<'KIDSOUT_CHECK_B1'
XIP=192.168.2.172
XMAC=d8:e2:df:92:a9:93
s() { echo; echo "##### $* #####"; }

s B1.1 FLOWTABLE '(the big question: does offload bypass my counters?)'
echo "-- firewall defaults --"
uci show firewall.@defaults[0] 2>/dev/null
echo "-- flowtables --"
nft list flowtables 2>/dev/null || echo "(none)"
echo "-- flowtable detail (devices + flags; 'flags offload' means HARDWARE) --"
nft list flowtable inet fw4 ft 2>/dev/null || echo "(no flowtable named ft)"
echo "-- the flow add rule --"
nft list chain inet fw4 forward 2>/dev/null | grep -n "flow add"

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
echo "-- dry run --"
nft -c -f - <<'NFT' && echo "DRY RUN OK: netdev ingress on br-lan accepted" || echo "DRY RUN FAILED"
table netdev kidsout_syntax_check {
	counter xbox_out { }
	chain ingress {
		type filter hook ingress device "br-lan" priority -300; policy accept;
		ether saddr d8:e2:df:92:a9:93 counter name xbox_out
	}
}
NFT

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

echo; echo "##### END OF B1 — nothing was modified #####"
KIDSOUT_CHECK_B1
