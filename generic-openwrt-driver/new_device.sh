#!/usr/bin/env bash
# Scaffold a new kidsout device that uses the shared generic OpenWrt driver.
#
#   ./new_device.sh <name> [devices_dir]
#
# <name>       the device directory name AND its id: lowercase letters and
#              digits only, starting with a letter (e.g. xbox, nintendoswitch2)
# devices_dir  where kidsout's devices live (default: ../devices next to this
#              directory). If it is elsewhere, also export
#              KIDSOUT_OPENWRT_DRIVER=<abs path to driver.py> for kidsout.
#
# Creates:
#   <devices_dir>/<name>/getState.sh block.sh unblock.sh    the kidsout contract
#   <devices_dir>/<name>/generic-openwrt-driver_files/
#       driver.sh        operator entry point (./driver.sh status ...)
#       device.json      from device.example.json, id set to <name>
#       .gitignore       keeps config.json and runtime files out of git
#
# Then follow README.md "Adding a device" from step 3 (router bootstrap).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
name="${1:-}"
devices="${2:-$here/../devices}"

usage() { echo "usage: $0 <name> [devices_dir]" >&2; exit 2; }
[ -n "$name" ] || usage
if ! [[ "$name" =~ ^[a-z][a-z0-9]{0,23}$ ]]; then
	echo "error: '$name' is not a valid id: lowercase letters and digits only, starting with a letter" >&2
	exit 2
fi
[ -d "$devices" ] || { echo "error: devices dir '$devices' does not exist" >&2; exit 2; }
dev="$devices/$name"
files="$dev/generic-openwrt-driver_files"
if [ -e "$dev" ]; then
	echo "error: $dev already exists; remove it or pick another name" >&2
	exit 1
fi

mkdir -p "$files"
for f in getState.sh block.sh unblock.sh; do
	cp -p "$here/contract/$f" "$dev/$f"
done
cp -p "$here/contract/driver.sh" "$files/driver.sh"
cp "$here/contract/files.gitignore" "$files/.gitignore"
sed "s/CHANGE_ME/$name/g" "$here/device.example.json" > "$files/device.json"
chmod +x "$dev"/*.sh "$files/driver.sh"

# sanity: the wrappers must find driver.py from where they now live
if [ -z "${KIDSOUT_OPENWRT_DRIVER:-}" ] && [ ! -f "$dev/../../generic-openwrt-driver/driver.py" ]; then
	echo "warning: $dev/../../generic-openwrt-driver/driver.py does not exist."
	echo "         export KIDSOUT_OPENWRT_DRIVER=$here/driver.py in kidsout's environment."
fi

cat <<MSG
created $dev

next steps (details in $here/README.md):
  1. edit $files/device.json
       - router.primary_host (and alt_hosts) = your OpenWrt router
       - network.zone / wan_zone if they are not 'lan' / 'wan'
       - discover.hostname_hints = what the device calls itself in DHCP
  2. on the router, once for this device (interactive, prompts for a NEW password):
       ssh root@<router> 'cat > /tmp/router_bootstrap.sh' < $here/router_bootstrap.sh
       ssh -t root@<router> sh /tmp/router_bootstrap.sh $name
  3. cp $here/config.example.json $files/config.json && chmod 600 $files/config.json
     set "password" to the one you chose (username stays null = kidsout-$name)
  4. cd $files && ./driver.sh probe && ./driver.sh pin && ./driver.sh selftest
  5. with the device switched on: ./driver.sh discover --write   (repeat on each interface)
  6. ./driver.sh install        (rule created in the ALLOWED state)
  7. ./driver.sh calibrate ...  then set state.threshold_* in device.json
  8. restart kidsout so it discovers devices/$name
MSG
