#!/bin/sh
# build-san-golden.sh - build a bootable "golden" OS image for KwickBoot-as-SAN (san.py).
#
# Produces a disk image whose OS boots DISKLESS over iSCSI: iPXE `sanboot` attaches the LUN,
# extlinux (in the image MBR) loads the kernel, and the kernel's open-iscsi/iBFT initramfs
# re-attaches the SAME LUN as its root filesystem. No local disk, no third-party SAN - the
# client gets both its boot config AND its OS from KwickBoot. Write it onto a golden LUN with
# san.provision(), then clone per server (san.clone) - "room = N servers from one golden image".
#
# Run as root on the build box, NOT on a production host carelessly - it uses loop devices.
#   sudo SUITE=trixie SIZE_MB=2048 OUT=$HOME/kwickboot/http/san sh build-san-golden.sh
# SPDX-License-Identifier: AGPL-3.0-or-later
set -e
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export DEBIAN_FRONTEND=noninteractive
SUITE=${SUITE:-trixie}
SIZE_MB=${SIZE_MB:-2048}
MIRROR=${MIRROR:-http://deb.debian.org/debian}
OUT=${OUT:-$HOME/kwickboot/http/san}
IMG="$OUT/golden-$SUITE.img"
# packages the diskless OS needs: a kernel, the iSCSI initiator (iBFT boot), dhcp, init, net drivers.
INCLUDE="linux-image-amd64,open-iscsi,isc-dhcp-client,ifupdown,systemd-sysv,udev,netbase,iproute2,initramfs-tools,e2fsprogs,openssh-server,ca-certificates,less"
# --test (or KB_GOLDEN_TEST=1): serial ROOT AUTOLOGIN + root:root + the KWICKBOOT-SAN-ROOT-OK boot marker -> for VM
# boot proofs ONLY. Default = a production image: no autologin, root LOCKED unless KB_SSH_KEY (an SSH public key) or
# KB_ROOT_PW is given. Every clone inherits this, so the default must be safe.
TEST=0; for _a in "$@"; do [ "$_a" = "--test" ] && TEST=1; done; [ "${KB_GOLDEN_TEST:-0}" = 1 ] && TEST=1

say(){ printf '\n>>> %s\n' "$*"; }
mnt=""; loop=""
cleanup(){ set +e; [ -n "$mnt" ] && { for d in dev/pts dev proc sys run; do umount "$mnt/$d" 2>/dev/null; done; umount "$mnt" 2>/dev/null; rmdir "$mnt" 2>/dev/null; }; [ -n "$loop" ] && losetup -d "$loop" 2>/dev/null; }
trap cleanup EXIT INT TERM

[ "$(id -u)" = 0 ] || { echo "!! run as root (sudo)"; exit 1; }
command -v debootstrap >/dev/null || { echo "!! debootstrap missing"; exit 1; }
mkdir -p "$OUT"

say "1. create $SIZE_MB MB image + MBR partition  ($IMG)"
rm -f "$IMG"
truncate -s "${SIZE_MB}M" "$IMG"
parted -s "$IMG" mklabel msdos
parted -s "$IMG" mkpart primary ext4 1MiB 100%
parted -s "$IMG" set 1 boot on

say "2. loop-mount the partition + mkfs.ext4 (label sanroot)"
loop=$(losetup --find --show -P "$IMG")
for i in 1 2 3 4 5; do [ -e "${loop}p1" ] && break; sleep 1; done
mkfs.ext4 -q -L sanroot "${loop}p1"
mnt=$(mktemp -d)
mount "${loop}p1" "$mnt"

say "3. debootstrap --variant=minbase $SUITE (this is the slow step)"
debootstrap --variant=minbase --include="$INCLUDE" "$SUITE" "$mnt" "$MIRROR"

say "4. configure the diskless OS in the chroot"
for d in dev dev/pts proc sys run; do mkdir -p "$mnt/$d"; mount --bind "/$d" "$mnt/$d"; done
echo kwickboot-san > "$mnt/etc/hostname"
printf 'LABEL=sanroot / ext4 defaults,_netdev,noatime 0 1\n' > "$mnt/etc/fstab"
# iSCSI root from firmware/iBFT (what iPXE sanboot populates):
mkdir -p "$mnt/etc/iscsi"
printf 'ISCSI_AUTO=true\n' > "$mnt/etc/iscsi/iscsi.initramfs"
# make sure the initiator name exists (open-iscsi postinst usually writes one)
[ -s "$mnt/etc/iscsi/initiatorname.iscsi" ] || printf 'InitiatorName=iqn.2026-11.com.kwickboot:client\n' > "$mnt/etc/iscsi/initiatorname.iscsi"
if [ "$TEST" = 1 ]; then
  say "   --test: serial ROOT AUTOLOGIN + root:root + boot marker (DEV/PROOF image — do NOT use for production clones)"
  mkdir -p "$mnt/etc/systemd/system/serial-getty@ttyS0.service.d"
  printf '[Service]\nExecStart=\nExecStart=-/sbin/agetty --autologin root --noclear %%I 115200 linux\n' > "$mnt/etc/systemd/system/serial-getty@ttyS0.service.d/autologin.conf"
  echo 'root:root' | chroot "$mnt" chpasswd
  chroot "$mnt" systemctl enable serial-getty@ttyS0.service >/dev/null 2>&1 || true
  # a loud, unmistakable success marker once userspace (on the iSCSI root) is up
  cat > "$mnt/etc/systemd/system/kb-sanmark.service" <<'EOF'
[Unit]
Description=KwickBoot SAN root marker
After=multi-user.target
[Service]
Type=oneshot
ExecStart=/bin/sh -c 'root=$(findmnt -no SOURCE /); echo "=== KWICKBOOT-SAN-ROOT-OK: / is $root over iSCSI ($(cat /etc/hostname)) ==="; echo "cmdline: $(cat /proc/cmdline)"'
StandardOutput=journal+console
StandardError=journal+console
[Install]
WantedBy=multi-user.target
EOF
  chroot "$mnt" systemctl enable kb-sanmark.service >/dev/null 2>&1 || true
else
  say "   production image: no autologin; root login via SSH key / first-boot password (KB_SSH_KEY / KB_ROOT_PW), else root LOCKED"
  if [ -n "${KB_SSH_KEY:-}" ]; then
    mkdir -p "$mnt/root/.ssh"; printf '%s\n' "$KB_SSH_KEY" > "$mnt/root/.ssh/authorized_keys"
    chmod 700 "$mnt/root/.ssh"; chmod 600 "$mnt/root/.ssh/authorized_keys"
  fi
  if [ -n "${KB_ROOT_PW:-}" ]; then echo "root:$KB_ROOT_PW" | chroot "$mnt" chpasswd
  else chroot "$mnt" passwd -l root >/dev/null 2>&1 || true; fi
  chroot "$mnt" systemctl enable ssh >/dev/null 2>&1 || true
fi

say "5. initramfs with iscsi_tcp + the open-iscsi boot hook"
echo iscsi_tcp >> "$mnt/etc/initramfs-tools/modules"
echo e1000    >> "$mnt/etc/initramfs-tools/modules"
echo virtio_net >> "$mnt/etc/initramfs-tools/modules"
chroot "$mnt" update-initramfs -u

say "6. extlinux bootloader into the image (MBR + /boot/extlinux)"
kver=$(ls "$mnt/lib/modules" | sort -V | tail -1)
mkdir -p "$mnt/boot/extlinux"
cat > "$mnt/boot/extlinux/extlinux.conf" <<EOF
DEFAULT san
PROMPT 0
TIMEOUT 20
LABEL san
  KERNEL /boot/vmlinuz-$kver
  APPEND initrd=/boot/initrd.img-$kver root=LABEL=sanroot ro ip=dhcp console=tty0 console=ttyS0,115200
EOF
extlinux --install "$mnt/boot/extlinux"
# MBR boot code (path differs across releases)
mbr=""
for p in /usr/lib/syslinux/mbr/mbr.bin /usr/lib/SYSLINUX/mbr.bin /usr/share/syslinux/mbr.bin; do [ -f "$p" ] && mbr="$p" && break; done
[ -n "$mbr" ] || { echo "!! mbr.bin not found"; exit 1; }
dd if="$mbr" of="$loop" bs=440 count=1 conv=notrunc 2>/dev/null

say "7. copy kernel+initrd out (optional PXE/HTTP serve of boot config)"
cp "$mnt/boot/vmlinuz-$kver"    "$OUT/vmlinuz"
cp "$mnt/boot/initrd.img-$kver" "$OUT/initrd.img"

sync
cleanup; mnt=""; loop=""
bytes=$(stat -c%s "$IMG")
printf '\n=== GOLDEN READY ===\n  image : %s (%s MB)\n  kernel: %s\n  initrd: %s\n  next  : san.provision("golden", "%s")  then  san.clone("golden","srv01",mac)\n' \
  "$IMG" "$((bytes/1024/1024))" "$OUT/vmlinuz" "$OUT/initrd.img" "$IMG"
