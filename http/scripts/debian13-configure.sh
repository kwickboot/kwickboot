#!/bin/sh
# PXE toolkit: configure a freshly debootstrapped Debian 13 (trixie) target.
# Run from the in-RAM Alpine after:  debootstrap ... trixie /mnt/t
#
# Usage:  HOSTNAME=pc01 TZ=America/Chicago LAN=192.168.0.0/24 \
#         KEYS="$(cat authorized_keys)" sh debian13-configure.sh /mnt/t
# Result: UEFI GRUB (NVRAM entry + removable fallback), systemd-networkd DHCP,
#         SSH key-only root login, nftables (SSH from LAN only), zram swap,
#         root password locked.
set -e
T=${1:-/mnt/t}
: "${HOSTNAME:?set HOSTNAME}" "${KEYS:?set KEYS (authorized_keys content)}"
TZ=${TZ:-America/Chicago}
LAN=${LAN:-192.168.0.0/24}
LOCALE=${LOCALE:-en_US.UTF-8}
KEYMAP=${KEYMAP:-us}
# per-arch EFI GRUB: target, packages, Secure-Boot flag. amd64/arm64 use MS-signed shim + Debian-signed GRUB (SB ON);
# i386/32-bit UEFI uses unsigned grub-efi-ia32 (Microsoft does not sign 32-bit UEFI, SB is uncommon there).
case "${ARCH:-amd64}" in
  amd64) GT=x86_64-efi; GPKG="shim-signed grub-efi-amd64-signed"; SB=--uefi-secure-boot; BOOTEFI=BOOTX64.EFI ;;
  arm64) GT=arm64-efi;  GPKG="shim-signed grub-efi-arm64-signed"; SB=--uefi-secure-boot; BOOTEFI=BOOTAA64.EFI ;;
  i386)  GT=i386-efi;   GPKG="grub-efi-ia32";                     SB=;                    BOOTEFI=BOOTIA32.EFI ;;
  *)     GT=x86_64-efi; GPKG="shim-signed grub-efi-amd64-signed"; SB=--uefi-secure-boot; BOOTEFI=BOOTX64.EFI ;;
esac
[ "${BOOT_MODE:-uefi}" = "bios" ] && { GT=i386-pc; GPKG="grub-pc"; SB=; BOOTEFI=; }   # legacy BIOS: MBR/core.img GRUB

mountpoint -q "$T" || { echo "target $T not mounted"; exit 1; }
[ "${BOOT_MODE:-uefi}" = "bios" ] || mountpoint -q "$T/boot/efi" || { echo "$T/boot/efi not mounted"; exit 1; }

# --- bind mounts for chroot
[ -d /sys/firmware/efi/efivars ] && ! mountpoint -q /sys/firmware/efi/efivars && mount -t efivarfs efivarfs /sys/firmware/efi/efivars || true
for d in dev dev/pts proc sys run; do mkdir -p "$T/$d"; done
mount --rbind /dev "$T/dev"; mount -t proc proc "$T/proc"; mount --rbind /sys "$T/sys"; mount -t tmpfs tmpfs "$T/run"
rm -f "$T/etc/resolv.conf"            # systemd-resolved leaves a symlink into /run (dangling in chroot)
cp -L /etc/resolv.conf "$T/etc/resolv.conf"

ROOT_UUID=$(blkid -s UUID -o value "$(findmnt -no SOURCE "$T")")
EFI_UUID=""; [ "${BOOT_MODE:-uefi}" = "bios" ] || EFI_UUID=$(blkid -s UUID -o value "$(findmnt -no SOURCE "$T/boot/efi")")
# GRUB names the root by UUID only when /dev/disk/by-uuid has it - without udev (mdev RAM OS, installer ISO) it would
# fall back to /dev/sdX, which changes when a USB stick is removed. Make the links (the chroot sees /dev via rbind).
for u in "$ROOT_UUID:$(findmnt -no SOURCE "$T")" "$EFI_UUID:$(findmnt -no SOURCE "$T/boot/efi")"; do
  [ -n "${u%%:*}" ] || continue; mkdir -p /dev/disk/by-uuid
  [ -e "/dev/disk/by-uuid/${u%%:*}" ] || ln -sf "${u#*:}" "/dev/disk/by-uuid/${u%%:*}"
done

chroot "$T" /usr/bin/env -i HOME=/root PATH=/usr/sbin:/usr/bin:/sbin:/bin TERM=xterm \
  HN="$HOSTNAME" TZN="$TZ" LANNET="$LAN" RU="$ROOT_UUID" EU="$EFI_UUID" BM="${BOOT_MODE:-uefi}" INSTDISK="$DISK" GT="$GT" GPKG="$GPKG" SB="$SB" KEYS="$KEYS" KBP="$KB_PROXY" IMG="${KB_FROM_IMAGE:-0}" XPKGS="$EXTRA_PKGS" LOC="$LOCALE" KMAP="$KEYMAP" /bin/bash -s <<'CHROOT'
set -e
export DEBIAN_FRONTEND=noninteractive
echo "$HN" > /etc/hostname
printf '127.0.0.1\tlocalhost\n127.0.1.1\t%s.lan %s\n::1\tlocalhost ip6-localhost ip6-loopback\n' "$HN" "$HN" > /etc/hosts

rm -f /etc/apt/sources.list
cat > /etc/apt/sources.list.d/debian.sources <<EOF
Types: deb
URIs: http://deb.debian.org/debian
Suites: trixie trixie-updates
Components: main non-free-firmware
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg

Types: deb
URIs: http://security.debian.org/debian-security
Suites: trixie-security
Components: main non-free-firmware
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
EOF

{ echo "UUID=$RU  /          ext4  errors=remount-ro,noatime  0 1"
  [ "$BM" = bios ] || echo "UUID=$EU  /boot/efi  vfat  umask=0077,nofail          0 2"
  echo "tmpfs     /tmp       tmpfs defaults,noatime,size=512m 0 0"; } > /etc/fstab

ln -sf "/usr/share/zoneinfo/$TZN" /etc/localtime; echo "$TZN" > /etc/timezone
# locale (worldwide): enable the requested locale + keep en_US as a safe fallback, then set it as the default
for L in en_US.UTF-8 "$LOC"; do
  sed -i "s/^# *$L UTF-8/$L UTF-8/" /etc/locale.gen
  grep -q "^$L UTF-8" /etc/locale.gen || echo "$L UTF-8" >> /etc/locale.gen
done
locale-gen >/dev/null
echo "LANG=$LOC" > /etc/default/locale
# keyboard layout (console-setup + X11/Wayland); Arabic/Hebrew RTL follows from the locale in GTK/Qt
printf 'XKBMODEL="pc105"\nXKBLAYOUT="%s"\nXKBVARIANT=""\nBACKSPACE="guess"\n' "$KMAP" > /etc/default/keyboard

if [ "$IMG" = 1 ]; then
  # prebuilt image: packages, locale and updates are already in it; only add this machine's extra firmware/microcode
  ssh-keygen -A >/dev/null 2>&1        # this machine's own SSH host keys (the image deliberately contains none)
  missing=""; for p in $XPKGS; do dpkg -s "$p" >/dev/null 2>&1 || missing="$missing $p"; done
  if [ -n "$missing" ]; then
    [ -n "$KBP" ] && echo "Acquire::http::Proxy \"$KBP\";" > /etc/apt/apt.conf.d/01kwickboot-cache
    if apt-get update -q >/dev/null 2>&1 && apt-get install -y -q $missing >/dev/null 2>&1; then echo "added for this machine:$missing"
    else echo "NOTE  could not add$missing (no network or mirror) - install later with: apt install$missing"; fi
  fi
else
[ -n "$KBP" ] && echo "Acquire::http::Proxy \"$KBP\";" > /etc/apt/apt.conf.d/01kwickboot-cache
  apt-get update -q
  apt-get install -y -q dosfstools >/dev/null      # fsck.vfat for /boot/efi (missing = emergency mode!)
  # Secure Boot (amd64/arm64): Microsoft-signed shim + Debian-signed GRUB; i386 installs unsigned grub-efi-ia32
  apt-get install -y -q $GPKG >/dev/null
  apt-get -y -q full-upgrade >/dev/null
fi

# network: DHCP on any wired NIC, client-id = MAC so router reservations match
mkdir -p /etc/systemd/network
cat > /etc/systemd/network/20-wired.network <<EOF
[Match]
Name=en* eth*

[Network]
DHCP=yes

[DHCPv4]
ClientIdentifier=mac
EOF

# SSH: key-only
mkdir -p /root/.ssh; chmod 700 /root/.ssh
printf '%s\n' "$KEYS" > /root/.ssh/authorized_keys; chmod 600 /root/.ssh/authorized_keys
printf 'PermitRootLogin prohibit-password\nPasswordAuthentication no\nKbdInteractiveAuthentication no\nX11Forwarding no\nMaxAuthTries 4\n' \
  > /etc/ssh/sshd_config.d/10-hardening.conf
passwd -l root >/dev/null

# firewall: SSH from LAN only
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
    ip saddr $LANNET tcp dport 22 accept
$(p=$(tr -s ' \n' ',' < /etc/kwickboot/lan-ports 2>/dev/null | sed 's/^,//; s/,$//'); [ -n "$p" ] && echo "    ip saddr $LANNET tcp dport { $p } accept   # appliance (image /etc/kwickboot/lan-ports)")
  }
  chain forward { type filter hook forward priority 0; policy $([ -f /etc/kwickboot/own-network ] && echo accept || echo drop); }   # hypervisors: VM traffic passes
  chain output  { type filter hook output priority 0; policy accept; }
}
EOF

printf 'ALGO=zstd\nPERCENT=50\n' > /etc/default/zramswap

# bootloader: NVRAM entry + removable fallback path EFI/BOOT/BOOTX64.EFI
sed -i 's/^GRUB_TIMEOUT=.*/GRUB_TIMEOUT=2/; s/^GRUB_CMDLINE_LINUX_DEFAULT=.*/GRUB_CMDLINE_LINUX_DEFAULT="quiet"/' /etc/default/grub
if [ "$BM" = bios ]; then
  grub-install --target=i386-pc --recheck "$INSTDISK" >/dev/null     # boot code -> MBR, core.img -> the EF02 BIOS-boot partition
else
  if [ -d /sys/firmware/efi/efivars ] && ls /sys/firmware/efi/efivars >/dev/null 2>&1; then
    grub-install --target=$GT --efi-directory=/boot/efi --bootloader-id=debian $SB >/dev/null 2>&1 || echo "NVRAM grub-install failed (fallback still installed)"
  fi
  grub-install --target=$GT --efi-directory=/boot/efi --bootloader-id=debian $SB --removable --no-nvram >/dev/null
fi
[ "$IMG" = 1 ] || update-initramfs -u -k all >/dev/null 2>&1   # image: generic initramfs already built
update-grub 2>&1 | grep -E 'Found linux|done'
# the root must be named by UUID (a /dev/sdX name changes when the install stick is removed or disks are added)
if grep -qE 'root=/dev/[a-z]' /boot/grub/grub.cfg; then
  sed -i -E "s#root=/dev/[a-z0-9]+#root=UUID=$RU#g" /boot/grub/grub.cfg; echo "grub: root set to UUID=$RU"
fi
grep -q "root=UUID=$RU" /boot/grub/grub.cfg || { echo "grub.cfg has no root=UUID=$RU"; exit 1; }

if [ -f /etc/kwickboot/own-network ]; then   # appliance brings its own network stack (Proxmox: ifupdown2 + bridge vmbr0)
  systemctl enable ssh nftables zramswap >/dev/null 2>&1; systemctl disable systemd-networkd >/dev/null 2>&1 || true
else
  systemctl enable ssh nftables systemd-networkd systemd-resolved zramswap >/dev/null 2>&1
fi
apt-get clean
echo "CONFIGURE DONE: $(grep -c '^menuentry' /boot/grub/grub.cfg) grub entries, kernel $(ls /boot/vmlinuz-* | xargs -n1 basename)"
rm -f /etc/apt/apt.conf.d/01kwickboot-cache   # installed machines update directly, not via this server
CHROOT

ln -sf ../run/systemd/resolve/stub-resolv.conf "$T/etc/resolv.conf"
[ "${BOOT_MODE:-uefi}" = bios ] || ls "$T/boot/efi/EFI"
[ "${BOOT_MODE:-uefi}" = bios ] || { command -v efibootmgr >/dev/null && efibootmgr 2>/dev/null | grep -iE 'BootOrder|debian' || true; }
