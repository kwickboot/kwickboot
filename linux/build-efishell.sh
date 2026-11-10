#!/bin/sh
# build-efishell.sh - the EFI Shell tool for the OS library (vendor EFI flash utilities: AMI AFUEFI, Intel, Broadcom/LSI
# sas3flash.efi, ...; firmware variables, boot entries). Run on the build box.
# SPDX-License-Identifier: AGPL-3.0-or-later  (EFI Shell: TianoCore EDK2, BSD-2-Clause-Patent - Debian package efi-shell-x64)
#   sh build-efishell.sh [KB_HOME]  ->  $KB_HOME/http/images/tools/efishell/{boot.efi,tool.info}
# UEFI only, and NOT Microsoft-signed: it runs with Secure Boot turned off (the boot menu says so under Secure Boot).
set -eu
T=${1:-${KB_HOME:-/opt/pxe-toolkit}}
W=$(mktemp -d); trap 'rm -rf "$W"' EXIT
( cd $W && apt-get download -q efi-shell-x64 >/dev/null 2>&1 ) || { echo "apt-get download efi-shell-x64 failed (Debian 13 mirror needed)"; exit 1; }
deb=$(ls $W/efi-shell-x64_*.deb | head -n1); ver=$(dpkg-deb -f "$deb" Version)
dpkg-deb -x "$deb" $W/x                                  # apt verified the package signature chain (Release -> Packages -> .deb)
efi=$(find $W/x -iname 'shellx64.efi' | head -n1); [ -n "$efi" ] || { echo "shellx64.efi not in the package"; exit 1; }
D=$T/http/images/tools/efishell; mkdir -p $D
install -m 0644 "$efi" $D/boot.efi
printf 'NAME=efishell\nTITLE=EFI Shell\nDESC=The UEFI Shell (TianoCore EDK2 %s) - run vendor EFI flash and setup utilities from a USB stick (fs0:, fs1: ...). UEFI PCs with Secure Boot turned off.\nROLE=manager\nKIND=efi\nUEFI_ONLY=1\nBUILT=%s\n' "$ver" "$(date +%F)" > $D/tool.info
( cd $D && sha256sum boot.efi > SHA256SUMS )
echo "tool efishell: $(( $(stat -c %s $D/boot.efi) / 1024 )) KB (EDK2 $ver) -> $D"
