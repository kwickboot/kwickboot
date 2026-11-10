#!/bin/sh
# Build iPXE (UEFI ipxe.efi + legacy-BIOS undionly.kpxe) with an embedded script
# that always asks the toolkit server what to boot - no console interaction, no
# DHCP loops - plus tiny no-PXE boot media (for clients that can't/won't PXE-boot).
#   build-ipxe.sh [server_ip] [http_port]
#
# IMAGE_BZIMAGE is enabled ONLY for the BIOS targets (bin/*). In the x86_64-EFI
# target it pulls 16/32-bit real-mode code into the 64-bit link and fails on
# modern binutils (ld "relocation truncated to fit: R_X86_64_32"). EFI clients
# boot Linux via GRUB / the EFI stub, so the EFI iPXE does not need it - it only
# has to chain to the server's boot.ipxe (http + chain + script are enough).
set -e
SRV=${1:-$(hostname -I | awk '{print $1}')}
PORT=${2:-8080}
SRC=${IPXE_SRC:-/opt/src/ipxe}
OUT=${KB_OUT:-/opt/pxe-toolkit/tftp/ipxe}     # build on the build box: KB_OUT=... IPXE_SRC=... then copy
[ -d "$SRC" ] || git clone -q --depth 1 https://github.com/ipxe/ipxe.git "$SRC"
mkdir -p "$OUT"          # also our build-log dir (avoid hardcoded /tmp/*.log: fs.protected_regular blocks root from a stale non-root-owned file in sticky /tmp)
cd "$SRC/src"

cat > embed.ipxe <<EOF
#!ipxe
# PXE toolkit embedded script: get an address, then ask the toolkit server what to do.
:retry
dhcp || goto failed
isset \${proxydhcp/next-server} && set srv \${proxydhcp/next-server} || set srv \${next-server}
isset \${srv} || set srv $SRV
chain --autofree http://\${srv}:$PORT/boot.ipxe?mac=\${mac} || chain --autofree http://$SRV:$PORT/boot.ipxe?mac=\${mac} || goto failed
:failed
echo PXE toolkit: server $SRV:$PORT not reachable - booting next device in 10s
sleep 10
exit
EOF

# $1 = per-target defines. IMAGE_EFI is EFI-ONLY (references efi_systab, absent in
# the BIOS build); IMAGE_BZIMAGE is BIOS-ONLY (real-mode code breaks the EFI link).
# The base set (HTTP/sanboot/script/diagnostics) is safe for both.
write_general() {
  cat > config/local/general.h <<EOF
#define DOWNLOAD_PROTO_HTTPS
#define SANBOOT_PROTO_HTTP
#define IMAGE_SCRIPT
#define NSLOOKUP_CMD
#define PING_CMD
#define REBOOT_CMD
#define POWEROFF_CMD
#define CONSOLE_CMD
$1
EOF
}

J=$(nproc)

echo "building iPXE UEFI (server $SRV:$PORT, EFI-safe config) -j$J ..."
write_general "#define IMAGE_EFI"
make -s -j"$J" clean >/dev/null 2>&1 || true
make -s -j"$J" bin-x86_64-efi/ipxe.efi EMBED=embed.ipxe >"$OUT/build-efi.log" 2>&1 \
  || { echo "EFI build FAILED:"; tail -n 30 "$OUT/build-efi.log"; exit 1; }
mkdir -p "$OUT"
install -m 644 bin-x86_64-efi/ipxe.efi "$OUT/"

# --- tiny no-PXE boot media -------------------------------------------------
# ~1 MB stick for clients whose firmware can't / won't PXE-boot. Carries iPXE's
# OWN NIC drivers (unlike undionly.kpxe, which needs firmware UNDI), then runs
# the embedded script -> chain to this server's /boot.ipxe. Output in $OUT/noPXE/.
NP="$OUT/noPXE"; mkdir -p "$NP/stick/EFI/BOOT"
cp bin-x86_64-efi/ipxe.efi "$NP/stick/EFI/BOOT/BOOTX64.EFI"   # copy onto any FAT32 stick

echo "building iPXE BIOS (undionly + lkrn + iso, with IMAGE_BZIMAGE) ..."
write_general "#define IMAGE_BZIMAGE"
make -s -j"$J" clean >/dev/null 2>&1 || true
for t in bin/undionly.kpxe bin/ipxe.lkrn bin/ipxe.iso; do
  make -s -j"$J" "$t" EMBED=embed.ipxe >>"$OUT/build-bios.log" 2>&1 \
    || { echo "WARN $t build failed:"; tail -n 20 "$OUT/build-bios.log"; }
done
[ -f bin/undionly.kpxe ] && install -m 644 bin/undionly.kpxe "$OUT/" || echo "WARN no undionly.kpxe"
[ -f bin/ipxe.iso ]      && cp bin/ipxe.iso "$NP/kwickboot-boot-bios.iso" || echo "WARN no ipxe.iso"

# One hybrid disk image: BIOS (syslinux) + UEFI (EFI/BOOT/BOOTX64.EFI) in one.
# Write with Rufus (DD mode) or `dd if=... of=/dev/sdX bs=1M`. genfsimg matches the
# EFI input by a lowercase .efi suffix, so feed it $OUT/ipxe.efi (not BOOTX64.EFI).
rm -rf "$NP"/kwickboot-boot.img.* 2>/dev/null || true
if [ -x util/genfsimg ] && [ -f bin/ipxe.lkrn ]; then
  util/genfsimg -o "$NP/kwickboot-boot.img" bin/ipxe.lkrn "$OUT/ipxe.efi" 2>"$OUT/genfsimg.log" \
    && echo "hybrid BIOS+UEFI boot image: $NP/kwickboot-boot.img" \
    || { echo "WARN genfsimg failed:"; cat "$OUT/genfsimg.log"; }
fi

cp embed.ipxe "$OUT/embed.ipxe.txt"
echo "=== iPXE $(git -C "$SRC" log -1 --format=%h) installed to $OUT ==="
ls -lR "$OUT"
echo "--- file types ---"
for f in "$NP/kwickboot-boot.img" "$NP/kwickboot-boot-bios.iso" "$NP/stick/EFI/BOOT/BOOTX64.EFI" "$OUT/ipxe.efi" "$OUT/undionly.kpxe"; do
  [ -f "$f" ] && { printf '%s: ' "$f"; file -b "$f"; }
done
echo "--- ESP contents of hybrid image ---"
mdir -i "$NP/kwickboot-boot.img" ::/EFI/BOOT 2>/dev/null || true
( cd "$OUT" && find . -type f -exec sha256sum {} + )
echo "=== build-ipxe DONE ==="
