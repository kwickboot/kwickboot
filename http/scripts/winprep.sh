#!/bin/sh
# winprep.sh - make a STREAMABLE (pipable) image of one Windows edition from an ISO in the library, on THIS machine's
# CPU (KwickBoot RAM OS) - so a weak boot server never has to do it. Done once per ISO and edition.
# SPDX-License-Identifier: AGPL-3.0-or-later
#   ISO=<name.iso> EDITION=Professional SCRATCH=/dev/sdX CONFIRM=<serial of SCRATCH> sh winprep.sh
# SCRATCH is ERASED (it holds the 5-8 GB source and the result). Output: /mnt/kbprep/<EDITION>.wim + .sha256
# Only the chosen edition is kept: a Pro install then downloads Pro, not all ten editions of the ISO. Compressed data is
# copied as it is (no recompression): the result is as small as Microsoft's own image and costs little CPU to make.
SRV=${SRV:-http://{{SERVER_IP}}:{{HTTP_PORT}}}
: "${ISO:?ISO=}" "${EDITION:=Professional}" "${SCRATCH:?SCRATCH=/dev/...}" "${CONFIRM:?CONFIRM=<serial>}"
die() { echo "STOP: $*"; exit 1; }
serial_of() { n=$(basename "$1"); s=$(lsblk -dno SERIAL "$1" 2>/dev/null | tr -d ' ')
  for f in /sys/block/$n/serial /sys/block/$n/device/serial; do [ -z "$s" ] && [ -r "$f" ] && s=$(tr -d ' \n' < "$f"); done
  [ -z "$s" ] && [ -r /sys/block/$n/device/vpd_pg80 ] && s=$(tail -c +5 /sys/block/$n/device/vpd_pg80 | tr -d ' \n\000')
  [ -n "$s" ] || s="size-$(lsblk -dno SIZE "$1" | tr -d ' ')"; echo "$s"; }
[ "$(serial_of "$SCRATCH")" = "$CONFIRM" ] || die "CONFIRM '$CONFIRM' does not match $SCRATCH serial '$(serial_of "$SCRATCH")'"
command -v wimlib-imagex >/dev/null || die "wimlib-imagex missing (imaging pack)"
t0=$(date +%s)
wipefs -a -q "$SCRATCH" 2>/dev/null; mkfs.ext4 -q -F "$SCRATCH" || die "cannot format $SCRATCH"
mkdir -p /mnt/kbprep && mount "$SCRATCH" /mnt/kbprep || die "cannot mount $SCRATCH"
for w in sources/install.wim sources/install.esd; do
  if wget -q --spider "$SRV/isofile/$ISO/$w"; then src=/mnt/kbprep/install.${w##*.}; url="$SRV/isofile/$ISO/$w"; break; fi
done
[ -n "$url" ] || die "$ISO has no install.wim/esd"
wget -q -O "$src" "$url" || die "download failed"
echo "source: $(du -m "$src" | cut -f1) MB in $(( $(date +%s) - t0 )) s"
idx=$(wimlib-imagex info "$src" | awk -v want="$EDITION" '/^Index:/ {i=$2} /^Edition ID:/ {if ($3 == want && !f) f=i} END {print f}')
[ -n "$idx" ] || die "edition $EDITION not in $ISO"
t1=$(date +%s)
# .esd (solid LZMS) cannot be streamed as is: recompress to LZX there; .wim chunks are copied unchanged
rc=""; case "$src" in *.esd) rc="--compress=LZX --recompress" ;; esac
wimlib-imagex export "$src" "$idx" "/mnt/kbprep/$EDITION.wim" --pipable $rc || die "export failed"
rm -f "$src"
( cd /mnt/kbprep && sha256sum "$EDITION.wim" > "$EDITION.wim.sha256" )
echo "prepared $EDITION.wim: $(du -m "/mnt/kbprep/$EDITION.wim" | cut -f1) MB, export $(( $(date +%s) - t1 )) s"
want=$(cut -d' ' -f1 "/mnt/kbprep/$EDITION.wim.sha256")
if [ -n "$JOB" ]; then
  # to the server over plain HTTP (kbpty put: - no SSH cipher on the boot server's CPU); it hashes on arrival
  hp=${SRV#http://}; MAC=$(cat /sys/class/net/$(ip route show default | awk '{print $5; exit}')/address); TOKEN=$(cat /root/tk/token)
  t2=$(date +%s)
  r=$(kbpty "${hp%:*}" "${hp#*:}" "$MAC" "$TOKEN" "$JOB" "put:$EDITION.wim" < "/mnt/kbprep/$EDITION.wim") || die "upload failed: $r"
  got=$(echo "$r" | awk '$1=="OK"{print $2}')
  [ "$got" = "$want" ] || die "server checksum $got differs from $want - not used"
  echo "uploaded in $(( $(date +%s) - t2 )) s, server checksum matches: $want"
fi
umount /mnt/kbprep
echo "WINPREP OK: $ISO $EDITION in $(( $(date +%s) - t0 )) s - installs of this edition now stream straight onto the disk"
