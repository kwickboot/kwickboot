#!/bin/sh
# PXE toolkit: install Debian 13 (trixie) from the in-RAM Alpine using debootstrap.
# WIPES the target disk. Two independent confirmations are required.
#
#   DISK=/dev/mmcblk0 CONFIRM=<disk serial, see inventory.sh> HOSTNAME=pc01 \
#     [TZ=America/Chicago] [LAN=192.168.0.0/24] sh /root/tk/install-debian13.sh
#
# Layout: GPT, 512M EFI (vfat) + rest ext4 root, no swap partition (zram instead).
set -e
: "${DISK:?set DISK (e.g. /dev/sda, /dev/nvme0n1, /dev/mmcblk0)}"
: "${CONFIRM:?set CONFIRM to the disk SERIAL shown by inventory.sh}"
: "${HOSTNAME:?set HOSTNAME}"
TK=/root/tk

[ -b "$DISK" ] || { echo "STOP: $DISK is not a block device"; exit 1; }
disk_serial() {   # lsblk needs udev for SERIAL; fall back to sysfs (virtio, eMMC/SD, NVMe, SCSI vpd)
  n=$(basename "$1"); s=$(lsblk -dno SERIAL "$1" 2>/dev/null | tr -d ' ')
  for f in /sys/block/$n/serial /sys/block/$n/device/serial; do [ -z "$s" ] && [ -r "$f" ] && s=$(tr -d ' \n' < "$f"); done; [ -z "$s" ] && [ -r /sys/block/$n/device/vpd_pg80 ] && s=$(tail -c +5 /sys/block/$n/device/vpd_pg80 | tr -d ' \n\000')   # SATA/SAS: serial from the VPD page (no udev here)
  [ -z "$s" ] && [ -r /sys/block/$n/device/vpd_pg80 ] && s=$(tr -dc '[:alnum:]-_' < /sys/block/$n/device/vpd_pg80)
  echo "$s"
}
SERIAL=$(disk_serial "$DISK")
SIZE=$(lsblk -dno SIZE "$DISK" | tr -d ' ')
MODEL=$(lsblk -dno MODEL "$DISK")
[ -n "$SERIAL" ] || SERIAL="size-$SIZE"
[ "$CONFIRM" = "$SERIAL" ] || { echo "STOP: CONFIRM '$CONFIRM' does not match $DISK serial '$SERIAL' ($MODEL $SIZE)"; exit 1; }
if lsblk -nro MOUNTPOINTS "$DISK" | grep -q .; then echo "STOP: $DISK has mounted partitions"; exit 1; fi

echo ">>> preflight hardware analysis..."
DISK="$DISK" OS=debian13 sh "$TK/preflight.sh" || { echo "STOP: preflight found blocking problems (see above)"; exit 1; }
. "$TK/preflight.env"
case "$(uname -m)" in aarch64|arm64) [ "$BOOT_MODE" = "uefi" ] || { echo "STOP: arm64 requires UEFI firmware (no legacy BIOS boot)"; exit 1; } ;; esac

# Target CPU architecture: the Core RAM OS runs ON the target, so uname -m is the target arch. Debian 13 (trixie) dropped
# i386 -> a 32-bit machine installs Debian 12 (bookworm, 6.1 LTS to 2028). amd64/arm64 install trixie.
case "$(uname -m)" in
  x86_64)         ARCH=amd64; SUITE=trixie;   KERNEL=linux-image-amd64;   GRUBPKG=grub-efi-amd64; UCODE="intel-microcode amd64-microcode"; GFX=firmware-intel-graphics ;;
  aarch64|arm64)  ARCH=arm64; SUITE=trixie;   KERNEL=linux-image-arm64;   GRUBPKG=grub-efi-arm64; UCODE=""; GFX="" ;;
  i686|i586|i386) ARCH=i386;  SUITE=bookworm; KERNEL=linux-image-686-pae; GRUBPKG=grub-efi-ia32;  UCODE="intel-microcode amd64-microcode"; GFX=firmware-intel-graphics ;;
  *) echo "STOP: unsupported target CPU arch $(uname -m)"; exit 1 ;;
esac
[ "$BOOT_MODE" = "bios" ] && GRUBPKG="grub-pc"   # legacy BIOS uses grub-pc (MBR/core.img), not grub-efi
echo ">>> target arch $(uname -m) -> Debian $ARCH ($SUITE), boot $BOOT_MODE"

echo ">>> Installing Debian 13 on $DISK ($MODEL $SIZE, serial $SERIAL) as $HOSTNAME"
echo ">>> extra hardware packages from preflight: ${EXTRA_PKGS:-none}"
echo ">>> ALL DATA ON $DISK WILL BE DESTROYED in 10 seconds (Ctrl+C to abort)"
sleep 10

case "$DISK" in *[0-9]) P="${DISK}p" ;; *) P="$DISK" ;; esac
wipefs -a -q "$DISK"* 2>/dev/null || true
sgdisk --zap-all "$DISK" >/dev/null
if [ "$BOOT_MODE" = "bios" ]; then
  # GPT + a 1 MiB BIOS-boot partition (EF02) holds GRUB's core.img; root on p2; no ESP.
  sgdisk -n1:1MiB:+1MiB -t1:EF02 -c1:BIOSboot -n2:0:0 -t2:8304 -c2:debian-root "$DISK" >/dev/null
  partprobe "$DISK" 2>/dev/null || true; sleep 2
  mkfs.ext4 -q -F -L debian-root "${P}2"
  mkdir -p /mnt/t; mount "${P}2" /mnt/t
else
  sgdisk -n1:1MiB:+512MiB -t1:EF00 -c1:EFI -n2:0:0 -t2:8304 -c2:debian-root "$DISK" >/dev/null
  partprobe "$DISK" 2>/dev/null || true; sleep 2
  mkfs.vfat -F32 -n EFI "${P}1" >/dev/null
  mkfs.ext4 -q -F -L debian-root "${P}2"
  mkdir -p /mnt/t; mount "${P}2" /mnt/t; mkdir -p /mnt/t/boot/efi; mount "${P}1" /mnt/t/boot/efi
fi

INC="$KERNEL,$GRUBPKG,firmware-linux-free,firmware-realtek,efibootmgr,openssh-server,sudo,systemd-resolved,nftables,wireguard-tools,rsync,curl,wget,ca-certificates,smartmontools,cryptsetup,restic,tmux,locales,zram-tools,less,nano,bash-completion,exfatprogs,dosfstools,usbutils,pciutils,dbus,iproute2,iputils-ping,ethtool,lm-sensors"
[ -n "$GFX" ] && INC="$INC,$GFX"                       # x86 Intel graphics firmware (not on arm64)
for u in $UCODE; do INC="$INC,$u" ; done               # CPU microcode (x86 only)
for p in $EXTRA_PKGS; do case ",$INC," in *",$p,"*) ;; *) INC="$INC,$p" ;; esac; done
echo ">>> debootstrap $ARCH/$SUITE (5-15 min)..."
debootstrap --arch=$ARCH --components=main,non-free-firmware --include="$INC" \
  --keyring=/usr/share/keyrings/debian-archive-keyring.gpg $SUITE /mnt/t http://deb.debian.org/debian \
  > /root/debootstrap.log 2>&1 || { echo "debootstrap FAILED - see /root/debootstrap.log"; tail -n 20 /root/debootstrap.log; exit 1; }

echo ">>> configuring..."
HOSTNAME="$HOSTNAME" TZ="${TZ:-America/Chicago}" LAN="${LAN:-192.168.0.0/24}" ARCH="$ARCH" SUITE="$SUITE" BOOT_MODE="$BOOT_MODE" DISK="$DISK" KEYS="$(cat $TK/authorized_keys)" \
  sh "$TK/debian13-configure.sh" /mnt/t

sync; umount -R /mnt/t 2>/dev/null || umount -Rl /mnt/t 2>/dev/null || true   # -R can hit a busy cgroup submount; lazy-detach as fallback
echo ">>> DONE. Reboot with:  reboot   (machine boots Debian 13 from $DISK)"
