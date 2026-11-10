# recipe: Debian 13 minimal (trixie) - the smallest useful Debian; sourced by install.sh (same steps as debian13.sh)
RECIPE_NAME="Debian 13 minimal"
RECIPE_DESC="The smallest Debian 13: kernel, SSH, sudo, network and firewall - nothing else (image about 150-200 MB). Add what you need with apt."
RECIPE_NOTE="DHCP networking, SSH with your key (no passwords), firewall allows SSH from your LAN only. UEFI machines."
RECIPE_MIN_GB=2
RECIPE_METHOD=rootfs
RECIPE_EFI_LABEL=debian
SUITE=trixie
MIRROR=http://deb.debian.org/debian
RECIPE_IMAGE=debian13-min      # prebuilt image (linux/build-images.sh debian13-min); debootstrap if absent
DEB_PKGS="linux-image-amd64,openssh-server,sudo,systemd-resolved,nftables,ca-certificates,locales,less,nano,iproute2,iputils-ping,dosfstools,efibootmgr,dbus"

recipe_requirements() {
  [ "$(( $(lsblk -bdno SIZE "$DISK") / 1000000000 ))" -ge 2 ] || { echo "need >= 2 GB"; return 1; }
  [ "$BOOT_MODE" = uefi ] || { echo "legacy BIOS not supported by this recipe yet (UEFI only)"; return 1; }
}

recipe_deploy() {
  if [ "$BOOT_MODE" = uefi ]; then
    image_deploy "$RECIPE_IMAGE"; r=$?
    [ $r = 0 ] && return 0
    [ $r = 2 ] && { echo "image failed - stopping (a half-written target must not be built on)"; return 1; }
  fi
  INC=$DEB_PKGS
  if [ "$BOOT_MODE" = uefi ]; then INC="$INC,grub-efi-amd64,grub-efi-amd64-signed,shim-signed"; else INC="$INC,grub-pc"; fi
  for p in $EXTRA_PKGS; do case ",$INC," in *",$p,"*) ;; *) INC="$INC,$p" ;; esac; done
  echo "packages: $INC"
  ${KB_PROXY:+env http_proxy=$KB_PROXY} debootstrap --arch=amd64 --components=main,non-free-firmware --include="$INC" \
    --keyring=/usr/share/keyrings/debian-archive-$SUITE-stable.gpg $SUITE $T $MIRROR > /root/debootstrap.log 2>&1 \
    || { tail -n 30 /root/debootstrap.log; return 1; }
  echo "debootstrap ok: $(du -sh $T | cut -f1)"
}

recipe_configure() {
  HOSTNAME="$HOSTNAME" TZ="$TZ" LAN="$LAN" KEYS="$(cat /root/tk/authorized_keys)" BOOT_MODE="$BOOT_MODE" DISK="$DISK" KB_FROM_IMAGE="$KB_FROM_IMAGE" \
    sh /root/tk/debian13-configure.sh $T
}

recipe_bootloader() { :; }   # done inside debian13-configure.sh (chroot)
