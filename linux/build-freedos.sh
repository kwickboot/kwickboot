#!/bin/sh
# build-freedos.sh - the FreeDOS tool for the OS library (BIOS firmware flashing, old DOS utilities). Run on the build box.
# SPDX-License-Identifier: AGPL-3.0-or-later  (FreeDOS itself: GPL and other free licences - see FREEDOS\DOC on the image)
#   sh build-freedos.sh <FD14-FloppyEdition.zip> [KB_HOME]  ->  $KB_HOME/http/images/tools/freedos/{vmlinuz,initrd.img,tool.info}
# The official FreeDOS 1.4 Floppy Edition boot floppy (SHA-256 checked against FreeDOS's verify.txt value below), made to
# start at a plain DOS prompt instead of the installer, booted with syslinux's memdisk. Legacy BIOS only: DOS cannot run on
# UEFI-only firmware. Needs: unzip, mtools, syslinux-common (memdisk).
set -eu
ZIP=${1:?FD14-FloppyEdition.zip}; T=${2:-${KB_HOME:-/opt/pxe-toolkit}}
WANT=45b1fa7c52dd996c3bfa5e352ffcd410781b952a6ad629f15a4c9ec4bbaefc5a   # FreeDOS 1.4 verify.txt, FD14-FloppyEdition.zip
[ "$(sha256sum "$ZIP" | cut -d' ' -f1)" = "$WANT" ] || { echo "FD14-FloppyEdition.zip: SHA-256 does not match FreeDOS's published value"; exit 1; }
MEMDISK=/usr/lib/syslinux/memdisk; [ -f $MEMDISK ] || { echo "memdisk missing (apt install syslinux-common)"; exit 1; }
for t in unzip mcopy mdel mtype; do command -v $t >/dev/null || { echo "missing $t (apt install unzip mtools)"; exit 1; }; done
W=$(mktemp -d); trap 'rm -rf "$W"' EXIT
unzip -q -o "$ZIP" 144m/x86BOOT.img -d $W
IMG=$W/144m/x86BOOT.img
export MTOOLS_SKIP_CHECK=1
# start at the prompt: no language menu, no installer
printf '!COUNTRY=001,858:\\FREEDOS\\BIN\\COUNTRY.SYS\r\n!LASTDRIVE=Z\r\n!BUFFERS=20\r\n!FILES=40\r\nSHELL=\\FREEDOS\\BIN\\COMMAND.COM \\FREEDOS\\BIN /E:2048 /P=\\FDAUTO.BAT\r\n' > $W/FDCONFIG.SYS
mtype -i $IMG ::/FDAUTO.BAT | tr -d '\r' | sed '/SETUP.BAT/d; /WELCOME.BAT/d; /FDBANNER.COM/d; /^:Done$/,$d' > $W/auto
cat >> $W/auto <<'EOF'
echo.
echo  KwickBoot - FreeDOS 1.4 (from the KwickBoot OS library)
echo  ------------------------------------------------------
echo  For BIOS / firmware updates and old DOS tools. Put the update files
echo  on a FAT32 USB stick (DOS cannot read exFAT or NTFS), then e.g.:
echo     C:     DIR     FLASH.EXE
echo  Tools: FDISK FORMAT SYS XCOPY MEM EDIT  -  restart: reboot  -  off: halt
echo.
:Done
EOF
sed 's/$/\r/' $W/auto > $W/FDAUTO.BAT
mdel -i $IMG ::/SETUP.BAT ::/FDCONFIG.SYS ::/FDAUTO.BAT
mcopy -i $IMG $W/FDCONFIG.SYS $W/FDAUTO.BAT ::/
D=$T/http/images/tools/freedos; mkdir -p $D
install -m 0644 $MEMDISK $D/vmlinuz
install -m 0644 $IMG $D/initrd.img
printf 'NAME=freedos\nTITLE=FreeDOS\nDESC=FreeDOS 1.4 to a DOS prompt - BIOS / firmware flashing tools and old DOS utilities. Legacy BIOS PCs only (DOS cannot run on UEFI-only firmware).\nROLE=manager\nCMDLINE=floppy\nBIOS_ONLY=1\nKERNEL=memdisk\nBUILT=%s\n' "$(date +%F)" > $D/tool.info
( cd $D && sha256sum vmlinuz initrd.img > SHA256SUMS )
echo "tool freedos: $(( ($(stat -c %s $D/vmlinuz) + $(stat -c %s $D/initrd.img)) / 1024 )) KB (memdisk + 1.44 MB floppy) -> $D"
