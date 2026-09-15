# openwrt FAQ
**Root-access** to **openwrt router** with wan and lan networks.
In lan there is an **xbox device** and an **android tablet**

## generic device connected via wifi to openwrt router

### What "connected to internet" can mean
Three different questions get conflated. Pick one before picking a mechanism:

1. **L2 present** — the device is associated to the AP / in the bridge.
2. **L3 present** — the device holds a lease and answers on the LAN.
3. **Actively using the internet** — packets are crossing lan→wan right now.

(3) is the one that matters for time accounting, and it is the only one that
survives modern standby modes: an Xbox with **Instant-On**, or an Android tablet
with a screen off, are both (1) and (2) indefinitely while nobody is using them.

### Alternative mechanisms to detect if device is connected to internet

Reliability below = rough probability that one poll's verdict matches the ground
truth for question **(3)**. They are engineering estimates unless marked
*hardware-confirmed*; the notes say what the failure mode is, which matters more
than the number.

#### Alternative1) nft named counters keyed on the device MAC
- reliability: 90–95% (near 100% for "traffic is flowing", ~90% once you need "a
  human is using it" — see the download caveat)
- requisites: `nftables` + `fw4` (OpenWrt 21.02+); the device's **MAC**; a
  writable place to keep the previous counter sample + timestamp; a byte-rate
  threshold obtained by calibration.
- mechanism: install a counter chain and read the counter twice, dividing the
  delta by elapsed time to get bytes/min:

  ```
  table inet kidsout {
          counter dev_out { }
          chain accounting {
                  type filter hook forward priority -160; policy accept;
                  ether saddr <mac> counter name dev_out
          }
  }
  ```

  `nft -j list counters` gives machine-readable output (`nftables-json`).
  Match **outbound on `ether saddr`**: that covers IPv4 and IPv6 with one rule
  and survives the device's address changing. Inbound cannot be MAC-matched in
  the forward hook — the L2 destination there is the router — so an inbound
  counter has to be keyed on the IP and is IPv4-only, i.e. diagnostics only.
- caveats:
  - **Flow offloading bypasses this.** If `flow_offloading` is on, the forward
    hook sees only each flow's first packets and gameplay under-reports to the
    point of reading `down`. Check `nft list flowtables`; if `flags offload`
    appears the offload is *hardware* and no software counter can see the
    packets at all. Move the counter to a `netdev` ingress chain
    (Alternative 2), which runs before the fast path.
  - Byte rate cannot distinguish **playing** from **downloading**. A background
    update in standby pulls tens of MB/min, and the outbound ACK stream alone
    (~1–2% of downstream) clears any threshold a real session also clears.
    Mitigate on the device (Energy-Saving power mode) rather than in the router.
  - Counters reset on router reboot or table flush. A decrease must be reported
    as `unknown` for that tick and re-baselined, never as `0`.
- *hardware-confirmed (OpenWrt 24.10.5, mediatek/filogic):* named counters,
  `ether saddr` and `priority -150` all parse; `fw4.uc` supports `src_mac`; a
  chain already occupies forward `priority mangle` (-150), so use **-160**.

#### Alternative2) netdev ingress counter on the bridge / wifi port
- reliability: 95%+ (the most trustworthy byte source on an offloading router)
- requisites: `nftables` with netdev family; the device MAC; the correct device
  name (`br-lan`, or the specific `phy*-ap0` for wifi-only accounting).
- mechanism: same counter idea, but hooked at ingress, which is evaluated
  **before** the flowtable fast path, so offloaded flows are still counted:

  ```
  table netdev kidsout {
          counter dev_out { }
          chain ingress {
                  type filter hook ingress device "br-lan" priority -300; policy accept;
                  ether saddr <mac> counter name dev_out
          }
  }
  ```
- caveats: ingress on the bridge counts **lan-internal** traffic too (SMB to a
  NAS, AirPlay, printer discovery), so it answers "the device is talking" rather
  than strictly "to the internet". For a console that is usually the same thing;
  for a tablet on a busy LAN it inflates. Hooking the **wan** device instead
  avoids that but loses the MAC (rewritten by NAT). Still hardware-*unverified*
  beyond the dry run, which parsed OK.

#### Alternative3) wifi association + per-station counters (iwinfo)
- reliability: 99% for question (1); **40–60%** for question (3)
- requisites: `rpcd-mod-iwinfo` (or the `iwinfo` CLI); device on **wifi**, not
  ethernet — this mechanism is blind to a wired device.
- mechanism: `iwinfo <iface> assoclist`, or
  `ubus call iwinfo assoclist '{"device":"phy0-ap0"}'`. Per station you get
  signal, **rx/tx bytes**, rx/rate and **`inactive`** (ms since last frame).
  Two usable signals: presence in the list at all, and the byte counters
  differenced over time like Alternative 1.
- caveats: association persists through standby, so presence alone over-reports
  badly. The byte counters include all wifi traffic (LAN + management), and they
  reset whenever the station re-associates — which happens on roaming, band
  steering and DTIM sleep, more often than you would expect. `inactive` is a
  decent tiebreaker but powersave wakeups keep it low on an idle device.

#### Alternative4) conntrack flow inspection
- reliability: **30–50%** — this is the mechanism to avoid
- requisites: readable `/proc/net/nf_conntrack`; for byte data,
  `net.netfilter.nf_conntrack_acct=1`, which is **off by default**; the device IP
  (so: IPv4-only in practice, and dependent on a stable address).
- mechanism: count lines mentioning the device's IP, or sum their `bytes=`
  fields if accounting is enabled, and threshold on flow count or byte delta.
- caveats: this is the design that fails hardest against standby. A console in
  Instant-On holds persistent connections to Xbox Live while apparently off, so
  any *flow-count* threshold reads `up` all day and silently eats the whole
  allowance. Byte accounting is off by default and, once on, offloaded flows
  stop updating their counters anyway. It is also IP-keyed, so IPv6 slips past.
- *hardware-confirmed:* accounting off, no `conntrack` binary installed,
  `nf_conntrack_tcp_timeout_established = 7440` (2 h).

#### Alternative5) neighbour table (ARP / NDP) state
- reliability: **50–70%** for (2), not usable for (3)
- requisites: `ip neigh` (present everywhere); device on the same L2.
- mechanism: `ip neigh show <ip>` and read the state —
  `REACHABLE` / `STALE` / `DELAY` / `PROBE` / `FAILED`. `STALE` only means
  nothing has been heard recently, not that the device is gone; `FAILED` after a
  probe is a reasonably strong "absent".
- caveats: the router only probes when it has something to send, so states go
  stale on an idle-but-present device. Kernel GC can evict entries entirely.
  Devices that ignore ARP from unexpected sources, or that randomise MACs
  (Android does this per-SSID by default — pin it off for the tablet, or key on
  the lease hostname), break the lookup. Useful mainly as a **negative** check
  and for discovering a device's IPv6 addresses.
- *hardware-confirmed:* with the console off, `ip neigh` shows
  `192.168.2.172 dev br-lan FAILED` — so the negative direction does work here.

#### Alternative6) bridge forwarding database (L2 presence)
- reliability: 60–75% for (1), not usable for (3)
- requisites: `bridge` from `ip-bridge` / busybox; device behind `br-lan`.
- mechanism: `bridge fdb show | grep -i <mac>`. Also tells you **which port**,
  i.e. whether the device is on wifi or ethernet — worth recording once, since
  Alternative 3 only applies to wifi.
- caveats: fdb entries age out on the bridge ageing timer (300 s default), so
  this lags; and a broadcast-chatty device stays in the fdb while doing nothing
  useful.

#### Alternative7) ICMP probe of the device
- reliability: **0–80%**, entirely device-dependent
- requisites: `ping`; the device's current IP; the device actually answering
  ICMP.
- mechanism: `ping -c2 -W1 <ip>` from the router.
- caveats: consoles and phones firewall themselves and sleep their stacks, so a
  silent device is routine. Even a reply only proves it is on the **LAN** — it
  says nothing about wan traffic. Do not build accounting on this.
- *hardware-confirmed:* the Xbox at `192.168.2.172` answers with **100% loss**
  while the router itself replies in 0.4 ms.

#### Alternative8) DHCP lease presence
- reliability: **20–40%** for (2), useless for (3)
- requisites: `dnsmasq`; read access to `/tmp/dhcp.leases`.
- mechanism: look the MAC or hostname up in `/tmp/dhcp.leases` and compare the
  expiry timestamp against now.
- caveats: lease lifetimes are hours, so the entry long outlives the device
  being present; a **static reservation** in `/etc/config/dhcp` persists for
  ever. Good for *identity* — resolving MAC ↔ IP ↔ hostname once, which every
  other mechanism needs — and bad for liveness.

#### Alternative9) DNS query observation
- reliability: 60–80% for (3), coarse in time
- requisites: `dnsmasq` as the LAN resolver *and* the device honouring it;
  query logging (`uci set dhcp.@dnsmasq[0].logqueries=1`) or a `--log-facility`
  file.
- mechanism: watch for queries originating from the device's IP; recent queries
  imply active use.
- caveats: logging every query to flash is a wear problem — log to `/tmp` or a
  socket. Devices increasingly use **DoH/DoT to a hard-coded resolver** and skip
  dnsmasq entirely; Android does this by default on many networks. Standby
  devices also refresh DNS periodically, so this over-reports.

### Summary — which to actually use
| want | use |
|---|---|
| time accounting ("is it in use") | **Alt 1**, or **Alt 2** if offload is on |
| identity (MAC ↔ IP ↔ hostname) | Alt 8, once, at discovery time |
| "is it gone" (negative check) | Alt 5, corroborated by Alt 6 |
| wifi link quality / roaming debug | Alt 3 |
| avoid | Alt 4 (standby), Alt 7 (silent devices) |

Two rules that apply whichever you pick:

- **Key on the MAC, not the IP**, so IPv6 and address changes do not create a
  silent blind spot.
- **Every mechanism needs a calibrated threshold and an explicit `unknown`.**
  A mechanism that collapses errors into `down` will happily report a powered-off
  router as a well-behaved child.
