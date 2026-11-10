#!/bin/sh
# PXE toolkit: pre-install hardware analysis. READ-ONLY.
# Turns hardware facts into GO / WARN / STOP and derives install parameters.
#   DISK=/dev/sda [OS=debian13] sh /root/tk/preflight.sh
# Exit: 0 = GO (maybe with warnings), 2 = STOP.
# Writes /root/tk/preflight.env  (BOOT_MODE, EXTRA_PKGS, KERNEL_OPTS) for the installers.
DISK=${DISK:-}
OS=${OS:-debian13}
stops=0; warns=0
ok()   { echo "  OK    $*"; }
warn() { echo "  WARN  $*"; warns=$((warns+1)); }
stop() { echo "  STOP  $*"; stops=$((stops+1)); }
# a judgment call, not a physical impossibility: STOP unless the user chose "install anyway" (FORCE=1) - we provide the tool, they decide
choice() { if [ "${FORCE:-0}" = 1 ]; then echo "  WARN  $* (accepted by the user - installing anyway)"; warns=$((warns+1)); else echo "  STOP  $* - choose 'install anyway' to proceed"; stops=$((stops+1)); fi; }
has()  { command -v "$1" >/dev/null 2>&1; }
pkgs=""; kopts=""
addpkg() { case " $pkgs " in *" $1 "*) ;; *) pkgs="$pkgs $1" ;; esac; }

echo "===== PREFLIGHT for OS=$OS target=${DISK:-<none>} ====="

echo "[boot]"
if [ -d /sys/firmware/efi ]; then
  BOOT_MODE=uefi
  sb=$(od -An -t u1 /sys/firmware/efi/efivars/SecureBoot-* 2>/dev/null | awk '{print $NF}')
  [ -z "$sb" ] && grep -q '\[integrity\]\|\[confidentiality\]' /sys/kernel/security/lockdown 2>/dev/null && sb=1
  ok "UEFI firmware"
  if [ "$sb" = "1" ]; then warn "Secure Boot is ON: only signed loaders/kernels (Debian shim+GRUB OK; unsigned drivers will not load)"; else ok "Secure Boot off"; fi
  ls /sys/firmware/efi/efivars >/dev/null 2>&1 && ok "EFI variables writable path present (boot order can be set from Linux)" || warn "EFI variables not accessible: boot order must be set in BIOS setup"
else
  BOOT_MODE=bios; warn "legacy BIOS mode: will use BIOS GRUB (GPT + bios_grub partition)"
fi

echo "[cpu / memory]"
mem_mb=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)
[ "$mem_mb" -lt 1000 ] && choice "only ${mem_mb} MB RAM" || ok "${mem_mb} MB RAM"
grep -qw lm /proc/cpuinfo && ok "64-bit CPU" || stop "CPU is not 64-bit"
if grep -qw sse4_2 /proc/cpuinfo && grep -qw popcnt /proc/cpuinfo; then ok "x86-64-v2 capable"; else warn "CPU below x86-64-v2 (RHEL9/Rocky9/Alma9 will not run; Debian fine)"; fi
grep -qwE 'vmx|svm' /proc/cpuinfo && ok "hardware virtualization present" || warn "no VT-x/AMD-V (no KVM/Proxmox VMs)"
grep -q 'GenuineIntel' /proc/cpuinfo && addpkg intel-microcode
grep -q 'AuthenticAMD' /proc/cpuinfo && addpkg amd64-microcode
for mc in /sys/devices/system/edac/mc/mc*; do
  [ -r "$mc/ue_count" ] && [ "$(cat $mc/ue_count)" -gt 0 ] && choice "uncorrected memory errors on $(basename $mc) - run memtester / replace DIMM"
done

echo "[target disk]"
if [ -z "$DISK" ]; then
  warn "no DISK given - disk checks skipped"
elif [ ! -b "$DISK" ]; then
  stop "$DISK does not exist"
else
  n=$(basename "$DISK")
  size_gb=$(( $(lsblk -bdno SIZE "$DISK") / 1000000000 ))
  tran=$(lsblk -dno TRAN "$DISK" | tr -d ' ')
  [ "$size_gb" -lt 8 ] && stop "$DISK is only ${size_gb} GB" || ok "$DISK ${size_gb} GB tran=${tran:-?}"
  [ "$tran" = "usb" ] && warn "$DISK is a USB disk - is that really the install target?"
  lsblk -nro MOUNTPOINTS "$DISK" | grep -q . && stop "$DISK has mounted filesystems"
  fs=$(lsblk -nro FSTYPE "$DISK" | grep . | sort -u | paste -sd, -)
  [ -n "$fs" ] && warn "$DISK contains data/filesystems: $fs (will be destroyed)"
  lsblk -nro FSTYPE "$DISK" | grep -qE 'linux_raid_member|LVM2_member|zfs_member|isw_raid|ddf_raid' && warn "$DISK is a member of RAID/LVM/ZFS - other disks of that set will break"
  if has smartctl; then
    h=$(smartctl -H "$DISK" 2>/dev/null | grep -iE 'overall-health|SMART Health Status' | awk -F: '{print $2}' | tr -d ' ')
    case "$h" in PASSED|OK) ok "SMART health $h" ;; "") ok "SMART not available (eMMC/virtio/some USB bridges)" ;; *) choice "SMART health: $h - disk is failing" ;; esac
    rs=$(smartctl -A "$DISK" 2>/dev/null | awk '/Reallocated_Sector_Ct|Current_Pending_Sector/{s+=$10} END{print s+0}')
    [ "${rs:-0}" -gt 0 ] && warn "$rs reallocated/pending sectors on $DISK"
  fi
  [ "$(cat /sys/block/$n/queue/rotational 2>/dev/null)" = 1 ] && ok "rotational disk (HDD)"
fi

echo "[storage controllers]"
if lspci -nn 2>/dev/null | grep -iqE 'raid bus controller'; then
  lspci -nn | grep -iE 'raid bus controller' | sed 's/^/        /'
  warn "hardware/firmware RAID controller present: disks may be hidden behind it (set HBA/JBOD/AHCI mode or use vendor tool)"
fi
lspci -nn 2>/dev/null | grep -iqE 'RAID mode|Intel.*RST|Volume Management Device' && warn "Intel RST/VMD mode: Linux may not see NVMe/SATA disks - switch BIOS to AHCI"

echo "[network]"
nic_ok=0
for i in /sys/class/net/*; do
  n=$(basename "$i"); [ "$n" = lo ] && continue
  drv=$(basename "$(readlink "$i/device/driver" 2>/dev/null)" 2>/dev/null); [ -z "$drv" ] && continue
  link=$(cat "$i/carrier" 2>/dev/null)
  case "$drv" in
    r8169|r8152) addpkg firmware-realtek ;;
    bnx2|bnx2x) addpkg firmware-bnx2; addpkg firmware-bnx2x ;;
    tg3) addpkg firmware-misc-nonfree ;;
    qed|qede|qla*|bnxt_en) addpkg firmware-qlogic; addpkg firmware-misc-nonfree ;;
    ice|i40e|ixgbe|igb|igc|e1000e) addpkg firmware-intel-misc ;;
    iwlwifi) addpkg firmware-iwlwifi ;;
    ath*|mt76*|rtw*|brcmfmac) addpkg firmware-atheros; addpkg firmware-realtek; addpkg firmware-brcm80211; addpkg firmware-misc-nonfree ;;
    virtio_net) ;;
  esac
  [ "$link" = 1 ] && nic_ok=1 && ok "$n driver=$drv link up" || echo "  info  $n driver=$drv link down"
done
[ "$nic_ok" = 1 ] || { [ "${KB_OFFLINE:-0}" = 1 ] && ok "no network link - fine: this install is offline (everything comes from the ISO)" || choice "no wired NIC with link - installer cannot download packages"; }
dmesg 2>/dev/null | grep -iE 'direct firmware load .* failed' | grep -o 'firmware [^ ]* failed' | sort -u | sed 's/^/  WARN  missing /' | while read -r l; do echo "$l"; done

echo "[graphics / other devices]"
for d in /sys/class/drm/card[0-9]; do
  [ -d "$d" ] || continue
  drv=$(basename "$(readlink $d/device/driver)" 2>/dev/null)
  case "$drv" in
    i915|xe) addpkg firmware-intel-graphics; ok "GPU $drv" ;;
    amdgpu|radeon) addpkg firmware-amd-graphics; ok "GPU $drv" ;;
    nouveau) warn "NVIDIA GPU: open driver only; proprietary nvidia-driver needed for full performance" ;;
    *) ok "GPU ${drv:-unknown}" ;;
  esac
done
nodrv=$(lspci -k 2>/dev/null | awk '/^[0-9a-f]/{if(dev && !drv) print dev; dev=$0; drv=0} /Kernel driver in use/{drv=1} END{if(dev && !drv) print dev}' | grep -viE 'bridge|host|isa|smbus|signal processing|communication controller|serial bus' )
[ -n "$nodrv" ] && { warn "devices without driver:"; echo "$nodrv" | sed 's/^/        /'; }

echo "[environment]"
yr=$(date +%Y); [ "$yr" -ge 2025 ] && ok "clock $(date '+%F %T')" || warn "clock looks wrong ($(date)) - TLS/GPG may fail"
if [ "${KB_OFFLINE:-0}" = 1 ]; then ok "offline install: no package downloads needed"
else wget -q -T 5 -O /dev/null http://deb.debian.org/debian/dists/trixie/Release && ok "Debian mirror reachable" || choice "cannot reach deb.debian.org"; fi

addpkg firmware-linux-free
cat > /root/tk/preflight.env <<EOF
BOOT_MODE=$BOOT_MODE
EXTRA_PKGS="$(echo $pkgs)"
KERNEL_OPTS="$(echo $kopts)"
EOF
echo "[derived install parameters]"; sed 's/^/  /' /root/tk/preflight.env
if [ $stops -gt 0 ]; then echo "===== RESULT: STOP ($stops blocking, $warns warnings) ====="; exit 2; fi
echo "===== RESULT: GO ($warns warnings) ====="
