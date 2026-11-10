#!/bin/sh
# KwickBoot: prepare the RAM OS with the full diagnostics + install tool set.
#   Debian KwickBoot RAM OS (Secure Boot): tools are built in, nothing is downloaded.
#   Alpine "lite" RAM OS: tools are installed from the Alpine mirror at boot.
if command -v apk >/dev/null 2>&1; then
grep -q '/community' /etc/apk/repositories || \
  sed -n 's#/main$#/community#p' /etc/apk/repositories | head -n1 >> /etc/apk/repositories
apk update -q

# grouped tool sets; each package installed individually so one missing package never blocks the rest
BASE="bash coreutils util-linux lsblk blkid wipefs findmnt curl wget zstd pv tmux less nano jq"
BIOS="dmidecode efibootmgr efivar mokutil fwupd flashrom"
CPU_RAM="stress-ng memtester edac-utils i2c-tools cpupower"
SENSORS="lm-sensors lm-sensors-detect ipmitool"
DISKS="smartmontools nvme-cli hdparm sdparm sg3_utils lsscsi fio e2fsprogs-extra parted sgdisk dosfstools e2fsprogs exfatprogs ntfs-3g-progs xfsprogs cryptsetup"
RAID="mdadm lvm2 btrfs-progs"
NET="ethtool iperf3 mtr tcpdump lldpd iproute2 bind-tools"
HW="pciutils usbutils lshw inxi hwdata-pci hwdata-usb"
INSTALL="debootstrap debian-archive-keyring gnupg gpgv perl"
missing=""
for p in $BASE $BIOS $CPU_RAM $SENSORS $DISKS $RAID $NET $HW $INSTALL; do
  apk add -q "$p" >/dev/null 2>&1 || missing="$missing $p"
done
[ -n "$missing" ] && echo "not available in this Alpine:$missing"
else
  echo "KwickBoot RAM OS $(cat /etc/kwickboot-ramos-version 2>/dev/null): tools built in"
fi

# kernel modules for sensors / IPMI / memory errors (silently skip what this hardware lacks)
for m in coretemp k10temp nct6775 it87 drivetemp ipmi_si ipmi_devintf ipmi_msghandler i2c-dev at24 ee1004 edac_core; do
  modprobe -q "$m" 2>/dev/null || true
done
command -v sensors-detect >/dev/null && yes '' | sensors-detect --auto >/dev/null 2>&1 || true
command -v lldpd >/dev/null && { lldpd -r >/dev/null 2>&1 || lldpd >/dev/null 2>&1; } || true

mkdir -p /root/tk
for s in inventory.sh preflight.sh install.sh install-debian13.sh debian13-configure.sh; do
  wget -qO /root/tk/$s http://{{SERVER_IP}}:{{HTTP_PORT}}/scripts/$s && chmod +x /root/tk/$s
done
wget -qO /root/tk/authorized_keys http://{{SERVER_IP}}:{{HTTP_PORT}}/authorized_keys
echo "toolkit ready in /root/tk:"; ls /root/tk

# report inventory to the PXE server (saved under machines/<mac>/ on the server)
pgrep lldpd >/dev/null 2>&1 && sleep 5   # give LLDP a moment to hear the switch
MAC=$(cat /sys/class/net/$(ip route show default | awk '{print $5; exit}')/address 2>/dev/null)
sh /root/tk/inventory.sh > /root/tk/inventory.txt 2>&1
TOKEN=$(tr ' ' '\n' < /proc/cmdline | sed -n 's/^pxetoken=//p')
curl -s -H "X-PXE-Token: $TOKEN" --data-binary @/root/tk/inventory.txt "http://{{SERVER_IP}}:{{HTTP_PORT}}/report?mac=$MAC" >/dev/null \
  && echo "inventory reported to PXE server (mac $MAC)" || echo "inventory report FAILED"
