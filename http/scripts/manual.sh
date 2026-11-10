#!/bin/sh
# KwickBoot manual install - runs inside the RAM OS. The vendor's own installer (any ISO: Windows, BSDs, Linux, appliances)
# runs in a VM on THIS machine with its real disk attached; its screen, keyboard and mouse are in the web console (noVNC).
# When the installer restarts the VM the job ends, and the machine itself boots the installed system on its own hardware.
# SPDX-License-Identifier: AGPL-3.0-or-later
#   ISO=<file in the server's iso library> DISK=/dev/x CONFIRM=<disk serial> [FW=auto|bios|uefi] [DISKBUS=auto|ide|sata|nvme|none] sh manual.sh
#   FW: firmware of the VM (auto = like this machine; bios for DOS, older Windows, firmware tools)
#   DISKBUS: how the real disk is attached (auto = its real type; ide for XP/2000-era systems and DOS; none = run the ISO only)
#   steps: 1 confirm  2 preflight  3 prepare VM  4 installer running (web console)  7 verify  8 finish
SRV="http://{{SERVER_IP}}:{{HTTP_PORT}}"
TK=/root/tk
FW_MODE=${FW:-auto}; BUS=${DISKBUS:-auto}
case "$FW_MODE" in auto|bios|uefi) ;; *) echo "STOP: FW must be auto, bios or uefi"; exit 1 ;; esac
case "$BUS" in auto|ide|sata|nvme|none) ;; *) echo "STOP: DISKBUS must be auto, ide, sata, nvme or none"; exit 1 ;; esac
: "${ISO:?set ISO}"
[ "$BUS" = none ] || : "${DISK:?set DISK}" "${CONFIRM:?set CONFIRM=<disk serial>}"
KB_T0=$(date +%s)
step() { echo; echo ">>> [$1] $2   (+$(( $(date +%s) - KB_T0 ))s)"; }
die()  { echo "STOP: $*"; exit 1; }

step 1 "confirm target disk"
echo "$ISO" | grep -qE '^[A-Za-z0-9._+-]+\.iso$' || die "bad ISO name"
if [ "$BUS" = none ]; then
  echo "no disk attached: the ISO runs on its own (live tools, firmware updates) - nothing on this machine is written"
else
[ -b "$DISK" ] || die "$DISK is not a block device"
n=$(basename "$DISK"); s=$(lsblk -dno SERIAL "$DISK" 2>/dev/null | tr -d ' ')
for f in /sys/block/$n/serial /sys/block/$n/device/serial; do [ -z "$s" ] && [ -r "$f" ] && s=$(tr -d ' \n' < "$f"); done
[ -z "$s" ] && [ -r /sys/block/$n/device/vpd_pg80 ] && s=$(tail -c +5 /sys/block/$n/device/vpd_pg80 | tr -d ' \n\000')
[ -n "$s" ] || s="size-$(lsblk -dno SIZE "$DISK" | tr -d ' ')"
[ "$CONFIRM" = "$s" ] || die "CONFIRM '$CONFIRM' does not match $DISK serial '$s'"
lsblk -nro MOUNTPOINTS "$DISK" | grep -q . && die "$DISK has mounted filesystems"
echo "target $DISK serial $s size $(lsblk -dno SIZE $DISK | tr -d ' ') confirmed"
fi
wget -q --spider "$SRV/iso/$ISO" || die "ISO $ISO is not in the server's ISO library"

step 2 "preflight"
command -v qemu-system-x86_64 >/dev/null || die "the virt pack did not load - nothing was started"
modprobe -q kvm_intel 2>/dev/null || modprobe -q kvm_amd 2>/dev/null
if [ -e /dev/kvm ]; then ACCEL="-accel kvm -cpu host"; echo "  OK    hardware virtualization (KVM)"
else ACCEL="-accel tcg"; echo "  WARN  no hardware virtualization (VT-x/AMD-V off in the firmware?) - the installer runs VERY slowly"; fi
mem=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo); vm=$(( mem - 1536 ))          # the RAM OS keeps 1.5 GB
[ "$vm" -ge 2048 ] || die "the installer VM needs 2 GB on top of the RAM OS: ${mem} MB found"
[ "$vm" -gt 16384 ] && vm=16384
cpus=$(nproc); [ "$cpus" -gt 8 ] && cpus=8
echo "  OK    VM: $vm MB RAM, $cpus CPUs (machine: $mem MB, $(nproc) CPUs)"

step 3 "prepare the installer VM"
FW=""; MACH="q35"
want=$FW_MODE; [ "$want" = auto ] && { [ -d /sys/firmware/efi ] && want=uefi || want=bios; }
# IDE: the classic PC chipset (i440FX) with a real IDE controller - XP/2000-era Windows and DOS have no AHCI driver
[ "$BUS" = ide ] && MACH="pc"
if [ "$want" = uefi ] && [ "$BUS" = ide ]; then
  cp /usr/share/OVMF/OVMF_VARS_4M.fd /run/kbvm-vars.fd
  FW="-drive if=pflash,format=raw,unit=0,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd -drive if=pflash,format=raw,unit=1,file=/run/kbvm-vars.fd"
  echo "  firmware: UEFI (classic chipset with IDE)"
elif [ "$want" = uefi ]; then
  # UEFI like the machine, and Secure Boot exactly as the machine has it: on -> Microsoft's keys enrolled (Windows 11,
  # signed Linux); off -> plain UEFI that boots unsigned installers too (BSDs, Arch, rescue tools)
  sb=$(od -An -t u1 /sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c 2>/dev/null | awk '{print $NF}')
  if [ "$sb" = 1 ]; then
    cp /usr/share/OVMF/OVMF_VARS_4M.ms.fd /run/kbvm-vars.fd
    MACH="q35,smm=on"
    FW="-global driver=cfi.pflash01,property=secure,value=on -drive if=pflash,format=raw,unit=0,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.ms.fd -drive if=pflash,format=raw,unit=1,file=/run/kbvm-vars.fd"
    echo "  firmware: UEFI, Secure Boot ON like this machine (unsigned installers will not start)"
  else
    cp /usr/share/OVMF/OVMF_VARS_4M.fd /run/kbvm-vars.fd
    FW="-drive if=pflash,format=raw,unit=0,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd -drive if=pflash,format=raw,unit=1,file=/run/kbvm-vars.fd"
    echo "  firmware: UEFI, Secure Boot off like this machine (any installer starts)"
  fi
else
  if [ -d /sys/firmware/efi ]; then
    echo "  firmware: legacy BIOS (chosen) - this machine boots UEFI: to start what gets installed, set its firmware to"
    echo "            legacy / CSM boot (not every UEFI PC offers that)"
  else echo "  firmware: legacy BIOS (like this machine)"; fi
fi
# the real disk on the controller type it really has, so the installed system boots on this hardware with the right driver
DRV=""; DEV=""
bus=$BUS; [ "$bus" = auto ] && { case "$DISK" in /dev/nvme*) bus=nvme ;; *) bus=sata ;; esac; }
case "$bus" in
  none) echo "  disk: none attached" ;;
  nvme) DEV="-device nvme,drive=d0,serial=$(echo "$s" | cut -c1-20)"; echo "  disk: $DISK as NVMe" ;;
  ide)  DEV="-device ide-hd,drive=d0,bus=ide.0,unit=0"; echo "  disk: $DISK as IDE (classic chipset)" ;;
  *)    DEV="-device ide-hd,drive=d0,bus=ide.0"; echo "  disk: $DISK as SATA (AHCI)" ;;
esac
[ "$bus" = none ] || DRV="-drive file=$DISK,format=raw,if=none,id=d0,cache=none,aio=native"
# the CD drive: on the classic chipset the second IDE channel, on q35 the second AHCI port
CD="-device ide-cd,drive=cd0,bus=ide.1,bootindex=0"
TPM=""
if [ -c /dev/tpm0 ]; then TPM="-tpmdev passthrough,id=tpm0,path=/dev/tpm0 -device tpm-tis,tpmdev=tpm0"; echo "  TPM: this machine's TPM passed through"
else echo "  TPM: none on this machine"; fi
echo "  ISO: $ISO streamed from the server"

step 4 "installer running - use the Screen tab in the KwickBoot web console"
if [ "$BUS" = none ]; then echo "The ISO runs until it shuts down or restarts; then this job ends and the machine stays in KwickBoot."
else echo "When the installer restarts the machine, this job ends and the machine boots the installed system itself."; fi
# the screen listens on loopback only - the RAM OS does not bring "lo" up by itself
ip link set lo up 2>/dev/null; ip -4 addr show lo | grep -q 127.0.0.1 || ip addr add 127.0.0.1/8 dev lo 2>/dev/null
echo "manual" > /run/kb-vnc            # tells the agent a VNC screen exists (port 5900 on this machine only)
# shellcheck disable=SC2086
NIC="e1000e"; [ "$MACH" = pc ] && NIC="e1000"        # older systems know the classic e1000, not e1000e
qemu-system-x86_64 -name "KwickBoot manual install" -machine $MACH $ACCEL -m $vm -smp $cpus $FW \
  $DRV $DEV \
  -drive file.driver=http,file.url="$SRV/iso/$ISO",file.readahead=4194304,if=none,id=cd0,media=cdrom,readonly=on \
  $CD -boot order=d,menu=off \
  $TPM -nic user,model=$NIC -device qemu-xhci -device usb-tablet -device usb-kbd \
  -vga std -display none -vnc 127.0.0.1:0 -no-reboot -monitor none -serial none > /tmp/kbvm.log 2>&1
rc=$?
tail -n 20 /tmp/kbvm.log
rm -f /run/kb-vnc /run/kbvm-vars.fd
echo "installer VM stopped after $(( ($(date +%s) - KB_T0) / 60 )) min"

step 7 "verify"
if [ "$bus" = none ]; then
  echo "  OK    ISO session ended (no disk was attached)"
  step 8 "finish"; echo "MANUAL RUN DONE: $ISO"; exit 0
fi
partprobe "$DISK" 2>/dev/null; sleep 1
parts=$(lsblk -nro NAME,FSTYPE "$DISK" | awk 'NR>1 && $2!=""' | wc -l)
[ "$parts" -gt 0 ] || die "nothing was written to $DISK - the installer did not finish (rc $rc)"
lsblk -o NAME,SIZE,FSTYPE,LABEL "$DISK" | sed 's/^/  /'
if [ -d /sys/firmware/efi ] && command -v efibootmgr >/dev/null; then
  # the installer wrote its boot entry into the VM's firmware: give the real firmware one too
  for p in $(lsblk -nro NAME,PARTTYPE "$DISK" | awk 'tolower($2)=="c12a7328-f81f-11d2-ba4b-00a0c93ec93b"{print $1}'); do
    mkdir -p /mnt/kbesp && mount -o ro /dev/$p /mnt/kbesp 2>/dev/null || continue
    for l in EFI/Microsoft/Boot/bootmgfw.efi EFI/*/shimx64.efi EFI/BOOT/BOOTX64.EFI; do
      f=$(ls /mnt/kbesp/$l 2>/dev/null | head -n1); [ -n "$f" ] && break
    done
    umount /mnt/kbesp
    [ -n "$f" ] || continue
    rel=$(echo "${f#/mnt/kbesp/}" | tr '/' '\\'); num=$(cat /sys/class/block/$p/partition)
    case "$rel" in *Microsoft*) label="Windows Boot Manager" ;; *) label="Installed system" ;; esac
    efibootmgr -q -c -d "$DISK" -p "$num" -L "$label" -l "\\$rel" && echo "  OK    firmware boot entry '$label' -> \\$rel"
    break
  done
  # KwickBoot stays in control: network entries first (the server hands the machine to its disk), then the rest
  all=$(efibootmgr | sed -n 's/^BootOrder: //p' | tr ',' ' ')
  net=$(efibootmgr | awk '/^Boot[0-9A-F]{4}\*? /{print}' | grep -iE 'PXE|IPv4|Network|NIC' | grep -viE 'IPv6|HTTP' | cut -c5-8 | tr '\n' ' ')
  order=""; for b in $net $all; do case " $order " in *" $b "*) ;; *) [ -n "$b" ] && order="$order $b" ;; esac; done
  [ -n "$net" ] && efibootmgr -q -o "$(echo $order | tr ' ' ',')" && echo "  OK    boot order: network first, then the installed system"
fi

step 8 "finish"
echo "MANUAL INSTALL DONE: $ISO on $DISK - the machine now boots the installed system"
