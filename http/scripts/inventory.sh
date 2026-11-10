#!/bin/sh
# PXE toolkit: full READ-ONLY hardware inventory.
# Never assembles RAID, imports pools, mounts, or writes to any disk.
#   sh /root/tk/inventory.sh
v() { cat "$1" 2>/dev/null | tr -d '\n'; }
has() { command -v "$1" >/dev/null 2>&1; }
sec() { echo; echo "[$1]"; }
ind() { sed 's/^/  /'; }

echo "===== HARDWARE INVENTORY $(date '+%F %T') ====="

sec system
echo "  vendor=$(v /sys/class/dmi/id/sys_vendor) product=$(v /sys/class/dmi/id/product_name) version=$(v /sys/class/dmi/id/product_version)"
echo "  serial=$(v /sys/class/dmi/id/product_serial) uuid=$(v /sys/class/dmi/id/product_uuid) chassis=$(v /sys/class/dmi/id/chassis_type)"
echo "  board=$(v /sys/class/dmi/id/board_vendor) $(v /sys/class/dmi/id/board_name) rev=$(v /sys/class/dmi/id/board_version) serial=$(v /sys/class/dmi/id/board_serial)"
# model keys of the PC makers' driver catalogs: Dell = System SKU, HP = board ID, Lenovo = machine type (product name)
echo "  sku=$(v /sys/class/dmi/id/product_sku)"
echo "  family=$(v /sys/class/dmi/id/product_family)"

sec bios
echo "  vendor=$(v /sys/class/dmi/id/bios_vendor) version=$(v /sys/class/dmi/id/bios_version) date=$(v /sys/class/dmi/id/bios_date)"
if [ -d /sys/firmware/efi ]; then
  sb=$(od -An -t u1 /sys/firmware/efi/efivars/SecureBoot-* 2>/dev/null | awk '{print $NF}')
  [ -z "$sb" ] && grep -q '\[integrity\]\|\[confidentiality\]' /sys/kernel/security/lockdown 2>/dev/null && sb=1
  echo "  mode=UEFI secureboot=${sb:-unknown}"
  # which Microsoft Secure Boot certificates the firmware trusts (the 2011 CAs retire in 2026; 2023 CAs replace them)
  ev=/sys/firmware/efi/efivars
  for var in db-d719b2cb-3d3a-4596-a3bc-dad00e67656f KEK-8be4df61-93ca-11d2-aa0d-00e098032b8c; do
    [ -r "$ev/$var" ] || continue
    names=$(grep -a -o -E 'Microsoft (Corporation )?(UEFI CA|KEK CA|KEK 2K CA|Option ROM UEFI CA) 20[0-9]{2}|(Microsoft )?Windows (Production PCA|UEFI CA) 20[0-9]{2}' "$ev/$var" 2>/dev/null | sort -u | paste -sd'|' -)
    echo "  sb_${var%%-*}=${names:-none}"
  done
  has efibootmgr && efibootmgr 2>/dev/null | grep -E '^Boot(Current|Order)|^Boot[0-9A-F]{4}' | cut -c1-90 | ind
else
  echo "  mode=legacy BIOS"
fi
has fwupdmgr && fwupdmgr get-devices --no-unreported-check 2>/dev/null | grep -E '^[^ ].*|Current version' | head -n 12 | ind

sec cpu
if has lscpu; then
  lscpu | grep -E '^(Model name|Socket|Core\(s\) per socket|Thread\(s\) per core|CPU\(s\)|CPU max MHz|CPU min MHz|Virtualization|L3 cache)' | ind
else
  echo "  $(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2) threads=$(nproc)"
fi
echo "  aes=$(grep -qw aes /proc/cpuinfo && echo yes || echo no) avx2=$(grep -qw avx2 /proc/cpuinfo && echo yes || echo no) microcode=$(grep -m1 microcode /proc/cpuinfo | awk '{print $3}')"

sec memory
echo "  total=$(awk '/MemTotal/{printf "%.1f GB", $2/1048576}' /proc/meminfo)"
if has dmidecode; then
  dmidecode -t 16 2>/dev/null | grep -E 'Maximum Capacity|Number Of Devices|Error Correction Type' | sort -u | ind
  dmidecode -t 17 2>/dev/null | awk -F': ' '/Memory Device/{if(l)print l; l=""} /^\s+Size:/{l=l" size="$2} /^\s+Locator:/{l=l" slot="$2} /^\s+Type:/{l=l" type="$2} /^\s+Speed:/{l=l" speed="$2} /^\s+Manufacturer:/{l=l" mfr="$2} /^\s+Part Number:/{l=l" part="$2} END{print l}' \
    | sed 's/^ //' | grep . | ind
fi
if [ -d /sys/devices/system/edac/mc ]; then
  for mc in /sys/devices/system/edac/mc/mc*; do
    [ -d "$mc" ] && echo "  EDAC $(basename $mc): corrected=$(v $mc/ce_count) uncorrected=$(v $mc/ue_count)"
  done
else
  echo "  EDAC: not available (no ECC or driver not loaded)"
fi

sec "sensors (temperatures / fans / voltages)"
if has sensors; then sensors 2>/dev/null | grep -E ':|^[a-z]' | grep -vE '^Adapter' | head -n 40 | ind; else echo "  lm-sensors not installed"; fi
if [ -e /dev/ipmi0 ] && has ipmitool; then
  echo "  --- BMC (IPMI) ---"
  ipmitool mc info 2>/dev/null | grep -E 'Manufacturer Name|Product Name|Firmware Revision' | ind
  ipmitool sdr elist 2>/dev/null | grep -iE 'fan|temp|volt|pwr|psu' | head -n 30 | ind
  echo "  --- BMC event log (last 10) ---"
  ipmitool sel elist last 10 2>/dev/null | ind
else
  echo "  no IPMI BMC detected"
fi

sec battery
found=0
for b in /sys/class/power_supply/BAT*; do
  [ -d "$b" ] || continue; found=1
  full=$(v $b/energy_full); design=$(v $b/energy_full_design)
  [ -n "$full" ] || { full=$(v $b/charge_full); design=$(v $b/charge_full_design); }
  hp=""; [ -n "$full" ] && [ -n "$design" ] && [ "$design" -gt 0 ] 2>/dev/null && hp=$((full * 100 / design))
  echo "  $(basename $b): health=${hp:-?}% cycles=$(v $b/cycle_count) status=$(v $b/status) capacity_now=$(v $b/capacity)% model=$(v $b/model_name | tr ' ' '_')"
done
[ $found = 1 ] || echo "  no battery"

sec security
# what decides which systems can run here: boot mode, Secure Boot, TPM, CPU feature level, virtualization
sb=$(od -An -t u1 /sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c 2>/dev/null | awk '{print $NF}')
# efivarfs not mounted yet: the kernel locks itself down under Secure Boot (Debian) - that answers too
[ -z "$sb" ] && grep -q '\[integrity\]\|\[confidentiality\]' /sys/kernel/security/lockdown 2>/dev/null && sb=1
fl=$(grep -m1 '^flags' /proc/cpuinfo)
has() { echo "$fl" | grep -qw "$1"; }
lvl=1; has sse4_2 && has popcnt && has ssse3 && lvl=2
[ $lvl = 2 ] && has avx2 && has bmi2 && has fma && has movbe && lvl=3
[ $lvl = 3 ] && has avx512f && has avx512bw && has avx512vl && lvl=4
tpm=$(cat /sys/class/tpm/tpm0/tpm_version_major 2>/dev/null); [ -z "$tpm" ] && [ -e /dev/tpm0 ] && tpm="present"
echo "  boot=$([ -d /sys/firmware/efi ] && echo uefi || echo bios) secure_boot=$([ "$sb" = 1 ] && echo on || echo off) tpm=${tpm:-none} cpu_level=x86-64-v$lvl virt=$(has vmx && echo vt-x || { has svm && echo amd-v || echo none; })"

sec "installed systems (from partition types and file systems - nothing mounted)"
lsblk -rno NAME,PARTTYPE,FSTYPE,LABEL 2>/dev/null | grep -vE '^(ram|loop|zram|sr)' | while read -r n pt fs lb; do
  case "$(echo "$pt" | tr A-Z a-z)" in
    ebd0a0a2-b9e5-4433-87c0-68b6b72699c7) [ "$fs" = ntfs ] && echo "  $n: Windows data (NTFS ${lb})" ;;
    de94bba4-06d1-4d40-a16a-bfd50179d6ac) echo "  $n: Windows recovery" ;;
    e3c9e316-0b5c-4db8-817d-f92df00215ae) echo "  $n: Windows reserved (MSR) - a Windows install" ;;
    fe3a2a5d-4f32-41a7-b725-accc3285a309) echo "  $n: ChromeOS kernel" ;;
    516e7cb6-6ecf-11d6-8ff8-00022d09712b|516e7cba-6ecf-11d6-8ff8-00022d09712b) echo "  $n: FreeBSD" ;;
    6a898cc3-1dd2-11b2-99a6-080020736631) echo "  $n: ZFS (Solaris/BSD)" ;;
    *) case "$fs" in ext4|xfs|btrfs) echo "  $n: Linux file system ($fs ${lb})" ;; zfs_member) echo "  $n: ZFS pool member (${lb}) - e.g. TrueNAS/Proxmox" ;;
                     ntfs) echo "  $n: NTFS ${lb}" ;; esac ;;
  esac
done

sec gpu
lspci -nnk 2>/dev/null | awk '/VGA|3D controller|Display controller/{p=1; print; next} p&&/^\t/{print; next} {p=0}' | ind
for c in /sys/class/drm/card[0-9]; do [ -d "$c" ] && echo "  $(basename $c): driver=$(basename $(readlink $c/device/driver) 2>/dev/null) vram=$(v $c/device/mem_info_vram_total)"; done

sec "disks (SERIAL = value install scripts require as CONFIRM=)"
for n in $(lsblk -dno NAME,TYPE 2>/dev/null | awk '$2=="disk"{print $1}' | grep -vE '^(ram|loop|zram)|boot[0-9]$'); do
  s=$(lsblk -dno SERIAL /dev/$n 2>/dev/null | tr -d ' ')
  for f in /sys/block/$n/serial /sys/block/$n/device/serial; do [ -z "$s" ] && [ -r "$f" ] && s=$(tr -d ' \n' < "$f"); done; [ -z "$s" ] && [ -r /sys/block/$n/device/vpd_pg80 ] && s=$(tail -c +5 /sys/block/$n/device/vpd_pg80 | tr -d ' \n\000')   # SATA/SAS: serial from the VPD page (no udev here)
  [ -z "$s" ] && [ -r /sys/block/$n/device/vpd_pg80 ] && s=$(tr -dc '[:alnum:]-_' < /sys/block/$n/device/vpd_pg80)
  printf '  %-10s %8s tran=%-6s rota=%s model=%-24s SERIAL=%s\n' "$n" "$(lsblk -dno SIZE /dev/$n | tr -d ' ')" \
    "$(lsblk -dno TRAN /dev/$n | tr -d ' ')" "$(lsblk -dno ROTA /dev/$n | tr -d ' ')" \
    "$(lsblk -dno MODEL /dev/$n | sed 's/ *$//' | tr ' ' '_')" "${s:-size-$(lsblk -dno SIZE /dev/$n | tr -d ' ')}"
  if has smartctl; then
    smartctl -H -A -i /dev/$n 2>/dev/null | grep -iE 'overall-health|SMART Health Status|Power_On_Hours|Power On Hours|Reallocated_Sector|Current_Pending|Offline_Uncorrectable|Wear_Leveling|Percentage Used|Media and Data Integrity|Temperature_Celsius|^Temperature:|Available Spare:' \
      | sed 's/  */ /g' | head -n 8 | sed 's/^/      /'
  fi
done

sec "partitions / filesystems (not mounted by inventory)"
lsblk -o NAME,SIZE,FSTYPE,LABEL,PARTLABEL 2>/dev/null | grep -vE '^(ram|loop|zram)' | ind

sec "raid / volumes (read-only detection)"
[ -r /proc/mdstat ] && grep -vE '^(Personalities|unused)' /proc/mdstat | grep . | ind
has mdadm && mdadm --examine --scan 2>/dev/null | ind
has pvs && pvs --noheadings -o pv_name,vg_name,pv_size 2>/dev/null | ind
lsblk -no NAME,FSTYPE 2>/dev/null | grep -E 'zfs_member|linux_raid_member|LVM2_member|btrfs|isw_raid|ddf_raid' | ind
lspci -nn 2>/dev/null | grep -iE 'raid|sas|megaraid|smart array|adaptec|hba' | ind
lspci -nn 2>/dev/null | grep -iqE 'megaraid|lsi|broadcom.*sas' && echo "  -> LSI/Broadcom controller: vendor tool 'storcli' needed for logical-drive details"
lspci -nn 2>/dev/null | grep -iqE 'smart array|hewlett.*raid' && echo "  -> HPE Smart Array: vendor tool 'ssacli' needed for logical-drive details"

sec network
for i in /sys/class/net/*; do
  n=$(basename "$i"); [ "$n" = lo ] && continue
  drv=$(basename "$(readlink "$i/device/driver" 2>/dev/null)" 2>/dev/null)
  ip4=$(ip -4 -o addr show "$n" 2>/dev/null | awk '{print $4}' | head -n1)
  fw=$(has ethtool && ethtool -i "$n" 2>/dev/null | awk -F': ' '/firmware-version/{print $2}')
  # Wake-on-LAN state + the fastest speed the card supports (tuning: a gigabit card linked at 100 Mb = cable/switch port)
  et=$(has ethtool && ethtool "$n" 2>/dev/null)
  wol=$(echo "$et" | awk -F': ' '/^\s*Wake-on:/{print $2; exit}')
  max=$(echo "$et" | sed -n '/Supported link modes:/,/Supported pause/p' | grep -oE '[0-9]+base' | tr -d 'base' | sort -n | tail -n1)
  echo "  $n mac=$(v "$i/address") driver=${drv:-?} fw=${fw:--} link=$(v "$i/carrier" | sed 's/1/up/;s/0/down/') speed=$(v "$i/speed")Mb ip=${ip4:--} rx_err=$(v "$i/statistics/rx_errors") tx_err=$(v "$i/statistics/tx_errors") wol=${wol:--} max=${max:--}"
done
lspci -nn 2>/dev/null | grep -iE 'ethernet|network' | ind
has lldpcli && lldpcli show neighbors summary 2>/dev/null | grep -E 'Interface|SysName|PortID|PortDescr' | ind

# every PCI device with its IDs (slot class vendor device subsystem-vendor subsystem-device): Windows driver matching
# SATA link speed per disk (an SSD on a 3 Gb/s link or cable runs at half speed)
sec sata-links
for d in /sys/block/sd*; do
  [ -e "$d" ] || continue; n=$(basename "$d")
  l=$(has smartctl && smartctl -i "/dev/$n" 2>/dev/null | sed -n 's/^SATA Version is: *//p')
  [ -n "$l" ] && echo "  $n $l"
done

sec pci-ids
lspci -n -mm 2>/dev/null | sed 's/"//g' | awk '{o=$1" "$2" "$3" "$4; n=0; for (i=5; i<=NF; i++) { if ($i ~ /^-/) continue; n++; if (n<=2) o=o" "$i } print o}' | ind

sec usb
has lsusb && lsusb 2>/dev/null | grep -viE 'root hub' | ind

sec "pci devices without a driver"
lspci -k 2>/dev/null | awk '/^[0-9a-f]/{if(dev && !drv) print dev; dev=$0; drv=0} /Kernel driver in use/{drv=1} END{if(dev && !drv) print dev}' | head -n 15 | ind

sec "firmware / hardware errors (kernel log)"
dmesg 2>/dev/null | grep -iE 'firmware.*(fail|error|not found)|direct firmware load|mce|machine check|hardware error|i/o error|ata[0-9].*(error|failed)|nvme.*(error|timeout)' | tail -n 12 | ind

echo
echo "===== END ====="
