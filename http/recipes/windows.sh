# recipe: Windows 10 / 11 from a Windows ISO in the library - unattended, optional debloat
# SPDX-License-Identifier: AGPL-3.0-or-later
# The RAM OS does the disk work itself (serial-confirmed disk, like every recipe): GPT with EFI + MSR + Windows, the
# chosen edition applied from the ISO's install.wim/esd with wimlib (staged on a temporary partition at the end of the
# disk, which is then removed and Windows grown over it), Microsoft's boot files from the applied image, KwickBoot's
# generic boot store (BCD with "locate" devices, made with Microsoft's bcdedit: linux/windows/make-generic-bcd.ps1),
# and a per-machine unattend.xml from the server. Windows then finishes its own setup on first boot (specialize + OOBE
# without questions), runs the debloat level chosen, and checks in with KwickBoot ("First boot").
RECIPE_NAME="Windows 10 / 11 (unattended, from your ISO)"
RECIPE_DESC="Microsoft's own media from your ISO library, installed without questions: hostname, region, a local admin account (password set at first sign-in), optional debloat. Activation as usual (your key, KMS or AD)."
RECIPE_NOTE="UEFI machines. Choose the Windows ISO and edition below. You accept Microsoft's license terms for your organisation when you start the install. The first sign-in asks for a new password. Legacy-BIOS machines: use Manual install."
RECIPE_MIN_GB=40
RECIPE_METHOD=image
RECIPE_PARAMS="WIN_ISO WIN_EULA WIN_DEBLOAT"
RECIPE_VARIANTS="pro education enterprise home"
RECIPE_EFI_LABEL="Windows Boot Manager"   # firmware entry created below; install.sh puts it right after network
RECIPE_CHECK=win                      # first boot confirmed when the new Windows checks in with its one-time secret

serial_of() { n=$(basename "$1"); s=$(lsblk -dno SERIAL "$1" 2>/dev/null | tr -d ' ')    # same rule as install.sh / wipe.sh
  for f in /sys/block/$n/serial /sys/block/$n/device/serial; do [ -z "$s" ] && [ -r "$f" ] && s=$(tr -d ' \n' < "$f"); done
  [ -z "$s" ] && [ -r /sys/block/$n/device/vpd_pg80 ] && s=$(tail -c +5 /sys/block/$n/device/vpd_pg80 | tr -d ' \n\000')
  [ -n "$s" ] || s="size-$(lsblk -dno SIZE "$1" | tr -d ' ')"; echo "$s"; }

edition_id() {    # template variant -> Windows edition ID in install.wim
  case "${VARIANT:-pro}" in pro) echo Professional ;; education) echo Education ;; enterprise) echo Enterprise ;; home) echo Core ;; *) echo Professional ;; esac
}

recipe_requirements() {
  [ -n "$ISO" ] || { echo "choose a Windows ISO from the library"; return 1; }
  [ "$WIN_EULA" = 1 ] || { echo "the Microsoft license terms must be accepted for an unattended install"; return 1; }
  [ "$BOOT_MODE" = uefi ] || { echo "this machine boots legacy BIOS: unattended Windows needs UEFI here - switch the firmware to UEFI, or use Manual install"; return 1; }
  WIMURL=""
  for w in sources/install.wim sources/install.esd; do
    wget -q --spider "$SRV/isofile/$ISO/$w" 2>/dev/null && { WIMURL="$SRV/isofile/$ISO/$w"; WIMEXT=${w##*.}; break; }
  done
  [ -n "$WIMURL" ] || { echo "$ISO has no sources/install.wim or install.esd - not a Windows setup ISO"; return 1; }
  # prepared edition image (pipable WIM of just this edition, made once per ISO - pxectl winimg): streamed straight
  # onto the disk - one pass, no staging partition, less to download; otherwise the whole install.wim is staged first
  PWIM="$SRV/iso/winimg/$ISO/$(edition_id).wim"
  wget -q --spider "$PWIM" 2>/dev/null || PWIM=""
  export PWIM
  wget -q --spider "$SRV/tools/windows/BCD-uefi" || { echo "the generic Windows boot store is not on the server yet (linux/windows/make-generic-bcd.ps1)"; return 1; }
  ram=$(awk '/MemTotal/{print int($2/1048576+0.5)}' /proc/meminfo)
  [ "$ram" -ge 4 ] || choice "Windows 11 asks for 4 GB RAM or more: $ram GB found" || return 1
  export WIMURL WIMEXT
  return 0
}

recipe_deploy() {
  for t in wimlib-imagex mkntfs ntfsresize ntfs-3g sgdisk mkfs.vfat mkfs.ext4; do command -v $t >/dev/null || { echo "missing tool $t"; return 1; }; done
  wimsize=$(wget -q --spider -S "${PWIM:-$WIMURL}" 2>&1 | sed -n 's/^ *Content-Length: *//p' | tail -n1 | tr -d '\r')
  [ -n "$wimsize" ] || { echo "cannot read the size of the Windows image"; return 1; }
  if [ -n "$PWIM" ]; then
    stage_mb=0; echo "Windows image: $(( wimsize / 1048576 )) MB, prepared $(edition_id) edition - streamed straight onto the disk"
  else
    stage_mb=$(( wimsize / 1048576 + 1024 ))
    echo "Windows image: $(( wimsize / 1048576 )) MB ($WIMEXT, all editions); needs ${stage_mb} MB of staging space (prepare this ISO to stream instead)"
  fi

  wipefs -a -q "$DISK"* 2>/dev/null || true
  # whole-disk TRIM: SSD/NVMe get their flash back (faster writes); virtual disks shrink. Best effort (HDDs skip it).
  { timeout 120 blkdiscard -f "$DISK" 2>/dev/null || timeout 120 blkdiscard "$DISK" 2>/dev/null; } && echo "  discarded old data on $DISK (TRIM)"
  sz=$(( $(cat /sys/class/block/$(basename $DISK)/size) / 2048 ))
  dd if=/dev/zero of="$DISK" bs=1M count=10 conv=fsync 2>/dev/null || true
  dd if=/dev/zero of="$DISK" bs=1M seek=$(( sz - 10 )) count=10 conv=fsync 2>/dev/null || true
  sgdisk --zap-all "$DISK" >/dev/null
  # Layout. Default: ONE large C: (+ hidden EFI and MSR). Extra partitions only when IT asked for them (WIN_PARTS
  # "D=200+E=0": GB each, 0 = the rest; WIN_C_GB = size of C:, 0/empty = the rest).
  set -- $(echo "$WIN_PARTS" | tr '+' ' ')
  if [ $# -eq 0 ] && [ -n "$PWIM" ]; then
    STAGE=none                     # streamed: one large C:, nothing to stage or resize
    sgdisk -n1:1MiB:+300MiB -t1:EF00 -c1:"EFI system partition" \
           -n2:0:+16MiB -t2:0C01 -c2:"Microsoft reserved partition" \
           -n3:0:0 -t3:0700 -c3:"Basic data partition" "$DISK" >/dev/null || return 1
  elif [ $# -eq 0 ]; then
    # single C: - the image is staged on a temporary partition at the end, which C: then grows over
    STAGE=tail
    sgdisk -n1:1MiB:+300MiB -t1:EF00 -c1:"EFI system partition" \
           -n2:0:+16MiB -t2:0C01 -c2:"Microsoft reserved partition" \
           -n3:0:-${stage_mb}MiB -t3:0700 -c3:"Basic data partition" \
           -n4:0:0 -t4:8300 -c4:kbstage "$DISK" >/dev/null || return 1
  else
    # C: + data partitions: the LAST data partition holds the image while it is applied, then becomes NTFS - no resizing
    STAGE=last; [ -n "$PWIM" ] && STAGE=none
    specs="C=${WIN_C_GB:-0} $*"; n=0; after_all=0
    for s in $specs; do g=${s#*=}; after_all=$(( after_all + g )); done
    args="-n1:1MiB:+300MiB -t1:EF00 -c1:EFI-system-partition -n2:0:+16MiB -t2:0C01 -c2:Microsoft-reserved-partition"
    i=3; seen=0; DRIVES=""
    for s in $specs; do
      l=${s%%=*}; g=${s#*=}; seen=$(( seen + g ))
      if [ "$g" = 0 ]; then rest_after=$(( after_all - seen )); end=$([ $rest_after -gt 0 ] && echo "-${rest_after}G" || echo 0)
      else end="+${g}G"; fi
      args="$args -n$i:0:$end -t$i:0700 -c$i:Basic-data-partition"
      [ "$l" != C ] && DRIVES="$DRIVES+$l"; LASTL=$l; i=$(( i + 1 ))
    done
    LASTN=$(( i - 1 )); DRIVES=${DRIVES#+}
    sgdisk $args "$DISK" >/dev/null || { echo "partition layout does not fit on $DISK ($(lsblk -dno SIZE $DISK | tr -d ' '))"; return 1; }
  fi
  partprobe "$DISK" 2>/dev/null || true; sleep 2
  mkfs.vfat -F32 -n SYSTEM "${P}1" >/dev/null 2>/tmp/mkfs.err || { cat /tmp/mkfs.err; return 1; }   # codepage notices are noise
  mkntfs -Q -L Windows "${P}3" >/dev/null || return 1
  if [ "$STAGE" = tail ]; then
    STAGEPART="${P}4"
  elif [ $# -gt 0 ]; then
    lastfmt=$([ "$STAGE" = none ] && echo $(( LASTN + 1 )) || echo $LASTN)    # streamed: every data partition is NTFS now
    i=4; for s in $*; do l=${s%%=*}; [ $i -lt $lastfmt ] && { mkntfs -Q -L "Data $l" "${P}$i" >/dev/null || return 1; }; i=$(( i + 1 )); done
    lay=""; for s in "C=${WIN_C_GB:-0}" $*; do g=${s#*=}; lay="$lay, ${s%%=*}: $([ "$g" = 0 ] && echo rest || echo "$g GB")"; done
    if [ "$STAGE" = last ]; then
      STAGEPART="${P}$LASTN"
      have=$(( $(blockdev --getsize64 "$STAGEPART") / 1048576 ))
      [ "$have" -ge "$stage_mb" ] || { echo "drive $LASTL: is ${have} MB - it holds the Windows image during the install, so it needs ${stage_mb} MB or more"; return 1; }
      echo "layout: ${lay#, } (Windows image staged on $LASTL: while it is applied)"
    else echo "layout: ${lay#, }"; fi
  fi
  [ "$STAGE" = none ] || mkfs.ext4 -q -F "$STAGEPART" || return 1
  # other physical drives IT chose (WIN_DISKS "sdb=D+nvme1n1=E", serials re-checked here): one NTFS data volume each
  set -- $(echo "$WIN_DISKS" | tr '+' ' '); k=1
  for s in "$@"; do
    d=/dev/${s%%=*}; l=${s#*=}; want=$(echo "$WIN_DISK_SERIALS" | cut -d+ -f$k); k=$(( k + 1 ))
    [ "$d" != "$DISK" ] || { echo "$d is the Windows disk"; return 1; }
    [ "$(serial_of "$d")" = "$want" ] || { echo "$d serial '$(serial_of "$d")' is not '$want' - not touched"; return 1; }
    wipefs -a -q "$d"* 2>/dev/null || true; { timeout 120 blkdiscard -f "$d" 2>/dev/null || timeout 120 blkdiscard "$d" 2>/dev/null; }; sgdisk --zap-all "$d" >/dev/null
    sgdisk -n1:1MiB:0 -t1:0700 -c1:Basic-data-partition "$d" >/dev/null || return 1
    partprobe "$d" 2>/dev/null || true; sleep 1
    case "$d" in *nvme*|*mmcblk*) dp="${d}p1" ;; *) dp="${d}1" ;; esac
    mkntfs -Q -L "Data $l" "$dp" >/dev/null || return 1
    echo "drive $l: = $d ($(lsblk -dno SIZE "$d" | tr -d ' '), serial $want)"
    DRIVES="${DRIVES:+$DRIVES+}$l"
  done

  step 4 "deploy Windows ($(edition_id))"
  if [ "$STAGE" = none ]; then
    # pipable WIM from the server, decompressed and written here as it arrives (wimlib checks every resource's SHA-1);
    # a broken or short download makes wimlib fail, so the exit status covers the transfer too
    how=""
    if [ "$MODE" = multicast ] && [ -n "$PORTBASE" ] && command -v udp-receiver >/dev/null; then
      # one stream for the whole room (the server waits up to 3 min for every machine). The SHA-256 of what arrived is
      # compared with the server's; a missed or broken stream -> C: is formatted again and the image fetched directly.
      IFC=$(ip route show default | awk '{print $5; exit}')
      echo "multicast: receiving the Windows image with the room on portbase $PORTBASE via $IFC"
      want=$(wget -q -O - "$PWIM.sha256" | cut -d' ' -f1)
      rm -f /tmp/kbwin.fifo; mkfifo /tmp/kbwin.fifo
      sha256sum < /tmp/kbwin.fifo | cut -d' ' -f1 > /tmp/kbwin.sum &
      # </dev/null: udpcast reads stdin even with --nokbd
      # wimlib stops reading once it has every file it needs (the WIM's trailing metadata is not used by apply):
      # `cat` then reads the stream to its end, so the receiver finishes the session and the SHA-256 covers the whole image
      sysctl -qw net.core.rmem_max=67108864 net.core.rmem_default=33554432 net.core.netdev_max_backlog=16384 2>/dev/null || true
      udp-receiver --portbase "$PORTBASE" --interface "$IFC" --nokbd --no-progress --start-timeout 240 --receive-timeout 60 \
        </dev/null 2>/tmp/kbwin.mc | tee /tmp/kbwin.fifo | { wimlib-imagex apply - 1 "${P}3"; r=$?; cat >/dev/null; exit $r; }; arc=$?
      wait
      got=$(cat /tmp/kbwin.sum)
      if [ $arc -eq 0 ] && [ -n "$want" ] && [ "$got" = "$want" ]; then how=multicast
      else
        why="wimlib exit $arc"; [ $arc -eq 0 ] && why="checksum $([ -n "$want" ] && echo "mismatch ($(echo "$got" | cut -c1-12)... vs $(echo "$want" | cut -c1-12)...)" || echo "file missing on the server")"
        echo "NOTE multicast stream missed or incomplete: $why; receiver: $(tr '\r' '\n' < /tmp/kbwin.mc 2>/dev/null | tail -n1 | cut -c1-80) - fetching the image directly"
        mkntfs -Q -L Windows "${P}3" >/dev/null || return 1
      fi
    fi
    if [ -z "$how" ]; then
      wget -q -O - "$PWIM" | wimlib-imagex apply - 1 "${P}3" || { echo "streaming the Windows image failed"; return 1; }
      how=direct
    fi
    echo "Windows image written ($how)"
    echo "note: the 'Ignoring extended attributes' warning above is expected - Windows rebuilds those cache entries itself"
  else
  mkdir -p /mnt/kbstage && mount "$STAGEPART" /mnt/kbstage || return 1
  wget -q -O /mnt/kbstage/install.$WIMEXT "$WIMURL" || { umount /mnt/kbstage; echo "download of the Windows image failed"; return 1; }
  [ "$(stat -c %s /mnt/kbstage/install.$WIMEXT)" = "$wimsize" ] || { umount /mnt/kbstage; echo "Windows image incomplete"; return 1; }
  # the image index whose edition matches the template (Pro by default); first image if none matches
  idx=$(wimlib-imagex info /mnt/kbstage/install.$WIMEXT 2>/dev/null | awk -v want="$(edition_id)" '
        /^Index:/ {i=$2} /^Edition ID:/ {if ($3 == want && !f) f=i} END {print f}')
  [ -n "$idx" ] || { echo "edition $(edition_id) not in this ISO - using image 1"; idx=1; }
  wimlib-imagex info /mnt/kbstage/install.$WIMEXT "$idx" 2>/dev/null | grep -E '^(Name|Edition ID|Build|Architecture):' | sed 's/^/  /'
  # applied straight onto the unmounted NTFS volume (libntfs-3g): security descriptors, reparse points, ADS intact;
  # wimlib checks the SHA-1 of every resource it extracts
  wimlib-imagex apply /mnt/kbstage/install.$WIMEXT "$idx" "${P}3" || { umount /mnt/kbstage; echo "applying the image failed"; return 1; }
  # wimlib cannot write NTFS extended attributes from Linux (upstream limitation, wimlib 1.14.x); on Windows images they
  # are code-integrity cache hints that Windows rebuilds - setup and boot do not need them (verified on Windows 11 26300)
  echo "note: the 'Ignoring extended attributes' warning above is expected - Windows rebuilds those cache entries itself"
  umount /mnt/kbstage
  fi

  if [ "$STAGE" = none ]; then
    step 5 "boot files, unattended setup"
  elif [ "$STAGE" = tail ]; then
    step 5 "boot files, unattended setup, grow Windows over the staging area"
    start=$(sgdisk -i3 "$DISK" | sed -n 's/^First sector: \([0-9]*\).*/\1/p')
    sgdisk -d4 -d3 -n3:"$start":0 -t3:0700 -c3:"Basic data partition" "$DISK" >/dev/null || return 1
    partprobe "$DISK" 2>/dev/null || true; sleep 2
    printf 'y\n' | ntfsresize -f "${P}3" >/dev/null || return 1
  else
    step 5 "boot files, unattended setup, drive $LASTL: from the staging area"
    mkntfs -Q -L "Data $LASTL" "$STAGEPART" >/dev/null || return 1
  fi
  mkdir -p /mnt/kbwin /mnt/kbesp
  ntfs-3g "${P}3" /mnt/kbwin || return 1
  mount "${P}1" /mnt/kbesp || return 1
  mkdir -p /mnt/kbesp/EFI/Microsoft/Boot /mnt/kbesp/EFI/Boot
  cp -r /mnt/kbwin/Windows/Boot/EFI/. /mnt/kbesp/EFI/Microsoft/Boot/
  [ -d /mnt/kbwin/Windows/Boot/Fonts ] && cp -r /mnt/kbwin/Windows/Boot/Fonts /mnt/kbesp/EFI/Microsoft/Boot/
  cp /mnt/kbwin/Windows/Boot/EFI/bootmgfw.efi /mnt/kbesp/EFI/Boot/bootx64.efi
  wget -q -O /mnt/kbesp/EFI/Microsoft/Boot/BCD "$SRV/tools/windows/BCD-uefi" || return 1
  # per-machine answer file (hostname, region, admin account) + first-boot script (debloat, check-in)
  MACA=$(cat /sys/class/net/$(ip route show default | awk '{print $5; exit}')/address)
  TOK=$(tr ' ' '\n' < /proc/cmdline | sed -n 's/^pxetoken=//p')
  mkdir -p /mnt/kbwin/Windows/Panther /mnt/kbwin/Windows/Setup/Scripts
  wget -q --header "X-PXE-Token: $TOK" -O /mnt/kbwin/Windows/Panther/unattend.xml "$SRV/agent/unattend?mac=$MACA" || { echo "the server did not return an answer file"; return 1; }
  wget -q -O /mnt/kbwin/Windows/Setup/Scripts/SetupComplete.cmd "$SRV/scripts/windows/SetupComplete.cmd" || return 1
  wget -q -O /mnt/kbwin/Windows/Setup/Scripts/kb-firstboot.ps1 "$SRV/scripts/windows/kb-firstboot.ps1" || return 1
  if [ "$WIN_DRIVERS" = 1 ]; then       # drivers matched from this PC's inventory (library on the server) -> C:\Drivers
    mkdir -p /mnt/kbwin/Drivers
    if wget -q --header "X-PXE-Token: $TOK" -O - "$SRV/agent/drivers?mac=$MACA" | tar -x -C /mnt/kbwin/Drivers; then
      echo "drivers: $(find /mnt/kbwin/Drivers -iname '*.inf' | wc -l) driver(s) in C:\Drivers ($(du -sm /mnt/kbwin/Drivers | cut -f1) MB) - Windows installs them during setup"
    else echo "WARN the driver folders could not be fetched - Windows uses its own drivers (and Windows Update)"; fi
  fi
  if [ "$WIN_JOIN" = entra ]; then      # Entra ID: the department's provisioning package, applied (then deleted) after setup
    wget -q --header "X-PXE-Token: $TOK" -O /mnt/kbwin/Windows/Setup/Scripts/kb-join.ppkg "$SRV/agent/ppkg?mac=$MACA" \
      && echo "Entra ID: provisioning package in place (joins right after setup)" \
      || { rm -f /mnt/kbwin/Windows/Setup/Scripts/kb-join.ppkg; echo "WARN Entra ID package not available - this PC will not join"; }
  fi
  printf 'BOOTORDER=%s\r\n' "${BOOT_ORDER:-network-first}" > /tmp/kb-bootorder.env
  printf 'SERVER=%s\r\nMAC=%s\r\nSECRET=%s\r\nDEBLOAT=%s\r\nUSER=%s\r\nAPPS=%s\r\nDRIVES=%s\r\n' "${SRV#http://}" "$MACA" "$WIN_FB" "${WIN_DEBLOAT:-standard}" "${WIN_USER:-admin}" "${WIN_APPS:-none}" "$DRIVES" \
    > /mnt/kbwin/Windows/Setup/Scripts/kb.env
  cat /tmp/kb-bootorder.env >> /mnt/kbwin/Windows/Setup/Scripts/kb.env
  sync; umount /mnt/kbesp; umount /mnt/kbwin
  # firmware boot entry (the disk's \EFI\Boot fallback also works, but an entry keeps the order KwickBoot sets)
  if command -v efibootmgr >/dev/null && [ -d /sys/firmware/efi ]; then
    for b in $(efibootmgr | awk '/Windows Boot Manager/{print substr($1,5,4)}'); do efibootmgr -q -b "$b" -B; done
    efibootmgr -q -c -d "$DISK" -p 1 -L "Windows Boot Manager" -l '\EFI\Microsoft\Boot\bootmgfw.efi' || echo "note: no firmware boot entry (the disk fallback loader is in place)"
  fi
  echo "Windows $(edition_id) applied; it finishes its own setup on first boot (a few minutes, one or two restarts)"
}

recipe_verify() {
  mount "${P}1" /mnt/kbesp 2>/dev/null; ntfs-3g -o ro "${P}3" /mnt/kbwin 2>/dev/null
  chk "boot manager in place"          '[ -f /mnt/kbesp/EFI/Microsoft/Boot/bootmgfw.efi ] && [ -f /mnt/kbesp/EFI/Boot/bootx64.efi ]'
  chk "boot store (BCD) in place"      '[ -s /mnt/kbesp/EFI/Microsoft/Boot/BCD ]'
  chk "Windows loader present"         '[ -f /mnt/kbwin/Windows/System32/winload.efi ]'
  chk "unattended setup prepared"      'grep -q "<ComputerName>" /mnt/kbwin/Windows/Panther/unattend.xml'
  chk "first-boot script in place"     '[ -f /mnt/kbwin/Windows/Setup/Scripts/SetupComplete.cmd ]'
  umount /mnt/kbesp /mnt/kbwin 2>/dev/null
  return 0   # failures are counted by chk in $fails
}
