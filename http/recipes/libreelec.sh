# recipe: LibreELEC 12.2 (Kodi media center) - official disk image, checksum verified
# SPDX-License-Identifier: AGPL-3.0-or-later
RECIPE_NAME="LibreELEC 12.2 (Kodi media center / TV box)"
RECIPE_DESC="Turns a small PC into a living-room media player (Kodi): local files, network shares, IPTV, Jellyfin client."
RECIPE_NOTE="Boots straight into Kodi on the TV. Settings on screen with a remote, keyboard or the Kore phone app. SSH as root with your key (password login off)."
RECIPE_MIN_GB=4
wget -qO /root/tk/image-lib.sh "$SRV/recipes/image.sh" && . /root/tk/image-lib.sh
RECIPE_NAME="LibreELEC 12.2 (Kodi media center / TV box)"
RECIPE_METHOD=image
RECIPE_CHECK_PORT=22                  # first boot confirmed when SSH answers
RECIPE_EFI_LABEL="UEFI OS|BOOT|LibreELEC"
IMAGE=https://releases.libreelec.tv/LibreELEC-Generic.x86_64-12.2.1.img.gz
SHA256=85fcac13e046858a142a3161f45771073fa9d3b4a836e5fa4890c4f924a386b8

# The official image is LibreELEC's USB installer. Written to the internal disk it is already a complete system: switch its
# boot menus (BIOS syslinux + UEFI GRUB) to the plain "run from this disk" line, as LibreELEC's own installer writes it.
# The storage partition grows to the whole disk on first boot (.please_resize_me in the image).
le_part() { case "$DISK" in *[0-9]) echo "${DISK}p$1" ;; *) echo "$DISK$1" ;; esac; }
libreelec_setup() {
  p1=$(le_part 1); p2=$(le_part 2); mkdir -p /tmp/le1 /tmp/le2
  mount "$p1" /tmp/le1 || { echo "cannot mount $p1"; return 1; }
  line=$(sed -n '/^LABEL run/,/APPEND/s/^ *APPEND //p' /tmp/le1/syslinux.cfg | sed 's/ portable//')
  case "$line" in *boot=*disk=*) ;; *) umount /tmp/le1; echo "unexpected syslinux.cfg layout"; return 1 ;; esac
  printf 'DEFAULT linux\nPROMPT 0\n\nLABEL linux\n  KERNEL /KERNEL\n  APPEND %s\n' "$line" > /tmp/le1/syslinux.cfg
  printf 'set timeout="0"\nset default="LibreELEC"\nmenuentry "LibreELEC" {\n\tsearch --set -f /KERNEL\n\tlinux /KERNEL %s\n}\n' "$line" > /tmp/le1/EFI/BOOT/grub.cfg
  sync; umount /tmp/le1
  # grow the storage partition to the end of the disk and make its filesystem there - what LibreELEC's own fs-resize does,
  # which skips itself once settings exist on the partition (same partition GUID, filesystem label and UUID)
  label=$(blkid -p -o value -s LABEL "$p2"); uuid=$(blkid -p -o value -s UUID "$p2")
  start=$(sgdisk -i 2 "$DISK" | awk '/^First sector/ {print $3}')
  pguid=$(sgdisk -i 2 "$DISK" | awk '/^Partition unique GUID/ {print $4}')
  ptype=$(sgdisk -i 2 "$DISK" | awk '/^Partition GUID code/ {print $4}')
  [ -n "$start" ] && [ -n "$uuid" ] || { echo "cannot read the storage partition"; return 1; }
  sgdisk -d 2 -n 2:$start:0 -t 2:$ptype -u 2:$pguid -c 2:"${label:-STORAGE}" "$DISK" >/dev/null || { echo "cannot grow the storage partition"; return 1; }
  partprobe "$DISK" 2>/dev/null; sleep 2
  mkfs.ext4 -F -q -m 0 -L "${label:-STORAGE}" -U "$uuid" "$p2" || { echo "cannot create the storage filesystem"; return 1; }
  # first-boot settings on the storage partition: hostname, SSH with keys only (no password login)
  mount "$p2" /tmp/le2 || { echo "cannot mount $p2"; return 1; }
  echo "storage: $(df -h /tmp/le2 | awk 'NR==2 {print $2}')"
  mkdir -p /tmp/le2/.cache/services /tmp/le2/.ssh
  echo "$HOSTNAME" > /tmp/le2/.cache/hostname
  printf 'SSH_ARGS=""\nSSHD_DISABLE_PW_AUTH="true"\n' > /tmp/le2/.cache/services/sshd.conf
  grep -E '^(ssh-|ecdsa-)' $TK/authorized_keys > /tmp/le2/.ssh/authorized_keys
  chmod 700 /tmp/le2/.ssh; chmod 600 /tmp/le2/.ssh/authorized_keys
  sync; umount /tmp/le2
  echo "LibreELEC set to boot from this disk; hostname $HOSTNAME, SSH key login only"
}
recipe_deploy() { image_write && libreelec_setup; }
