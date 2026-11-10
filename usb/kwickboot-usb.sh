#!/bin/sh
# KwickBoot USB Creator (Linux) - SPDX-License-Identifier: AGPL-3.0-or-later
# Same stick as the Windows creator:
#   partition 1  KWICKDATA  exFAT, the rest: kwickboot.cfg (installer answers) + iso/ (dropped ISOs -> the server's library)
#   partition 2  KWICKBOOT  FAT32 EFI system partition: the verified ISO's files (UEFI, Secure Boot via signed shim)
# Checks the release signature (gpgv + the built-in release key) and the ISO's SHA-256 first, reads everything back after.
#   sudo sh kwickboot-usb.sh [--iso <file>] [--disk /dev/sdX --confirm <serial>] [--image <file> --size 8G]
#        [--host <name>] [--tz Area/City] [--static 192.168.1.10/24 --gw <ip> --dns <ip,ip>] [--install-disk ask|only-internal|<serial>]
#        [--start live|install] [--update] [--boot-mb 2048] [--yes]
#   --start: what the stick does when a PC starts from it - live: run KwickBoot from the stick (default, nothing is
#            installed; settings + ISOs stay on the stick), install: the installer (the menu always offers both)
#   --image: build a stick image file instead of writing a disk (testing, VMs; dd it to a stick later)
# Needs: sfdisk (util-linux), mkfs.exfat (exfatprogs), mkfs.vfat (dosfstools), gpgv (gpgv/gnupg), wget or curl.
set -eu
URL=https://kwickboot.com/download/latest/
FPR=2457EAE1870074A68CC9630A6A315D0AE6383C48
ISO=""; DISK=""; CONFIRM=""; IMAGE=""; SIZE=8G; HN=""; TZN=""; ST=""; GW=""; DNS=""; IDISK=""; START=live; UPDATE=0; BOOTMB=2048; YES=0
while [ $# -gt 0 ]; do case "$1" in
  --iso) ISO=$2; shift ;; --disk) DISK=$2; shift ;; --confirm) CONFIRM=$2; shift ;; --image) IMAGE=$2; shift ;; --size) SIZE=$2; shift ;;
  --host) HN=$2; shift ;; --tz) TZN=$2; shift ;; --static) ST=$2; shift ;; --gw) GW=$2; shift ;; --dns) DNS=$2; shift ;;
  --install-disk) IDISK=$2; shift ;; --start) START=$2; shift ;; --update) UPDATE=1 ;; --boot-mb) BOOTMB=$2; shift ;; --yes|-y) YES=1 ;; --url) URL=$2; shift ;;
  -h|--help) sed -n '2,12p' "$0"; exit 0 ;; *) echo "unknown option $1"; exit 1 ;; esac; shift; done
say() { printf '\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
ask() { if [ "$YES" = 1 ]; then REPLY=$2; else printf '%s%s ' "$1" "${2:+ [$2]}"; read -r REPLY; REPLY=${REPLY:-$2}; fi; }
[ "$(id -u)" = 0 ] || die "run as root (sudo): it partitions the stick"
for t in sfdisk mkfs.exfat mkfs.vfat gpgv sha256sum losetup; do command -v $t >/dev/null || die "missing $t (apt install util-linux exfatprogs dosfstools gpgv)"; done
echo "KwickBoot USB Creator (Linux)"
echo "KwickBoot installs, restores and wipes disks. You are solely responsible for your data and its backups. NO WARRANTY (AGPL-3.0)."
W=$(mktemp -d); trap 'umount -q "$W/iso" "$W/boot" "$W/data" 2>/dev/null || true; [ -n "${LOOP:-}" ] && losetup -d "$LOOP" 2>/dev/null; rm -rf "$W"' EXIT
mkdir -p "$W/iso" "$W/boot" "$W/data"

# ---- 1. release: download or given ISO; signature (built-in key) + SHA-256
if [ -z "$ISO" ]; then
  get() { if command -v wget >/dev/null; then wget -q -O "$2" "$1"; else curl -fsSL -o "$2" "$1"; fi; }
  say "Downloading the release list from $URL"
  D=${XDG_CACHE_HOME:-/var/tmp}/kwickboot-usb; mkdir -p "$D"
  get "${URL}SHA256SUMS" "$D/SHA256SUMS"; get "${URL}SHA256SUMS.asc" "$D/SHA256SUMS.asc"
  n=$(awk '$2 ~ /^\*?kwickboot-[0-9A-Za-z.-]+-amd64\.iso$/ {sub(/^\*/,"",$2); print $2; exit}' "$D/SHA256SUMS")
  [ -n "$n" ] || die "no ISO in the release list"
  [ -f "$D/$n" ] || { say "Downloading $n (about 800 MB)"; get "$URL$n" "$D/$n.part"; mv "$D/$n.part" "$D/$n"; }
  ISO=$D/$n
fi
[ -f "$ISO" ] || die "ISO not found: $ISO"
DIR=$(cd "$(dirname "$ISO")" && pwd); NAME=$(basename "$ISO"); VERIFIED=0
if [ -f "$DIR/SHA256SUMS" ] && [ -f "$DIR/SHA256SUMS.asc" ]; then
  say "Checking the release signature"
  sed -n '/^-----BEGIN PGP PUBLIC KEY BLOCK-----$/,/^-----END PGP PUBLIC KEY BLOCK-----$/p' "$0" | gpg --dearmor > "$W/key.gpg" 2>/dev/null \
    || die "gpg (to read the built-in key) missing: apt install gnupg"
  out=$(gpgv --keyring "$W/key.gpg" --status-fd 1 "$DIR/SHA256SUMS.asc" "$DIR/SHA256SUMS" 2>/dev/null || true)
  echo "$out" | grep -q "VALIDSIG $FPR" || die "SHA256SUMS signature is NOT good - do not use this download"
  echo "    Good signature: Jins Tech Corp KwickBoot Release"
  want=$(awk -v n="$NAME" '{f=$2; sub(/^\*/,"",f)} f==n {print $1; exit}' "$DIR/SHA256SUMS"); [ -n "$want" ] || die "$NAME is not in the signed SHA256SUMS"
  say "Checking the ISO's SHA-256"
  [ "$(sha256sum "$ISO" | cut -d' ' -f1)" = "$want" ] || die "$NAME is damaged or changed - download it again"
  echo "    SHA-256 OK"; VERIFIED=1
else
  echo "WARNING: no SHA256SUMS + SHA256SUMS.asc next to $NAME - the ISO cannot be checked."
  ask "Type UNVERIFIED to use it anyway:" ""; [ "$REPLY" = UNVERIFIED ] || die "stopped"
fi

# ---- 2. the target: an image file, or a USB disk (never anything else)
if [ -n "$IMAGE" ]; then
  [ "$UPDATE" = 1 ] || { rm -f "$IMAGE"; truncate -s "$SIZE" "$IMAGE"; }
  LOOP=$(losetup -f --show -P "$IMAGE"); DEV=$LOOP; P1=${LOOP}p1; P2=${LOOP}p2
else
  echo; echo " USB disks:"
  lsblk -dnpo NAME,TRAN,SIZE,MODEL,SERIAL | awk '$2=="usb"' | sed 's/^/   /'
  [ -n "$DISK" ] || { ask "Which disk? (e.g. /dev/sdb)" ""; DISK=$REPLY; }
  [ "$(lsblk -dno TRAN "$DISK" 2>/dev/null)" = usb ] || die "$DISK is not a USB disk"
  grep -q "^$DISK" /proc/mounts && [ "$UPDATE" = 0 ] && for m in $(awk -v d="$DISK" 'index($1,d)==1{print $2}' /proc/mounts); do umount "$m"; done
  SER=$(lsblk -dno SERIAL "$DISK" | tr -d ' '); ID=${SER:-$(lsblk -dno MODEL "$DISK" | tr -d ' ')}
  DEV=$DISK; case "$DISK" in *[0-9]) P1=${DISK}p1; P2=${DISK}p2 ;; *) P1=${DISK}1; P2=${DISK}2 ;; esac
  if [ "$UPDATE" = 0 ]; then
    echo "WARNING: EVERYTHING on $DISK ($(lsblk -dno SIZE,MODEL "$DISK")) will be ERASED."
    [ -n "$CONFIRM" ] || { printf "Type the disk's serial (%s) to confirm: " "$ID"; read -r CONFIRM; }
    [ "$CONFIRM" = "$ID" ] || die "not confirmed - nothing was changed"
  fi
fi
SZ=$(blockdev --getsize64 "$DEV")
[ "$UPDATE" = 1 ] || [ "$SZ" -ge $(( (BOOTMB + 1024) * 1048576 )) ] || die "the stick is too small"
[ "$(stat -c %s "$ISO")" -lt $(( BOOTMB * 1048576 * 9 / 10 )) ] || die "boot partition too small for this ISO (--boot-mb)"

# ---- 3. partitions (GPT): KWICKDATA first, KWICKBOOT (EFI system partition) at the end
if [ "$UPDATE" = 0 ]; then
  say "Creating KWICKDATA (exFAT) + KWICKBOOT (FAT32 EFI, $BOOTMB MB)"
  wipefs -qa "$DEV" 2>/dev/null || true
  datamb=$(( SZ / 1048576 - BOOTMB - 2 ))
  # MBR (not GPT) so the same stick boots legacy BIOS too; partition 2 = type EF (EFI system partition) for UEFI
  printf 'label: dos\nsize=%sMiB, type=7\ntype=ef, bootable\n' "$datamb" \
    | sfdisk -q "$DEV"
  partprobe "$DEV" 2>/dev/null || blockdev --rereadpt "$DEV" 2>/dev/null || true; udevadm settle 2>/dev/null || sleep 2
  mkfs.exfat -q -L KWICKDATA "$P1" >/dev/null 2>&1 || mkfs.exfat -n KWICKDATA "$P1" >/dev/null
fi
[ -b "$P2" ] || die "no boot partition $P2 (not a KwickBoot stick? run without --update)"
mkfs.vfat -F 32 -n KWICKBOOT "$P2" >/dev/null

# ---- 4. the ISO's files -> KWICKBOOT, then compare after dropping the page cache
say "Copying the KwickBoot files"
mount -o ro,loop "$ISO" "$W/iso"; mount "$P2" "$W/boot"
cp -r "$W/iso/." "$W/boot/"; sync; umount "$W/boot"
echo 3 > /proc/sys/vm/drop_caches; blockdev --flushbufs "$P2" 2>/dev/null || true
mount -o ro "$P2" "$W/boot"
say "Verifying: reading the files back from the stick"
( cd "$W/iso" && find . -type f -print0 | xargs -0 sha256sum ) > "$W/sums"
( cd "$W/boot" && sha256sum --quiet -c "$W/sums" ) || die "files differ on the stick - it may be faulty; try another one"
echo "    all $(wc -l < "$W/sums") files identical"
umount "$W/boot"
# legacy BIOS: GRUB's MBR code (bytes 0-439; the partition table stays) + its core in the gap before partition 1
if [ -f "$W/iso/boot/grub/kb-usb-mbr.bin" ] && [ -f "$W/iso/boot/grub/kb-usb-core.img" ]; then
  first=$(sfdisk -d "$DEV" | sed -n 's/.*start= *\([0-9]*\),.*/\1/p' | sort -n | head -n1)
  n=$(( ($(stat -c %s "$W/iso/boot/grub/kb-usb-core.img") + 511) / 512 ))
  if [ "$(sfdisk -d "$DEV" | sed -n 's/^label: //p')" = dos ] && [ "${first:-0}" -gt $((n + 1)) ]; then
    dd if="$W/iso/boot/grub/kb-usb-mbr.bin" of="$DEV" bs=440 count=1 conv=notrunc status=none
    dd if="$W/iso/boot/grub/kb-usb-core.img" of="$DEV" bs=512 seek=1 conv=notrunc status=none; sync
    echo "    legacy BIOS boot written"
  else echo "WARNING: not an MBR stick with room before partition 1 - it boots UEFI only (remake it without --update)"; fi
else echo "WARNING: this ISO has no USB BIOS loader (older version) - the stick boots UEFI only"; fi
umount "$W/iso"

# ---- 5. KWICKDATA: kwickboot.cfg + iso/ + README
mount "$P1" "$W/data"; mkdir -p "$W/data/iso"
if [ "$UPDATE" = 0 ]; then
  case "$START" in live|install) ;; *) die "--start: live or install" ;; esac
  { echo "# KwickBoot stick settings (KwickBoot USB Creator, $(date +%F)). Delete a line to be asked on screen."
    echo "# start=live: run KwickBoot from the stick (nothing installed). start=install: install it onto the PC's disk."
    echo "start=$START"
    echo "# No passwords here: the first admin is created in the web console with the setup code shown on the server's screen."
    if [ -n "$HN$TZN$ST$IDISK" ]; then
      echo "host=${HN:-kwickboot}"; echo "timezone=${TZN:-UTC}"
      if [ -n "$ST" ]; then echo "network=static"; echo "address=$ST"; echo "gateway=$GW"; echo "dns=${DNS:-$GW}"; else echo "network=dhcp"; fi
      echo "test_lab=no"; echo "disk=${IDISK:-ask}"
    else printf '# host=kwickboot\n# timezone=America/Chicago\n# network=dhcp\n# disk=ask\n'; fi
    echo "import_isos=yes"; } | sed 's/$/\r/' > "$W/data/kwickboot.cfg"
  [ "$IDISK" = only-internal ] && echo "WARNING: this stick ERASES the only internal disk of any PC it starts (30 s countdown; never a disk with a KwickBoot server)."
  printf 'KwickBoot USB stick\r\n\r\nStart a PC from this stick (UEFI; Secure Boot may stay ON) to install the KwickBoot server.\r\nkwickboot.cfg - installer answers. iso\\ - drop ISO files here: copied into the new server'"'"'s library.\r\n' > "$W/data/README.txt"
fi
umount "$W/data"; sync
say "Done: KwickBoot stick ($NAME$( [ "$VERIFIED" = 1 ] || echo ', UNVERIFIED'))${IMAGE:+ - image $IMAGE}"
exit 0
# ---- the release public key (Jins Tech Corp KwickBoot Release <release@kwickboot.com>, fingerprint above)
-----BEGIN PGP PUBLIC KEY BLOCK-----

mDMEauqajxYJKwYBBAHaRw8BAQdA6HpYOsCX9WcCXj6PBWqC3YVS8R/OCVnuqFy6
teRW+Km0OEppbnMgVGVjaCBDb3JwIEt3aWNrQm9vdCBSZWxlYXNlIDxyZWxlYXNl
QGt3aWNrYm9vdC5jb20+iJYEExYKAD4WIQQkV+rhhwB0pozJYwpqMV0K5jg8SAUC
auqajwIbAwUJBaOagAULCQgHAgYVCgkICwIEFgIDAQIeAQIXgAAKCRBqMV0K5jg8
SCbaAQCfQQ8DYpTnwyrAwgSkhiQyREBM5WUVLByt6mKKhbrYHgEA1oOa0ttkNlCn
7xblI5iTIvIYHWarHwznc2rSf27f0Ag=
=lktx
-----END PGP PUBLIC KEY BLOCK-----
