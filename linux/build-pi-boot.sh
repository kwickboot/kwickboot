#!/bin/sh
# Assemble a Raspberry Pi network-boot tree (Pi 3/4/5): the Pi GPU firmware (Debian's raspi-firmware) + config.txt/cmdline.txt
# + the KwickBoot arm64 Core kernel/initrd + the Pi device trees (already shipped by the Debian arm64 kernel). The Pi's
# VideoCore/EEPROM bootloader TFTPs this tree, then boots the arm64 Core RAM OS - so a Pi joins KwickBoot like any machine.
# Output goes to the TFTP root level (the Pi fetches config.txt/start4.elf by bare name from the TFTP root).
# SPDX-License-Identifier: AGPL-3.0-or-later
set -e
T=${KB_HOME:-$(cd "$(dirname "$0")/.." && pwd)}
CORE=${KB_CORE:-$T/http/kbcore-arm64}
OUT=${KB_OUT:-$T/tftp}                       # staged into the TFTP root (shared with x86 clients; filenames don't clash)
CHROOT=${KB_ARM64_CHROOT:-/var/tmp/kbcore-build-arm64/chroot}
FWVER=${RASPI_FW:-1.20240424+ds-6}
[ -s "$CORE/vmlinuz" ] || { echo "no arm64 Core at $CORE (build it: KB_ARCH=arm64 build-core.sh)"; exit 1; }

echo ">>> Pi firmware (raspi-firmware $FWVER)"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
curl -fsS -o "$TMP/raspi-fw.deb" "http://deb.debian.org/debian/pool/non-free-firmware/r/raspi-firmware/raspi-firmware_${FWVER}_all.deb"
dpkg-deb -x "$TMP/raspi-fw.deb" "$TMP/fw"
FWD="$TMP/fw/usr/lib/raspi-firmware"
mkdir -p "$OUT/overlays"
for f in bootcode.bin start.elf start4.elf start_cd.elf start4cd.elf fixup.dat fixup4.dat fixup_cd.dat fixup4cd.dat; do
  [ -f "$FWD/$f" ] && cp "$FWD/$f" "$OUT/$f"
done

echo ">>> arm64 Core kernel + initrd"
cp "$CORE/vmlinuz" "$OUT/vmlinuz"
cp "$CORE/initrd.img" "$OUT/initrd.img"

echo ">>> Pi device trees + overlays (from the Debian arm64 kernel)"
KV=$(ls "$CHROOT/lib/modules" 2>/dev/null | sort -V | tail -1)
DTBSRC="$CHROOT/usr/lib/linux-image-$KV/broadcom"
if [ -d "$DTBSRC" ]; then
  cp "$DTBSRC"/bcm2*.dtb "$OUT/" 2>/dev/null || true
  cp "$CHROOT/usr/lib/linux-image-$KV/overlays/"*.dtbo "$OUT/overlays/" 2>/dev/null || true
  cp "$CHROOT/usr/lib/linux-image-$KV/overlays/overlay_map.dtb" "$OUT/overlays/" 2>/dev/null || true
else
  echo "    (arm64 kernel chroot not found - staging without DTBs; the Pi firmware carries fallbacks for some models)"
fi

echo ">>> config.txt + cmdline.txt"
cat > "$OUT/config.txt" <<'CFG'
# KwickBoot Raspberry Pi netboot (Pi 3/4/5). The firmware auto-selects bcmNNNN-rpi-*.dtb for this board.
arm_64bit=1
enable_uart=1
kernel=vmlinuz
initramfs initrd.img followkernel
disable_overscan=1
CFG
# {{SERVER_IP}} is rendered by the server at start (like the other TFTP templates); the Core fetches its packs from kb_server.
cat > "$OUT/cmdline.txt.tmpl" <<'CMD'
console=serial0,115200 console=tty1 ip=dhcp kb_server={{SERVER_IP}}:{{HTTP_PORT}} loglevel=4
CMD

echo "Pi boot tree assembled in $OUT:"
ls -1 "$OUT" | grep -iE 'bootcode|start|fixup|vmlinuz|initrd|config|cmdline|bcm27' | sed 's/^/  /'
echo "  overlays: $(ls "$OUT/overlays" 2>/dev/null | wc -l) files"
echo "  device trees: $(ls "$OUT"/bcm2*.dtb 2>/dev/null | wc -l)"
