#!/bin/sh
# KwickBoot image restore - runs inside the RAM OS. A captured golden image (capture.sh) -> this machine's disk:
# the partition table, then every partition streamed from the server and checked against the SHA-256 in the image's
# manifest while it is written. WIPES the target disk (serial-confirmed). MODE=unicast (default) or multicast.
# SPDX-License-Identifier: AGPL-3.0-or-later
#   NAME=<image name> DISK=/dev/x CONFIRM=<disk serial> [MODE=unicast|multicast] [HOSTNAME=pc01] sh restore.sh
SRV="http://{{SERVER_IP}}:{{HTTP_PORT}}"
: "${NAME:?set NAME}" "${DISK:?set DISK}" "${CONFIRM:?set CONFIRM=<disk serial>}"
KB_T0=$(date +%s)
step() { echo; echo ">>> [$1] $2   (+$(( $(date +%s) - KB_T0 ))s)"; }
die()  { echo "STOP: $*"; exit 1; }
IMG="$SRV/images/captures/$NAME"
jget() { sed -n "s/.*\"$1\":\"\{0,1\}\([^\",}]*\)\"\{0,1\}.*/\1/p"; }      # one value from a flat JSON snippet

step 1 "confirm target disk"
echo "$NAME" | grep -qE '^[a-z0-9][a-z0-9._-]{0,60}$' || die "bad image name"
[ -b "$DISK" ] || die "$DISK is not a block device"
n=$(basename "$DISK"); s=$(lsblk -dno SERIAL "$DISK" 2>/dev/null | tr -d ' ')
for f in /sys/block/$n/serial /sys/block/$n/device/serial; do [ -z "$s" ] && [ -r "$f" ] && s=$(tr -d ' \n' < "$f"); done
[ -z "$s" ] && [ -r /sys/block/$n/device/vpd_pg80 ] && s=$(tail -c +5 /sys/block/$n/device/vpd_pg80 | tr -d ' \n\000')
[ -n "$s" ] || s="size-$(lsblk -dno SIZE "$DISK" | tr -d ' ')"
[ "$CONFIRM" = "$s" ] || die "CONFIRM '$CONFIRM' does not match $DISK serial '$s'"
lsblk -nro MOUNTPOINTS "$DISK" | grep -q . && die "$DISK has mounted filesystems"
wget -qO /tmp/kbres.json "$IMG/manifest.json" || die "image '$NAME' not found on the server"
need=$(jget disk_bytes < /tmp/kbres.json | head -n1); have=$(( $(cat /sys/class/block/$n/size) * 512 ))
[ "$have" -ge "$need" ] || echo "  NOTE  target ($((have / 1000000000)) GB) is smaller than the source ($((need / 1000000000)) GB) - works only if the partitions fit"
for t in partclone.restore zstd sgdisk sha256sum; do command -v $t >/dev/null || die "tool $t missing (imaging pack not loaded) - disk NOT touched"; done
grep -q table.sfdisk /tmp/kbres.json && ! command -v sfdisk >/dev/null && die "this image has an MBR table and sfdisk is missing - disk NOT touched"
boot=$(jget boot < /tmp/kbres.json | tail -n1); mine=$([ -d /sys/firmware/efi ] && echo uefi || echo bios)
[ "$boot" = "$mine" ] || echo "  WARN  image was captured on a $boot machine, this one boots $mine - it may not start"
echo "target $DISK serial $s, $((have / 1000000000)) GB - image '$NAME' from $(jget model < /tmp/kbres.json | tail -n1)"
echo "WIPING $DISK in 3 s"; sleep 3

step 3 "partition table"
case "$DISK" in *[0-9]) P="${DISK}p" ;; *) P="$DISK" ;; esac
TF=$(jget table_file < /tmp/kbres.json | head -n1); TSHA=$(jget table_sha256 < /tmp/kbres.json | head -n1)
wget -qO /tmp/kbres.pt "$IMG/$TF" || die "table file missing"
[ "$(sha256sum < /tmp/kbres.pt | cut -d' ' -f1)" = "$TSHA" ] || die "partition table CHECKSUM MISMATCH - disk NOT touched"
wipefs -a -q "$DISK" 2>/dev/null; sgdisk --zap-all "$DISK" >/dev/null 2>&1
# whole-disk TRIM: SSD/NVMe get their flash back (faster writes); virtual disks shrink. Best effort (HDDs skip it).
{ timeout 120 blkdiscard -f "$DISK" 2>/dev/null || timeout 120 blkdiscard "$DISK" 2>/dev/null; } && echo "  discarded old data on $DISK (TRIM)"
# old signatures at the END of the disk (ZFS keeps 2 of its 4 labels there, GPT its backup) survive wipefs: zero both ends
sz=$(( $(cat /sys/class/block/$n/size) / 2048 )); dd if=/dev/zero of="$DISK" bs=1M count=10 conv=fsync 2>/dev/null; dd if=/dev/zero of="$DISK" bs=1M seek=$(( sz - 10 )) count=10 conv=fsync 2>/dev/null
case "$TF" in
  table.gpt) sgdisk --load-backup=/tmp/kbres.pt "$DISK" >/dev/null && sgdisk -e "$DISK" >/dev/null || die "cannot write the GPT" ;;
  *)         sfdisk -q "$DISK" < /tmp/kbres.pt || die "cannot write the MBR table" ;;
esac
partprobe "$DISK" 2>/dev/null; sleep 2
echo "  OK    table written ($TF, sha256 verified)"

step 4 "partitions"
rm -f /tmp/kbres.fifo; mkfifo /tmp/kbres.fifo
IFC=$(ip route show default | awk '{print $5; exit}')
if [ "$MODE" = multicast ]; then
  if [ -n "$PORTBASE" ] && command -v udp-receiver >/dev/null; then echo "  multicast: receiving with the room on portbase $PORTBASE via $IFC"
    sysctl -qw net.core.rmem_max=67108864 net.core.rmem_default=33554432 net.core.netdev_max_backlog=16384 2>/dev/null || true   # no drops at line rate
  else echo "  NOTE  multicast not possible here (no portbase or udp-receiver) - fetching directly"; MODE=unicast; fi
fi
# write one partition from a source command (stdout = the compressed part); the SHA-256 is computed on the way in
write_part() {   # $1 = source command, $2 = tool, $3 = device
  sha256sum < /tmp/kbres.fifo | cut -d' ' -f1 > /tmp/kbres.sum &
  if [ "$2" = raw ]; then sh -c "$1" | tee /tmp/kbres.fifo | zstd -dc 2>/dev/null | dd of="$3" bs=4M 2>/dev/null
  else sh -c "$1" | tee /tmp/kbres.fifo | zstd -dc 2>/dev/null | partclone.restore -q -s - -o "$3" 2>/tmp/kbres.err; fi
  wait
}
tr '{' '\n' < /tmp/kbres.json | grep '"n":' | while read -r p; do
  num=$(echo "$p" | jget n); fs=$(echo "$p" | jget fs); dev="$P$num"
  if [ "$fs" = swap ]; then mkswap -q -U "$(echo "$p" | jget uuid)" "$dev" && echo "  OK    partition $num: swap recreated"; continue; fi
  file=$(echo "$p" | jget file); want=$(echo "$p" | jget sha256); tool=$(echo "$p" | jget tool)
  t1=$(date +%s); how=""
  if [ "$MODE" = multicast ]; then
    # waits for the server's stream of this part (the server waits up to 3 min for the whole room)
    # </dev/null: udpcast reads its input even with --nokbd - a pipe there makes it fail ("Receiver error") or eat the loop's list
    write_part "udp-receiver --portbase $PORTBASE --interface $IFC --nokbd --no-progress --start-timeout 240 --receive-timeout 60 </dev/null 2>/tmp/kbres.mc" "$tool" "$dev"
    if [ "$(cat /tmp/kbres.sum)" = "$want" ]; then how="multicast"
    else echo "  NOTE  partition $num: multicast stream missed or incomplete - fetching it and the rest directly"; MODE=unicast; fi
  fi
  if [ -z "$how" ]; then
    write_part "wget -qO - '$IMG/$file'" "$tool" "$dev"; how="direct"
  fi
  [ "$(cat /tmp/kbres.sum)" = "$want" ] || { echo "STOP: partition $num CHECKSUM MISMATCH ($file) - this disk is incomplete, re-run the restore"; exit 1; }
  echo "  OK    partition $num $fs written and verified in $(( $(date +%s) - t1 ))s ($how)"
done || exit 1
sync

step 6 "new identity for this machine"
# one image -> many PCs: each restored copy must not share the captured PC's identity
partprobe "$DISK" 2>/dev/null; sleep 1
HN=$(echo "${HOSTNAME:-}" | tr 'A-Z' 'a-z' | grep -E '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$')
for dev in $(lsblk -nrpo NAME,FSTYPE "$DISK" | awk '$2 ~ /^(ext4|ext3|ext2|xfs|btrfs|ntfs)$/{print $1}'); do
  fs=$(lsblk -nro FSTYPE "$dev"); mkdir -p /mnt/kbid
  if [ "$fs" = ntfs ]; then
    command -v ntfs-3g >/dev/null || continue
    ntfs-3g -o ro "$dev" /mnt/kbid 2>/dev/null || continue
    st=/mnt/kbid/Windows/Setup/State/State.ini
    if [ -f "$st" ]; then
      state=$(tr -d '\000\r' < "$st" | sed -n 's/^ImageState=//p')
      case "$state" in
        IMAGE_STATE_GENERALIZE_RESEAL_TO_OOBE|IMAGE_STATE_SPECIALIZE_RESEAL_TO_OOBE)
          echo "  OK    Windows image is generalized (sysprep): it creates a new identity (SID, name) at first start" ;;
        *) echo "  WARN  Windows image is NOT generalized ($state): every restored PC shares the captured PC's identity"
           echo "        (security ID, computer name, domain membership). Run 'sysprep /generalize /oobe /shutdown' on the"
           echo "        source PC before capturing, or deploy fresh installs (Install) instead of a golden image." ;;
      esac
    fi
    umount /mnt/kbid; continue
  fi
  mount "$dev" /mnt/kbid 2>/dev/null || continue
  if [ -f /mnt/kbid/etc/os-release ]; then          # a Linux root file system
    # machine-id: empty file = systemd creates a new one at first boot (and dbus follows it)
    [ -f /mnt/kbid/etc/machine-id ] && : > /mnt/kbid/etc/machine-id
    rm -f /mnt/kbid/var/lib/dbus/machine-id
    [ -d /mnt/kbid/var/lib/dbus ] && ln -sf /etc/machine-id /mnt/kbid/var/lib/dbus/machine-id
    # SSH host keys: removed here, new ones made at first boot by a one-time unit
    if ls /mnt/kbid/etc/ssh/ssh_host_* >/dev/null 2>&1; then
      rm -f /mnt/kbid/etc/ssh/ssh_host_*
      mkdir -p /mnt/kbid/etc/systemd/system/multi-user.target.wants
      cat > /mnt/kbid/etc/systemd/system/kb-ssh-hostkeys.service <<'UNIT'
[Unit]
Description=KwickBoot: new SSH host keys for this machine (after a golden-image restore)
Before=ssh.service sshd.service
ConditionPathExists=!/etc/ssh/ssh_host_ed25519_key
[Service]
Type=oneshot
ExecStart=/usr/bin/ssh-keygen -A
[Install]
WantedBy=multi-user.target
UNIT
      ln -sf /etc/systemd/system/kb-ssh-hostkeys.service /mnt/kbid/etc/systemd/system/multi-user.target.wants/kb-ssh-hostkeys.service
    fi
    if [ -n "$HN" ]; then
      echo "$HN" > /mnt/kbid/etc/hostname
      [ -f /mnt/kbid/etc/hosts ] && { sed -i '/^127\.0\.1\.1[[:space:]]/d' /mnt/kbid/etc/hosts; echo "127.0.1.1 $HN" >> /mnt/kbid/etc/hosts; }
    fi
    echo "  OK    Linux $(. /mnt/kbid/etc/os-release; echo "$PRETTY_NAME"): new machine-id + SSH host keys at first boot${HN:+, hostname $HN}"
  fi
  umount /mnt/kbid
done
sync

step 7 "verify"
partprobe "$DISK" 2>/dev/null; sleep 1
lsblk -o NAME,SIZE,FSTYPE,LABEL "$DISK" | sed 's/^/  /'
if [ -d /sys/firmware/efi ] && command -v efibootmgr >/dev/null; then
  # a firmware boot entry for the restored system's loader, network first (KwickBoot stays in control)
  for p in $(lsblk -nro NAME,PARTTYPE "$DISK" | awk 'tolower($2)=="c12a7328-f81f-11d2-ba4b-00a0c93ec93b"{print $1}'); do
    mkdir -p /mnt/kbesp && mount -o ro /dev/$p /mnt/kbesp 2>/dev/null || continue
    f=""; for l in EFI/Microsoft/Boot/bootmgfw.efi EFI/*/shimx64.efi EFI/BOOT/BOOTX64.EFI; do f=$(ls /mnt/kbesp/$l 2>/dev/null | head -n1); [ -n "$f" ] && break; done
    umount /mnt/kbesp; [ -n "$f" ] || continue
    rel=$(echo "${f#/mnt/kbesp/}" | tr '/' '\\'); num=$(cat /sys/class/block/$p/partition)
    case "$rel" in *Microsoft*) label="Windows Boot Manager" ;; *) label="$NAME" ;; esac
    efibootmgr -q -c -d "$DISK" -p "$num" -L "$label" -l "\\$rel" && echo "  OK    firmware boot entry '$label'"
    all=$(efibootmgr | sed -n 's/^BootOrder: //p' | tr ',' ' ')
    net=$(efibootmgr | awk '/^Boot[0-9A-F]{4}\*? /{print}' | grep -iE 'PXE|IPv4|Network|NIC' | grep -viE 'IPv6|HTTP' | cut -c5-8 | tr '\n' ' ')
    order=""; for b in $net $all; do case " $order " in *" $b "*) ;; *) [ -n "$b" ] && order="$order $b" ;; esac; done
    [ -n "$net" ] && [ "${BOOT_ORDER:-network-first}" != os-first ] && efibootmgr -q -o "$(echo $order | tr ' ' ',')"
    [ "${BOOT_ORDER:-network-first}" = os-first ] && efibootmgr -q -o "$(echo "$(efibootmgr | awk -v l="$label" '$0 ~ l {print substr($1,5,4); exit}') $all" | tr ' ' '\n' | awk 'NF && !s[$0]++' | paste -sd, -)" \
      && echo "  OK    boot order: $label first (network only when IT starts a reinstall)"
    break
  done
fi

step 8 "finish"
echo "RESTORE DONE: image '$NAME' on $DISK in $(( ($(date +%s) - KB_T0) / 60 )) min"
