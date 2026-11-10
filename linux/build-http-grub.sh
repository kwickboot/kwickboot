#!/bin/sh
# Build the x64 UEFI HTTP-Boot GRUB loader (RFC 5970): http/http/grubnetx64.efi
#
# UEFI HTTP Boot firmware fetches this over HTTP (pxeserver hands arch-16 HTTPClient
# clients the URL http://SRV:PORT/http/grubnetx64.efi). It is a grub-mkstandalone
# image: all modules + a memdisk /boot/grub/grub.cfg that does net_bootp (DHCP) and
# then loads the per-machine menu the server generates - the SAME menu the TFTP chain
# uses (shim -> grubx64 -> grub.cfg.tmpl), which carries the per-boot pxetoken. The
# HTTP server is taken from DHCP (${net_default_server}); only the port is baked, so
# ONE loader works for any KwickBoot server on the LAN.
#
#   build-http-grub.sh [fallback_server_ip] [http_port]
# Needs: grub-efi-amd64-bin + grub-common (grub-mkstandalone, /usr/lib/grub/x86_64-efi).
#
# NOTE: the committed http/http/grubnetx64.efi is the proven, deployed binary. This
# script REGENERATES an equivalent (verify on real UEFI-HTTP-boot firmware before it
# replaces the committed one). The aa64/ia32 HTTP loaders come from Debian's signed
# netboot instead (kwickboot-add-arch skill) - do not rebuild those here.
set -e
SRV=${1:-}                       # optional: only a fallback if DHCP gives no next-server
PORT=${2:-8080}
T=${KB_HOME:-/opt/pxe-toolkit}
OUT="${KB_OUT:-$T/http/http}/grubnetx64.efi"

command -v grub-mkstandalone >/dev/null 2>&1 || { echo "need grub-mkstandalone: apt-get install -y grub-efi-amd64-bin grub-common"; exit 1; }
[ -d /usr/lib/grub/x86_64-efi ] || { echo "need /usr/lib/grub/x86_64-efi (grub-efi-amd64-bin)"; exit 1; }

W=$(mktemp -d); mkdir -p "$W/boot/grub"
{
  echo '# KwickBoot x64 UEFI HTTP-Boot GRUB (embedded memdisk cfg).'
  echo 'insmod efinet'
  echo 'insmod http'
  echo 'insmod linux'
  echo 'insmod normal'
  echo 'net_bootp'
  [ -n "$SRV" ] && echo "isset \${net_default_server} || set net_default_server=$SRV"
  echo "set kbsrv=\${net_default_server}:$PORT"
  echo '# the server-generated per-machine menu (carries the per-boot pxetoken), twice'
  echo '# (the first request can drop right after the NIC comes up):'
  echo "configfile (http,\${kbsrv})/grub/\${net_default_mac}.cfg"
  echo 'sleep 2'
  echo "configfile (http,\${kbsrv})/grub/\${net_default_mac}.cfg"
  echo '# fallback (unknown machine / discovery closed): boot the Core RAM OS directly'
  echo "set root=(http,\${kbsrv})"
  echo "linux /kbcore/vmlinuz kb_server=\${net_default_server}:$PORT quiet loglevel=3 console=tty0"
  echo '  initrd /kbcore/initrd.img'
  echo '  boot'
} > "$W/boot/grub/grub.cfg"

mkdir -p "$(dirname "$OUT")"
grub-mkstandalone -O x86_64-efi -o "$OUT" "boot/grub/grub.cfg=$W/boot/grub/grub.cfg"
rm -rf "$W"
echo "built $OUT ($(wc -c < "$OUT") bytes); HTTP server from DHCP, port $PORT${SRV:+, fallback $SRV}"
