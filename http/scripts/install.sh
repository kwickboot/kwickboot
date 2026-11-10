#!/bin/sh
# PXE toolkit install engine - runs inside the RAM OS. Every OS is installed the same way:
#   1 confirm  2 preflight  3 partition  4 deploy  5 configure  6 bootloader  7 verify  8 finish
#
#   OS=<recipe> DISK=/dev/x CONFIRM=<disk serial> HOSTNAME=<name> [TZ=..] [LAN=..] [BUNDLE=..] [FIN=reboot|none] sh install.sh
# Recipes: http/recipes/<OS>.sh  (fetched from the toolkit server). WIPES DISK.
set -e
SRV="http://{{SERVER_IP}}:{{HTTP_PORT}}"
TK=/root/tk
: "${OS:?set OS (recipe name)}" "${DISK:?set DISK}" "${CONFIRM:?set CONFIRM=<disk serial>}" "${HOSTNAME:?set HOSTNAME}"
[ -n "$TZ" ] || TZ=America/Chicago
[ -n "$LAN" ] || LAN=$(ip -4 -o addr show | awk '$2!="lo"{print $4; exit}' | sed 's#\.[0-9]*/#.0/#')
[ -n "$LOCALE" ] || LOCALE=en_US.UTF-8       # worldwide install: language/locale (e.g. ar_SA.UTF-8, de_DE.UTF-8)
[ -n "$KEYMAP" ] || KEYMAP=us                # keyboard layout (XKB, e.g. ara, he, de, fr, es, br); RTL follows the locale
export TZ LAN LOCALE KEYMAP
T=/mnt/t
KB_T0=$(date +%s)
step() { echo; echo ">>> [$1] $2   (+$(( $(date +%s) - KB_T0 ))s)"; }
# release everything an install mounted under $T (deepest first) - after a failure too, so a retry finds the disk free
umount_target() { sync; for m in $(awk -v t="$T" '$2==t || index($2, t"/")==1 {print $2}' /proc/mounts | sort -r); do umount "$m" 2>/dev/null || umount -l "$m"; done; }
# recipe helper: a judgment call (RAM below the recommendation, Secure Boot on...) - stop unless the user chose "install anyway"
choice() { if [ "${FORCE:-0}" = 1 ]; then echo "WARN  $* (accepted by the user - installing anyway)"; else echo "STOP  $* - choose 'install anyway' to proceed"; return 1; fi; }
die()  { echo "STOP: $*"; umount_target; exit 1; }

# ---- prebuilt image: stream + verify + unpack on all cores (any recipe can call it from recipe_deploy)
KB_FROM_IMAGE=0
image_deploy() {   # $1 = image name on the server (http/images/<name>.tar.zst)
  [ -n "$1" ] || return 1
  want=$(wget -q -O - "$SRV/images/$1.tar.zst.sha256" 2>/dev/null | cut -d' ' -f1)
  [ ${#want} = 64 ] || { echo "no prebuilt image '$1' on the server - building from packages instead"; return 1; }
  command -v zstd >/dev/null || { echo "zstd missing - building from packages instead"; return 1; }
  echo "image install: $1 ($(wget -q -O - "$SRV/images/$1.info" 2>/dev/null))"
  t0=$(date +%s); rm -f /tmp/kbimg.fifo; mkfifo /tmp/kbimg.fifo
  sha256sum < /tmp/kbimg.fifo | cut -d' ' -f1 > /tmp/kbimg.sum &
  if ! wget -q -O - "$SRV/images/$1.tar.zst" | tee /tmp/kbimg.fifo | zstd -dc -T0 | tar -x --xattrs --xattrs-include='*' --acls --numeric-owner -C "$T"; then
    wait; echo "image download/unpack failed"; return 2
  fi
  wait
  [ "$(cat /tmp/kbimg.sum)" = "$want" ] || { echo "IMAGE CHECKSUM MISMATCH - refusing this system"; return 2; }
  KB_FROM_IMAGE=1; export KB_FROM_IMAGE
  echo "image ok: sha256 verified, $(du -sh $T 2>/dev/null | cut -f1) in $(( $(date +%s) - t0 ))s"
}

# ---- recipe
wget -qO $TK/recipe.sh "$SRV/recipes/$OS.sh" || die "recipe '$OS' not found on server"
. $TK/recipe.sh
echo "recipe: $RECIPE_NAME ($RECIPE_METHOD)"

step 1 "confirm target disk"
[ -b "$DISK" ] || die "$DISK is not a block device"
n=$(basename "$DISK"); s=$(lsblk -dno SERIAL "$DISK" 2>/dev/null | tr -d ' ')
for f in /sys/block/$n/serial /sys/block/$n/device/serial; do [ -z "$s" ] && [ -r "$f" ] && s=$(tr -d ' \n' < "$f"); done; [ -z "$s" ] && [ -r /sys/block/$n/device/vpd_pg80 ] && s=$(tail -c +5 /sys/block/$n/device/vpd_pg80 | tr -d ' \n\000')   # SATA/SAS: serial from the VPD page (no udev here)
[ -n "$s" ] || s="size-$(lsblk -dno SIZE "$DISK" | tr -d ' ')"
[ "$CONFIRM" = "$s" ] || die "CONFIRM '$CONFIRM' does not match $DISK serial '$s'"
umount_target    # leftovers of an earlier failed job (only KwickBoot's own target mount point)
lsblk -nro MOUNTPOINTS "$DISK" | grep -q . && die "$DISK has mounted filesystems"
echo "target $DISK serial $s size $(lsblk -dno SIZE $DISK | tr -d ' ') confirmed"
# every tool the wipe + rebuild needs must be here BEFORE anything is erased
miss=""; for t in wipefs sgdisk partprobe mkfs.ext4 mkfs.vfat mount blkid zstd tar; do command -v $t >/dev/null || miss="$miss $t"; done
[ -z "$miss" ] || die "tools missing in the RAM OS:$miss - disk NOT touched (reboot the machine into KwickBoot and retry)"

step 2 "preflight"
DISK="$DISK" OS="$OS" FORCE="${FORCE:-0}" sh $TK/preflight.sh || die "preflight found blocking problems"
. $TK/preflight.env
type recipe_requirements >/dev/null 2>&1 && { recipe_requirements || die "recipe requirements not met"; }
# the server already required the disk serial (CONFIRM) and the web UI asked the operator: 3 s is a last-chance pause, not the safety
echo "WIPING $DISK in 3 s"; sleep 3

case "$DISK" in *[0-9]) P="${DISK}p" ;; *) P="$DISK" ;; esac
# package cache on the KwickBoot server (apt-cacher-ng :3142): first machine downloads, the rest install from the LAN
KB_PROXY=""
if wget -q -T 3 -O /dev/null "http://{{SERVER_IP}}:3142/acng-report.html" 2>/dev/null; then
  KB_PROXY="http://{{SERVER_IP}}:3142"; echo "package cache: $KB_PROXY"
else echo "package cache: not available - downloading directly"; fi
export DISK P T BOOT_MODE EXTRA_PKGS KERNEL_OPTS HOSTNAME BUNDLE VARIANT KIOSK_URL_B64 KIOSK_ALLOW_B64 KIOSK_IDLE KIOSK_RO EP_TOKEN KB_PROXY

# verification helper for install.sh and every recipe_verify (image recipes too): counts failures in $fails
chk() { if eval "$2"; then echo "  OK    $1"; else echo "  FAIL  $1"; fails=$((fails+1)); fi; }

if [ "$RECIPE_METHOD" = image ] || [ "$RECIPE_METHOD" = self ]; then
  wget -qO $TK/authorized_keys "$SRV/authorized_keys"
  step 3 "deploy ($RECIPE_METHOD: the recipe brings its own disk layout and bootloader)"
  recipe_deploy || die "deploy failed"
  step 7 "verify"
  fails=0
  if type recipe_verify >/dev/null 2>&1; then recipe_verify || fails=$((fails+1)); fi
  [ $fails -eq 0 ] || die "$fails verification checks failed - NOT rebooting"
else
  step 3 "partition ($BOOT_MODE)"
  wipefs -a -q "$DISK"* 2>/dev/null || true
  # whole-disk TRIM: SSD/NVMe get their flash back (faster writes); virtual disks shrink. Best effort (HDDs skip it).
  { timeout 120 blkdiscard -f "$DISK" 2>/dev/null || timeout 120 blkdiscard "$DISK" 2>/dev/null; } && echo "  discarded old data on $DISK (TRIM)"
  # old signatures at the END of the disk (ZFS keeps 2 of its 4 labels there) survive wipefs: zero both ends
  sz=$(( $(cat /sys/class/block/$(basename $DISK)/size) / 2048 )); dd if=/dev/zero of="$DISK" bs=1M count=10 conv=fsync 2>/dev/null || true; dd if=/dev/zero of="$DISK" bs=1M seek=$(( sz - 10 )) count=10 conv=fsync 2>/dev/null || true
  sgdisk --zap-all "$DISK" >/dev/null
  if [ "$BOOT_MODE" = uefi ]; then
    sgdisk -n1:1MiB:+512MiB -t1:EF00 -c1:EFI -n2:0:0 -t2:8304 -c2:root "$DISK" >/dev/null
  else
    sgdisk -n1:1MiB:+2MiB -t1:EF02 -c1:biosgrub -n2:0:0 -t2:8304 -c2:root "$DISK" >/dev/null
  fi
  partprobe "$DISK" 2>/dev/null || true; sleep 2
  [ "$BOOT_MODE" = uefi ] && { mkfs.vfat -F32 -n EFI "${P}1" >/dev/null 2>/tmp/mkfs.err || { cat /tmp/mkfs.err; die "mkfs.vfat failed on ${P}1"; }; }
  mkfs.ext4 -q -F -L root "${P}2"
  mkdir -p $T; mount "${P}2" $T
  [ "$BOOT_MODE" = uefi ] && { mkdir -p $T/boot/efi; mount "${P}1" $T/boot/efi; }

  step 4 "deploy root filesystem"
  recipe_deploy

  step 5 "configure (network, SSH key, firewall, firmware, hostname)"
  wget -qO $TK/authorized_keys "$SRV/authorized_keys" || : > $TK/authorized_keys     # none configured: no root SSH keys
  recipe_configure
  # managed from the KwickBoot web console (users, passwords, updates, reboot): per-machine token from the install job.
  # Kiosks bring their own agent (profiles + screenshots); everything else gets the generic one.
  if [ -n "$EP_TOKEN" ] && [ ! -e $T/usr/local/sbin/kb-endpoint ]; then
    mkdir -p $T/usr/local/sbin
    wget -qO $T/usr/local/sbin/kb-endpoint "$SRV/scripts/kb-endpoint.sh" || die "cannot fetch the endpoint agent"
    chmod 700 $T/usr/local/sbin/kb-endpoint
    # encrypted channel: HTTPS to the server, trusting exactly the server's public key (pinned; no CA needed)
    read -r eph eppin <<EOF
$(wget -qO- "$SRV/ep/pin" 2>/dev/null)
EOF
    case "$eppin" in sha256//*) EPURL="https://$eph" ;; *) EPURL="$SRV"; eppin=""; echo "WARN  endpoint channel is plain HTTP (server has ep_https_port off)" ;; esac
    printf 'SERVER=%s\nURL=%s\nPIN=%s\nTOKEN=%s\n' "${SRV#http://}" "$EPURL" "$eppin" "$EP_TOKEN" > $T/etc/kb-endpoint.conf; chmod 600 $T/etc/kb-endpoint.conf
    printf '[Unit]\nDescription=KwickBoot endpoint check-in\nAfter=network-online.target\nWants=network-online.target\n[Service]\nType=oneshot\nTimeoutStartSec=120\nExecStart=-/usr/local/sbin/kb-endpoint\n' > $T/etc/systemd/system/kb-endpoint.service
    printf '[Unit]\nDescription=KwickBoot endpoint check-in every 30 s\n[Timer]\nOnBootSec=30\nOnUnitActiveSec=30\nAccuracySec=5\n[Install]\nWantedBy=timers.target\n' > $T/etc/systemd/system/kb-endpoint.timer
    mkdir -p $T/etc/systemd/system/timers.target.wants
    ln -sf /etc/systemd/system/kb-endpoint.timer $T/etc/systemd/system/timers.target.wants/kb-endpoint.timer
    echo "managed by KwickBoot: endpoint agent installed (web console: users, passwords, updates, reboot)"
  fi

  step 6 "bootloader"
  recipe_bootloader

  step 7 "verify"
  fails=0
  chk "kernel present"                 'ls $T/boot/vmlinuz-* >/dev/null 2>&1'
  chk "initramfs present"              'ls $T/boot/initrd.img-* >/dev/null 2>&1'
  chk "fstab root UUID exists"         'blkid -U "$(awk '"'"'$2=="/"{sub("UUID=","",$1);print $1}'"'"' $T/etc/fstab)" >/dev/null'
  chk "grub.cfg has a linux entry"     'grep -q "^\s*linux\s" $T/boot/grub/grub.cfg'
  [ "$BOOT_MODE" = uefi ] && chk "EFI fallback loader present" '[ -f $T/boot/efi/EFI/BOOT/BOOTX64.EFI ]'
  chk "SSH authorized_keys installed"  '[ -s $T/root/.ssh/authorized_keys ]'
  chk "network config present"         'ls $T/etc/systemd/network/*.network >/dev/null 2>&1 || grep -q dhcp $T/etc/network/interfaces 2>/dev/null || ls $T/etc/netplan/*.yaml >/dev/null 2>&1'
  [ -n "$EP_TOKEN" ] && chk "endpoint agent (web console management)" '[ -x $T/usr/local/sbin/kb-endpoint ] && [ -s $T/etc/kb-endpoint.conf ] && [ -L $T/etc/systemd/system/timers.target.wants/kb-endpoint.timer ]'
  type recipe_verify >/dev/null 2>&1 && recipe_verify
  [ $fails -eq 0 ] || die "$fails verification checks failed - NOT rebooting"
  # systemd RAM OS: bind-mounted /sys keeps cgroup mounts busy -> flush first, then lazy-unmount what remains
  # unmount everything below $T, deepest first (works with busybox umount too - no -R needed)
  umount_target
fi

step 8 "finish"
# Boot order: KwickBoot stays in control. Network (PXE) entries first, then the new OS, then the rest:
# every boot asks the server, which hands the machine to its disk (default=local) or to the RAM OS when IT needs it.
# If the server is unreachable, the firmware falls through to the installed OS. BOOT_ORDER=os-first keeps the old behaviour.
if [ -d /sys/firmware/efi ] && command -v efibootmgr >/dev/null; then
  all=$(efibootmgr | sed -n 's/^BootOrder: //p' | tr ',' ' ')
  cur=$(efibootmgr | awk '/^Boot[0-9A-F]{4}\*? /{print}' | grep -iE "$RECIPE_EFI_LABEL" | head -n1 | cut -c5-8)
  net=$(efibootmgr | awk '/^Boot[0-9A-F]{4}\*? /{print}' | grep -iE 'PXE|IPv4|Network|NIC' | grep -viE 'IPv6|HTTP' | cut -c5-8 | tr '\n' ' ')
  if [ "${BOOT_ORDER:-network-first}" = os-first ]; then first="$cur"; else first="$net $cur"; fi
  order=""; for b in $first $all; do case " $order " in *" $b "*) ;; *) [ -n "$b" ] && order="$order $b" ;; esac; done
  order=$(echo $order | tr ' ' ',')
  if [ -n "$order" ] && efibootmgr -o "$order" >/dev/null 2>&1; then
    echo "UEFI boot order: $order ($( [ -n "$net" ] && [ "${BOOT_ORDER:-network-first}" != os-first ] && echo "network first, then $RECIPE_EFI_LABEL" || echo "$RECIPE_EFI_LABEL first"))"
  else echo "UEFI boot order unchanged"; fi
fi
echo "INSTALL OK: $RECIPE_NAME on $DISK as $HOSTNAME"
