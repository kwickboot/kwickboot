#!/bin/bash
# Build KwickBoot Core: signed Debian kernel + ONE small zstd initrd (no root image) + on-demand tool packs.
# SPDX-License-Identifier: AGPL-3.0-or-later
#   linux/build-core.sh  ->  /opt/pxe-toolkit/http/kbcore/{vmlinuz, initrd.img, packs/*.sqfs, SHA256SUMS, size-report.txt}
# Core contains only what EVERY machine needs to come up and report: wired NIC + storage + USB/input drivers,
# the firmware those exact drivers declare, busybox, dropbear (SSH for IT), lsblk/lspci/dmidecode/sgdisk, the agent.
# No Wi-Fi, no GPU firmware (network boot is always wired; the console uses the firmware framebuffer).
set -euo pipefail
trap 'echo "BUILD FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
ARCH=${KB_ARCH:-amd64}                                         # amd64 (default), arm64, or i386. arm64/i386 = a lean Core: no RICH WebKit tier, no x86 virt pack.
case "$ARCH" in
  amd64)   TRIPLET=x86_64-linux-gnu;     KPKG=linux-image-amd64;    SUITE=trixie ;;
  arm64)   TRIPLET=aarch64-linux-gnu;    KPKG=linux-image-arm64;    SUITE=trixie ;;
  i386)    TRIPLET=i386-linux-gnu;       KPKG=linux-image-686-pae;  SUITE=bookworm ;;   # Debian 13 dropped i386; bookworm (6.1 LTS, supported to 2028) keeps old 32-bit hardware alive
  riscv64) TRIPLET=riscv64-linux-gnu;    KPKG=linux-image-riscv64;  SUITE=trixie ;;      # RISC-V 64 is an official Debian 13 arch
  loong64) TRIPLET=loongarch64-linux-gnu;KPKG=linux-image-loong64;  SUITE=sid ;;         # LoongArch is in sid (not yet in a stable release)
  *) echo "unsupported KB_ARCH=$ARCH" >&2; exit 2 ;;
esac
SUITE=${KB_SUITE:-$SUITE}
# Kernel: "backports" (default) = Debian's newest SIGNED kernel + NIC firmware from <suite>-backports, so brand-new NICs
# (Realtek RTL8127, Motorcomm YT6801, Intel E610, ...) work on day one and Secure Boot stays intact (linux-signed-*).
# "stable" = the suite's own kernel (fallback). sid has no backports. The build uses the NEWEST kernel in the chroot.
KERNEL=${KB_KERNEL:-backports}; [ "$SUITE" = sid ] && KERNEL=stable
T=${KB_HOME:-/opt/pxe-toolkit}; OUT=$T/http/$([ "$ARCH" = amd64 ] && echo kbcore || echo kbcore-$ARCH)
B=${KB_BUILD:-/var/tmp/kbcore-build-$ARCH}; R=$B/root           # KB_HOME/KB_BUILD: build anywhere; per-arch build dir + output
MIRROR=http://deb.debian.org/debian
PROXY="${KB_PROXY:-}"; [ -z "$PROXY" ] && curl -s -m 3 -o /dev/null http://127.0.0.1:3142/acng-report.html && PROXY=http://127.0.0.1:3142
for t in debootstrap mksquashfs zstd cpio; do command -v $t >/dev/null || { apt-get install -y -q debootstrap squashfs-tools zstd cpio >/dev/null; break; }; done
# The Debian build environment is kept between runs: a rebuild only re-assembles Core + packs (well under a minute).
# "--fresh" recreates it (do this for kernel/security updates).
C=$B/chroot
umount -l $C/dev $C/proc $C/sys 2>/dev/null || true
FRESH=0; [ "${1:-}" = --fresh ] || [ ! -x $C/usr/bin/busybox ] && FRESH=1
if [ $FRESH = 1 ]; then rm -rf --one-file-system $B; else rm -rf --one-file-system $R $B/pack-install $B/pack-diag $B/pack-imaging; fi
mkdir -p $B $R $OUT/packs
if [ $FRESH = 1 ]; then
  echo ">>> build chroot (Debian 13, packages via cache: ${PROXY:-none})"
  env ${PROXY:+http_proxy=$PROXY} debootstrap --arch=$ARCH --variant=minbase $SUITE $C $MIRROR > $B/debootstrap.log
fi
# rslave: chroot mounts/unmounts never propagate to the build host's /dev (it may run other services/VMs)
mount --bind /dev $C/dev; mount --make-rslave $C/dev; mkdir -p $C/dev/pts; mount -t devpts devpts $C/dev/pts 2>/dev/null || mount --bind /dev/pts $C/dev/pts
mount -t proc proc $C/proc; mount -t sysfs sys $C/sys
trap 'umount -l $C/dev/pts $C/dev $C/proc $C/sys 2>/dev/null || true' EXIT
cp /etc/resolv.conf $C/etc/resolv.conf
chroot $C /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin DEBIAN_FRONTEND=noninteractive KBP="$PROXY" KBARCH="$ARCH" KBSUITE="$SUITE" KBKPKG="$KPKG" KBKERNEL="$KERNEL" /bin/bash -s <<'CHROOT'
set -e
[ -n "$KBP" ] && echo "Acquire::http::Proxy \"$KBP\";" > /etc/apt/apt.conf.d/01cache
if [ "$KBSUITE" = sid ]; then   # sid (unstable) is rolling - it has NO -updates / -security suites
  printf 'deb http://deb.debian.org/debian sid main non-free-firmware\n' > /etc/apt/sources.list
else
  printf 'deb http://deb.debian.org/debian %s main non-free-firmware\ndeb http://deb.debian.org/debian %s-updates main non-free-firmware\ndeb http://security.debian.org/debian-security %s-security main non-free-firmware\n' "$KBSUITE" "$KBSUITE" "$KBSUITE" > /etc/apt/sources.list
fi
apt-get update -q >/dev/null
EXTRA=""
case "$KBARCH" in amd64|i386) EXTRA="$EXTRA syslinux-common" ;; esac   # syslinux boot code is x86-only (not arm64/riscv64/loong64)
[ "$KBSUITE" = trixie ] && EXTRA="$EXTRA firmware-intel-misc"     # this firmware split exists only from Debian 13 (trixie); bookworm folds it into firmware-misc-nonfree
apt-get install -y -q --no-install-recommends $KBKPKG busybox-static dropbear-bin util-linux fdisk gdisk pciutils \
  dmidecode efibootmgr kmod xz-utils zstd bzip2 ca-certificates curl \
  firmware-realtek firmware-bnx2 firmware-bnx2x firmware-misc-nonfree firmware-qlogic firmware-netronome \
  debootstrap gpgv gnupg wget dosfstools e2fsprogs xfsprogs btrfs-progs exfatprogs ntfs-3g parted cryptsetup mdadm lvm2 \
  smartmontools nvme-cli hdparm lshw usbutils lm-sensors ipmitool memtester stress-ng ethtool \
  partclone udpcast pv wimtools chntpw sg3-utils gddrescue testdisk cifs-utils $EXTRA >/dev/null
if [ "$KBKERNEL" = backports ]; then   # newest signed kernel + NIC firmware (packages not in backports fall back to stable)
  printf 'deb http://deb.debian.org/debian %s-backports main non-free-firmware\n' "$KBSUITE" > /etc/apt/sources.list.d/backports.list
  apt-get update -q >/dev/null
  BFW="firmware-realtek firmware-misc-nonfree firmware-bnx2 firmware-bnx2x firmware-qlogic"
  [ "$KBSUITE" = trixie ] && BFW="$BFW firmware-intel-misc"
  apt-get install -y -q --no-install-recommends -t "$KBSUITE-backports" $KBKPKG $BFW >/dev/null
else
  rm -f /etc/apt/sources.list.d/backports.list
fi
# RICH-tier browser, only to BUILD the on-demand 'desktop' pack (these go into the chroot, not the Core itself).
# Exactly what kbweb + WebKit + the software-EGL stack need (no cog/cage/seatd/dbus) + multi-script fonts + the toolchain.
# arm64 (lean Core v1) skips the RICH tier entirely - those seats fall back to the kbgui/text tiers.
if [ "$KBARCH" = amd64 ]; then
apt-get install -y -q --no-install-recommends \
  libwpewebkit-2.0-1 libwpebackend-fdo-1.0-1 libwpe-1.0-1 cog cage \
  libwayland-server0 libwayland-client0 libwayland-egl1 libxkbcommon0 libdrm2 libepoxy0 \
  libglvnd0 libegl1 libegl-mesa0 libgles2 libgl1-mesa-dri mesa-libgallium libgbm1 \
  xkb-data fonts-dejavu-core fonts-noto-core fonts-wqy-zenhei fontconfig \
  gcc pkg-config libwpewebkit-2.0-dev libwpebackend-fdo-1.0-dev libwpe-1.0-dev libwayland-dev libxkbcommon-dev >/dev/null
fi
CHROOT
KV=$(ls $C/lib/modules | sort -V | tail -n1)
echo ">>> kernel $KV"

# ---------------------------------------------------------------- helper: copy a binary + its shared libraries
copy_bin() {   # $1 = path inside chroot, $2 = destination root
  local f=$1 dst=$2
  [ -e "$C$f" ] || return 0
  mkdir -p "$dst$(dirname $f)"; cp -a "$C$f" "$dst$f"
  { chroot $C ldd "$f" 2>/dev/null || true; } | { grep -oE '/[^ ]+' || true; } | while read -r l; do
    [ -e "$C$l" ] || continue
    mkdir -p "$dst$(dirname $l)"; [ -e "$dst$l" ] || cp -aL "$C$l" "$dst$l"
  done
  return 0
}

# compile a small C tool FOR THE TARGET ARCH. amd64: host gcc (fast). arm64: inside the chroot via binfmt/qemu-user
# (a host x86 gcc would emit an x86 binary that cannot run on an aarch64 Core). Returns gcc's exit status.
CC_READY=0
ccbuild() {   # $1 = output path inside $R, $2 = source .c (host path), $3.. = extra gcc flags
  local out=$1 src=$2; shift 2
  if [ "$ARCH" = amd64 ]; then
    gcc "$@" -o "$R$out" "$src"
  else
    if [ "$CC_READY" = 0 ]; then
      chroot $C /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin DEBIAN_FRONTEND=noninteractive \
        apt-get install -y -q --no-install-recommends gcc libc6-dev >/dev/null 2>&1 || return 1
      CC_READY=1
    fi
    local b; b=$(basename "$src"); cp "$src" "$C/tmp/$b"
    chroot $C /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin /bin/sh -c "gcc $* -o /tmp/kbout.bin /tmp/$b" || return 1
    cp "$C/tmp/kbout.bin" "$R$out"
  fi
}

echo ">>> Core: busybox, SSH, pack loader"
mkdir -p $R/{bin,etc,proc,sys,dev,run,tmp,root/.ssh,lib/modules,lib/firmware,usr/share/misc,var/log,etc/dropbear}
cp $C/usr/bin/busybox $R/bin/busybox
ln -s bin $R/sbin    # kernel helpers live at /sbin/modprobe and /sbin/mdev
for a in $($C/usr/bin/busybox --list); do [ -e $R/bin/$a ] || ln -s busybox $R/bin/$a; done
for f in /usr/sbin/dropbear /usr/bin/dropbearkey; do copy_bin $f $R; done
# /bin/sh = dash: busybox sh runs its own mini applets (tar, dpkg, ...) even when real tools from a pack come first
# in PATH; dash honours PATH, so pack tools (GNU tar, dpkg, ...) win and busybox still covers everything else
copy_bin /usr/bin/dash $R; ln -sf /usr/bin/dash $R/bin/sh
for ld in /lib64/ld-linux-x86-64.so.2 /lib/ld-linux-aarch64.so.1 /lib/ld-linux.so.2; do   # the dynamic linker for the target arch (x86-64 / aarch64 / i386)
  [ -e "$C$ld" ] && { mkdir -p "$R$(dirname $ld)"; cp -aL "$C$ld" "$R$ld"; }
done
# i386: busybox-static's wget applet segfaults on bookworm i386 -> ship real wget as the bootstrap downloader (init uses `wget`)
if [ "$ARCH" = i386 ]; then
  rm -f $R/bin/wget
  copy_bin /usr/bin/wget $R && [ -e "$R/usr/bin/wget" ] && ln -sf /usr/bin/wget $R/bin/wget
  echo "    i386: real wget shipped as /bin/wget (busybox wget segfaults on i386)"
fi

# ---------------------------------------------------------------- driver sets
# resolver: module list -> + dependencies + soft dependencies (e.g. ext4 -> crc32c), transitively
cat > $B/resolve.py <<'PY'
import os, re, sys
base, sel = sys.argv[1], sys.argv[2]
dep, byname = {}, {}
for line in open(base + "/modules.dep"):
    k, _, v = line.partition(":")
    k = k.strip(); dep[k] = v.split()
    byname[re.sub(r"\.ko(\.xz|\.zst|\.gz)?$", "", os.path.basename(k)).replace("-", "_")] = k
soft = {}                                       # soft dependencies (e.g. ext4 -> crc32c), not listed in modules.dep
for f in ("modules.softdep",):
    try:
        for line in open(base + "/" + f):
            w = line.split()
            if len(w) > 2 and w[0] == "softdep":
                soft.setdefault(w[1].replace("-", "_"), []).extend(x for x in w[2:] if not x.endswith(":"))
    except OSError:
        pass
alias = {}                                      # crypto algorithm names like "crc32c" -> providing modules
for line in open(base + "/modules.alias"):
    w = line.split()
    if len(w) == 3 and w[0] == "alias" and w[1].startswith("crypto-"):
        alias.setdefault(w[1][7:], []).append(w[2].replace("-", "_"))
def mods_for(name):
    n = name.replace("-", "_")
    if n in byname:
        return [byname[n]]
    return [byname[m] for m in alias.get(name, []) if m in byname]
want, stack = set(), [l.strip() for l in open(sel) if l.strip()]
stack += [m for n in ("crc32c", "crct10dif", "crc64-rocksoft", "crc32") for m in mods_for(n)]   # checksum algorithms fs/storage ask for at runtime
while stack:                                    # dependencies + soft dependencies, transitively
    m = stack.pop()
    if m in want or m not in dep:
        continue
    want.add(m); stack.extend(dep[m])
    name = re.sub(r"\.ko(\.xz|\.zst|\.gz)?$", "", os.path.basename(m)).replace("-", "_")
    for sd in soft.get(name, []):
        stack.extend(mods_for(sd))
print("\n".join(sorted(want)))
PY
pick() {   # pick <list-file> <dirs...> : module paths under the given kernel dirs
  local out=$1; shift; : > $out
  for d in "$@"; do p=kernel/$d; [ -e "$C/lib/modules/$KV/$p" ] && (cd $C/lib/modules/$KV && find $p -name '*.ko*') >> $out || true; done
  # Datacenter/SAN NICs (mellanox, qlogic, chelsio, sfc + pensando = AMD, emulex, cisco, cavium, huawei, wangxun, mucse) are IN: iSCSI/SAN
  # servers must reach the network from the Core. Still out: netronome (~100 MB firmware) + cloud-only/rare vNICs.
  grep -vE 'ethernet/(netronome|fungible|google|microsoft|amazon|brocade|ibm|mscc|meta)/' $out > $out.f || true
  mv $out.f $out
}
pickmod() {   # pickmod <list-file> <module-basenames...> : append each module found by NAME anywhere in the tree.
  # Arch-robust: a module's path/extension differs between kernels (amd64 6.12 vs i386 6.1; bochs moved
  # drivers/gpu/drm/bochs -> drivers/gpu/drm/tiny), so a hardcoded drivers/.../x.ko.xz silently vanishes on
  # another arch. By name it is always found. Appends (call AFTER pick, which truncates the list).
  local out=$1; shift
  for n in "$@"; do (cd $C/lib/modules/$KV && find kernel -name "$n.ko*") >> $out || true; done
}
install_mods() {   # install_mods <resolved-list> <dest-root> : decompressed (busybox modprobe; signatures stay valid)
  while read -r m; do
    mkdir -p $2/lib/modules/$KV/$(dirname $m)
    case "$m" in *.xz) xz -dc "$C/lib/modules/$KV/$m" > "$2/lib/modules/$KV/${m%.xz}" ;; *) cp "$C/lib/modules/$KV/$m" "$2/lib/modules/$KV/$m" ;; esac
  done < $1
}
install_fw() {   # install_fw <resolved-list> <dest-root> : only the firmware these exact drivers declare
  while read -r m; do chroot $C modinfo -F firmware "/lib/modules/$KV/$m" 2>/dev/null; done < $1 | sort -u | while read -r fw; do
    for src in $C/lib/firmware/$fw $C/lib/firmware/$fw.zst $C/lib/firmware/$fw.xz; do
      [ -e "$src" ] || continue
      mkdir -p "$2/lib/firmware/$(dirname $fw)"; cp -aL "$src" "$2/lib/firmware/$(dirname $fw)/"
    done
  done
}

echo ">>> Core: ALL wired network drivers + their firmware (the network brings everything else)"
# Plus the pointer/keyboard input stack (evdev + USB/PS2/VM-tablet HID) so the local framebuffer GUI (kbgui) has a working
# mouse the moment it starts - the GUI runs from Core before any pack loads, so these cannot wait for the hw pack.
pick $B/core.sel drivers/net/ethernet drivers/net/usb drivers/net/phy drivers/net/mdio drivers/net/pcs drivers/ptp drivers/pps \
  drivers/usb/host drivers/usb/core drivers/usb/common drivers/virtio drivers/net/virtio_net.ko.xz drivers/net/vmxnet3 \
  drivers/net/hyperv drivers/hv drivers/block/loop.ko.xz fs/squashfs fs/overlayfs \
  drivers/input/evdev.ko.xz drivers/input/serio/hyperv-keyboard.ko.xz \
  drivers/hid/hid.ko.xz drivers/hid/usbhid drivers/hid/hid-generic.ko.xz drivers/hid/hid-hyperv.ko.xz drivers/hid/hid-multitouch.ko.xz \
  drivers/gpu/drm/drm.ko.xz drivers/gpu/drm/drm_kms_helper.ko.xz drivers/gpu/drm/drm_shmem_helper.ko.xz drivers/gpu/drm/ttm \
  drivers/gpu/drm/virtio drivers/gpu/drm/hyperv drivers/gpu/drm/vmwgfx drivers/gpu/drm/vboxvideo
# input + VM video by NAME so they are present on EVERY arch (fixes evdev missing on i386 -> kbgui had no
# input): evdev + psmouse (PS/2 mice) + atkbd (PS/2 kbd) + serio_raw; bochs = bochs-drm, the standard
# QEMU/Proxmox/Bochs VGA the local GUI draws on.
pickmod $B/core.sel evdev mousedev psmouse atkbd serio_raw bochs bochs-drm
python3 $B/resolve.py $C/lib/modules/$KV $B/core.sel > $B/modules.all
install_mods $B/modules.all $R
cp $C/lib/modules/$KV/modules.{order,builtin,builtin.modinfo} $R/lib/modules/$KV/ 2>/dev/null || true
depmod -b $R $KV
install_fw $B/modules.all $R
# NIC coverage gate: every chip in linux/nic-must-have.txt must be drivable by THIS Core, or the build fails
# (KB_NICGATE=warn to only report, e.g. for a KB_KERNEL=stable fallback build). amd64 list only, for now.
if [ "$ARCH" = amd64 ] && [ -f $T/linux/nic-must-have.txt ]; then
  if python3 $T/linux/nicgate.py $R/lib/modules/$KV $T/linux/nic-must-have.txt > $B/nicgate.txt; then
    echo ">>> $(head -n1 $B/nicgate.txt) (kernel $KV)"
  else
    cat $B/nicgate.txt
    [ "${KB_NICGATE:-$([ "$KERNEL" = stable ] && echo warn || echo fail)}" = warn ] || { echo "BUILD FAILED: NIC coverage gate (kernel $KV)"; exit 1; }
  fi
fi

echo ">>> hw pack: storage, USB storage, input, filesystems + hardware-scan tools (loaded right after the network)"
H=$B/pack-hw; rm -rf $H; mkdir -p $H/lib/modules/$KV $H/usr/share/misc
pick $B/hw.sel drivers/ata drivers/nvme drivers/scsi/sd_mod.ko.xz drivers/scsi/sr_mod.ko.xz drivers/scsi/megaraid drivers/scsi/mpt3sas \
  drivers/scsi/smartpqi drivers/scsi/hpsa.ko.xz drivers/scsi/aacraid drivers/scsi/virtio_scsi.ko.xz drivers/scsi/vmw_pvscsi.ko.xz drivers/scsi/hv_storvsc.ko.xz \
  drivers/block/virtio_blk.ko.xz drivers/usb/storage drivers/hid/usbhid drivers/hid/hid-generic.ko.xz drivers/input/keyboard/atkbd.ko.xz \
  drivers/mmc/host/sdhci.ko.xz drivers/mmc/host/sdhci-pci.ko.xz drivers/mmc/core \
  fs/fat fs/ext4 fs/jbd2 fs/mbcache.ko.xz fs/exfat fs/ntfs3 fs/isofs fs/nls fs/efivarfs
python3 $B/resolve.py $C/lib/modules/$KV $B/hw.sel | grep -vxF -f $B/modules.all > $B/hw.all || true
install_mods $B/hw.all $H
install_fw $B/hw.all $H
for f in /usr/bin/lsblk /usr/sbin/blkid /usr/sbin/wipefs /usr/bin/lscpu /usr/sbin/sgdisk /usr/bin/lspci /usr/sbin/dmidecode /usr/bin/efibootmgr; do copy_bin $f $H; done
cp $C/usr/share/misc/pci.ids $H/usr/share/misc/ 2>/dev/null || true
(cd $R && find . -type f) | while read -r x; do if [ -f "$H/$x" ] && cmp -s "$H/$x" "$R/$x"; then rm -f "$H/$x"; fi; done
mksquashfs $H $OUT/packs/hw.sqfs.new -comp zstd -Xcompression-level 19 -b 1M -noappend -quiet -all-root
mv -f $OUT/packs/hw.sqfs.new $OUT/packs/hw.sqfs

echo ">>> Core: init, agent bootstrap, console, curl shim"
python3 - "$R" <<'PY'
import os, sys
sys.path.insert(0, os.environ.get("KB_HOME", "/opt/pxe-toolkit"))
import ramos
r = sys.argv[1]
open(r + "/bin/kb-console", "w").write(ramos.CONSOLE)
open(r + "/bin/kb-facts", "w").write(ramos.FACTS)
open(r + "/bin/kb-gui-agent", "w").write(ramos.GUIAGENT)
open(r + "/bin/kb-desktop", "w").write(ramos.DESKTOP)
open(r + "/etc/issue", "w").write(ramos.ISSUE)
open(r + "/etc/motd", "w").write(ramos.MOTD)
PY
# web console terminal (admins): our own small pty relay, static so it needs nothing else in the Core (linux/kbpty.c)
ccbuild /bin/kbpty "${KB_HOME:-/opt/pxe-toolkit}/linux/kbpty.c" -static -Os -s -Wall
# branded boot splash (plymouth-style): KwickBoot logo + progress bar on the framebuffer, shown by init as Core comes up.
# Built on the host (gcc + python3-pil + a kbd console font); links Core's glibc at runtime. Plain text boot if absent or
# the PC has no 32bpp framebuffer (kbsplash exits 2 -> init just skips it).
KBH="${KB_HOME:-/opt/pxe-toolkit}"
if command -v gcc >/dev/null 2>&1 && python3 -c 'import PIL' 2>/dev/null; then
  mkdir -p $R/usr/share/kbsplash
  if ccbuild /bin/kbsplash "$KBH/linux/kbsplash.c" -O2 -s 2>/dev/null \
     && python3 "$KBH/linux/boot-splash.py" $R/usr/share/kbsplash >/dev/null \
     && python3 "$KBH/linux/psf2raw.py" $R/usr/share/kbsplash/font.raw >/dev/null; then
    rm -f $R/usr/share/kbsplash/splash.png; chmod 755 $R/bin/kbsplash; echo "    boot splash: framebuffer (branded)"
  else echo "    boot splash: none (framebuffer splash build failed)"; rm -rf $R/bin/kbsplash $R/usr/share/kbsplash; fi
else echo "    boot splash: none (needs gcc + python3-pil + kbd console fonts on the build box)"; fi
# branded local GUI (framebuffer + mouse): the status/install screen the person at the machine sees. Blits pre-rendered
# chrome, draws the live facts (kb-facts) into it, runs a mouse cursor + clickable tiles. Scales to any framebuffer res;
# a click writes the action to /run/kb-gui-action. Built on the host (gcc + python3-pil + a 24px kbd font). If absent or
# the PC has no 32bpp framebuffer, init falls back to the text kb-console - same facts, no mouse.
if command -v gcc >/dev/null 2>&1 && python3 -c 'import PIL' 2>/dev/null; then
  mkdir -p $R/usr/share/kbgui
  # --all bakes the chrome in every UI language (~4.6 MB total) so the machine can switch its own UI offline. Needs the
  # DejaVu + Noto (CJK/Arabic/Thai/Devanagari) fonts + Pillow-with-raqm on the BUILD HOST; without them CJK/complex langs
  # render as blanks (English still works). apt: fonts-noto-core fonts-noto-cjk fonts-dejavu-core python3-pil(+raqm).
  if ccbuild /bin/kbgui "$KBH/linux/kbgui.c" -O2 -s 2>/dev/null \
     && python3 "$KBH/linux/gui-chrome.py" $R/usr/share/kbgui --all >/dev/null; then
    rm -f $R/usr/share/kbgui/chrome-*.png $R/usr/share/kbgui/.probe.raw; chmod 755 $R/bin/kbgui
    echo "    local GUI: framebuffer + mouse ($(ls $R/usr/share/kbgui/chrome-*-*.info 2>/dev/null | sed 's/.*chrome-\([a-z][a-z]\)-.*/\1/' | sort -u | tr '\n' ' ')/ $(ls $R/usr/share/kbgui/chrome-en-*.info 2>/dev/null | wc -l) res)"
  else echo "    local GUI: none (framebuffer GUI build failed) - text console fallback"; rm -rf $R/bin/kbgui $R/usr/share/kbgui; fi
else echo "    local GUI: none (needs gcc + python3-pil + kbd console fonts on the build box)"; fi
# kb-console translations: the text fallback's labels in the Latin/Cyrillic languages (the console font can't do CJK/Arabic,
# which get the localised UI from kbgui/the browser tier). kb-console sources /usr/share/kb-i18n/$lang.env; set by kb_lang=.
if python3 -c 'import sys' 2>/dev/null; then
  mkdir -p $R/usr/share/kb-i18n
  if python3 "$KBH/linux/gen-console-i18n.py" $R/usr/share/kb-i18n >/dev/null 2>&1; then
    echo "    console i18n: $(ls $R/usr/share/kb-i18n/*.env 2>/dev/null | wc -l) languages"
  else echo "    console i18n: none (generator failed)"; rm -rf $R/usr/share/kb-i18n; fi
fi

cat > $R/bin/curl <<'EOF'
#!/bin/sh
# KwickBoot Core: the few curl options the agent uses, on top of busybox wget (saves curl's libraries)
t=20; h1=""; h2=""; post=""; pf=""; out="-"; url=""; hasdata=""
while [ $# -gt 0 ]; do
  case "$1" in
    -m|--max-time) t=$2; shift ;;
    -H|--header) if [ -z "$h1" ]; then h1=$2; else h2=$2; fi; shift ;;
    --data-binary|-d|--data) hasdata=1; case "$2" in @*) pf=${2#@} ;; *) post=$2 ;; esac; shift ;;
    -o|--output) out=$2; shift ;;
    -X|-w|-A|--pinnedpubkey|--cacert|--capath) shift ;;   # --pinnedpubkey: busybox wget can't pin, so consume its value and ignore (TLS still encrypts)
    -*) ;;                                                  # other bare flags (-s, -k, --insecure, ...) take no value
    *) url=$1 ;;
  esac; shift
done
set -- -q -T "$t" -O "$out"
[ -n "$h1" ] && set -- "$@" --header "$h1"
[ -n "$h2" ] && set -- "$@" --header "$h2"
if [ -n "$pf" ]; then set -- "$@" --post-file "$pf"; elif [ -n "$hasdata" ]; then set -- "$@" --post-data "$post"; fi
exec busybox wget "$@" "$url"
EOF
cat > $R/etc/udhcpc.script <<'EOF'
#!/bin/sh
case "$1" in
  bound|renew) ip addr flush dev "$interface"; ip addr add "$ip/${mask:-24}" broadcast "${broadcast:-+}" dev "$interface"   # broadcast: multicast imaging (udpcast) finds its peers through it
               [ -n "$router" ] && { ip route del default 2>/dev/null; ip route add default via ${router%% *} dev "$interface"; }
               : > /etc/resolv.conf; for d in $dns; do echo "nameserver $d" >> /etc/resolv.conf; done ;;
esac
EOF
cat > $R/bin/kb-net <<'EOF'
#!/bin/sh
# kb-net: keep trying until this PC is on the network, then fetch the SSH keys + the agent. Never gives up (backoff to
# 30 s), and writes WHY it is offline to /run/kb-netdiag (one line per cause) for the GUI and the text console:
# no link (cable/switch), link but no DHCP reply, an Ethernet card with no driver in this Core, or the server not answering.
S=$(cat /etc/kb-server 2>/dev/null); D=/run/kb-netdiag; w=3
nics() { for i in $(ls /sys/class/net); do [ -e /sys/class/net/$i/device ] && echo $i; done; }   # physical cards only
myip() { ip -4 -o addr show 2>/dev/null | awk '$2!="lo"{sub("/.*","",$4); print $4; exit}'; }
backoff() { sleep $w; w=$((w * 2)); [ $w -gt 30 ] && w=30; }
diag() {
  : > $D.tmp
  for i in $(nics); do
    if [ "$(cat /sys/class/net/$i/carrier 2>/dev/null)" = 1 ]; then echo "$i: cable connected, but no DHCP reply - retrying" >> $D.tmp
    else echo "$i: no link - check the cable / switch port" >> $D.tmp; fi
  done
  for p in /sys/bus/pci/devices/*; do   # PCI class 0x0200 = Ethernet controller; no driver bound = this Core cannot use it
    case "$(cat $p/class 2>/dev/null)" in 0x0200*) ;; *) continue ;; esac
    [ -e $p/driver ] && continue
    v=$(cat $p/vendor); d=$(cat $p/device)
    echo "Ethernet card ${v#0x}:${d#0x} has NO DRIVER in this RAM OS - report this ID to IT" >> $D.tmp
  done
  [ -s $D.tmp ] || echo "no wired network card found" >> $D.tmp
  mv -f $D.tmp $D
}
while [ -z "$(myip)" ]; do
  for i in $(nics); do ip link set "$i" up 2>/dev/null; done
  for i in $(nics); do
    [ "$(cat /sys/class/net/$i/carrier 2>/dev/null)" = 1 ] || continue
    udhcpc -i "$i" -s /etc/udhcpc.script -q -n -t 3 -T 2 -x hostname:kwickboot >/dev/null 2>&1 && break
  done
  [ -n "$(myip)" ] && break
  diag; backoff
done
w=2
until wget -q -T 10 -O /tmp/agent.sh "http://$S/scripts/agent.sh"; do
  echo "network up ($(myip)) but the KwickBoot server $S does not answer - retrying" > $D; backoff
done
rm -f $D
wget -q -T 10 -O /root/.ssh/authorized_keys "http://$S/authorized_keys" 2>/dev/null; chmod 600 /root/.ssh/authorized_keys 2>/dev/null
exec sh /tmp/agent.sh > /var/log/pxe-agent.log 2>&1
EOF
cat > $R/init <<'EOF'
#!/bin/sh
# KwickBoot Core init: drivers -> wired network -> SSH -> agent. Everything runs from RAM; disks are never mounted.
export PATH=/bin:/sbin:/usr/bin:/usr/sbin
mount -t proc proc /proc; mount -t sysfs sys /sys; mount -t devtmpfs dev /dev; mkdir -p /dev/pts /run
mount -t devpts devpts /dev/pts; mount -t tmpfs run /run
mount -t efivarfs efivarfs /sys/firmware/efi/efivars 2>/dev/null
echo "KwickBoot Core $(cat /etc/kb-core-version)" > /dev/kmsg
hostname kwickboot; echo kwickboot > /etc/hostname
# branded boot splash (plymouth-style): shows the KwickBoot logo + a progress bar on the framebuffer as Core comes up.
# No-op on a PC with no 32bpp framebuffer (kbsplash exits 2) or if the splash was not built -> plain text boot.
splash() { [ -x /bin/kbsplash ] && KB_SPIN="$3" /bin/kbsplash /usr/share/kbsplash/splash.bgra.gz /usr/share/kbsplash/splash.info /usr/share/kbsplash/font.raw "$1" "$2" 2>/dev/null; }
splash 8 "Starting KwickBoot" 1.0
# drivers for the hardware that is present (modalias -> modprobe); repeat until a pass loads nothing new (max 5).
# Storage drivers are in the hw pack: the agent runs the same scan (plus disk class drivers) after loading it.
n0=-1; for pass in 1 2 3 4 5; do
  for a in $(cat /sys/bus/*/devices/*/modalias 2>/dev/null | sort -u); do modprobe -q "$a" 2>/dev/null; done
  n=$(wc -l < /proc/modules); [ "$n" = "$n0" ] && break; n0=$n
done   # no inter-pass sleep: the modalias scan is instant; devices that appear later are caught by mdev hotplug below
# devices that appear after boot (USB disks plugged in, slow links): mdev loads their driver from the modalias
echo '$MODALIAS=.* 0:0 660 @modprobe -q -b "$MODALIAS"' > /etc/mdev.conf
echo /sbin/modprobe > /proc/sys/kernel/modprobe   # lets the kernel load filesystem drivers on demand (ext4, ...)
for m in loop squashfs overlay evdev psmouse atkbd bochs bochs-drm virtio_gpu hyperv_drm vmwgfx vboxvideo; do modprobe -q $m 2>/dev/null; done   # not tied to a probeable bus device: load explicitly. evdev/psmouse/atkbd = local GUI input (incl. PS/2 mouse+kbd); bochs = QEMU/Proxmox std VGA fb; virtio_gpu/vmwgfx/vboxvideo = GBM DRM for RICH; hyperv_drm = fb-only -> kbgui
# device nodes now + hotplug via netlink (Debian kernels have no /proc/sys/kernel/hotplug; writing it printed an error on screen)
mdev -s 2>/dev/null; mdev -d 2>/dev/null
splash 30 "Loading drivers"
# network: DHCP only on a wired interface that HAS a link (skip link-down cards entirely - no wasted DHCP
# timeout). The NIC we booted from already has carrier, so usually there is nothing to wait for; wait once,
# briefly, only if no link is up yet (a slow PHY settling) instead of a flat delay.
for i in $(ls /sys/class/net | grep -v '^lo$'); do ip link set "$i" up; done
# Real PHYs (e.g. Realtek RTL8168/8111 on mini PCs) need ~2-4 s after "link up" to report carrier; a flat 1 s wait
# skipped DHCP on them entirely. Poll up to 8 s, leaving as soon as any card has a link.
for t in 1 2 3 4 5 6 7 8; do
  up=; for i in $(ls /sys/class/net | grep -v '^lo$'); do [ "$(cat /sys/class/net/$i/carrier 2>/dev/null)" = 1 ] && up=1; done
  [ -n "$up" ] && break; sleep 1
done
for i in $(ls /sys/class/net | grep -v '^lo$'); do
  [ "$(cat /sys/class/net/$i/carrier 2>/dev/null)" = 1 ] || continue   # no link -> skip DHCP on this card
  udhcpc -i "$i" -s /etc/udhcpc.script -q -n -t 8 -T 2 -x hostname:kwickboot >/dev/null 2>&1 && break
done
splash 65 "Network ready"
S=""; KL=""; for w in $(cat /proc/cmdline); do case "$w" in kb_server=*) S=${w#kb_server=} ;; kb_lang=*) KL=${w#kb_lang=} ;; esac; done
echo "$S" > /etc/kb-server
echo "${KL:-en}" > /etc/kb-lang    # text-console UI language (kbgui has its own in-screen picker)
# SSH for IT: keys from the server, host keys new on every boot
wget -q -T 10 -O /root/.ssh/authorized_keys "http://$S/authorized_keys" 2>/dev/null; chmod 700 /root/.ssh; chmod 600 /root/.ssh/authorized_keys 2>/dev/null
dropbearkey -t ed25519 -f /etc/dropbear/dropbear_ed25519_host_key >/dev/null 2>&1
echo 'root:x:0:0:root:/root:/bin/sh' > /etc/passwd; echo 'root:*:1::::::' > /etc/shadow; echo 'root:x:0:' > /etc/group
dropbear -R -s -E 2>/var/log/dropbear.log
splash 100 "Ready" 2.0
# consoles: status screen on tty1. A password-less root shell on tty2 would let anyone at the keyboard (netbar, lab,
# kiosk) wipe disks, so it exists only when IT boots with kb_shell=1 on the kernel command line.
# A framebuffer PC gets the branded mouse-driven GUI (kb-facts feeds it the live facts); everything else, or if the GUI
# cannot open a 32bpp framebuffer, drops to the same facts as text. Both stay in sync with the web console.
# Adaptive UI: pick the best UI for this machine's power. RICH (capable: DRM/KMS GPU + enough RAM) -> the browser desktop
# (kb-desktop, fetched on demand); STANDARD (any framebuffer) -> the native kbgui; ANCIENT (no fb) -> the text console.
# Each tier falls through to the next if its program is missing or fails, so weak/old hardware always lands somewhere.
RAMKB=$(awk '/^MemTotal/{print $2}' /proc/meminfo 2>/dev/null)
# RICH (kbweb browser) needs a framebuffer (/dev/fb0) + enough RAM. kbweb paints WebKit on the CPU and blits straight to
# fb0 - NO GPU, NO DRM master, NO KMS/GL - so it runs wherever kbgui does (efifb, simpledrm, hyperv_drm, virtio-gpu, real
# GPUs). Machines with the fb but too little RAM fall to kbgui; machines with no fb at all fall to the text console.
# RICH from ~1 GB (900000 kB: a "1 GB" PC reports ~0.9-0.97 GB usable after firmware/iGPU reservations).
# Each tier runs WITHOUT exec so that when it exits (no chrome for this screen, no input, crash) the next tier really
# starts - "exec A || exec B" never reaches B once A has started, which left tty1 black.
if [ -e /dev/fb0 ] && [ "${RAMKB:-0}" -ge 900000 ] && [ -x /bin/kb-desktop ]; then
  /bin/kb-facts >/dev/null 2>&1 &
  /bin/kb-gui-agent >/dev/null 2>&1 &
  # NB: kb-desktop WITHOUT exec - it can exit (RICH unavailable on this GPU), and the shell must survive to run the || fallback.
  setsid sh -c '/bin/kb-desktop <> /dev/tty1 >&0 2>&1 || /bin/kbgui /usr/share/kbgui /run/kb-facts <> /dev/tty1 >&0 2>&1 || exec /bin/kb-console <> /dev/tty1 >&0 2>&1' &
elif [ -e /dev/fb0 ] && [ -x /bin/kbgui ]; then
  /bin/kb-facts >/dev/null 2>&1 &
  /bin/kb-gui-agent >/dev/null 2>&1 &
  setsid sh -c '/bin/kbgui /usr/share/kbgui /run/kb-facts <> /dev/tty1 >&0 2>&1 || exec /bin/kb-console <> /dev/tty1 >&0 2>&1' &
else
  setsid sh -c 'exec /bin/kb-console <> /dev/tty1 >&0 2>&1' &
fi
# (setsid -c: tty2 becomes the shell's controlling terminal - job control, Ctrl+C; respawned when the shell exits)
grep -qw kb_shell=1 /proc/cmdline && ( while :; do setsid -c /bin/sh -l <> /dev/tty2 >&0 2>&1; sleep 1; done ) &
# network watchdog + agent (fetched from the server, so it is always current). kb-net retries forever and shows why
# it is offline on screen, so a slow link / late cable / missing driver never leaves the Core sitting silently.
/bin/kb-net > /dev/null 2>&1 &
# PID 1 must never exit
while true; do sleep 3600; done
EOF
chmod 755 $R/init $R/bin/kb-net $R/bin/curl $R/bin/kb-console $R/bin/kb-facts $R/bin/kb-gui-agent $R/bin/kb-desktop $R/bin/kbpty $R/etc/udhcpc.script
date +%Y%m%d-%H%M > $R/etc/kb-core-version; echo "$(cat $R/etc/kb-core-version)" > $R/etc/kwickboot-ramos-version
touch $R/etc/resolv.conf

echo ">>> packs (zstd squashfs, loaded on demand by the agent)"
mkpack() {   # $1 = name, rest = binaries / dirs (paths inside the chroot)
  local name=$1; shift; local P=$B/pack-$name; rm -rf $P; mkdir -p $P
  for f in "$@"; do
    if [ -d "$C$f" ]; then mkdir -p "$P$(dirname $f)"; cp -a "$C$f" "$P$(dirname $f)/"
    else copy_bin "$f" "$P"; fi
  done
  # drop what Core and the always-loaded hw pack already have
  for base in $R $B/pack-hw; do
    (cd $base && find . -type f) | while read -r x; do if [ -f "$P/$x" ] && cmp -s "$P/$x" "$base/$x"; then rm -f "$P/$x"; fi; done
  done
  mksquashfs $P $OUT/packs/$name.sqfs.new -comp zstd -Xcompression-level 19 -b 1M -noappend -quiet -all-root
  mv -f $OUT/packs/$name.sqfs.new $OUT/packs/$name.sqfs
}
mkpack install /usr/bin/mountpoint /usr/bin/gpg /usr/bin/gpg-agent /usr/bin/gpgconf /usr/libexec/keyboxd /usr/lib/gnupg /usr/sbin/debootstrap /usr/share/debootstrap /usr/share/keyrings /usr/bin/perl /usr/share/perl /usr/lib/$TRIPLET/perl-base \
  /usr/bin/gpgv /usr/bin/wget /usr/sbin/mkfs.vfat /usr/sbin/mkfs.fat /usr/sbin/mkfs.ext4 /usr/sbin/mke2fs /usr/sbin/mkfs.xfs \
  /usr/sbin/partprobe /usr/sbin/parted /usr/bin/findmnt /usr/sbin/chroot /usr/bin/dpkg /usr/bin/dpkg-deb /usr/bin/ar \
  /usr/bin/zstd /usr/bin/xz /usr/bin/gzip /usr/bin/bzip2 /usr/bin/tar /usr/bin/base64 /usr/bin/sha256sum /usr/sbin/fsck.vfat /usr/sbin/e2fsck \
  /etc/ssl/certs/ca-certificates.crt
# secure: real curl (+ libcurl/TLS libs), fetched on demand only when the GUI submits IT credentials, so it can key-PIN the
# server (--pinnedpubkey) instead of the busybox-wget shim's encrypt-only TLS. Kept out of the Core so the RAM image stays tiny.
mkpack secure /usr/bin/curl
# desktop: the RICH-tier browser kiosk (cog on WPE WebKit + cage + software-GL mesa/llvmpipe), fetched on demand ONLY by
# capable machines (kb-desktop). ~150 MB, software rendering = one path for any GPU with KMS. Assembled from the packages'
# complete dpkg -L file lists (versioned/dlopen'd mesa+webkit files are not ldd-visible) + the ldd closure of the binaries.
# Cache-guarded: a normal rebuild reuses the existing pack; force a rebuild with --packs.
if [ "$ARCH" != amd64 ]; then
  echo ">>> Core: desktop (RICH WebKit) pack skipped on $ARCH - seats use the kbgui/text tiers"
elif [ -s $OUT/packs/desktop.sqfs ] && [ "${1:-}" != --packs ]; then
  echo ">>> Core: desktop pack cached ($(du -h $OUT/packs/desktop.sqfs | cut -f1))"
else
  echo ">>> Core: desktop pack (kbweb CPU framebuffer WPE browser) - building"
  DPK=$B/pack-desktop; rm -rf $DPK; mkdir -p $DPK
  # Compile kbweb (our CPU framebuffer WPE browser) inside the chroot against its WPE libs. No cog/cage/mesa/seatd.
  cp "$KB_HOME/kbweb.c" $C/tmp/kbweb.c
  chroot $C /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin /bin/sh -c 'cd /tmp && rm -f /usr/bin/kbweb && gcc -O2 -o /usr/bin/kbweb kbweb.c $(pkg-config --cflags --libs wpe-webkit-2.0 wpebackend-fdo-1.0 xkbcommon) -lwayland-server'
  [ -x $C/usr/bin/kbweb ] || { echo "!!! kbweb compile FAILED - aborting"; exit 1; }
  echo "    kbweb compiled ($(du -h $C/usr/bin/kbweb | cut -f1))"
  # Minimal set: ONLY what kbweb + WebKit's out-of-process helpers + the software-EGL stack actually load. No cog/cage/
  # wlroots/seatd/dbus/udev/bubblewrap (kbweb is the runtime - it needs none of the Wayland-compositor/session machinery).
  # The EGL-platform libs that WebKit's GPU process DLOPENs (not ldd-visible) are listed explicitly: libdrm/libepoxy/
  # libwayland-egl+client/libxkbcommon/libGLESv2(libgles2)/libglapi. xkb-data = the keymap data xkbcommon needs.
  for pkg in libwpewebkit-2.0-1 libwpebackend-fdo-1.0-1 libwpe-1.0-1 \
             libwayland-server0 libwayland-client0 libwayland-egl1 libxkbcommon0 libdrm2 libepoxy0 \
             libglvnd0 libegl1 libegl-mesa0 libgles2 libgl1-mesa-dri mesa-libgallium libllvm19 libgbm1 \
             xkb-data fonts-dejavu-core fonts-noto-core fonts-wqy-zenhei fontconfig libfontconfig1 libcap2; do
    chroot $C dpkg -L "$pkg" 2>/dev/null | while read -r f; do
      [ -f "$C$f" ] || continue
      case "$f" in */doc/*|*/man/*|*/lintian/*|*/changelog*|*.h) continue ;; esac
      mkdir -p "$DPK$(dirname "$f")"; cp -a "$C$f" "$DPK$f"
    done
  done
  # kbweb + fc-cache + their ldd closures (libWPEWebKit, libWPEBackend-fdo, libwpe, libwayland-server, glib, gstreamer, ...)
  copy_bin /usr/bin/kbweb $DPK
  copy_bin /usr/bin/fc-cache $DPK
  # cog/cage are NOT run (kbweb is the browser) but their ldd closure reliably pulls the exact set of libs WebKit's EGL/GPU
  # process needs at runtime - a fragile, hard-to-enumerate set - so we closure them purely as a lib source. The main pack
  # bulk is libLLVM (~129M, llvmpipe) + WebKit, not these; dropping them saves little and breaks EGL, so keep them.
  for b in /usr/bin/cog /usr/bin/cage; do copy_bin "$b" "$DPK"; done
  for so in $DPK/usr/lib/x86_64-linux-gnu/cog/modules/*.so; do [ -e "$so" ] && copy_bin "${so#$DPK}" "$DPK"; done
  # WebKit runs out-of-process helpers (WPEWebProcess/WPENetworkProcess + the injected bundle), dlopen'd / exec'd and NOT
  # ldd-visible from kbweb - closure every file WebKit ships under its private dirs so their deps come along too.
  for d in /usr/lib/x86_64-linux-gnu/wpe-webkit-2.0 /usr/libexec/wpe-webkit-2.0; do
    [ -d "$DPK$d" ] && find "$DPK$d" -type f 2>/dev/null | while read -r x; do copy_bin "${x#$DPK}" "$DPK"; done
  done
  # mesa software EGL: WebKit's WebProcess creates an EGL display at startup even in CPU/SHM mode. The dri drivers
  # (swrast/kms_swrast -> llvmpipe via libLLVM) are dlopen'd, not ldd-visible, so closure them for their deps.
  for so in $DPK/usr/lib/x86_64-linux-gnu/dri/*.so $DPK/usr/lib/x86_64-linux-gnu/gbm/*.so; do
    [ -e "$so" ] && copy_bin "${so#$DPK}" "$DPK"
  done
  # Dedup ONLY against Core ($R) - the hw pack is NOT overlaid on the RICH/desktop path, so a lib deduped against it would
  # go missing at runtime (this is what dropped libm.so.6 and broke dbus-run-session/cog). Core is the sole guaranteed base.
  for base in $R; do (cd $base && find . -type f 2>/dev/null) | while read -r x; do if [ -f "$DPK/$x" ] && cmp -s "$DPK/$x" "$base/$x"; then rm -f "$DPK/$x"; fi; done; done
  mksquashfs $DPK $OUT/packs/desktop.sqfs.new -comp zstd -Xcompression-level 19 -b 1M -noappend -quiet -all-root && mv -f $OUT/packs/desktop.sqfs.new $OUT/packs/desktop.sqfs
  echo "    desktop pack: $(du -h $OUT/packs/desktop.sqfs | cut -f1)"
fi
mkpack diag /usr/sbin/smartctl /usr/sbin/nvme /usr/sbin/hdparm /usr/bin/lshw /usr/bin/lsusb /usr/bin/sensors /usr/sbin/sensors-detect \
  /usr/bin/ipmitool /usr/sbin/memtester /usr/bin/stress-ng /usr/sbin/ethtool /usr/sbin/mdadm /usr/sbin/lvm /usr/sbin/cryptsetup \
  /usr/share/smartmontools /usr/share/misc/usb.ids /usr/bin/perl /usr/share/perl
# wipe: the lean secure-wipe start (stick "Securely wipe", ~10 MB): ATA / NVMe / SCSI sanitize + secure erase tools
mkpack wipe /usr/sbin/hdparm /usr/sbin/nvme /usr/bin/sg_sanitize /usr/bin/sg_format /usr/bin/sg_readcap /usr/bin/sg_inq /usr/sbin/smartctl /usr/share/smartmontools
# rescue: the lean rescue start (~10 MB): offline password reset (Windows: chntpw, Linux: chpasswd in the system),
# BitLocker / LUKS unlock, NTFS + Linux file system checks, boot repair (EFI entries, GPT backup, standard MBR code)
mkpack rescue /usr/bin/mountpoint /usr/sbin/chntpw /usr/bin/ntfs-3g /usr/bin/ntfsfix /usr/sbin/cryptsetup /usr/sbin/e2fsck /usr/sbin/fsck.vfat \
  /usr/sbin/fsck.exfat /usr/sbin/xfs_repair /usr/sbin/smartctl /usr/share/smartmontools /usr/lib/syslinux/mbr \
  /usr/bin/ddrescue /usr/bin/testdisk /usr/bin/photorec /usr/sbin/mount.cifs /usr/sbin/memtester /usr/bin/sensors /etc/sensors3.conf /etc/sensors.d
mkpack imaging /usr/sbin/sfdisk /usr/sbin/partclone.ext4 /usr/sbin/partclone.extfs /usr/sbin/partclone.vfat /usr/sbin/partclone.fat /usr/sbin/partclone.ntfs /usr/sbin/partclone.xfs /usr/sbin/partclone.btrfs \
  /usr/sbin/partclone.dd /usr/sbin/partclone.restore /usr/bin/udp-sender /usr/bin/udp-receiver /usr/bin/zstd /usr/bin/pv \
  /usr/bin/wimlib-imagex /usr/sbin/mkntfs /usr/sbin/ntfsresize /usr/sbin/ntfslabel /usr/bin/ntfs-3g
# virt: manual installs - the vendor's own installer in a VM on the machine (real disk attached, ISO streamed over HTTP,
# screen in the web console via noVNC). QEMU + its HTTP disk module, UEFI (OVMF, Microsoft keys for Windows 11) and
# BIOS (SeaBIOS) firmware, KVM and TPM drivers (the PC's TPM is passed through). Loaded only for a manual install.
if [ "$ARCH" = amd64 ]; then   # virt pack is x86-only (qemu-system-x86 + OVMF/SeaBIOS); arm64 skips it for now
chroot $C /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin DEBIAN_FRONTEND=noninteractive \
  apt-get install -y -q --no-install-recommends qemu-system-x86 qemu-block-extra ovmf seabios >/dev/null
P=$B/pack-virt; rm -rf $P; mkdir -p $P
for f in /usr/bin/qemu-system-x86_64 /usr/lib/x86_64-linux-gnu/qemu/block-curl.so; do copy_bin $f $P; done
for d in /usr/share/qemu /usr/share/seabios /usr/share/OVMF; do mkdir -p "$P$(dirname $d)"; cp -a "$C$d" "$P$(dirname $d)/"; done
for md in kernel/arch/x86/kvm kernel/drivers/char/tpm; do
  mkdir -p $P/lib/modules/$KV/$md
  for m in $C/lib/modules/$KV/$md/*.ko*; do [ -e "$m" ] || continue; b=$(basename $m); xz -dc "$m" > "$P/lib/modules/$KV/$md/${b%.xz}" 2>/dev/null || cp "$m" "$P/lib/modules/$KV/$md/"; done
done
for base in $R $B/pack-hw; do
  (cd $base && find . -type f) | while read -r x; do if [ -f "$P/$x" ] && cmp -s "$P/$x" "$base/$x"; then rm -f "$P/$x"; fi; done
done
mksquashfs $P $OUT/packs/virt.sqfs.new -comp zstd -Xcompression-level 19 -b 1M -noappend -quiet -all-root && mv -f $OUT/packs/virt.sqfs.new $OUT/packs/virt.sqfs
else echo ">>> Core: virt (QEMU/OVMF) pack skipped on $ARCH"; fi
# all remaining drivers + firmware: only for machines whose hardware Core does not cover
P=$B/pack-drivers
if [ -s $OUT/packs/drivers.sqfs ] && [ "$(cat $B/drivers.kv 2>/dev/null)" = "$KV" ] && [ "${1:-}" != --packs ]; then
  echo "drivers pack unchanged (kernel $KV) - reused"
else
rm -rf $P; mkdir -p $P/lib/modules/$KV $P/lib/firmware
(cd $C/lib/modules/$KV && find kernel -name '*.ko*' | grep -vE '/(sound|gpu|media|wireless|bluetooth|staging)/') | while read -r m; do
  [ -e "$R/lib/modules/$KV/${m%.xz}" ] && continue
  mkdir -p "$P/lib/modules/$KV/$(dirname $m)"; xz -dc "$C/lib/modules/$KV/$m" > "$P/lib/modules/$KV/${m%.xz}" 2>/dev/null || true
done
mksquashfs $P $OUT/packs/drivers.sqfs.new -comp zstd -Xcompression-level 19 -b 1M -noappend -quiet -all-root && mv -f $OUT/packs/drivers.sqfs.new $OUT/packs/drivers.sqfs
echo "$KV" > $B/drivers.kv
fi

# official firmware pack: the vendor firmware the EXTENDED drivers (the 'drivers' pack) declare but Core and the hw pack do
# not bake. Fetched on demand (alongside the drivers pack) only when a device here needs firmware, so Core stays lean.
# Same module filter as the drivers pack (storage/NIC/misc); Wi-Fi/GPU firmware would additionally need those driver
# families shipped and their firmware packages installed in the chroot - a later, larger pack.
P=$B/pack-firmware
if [ -s $OUT/packs/firmware.sqfs ] && [ "$(cat $B/firmware.kv 2>/dev/null)" = "$KV" ] && [ "${1:-}" != --packs ]; then
  echo "firmware pack unchanged (kernel $KV) - reused"
else
rm -rf $P; mkdir -p $P/lib/firmware
( cd $C/lib/modules/$KV && find kernel -name '*.ko*' | grep -vE '/(sound|gpu|media|wireless|bluetooth|staging)/' ) > $B/modules.fw
install_fw $B/modules.fw $P
for base in $R $B/pack-hw; do (cd "$base" 2>/dev/null && find lib/firmware -type f 2>/dev/null) | while read -r x; do [ -f "$P/$x" ] && cmp -s "$P/$x" "$base/$x" && rm -f "$P/$x"; done; done
find $P -type d -empty -delete 2>/dev/null
if [ -n "$(find $P/lib/firmware -type f 2>/dev/null | head -1)" ]; then
  mksquashfs $P $OUT/packs/firmware.sqfs.new -comp zstd -Xcompression-level 19 -b 1M -noappend -quiet -all-root && mv -f $OUT/packs/firmware.sqfs.new $OUT/packs/firmware.sqfs
  echo "$KV" > $B/firmware.kv
  echo ">>> firmware pack: $(find $P/lib/firmware -type f | wc -l) official blobs, $(du -sh $P/lib/firmware | cut -f1) (on-demand)"
else
  rm -f $OUT/packs/firmware.sqfs $B/firmware.kv; echo ">>> firmware pack: none needed (Core + hw already carry every declared firmware)"
fi
fi

echo ">>> bake trusted pack checksums into the initrd (anchored to Secure Boot once the Core ships as a signed UKI)"
# agent.sh verifies packs against /etc/kb-sums when present, so a network attacker who tampers a pack AND the
# server's SHA256SUMS still cannot get it loaded: the baked sums ride inside the signed initrd.
mkdir -p $R/etc; ( cd $OUT && sha256sum packs/*.sqfs ) > $R/etc/kb-sums 2>/dev/null || : > $R/etc/kb-sums
echo ">>> pack initrd (cpio newc + zstd -19)"
(cd $R && find . -print0 | cpio --null -o -H newc --quiet) | zstd -19 -T0 -q -o $OUT/initrd.img.new
KSRC=$C/boot/vmlinuz-$KV; [ -f "$KSRC" ] || KSRC=$C/boot/vmlinux-$KV   # riscv64 ships an uncompressed vmlinux, not vmlinuz
cp "$KSRC" $OUT/vmlinuz.new
mv -f $OUT/initrd.img.new $OUT/initrd.img; mv -f $OUT/vmlinuz.new $OUT/vmlinuz
echo "$KV $(cat $R/etc/kb-core-version)" > $OUT/version
( cd $OUT && sha256sum vmlinuz initrd.img packs/*.sqfs > SHA256SUMS )
{
  echo "KwickBoot Core $(cat $R/etc/kb-core-version) kernel $KV"
  echo "download at boot: $(( ($(stat -c %s $OUT/vmlinuz) + $(stat -c %s $OUT/initrd.img)) / 1048576 )) MB (kernel $(du -h $OUT/vmlinuz | cut -f1) + initrd $(du -h $OUT/initrd.img | cut -f1))"
  echo "initrd contents: modules $(du -sh $R/lib/modules | cut -f1), firmware $(du -sh $R/lib/firmware | cut -f1), total $(du -sh $R | cut -f1) uncompressed"
  echo "drivers: $(wc -l < $B/modules.all), firmware files: $(find $R/lib/firmware -type f | wc -l)"
  for p in $OUT/packs/*.sqfs; do echo "pack $(basename $p .sqfs): $(du -h $p | cut -f1)"; done
} | tee $OUT/size-report.txt
chmod -R u=rwX,go=rX $OUT
for m in $C/dev/pts $C/dev $C/proc $C/sys; do umount -l "$m" 2>/dev/null || true; done; trap - EXIT
# OS library tools: Wipe and Rescue as small self-contained boot images (kernel + one initrd: Core + hw pack + the tool's
# pack + keyboard/input drivers), in http/images/tools/<name>/ next to the other OS images - network-booted from the
# console ("Start on a PC") and copied onto the USB stick / ISO by make-iso.sh. Core itself stays the slim agent RAM OS.
if [ "$ARCH" = amd64 ]; then   # the rescue/wipe tool image embeds a host-compiled framebuffer UI (x86) - skip on arm64 for now
TOOLS=$T/http/images/tools
tool_info() {   # tool_info <name> <title> <description> <role> <kernel options>
  printf 'NAME=%s\nTITLE=%s\nDESC=%s\nROLE=%s\nCMDLINE=%s\nKERNEL=%s\nBUILT=%s\n' "$1" "$2" "$3" "$4" "$5" "$KV" "$(date +%F)" > $TOOLS/$1/tool.info
}
for n in rescue; do             # ONE tool: Rescue & Wipe (both packs; the menu has Secure wipe; kb_tool=wipe starts the wipe)
  L=$B/lean-$n; rm -rf $L; mkdir -p $L/bin $TOOLS/$n
  for p in hw rescue wipe; do unsquashfs -q -f -d $L $OUT/packs/$p.sqfs >/dev/null; done
  unsquashfs -q -f -d $L $OUT/packs/drivers.sqfs 'lib/modules/*/kernel/drivers/hid' 'lib/modules/*/kernel/drivers/input' 'lib/modules/*/kernel/drivers/hv' >/dev/null 2>&1 || true
  install -m 0755 $T/linux/iso/init $L/init
  install -m 0755 $T/linux/iso/kb-tools $L/bin/kb-tools
  install -m 0755 $T/linux/iso/kb-rescue $L/bin/kb-rescue
  # branded framebuffer rescue menu (same theme as the boot menu): a small C tool + pre-rendered chrome + console font.
  # Built on the host (gcc + python3-pil + kbd fonts); the tool links against Core's glibc at runtime. Text menu if absent.
  if command -v gcc >/dev/null 2>&1 && python3 -c 'import PIL' 2>/dev/null; then
    mkdir -p $L/usr/share/kbrescue
    if gcc -O2 -o $L/bin/kbrescue-fb $T/linux/iso/kbrescue-fb.c 2>/dev/null \
       && python3 $T/linux/rescue-chrome.py $L/usr/share/kbrescue >/dev/null \
       && python3 $T/linux/psf2raw.py $L/usr/share/kbrescue/font.raw >/dev/null; then
      rm -f $L/usr/share/kbrescue/rescue-chrome.png; echo "    rescue menu: framebuffer UI (branded)"
    else echo "    rescue menu: TEXT only (build of the framebuffer UI failed)"; rm -rf $L/bin/kbrescue-fb $L/usr/share/kbrescue; fi
  else echo "    rescue menu: TEXT only (needs gcc + python3-pil + kbd console fonts on the build box)"; fi
  # one initrd file: Core's (zstd) followed by the tool's (gzip) - the kernel unpacks concatenated archives in order
  ( cat $OUT/initrd.img; ( cd $L && find . | cpio -o -H newc --quiet ) | gzip -9 ) > $TOOLS/$n/initrd.img.new
  mv -f $TOOLS/$n/initrd.img.new $TOOLS/$n/initrd.img
  cp $OUT/vmlinuz $TOOLS/$n/vmlinuz
  rm -rf $L
done
tool_info rescue "Rescue & Wipe" "Passwords (Windows / Linux), start-up repair (GRUB, Windows, MBR), copy files off, clone failing disks, recover partitions, disk checks - and secure wipe (NIST SP 800-88 Rev. 2)." admin "kb_tool=rescue quiet loglevel=3"
rm -rf $TOOLS/wipe
for n in rescue; do
  ( cd $TOOLS/$n && sha256sum vmlinuz initrd.img > SHA256SUMS )
  echo "tool $n: $(( ($(stat -c %s $TOOLS/$n/vmlinuz) + $(stat -c %s $TOOLS/$n/initrd.img)) / 1048576 )) MB (kernel + initrd)" >> $OUT/size-report.txt
done
else echo ">>> rescue/wipe tool image skipped on $ARCH"; fi
rm -f $OUT/wipe.cpio.gz $OUT/rescue.cpio.gz
rm -rf --one-file-system $R $B/pack-install $B/pack-diag $B/pack-imaging $B/pack-hw $B/pack-virt $B/pack-wipe $B/pack-rescue   # keep build env + drivers pack for fast rebuilds
