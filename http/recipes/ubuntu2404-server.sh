# recipe: Ubuntu 24.04 LTS server via debootstrap - sourced by install.sh
# SPDX-License-Identifier: AGPL-3.0-or-later
RECIPE_NAME="Ubuntu 24.04 LTS Server"
RECIPE_DESC="Long-term-support server: SSH, firewall, automatic security updates. Five years of free security updates."
RECIPE_NOTE="DHCP on wired ports (systemd-networkd), SSH with your key only, firewall allows SSH from your LAN only, no snaps. UEFI machines."
RECIPE_MIN_GB=6
RECIPE_METHOD=rootfs
RECIPE_EFI_LABEL=ubuntu
SUITE=noble
MIRROR=http://archive.ubuntu.com/ubuntu
# Ubuntu Archive Automatic Signing Key (2018) - signs noble's Release files. Install STOPS if the fetched key differs.
UBUNTU_KEY_FPR=F6ECB3762474EDA9D21B7022871920D1991BC93C

recipe_requirements() {
  [ "$(( $(lsblk -bdno SIZE "$DISK") / 1000000000 ))" -ge 6 ] || { echo "need >= 6 GB"; return 1; }
  [ "$BOOT_MODE" = uefi ] || { echo "legacy BIOS not supported by this recipe yet (UEFI only)"; return 1; }
}

ubuntu_keyring() {   # fetch the archive key, refuse it unless the fingerprint matches the pinned one
  command -v apk >/dev/null && { apk add -q gnupg gnupg-keyboxd >/dev/null 2>&1 || apk add -q gnupg >/dev/null; }   # Alpine lite only; KwickBoot Core: install pack
  G=$(mktemp -d); export GNUPGHOME=$G
  wget -qO $G/key.asc "https://keyserver.ubuntu.com/pks/lookup?op=get&options=mr&search=0x$UBUNTU_KEY_FPR" || { echo "cannot fetch Ubuntu archive key"; return 1; }
  gpg -q --import $G/key.asc 2>/dev/null
  gpg --with-colons --fingerprint 2>/dev/null | grep -q "^fpr:*$UBUNTU_KEY_FPR:" || { echo "Ubuntu key fingerprint MISMATCH - refusing"; return 1; }
  gpg -q --export "$UBUNTU_KEY_FPR" > $TK/ubuntu-archive.gpg
  unset GNUPGHOME; rm -rf $G
  echo "Ubuntu archive key verified ($UBUNTU_KEY_FPR)"
}

recipe_deploy() {
  # prebuilt image (linux/build-images.sh) when the server has one: about a minute instead of 10-25
  image_deploy "ubuntu2404-server"; r=$?
  [ $r = 0 ] && return 0
  [ $r = 2 ] && { echo "image failed - stopping (a half-written target must not be built on)"; return 1; }
  ubuntu_keyring || return 1
  INC=ubuntu-keyring,ca-certificates,locales,systemd-resolved,openssh-server,sudo,nftables,curl,wget,gnupg,less,nano,dosfstools,efibootmgr
  for p in $EXTRA_PKGS; do case "$p" in firmware-*) ;; *) INC="$INC,$p" ;; esac; done   # Debian firmware names do not exist on Ubuntu (linux-firmware covers them)
  echo "bootstrap packages: $INC"
  S=/usr/share/debootstrap/scripts/$SUITE; [ -e "$S" ] || S=/usr/share/debootstrap/scripts/gutsy   # all modern Ubuntu suites use the gutsy script
  ${KB_PROXY:+env http_proxy=$KB_PROXY} debootstrap --arch=amd64 --components=main,restricted,universe --include="$INC" \
    --keyring=$TK/ubuntu-archive.gpg $SUITE $T $MIRROR $S > /root/debootstrap.log 2>&1 \
    || { tail -n 30 /root/debootstrap.log; return 1; }
  echo "debootstrap ok: $(du -sh $T | cut -f1)"
}

recipe_configure() {
  wget -qO $TK/ubuntu-configure.sh "$SRV/scripts/ubuntu-configure.sh" || { echo "ubuntu-configure.sh not on server"; return 1; }
  HOSTNAME="$HOSTNAME" TZ="$TZ" LAN="$LAN" BUNDLE=basic PROFILE=server KEYS="$(cat $TK/authorized_keys)" \
    sh $TK/ubuntu-configure.sh $T
}

recipe_bootloader() { :; }   # done inside ubuntu-configure.sh (chroot)

recipe_verify() {
  chk "no desktop installed"                     '[ ! -e $T/usr/sbin/gdm3 ]'
  chk "netplan DHCP config"                      'grep -q "dhcp4: true" $T/etc/netplan/01-kwickboot.yaml'
  chk "Secure Boot shim installed"               '[ -f $T/boot/efi/EFI/ubuntu/shimx64.efi ]'
  chk "automatic security updates on"            'grep -q "Unattended-Upgrade \"1\"" $T/etc/apt/apt.conf.d/20auto-upgrades'
  chk "sshd enabled"                             'find $T/etc/systemd/system -name ssh.service -o -name ssh.socket | grep -q .'   # find: absolute links do not resolve inside the RAM OS
}
