#!/bin/sh
# KwickBoot image capture - runs inside the RAM OS. A "golden" machine's disk -> the KwickBoot server:
# the partition table and every partition, used blocks only (partclone), zstd-compressed, streamed straight to the
# server (kbpty put:, no temporary copy anywhere). The server computes each file's SHA-256 on arrival; a manifest
# with all of them is written last, so restores verify every byte. Read-only on this machine's disk.
# SPDX-License-Identifier: AGPL-3.0-or-later
#   NAME=<image name> DISK=/dev/x CONFIRM=<disk serial> JOB=<job id> sh capture.sh
SRV="http://{{SERVER_IP}}:{{HTTP_PORT}}"
: "${NAME:?set NAME}" "${DISK:?set DISK}" "${CONFIRM:?set CONFIRM=<disk serial>}" "${JOB:?set JOB}"
KB_T0=$(date +%s)
step() { echo; echo ">>> [$1] $2   (+$(( $(date +%s) - KB_T0 ))s)"; }
die()  { echo "STOP: $*"; exit 1; }
hp=${SRV#http://}; KBIF=$(ip route get {{SERVER_IP}} 2>/dev/null | sed -n 's/.* dev \([^ ]*\).*/\1/p' | head -n1); [ -n "$KBIF" ] || KBIF=$(ip route show default 2>/dev/null | awk '{print $5; exit}'); MAC=$(cat /sys/class/net/$KBIF/address); TOKEN=$(cat /root/tk/token)
# send stdin to the server as <file>; prints "<sha256> <bytes>" or fails
put() { r=$(kbpty "${hp%:*}" "${hp#*:}" "$MAC" "$TOKEN" "$JOB" "put:$1") || { echo "upload of $1 failed: $r" >&2; return 1; }
        echo "$r" | awk '$1=="OK"{print $2, $3}'; }

step 1 "confirm source disk"
echo "$NAME" | grep -qE '^[a-z0-9][a-z0-9._-]{0,60}$' || die "image name: lowercase letters, digits, . _ -"
[ -b "$DISK" ] || die "$DISK is not a block device"
n=$(basename "$DISK"); s=$(lsblk -dno SERIAL "$DISK" 2>/dev/null | tr -d ' ')
for f in /sys/block/$n/serial /sys/block/$n/device/serial; do [ -z "$s" ] && [ -r "$f" ] && s=$(tr -d ' \n' < "$f"); done
[ -z "$s" ] && [ -r /sys/block/$n/device/vpd_pg80 ] && s=$(tail -c +5 /sys/block/$n/device/vpd_pg80 | tr -d ' \n\000')
[ -n "$s" ] || s="size-$(lsblk -dno SIZE "$DISK" | tr -d ' ')"
[ "$CONFIRM" = "$s" ] || die "CONFIRM '$CONFIRM' does not match $DISK serial '$s'"
lsblk -nro MOUNTPOINTS "$DISK" | grep -q . && die "$DISK has mounted filesystems"
for t in partclone.ext4 partclone.dd zstd sgdisk kbpty; do command -v $t >/dev/null || die "tool $t missing (imaging pack not loaded)"; done
DBYTES=$(( $(cat /sys/class/block/$n/size) * 512 ))
echo "source $DISK serial $s, $((DBYTES / 1000000000)) GB - read only, nothing on it is changed"

step 2 "partition table"
case "$DISK" in *[0-9]) P="${DISK}p" ;; *) P="$DISK" ;; esac
PT=$(blkid -p -s PTTYPE -o value "$DISK" 2>/dev/null)
case "$PT" in
  gpt) sgdisk --backup=/tmp/kbcap.pt "$DISK" >/dev/null || die "cannot read the GPT"; TF=table.gpt ;;
  dos) sfdisk -d "$DISK" > /tmp/kbcap.pt || die "cannot read the MBR table"; TF=table.sfdisk ;;
  *)   die "no partition table on $DISK (capture works on partitioned disks)" ;;
esac
r=$(put $TF < /tmp/kbcap.pt) || die "upload failed"
TSHA=${r% *}; echo "  OK    $PT table ($TF) sha256 ${TSHA}"

# a Windows image meant for many PCs must be generalized (sysprep): warn now, before anyone restores it
for dev in $(lsblk -nrpo NAME,FSTYPE "$DISK" | awk '$2=="ntfs"{print $1}'); do
  command -v ntfs-3g >/dev/null || break
  mkdir -p /mnt/kbcap && ntfs-3g -o ro "$dev" /mnt/kbcap 2>/dev/null || continue
  st=/mnt/kbcap/Windows/Setup/State/State.ini
  if [ -f "$st" ]; then
    state=$(tr -d '\000\r' < "$st" | sed -n 's/^ImageState=//p')
    case "$state" in
      IMAGE_STATE_GENERALIZE_RESEAL_TO_OOBE|IMAGE_STATE_SPECIALIZE_RESEAL_TO_OOBE) echo "  OK    Windows is generalized (sysprep) - safe to restore to many PCs" ;;
      *) echo "  WARN  Windows on $dev is NOT generalized ($state): restored copies would share this PC's identity"
         echo "        (security ID, name, domain membership). Fine as a backup of THIS PC; for many PCs run"
         echo "        'sysprep /generalize /oobe /shutdown' first, then capture again." ;;
    esac
  fi
  umount /mnt/kbcap
done
for dev in $(lsblk -nrpo NAME "$DISK" | tail -n +2); do
  [ "$(blkid -p -s TYPE -o value "$dev" 2>/dev/null)" = BitLocker ] && echo "  NOTE  $dev is BitLocker-encrypted: copied block for block, still encrypted - restoring it needs the recovery key"
done

step 3 "partitions (used blocks only)"
parts=""; total=0
for dev in $(lsblk -nrpo NAME "$DISK" | tail -n +2); do
  num=$(cat /sys/class/block/$(basename $dev)/partition)
  fs=$(blkid -p -s TYPE -o value "$dev" 2>/dev/null)
  uuid=$(blkid -p -s UUID -o value "$dev" 2>/dev/null)
  psz=$(( $(cat /sys/class/block/$(basename $dev)/size) * 512 ))
  case "$fs" in
    ext2|ext3|ext4) tool=partclone.ext4 ;;  vfat) tool=partclone.vfat ;;  ntfs) tool=partclone.ntfs ;;
    xfs) tool=partclone.xfs ;;  btrfs) tool=partclone.btrfs ;;
    swap) echo "  OK    partition $num: swap - recreated on restore (UUID $uuid), not copied"
          parts="$parts{\"n\":$num,\"fs\":\"swap\",\"uuid\":\"$uuid\",\"size\":$psz},"; continue ;;
    *)    tool=raw ;;            # unknown/encrypted (BitLocker, LUKS, ZFS, empty): every block
  esac
  command -v "$tool" >/dev/null 2>&1 || [ "$tool" = raw ] || tool=raw
  f="part$num.${tool#partclone.}.zst"
  t1=$(date +%s); rm -f /tmp/kbcap.rc
  if [ "$tool" = raw ]; then
    r=$( { dd if="$dev" bs=4M 2>/dev/null; echo $? > /tmp/kbcap.rc; } | zstd -q -T0 -3 | put "$f") || die "upload of partition $num failed"
  else
    r=$( { $tool -c -q -s "$dev" -o - 2>/tmp/kbcap.err; echo $? > /tmp/kbcap.rc; } | zstd -q -T0 -3 | put "$f") || die "upload of partition $num failed"
  fi
  [ "$(cat /tmp/kbcap.rc)" = 0 ] || { tail -n 3 /tmp/kbcap.err 2>/dev/null; die "$tool failed on partition $num (filesystem not clean? for Windows: shut down fully, not hibernate/Fast Startup)"; }
  sha=${r% *}; bytes=${r#* }; total=$(( total + bytes ))
  echo "  OK    partition $num ${fs:-raw} -> $f  $((bytes / 1000000)) MB in $(( $(date +%s) - t1 ))s  sha256 ${sha%${sha#????????????}}…"
  parts="$parts{\"n\":$num,\"fs\":\"${fs:-raw}\",\"tool\":\"$tool\",\"file\":\"$f\",\"sha256\":\"$sha\",\"bytes\":$bytes,\"size\":$psz,\"uuid\":\"$uuid\"},"
done

step 7 "manifest"
model=$(cat /sys/class/dmi/id/product_name 2>/dev/null | tr -cd 'A-Za-z0-9 ._-')
printf '{"name":"%s","created":"%s","disk_bytes":%s,"table":"%s","table_file":"%s","table_sha256":"%s","parts":[%s],"source":{"mac":"%s","model":"%s","disk":"%s","boot":"%s"}}\n' \
  "$NAME" "$(date -u +%FT%TZ)" "$DBYTES" "$PT" "$TF" "$TSHA" "${parts%,}" "$MAC" "$model" "$DISK" "$([ -d /sys/firmware/efi ] && echo uefi || echo bios)" > /tmp/kbcap.json
put manifest.json < /tmp/kbcap.json >/dev/null || die "manifest upload failed"
echo "  OK    manifest with $(echo "$parts" | tr -cd '{' | wc -c) partitions"

step 8 "finish"
echo "CAPTURE DONE: image '$NAME' - $((total / 1000000)) MB on the server (compressed) in $(( ($(date +%s) - KB_T0) / 60 )) min"
