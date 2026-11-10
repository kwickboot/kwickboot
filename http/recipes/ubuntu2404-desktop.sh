# recipe: Ubuntu 24.04 LTS desktop (GNOME) via debootstrap - sourced by install.sh
# SPDX-License-Identifier: AGPL-3.0-or-later
RECIPE_NAME="Ubuntu 24.04 LTS Desktop"
RECIPE_DESC="GNOME desktop for offices, schools and labs. The person at the machine creates their own account on first boot."
RECIPE_NOTE="App bundle chosen below. Firefox from Mozilla's repository (no snaps), automatic security updates, SSH key login for IT, firewall allows SSH from your LAN only. UEFI machines."
RECIPE_MIN_GB=15
RECIPE_METHOD=rootfs
RECIPE_EFI_LABEL=ubuntu
RECIPE_BUNDLES="office creative stem basic"
SUITE=noble
MIRROR=http://archive.ubuntu.com/ubuntu
# Ubuntu Archive Automatic Signing Key (2018) - signs noble's Release files. Install STOPS if the fetched key differs.
UBUNTU_KEY_FPR=F6ECB3762474EDA9D21B7022871920D1991BC93C

recipe_requirements() {
  [ "$(( $(lsblk -bdno SIZE "$DISK") / 1000000000 ))" -ge 15 ] || { echo "need >= 15 GB"; return 1; }
  [ "$BOOT_MODE" = uefi ] || { echo "legacy BIOS not supported by this recipe yet (UEFI only)"; return 1; }
  case " $RECIPE_BUNDLES " in *" ${BUNDLE:-office} "*) ;; *) echo "unknown app bundle '$BUNDLE'"; return 1 ;; esac
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
  image_deploy "ubuntu2404-desktop-${BUNDLE:-office}"; r=$?
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
  HOSTNAME="$HOSTNAME" TZ="$TZ" LAN="$LAN" BUNDLE="${BUNDLE:-office}" KEYS="$(cat $TK/authorized_keys)" \
    sh $TK/ubuntu-configure.sh $T
}

recipe_bootloader() { :; }   # done inside ubuntu-configure.sh (chroot)

recipe_verify() {
  chk "desktop installed (gdm3 + gnome-shell)"   '[ -x $T/usr/sbin/gdm3 ] && [ -x $T/usr/bin/gnome-shell ]'
  chk "first-boot account setup enabled"         'grep -q "^InitialSetupEnable=true" $T/etc/gdm3/custom.conf'
  chk "Secure Boot shim installed"               '[ -f $T/boot/efi/EFI/ubuntu/shimx64.efi ]'
  chk "no snap-only Firefox stub"                '! grep -qs "snap" $T/var/lib/dpkg/info/firefox.preinst'
  chk "automatic security updates on"            'grep -q "Unattended-Upgrade \"1\"" $T/etc/apt/apt.conf.d/20auto-upgrades'
  chk "no local user accounts pre-created"       '! awk -F: '"'"'$3>=1000 && $3<60000{f=1} END{exit !f}'"'"' $T/etc/passwd'
}
