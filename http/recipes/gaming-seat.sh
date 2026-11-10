# recipe: KwickBoot gaming seat - diskless Steam PC (netbar, LAN party); this job only prepares its local games disk
# SPDX-License-Identifier: AGPL-3.0-or-later
# The seat's OS is never installed: every boot loads it from the server into RAM (linux/build-seat.sh -> http/seat/) and
# starts Steam Big Picture; a reboot forgets sign-ins and settings. The chosen disk becomes one ext4 file system labelled
# KBGAMES, which the seat mounts as its Steam library, so games stay installed between boots.
RECIPE_NAME="Gaming seat (diskless Steam PC)"
RECIPE_DESC="Netbar / LAN-party PC: boots Steam Big Picture from the server, clean at every start. The disk holds only games."
RECIPE_NOTE="The chosen disk is erased and becomes the seat's games library. Players sign in to Steam themselves; a reboot signs them out. Commercial netbars need Valve's cyber-cafe licence. Needs the seat image on the server (linux/build-seat.sh) and 8 GB RAM or more."
RECIPE_MIN_GB=64
RECIPE_METHOD=image
RECIPE_CHECK_PORT=22                  # first boot confirmed when SSH answers
RECIPE_BOOT=seat
RECIPE_EFI_LABEL="^KwickBoot-no-local-OS$"

recipe_requirements() {
  wget -q --spider "$SRV/seat/filesystem.squashfs" || { echo "the seat image is not on the server yet (build it with linux/build-seat.sh)"; return 1; }
  ram=$(awk '/MemTotal/{print int($2/1048576+0.5)}' /proc/meminfo)
  [ "$ram" -ge 7 ] || choice "the seat runs from RAM: $ram GB found, 8 GB or more recommended (games may stutter or fail)" || return 1
}
recipe_deploy() {
  wipefs -a -q "$DISK"* 2>/dev/null || true
  # whole-disk TRIM: SSD/NVMe get their flash back (faster writes); virtual disks shrink. Best effort (HDDs skip it).
  { timeout 120 blkdiscard -f "$DISK" 2>/dev/null || timeout 120 blkdiscard "$DISK" 2>/dev/null; } && echo "  discarded old data on $DISK (TRIM)"
  sgdisk --zap-all "$DISK" >/dev/null && sgdisk -n1:1MiB:0 -t1:8300 -c1:KBGAMES "$DISK" >/dev/null || return 1
  partprobe "$DISK" 2>/dev/null || true; sleep 2
  case "$DISK" in *[0-9]) part=${DISK}p1 ;; *) part=${DISK}1 ;; esac
  # no reserved blocks (games only), lazy init: a 2 TB disk is ready in seconds
  mkfs.ext4 -F -q -m 0 -L KBGAMES -E lazy_itable_init=1,lazy_journal_init=1 "$part" || return 1
  echo "games disk ready: $part ($(lsblk -no SIZE "$part" | head -n1 | tr -d ' ')) labelled KBGAMES; this machine now boots as a gaming seat"
}
recipe_verify() { [ "$(blkid -s LABEL -o value "$(blkid -L KBGAMES)" 2>/dev/null)" = KBGAMES ] && echo "  OK    KBGAMES file system present"; }
