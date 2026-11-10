"""KwickBoot RAM OS customisation (built by the server at start; the downloaded Alpine files are never modified).
SPDX-License-Identifier: AGPL-3.0-or-later

- kb-init.cpio.gz : a patched copy of Alpine's initramfs /init, loaded AFTER initramfs-lts so it replaces it
                    (cpio archives concatenate; later files win). Patches: KwickBoot banner, silence the harmless
                    "df: /tmp/tk.apkovl.tar.gz: can't find mount point" noise of HTTP-fetched overlays.
- overlay files   : branded /etc/issue, /etc/motd, hostname, and a live status screen on tty1 (login moves to tty2).
"""
import gzip
import io
import os

CONSOLE = r'''#!/bin/sh
# KwickBoot RAM OS status screen (tty1). Read-only: the same picture IT sees in the web console, on the machine itself.
# Reads only /proc and /sys (works before any pack is loaded); redraws every 2 s.
SRV="$(cat /etc/kb-server 2>/dev/null)"
# UI language for the text console (Latin/Cyrillic labels only - the console font cannot render CJK/Arabic/Thai, which get
# the localised UI from the framebuffer GUI instead). Set by kb_lang= on the kernel command line; defaults to English.
KBLANG="$(cat /etc/kb-lang 2>/dev/null)"; [ -z "$KBLANG" ] && KBLANG=en
[ -f "/usr/share/kb-i18n/$KBLANG.env" ] && . "/usr/share/kb-i18n/$KBLANG.env"
B='\033[1m'; D='\033[2m'; G='\033[1;32m'; Y='\033[1;33m'; C='\033[1;36m'; R='\033[1;31m'; O='\033[1;38;5;160m'; N='\033[0m'   # O = KwickBoot brand red (#C32B27 ~ 256-colour 160)
hsize() { awk -v b="$1" 'BEGIN{ split("B KB MB GB TB PB",u," "); i=1; while (b>=1000 && i<6) { b/=1000; i++ } printf (b<10 ? "%.1f %s" : "%.0f %s"), b, u[i] }'; }
row() { printf "  $D%-10s$N %b\n" "$1" "$2"; }      # %b: colour codes inside the value work too

# --- static facts (once)
vendor=$(cat /sys/class/dmi/id/sys_vendor 2>/dev/null); product=$(cat /sys/class/dmi/id/product_name 2>/dev/null)
serial=$(cat /sys/class/dmi/id/product_serial 2>/dev/null); bios="$(cat /sys/class/dmi/id/bios_vendor 2>/dev/null) $(cat /sys/class/dmi/id/bios_version 2>/dev/null) ($(cat /sys/class/dmi/id/bios_date 2>/dev/null))"
cpu=$(awk -F': ' '/^model name/{print $2; exit}' /proc/cpuinfo | sed 's/  */ /g'); threads=$(grep -c '^processor' /proc/cpuinfo)
cores=$(awk -F': ' '/^cpu cores/{print $2; exit}' /proc/cpuinfo)
ram=$(awk '/^MemTotal/{print $2*1024}' /proc/meminfo)
# Secure Boot: efivarfs is mounted only after the hw pack loads (seconds after this screen starts) -> re-read until known;
# the kernel's lockdown state (Debian locks down under Secure Boot) answers before that
fwinfo() {
  if [ -d /sys/firmware/efi ]; then
    sb=$(od -An -tu1 -j4 -N1 /sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c 2>/dev/null | tr -d ' ')
    [ -z "$sb" ] && grep -q '\[integrity\]\|\[confidentiality\]' /sys/kernel/security/lockdown 2>/dev/null && sb=1
    case "$sb" in 1) fw="UEFI, Secure Boot on" ;; 0) fw="UEFI, Secure Boot off" ;; *) fw="UEFI, Secure Boot ?" ;; esac
  else fw="legacy BIOS"; sb=0; fi
}
fwinfo
case "$vendor $product" in *QEMU*|*VMware*|*VirtualBox*|*Hyper-V*|*KVM*) kind=" (virtual machine)" ;; *) kind="" ;; esac

n=0
while true; do
  # --- live facts
  st=$(sed -n 1p /run/kb-state 2>/dev/null); msg=$(sed -n 2p /run/kb-state 2>/dev/null)
  ifc=$(ip route show default 2>/dev/null | awk '{print $5; exit}')
  ip=$(ip -4 -o addr show ${ifc:-} 2>/dev/null | awk '$2!="lo"{print $4; exit}')
  mac=$(cat /sys/class/net/${ifc:-eth0}/address 2>/dev/null)
  drv=$(basename "$(readlink /sys/class/net/${ifc:-eth0}/device/driver 2>/dev/null)" 2>/dev/null)
  spd=$(cat /sys/class/net/${ifc:-eth0}/speed 2>/dev/null); [ -n "$spd" ] && [ "$spd" -gt 0 ] 2>/dev/null && spd="$spd Mb/s" || spd=""
  avail=$(awk '/^MemAvailable/{print $2*1024}' /proc/meminfo)
  up=$(awk '{s=int($1); printf "%dm %02ds", s/60, s%60}' /proc/uptime)
  load=$(cut -d' ' -f1 /proc/loadavg)
  packs=$(ls /run/kb-pack-* 2>/dev/null | sed 's|.*/kb-pack-||' | tr '\n' ' ')
  seen=""; [ -f /run/kb-seen ] && seen=$(( $(date +%s) - $(date -r /run/kb-seen +%s 2>/dev/null || echo 0) ))
  # disks: refresh every ~10 s (lsblk only once the hw pack brought it; /sys otherwise)
  if [ $((n % 5)) = 0 ]; then
    disks=""
    for b in /sys/block/*; do
      d=${b##*/}; case "$d" in loop*|ram*|zram*|sr*|fd*|dm-*|md*|nbd*) continue ;; esac
      sz=$(( $(cat $b/size 2>/dev/null || echo 0) * 512 )); [ "$sz" -gt 0 ] || continue
      mdl=$(cat $b/device/model 2>/dev/null | sed 's/  *$//'); sn=$(cat $b/device/serial $b/serial 2>/dev/null | head -n1 | tr -d ' ')   # NVMe/SCSI: device/serial, virtio: serial
      [ -z "$sn" ] && [ -r $b/device/vpd_pg80 ] && sn=$(tail -c +5 $b/device/vpd_pg80 | tr -dc 'A-Za-z0-9._-')   # SATA/SAS: VPD page 0x80
      [ -z "$sn" ] && command -v lsblk >/dev/null && sn=$(lsblk -dno SERIAL /dev/$d 2>/dev/null | tr -d ' ')
      case "$d" in nvme*) t=NVMe ;; *) [ "$(cat $b/queue/rotational 2>/dev/null)" = 1 ] && t=HDD || t=SSD ;; esac
      [ "$(cat $b/removable 2>/dev/null)" = 1 ] && t="$t, removable"
      tr=$(readlink -f $b 2>/dev/null); case "$tr" in */usb*) t="$t, USB" ;; esac
      disks="$disks$(printf '%-8s %8s  %-5s %s%s' "$d" "$(hsize $sz)" "$t" "${mdl:-?}" "${sn:+  serial $sn}")
"
    done
  fi
  n=$((n + 1)); spin=$(printf '%s' '|/-\' | cut -c$(( n % 4 + 1 )))     # moves every redraw: the screen is alive

  # --- draw: the whole frame off-screen first, then painted over the old one (no clear = no flicker, here and in
  # the web console's screen view)
  {
  printf "  $O$B KwickBoot $N $D| ${C_subtitle:-the RAM OS with AI built in}$N\n"
  printf "  ${D}disks are untouched unless IT starts an install$N\n\n"
  case "$st" in
    ready)     sval="${G}${C_ready:-READY}$N ${D}$spin  managed from the KwickBoot web console$N" ;;
    busy)      sval="${Y}WORKING $spin$N  $msg" ;;
    booting)   sval="${Y}STARTING $spin$N  $msg" ;;
    rebooting) sval="${C}RESTARTING$N  $msg" ;;
    *)         sval="starting up..." ;;
  esac
  row "${C_status:-Status}" "$sval"
  if [ -z "$ip" ]; then row "${C_server:-Server}" "${R}no network address yet$N"
  elif [ -n "$seen" ] && [ "$seen" -gt 90 ]; then row "${C_server:-Server}" "$SRV  ${R}no contact for ${seen}s$N"
  else row "${C_server:-Server}" "$SRV  ${D}${seen:+contact ${seen}s ago}$N"; fi
  echo
  printf "  ${B}${C_machine:-Machine}$N\n"
  row "${C_model:-Model}" "$vendor $product$kind"
  [ -n "$serial" ] && row "${C_serial:-Serial}" "$serial"
  [ "$sb" = 1 ] || [ "$sb" = 0 ] || fwinfo
  row "${C_firmware:-Firmware}" "$fw  ${D}$bios$N"
  row "${C_cpu:-CPU}" "$cpu  ${D}${cores:+$cores cores, }$threads threads$N"
  row "${C_memory:-Memory}" "$(hsize $ram)  ${D}$(hsize $avail) free$N"
  row "${C_network:-Network}" "${ifc:-?} ${mac:-}  ${ip:-waiting...}  ${D}${drv}${spd:+, $spd}$N"
  echo
  printf "  ${B}${C_disks:-Disks}$N\n"
  if [ -n "$disks" ]; then printf '%s' "$disks" | sed 's/^/    /'; else printf "    ${D}none found (yet)$N\n"; fi
  echo
  row "RAM OS" "up $up, load $load  ${D}packs: ${packs:-none}$N"

  # current job: step k of 8 with a progress bar, then the last lines of its log
  log=$(cat /run/kb-joblog 2>/dev/null)
  if [ -n "$log" ] && [ -f "$log" ]; then
    cur=$(grep '^>>> \[' "$log" | tail -n1 | sed 's/^>>> //')
    k=$(echo "$cur" | sed -n 's/^\[\([0-9]*\)\].*/\1/p')
    now=$(date +%s); [ "$cur" != "$lastcur" ] && { lastcur=$cur; t_step=$now; }; el=$(( now - ${t_step:-$now} ))
    quiet=$(( now - $(date -r "$log" +%s 2>/dev/null || echo $now) ))
    echo
    if [ -n "$k" ]; then
      bar=$(awk -v k="$k" 'BEGIN{ w=32; f=int(w*k/8); s=""; for(i=0;i<w;i++) s=s (i<f?"#":"."); print s }')
      printf "  ${B}Job$N  [%s] step %s of 8: %s  ${Y}%s$N ${D}running %ss$N\n" "$bar" "$k" "$(echo "$cur" | sed 's/^\[[0-9]*\] //; s/   (+[0-9]*s)$//')" "$spin" "$el"
      [ "$quiet" -gt 20 ] && ! grep -q '^=== job .* finished' "$log" && printf "  ${D}working quietly (no new output for %ss) - this step can take a while, the machine is not stuck$N\n" "$quiet"
    else printf "  ${B}Job$N  %s\n" "${cur:-$(basename "$log" .log)}"; fi
    # progress meters (wimlib, wget, pv) rewrite one line with CR: show the newest update, not the first one
    tail -n 8 "$log" | awk -F'\r' '{ s=""; for (i=NF; i>0; i--) if ($i ~ /[^ ]/) { s=$i; break }; print s }' | cut -c1-110 | sed 's/^/    /'
    grep -q '^=== job .* finished' "$log" && printf "  ${D}(finished)$N\n"
  fi
  hint="managed remotely - no local login"; grep -qw kb_shell=1 /proc/cmdline && hint="Alt+F2: emergency console"
  printf "\n  ${D}%s     KwickBoot - AGPL-3.0$N\n" "$hint"
  } > /run/kb-frame 2>&1
  # home, each line + "erase to end of line", then erase whatever is left below
  printf '\033[H'; sed "s/\$/$(printf '\033')[K/" /run/kb-frame; printf '\033[J'
  sleep 2
done
'''

FACTS = r'''#!/bin/sh
# KwickBoot fact gatherer -> /run/kb-facts (key=value) for the framebuffer GUI (kbgui). The same facts the text console
# shows, keyed to the GUI's zones. Read-only (/proc, /sys, /run/kb-state, /run/kb-joblog); rewrites atomically every 2 s.
# A value ending in "|g" is drawn green (good/ready). Runs only when the PC has a usable framebuffer; else kb-console.
hsize() { awk -v b="$1" 'BEGIN{ split("B KB MB GB TB PB",u," "); i=1; while (b>=1000 && i<6){b/=1000;i++} printf (b<10?"%.1f %s":"%.0f %s"),b,u[i] }'; }
vendor=$(cat /sys/class/dmi/id/sys_vendor 2>/dev/null); product=$(cat /sys/class/dmi/id/product_name 2>/dev/null)
serial=$(cat /sys/class/dmi/id/product_serial 2>/dev/null)
cpu=$(awk -F': ' '/^model name/{print $2; exit}' /proc/cpuinfo | sed 's/  */ /g'); threads=$(grep -c '^processor' /proc/cpuinfo)
cores=$(awk -F': ' '/^cpu cores/{print $2; exit}' /proc/cpuinfo); ram=$(awk '/^MemTotal/{print $2*1024}' /proc/meminfo)
case "$vendor $product" in *QEMU*|*VMware*|*VirtualBox*|*Hyper-V*|*KVM*) kind=" (virtual machine)" ;; *) kind="" ;; esac
fwinfo() {
  if [ -d /sys/firmware/efi ]; then
    sb=$(od -An -tu1 -j4 -N1 /sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c 2>/dev/null | tr -d ' ')
    [ -z "$sb" ] && grep -q '\[integrity\]\|\[confidentiality\]' /sys/kernel/security/lockdown 2>/dev/null && sb=1
    case "$sb" in 1) fw="UEFI, Secure Boot ON" ;; 0) fw="UEFI, Secure Boot off" ;; *) fw="UEFI, Secure Boot ?" ;; esac
  else fw="legacy BIOS"; sb=0; fi
}
tpmv() {
  if [ -d /sys/class/tpm/tpm0 ]; then mj=$(cat /sys/class/tpm/tpm0/tpm_version_major 2>/dev/null)
    case "$mj" in 2) tpm="2.0" ;; 1) tpm="1.2" ;; *) tpm="present" ;; esac
  else tpm="none"; fi
}
emit() { printf '%s=%s\n' "$1" "$2"; }
while :; do
  fwinfo; tpmv
  st=$(sed -n 1p /run/kb-state 2>/dev/null); msg=$(sed -n 2p /run/kb-state 2>/dev/null)
  ifc=$(ip route show default 2>/dev/null | awk '{print $5; exit}')
  ip=$(ip -4 -o addr show ${ifc:-} 2>/dev/null | awk '$2!="lo"{print $4; exit}')
  drv=$(basename "$(readlink /sys/class/net/${ifc:-eth0}/device/driver 2>/dev/null)" 2>/dev/null)
  mac=$(cat /sys/class/net/${ifc:-eth0}/address 2>/dev/null)
  spd=$(cat /sys/class/net/${ifc:-eth0}/speed 2>/dev/null); if [ -n "$spd" ] && [ "$spd" -gt 0 ] 2>/dev/null; then spd="$spd Mb/s"; else spd=""; fi
  avail=$(awk '/^MemAvailable/{print $2*1024}' /proc/meminfo)
  disk=""; : > /run/kb-disks.tmp
  for b in /sys/block/*; do
    d=${b##*/}; case "$d" in loop*|ram*|zram*|sr*|fd*|dm-*|md*|nbd*) continue ;; esac
    sz=$(( $(cat $b/size 2>/dev/null || echo 0) * 512 )); [ "$sz" -gt 0 ] || continue
    [ "$(cat $b/removable 2>/dev/null)" = 1 ] && continue
    mdl=$(cat $b/device/model 2>/dev/null | sed 's/  *$//')
    case "$d" in nvme*) t=NVMe ;; *) [ "$(cat $b/queue/rotational 2>/dev/null)" = 1 ] && t=HDD || t=SSD ;; esac
    [ -z "$disk" ] && disk="$d  $(hsize $sz) $t  ${mdl:-disk}"
    printf '%s|%s|%s|%s\n' "$d" "$(hsize $sz)" "$t" "${mdl:-disk}" >> /run/kb-disks.tmp   # full list for the GUI disk picker
  done
  mv -f /run/kb-disks.tmp /run/kb-disks 2>/dev/null
  [ -z "$disk" ] && disk="scanning disks..."
  {
    case "$st" in
      ready)     emit statuschip "READY  managed|g"; emit status "Idle - no job running."; emit status2 "Pick a disk and an OS below, or let IT start it from the web console. Both stay in sync." ;;
      busy)      emit statuschip "WORKING"; emit status "${msg:-Working...}"; emit status2 "A job is running - watch it here or in the web console." ;;
      booting)   emit statuschip "STARTING"; emit status "Starting up..."; emit status2 "Bringing up drivers and the network." ;;
      rebooting) emit statuschip "RESTARTING"; emit status "Restarting..."; emit status2 "${msg:-Rebooting the machine.}" ;;
      *)         emit statuschip "STARTING"; emit status "Starting up..."; emit status2 "Please wait." ;;
    esac
    emit m_model "$vendor $product$kind"
    emit m_serial "${serial:--}"
    emit m_cpu "$cpu  ${cores:+${cores}c/}${threads}t"
    emit m_memory "$(hsize $ram)  ($(hsize $avail) free)"
    if [ "$sb" = 1 ]; then emit m_firmware "$fw|g"; else emit m_firmware "$fw"; fi
    emit m_tpm "$tpm"
    emit disks "$disk"
    if [ -n "$ip" ]; then emit net_addr "$ip${spd:+  $spd}|g"; else emit net_addr "waiting for network..."; fi
    emit net_nic "${drv:-?}${mac:+  $mac}"
    if [ "$sb" = 1 ] && [ "$tpm" = "2.0" ]; then
      emit ai "Windows 11 ready - TPM 2.0 + Secure Boot detected|Windows 10 or any Linux also install cleanly|No disk problems found - safe to install"
    elif [ -d /sys/firmware/efi ]; then
      emit ai "Windows 11 needs Secure Boot ON (TPM $tpm)|Windows 10 or any Linux install cleanly now|No disk problems found - safe to install"
    else
      emit ai "Legacy BIOS - Windows 10 or Linux recommended|Windows 11 needs UEFI + TPM 2.0 + Secure Boot|No disk problems found - safe to install"
    fi
  } > /run/kb-facts.tmp 2>/dev/null && mv -f /run/kb-facts.tmp /run/kb-facts
  sleep 2
done
'''

GUIAGENT = r'''#!/bin/sh
# kb-gui-agent: carries out the action the framebuffer GUI (kbgui) writes to /run/kb-gui-action. Read-only to disks here;
# it stages the choices (locale, account, target) that an install job consumes, and applies the keyboard live when it can.
A=/run/kb-gui-action
# fetch the installable-OS catalog once at start so the GUI's OS picker has real options (best-effort; retries)
S=$(cat /etc/kb-server 2>/dev/null)
[ -n "$S" ] && for i in 1 2 3 4 5; do
  ifc=$(ip route show default 2>/dev/null | awk '{print $5; exit}'); MYMAC=$(cat /sys/class/net/${ifc:-eth0}/address 2>/dev/null)
  wget -qO /run/kb-oslist.tmp "http://$S/agent/oslist?mac=$MYMAC" 2>/dev/null && [ -s /run/kb-oslist.tmp ] && mv /run/kb-oslist.tmp /run/kb-oslist && break   # mac -> department-filtered catalog
  sleep 2
done
[ -n "$S" ] && wget -qO /run/kb-tools.tmp "http://$S/agent/toollist" 2>/dev/null && mv -f /run/kb-tools.tmp /run/kb-tools   # rescue-tool catalog for the GUI
# credential POSTs (install / rescue) go over the server's pinned HTTPS endpoint; the pin trusts exactly the server's key (no CA).
# /ep/pin -> "<host:port> sha256//<key>", or "off" when the server has ep_https_port disabled (then we fall back to plain HTTP).
EPP=$([ -n "$S" ] && wget -qO- "http://$S/ep/pin" 2>/dev/null)
EPH=$(printf '%s' "$EPP" | awk '{print $1}'); EPPIN=$(printf '%s' "$EPP" | awk '{print $2}')
case "$EPPIN" in sha256//*) : ;; *) EPH=""; EPPIN="" ;; esac
REALCURL=""
ensure_curl() {   # load the 'secure' pack (real curl + TLS libs) on demand so credential POSTs can key-PIN the server
  [ -n "$REALCURL" ] && return 0
  [ -x /usr/bin/curl ] && { REALCURL=/usr/bin/curl; return 0; }         # pack already loaded (Core ships only the /bin/curl shim)
  [ -z "$S" ] && return 1
  [ -s /run/kb-sums ] || wget -qO /run/kb-sums "http://$S/kbcore/SHA256SUMS" 2>/dev/null
  want=$(awk '$2=="packs/secure.sqfs"{print $1}' /run/kb-sums 2>/dev/null)
  wget -qO /tmp/secure.sqfs "http://$S/kbcore/packs/secure.sqfs" 2>/dev/null || return 1
  [ -n "$want" ] && [ "$(sha256sum /tmp/secure.sqfs | cut -d' ' -f1)" != "$want" ] && { rm -f /tmp/secure.sqfs; return 1; }   # integrity-checked like every pack
  mkdir -p /run/pk-secure; mount -t squashfs -o loop,ro /tmp/secure.sqfs /run/pk-secure 2>/dev/null || { rm -f /tmp/secure.sqfs; return 1; }
  cp -a /run/pk-secure/. / 2>/dev/null; umount /run/pk-secure; rm -f /tmp/secure.sqfs; touch /run/kb-pack-secure
  [ -x /usr/bin/curl ] && { REALCURL=/usr/bin/curl; return 0; }; return 1
}
postcred() {  # $1 = /path?query ; $2 = json body ; echoes the server response
  if [ -n "$EPH" ] && [ -n "$EPPIN" ] && ensure_curl; then
    "$REALCURL" -s -m 20 -k --pinnedpubkey "$EPPIN" -H "Content-Type: application/json" -d "$2" "https://$EPH$1" 2>/dev/null   # TLS + real key pinning
  elif [ -n "$EPH" ] && [ -n "$EPPIN" ]; then
    curl -s -m 20 -H "Content-Type: application/json" -d "$2" "https://$EPH$1" 2>/dev/null   # pack unavailable: busybox-wget TLS, encrypt-only
  else
    curl -s -m 20 -H "Content-Type: application/json" -d "$2" "http://$S$1" 2>/dev/null       # server HTTPS off: plain HTTP fallback
  fi
}
while :; do
  if [ -s "$A" ]; then
    act=$(cat "$A" 2>/dev/null); : > "$A"
    case "$act" in
      language:*) r=${act#language:}; loc=${r%%:*}; km=${r#*:}
        printf '%s\n' "$loc" > /run/kb-locale; printf '%s\n' "$km" > /run/kb-keymap
        # live keyboard: only if a busybox binary keymap happens to ship for this layout (cosmetic; the install applies it for real)
        [ -f "/usr/share/keymaps/$km.bmap" ] && loadkmap < "/usr/share/keymaps/$km.bmap" 2>/dev/null
        printf 'Language %s / keymap %s staged for the install\n' "$loc" "$km" > /run/kb-gui-notice ;;
      login:*)    printf '%s\n' "$act" > /run/kb-login      # staged sign-in (validated server-side when the job starts)
        printf 'Signed in\n' > /run/kb-gui-notice ;;
      install:*)  r=${act#install:}; osid=${r%%:*}; osid=${osid#os=}; S=$(cat /etc/kb-server 2>/dev/null)
        gdisk=$(printf '%s' "$act" | sed -n 's/.*:disk=\([A-Za-z0-9_-]*\).*/\1/p')   # disk the GUI picker chose (if any)
        lu=$(sed -n 's/^login:user=\([^:]*\):.*/\1/p' /run/kb-login 2>/dev/null)
        lp=$(sed -n 's/^login:user=[^:]*:pass=\([^:]*\):.*/\1/p' /run/kb-login 2>/dev/null)
        loc=$(cat /run/kb-locale 2>/dev/null); km=$(cat /run/kb-keymap 2>/dev/null)
        ifc=$(ip route show default 2>/dev/null | awk '{print $5; exit}'); mac=$(cat /sys/class/net/${ifc:-eth0}/address 2>/dev/null)
        disk=""; [ -n "$gdisk" ] && [ -e "/sys/block/$gdisk" ] && disk=$gdisk            # honor the chosen disk when it is a real block device
        [ -z "$disk" ] && for b in /sys/block/*; do dd=${b##*/}; case "$dd" in loop*|ram*|sr*|zram*|fd*|dm-*|md*|nbd*) continue ;; esac
          [ "$(cat $b/removable 2>/dev/null)" = 1 ] && continue; [ "$(cat $b/size 2>/dev/null || echo 0)" -gt 0 ] || continue; disk=$dd; break; done
        json="{\"os\":\"$osid\",\"user\":\"$lu\",\"pass\":\"$lp\",\"disk\":\"$disk\",\"locale\":\"$loc\",\"keymap\":\"$km\"}"
        r=$(postcred "/agent/install?mac=$mac" "$json")
        printf 'install:os=%s disk=%s\n' "$osid" "$disk" > /run/kb-install-req
        case "$r" in ok*) printf 'Install started on %s - watch here and in the web console\n' "${disk:-disk}" > /run/kb-gui-notice ;;
          error:*) printf '%s\n' "${r#error: }" > /run/kb-gui-notice ;;                           # clean message from the server
          *) printf 'Install request failed - check with IT\n' > /run/kb-gui-notice ;; esac ;;    # HTML error page / empty -> clean fallback
      rescue:*)   tool=$(printf '%s' "$act" | sed -n 's/.*tool=\([A-Za-z0-9_-]*\).*/\1/p'); S=$(cat /etc/kb-server 2>/dev/null)
        lu=$(sed -n 's/^login:user=\([^:]*\):.*/\1/p' /run/kb-login 2>/dev/null)
        lp=$(sed -n 's/^login:user=[^:]*:pass=\([^:]*\):.*/\1/p' /run/kb-login 2>/dev/null)
        ifc=$(ip route show default 2>/dev/null | awk '{print $5; exit}'); mac=$(cat /sys/class/net/${ifc:-eth0}/address 2>/dev/null)
        json="{\"user\":\"$lu\",\"pass\":\"$lp\",\"tool\":\"$tool\"}"
        r=$(postcred "/agent/rescue?mac=$mac" "$json")
        printf 'rescue:tool=%s -> %s\n' "$tool" "$r" > /run/kb-rescue-req
        case "$r" in ok*) printf 'Rescue armed - rebooting into the tool now...\n' > /run/kb-gui-notice; sync; sleep 2; reboot ;;
          error:*) printf '%s\n' "${r#error: }" > /run/kb-gui-notice ;;                           # clean message from the server
          *) printf 'Rescue request failed - check with IT\n' > /run/kb-gui-notice ;; esac ;;     # HTML error page / empty -> clean fallback
      details|connect|language|install|rescue) : ;;   # a bare tile name = just opened the panel; nothing to carry out
      *) : ;;
    esac
  fi
  sleep 1
done
'''

# kb-desktop: the RICH-tier launcher. init runs it on capable machines (DRM/KMS + enough RAM). It fetches the on-demand
# 'desktop' pack (cog WPE-WebKit kiosk browser + cage Wayland compositor + software-GL mesa/llvmpipe), then shows the
# machine-scoped page (/local) fullscreen. Any failure exits non-zero so init falls through to kbgui, then kb-console.
DESKTOP = r'''#!/bin/sh
S=$(cat /etc/kb-server 2>/dev/null)
log(){ echo "kb-desktop: $*" > /dev/kmsg 2>/dev/null; echo "kb-desktop: $*"; }
if [ ! -x /usr/bin/kbweb ]; then                                 # load the desktop pack on demand (fetch + sha256-verify + overlay)
  [ -n "$S" ] || { log "no server"; exit 1; }
  [ -s /run/kb-sums ] || wget -qO /run/kb-sums "http://$S/kbcore/SHA256SUMS" 2>/dev/null
  want=$(awk '$2=="packs/desktop.sqfs"{print $1}' /run/kb-sums 2>/dev/null)
  log "fetching desktop pack (~150MB)..."
  wget -qO /tmp/desktop.sqfs "http://$S/kbcore/packs/desktop.sqfs" 2>/dev/null || { log "download failed"; exit 1; }
  [ -n "$want" ] && [ "$(sha256sum /tmp/desktop.sqfs | cut -d' ' -f1)" != "$want" ] && { log "checksum mismatch - refused"; rm -f /tmp/desktop.sqfs; exit 1; }
  mkdir -p /run/pk-desktop; mount -t squashfs -o loop,ro /tmp/desktop.sqfs /run/pk-desktop 2>/dev/null || { log "mount failed"; exit 1; }
  cp -a /run/pk-desktop/. / 2>/dev/null; umount /run/pk-desktop; rm -f /tmp/desktop.sqfs; touch /run/kb-pack-desktop
fi
[ -x /usr/bin/kbweb ] || { log "browser not in pack"; exit 1; }
# kbweb = our tiny CPU framebuffer WPE browser: WebKit paints on the CPU (Skia) into an offscreen SHM buffer (via the
# wpebackend-fdo SHM exportable backend), kbweb blits each frame straight to /dev/fb0 and feeds it /dev/input/event*.
# NO cog, NO DRM master, NO KMS modeset, NO GL/EGL/GBM/dmabuf, NO seatd/udev/openvt/mesa. Works on ANY framebuffer
# (efifb, simpledrm, hyperv_drm, virtio-gpu, real GPUs) - exactly where kbgui works. Boots in seconds.
export WEBKIT_SKIA_ENABLE_CPU_RENDERING=1 LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu
# WebKit's WebProcess insists on creating an EGL display at startup even though kbweb paints on the CPU and exports SHM.
# Force mesa's SOFTWARE llvmpipe via the SURFACELESS platform so that EGL needs NO GPU, NO render node and NO KMS - this is
# what lets the browser tier run on hyperv_drm / simpledrm / basic-framebuffer machines, not just GPUs with a render node.
export LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=llvmpipe EGL_PLATFORM=surfaceless
# We render ONLY our own /local console (never untrusted web) on a trusted appliance, so disable WebKit's bubblewrap
# sandbox (it pivot_roots, impossible on the initramfs root). User decision.
export WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1
export XDG_RUNTIME_DIR=/run/kb-xdg; mkdir -p "$XDG_RUNTIME_DIR"; chmod 700 "$XDG_RUNTIME_DIR"
mountpoint -q /dev/shm 2>/dev/null || { mkdir -p /dev/shm; mount -t tmpfs tmpfs /dev/shm 2>/dev/null; }   # WPE SHM frame buffers
# fontconfig: ship a minimal config if the pack lacks /etc/fonts/fonts.conf (else WebKit renders blank text)
[ -f /etc/fonts/fonts.conf ] || { mkdir -p /etc/fonts /var/cache/fontconfig; printf '%s' '<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd"><fontconfig><dir>/usr/share/fonts</dir><cachedir>/var/cache/fontconfig</cachedir></fontconfig>' > /etc/fonts/fonts.conf; }
export FONTCONFIG_PATH=/etc/fonts; fc-cache -f >/dev/null 2>&1 || true
ifc=$(ip route show default 2>/dev/null | awk '{print $5; exit}'); MAC=$(cat /sys/class/net/${ifc:-eth0}/address 2>/dev/null)
# signage mode (kb_signage on the kernel cmdline, set by the server's "signage" action): show the digital-signage player
# page instead of this machine's console. The page polls /signage/now and renders the zone's scheduled content fullscreen.
if grep -qw kb_signage /proc/cmdline 2>/dev/null; then URL="http://$S/signage-display.html?mac=$MAC"; else URL="http://$S/local?mac=$MAC"; fi
log "starting kiosk (kbweb) -> $URL"
kbweb "$URL" >>/run/kb-desktop.log 2>&1
rc=$?
[ -s /run/kb-desktop.log ] && sed 's/^/KBWEB: /' /run/kb-desktop.log > /dev/kmsg 2>/dev/null   # surface the kbweb log on the kernel/serial console for headless diagnosis
log "kbweb exited (rc=$rc) - falling back to kbgui"
exit 1
'''

INITTAB = """# KwickBoot RAM OS
::sysinit:/sbin/openrc sysinit
::sysinit:/sbin/openrc boot
::wait:/sbin/openrc default
tty1::respawn:/usr/local/bin/kb-console
tty2::respawn:/sbin/getty 38400 tty2
tty3::respawn:/sbin/getty 38400 tty3
ttyS0::respawn:/sbin/getty -L 115200 ttyS0 vt100
::ctrlaltdel:/sbin/reboot
::shutdown:/sbin/openrc shutdown
"""

ISSUE = "\nKwickBoot RAM OS (\\n) - emergency console\nThis machine is managed by KwickBoot. Normal work happens in the web console.\n\n"
MOTD = "\nKwickBoot RAM OS - running from memory; disks are untouched unless an install job runs.\nAgent log: /var/log/pxe-agent.log   Job logs: /root/tk/jobs/\n\n"


def overlay_files(server):
    """(path, bytes, mode) for the apkovl."""
    return [("etc/hostname", b"kwickboot\n", 0o644), ("etc/issue", ISSUE.encode(), 0o644), ("etc/motd", MOTD.encode(), 0o644),
            ("etc/inittab", INITTAB.encode(), 0o644), ("etc/kb-server", (server + "\n").encode(), 0o644),
            ("usr/local/bin/kb-console", CONSOLE.encode(), 0o755)]


def _read_newc(data):
    """Yield (name, mode, body) from a newc cpio stream (only what we need)."""
    off = 0
    while off + 110 <= len(data):
        h = data[off:off + 110]
        if h[:6] not in (b"070701", b"070702"):
            break
        f = [int(h[6 + i * 8:14 + i * 8], 16) for i in range(13)]
        mode, fsize, nsize = f[1], f[6], f[11]
        name = data[off + 110:off + 110 + nsize - 1].decode(errors="replace")
        off = (off + 110 + nsize + 3) & ~3
        body = data[off:off + fsize]
        off = (off + fsize + 3) & ~3
        if name == "TRAILER!!!":
            break
        yield name, mode, body


def _newc(entries):
    out = io.BytesIO()
    ino = 1
    for name, mode, body in entries + [("TRAILER!!!", 0, b"")]:
        nb = name.encode() + b"\0"
        hdr = "070701" + "".join(f"{v:08X}" for v in (ino, mode, 0, 0, 1, 0, len(body), 0, 0, 0, 0, len(nb), 0))
        out.write(hdr.encode() + nb)
        out.write(b"\0" * ((4 - (110 + len(nb)) % 4) % 4))
        out.write(body)
        out.write(b"\0" * ((4 - len(body) % 4) % 4))
        ino += 1
    return out.getvalue()


def build_init_overlay(initramfs, out):
    """Write kb-init.cpio.gz next to the initramfs. Returns a short status string."""
    with gzip.open(initramfs, "rb") as f:
        data = f.read()
    init = next((b for n, m, b in _read_newc(data) if n in ("init", "./init")), None)
    if init is None:
        return "init not found in initramfs - RAM OS not customised"
    s = init.decode()
    old_df = '''dev=$(df -P "$dir" | tail -1 | awk '{print $1}')'''
    # an HTTP-fetched overlay lives in /tmp, which is no mount point: df fails and its header line ("Filesystem")
    # would then be stat'ed -> skip such entries quietly instead
    new_df = '''dev=$(df -P "$dir" 2>/dev/null | tail -n +2 | tail -1 | awk '{print $1}'); [ -n "$dev" ] || continue'''
    patched = s.replace(old_df, new_df)
    patched = patched.replace('echo "Alpine Init $VERSION"', 'echo "KwickBoot RAM OS (Alpine Init $VERSION)"')
    n = (old_df in s and new_df in patched) + ("KwickBoot RAM OS (Alpine Init" in patched)
    tmp = out + ".tmp"
    with gzip.open(tmp, "wb", 9) as g:
        g.write(_newc([("init", 0o100755, patched.encode())]))
    os.replace(tmp, out)
    return f"{n} of 2 patches applied"
