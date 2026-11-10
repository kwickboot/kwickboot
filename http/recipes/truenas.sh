# recipe: TrueNAS Community Edition - iXsystems' official installer, network-booted and driven unattended by KwickBoot
# SPDX-License-Identifier: AGPL-3.0-or-later
# The RAM OS only checks the machine here (nothing is written). The job then reboots the machine into TrueNAS's own
# installer from the server (linux/prepare-truenas.sh -> http/truenas/), which installs onto the disk with the confirmed
# serial through the installer's API and reboots into TrueNAS. The whole disk becomes TrueNAS's boot disk.
RECIPE_NAME="TrueNAS Community Edition (NAS)"
RECIPE_DESC="ZFS storage server: SMB/NFS shares, snapshots, replication, apps. iXsystems' official installer, unattended."
RECIPE_NOTE="Secure Boot must be OFF (TrueNAS is not signed). 8 GB RAM or more. Pick a small SSD as the boot disk - TrueNAS keeps data on OTHER disks, added later in its web UI. No password is set: the first visit to http://<machine> creates the admin login."
RECIPE_MIN_GB=16
RECIPE_METHOD=image
RECIPE_CHECK_PORT=80                  # first boot confirmed when the TrueNAS web UI answers
RECIPE_BOOT=truenas
RECIPE_EFI_LABEL="^KwickBoot-handover$"

recipe_requirements() {
  wget -q --spider "$SRV/truenas/kb-truenas.cpio" || { echo "TrueNAS installer is not on the server yet (linux/prepare-truenas.sh)"; return 1; }
  ram=$(awk '/MemTotal/{print int($2/1048576+0.5)}' /proc/meminfo)
  [ "$ram" -ge 8 ] || choice "TrueNAS recommends 8 GB RAM or more: $ram GB found (ZFS will be slow)" || return 1
  if [ -d /sys/firmware/efi ]; then
    sb=$(od -An -t u1 /sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c 2>/dev/null | awk '{print $NF}')
    [ "$sb" = 1 ] && { choice "Secure Boot is ON: TrueNAS is not signed and will not start until Secure Boot is turned off in the firmware setup" || return 1; }
  fi
  return 0
}
recipe_deploy() {
  echo "machine checked; next boot: TrueNAS installer from the server installs onto the disk with serial $CONFIRM ($DISK), unattended"
}
