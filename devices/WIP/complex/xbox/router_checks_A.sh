sh <<'KIDSOUT_CHECK'
XIP=192.168.2.172
s() { echo; echo "##### $* #####"; }
have() { command -v "$1" >/dev/null 2>&1 && echo "yes ($(command -v $1))" || echo "NO"; }

s 1 IDENTITY
. /etc/openwrt_release 2>/dev/null
echo "distrib : ${DISTRIB_ID:-?} ${DISTRIB_RELEASE:-?} ${DISTRIB_REVISION:-?} ${DISTRIB_TARGET:-?}"
uname -srm
echo "uptime  : $(uptime)"

s 2 FIREWALL FLAVOUR
echo "fw4      : $(have fw4)"
echo "fw3      : $(have fw3)"
echo "nft      : $(have nft)"
echo "iptables : $(have iptables)"
nft --version 2>&1 | head -1
echo "-- packages --"
{ opkg list-installed 2>/dev/null || apk list -I 2>/dev/null; } \
  | grep -Ei '^(firewall|nftables|ucode|rpcd|uhttpd|conntrack|libnftnl)' | sort

s 3 FIREWALL ZONES
uci show firewall 2>/dev/null | grep -E "\.name='|=zone|\.network=" | head -30
echo "-- zone names --"
uci show firewall 2>/dev/null | grep -E "^firewall\.@zone\[[0-9]+\]\.name=" 

s 4 EXISTING KIDSOUT STATE  '(expected: all empty)'
echo "-- uci firewall --";  uci show firewall 2>/dev/null | grep -i kidsout || echo "(none)"
echo "-- nft tables --";    nft list tables 2>/dev/null || echo "(nft list failed)"
echo "-- helper/acl --";    ls -l /usr/libexec/kidsout-xbox /usr/share/rpcd/acl.d/kidsout-xbox.json 2>&1
echo "-- libexec dir --";   ls -ld /usr/libexec 2>&1

s 5 src_mac SUPPORT IN fw4
echo "-- occurrences in fw4 ucode --"
grep -rn "src_mac" /usr/share/ucode/fw4.uc 2>/dev/null | head -8 || echo "(fw4.uc not found)"
grep -rn "src_mac" /usr/share/firewall4/ 2>/dev/null | head -5
echo "-- rendered ruleset, first forward rules --"
fw4 print 2>/dev/null | grep -n -A2 "chain forward" | head -20 || echo "(fw4 print unavailable)"

s 6 NFT DRY RUN  '(check-only: -c never loads anything)'
nft -c -f - <<'NFT' && echo "DRY RUN OK: counters + ether saddr + priority -150 all accepted"
table inet kidsout_syntax_check {
	counter xbox_out { }
	counter xbox_in { }
	chain accounting {
		type filter hook forward priority -150; policy accept;
		ether saddr aa:bb:cc:dd:ee:ff counter name xbox_out
		ip daddr 192.168.2.172 counter name xbox_in
	}
}
NFT
echo "-- nft json support --"
nft -j list counters >/dev/null 2>&1 && echo "nft -j: YES" || echo "nft -j: NO (json unsupported)"
echo "-- existing chains at priority <= -150 (conflict check) --"
nft list ruleset 2>/dev/null | grep -E "type filter hook (forward|prerouting)" | head -10
echo "-- ruleset size --"; nft list ruleset 2>/dev/null | wc -l

s 7 RPCD AND ACLS
echo "-- /etc/config/rpcd (passwords redacted) --"
sed -e "s/\(option password\).*/\1 '<redacted>'/" /etc/config/rpcd 2>/dev/null
echo "-- login sections --"
uci show rpcd 2>/dev/null | grep -E "^rpcd\.@login" | sed -e "s/\(password\)=.*/\1=<redacted>/"
echo "-- acl.d contents --"; ls -l /usr/share/rpcd/acl.d/ 2>&1
echo "-- an ACL that uses file exec (schema reference) --"
grep -l '"file"' /usr/share/rpcd/acl.d/*.json 2>/dev/null | head -3
for f in $(grep -l '"file"' /usr/share/rpcd/acl.d/*.json 2>/dev/null | head -1); do
	echo "--- $f ---"; head -40 "$f"
done
echo "-- ubus objects --"; ubus list 2>/dev/null | grep -E "^(file|uci|session|system|service)$"
echo "-- all ubus objects (count) --"; ubus list 2>/dev/null | wc -l

s 8 PASSWORD HASHING TOOLS
echo "cryptpw  : $(have cryptpw)"
echo "mkpasswd : $(have mkpasswd)"
echo "openssl  : $(have openssl)"
echo "passwd   : $(have passwd)"
echo "-- dummy hash of the literal string 'dummy' (not a real password) --"
printf 'dummy' | cryptpw -m sha512 2>/dev/null | cut -c1-12 || echo "(cryptpw failed)"
printf 'dummy' | mkpasswd -m sha-512 2>/dev/null | cut -c1-12 || echo "(mkpasswd failed)"
openssl passwd -6 dummy 2>/dev/null | cut -c1-12 || echo "(openssl passwd failed)"

s 9 CONNTRACK
echo "conntrack tool : $(have conntrack)"
echo "-- /proc/net/nf_conntrack --"
ls -l /proc/net/nf_conntrack 2>&1
echo "entries: $(wc -l < /proc/net/nf_conntrack 2>/dev/null || echo unreadable)"
echo "-- established timeout (F-05's 5-day figure) --"
sysctl net.netfilter.nf_conntrack_tcp_timeout_established 2>/dev/null || \
  cat /proc/sys/net/netfilter/nf_conntrack_tcp_timeout_established 2>/dev/null
echo "-- early established accept in the forward chain --"
nft list chain inet fw4 forward 2>/dev/null | head -12

s 10 IPv6
echo "-- default v6 route (does the ISP delegate a prefix?) --"
ip -6 route show default 2>/dev/null | head -3 || echo "(none)"
echo "-- global v6 on the LAN bridge --"
ip -6 addr show br-lan 2>/dev/null | grep "inet6" | head -5 || echo "(none)"
echo "-- v6 neighbours total --"; ip -6 neigh show 2>/dev/null | wc -l

s 11 XBOX IDENTITY  '(console is OFF, so stale entries are expected)'
echo "-- lease for $XIP --"
grep -F "$XIP" /tmp/dhcp.leases 2>/dev/null || echo "(no current lease)"
echo "-- other leases: $(grep -cv "$XIP" /tmp/dhcp.leases 2>/dev/null || echo 0) --"
echo "-- neighbour entry for $XIP --"
ip neigh show 2>/dev/null | grep -F "$XIP" || echo "(not in neighbour table)"
echo "-- static DHCP reservation for $XIP --"
uci show dhcp 2>/dev/null | grep -F "$XIP" || echo "(no static reservation)"

s 12 UHTTPD / TRANSPORT
uci show uhttpd 2>/dev/null | grep -E "listen|cert|key|ubus" | head -15

s 13 STORAGE  '(F-09 context)'
mount 2>/dev/null | grep -E "overlay|jffs2|ubifs" | head -5
df -h /overlay 2>/dev/null | tail -2

echo; echo "##### END OF CHECKS — nothing was modified #####"
KIDSOUT_CHECK
