#!/bin/bash
# Put iXsystems' official TrueNAS installer on the KwickBoot server for unattended network installs.
# SPDX-License-Identifier: AGPL-3.0-or-later
#   sudo KB_OUT=/opt/pxe-toolkit/http/truenas linux/prepare-truenas.sh <TrueNAS-SCALE-x.y.z.iso> <sha256 from truenas.com>
# Copies from the verified ISO, unmodified: vmlinuz, initrd.img, live/filesystem.squashfs, TrueNAS-SCALE.update.
# Adds kb-truenas.cpio: a small extra initrd with KwickBoot's init-bottom step (linux/truenas/) - see recipes/truenas.sh.
set -euo pipefail
ISO=${1:?usage: prepare-truenas.sh <iso> <sha256>}; SUM=${2:?sha256 of the ISO, as published by iXsystems}
OUT=${KB_OUT:-/opt/pxe-toolkit/http/truenas}; HERE=$(cd "$(dirname "$0")" && pwd)
echo ">>> verify $ISO"
echo "$SUM  $ISO" | sha256sum -c --quiet || { echo "ISO CHECKSUM MISMATCH - refused"; exit 1; }
M=$(mktemp -d); W=$(mktemp -d); trap 'umount "$M" 2>/dev/null; rmdir "$M"; rm -rf "$W"' EXIT
mount -o loop,ro "$ISO" "$M"
for f in vmlinuz initrd.img live/filesystem.squashfs TrueNAS-SCALE.update; do [ -f "$M/$f" ] || { echo "not a TrueNAS installer ISO: $f missing"; exit 1; }; done
mkdir -p "$OUT"
echo ">>> copy installer files (unmodified)"
for f in vmlinuz initrd.img live/filesystem.squashfs TrueNAS-SCALE.update; do cp "$M/$f" "$OUT/${f##*/}.new"; done
echo ">>> KwickBoot step (extra initrd)"
mkdir -p "$W/x" "$W/c/scripts/init-bottom" "$W/c/kb"
( cd "$W/x" && unmkinitramfs "$M/initrd.img" . )
ORD=$(find "$W/x" -path '*/scripts/init-bottom/ORDER' | head -n1)
[ -n "$ORD" ] || { echo "installer initrd has no scripts/init-bottom/ORDER - layout changed, refusing"; exit 1; }
# ours first (it only writes into the new root), then the installer's own steps unchanged
{ printf '/scripts/init-bottom/kb-truenas "$@"\n[ -e /conf/param.conf ] && . /conf/param.conf\n'; cat "$ORD"; } > "$W/c/scripts/init-bottom/ORDER"
install -m 755 "$HERE/truenas/kb-truenas-hook" "$W/c/scripts/init-bottom/kb-truenas"
install -m 755 "$HERE/truenas/kb-truenas-install" "$W/c/kb/kb-truenas-install"
install -m 644 "$HERE/truenas/kb-truenas.service" "$W/c/kb/kb-truenas.service"
sed -i 's/\r$//' "$W/c/scripts/init-bottom/"* "$W/c/kb/"*
( cd "$W/c" && find . | LC_ALL=C sort | cpio -o -H newc --owner=0:0 --quiet ) > "$OUT/kb-truenas.cpio.new"
for f in vmlinuz initrd.img filesystem.squashfs TrueNAS-SCALE.update kb-truenas.cpio; do mv -f "$OUT/$f.new" "$OUT/$f"; done
cat "$M/.disk/info" 2>/dev/null > "$OUT/version" || true; echo " ($(basename "$ISO"))" >> "$OUT/version"
( cd "$OUT" && sha256sum vmlinuz initrd.img filesystem.squashfs TrueNAS-SCALE.update kb-truenas.cpio > SHA256SUMS )
chmod -R u=rwX,go=rX "$OUT"
ls -la "$OUT"
