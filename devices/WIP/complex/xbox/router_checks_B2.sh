sh <<'KIDSOUT_CHECK_B2'
XIP=192.168.2.172
XMAC=d8:e2:df:92:a9:93
s() { echo; echo "##### $* #####"; }

s B2.1 IS THE CONSOLE VISIBLE
echo "-- dhcp lease --"
grep -iF "$XMAC" /tmp/dhcp.leases 2>/dev/null || echo "(no lease)"
echo "-- ipv4 neighbour --"
ip neigh show 2>/dev/null | grep -F "$XIP" || echo "(absent)"
echo "-- ipv6 neighbours for this MAC --"
ip -6 neigh show 2>/dev/null | grep -i "$XMAC" || echo "(none - no IPv6 on the console)"
echo "-- which bridge port is it on (wifi vs ethernet) --"
bridge fdb show 2>/dev/null | grep -i "$XMAC" | head -5 || echo "(not in fdb)"

s B2.2 FLOW COUNTS FOR THE CONSOLE
echo "conntrack entries mentioning it : $(grep -cF "$XIP" /proc/net/nf_conntrack)"
echo "  of those, [OFFLOAD]           : $(grep -F "$XIP" /proc/net/nf_conntrack | grep -c OFFLOAD)"
echo "  of those, [HW_OFFLOAD]        : $(grep -F "$XIP" /proc/net/nf_conntrack | grep -c HW_OFFLOAD)"
echo "  ESTABLISHED                   : $(grep -F "$XIP" /proc/net/nf_conntrack | grep -c ESTABLISHED)"
echo "  udp                           : $(grep -F "$XIP" /proc/net/nf_conntrack | grep -c '^udp')"
echo "-- destination ports it is talking to (top 8) --"
grep -F "$XIP" /proc/net/nf_conntrack 2>/dev/null \
  | grep -o "dport=[0-9]*" | sort | uniq -c | sort -rn | head -8

s B2.3 SAMPLE LINE '(remote addresses masked)'
grep -F "$XIP" /proc/net/nf_conntrack 2>/dev/null | head -2 \
  | sed -e "s/$XIP/<XBOX>/g" -e 's/[0-9]\{1,3\}\.[0-9]\{1,3\}\.[0-9]\{1,3\}\.[0-9]\{1,3\}/<remote>/g'

s B2.4 BYTE RATE OVER 30s '(THE decisive measurement)'
sum() {
  grep -F "$XIP" /proc/net/nf_conntrack 2>/dev/null \
  | awk '{ t=0; for (i=1;i<=NF;i++) if ($i ~ /^bytes=/) { split($i,a,"="); t+=a[2] } print t }' \
  | awk '{ s+=$1 } END { print s+0 }'
}
A=$(sum); TA=$(date +%s)
echo "sample 1: $A bytes across the console's flows"
echo "(waiting 30s - keep the console doing whatever it is doing)"
sleep 30
B=$(sum); TB=$(date +%s)
echo "sample 2: $B bytes"
D=$((B - A)); DT=$((TB - TA))
echo "delta   : $D bytes over ${DT}s"
if [ "$D" -le 0 ]; then
  echo "RESULT  : NO byte movement -> conntrack accounting is off, or flows are offloaded"
else
  echo "RESULT  : ~$((D * 60 / DT)) bytes/min  (~$((D * 60 / DT / 1024)) KB/min)"
fi

s B2.5 INTERFACE THROUGHPUT CROSS-CHECK '(sanity: is traffic flowing at all?)'
R1=$(cat /sys/class/net/br-lan/statistics/rx_bytes)
T1=$(cat /sys/class/net/br-lan/statistics/tx_bytes)
sleep 10
R2=$(cat /sys/class/net/br-lan/statistics/rx_bytes)
T2=$(cat /sys/class/net/br-lan/statistics/tx_bytes)
echo "br-lan over 10s: rx=$(( (R2-R1) * 6 / 1024 )) KB/min  tx=$(( (T2-T1) * 6 / 1024 )) KB/min"
echo "(this is the WHOLE lan, not just the console - it only proves traffic exists)"

echo; echo "##### END OF B2 — nothing was modified #####"
KIDSOUT_CHECK_B2
