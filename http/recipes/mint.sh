# recipe: Linux Mint 22.3 "Zena" Xfce - debootstrap an Ubuntu 24.04 (noble) base, then add Mint's own repo + Xfce desktop
# SPDX-License-Identifier: AGPL-3.0-or-later
# Mint ships only live-installer ISOs (no cloud/generic image), so KwickBoot builds a clean system from the official
# Ubuntu base packages + Mint's official repository - both checksum/key verified. A light Xfce desktop for reviving
# older PCs that cannot run Windows 11.
RECIPE_NAME="Linux Mint 22.3 Xfce"
RECIPE_DESC="Linux Mint Xfce: a light, familiar desktop for reviving older PCs that can't run Windows 11. Built from the official Ubuntu 24.04 LTS base plus Mint's own updates and apps."
RECIPE_NOTE="UEFI machines, 15 GB+. Light Xfce desktop. A local admin 'mint' is created and must set its own password at first sign-in (none is stored in KwickBoot). SSH key login for IT; firewall allows SSH from your LAN only. Firefox + LibreOffice, automatic security updates."
RECIPE_MIN_GB=15
RECIPE_METHOD=rootfs
RECIPE_EFI_LABEL="ubuntu|Mint|linuxmint|UEFI OS|BOOT"
SUITE=noble
MIRROR=http://archive.ubuntu.com/ubuntu
# Ubuntu Archive Automatic Signing Key (2018) - signs noble's Release files. Install STOPS if the fetched key differs.
UBUNTU_KEY_FPR=F6ECB3762474EDA9D21B7022871920D1991BC93C
MINT_CODENAME=zena                       # Linux Mint 22.3 (Ubuntu 24.04 / noble base)
MINT_MIRROR=http://packages.linuxmint.com
# Mint's official repository signing key ("Linux Mint Repository Signing Key"), fetched + fingerprint-pinned like the Ubuntu key
MINT_KEY_FPR=302F0738F465C1535761F965A6616109451BBBF2

recipe_requirements() {
  [ "$(( $(lsblk -bdno SIZE "$DISK") / 1000000000 ))" -ge 15 ] || { echo "need >= 15 GB"; return 1; }
  [ "$BOOT_MODE" = uefi ] || { echo "legacy BIOS not supported by this recipe yet (UEFI only)"; return 1; }
}

ubuntu_keyring() {   # fetch the Ubuntu archive key, refuse it unless the fingerprint matches the pinned one
  command -v apk >/dev/null && { apk add -q gnupg gnupg-keyboxd >/dev/null 2>&1 || apk add -q gnupg >/dev/null; }
  G=$(mktemp -d); export GNUPGHOME=$G
  wget -qO $G/key.asc "https://keyserver.ubuntu.com/pks/lookup?op=get&options=mr&search=0x$UBUNTU_KEY_FPR" || { echo "cannot fetch Ubuntu archive key"; return 1; }
  gpg -q --import $G/key.asc 2>/dev/null
  gpg --with-colons --fingerprint 2>/dev/null | grep -q "^fpr:*$UBUNTU_KEY_FPR:" || { echo "Ubuntu key fingerprint MISMATCH - refusing"; return 1; }
  gpg -q --export "$UBUNTU_KEY_FPR" > $TK/ubuntu-archive.gpg
  unset GNUPGHOME; rm -rf $G
  echo "Ubuntu archive key verified ($UBUNTU_KEY_FPR)"
}

recipe_deploy() {
  r=0; image_deploy "mint-xfce" || r=$?     # '|| r=$?' so set -e does not abort when no prebuilt image (r=1)
  [ "$r" = 0 ] && return 0
  [ "$r" = 2 ] && { echo "image failed - stopping (a half-written target must not be built on)"; return 1; }
  ubuntu_keyring || return 1
  INC=ubuntu-keyring,ca-certificates,gpgv,gnupg,locales,systemd-resolved,openssh-server,sudo,nftables,curl,wget,less,nano,dosfstools,efibootmgr
  for p in $EXTRA_PKGS; do case "$p" in firmware-*) ;; *) INC="$INC,$p" ;; esac; done
  echo "bootstrap packages: $INC"
  S=/usr/share/debootstrap/scripts/$SUITE; [ -e "$S" ] || S=/usr/share/debootstrap/scripts/gutsy
  ${KB_PROXY:+env http_proxy=$KB_PROXY} debootstrap --arch=amd64 --components=main,restricted,universe --include="$INC" \
    --keyring=$TK/ubuntu-archive.gpg $SUITE $T $MIRROR $S > /root/debootstrap.log 2>&1 \
    || { tail -n 30 /root/debootstrap.log; return 1; }
  echo "debootstrap ok: $(du -sh $T | cut -f1)"
}

mint_keyring() {   # fetch the Mint repo signing key; refuse unless the fingerprint matches the pinned one
  command -v apk >/dev/null && { apk add -q gnupg gnupg-keyboxd >/dev/null 2>&1 || apk add -q gnupg >/dev/null 2>&1 || true; }
  G=$(mktemp -d); export GNUPGHOME=$G
  wget -qO $G/mint.asc "https://keyserver.ubuntu.com/pks/lookup?op=get&options=mr&search=0x$MINT_KEY_FPR" || { echo "cannot fetch the Mint repo key"; return 1; }
  gpg -q --import $G/mint.asc 2>/dev/null
  gpg --with-colons --fingerprint 2>/dev/null | grep -q "^fpr:*$MINT_KEY_FPR:" || { echo "Mint repo key fingerprint MISMATCH - refusing"; return 1; }
  gpg -q --export "$MINT_KEY_FPR" > $TK/linuxmint.gpg
  unset GNUPGHOME; rm -rf $G
  echo "Mint repo key verified ($MINT_KEY_FPR)"
}

recipe_configure() {
  wget -qO $TK/mint-configure.sh "$SRV/scripts/mint-configure.sh" || { echo "mint-configure.sh not on server"; return 1; }
  # Only the packages phase (RAM-OS debootstrap fall-through) needs the Mint key; a prebuilt image already has it baked in.
  if [ "${KB_FROM_IMAGE:-0}" = 1 ]; then
    HOSTNAME="$HOSTNAME" TZ="$TZ" LAN="$LAN" KEYS="$(cat $TK/authorized_keys)" sh $TK/mint-configure.sh $T
  else
    mint_keyring || return 1
    HOSTNAME="$HOSTNAME" TZ="$TZ" LAN="$LAN" KEYS="$(cat $TK/authorized_keys)" \
      MINT_CODENAME="$MINT_CODENAME" MINT_MIRROR="$MINT_MIRROR" MINT_KEYRING="$TK/linuxmint.gpg" \
      sh $TK/mint-configure.sh $T
  fi
}

recipe_bootloader() { :; }   # done inside mint-configure.sh (chroot)

recipe_verify() {
  chk "Xfce desktop installed"                   '[ -x $T/usr/bin/xfce4-session ]'
  chk "LightDM display manager installed"        '[ -x $T/usr/sbin/lightdm ]'
  chk "Mint repository configured"               'grep -rqs packages.linuxmint.com $T/etc/apt/sources.list.d'
  chk "admin user mint, password must be set"    'grep -q "^mint:" $T/etc/passwd && [ "$(chroot $T passwd -S mint 2>/dev/null | awk "{print \$2}")" != "P" ]'
  chk "Secure Boot shim installed"               '[ -f $T/boot/efi/EFI/ubuntu/shimx64.efi ]'
  chk "automatic security updates on"            'grep -q "Unattended-Upgrade \"1\"" $T/etc/apt/apt.conf.d/20auto-upgrades'
}
