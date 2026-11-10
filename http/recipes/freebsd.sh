# recipe: FreeBSD 15.1 - official BASIC-CLOUDINIT disk image (UFS, UEFI), checksum verified, configured on first boot
# SPDX-License-Identifier: AGPL-3.0-or-later
RECIPE_NAME="FreeBSD 15.1"
RECIPE_DESC="Rock-solid BSD for servers, storage and network appliances: ZFS, jails, pf firewall. Official image from freebsd.org."
RECIPE_NOTE="DHCP networking. SSH with your key as root and as user 'freebsd' (wheel). The root filesystem grows to fill the disk on first boot."
RECIPE_MIN_GB=8
wget -qO /root/tk/image-lib.sh "$SRV/recipes/image.sh" && . /root/tk/image-lib.sh
RECIPE_NAME="FreeBSD 15.1"
RECIPE_METHOD=image
RECIPE_CHECK_PORT=22                  # first boot confirmed when SSH answers
RECIPE_EFI_LABEL="UEFI OS|BOOT|FreeBSD"
IMAGE=https://download.freebsd.org/releases/VM-IMAGES/15.1-RELEASE/amd64/Latest/FreeBSD-15.1-RELEASE-amd64-BASIC-CLOUDINIT-ufs.raw.xz
SHA256=926733d965078b5d635931eccc34022f74f803d35b79c05ccc2ec8a7f0ebcca2

# First-boot settings for FreeBSD's nuageinit (cloud-init "NoCloud"): a small FAT partition labelled CIDATA at the end of
# the disk with meta-data (hostname) and user-data (SSH keys). The root partition still grows into the space before it.
freebsd_seed() {
  [ "$(blkid -p -o value -s PTTYPE "$DISK")" = gpt ] || { echo "expected a GPT image"; return 1; }
  sgdisk -n 0:-64M:0 -t 0:0700 -c 0:cidata "$DISK" >/dev/null || { echo "cannot add the CIDATA partition"; return 1; }
  partprobe "$DISK" 2>/dev/null; sleep 2
  n=$(sgdisk -p "$DISK" | awk '/cidata/ {print $1}' | tail -n1)
  case "$DISK" in *[0-9]) part="${DISK}p$n" ;; *) part="$DISK$n" ;; esac
  [ -b "$part" ] || { echo "CIDATA partition $part not found"; return 1; }
  mkfs.vfat -n CIDATA "$part" >/dev/null 2>/tmp/mkfs.err || { cat /tmp/mkfs.err; return 1; }   # no iconv tables in the RAM OS: dosfstools notes its fallback on stderr
  mkdir -p /tmp/cidata && mount "$part" /tmp/cidata || return 1
  printf 'instance-id: kwickboot-%s\nlocal-hostname: %s\n' "$(date +%s)" "$HOSTNAME" > /tmp/cidata/meta-data
  {
    echo "#cloud-config"
    echo "hostname: $HOSTNAME"
    echo "ssh_authorized_keys:"
    grep -E '^(ssh-|ecdsa-)' $TK/authorized_keys | sed 's/^/  - /'
    echo "runcmd:"
    echo "  - install -d -m 700 /root/.ssh"
    echo "  - cp /home/freebsd/.ssh/authorized_keys /root/.ssh/authorized_keys"
    echo "  - chmod 600 /root/.ssh/authorized_keys"
    echo "  - sysrc sshd_enable=YES"
    echo "  - sh -c 'grep -q ^PermitRootLogin /etc/ssh/sshd_config || echo PermitRootLogin prohibit-password >> /etc/ssh/sshd_config'"
    echo "  - service sshd restart"
  } > /tmp/cidata/user-data
  sync; umount /tmp/cidata
  echo "first-boot settings: hostname $HOSTNAME, $(grep -cE '^(ssh-|ecdsa-)' $TK/authorized_keys) SSH key(s) (CIDATA on $part)"
}
recipe_deploy() { image_write && freebsd_seed; }
