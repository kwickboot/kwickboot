#!/bin/sh
# KwickBoot: turn a debootstrapped Ubuntu 24.04 (noble) base into a Linux Mint Xfce desktop.
# SPDX-License-Identifier: AGPL-3.0-or-later
# Run from the RAM OS (install) or from linux/build-images.sh (prebuilt image):
#   HOSTNAME=lab01 TZ=.. LAN=.. KEYS="$(cat keys)" MINT_CODENAME=zena MINT_MIRROR=http://packages.linuxmint.com \
#   MINT_KEYRING=/path/linuxmint-keyring.gpg  sh mint-configure.sh /mnt/t
# PHASE=all (default) | packages | machine
#   packages: everything identical on every machine (apt repos, Xfce desktop, the 'mint' admin shell account) - baked by build-images.sh
#   machine : hostname, fstab, timezone, SSH host+authorized keys, firewall, bootloader - always run by the install
# Result: Ubuntu noble base + Mint's official repo + Xfce (LightDM), Firefox + LibreOffice, UEFI GRUB with signed shim,
#         unattended security updates, SSH key-only root + a 'mint' admin that must set its own password at first sign-in.
set -e
T=${1:-/mnt/t}
PHASE=${PHASE:-all}; [ "${KB_FROM_IMAGE:-0}" = 1 ] && PHASE=machine
TZ=${TZ:-America/Chicago}; LAN=${LAN:-192.168.0.0/24}; LOCALE=${LOCALE:-en_US.UTF-8}; KEYMAP=${KEYMAP:-us}
if [ "$PHASE" != packages ]; then
  : "${HOSTNAME:?set HOSTNAME}" "${KEYS:?set KEYS (authorized_keys content)}"
  mountpoint -q "$T" && mountpoint -q "$T/boot/efi" || { echo "target $T (and $T/boot/efi) must be mounted"; exit 1; }
fi
if [ "$PHASE" != machine ]; then
  : "${MINT_CODENAME:?}" "${MINT_MIRROR:?}" "${MINT_KEYRING:?set MINT_KEYRING}"
fi

[ -d /sys/firmware/efi/efivars ] && ! mountpoint -q /sys/firmware/efi/efivars && mount -t efivarfs efivarfs /sys/firmware/efi/efivars || true
for d in dev dev/pts proc sys run; do mkdir -p "$T/$d"; done
mount --rbind /dev "$T/dev"; mount -t proc proc "$T/proc"; mount --rbind /sys "$T/sys"; mount -t tmpfs tmpfs "$T/run"
rm -f "$T/etc/resolv.conf"; cp -L /etc/resolv.conf "$T/etc/resolv.conf"
[ "$PHASE" != packages ] || install -D -m 0644 "$MINT_KEYRING" "$T/usr/share/keyrings/linuxmint-keyring.gpg"
ROOT_UUID=""; EFI_UUID=""
if [ "$PHASE" != packages ]; then
  ROOT_UUID=$(blkid -s UUID -o value "$(findmnt -no SOURCE "$T")")
  EFI_UUID=$(blkid -s UUID -o value "$(findmnt -no SOURCE "$T/boot/efi")")
  for u in "$ROOT_UUID:$(findmnt -no SOURCE "$T")" "$EFI_UUID:$(findmnt -no SOURCE "$T/boot/efi")"; do
    [ -n "${u%%:*}" ] || continue; mkdir -p /dev/disk/by-uuid
    [ -e "/dev/disk/by-uuid/${u%%:*}" ] || ln -sf "${u#*:}" "/dev/disk/by-uuid/${u%%:*}"
  done
fi

chroot "$T" /usr/bin/env -i HOME=/root PATH=/usr/sbin:/usr/bin:/sbin:/bin TERM=xterm \
  HN="$HOSTNAME" TZN="$TZ" LANNET="$LAN" RU="$ROOT_UUID" EU="$EFI_UUID" KEYS="$KEYS" \
  MC="$MINT_CODENAME" MM="$MINT_MIRROR" KBP="$KB_PROXY" PHASE="$PHASE" LOC="$LOCALE" KMAP="$KEYMAP" /bin/bash -s <<'CHROOT'
set -e
export DEBIAN_FRONTEND=noninteractive
[ -n "$KBP" ] && echo "Acquire::http::Proxy \"$KBP\";" > /etc/apt/apt.conf.d/01kwickboot-cache

if [ "$PHASE" != machine ]; then          # ======== packages phase: identical on every machine
rm -f /etc/apt/sources.list
cat > /etc/apt/sources.list.d/ubuntu.sources <<EOF
Types: deb
URIs: http://archive.ubuntu.com/ubuntu
Suites: noble noble-updates noble-backports
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg

Types: deb
URIs: http://security.ubuntu.com/ubuntu
Suites: noble-security
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
EOF
echo "deb [signed-by=/usr/share/keyrings/linuxmint-keyring.gpg] $MM $MC main upstream import backport" > /etc/apt/sources.list.d/mint.list
cat > /etc/apt/preferences.d/no-snap-stubs <<EOF
Package: firefox* thunderbird*
Pin: release o=Ubuntu
Pin-Priority: -1
EOF
locale-gen en_US.UTF-8 "$LOC" >/dev/null 2>&1 || locale-gen "$LOC" >/dev/null 2>&1   # worldwide: requested locale + en_US fallback
echo "LANG=$LOC" > /etc/default/locale
printf 'XKBMODEL="pc105"\nXKBLAYOUT="%s"\nXKBVARIANT=""\nBACKSPACE="guess"\n' "$KMAP" > /etc/default/keyboard   # RTL follows the locale
apt-get update -q
apt-get -y -q full-upgrade >/dev/null
echo "installing kernel + bootloader"
apt-get install -y -q linux-generic shim-signed grub-efi-amd64-signed grub-efi-amd64 efibootmgr dosfstools zram-tools unattended-upgrades >/dev/null
echo "installing Mint Xfce desktop (the long step)"
if apt-get install -y -q mint-meta-xfce lightdm lightdm-gtk-greeter network-manager-gnome 2>/root/mint.err; then
  echo "mint-meta-xfce installed"
else
  echo "mint-meta-xfce failed; falling back to xfce4 core + Mint themes"; tail -n 5 /root/mint.err || true
  apt-get install -y -q xfce4 xfce4-terminal xfce4-goodies lightdm lightdm-gtk-greeter network-manager-gnome mint-themes mint-x-icons mint-y-icons >/dev/null
fi
apt-get install -y -q firefox >/dev/null 2>&1 && echo "firefox (Mint) installed" || echo "WARN: firefox not installed"
apt-get install -y -q libreoffice-writer libreoffice-calc libreoffice-impress >/dev/null 2>&1 || echo "WARN: libreoffice partial"
# Worldwide fonts: without these a CJK/Arabic/Thai/Devanagari/Hebrew locale renders as tofu boxes on the installed desktop.
# fonts-noto-core = Arabic/Thai/Devanagari/Hebrew/... ; fonts-noto-cjk = Chinese/Japanese/Korean ; + colour emoji. RTL shaping
# (Arabic/Hebrew) then follows from the locale in GTK/Qt. Baked into the image so every machine has them offline.
echo "installing worldwide fonts (Noto: CJK + Arabic/Thai/Devanagari/Hebrew + emoji)"
apt-get install -y -q fonts-noto-core fonts-noto-cjk fonts-noto-color-emoji >/dev/null 2>&1 && echo "noto fonts installed" || echo "WARN: noto fonts partial"

# admin user 'mint': sudo; NO stored password - empty + expired so it must be set at first sign-in. Its SSH key is per-machine (machine phase).
id mint >/dev/null 2>&1 || useradd -m -s /bin/bash -G sudo,adm,plugdev mint
passwd -d mint >/dev/null
chage -d 0 mint

printf 'network:\n  version: 2\n  renderer: NetworkManager\n' > /etc/netplan/01-network-manager-all.yaml
chmod 600 /etc/netplan/01-network-manager-all.yaml
printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' > /etc/apt/apt.conf.d/20auto-upgrades
printf 'ALGO=zstd\nPERCENT=50\n' > /etc/default/zramswap
printf 'PermitRootLogin prohibit-password\nPasswordAuthentication no\nKbdInteractiveAuthentication no\nX11Forwarding no\nMaxAuthTries 4\n' > /etc/ssh/sshd_config.d/10-hardening.conf
sed -i 's/^GRUB_TIMEOUT=.*/GRUB_TIMEOUT=2/; s/^GRUB_CMDLINE_LINUX_DEFAULT=.*/GRUB_CMDLINE_LINUX_DEFAULT="quiet splash"/' /etc/default/grub
passwd -l root >/dev/null
systemctl enable ssh nftables NetworkManager systemd-resolved zramswap lightdm >/dev/null 2>&1
systemctl set-default graphical.target >/dev/null 2>&1
update-initramfs -u -k all >/dev/null 2>&1
fi                                        # ======== end packages phase

if [ "$PHASE" != packages ]; then         # ======== machine phase: what makes this disk this machine
echo "$HN" > /etc/hostname
printf '127.0.0.1\tlocalhost\n127.0.1.1\t%s.lan %s\n::1\tlocalhost ip6-localhost ip6-loopback\n' "$HN" "$HN" > /etc/hosts
cat > /etc/fstab <<EOF
UUID=$RU  /          ext4  errors=remount-ro,noatime  0 1
UUID=$EU  /boot/efi  vfat  umask=0077,nofail          0 2
EOF
ln -sf "/usr/share/zoneinfo/$TZN" /etc/localtime; echo "$TZN" > /etc/timezone
ls /etc/ssh/ssh_host_*_key >/dev/null 2>&1 || ssh-keygen -A >/dev/null
[ -s /etc/machine-id ] || systemd-machine-id-setup >/dev/null 2>&1 || true
mkdir -p /root/.ssh; chmod 700 /root/.ssh
printf '%s\n' "$KEYS" > /root/.ssh/authorized_keys; chmod 600 /root/.ssh/authorized_keys
if id mint >/dev/null 2>&1; then
  install -d -m 700 -o mint -g mint /home/mint/.ssh
  printf '%s\n' "$KEYS" > /home/mint/.ssh/authorized_keys
  chmod 600 /home/mint/.ssh/authorized_keys; chown mint:mint /home/mint/.ssh/authorized_keys
fi
cat > /etc/nftables.conf <<EOF
#!/usr/sbin/nft -f
flush ruleset
table inet filter {
  chain input {
    type filter hook input priority 0; policy drop;
    ct state established,related accept
    ct state invalid drop
    iif lo accept
    meta l4proto { icmp, ipv6-icmp } accept
    udp sport 67 udp dport 68 accept
    udp sport 547 udp dport 546 accept
    ip saddr $LANNET tcp dport 22 accept
  }
  chain forward { type filter hook forward priority 0; policy drop; }
  chain output  { type filter hook output priority 0; policy accept; }
}
EOF
if ls /sys/firmware/efi/efivars >/dev/null 2>&1; then
  grub-install --target=x86_64-efi --efi-directory=/boot/efi --bootloader-id=ubuntu --uefi-secure-boot >/dev/null 2>&1 || echo "NVRAM grub-install failed (fallback still installed)"
fi
grub-install --target=x86_64-efi --efi-directory=/boot/efi --bootloader-id=ubuntu --uefi-secure-boot --removable --no-nvram >/dev/null
update-grub 2>&1 | grep -E 'Found linux|done'
fi                                        # ======== end machine phase

apt-get clean
echo "CONFIGURE DONE: phase=$PHASE Mint Xfce, $(dpkg -l | grep -c '^ii') packages, kernel $(ls /boot/vmlinuz-* | xargs -n1 basename | tail -n1)"
rm -f /etc/apt/apt.conf.d/01kwickboot-cache
CHROOT

ln -sf ../run/systemd/resolve/stub-resolv.conf "$T/etc/resolv.conf"
[ "$PHASE" = packages ] || ls "$T/boot/efi/EFI"
