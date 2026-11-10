#!/bin/sh
# make-iso.sh - build the KwickBoot installer ISO (hybrid: USB with dd/Rufus DD mode/Etcher, DVD, VMs; UEFI with Secure
# Boot via Debian's signed shim + CD GRUB, and legacy BIOS boot of the installer). Run on the build box as root.
# SPDX-License-Identifier: AGPL-3.0-or-later
#   sudo sh make-iso.sh <release-tree> <kbserver.tar.zst> <version> [outdir]
#   <release-tree>    unpacked KwickBoot release (or a copy of a working /opt/pxe-toolkit): code, http/, kbcore
#   <kbserver.tar.zst> image from: KB_HOME=... linux/build-images.sh kbserver (+ .sha256 and .info next to it)
#   KB_ISO_PROFILE=compact (default): amd64 server, without optional architecture/RICH packs.
#   KB_ISO_PROFILE=full: air-gapped mixed-architecture server with the RICH local desktop pack.
# Needs (Debian packages): xorriso grub-pc-bin grub-efi-amd64-signed shim-signed mtools dosfstools squashfs-tools cpio
#   python3-pil fonts-dejavu-core (boot menu theme)
set -eu
# Non-login build shells may omit /usr/sbin even though dosfstools is installed there.
PATH=/usr/sbin:/usr/bin:/sbin:/bin:$PATH; export PATH
TREE=${1:?release tree}; IMG=${2:?kbserver image}; VER=${3:?version}; OUT=${4:-$PWD}
ISO_PROFILE=${KB_ISO_PROFILE:-compact}
case "$VER" in *[!0-9A-Za-z.-]*) echo "bad version"; exit 1 ;; esac
case "$ISO_PROFILE" in compact|full) ;; *) echo "bad KB_ISO_PROFILE: $ISO_PROFILE (use compact or full)"; exit 1 ;; esac
for t in xorriso grub-mkimage mkfs.vfat mcopy unsquashfs cpio gzip; do command -v $t >/dev/null || { echo "missing $t"; exit 1; }; done
SIGNED_GRUB=/usr/lib/grub/x86_64-efi-signed/gcdx64.efi.signed      # Debian's signed GRUB for CD/USB media
SHIM=/usr/lib/shim/shimx64.efi.signed; MOK=/usr/lib/shim/mmx64.efi.signed
for f in "$SIGNED_GRUB" "$SHIM" "$MOK" "$IMG" "$IMG.sha256" "$TREE/http/kbcore/vmlinuz" "$TREE/http/kbcore/initrd.img"; do
  [ -f "$f" ] || { echo "missing $f"; exit 1; }; done
INFO=${IMG%.tar.zst}.info
if [ -f "$INFO" ]; then
  ip=$(sed -n 's/.* profile=\([^ ]*\).*/\1/p' "$INFO")
  want=headless; [ "$ISO_PROFILE" = full ] && want=full
  [ -z "$ip" ] || [ "$ip" = "$want" ] || { echo "server image profile is $ip, but ISO profile $ISO_PROFILE needs $want"; exit 1; }
fi
HERE=$(cd "$(dirname "$0")" && pwd)
W=$(mktemp -d); trap 'rm -rf "$W"' EXIT
R=$W/iso; mkdir -p $R/.disk $R/kbcore $R/boot/grub $R/EFI/boot $R/http/scripts $R/http/recipes $R/http/images $R/http/release
echo "$VER" > $R/.disk/kwickboot; echo "KwickBoot $VER amd64 $ISO_PROFILE installer" > $R/.disk/info

echo ">>> install engine + recipes (served from the ISO on 127.0.0.1:8080)"
for f in install.sh preflight.sh debian13-configure.sh wipe.sh inventory.sh stress.sh; do
  sed 's#{{SERVER_IP}}:{{HTTP_PORT}}#127.0.0.1:8080#g; s#{{SERVER_IP}}#127.0.0.1#g; s#{{HTTP_PORT}}#8080#g' "$TREE/http/scripts/$f" > $R/http/scripts/$f
done
sed 's#{{SERVER_IP}}:{{HTTP_PORT}}#127.0.0.1:8080#g' "$TREE/http/recipes/kbserver.sh" > $R/http/recipes/kbserver.sh
cp "$IMG" $R/http/images/kbserver.tar.zst; cp "$IMG.sha256" $R/http/images/kbserver.tar.zst.sha256
[ -f "$INFO" ] && cp "$INFO" $R/http/images/kbserver.info || echo "kbserver image" > $R/http/images/kbserver.info
: > $R/http/authorized_keys                      # the install engine fetches it (no SSH keys on a fresh server)
find /usr/share/zoneinfo -type f -path '*/*/*' | sed 's#/usr/share/zoneinfo/##' | grep -vE '^(posix|right)/' | sort > $R/http/zoneinfo.list
echo "UTC" >> $R/http/zoneinfo.list

echo ">>> the release itself (the FILTERED server bundle from make-release.sh - NO OS image library, NO test artifacts)"
# Was: a tar of the whole $TREE (dragged in http/truenas, http/seat, http/san test golden, all 5 Cores -> a 4 GB tarball).
# Now: the allow-listed make-release.sh bundle. The normal compact ISO installs the complete amd64 server without
# optional mixed-architecture/RICH packs; KB_ISO_PROFILE=full retains the self-contained air-gapped edition.
# The big OS image library / TrueNAS / seat / SAN test golden are excluded from both editions.
ST=$W/rel; mkdir -p $ST
KB_RELEASE_PROFILE="$ISO_PROFILE" KB_OMIT_RUNTIME=1 sh "$HERE/make-release.sh" "$TREE" "$VER" "$ST"
mv "$ST/kwickboot-$VER.tar.gz" "$R/http/release/kwickboot.tar.gz"

echo ">>> RAM OS: Core + a second initrd with the packs (drivers, install tools), the ISO init and the installer"
cp "$TREE/http/kbcore/vmlinuz" "$TREE/http/kbcore/initrd.img" $R/kbcore/
X=$W/extra; mkdir -p $X/bin
for p in hw drivers install imaging diag; do      # drivers: extra hardware (also the Hyper-V keyboard)
  f="$TREE/http/kbcore/packs/$p.sqfs"; [ -f "$f" ] || { echo "missing pack $p"; exit 1; }
  unsquashfs -q -f -d $X "$f" >/dev/null
done
install -m 0755 "$HERE/iso/init" $X/init
install -m 0755 "$HERE/iso/kb-installer" $X/bin/kb-installer
install -m 0755 "$HERE/iso/kb-live" $X/bin/kb-live
install -m 0755 "$HERE/iso/kb-tools" $X/bin/kb-tools
install -m 0755 "$HERE/iso/kb-rescue" $X/bin/kb-rescue
( cd $X && find . | cpio -o -H newc --quiet ) | gzip -9 > $R/kbcore/iso.cpio.gz

echo ">>> server runtime (one Core copy, restored beside the release during install/live boot)"
RT=$R/http/runtime/http; mkdir -p $RT/kbcore
cp -a "$TREE/http/kbcore"/. $RT/kbcore/
[ "$ISO_PROFILE" = compact ] && rm -f $RT/kbcore/packs/desktop.sqfs
# Reuse the boot kernel/initrd as hard links so xorriso stores each inode once.
rm -f $RT/kbcore/vmlinuz $RT/kbcore/initrd.img
ln $R/kbcore/vmlinuz $RT/kbcore/vmlinuz
ln $R/kbcore/initrd.img $RT/kbcore/initrd.img
if [ "$ISO_PROFILE" = full ]; then
  for a in arm64 i386; do
    [ -d "$TREE/http/kbcore-$a" ] || { echo "missing full-profile Core: kbcore-$a"; exit 1; }
    cp -a "$TREE/http/kbcore-$a" $RT/
  done
fi

echo ">>> OS library tool: Rescue & Wipe (one image; the stick menu has an entry for each)"
[ -f "$TREE/http/images/tools/rescue/initrd.img" ] || { echo "missing OS library tool rescue (run linux/build-core.sh)"; exit 1; }
for t in "$TREE"/http/images/tools/*/; do   # every OS library tool (rescue; freedos, efishell when built) -> /tools/<name>
  n=$(basename "$t"); [ -f "$t/tool.info" ] || continue
  mkdir -p $R/tools/$n; cp "$t"/* $R/tools/$n/; rm -f $R/tools/$n/SHA256SUMS
  echo "    tool $n: $(du -sh $R/tools/$n | cut -f1)"
done

echo ">>> boot menu: the KwickBoot theme (linux/grub-theme.py) + fonts"
TH=$R/boot/grub/themes/kwickboot; mkdir -p $TH
python3 "$HERE/grub-theme.py" $TH "${KB_PRODUCT_VERSION:-0.1}" >/dev/null || { echo "theme: needs python3-pil"; exit 1; }
FR=/usr/share/fonts/truetype/dejavu
RANGE=0x20-0x7E,0xA0-0x17F,0x2010-0x2027,0x2190-0x2199,0x25B2-0x25BC
for sz in 14 16 18 20 22; do grub-mkfont -n "KB Sans" -s $sz -r $RANGE -o $TH/kbsans-$sz.pf2 $FR/DejaVuSans.ttf; done
grub-mkfont -n "KB Sans" -b -s 20 -r $RANGE -o $TH/kbsans-bold-20.pf2 $FR/DejaVuSans-Bold.ttf
grub-mkfont -n "KB Mono" -s 16 -r $RANGE -o $TH/kbmono-16.pf2 $FR/DejaVuSansMono.ttf
cat > $R/boot/grub/grub.cfg <<EOF
search --no-floppy --set=root --file /.disk/kwickboot
set timeout=8
set default=0
insmod all_video
insmod gfxterm
insmod gfxmenu
insmod png
# fonts: only when GRUB is not locked down - under Secure Boot the signed GRUB refuses font files (its own font is used;
# all fixed text of the theme is drawn as pictures for that reason)
if [ "\$lockdown" != "y" ]; then
  loadfont (\$root)/boot/grub/themes/kwickboot/kbsans-14.pf2
  loadfont (\$root)/boot/grub/themes/kwickboot/kbsans-16.pf2
  loadfont (\$root)/boot/grub/themes/kwickboot/kbsans-18.pf2
  loadfont (\$root)/boot/grub/themes/kwickboot/kbsans-20.pf2
  loadfont (\$root)/boot/grub/themes/kwickboot/kbsans-22.pf2
  loadfont (\$root)/boot/grub/themes/kwickboot/kbsans-bold-20.pf2
  loadfont (\$root)/boot/grub/themes/kwickboot/kbmono-16.pf2
fi
set gfxmode=1920x1080,1600x900,1366x768,1280x720,1024x768,auto
set gfxpayload=keep
terminal_output gfxterm
set theme=(\$root)/boot/grub/themes/kwickboot/theme.txt
export theme
menuentry 'Run KwickBoot - from this stick, nothing is installed' --class run {
  linux /kbcore/vmlinuz quiet loglevel=3
  initrd /kbcore/initrd.img /kbcore/iso.cpio.gz
}
menuentry 'Install the KwickBoot server - onto this PC' --class install {
  linux /kbcore/vmlinuz quiet loglevel=3 kb_install=1
  initrd /kbcore/initrd.img /kbcore/iso.cpio.gz
}
menuentry 'Secure wipe - NIST SP 800-88 Rev. 2' --class wipe {
  linux /tools/rescue/vmlinuz quiet loglevel=3 kb_tool=wipe
  initrd /tools/rescue/initrd.img
}
menuentry 'Rescue - passwords, start-up repair, files, disks' --class rescue {
  set gfxpayload=1024x768      # the branded rescue menu is drawn at 1024x768 (centered if the screen is larger)
  linux /tools/rescue/vmlinuz quiet loglevel=3 kb_tool=rescue
  initrd /tools/rescue/initrd.img
}
submenu 'More start options  >' --class safe {
  menuentry 'Run KwickBoot - text only (less RAM)' --class text {
    linux /kbcore/vmlinuz quiet loglevel=3 kb_live=1 kb_gui=0
    initrd /kbcore/initrd.img /kbcore/iso.cpio.gz
  }
  menuentry 'Run KwickBoot - safe graphics' --class safe {
    linux /kbcore/vmlinuz nomodeset loglevel=4 kb_live=1
    initrd /kbcore/initrd.img /kbcore/iso.cpio.gz
  }
  menuentry 'Install - safe graphics' --class safe {
    linux /kbcore/vmlinuz nomodeset loglevel=4 kb_install=1
    initrd /kbcore/initrd.img /kbcore/iso.cpio.gz
  }
  if [ "\$grub_platform" = "pc" -a -f /tools/freedos/initrd.img ]; then
    menuentry 'FreeDOS - BIOS / firmware tools' --class dos {
      linux16 /tools/freedos/vmlinuz floppy
      initrd16 /tools/freedos/initrd.img
    }
  fi
  if [ "\$grub_platform" = "efi" -a -f /tools/efishell/boot.efi ]; then
    menuentry 'EFI Shell - vendor EFI utilities (Secure Boot off)' --class shell {
      if [ "\$lockdown" = "y" ]; then
        echo 'The EFI Shell is not Microsoft-signed: turn Secure Boot off in the firmware settings to use it.'
        sleep 15
      else
        chainloader /tools/efishell/boot.efi
      fi
    }
  fi
}
if [ "\$grub_platform" = "efi" ]; then      # boot the installed OS, shown only when a bootable disk is detected
  if search --no-floppy --file --set=oswin /EFI/Microsoft/Boot/bootmgfw.efi; then
    menuentry 'Boot the installed Windows' --class disk {
      insmod chain
      chainloader (\$oswin)/EFI/Microsoft/Boot/bootmgfw.efi
    }
  elif search --no-floppy --file --set=oslin /EFI/debian/grubx64.efi; then
    menuentry 'Boot the installed Linux' --class disk {
      insmod chain
      chainloader (\$oslin)/EFI/debian/grubx64.efi
    }
  fi
else
  if [ -e (hd1) ]; then                     # BIOS: an internal disk beyond the stick we booted from
    menuentry 'Boot from the internal disk' --class disk {
      insmod chain
      set root=(hd1)
      chainloader +1
    }
  fi
fi
menuentry 'Boot from the next device' --class next {
  exit
}
EOF
# Debian's signed CD GRUB looks for its config next to itself and under /EFI/debian
mkdir -p $R/EFI/debian; cp $R/boot/grub/grub.cfg $R/EFI/debian/grub.cfg

echo ">>> UEFI (Secure Boot: shim -> Debian-signed GRUB -> Debian-signed kernel)"
cp "$SHIM" $R/EFI/boot/bootx64.efi; cp "$SIGNED_GRUB" $R/EFI/boot/grubx64.efi; cp "$MOK" $R/EFI/boot/mmx64.efi
EFI=$W/efi.img; dd if=/dev/zero of=$EFI bs=1M count=16 status=none; mkfs.vfat -n KBEFI $EFI >/dev/null   # 16M: room for the ia32 loader too
mmd -i $EFI ::/EFI ::/EFI/boot ::/EFI/debian
mcopy -i $EFI $R/EFI/boot/bootx64.efi $R/EFI/boot/grubx64.efi $R/EFI/boot/mmx64.efi ::/EFI/boot/
# GRUB on the EFI image: find the ISO (by its .disk marker) and use the ISO's menu
printf 'search --no-floppy --set=root --file /.disk/kwickboot\nset prefix=($root)/boot/grub\nconfigfile ($root)/boot/grub/grub.cfg\n' > $W/stub.cfg
mcopy -i $EFI $W/stub.cfg ::/EFI/debian/grub.cfg; mcopy -i $EFI $W/stub.cfg ::/EFI/boot/grub.cfg
# 32-bit (IA32) UEFI: 64-bit CPUs with 32-bit firmware (Bay Trail tablets, cheap kiosks/POS) that only look for bootia32.efi.
# No Microsoft-signed 32-bit shim exists, so this is unsigned grub-efi-ia32 (Secure Boot is uncommon on 32-bit UEFI); a
# 32-bit UEFI GRUB boots the 64-bit kernel fine on a 64-bit CPU. Guarded: skipped if grub-efi-ia32-bin isn't installed.
if [ -d /usr/lib/grub/i386-efi ]; then
  grub-mkimage -O i386-efi -p /boot/grub -c $W/stub.cfg -o $R/EFI/boot/bootia32.efi \
    part_gpt part_msdos fat iso9660 normal configfile search search_fs_file search_label linux all_video gfxterm gfxmenu png chain echo test
  mcopy -i $EFI $R/EFI/boot/bootia32.efi ::/EFI/boot/
  echo "    + 32-bit UEFI (bootia32.efi) - boots 32-bit-firmware machines"
else
  echo "    (grub-efi-ia32-bin not installed: 32-bit-UEFI boot NOT added - 'apt install grub-efi-ia32-bin')"
fi
cp $EFI $R/boot/grub/efi.img

echo ">>> legacy BIOS (El Torito + hybrid MBR)"
grub-mkimage -O i386-pc-eltorito -p /boot/grub -o $R/boot/grub/bios.img \
  biosdisk iso9660 part_msdos part_gpt linux normal configfile search search_fs_file echo test all_video gfxterm
cp -r /usr/lib/grub/i386-pc $R/boot/grub/ 2>/dev/null || true
# legacy BIOS boot of a KwickBoot USB Creator stick (MBR stick): GRUB's MBR code + a core that finds the KWICKBOOT
# partition by label. The creators write kb-usb-mbr.bin into bytes 0-439 of sector 0 and kb-usb-core.img from sector 1
# (GRUB's default layout: boot.img points at sector 1, the core's blocklist continues at sector 2 - no patching needed).
printf 'search --no-floppy --label KWICKBOOT --set=root
set prefix=($root)/boot/grub
' > $W/usb-embed.cfg
grub-mkimage -O i386-pc -c $W/usb-embed.cfg -p /boot/grub -o $R/boot/grub/kb-usb-core.img   biosdisk part_msdos part_gpt fat search search_label normal configfile
dd if=/usr/lib/grub/i386-pc/boot.img of=$R/boot/grub/kb-usb-mbr.bin bs=440 count=1 status=none
[ $(( $(stat -c %s $R/boot/grub/kb-usb-core.img) / 512 )) -lt 2000 ] || { echo "USB BIOS core too big for the MBR gap"; exit 1; }

echo ">>> ISO"
mkdir -p "$OUT"; ISO="$OUT/kwickboot-$VER-amd64.iso"
[ "$ISO_PROFILE" = full ] && ISO="$OUT/kwickboot-$VER-amd64-full.iso"
xorriso -as mkisofs -iso-level 3 -V KWICKBOOT -J -joliet-long -r \
  -b boot/grub/bios.img -no-emul-boot -boot-load-size 4 -boot-info-table --grub2-boot-info \
  --grub2-mbr /usr/lib/grub/i386-pc/boot_hybrid.img \
  -eltorito-alt-boot -e boot/grub/efi.img -no-emul-boot \
  -append_partition 2 0xef $EFI -appended_part_as_gpt \
  -o "$ISO" $R 2>$W/xorriso.log || { tail -n 20 $W/xorriso.log; exit 1; }
( cd "$OUT" && sha256sum "$(basename "$ISO")" > "$(basename "$ISO").sha256" )
echo "$ISO  profile=$ISO_PROFILE  $(du -h "$ISO" | cut -f1)  $(cut -d' ' -f1 "$ISO.sha256")"
