#!/bin/sh
# KwickBoot: turn a freshly debootstrapped Ubuntu 24.04 (noble) into a GNOME desktop or a server.
# SPDX-License-Identifier: AGPL-3.0-or-later
# Run from the RAM OS:  HOSTNAME=lab01 TZ=.. LAN=.. BUNDLE=office|creative|stem|basic PROFILE=desktop|server KEYS="$(cat keys)" sh ubuntu-configure.sh /mnt/t
# Result: ubuntu-desktop-minimal + bundle apps, Firefox .deb from Mozilla (not the snap stub), NetworkManager,
#         UEFI GRUB with signed shim (Secure Boot), unattended security upgrades, SSH key-only root for IT,
#         nftables (SSH from LAN only), zram swap, NO pre-made user: GDM's first-boot setup creates the account.
# PHASE=all (default) | packages | machine
#   packages: everything identical on every machine (apt, desktop, settings) - what linux/build-images.sh bakes into an image
#   machine : hostname, fstab, timezone, SSH keys + host keys, firewall LAN, bootloader - all an image install still runs
set -e
T=${1:-/mnt/t}
PHASE=${PHASE:-all}; [ "${KB_FROM_IMAGE:-0}" = 1 ] && PHASE=machine
TZ=${TZ:-America/Chicago}; LAN=${LAN:-192.168.0.0/24}; BUNDLE=${BUNDLE:-office}; PROFILE=${PROFILE:-desktop}; LOCALE=${LOCALE:-en_US.UTF-8}; KEYMAP=${KEYMAP:-us}
if [ "$PHASE" != packages ]; then
  : "${HOSTNAME:?set HOSTNAME}" "${KEYS:?set KEYS (authorized_keys content)}"
  mountpoint -q "$T" && mountpoint -q "$T/boot/efi" || { echo "target $T (and $T/boot/efi) must be mounted"; exit 1; }
fi

[ -d /sys/firmware/efi/efivars ] && ! mountpoint -q /sys/firmware/efi/efivars && mount -t efivarfs efivarfs /sys/firmware/efi/efivars || true
for d in dev dev/pts proc sys run; do mkdir -p "$T/$d"; done
mount --rbind /dev "$T/dev"; mount -t proc proc "$T/proc"; mount --rbind /sys "$T/sys"; mount -t tmpfs tmpfs "$T/run"
rm -f "$T/etc/resolv.conf"; cp -L /etc/resolv.conf "$T/etc/resolv.conf"
ROOT_UUID=""; EFI_UUID=""
if [ "$PHASE" != packages ]; then
  ROOT_UUID=$(blkid -s UUID -o value "$(findmnt -no SOURCE "$T")")
  EFI_UUID=$(blkid -s UUID -o value "$(findmnt -no SOURCE "$T/boot/efi")")
  # GRUB names the root by UUID only when /dev/disk/by-uuid has it (no udev in the RAM OS -> /dev/sdX, which can change)
  for u in "$ROOT_UUID:$(findmnt -no SOURCE "$T")" "$EFI_UUID:$(findmnt -no SOURCE "$T/boot/efi")"; do
    [ -n "${u%%:*}" ] || continue; mkdir -p /dev/disk/by-uuid
    [ -e "/dev/disk/by-uuid/${u%%:*}" ] || ln -sf "${u#*:}" "/dev/disk/by-uuid/${u%%:*}"
  done
fi

chroot "$T" /usr/bin/env -i HOME=/root PATH=/usr/sbin:/usr/bin:/sbin:/bin TERM=xterm \
  HN="$HOSTNAME" TZN="$TZ" LANNET="$LAN" RU="$ROOT_UUID" EU="$EFI_UUID" KEYS="$KEYS" BUNDLE="$BUNDLE" PROFILE="$PROFILE" \
  KBP="$KB_PROXY" PHASE="$PHASE" LOC="$LOCALE" KMAP="$KEYMAP" /bin/bash -s <<'CHROOT'
set -e
export DEBIAN_FRONTEND=noninteractive
[ -n "$KBP" ] && echo "Acquire::http::Proxy \"$KBP\";" > /etc/apt/apt.conf.d/01kwickboot-cache

if [ "$PHASE" != machine ]; then          # ======== packages phase: the same on every machine
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
# Ubuntu's firefox/thunderbird debs are stubs that install snaps (cannot work in a chroot) -> never use them
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
apt-get install -y -q linux-generic shim-signed grub-efi-amd64-signed grub-efi-amd64 efibootmgr dosfstools zram-tools \
  unattended-upgrades >/dev/null
if [ "$PROFILE" = desktop ]; then
echo "installing desktop (ubuntu-desktop-minimal) - this is the long step"
apt-get install -y -q ubuntu-desktop-minimal gnome-initial-setup language-pack-en language-pack-gnome-en flatpak >/dev/null
# Worldwide fonts: a CJK/Arabic/Thai/Devanagari/Hebrew locale renders as tofu without these. fonts-noto-core = Arabic/Thai/
# Devanagari/Hebrew/... ; fonts-noto-cjk = CJK ; + colour emoji. RTL shaping follows from the locale. Baked into the image.
echo "installing worldwide fonts (Noto: CJK + Arabic/Thai/Devanagari/Hebrew + emoji)"
apt-get install -y -q fonts-noto-core fonts-noto-cjk fonts-noto-color-emoji >/dev/null 2>&1 && echo "noto fonts installed" || echo "WARN: noto fonts partial"

case "$BUNDLE" in
  office|creative|stem) APPS="libreoffice-writer libreoffice-calc libreoffice-impress libreoffice-gnome libreoffice-style-yaru hunspell-en-us evince gnome-calculator file-roller" ;;
  *) APPS="" ;;
esac
[ "$BUNDLE" = creative ] && APPS="$APPS krita gimp inkscape blender"
[ "$BUNDLE" = stem ] && APPS="$APPS python3-full idle geany git"
if [ -n "$APPS" ]; then echo "bundle '$BUNDLE': $APPS"; apt-get install -y -q $APPS >/dev/null; fi

# Firefox as a normal package from Mozilla (pinned key; non-fatal so a Mozilla outage cannot fail the install)
MOZ_FPR=35BAA0B33E9EB396F59CA838C0BA5CE6DC6315A3
install -d -m 0755 /etc/apt/keyrings
if wget -qO /etc/apt/keyrings/packages.mozilla.org.asc https://packages.mozilla.org/apt/repo-signing-key.gpg \
   && gpg -n -q --import --import-options import-show /etc/apt/keyrings/packages.mozilla.org.asc 2>/dev/null | tr -d ' ' | grep -q "$MOZ_FPR"; then
  echo "deb [signed-by=/etc/apt/keyrings/packages.mozilla.org.asc] https://packages.mozilla.org/apt mozilla main" > /etc/apt/sources.list.d/mozilla.list
  printf 'Package: *\nPin: origin packages.mozilla.org\nPin-Priority: 1000\n' > /etc/apt/preferences.d/mozilla
  apt-get update -q >/dev/null && apt-get install -y -q firefox >/dev/null && echo "firefox (Mozilla .deb) installed" || echo "WARN: firefox install failed"
else
  rm -f /etc/apt/keyrings/packages.mozilla.org.asc; echo "WARN: Mozilla key missing or fingerprint mismatch - no browser installed"
fi

# network: NetworkManager for every interface (desktop users manage Wi-Fi themselves)
printf 'network:\n  version: 2\n  renderer: NetworkManager\n' > /etc/netplan/01-network-manager-all.yaml
chmod 600 /etc/netplan/01-network-manager-all.yaml

# first boot: no users exist -> GDM runs gnome-initial-setup, the person at the machine creates the (admin) account
grep -q '^\[daemon\]' /etc/gdm3/custom.conf || echo '[daemon]' >> /etc/gdm3/custom.conf
sed -i '/^InitialSetupEnable=/d; /^\[daemon\]/a InitialSetupEnable=true' /etc/gdm3/custom.conf
else
  # server: systemd-networkd via netplan, DHCP on every wired port, client-id = MAC (router reservations match)
  apt-get install -y -q openssh-server systemd-resolved curl ca-certificates smartmontools lm-sensors tmux >/dev/null
  printf 'network:\n  version: 2\n  renderer: networkd\n  ethernets:\n    wired:\n      match: {name: "en*"}\n      dhcp4: true\n      dhcp-identifier: mac\n' > /etc/netplan/01-kwickboot.yaml
  chmod 600 /etc/netplan/01-kwickboot.yaml
fi

# automatic security updates, zram, SSH hardening, GRUB settings, root password locked
printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' > /etc/apt/apt.conf.d/20auto-upgrades
printf 'ALGO=zstd\nPERCENT=50\n' > /etc/default/zramswap
printf 'PermitRootLogin prohibit-password\nPasswordAuthentication no\nKbdInteractiveAuthentication no\nX11Forwarding no\nMaxAuthTries 4\n' \
  > /etc/ssh/sshd_config.d/10-hardening.conf
sed -i 's/^GRUB_TIMEOUT=.*/GRUB_TIMEOUT=2/; s/^GRUB_CMDLINE_LINUX_DEFAULT=.*/GRUB_CMDLINE_LINUX_DEFAULT="quiet splash"/' /etc/default/grub
passwd -l root >/dev/null
if [ "$PROFILE" = desktop ]; then
  systemctl enable ssh nftables NetworkManager systemd-resolved zramswap gdm3 >/dev/null 2>&1
  systemctl set-default graphical.target >/dev/null 2>&1
else
  systemctl enable ssh nftables systemd-networkd systemd-resolved zramswap >/dev/null 2>&1
  systemctl set-default multi-user.target >/dev/null 2>&1
fi
update-initramfs -u -k all >/dev/null 2>&1      # generic initramfs (MODULES=most): boots on any hardware
fi                                        # ======== end packages phase

if [ "$PHASE" != packages ]; then         # ======== machine phase: what makes this disk this machine
echo "$HN" > /etc/hostname
printf '127.0.0.1\tlocalhost\n127.0.1.1\t%s.lan %s\n::1\tlocalhost ip6-localhost ip6-loopback\n' "$HN" "$HN" > /etc/hosts
cat > /etc/fstab <<EOF
UUID=$RU  /          ext4  errors=remount-ro,noatime  0 1
UUID=$EU  /boot/efi  vfat  umask=0077,nofail          0 2
EOF
ln -sf "/usr/share/zoneinfo/$TZN" /etc/localtime; echo "$TZN" > /etc/timezone
ls /etc/ssh/ssh_host_*_key >/dev/null 2>&1 || ssh-keygen -A >/dev/null        # images carry no host keys: new per machine
[ -s /etc/machine-id ] || systemd-machine-id-setup >/dev/null 2>&1 || true

# IT access: SSH key-only root (password locked in the packages phase)
mkdir -p /root/.ssh; chmod 700 /root/.ssh
printf '%s\n' "$KEYS" > /root/.ssh/authorized_keys; chmod 600 /root/.ssh/authorized_keys

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

# bootloader: signed shim + GRUB (Secure Boot), NVRAM entry + removable fallback EFI/BOOT/BOOTX64.EFI
if ls /sys/firmware/efi/efivars >/dev/null 2>&1; then
  grub-install --target=x86_64-efi --efi-directory=/boot/efi --bootloader-id=ubuntu --uefi-secure-boot >/dev/null 2>&1 || echo "NVRAM grub-install failed (fallback still installed)"
fi
grub-install --target=x86_64-efi --efi-directory=/boot/efi --bootloader-id=ubuntu --uefi-secure-boot --removable --no-nvram >/dev/null
update-grub 2>&1 | grep -E 'Found linux|done'
fi                                        # ======== end machine phase

apt-get clean
echo "CONFIGURE DONE: phase=$PHASE profile=$PROFILE bundle=$BUNDLE, $(dpkg -l | grep -c '^ii') packages, kernel $(ls /boot/vmlinuz-* | xargs -n1 basename | tail -n1)"
rm -f /etc/apt/apt.conf.d/01kwickboot-cache   # installed machines update directly, not via this server
CHROOT

ln -sf ../run/systemd/resolve/stub-resolv.conf "$T/etc/resolv.conf"
[ "$PHASE" = packages ] || ls "$T/boot/efi/EFI"
