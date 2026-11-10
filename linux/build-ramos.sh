#!/bin/bash
# Build the KwickBoot RAM OS (Secure Boot capable): Debian 13 with Debian's SIGNED kernel, loaded into RAM over HTTP.
# SPDX-License-Identifier: AGPL-3.0-or-later
#   linux/build-ramos.sh            -> /opt/pxe-toolkit/http/kbos/{vmlinuz,initrd.img,filesystem.squashfs,version}
# Boot chain under Secure Boot: firmware (Microsoft key) -> Debian shim -> Debian GRUB -> Debian-signed kernel (+ signed modules).
# The root filesystem is fetched by Debian's live-boot (fetch=http://...) and runs from RAM; disks are never mounted.
# Everything the agent needs is pre-installed: no internet needed at boot.
set -euo pipefail
T=/opt/pxe-toolkit
OUT=$T/http/kbos
B=/var/tmp/kbos-build
MIRROR=http://deb.debian.org/debian
PROXY=""; curl -s -m 3 -o /dev/null http://127.0.0.1:3142/acng-report.html && PROXY=http://127.0.0.1:3142
command -v debootstrap >/dev/null && command -v mksquashfs >/dev/null || apt-get install -y -q debootstrap squashfs-tools >/dev/null
umount -l $B/sys $B/proc $B/dev 2>/dev/null || true; rm -rf --one-file-system $B; mkdir -p $B $OUT

echo ">>> debootstrap trixie (cache: ${PROXY:-none})"
env ${PROXY:+http_proxy=$PROXY} debootstrap --variant=minbase --components=main,non-free-firmware \
  --include=systemd-sysv,udev,dbus,ca-certificates trixie $B $MIRROR > /var/tmp/kbos-debootstrap.log

mount --bind /dev $B/dev; mount -t proc proc $B/proc; mount -t sysfs sys $B/sys
trap 'umount -l $B/sys $B/proc $B/dev 2>/dev/null || true' EXIT
cp /etc/resolv.conf $B/etc/resolv.conf
chroot $B /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root DEBIAN_FRONTEND=noninteractive KBP="$PROXY" /bin/bash -s <<'CHROOT'
set -e
[ -n "$KBP" ] && echo "Acquire::http::Proxy \"$KBP\";" > /etc/apt/apt.conf.d/01cache
# slim: no docs, man pages, info or translations (licence files are kept)
cat > /etc/dpkg/dpkg.cfg.d/01kwickboot-slim <<EOF
path-exclude=/usr/share/doc/*
path-include=/usr/share/doc/*/copyright
path-exclude=/usr/share/man/*
path-exclude=/usr/share/info/*
path-exclude=/usr/share/locale/*
path-include=/usr/share/locale/locale.alias
path-exclude=/usr/share/lintian/*
path-exclude=/usr/share/bash-completion/*
EOF
find /usr/share/doc -type f ! -name copyright -delete 2>/dev/null; rm -rf /usr/share/man/* /usr/share/info/* 2>/dev/null
find /usr/share/locale -mindepth 1 -maxdepth 1 ! -name locale.alias -exec rm -rf {} + 2>/dev/null
cat > /etc/apt/sources.list <<EOF
deb http://deb.debian.org/debian trixie main contrib non-free-firmware
deb http://deb.debian.org/debian trixie-updates main contrib non-free-firmware
deb http://security.debian.org/debian-security trixie-security main contrib non-free-firmware
EOF
apt-get update -q
echo ">>> kernel (Debian-signed) + live-boot + tools"
apt-get install -y -q --no-install-recommends \
  linux-image-amd64 live-boot live-boot-initramfs-tools initramfs-tools \
  firmware-linux-free firmware-realtek firmware-misc-nonfree firmware-iwlwifi firmware-bnx2 firmware-bnx2x firmware-intel-misc firmware-amd-graphics \
  systemd-resolved openssh-server curl wget ca-certificates gnupg iproute2 iputils-ping ethtool procps less nano tmux kmod \
  util-linux fdisk gdisk parted dosfstools e2fsprogs xfsprogs btrfs-progs exfatprogs ntfs-3g mdadm lvm2 cryptsetup dmsetup \
  smartmontools nvme-cli hdparm sg3-utils lshw pciutils usbutils dmidecode lm-sensors ipmitool efibootmgr mokutil \
  debootstrap zstd xz-utils gzip pv rsync partclone udpcast memtester \
  >/dev/null
echo ">>> configure"
echo kwickboot > /etc/hostname
printf '127.0.0.1\tlocalhost\n127.0.1.1\tkwickboot\n' > /etc/hosts
usermod -p '*' root                          # no password, not locked: key-only SSH works
mkdir -p /etc/systemd/network
printf '[Match]\nName=en* eth*\n\n[Network]\nDHCP=yes\n\n[DHCPv4]\nClientIdentifier=mac\n' > /etc/systemd/network/20-wired.network
systemctl enable systemd-networkd systemd-resolved ssh >/dev/null 2>&1
printf 'PermitRootLogin prohibit-password\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n' > /etc/ssh/sshd_config.d/10-kwickboot.conf
rm -f /etc/ssh/ssh_host_*                    # generated per boot (see kb-hostkeys) - every machine gets its own keys
# initramfs: network drivers + firmware for live-boot's HTTP fetch
sed -i 's/^MODULES=.*/MODULES=netboot/; s/^COMPRESS=.*/COMPRESS=zstd/' /etc/initramfs-tools/initramfs.conf
grep -q '^COMPRESS=' /etc/initramfs-tools/initramfs.conf || echo COMPRESS=zstd >> /etc/initramfs-tools/initramfs.conf
update-initramfs -u -k all >/dev/null 2>&1
apt-get clean; rm -f /etc/apt/apt.conf.d/01cache; rm -rf /var/lib/apt/lists/* /var/cache/apt/*.bin /var/cache/debconf/*-old \
  /var/log/*.log /var/log/apt/* /usr/share/doc/*/changelog* /usr/share/common-licenses 2>/dev/null || true
echo "chroot done"
CHROOT

echo ">>> KwickBoot units, console, branding"
install -d $B/usr/local/bin $B/root/tk
cat > $B/usr/local/bin/kb-boot <<'EOF'
#!/bin/sh
# KwickBoot RAM OS start: server address + agent token come from the kernel command line
for w in $(cat /proc/cmdline); do case "$w" in kb_server=*) S=${w#kb_server=} ;; esac; done
echo "$S" > /etc/kb-server
mkdir -p /root/.ssh; chmod 700 /root/.ssh
for i in $(seq 1 30); do curl -sf -m 5 -o /root/.ssh/authorized_keys "http://$S/authorized_keys" && break; sleep 2; done
chmod 600 /root/.ssh/authorized_keys 2>/dev/null || true
for i in $(seq 1 30); do curl -sf -m 10 -o /tmp/agent.sh "http://$S/scripts/agent.sh" && break; sleep 2; done
exec sh /tmp/agent.sh > /var/log/pxe-agent.log 2>&1
EOF
chmod 755 $B/usr/local/bin/kb-boot
cat > $B/etc/systemd/system/kb-hostkeys.service <<'EOF'
[Unit]
Description=KwickBoot: per-boot SSH host keys
Before=ssh.service
[Service]
Type=oneshot
ExecStart=/usr/bin/ssh-keygen -A
[Install]
WantedBy=multi-user.target
EOF
cat > $B/etc/systemd/system/kb-agent.service <<'EOF'
[Unit]
Description=KwickBoot RAM OS agent
Wants=network-online.target
After=network-online.target systemd-networkd-wait-online.service
[Service]
ExecStart=/usr/local/bin/kb-boot
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF
python3 - "$B" <<'PY'
import sys
sys.path.insert(0, "/opt/pxe-toolkit")
import ramos                                   # same console + branding as the Alpine RAM OS
b = sys.argv[1]
open(b + "/usr/local/bin/kb-console", "w").write(ramos.CONSOLE)
open(b + "/etc/issue", "w").write(ramos.ISSUE)
open(b + "/etc/motd", "w").write(ramos.MOTD)
PY
chmod 755 $B/usr/local/bin/kb-console
mkdir -p $B/etc/systemd/system/getty@tty1.service.d
printf '[Service]\nExecStart=\nExecStart=-/usr/local/bin/kb-console\nStandardInput=tty\nStandardOutput=tty\nTTYPath=/dev/tty1\nType=simple\nRestart=always\n' \
  > $B/etc/systemd/system/getty@tty1.service.d/kwickboot.conf
chroot $B systemctl enable kb-hostkeys kb-agent >/dev/null 2>&1
rm -f $B/etc/resolv.conf; ln -s ../run/systemd/resolve/stub-resolv.conf $B/etc/resolv.conf
date +%Y%m%d-%H%M > $B/etc/kwickboot-ramos-version

echo ">>> pack"
umount -l $B/sys $B/proc $B/dev 2>/dev/null || true; trap - EXIT
K=$(ls $B/boot/vmlinuz-* | sort -V | tail -n1); I=$B/boot/initrd.img-${K##*/vmlinuz-}
cp $K $OUT/vmlinuz.new; cp $I $OUT/initrd.img.new
# zstd level 19 + 1 MB blocks: smallest download; zstd decompression stays fast on old CPUs
mksquashfs $B $OUT/filesystem.squashfs.new -comp zstd -Xcompression-level 19 -b 1M -noappend -quiet -e boot -e var/cache/apt -e var/lib/apt/lists
for f in vmlinuz initrd.img filesystem.squashfs; do mv -f $OUT/$f.new $OUT/$f; done
cat $B/etc/kwickboot-ramos-version > $OUT/version
( cd $OUT && sha256sum vmlinuz initrd.img filesystem.squashfs > SHA256SUMS )
chmod -R u=rwX,go=rX $OUT
rm -rf --one-file-system $B
ls -la $OUT
echo "KwickBoot RAM OS built: kernel ${K##*/vmlinuz-}, $(du -h $OUT/filesystem.squashfs | cut -f1) root filesystem"
