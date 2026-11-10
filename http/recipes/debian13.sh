# recipe: Debian 13 (trixie) via debootstrap - sourced by install.sh
RECIPE_NAME="Debian 13 (trixie)"
RECIPE_DESC="Stable, general-purpose Linux for servers and desktops. Minimal server install with SSH and firewall."
RECIPE_NOTE="DHCP networking, SSH with your key (no passwords), firewall allows SSH from your LAN only. UEFI machines."
RECIPE_MIN_GB=4
RECIPE_METHOD=rootfs
RECIPE_EFI_LABEL=debian
SUITE=trixie
MIRROR=http://deb.debian.org/debian
RECIPE_IMAGE=debian13          # prebuilt image (linux/build-images.sh) - streamed in about a minute; debootstrap if absent
DEB_PKGS="linux-image-amd64,firmware-linux-free,openssh-server,sudo,systemd-resolved,nftables,wireguard-tools,rsync,curl,wget,ca-certificates,smartmontools,cryptsetup,restic,tmux,locales,zram-tools,less,nano,bash-completion,exfatprogs,dosfstools,usbutils,pciutils,dbus,iproute2,iputils-ping,ethtool,lm-sensors,efibootmgr"

recipe_requirements() {
  [ "$(( $(lsblk -bdno SIZE "$DISK") / 1000000000 ))" -ge 4 ] || { echo "need >= 4 GB"; return 1; }
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
