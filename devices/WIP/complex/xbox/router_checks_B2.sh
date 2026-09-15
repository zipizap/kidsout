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
# The driver's up/down metric is OUTBOUND ONLY. A conntrack line is
#   <orig tuple> <orig counters> <reply tuple> <reply counters>
# so summing every bytes= field on the line adds download to upload and would
# inflate the calibration target by roughly the download ratio. Split them:
# the counter block belonging to the tuple whose src= is the console is OUT.
sum() {
  grep -F "$XIP" /proc/net/nf_conntrack 2>/dev/null \
  | awk -v ip="$XIP" '
      {
        dir = 0; out = 0; in_ = 0; n = 0
        for (i = 1; i <= NF; i++) {
          if ($i ~ /^src=/)   { split($i, a, "="); dir = (a[2] == ip) ? 1 : 2 }
          if ($i ~ /^bytes=/) { split($i, a, "="); if (dir == 1) out += a[2]; else in_ += a[2] }
        }
        O += out; I += in_; n++
      }
      END { printf "%d %d %d\n", O+0, I+0, NR+0 }'
}
A=$(sum); TA=$(date +%s)
oa=$(echo "$A" | cut -d' ' -f1); ia=$(echo "$A" | cut -d' ' -f2); fa=$(echo "$A" | cut -d' ' -f3)
echo "sample 1: out=$oa in=$ia across $fa flows"
echo "(waiting 30s - KEEP THE CONSOLE ACTIVELY PLAYING, not idle)"
sleep 30
B=$(sum); TB=$(date +%s)
ob=$(echo "$B" | cut -d' ' -f1); ib=$(echo "$B" | cut -d' ' -f2); fb=$(echo "$B" | cut -d' ' -f3)
echo "sample 2: out=$ob in=$ib across $fb flows"
DO=$((ob - oa)); DI=$((ib - ia)); DT=$((TB - TA))
echo "delta   : out=$DO in=$DI bytes over ${DT}s   (flows ${fa}->${fb})"
if [ "$fb" -lt "$fa" ]; then
  echo "NOTE    : flow count FELL - some flows expired and took their counters with them."
  echo "          This is exactly why a naive sum is not a monotonic counter."
fi
if [ "$DO" -le 0 ]; then
  echo "RESULT  : NO outbound byte movement -> accounting off, bypassed, or console idle"
  echo "          (cross-check against B2.5 before concluding)"
else
  echo "RESULT  : outbound ~$((DO * 60 / DT)) bytes/min (~$((DO * 60 / DT / 1024)) KB/min)"
  echo "          inbound  ~$((DI * 60 / DT)) bytes/min (~$((DI * 60 / DT / 1024)) KB/min)"
  echo "          device.json threshold_bytes_per_min is currently 204800 (200 KB/min)"
fi

s B2.5 INTERFACE THROUGHPUT CROSS-CHECK '(the honest denominator)'
R1=$(cat /sys/class/net/br-lan/statistics/rx_bytes)
T1=$(cat /sys/class/net/br-lan/statistics/tx_bytes)
# The console's own switch port, if it is on wired ethernet. These come from the
# switch's hardware MIB, so they are true even under PPE hardware offload -- which
# makes this the one number that is never lying to us.
XPORT=$(bridge fdb show 2>/dev/null | grep -i "$XMAC" | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1)}' | head -1)
P1=""; [ -n "$XPORT" ] && P1=$(cat "/sys/class/net/$XPORT/statistics/rx_bytes" 2>/dev/null)
sleep 10
R2=$(cat /sys/class/net/br-lan/statistics/rx_bytes)
T2=$(cat /sys/class/net/br-lan/statistics/tx_bytes)
P2=""; [ -n "$XPORT" ] && P2=$(cat "/sys/class/net/$XPORT/statistics/rx_bytes" 2>/dev/null)
echo "br-lan over 10s: rx=$(( (R2-R1) * 6 / 1024 )) KB/min  tx=$(( (T2-T1) * 6 / 1024 )) KB/min"
echo "(this is the WHOLE lan, not just the console - it only proves traffic exists)"
if [ -n "$P1" ] && [ -n "$P2" ]; then
  echo "console port '$XPORT' rx: $(( (P2-P1) * 6 / 1024 )) KB/min  <-- console->router, hardware MIB"
  echo "   Compare with B2.4's outbound figure. If B2.4 is near zero while this"
  echo "   is large, conntrack accounting is being bypassed by offload."
else
  echo "console port: not resolvable (on wifi, or idle/absent from the fdb)"
fi

s B2.6 WIFI PER-STATION COUNTERS '(alternative byte source if console is wireless)'
if [ -n "$XPORT" ] && echo "$XPORT" | grep -qE "^(phy|wlan|ra|ap)"; then
  echo "console appears WIRELESS on '$XPORT' - sampling iwinfo assoclist"
  ubus call iwinfo assoclist "{\"device\":\"$XPORT\"}" 2>/dev/null \
    | grep -A6 -i "$XMAC" | head -20 || echo "(iwinfo assoclist unavailable)"
else
  echo "console is wired (or absent) - iwinfo path not applicable"
  echo "wireless devices present: $(ls /sys/class/net/ | grep -E '^(phy|wlan|ra)' | tr '\n' ' ')"
fi

echo; echo "##### END OF B2 — nothing was modified #####"
KIDSOUT_CHECK_B2
