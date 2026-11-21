#!/usr/bin/env bash
#
# The rig VM's one host setting: a cap on what the container engine writes to disk.
# The VM shares its disks with others, and a build or a pull at full speed stalls
# them. Docker's own processes, its containers and its build steps all sit in
# system.slice under the systemd cgroup driver, so that is where the cap goes, and
# the rig's scripts and SSH sessions in user.slice stay free. The IOPS line sits above
# what a first boot's small files need: under about 400 the kernel's own journal
# flush waits behind the capped writers, and Docker itself stops answering.
#
#   host-setup.sh on [write MB/s] [write IOPS]    (default 20 and 1600)
#   host-setup.sh off
#   host-setup.sh show
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=rig/install/guard.sh
. "$HERE/guard.sh"
DROPIN=/etc/systemd/system/system.slice.d/50-rig-io.conf
disk() { lsblk -snlo NAME,TYPE "$(findmnt -no SOURCE -T "$HOME")" | awk '$2 == "disk" { print $1; exit }'; }
case "${1:-show}" in
    on)
        dev="/dev/$(disk)"
        sudo mkdir -p "$(dirname "$DROPIN")"
        printf '[Slice]\nIOAccounting=yes\nIOWriteBandwidthMax=%s %sM\nIOWriteIOPSMax=%s %s\n' \
            "$dev" "${2:-20}" "$dev" "${3:-1600}" | sudo tee "$DROPIN" >/dev/null
        sudo systemctl daemon-reload ;;
    off)
        sudo rm -f "$DROPIN"
        sudo systemctl daemon-reload
        # A reload leaves the value in the cgroup: it goes by hand.
        echo "$(lsblk -dno MAJ:MIN "/dev/$(disk)" | tr -d ' ') wbps=max wiops=max" | sudo tee /sys/fs/cgroup/system.slice/io.max >/dev/null ;;
    show) ;;
    *) echo "usage: host-setup.sh on|off|show" >&2; exit 2 ;;
esac
cat /sys/fs/cgroup/system.slice/io.max
