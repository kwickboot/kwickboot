#!/bin/sh
# KwickBoot RAM OS agent - started automatically at boot.
# Reports hardware, then runs jobs queued on the server. No console interaction.
# KwickBoot Core: tools come as packs, downloaded only when a job or the hardware needs them.
SRV="http://{{SERVER_IP}}:{{HTTP_PORT}}"
TK=/root/tk; mkdir -p $TK/jobs
IFC=$(ip route show default 2>/dev/null | awk '{print $5; exit}')
MAC=$(cat /sys/class/net/${IFC:-eth0}/address 2>/dev/null)
ip4() { ip -4 -o addr show "$IFC" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1; }
# per-boot secret from the kernel command line; the server refuses agent calls without it
TOKEN=$(tr ' ' '\n' < /proc/cmdline | sed -n 's/^pxetoken=//p')
echo "$TOKEN" > $TK/token; chmod 600 $TK/token
curl() { command curl -H "X-PXE-Token: $TOKEN" "$@"; }
post() { curl -s -m 20 --data-binary "$2" "$SRV$1&mac=$MAC" >/dev/null 2>&1; }
hello() { printf '%s\n%s\n' "$1" "$2" > /run/kb-state 2>/dev/null; post "/agent/hello?state=$1&ip=$(ip4)" "$2" && touch /run/kb-seen; }
note()  { printf '%s\n%s\n' "$(sed -n 1p /run/kb-state 2>/dev/null)" "$*" > /run/kb-state.new && mv /run/kb-state.new /run/kb-state; }   # local screen only
log() { echo "$(date '+%T') $*"; }
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin   # pack tools win over busybox applets
# KwickBoot Core's PID 1 is a tiny init script, not an init system: reboot/power off directly after flushing disks
kb_reboot()   { sync; if [ -f /etc/kb-core-version ]; then reboot -f; else reboot; fi; }
kb_poweroff() { sync; if [ -f /etc/kb-core-version ]; then poweroff -f; else poweroff; fi; }
# need <pack>...: download (once per boot), verify SHA-256, add to the running system in RAM. No-op outside KwickBoot Core.
need() {
  [ -f /etc/kb-core-version ] || return 0
  for p in "$@"; do
    [ -e /run/kb-pack-$p ] && continue
    # one loader per pack: a background preload and a job may ask for the same pack at the same time
    while ! mkdir /run/kb-lock-$p 2>/dev/null; do sleep 1; done
    [ -e /run/kb-pack-$p ] && { rmdir /run/kb-lock-$p; continue; }
    # Trust the checksums BAKED INTO the (signed-UKI) initrd; they cannot be MITM'd. Only fall back to
    # the server's SHA256SUMS over HTTP when no baked file is present (legacy / unsigned boots).
    if [ -s /etc/kb-sums ]; then SUMS=/etc/kb-sums; else [ -s /run/kb-sums ] || curl -s -m 20 -o /run/kb-sums "$SRV/kbcore/SHA256SUMS"; SUMS=/run/kb-sums; fi
    want=$(awk -v f="packs/$p.sqfs" '$2==f{print $1}' "$SUMS")
    t0=$(date +%s); prev=$(sed -n 2p /run/kb-state 2>/dev/null); note "loading $p tools from the server..."
    curl -s -m 300 -o /tmp/$p.sqfs "$SRV/kbcore/packs/$p.sqfs" || { log "pack $p: download failed"; rmdir /run/kb-lock-$p; continue; }
    if [ -z "$want" ] && [ "$SUMS" = /etc/kb-sums ]; then log "pack $p: not in the signed checksums - refused"; rm -f /tmp/$p.sqfs; rmdir /run/kb-lock-$p; continue; fi
    if [ -n "$want" ] && [ "$(sha256sum /tmp/$p.sqfs | cut -d' ' -f1)" != "$want" ]; then log "pack $p: CHECKSUM MISMATCH - refused"; rm -f /tmp/$p.sqfs; rmdir /run/kb-lock-$p; continue; fi
    mkdir -p /run/pk-$p;   # own mount point per pack: a job and the background preload may load packs at the same time
    modprobe -q loop 2>/dev/null; modprobe -q squashfs 2>/dev/null
    if ! mount -t squashfs -o loop,ro /tmp/$p.sqfs /run/pk-$p; then log "pack $p: mount failed"; rm -f /tmp/$p.sqfs; rmdir /run/kb-lock-$p; continue; fi
    mods=0; [ -d /run/pk-$p/lib/modules ] && mods=1
    cp -a /run/pk-$p/. / ; umount /run/pk-$p; rm -f /tmp/$p.sqfs
    # real tools replace busybox applets of the same name - also for scripts that set their own PATH (debootstrap: /sbin first)
    for f in /usr/bin/* /usr/sbin/*; do b=${f##*/}; [ -L /bin/$b ] && [ "$(readlink /bin/$b)" = busybox ] && ln -sf "$f" /bin/$b; done
    if [ $mods = 1 ]; then          # new drivers: index them and bind them to the hardware present
      # controllers create their devices after they load (SATA links, NVMe namespaces, USB disks): disk class drivers always,
      # then repeat the scan until nothing new loads; later devices get their driver from mdev (modalias rule)
      depmod 2>/dev/null; for m in sd_mod sr_mod nvme usb-storage uas ext4 vfat; do modprobe -q $m 2>/dev/null; done
      n0=-1; for pass in 1 2 3 4 5; do
        for a in $(cat /sys/bus/*/devices/*/modalias 2>/dev/null | sort -u); do modprobe -q "$a" 2>/dev/null; done
        n=$(wc -l < /proc/modules); [ "$n" = "$n0" ] && break; n0=$n; sleep 1
      done
      grep -q MODALIAS /etc/mdev.conf 2>/dev/null || echo '$MODALIAS=.* 0:0 660 @modprobe -q -b "$MODALIAS"' >> /etc/mdev.conf
      mdev -s 2>/dev/null; sleep 1
    fi
    note "$prev"; touch /run/kb-pack-$p; rmdir /run/kb-lock-$p; log "pack $p loaded in $(( $(date +%s) - t0 ))s"
  done
}
# devices the Core drivers do not cover (no driver bound) -> load the full driver pack once
missing_drivers() { for d in /sys/bus/pci/devices/*; do [ -e $d/driver ] || case "$(cat $d/class)" in 0x01*|0x02*|0x0c03*) echo x; return ;; esac; done; }
# a driver that bound but could not fetch its firmware (Core/hw do not bake every blob) -> the on-demand firmware pack has it
fw_failed() { dmesg 2>/dev/null | grep -qiE 'Direct firmware load for .* failed|firmware: failed to load' && echo x; }

# graphical screens (web desktop kbweb / kbgui) paint pixels straight into /dev/fb0, so /dev/vcsa only holds the text
# left underneath them. While one runs, send the framebuffer instead: gzip -1 of the raw pixels (~100 KB for an 800x600
# desktop), base64 like the text screen, only when it changed. The browser unpacks it - no image work on the server.
fb_shot() {
  pidof kbweb kbgui kbrescue-fb >/dev/null 2>&1 || return 1   # desktop, kbgui or the Rescue menu UI
  [ -r /dev/fb0 ] || return 1
  m=$(sed -n 's/^[A-Za-z]*:\([0-9]*\)x\([0-9]*\).*/\1 \2/p' /sys/class/graphics/fb0/modes 2>/dev/null | head -n1)
  [ -n "$m" ] || m=$(tr ',' ' ' < /sys/class/graphics/fb0/virtual_size 2>/dev/null)
  set -- $m; fw=${1:-0}; fh=${2:-0}
  fs=$(cat /sys/class/graphics/fb0/stride 2>/dev/null); fbpp=$(cat /sys/class/graphics/fb0/bits_per_pixel 2>/dev/null)
  [ "$fw" -gt 0 ] 2>/dev/null && [ "$fh" -gt 0 ] && [ "${fs:-0}" -gt 0 ] || return 1
  head -c $((fs * fh)) /dev/fb0 | gzip -1 | base64 | tr -d '\n' > /run/kb-fb
  [ "$(wc -c < /run/kb-fb)" -le 3900000 ] || return 0          # over one post's cap (huge 4K+ frame): skip this one
  s=$(md5sum < /run/kb-fb)
  fbn=$(( ${fbn:-0} + 1 ))   # an unchanged frame is resent every ~5 s: a viewer who just opened the tab / reconnected still gets one
  { [ "$s" != "$last" ] || [ $((fbn % 5)) = 0 ]; } && curl -s -m 10 --data-binary @/run/kb-fb "$SRV/agent/screenfb?mac=$MAC&w=$fw&h=$fh&s=$fs&b=$fbpp" >/dev/null 2>&1 && last=$s
  return 0
}
screen_loop() {
  last=""
  hp=${SRV#http://}
  while :; do
    w=$(curl -s -m 5 "$SRV/agent/watch?mac=$MAC")
    # terminals an admin opened in the web console: kbpty dials back to the server with this boot's token
    for sid in $(echo "$w" | sed -n 's/^PTY \([0-9a-f]\{16\}\)$/\1/p'); do
      [ -e /run/kb-pty-$sid ] && continue
      touch /run/kb-pty-$sid
      command -v kbpty >/dev/null && ( kbpty "${hp%:*}" "${hp#*:}" "$MAC" "$TOKEN" "$sid"; rm -f /run/kb-pty-$sid ) >/dev/null 2>&1 &
    done
    # the manual-install VM's screen (VNC on 127.0.0.1:5900, only while a manual install runs): raw relay to the console
    for sid in $(echo "$w" | sed -n 's/^VNC \([0-9a-f]\{16\}\)$/\1/p'); do
      [ -e /run/kb-pty-$sid ] || [ ! -e /run/kb-vnc ] && continue      # VM not up yet: the next poll retries
      touch /run/kb-pty-$sid
      command -v kbpty >/dev/null && ( kbpty "${hp%:*}" "${hp#*:}" "$MAC" "$TOKEN" "$sid" tcp:5900; rm -f /run/kb-pty-$sid ) >/dev/null 2>&1 &
    done
    # remote control of THIS screen (web console): kbvnc listens on 127.0.0.1:5901 once /run/kb-uinput exists
    for sid in $(echo "$w" | sed -n 's/^RFB \([0-9a-f]\{16\}\)$/\1/p'); do
      [ -e /run/kb-pty-$sid ] || [ ! -e /run/kb-uinput ] && continue
      touch /run/kb-pty-$sid
      command -v kbpty >/dev/null && ( kbpty "${hp%:*}" "${hp#*:}" "$MAC" "$TOKEN" "$sid" tcp:5901; rm -f /run/kb-pty-$sid ) >/dev/null 2>&1 &
    done
    if [ "$(echo "$w" | head -n1)" = 1 ]; then
      for i in 1 2 3; do
        if fb_shot; then sleep 1; continue; fi                       # graphical screen up: its pixels, not the text under it
        # base64: Core's curl is busybox wget, whose upload stops at the first zero byte (screens are full of them)
        # a stable frame: two identical reads 0.2 s apart, so a screen caught mid-redraw is never sent
        p=""; for t in 1 2 3 4 5; do
          { cat /dev/vcsa 2>/dev/null || cat /dev/vcsa1; } | base64 | tr -d '\n' > /run/kb-scr
          c=$(md5sum < /run/kb-scr); [ "$c" = "$p" ] && break; p=$c; sleep 0.2
        done
        s=$(md5sum < /run/kb-scr)
        txn=$(( ${txn:-0} + 1 ))   # same for the text screen: resend unchanged every ~5 s while watched
        { [ "$s" != "$last" ] || [ $((txn % 5)) = 0 ]; } && curl -s -m 5 --data-binary @/run/kb-scr "$SRV/agent/screen?mac=$MAC" >/dev/null 2>&1 && last=$s
        sleep 1
      done
    else last=""; sleep 3; fi
  done
}

# An OS-library tool (Rescue & Wipe) network-booted from the server runs ONLY this part of the agent: the web console's
# Screen view, Remote control and terminal. No inventory, no jobs, nothing that touches a disk.
if [ -n "${KB_SCREEN_ONLY:-}" ]; then
  log "screen-only agent for tool $KB_SCREEN_ONLY, mac=$MAC ip=$(ip4) server=$SRV"
  ( while :; do hello busy "$KB_SCREEN_ONLY tool running"; sleep 30; done ) &
  screen_loop
  exit 0
fi

log "agent start mac=$MAC ip=$(ip4) server=$SRV"
# keep the DHCP lease alive for long jobs
pgrep -x udhcpc >/dev/null || udhcpc -b -q -i "$IFC" -t 10 >/dev/null 2>&1 &

hello booting "RAM OS up, scanning hardware"
need hw                                   # storage, filesystems, input + scan tools (Core itself = network + loader)
# UEFI variables (boot order after installs): the efivarfs driver arrives with the hw pack, mount it now
[ -d /sys/firmware/efi ] && ! grep -q " /sys/firmware/efi/efivars " /proc/mounts && { modprobe -q efivarfs 2>/dev/null; mount -t efivarfs efivarfs /sys/firmware/efi/efivars 2>/dev/null; }
# extended hardware: fetch its firmware first (so the drivers bind with firmware present), then the driver pack
[ -n "$(missing_drivers)" ] && { need firmware; need drivers; }
# a device whose driver loaded but whose firmware Core lacks: pull the firmware pack and re-trigger driver binding
if [ -n "$(fw_failed)" ]; then
  need firmware
  for a in $(cat /sys/bus/*/devices/*/modalias 2>/dev/null | sort -u); do modprobe -q "$a" 2>/dev/null; done
fi
for i in 1 2 3; do wget -qO /tmp/prep.sh "$SRV/scripts/prep.sh" && sh /tmp/prep.sh > $TK/prep.log 2>&1 && break; sleep 10; done
# prep.sh already posted the inventory; add an initial preflight (no target disk yet)
sh $TK/preflight.sh > $TK/preflight.txt 2>&1
curl -s -m 20 --data-binary @$TK/preflight.txt "$SRV/report?kind=preflight&mac=$MAC" >/dev/null
hello ready "inventory + preflight reported"
log "ready; polling for jobs"
# full detail (SMART, NVMe health, sensors) arrives a moment later, without delaying READY
# then preload the install pack (9 MB of RAM) so an install job starts writing the disk immediately
[ -f /etc/kb-core-version ] && ( need diag && sh $TK/inventory.sh > $TK/inventory.txt 2>&1 \
   && curl -s -m 20 --data-binary @$TK/inventory.txt "$SRV/report?kind=inventory&mac=$MAC" >/dev/null && log "detailed inventory reported"; need install ) &

# screen view in the web console: while someone watches, send the visible text screen (/dev/vcsa = the console now on
# the monitor) about once a second when it changed. Its own loop, so it keeps running while a job (an install) runs.
screen_loop &

run_job() {   # $1=id $2=type, params exported as env
  id=$1; type=$2; L=$TK/jobs/$id.log; : > "$L"; echo "$L" > /run/kb-joblog
  ( last=""; while kill -0 $$ 2>/dev/null && [ ! -f "$L.done" ]; do
      s=$(stat -c %s.%Y "$L" 2>/dev/null)
      [ "$s" != "$last" ] && curl -s -m 20 --data-binary @"$L" "$SRV/agent/log?job=$id&mac=$MAC" >/dev/null 2>&1 && last=$s
      sleep 3; done ) &
  poster=$!
  rc=0
  case "$type" in
    inventory) need diag; sh $TK/inventory.sh > $TK/inventory.txt 2>&1; rc=$?; cat $TK/inventory.txt >> "$L"
               curl -s -m 20 --data-binary @$TK/inventory.txt "$SRV/report?kind=inventory&mac=$MAC" >/dev/null ;;
    preflight) need diag; DISK="$DISK" sh $TK/preflight.sh >> "$L" 2>&1; rc=$?
               cp "$L" $TK/preflight.txt; curl -s -m 20 --data-binary @$TK/preflight.txt "$SRV/report?kind=preflight&mac=$MAC" >/dev/null ;;
    stress)    need diag; wget -qO $TK/stress.sh "$SRV/scripts/stress.sh"; MIN="$MIN" sh $TK/stress.sh >> "$L" 2>&1; rc=$? ;;
    install)   need install diag; [ "$OS" = windows ] && need imaging      # Windows: wimlib + NTFS tools
               wget -qO $TK/install.sh "$SRV/scripts/install.sh"
               OS="$OS" DISK="$DISK" CONFIRM="$CONFIRM" HOSTNAME="$HOSTNAME" TZ="${TZ:-America/Chicago}" LAN="$LAN" LOCALE="$LOCALE" KEYMAP="$KEYMAP" \
                 IMAGE="$IMAGE" SHA256="$SHA256" BUNDLE="$BUNDLE" VARIANT="$VARIANT" KIOSK_URL_B64="$KIOSK_URL_B64" KIOSK_ALLOW_B64="$KIOSK_ALLOW_B64" KIOSK_IDLE="$KIOSK_IDLE" KIOSK_RO="$KIOSK_RO" EP_TOKEN="$EP_TOKEN" FORCE="$FORCE" ISO="$ISO" WIN_EULA="$WIN_EULA" WIN_DEBLOAT="$WIN_DEBLOAT" WIN_FB="$WIN_FB" WIN_USER="$WIN_USER" WIN_APPS="$WIN_APPS" WIN_C_GB="$WIN_C_GB" WIN_PARTS="$WIN_PARTS" WIN_DISKS="$WIN_DISKS" WIN_DISK_SERIALS="$WIN_DISK_SERIALS" MODE="${MODE:-unicast}" PORTBASE="$PORTBASE" WIN_JOIN="$WIN_JOIN" WIN_DRIVERS="$WIN_DRIVERS" BOOT_ORDER="$BOOT_ORDER" sh $TK/install.sh >> "$L" 2>&1; rc=$? ;;
    wipe)      need diag imaging; wget -qO $TK/wipe.sh "$SRV/scripts/wipe.sh"
               DISKS="$DISKS" CONFIRMS="$CONFIRMS" sh $TK/wipe.sh >> "$L" 2>&1; rc=$?
               [ $rc -eq 0 ] && [ "$FIN" = poweroff ] && (sleep 8; hello off "wiped - powered off"; kb_poweroff) & ;;
    winprep)   need imaging; wget -qO $TK/winprep.sh "$SRV/scripts/winprep.sh"
               ISO="$ISO" EDITION="$EDITION" SCRATCH="$DISK" CONFIRM="$CONFIRM" JOB="$id" sh $TK/winprep.sh >> "$L" 2>&1; rc=$? ;;
    capture)   need imaging install; wget -qO $TK/capture.sh "$SRV/scripts/capture.sh"
               NAME="$NAME" DISK="$DISK" CONFIRM="$CONFIRM" JOB="$id" sh $TK/capture.sh >> "$L" 2>&1; rc=$? ;;
    restore)   need imaging install; wget -qO $TK/restore.sh "$SRV/scripts/restore.sh"
               NAME="$NAME" DISK="$DISK" CONFIRM="$CONFIRM" MODE="${MODE:-unicast}" PORTBASE="$PORTBASE" HOSTNAME="$HOSTNAME" BOOT_ORDER="$BOOT_ORDER" sh $TK/restore.sh >> "$L" 2>&1; rc=$? ;;
    manual)    need virt diag; wget -qO $TK/manual.sh "$SRV/scripts/manual.sh"
               ISO="$ISO" DISK="$DISK" CONFIRM="$CONFIRM" FW="$FW" DISKBUS="$DISKBUS" sh $TK/manual.sh >> "$L" 2>&1; rc=$? ;;
    reboot)    echo "rebooting in 5s" >> "$L"; (sleep 4; hello rebooting "restart requested from the web console"; kb_reboot) & ;;
    poweroff)  echo "powering off in 5s" >> "$L"; (sleep 4; hello off "power off requested from the web console"; kb_poweroff) & ;;
    *)         echo "job type '$type' not allowed" >> "$L"; rc=4 ;;
  esac
  echo "=== job $id finished rc=$rc ===" >> "$L"; touch "$L.done"; sleep 1; kill $poster 2>/dev/null
  curl -s -m 20 --data-binary @"$L" "$SRV/agent/log?job=$id&mac=$MAC" >/dev/null
  curl -s -m 20 -d "" "$SRV/agent/done?job=$id&rc=$rc&mac=$MAC" >/dev/null
  log "job $id ($type) rc=$rc"
  # an install that ends with FINISH=reboot: reboot after reporting
  { [ "$type" = install ] || [ "$type" = manual ] || [ "$type" = restore ]; } && [ $rc -eq 0 ] && [ "${FIN:-reboot}" = reboot ] && { hello rebooting "install done, rebooting into installed OS"; sleep 3; kb_reboot; }
}

while true; do
  J=$(curl -s -m 20 "$SRV/agent/job?mac=$MAC")
  if [ -n "$J" ] && [ "$J" != "{}" ]; then
    # server sends shell-safe KEY='value' lines (validated server side)
    id=$(echo "$J" | sed -n "s/^JOB_ID='\(.*\)'$/\1/p"); type=$(echo "$J" | sed -n "s/^JOB_TYPE='\(.*\)'$/\1/p")
    # every job starts clean: values of an earlier job (bundle, token, kiosk page...) must never carry over
    JOB_VARS="OS DISK CONFIRM HOSTNAME TZ LAN LOCALE KEYMAP FIN IMAGE SHA256 BUNDLE VARIANT KIOSK_URL_B64 KIOSK_ALLOW_B64 KIOSK_IDLE KIOSK_RO EP_TOKEN ISO NAME MODE PORTBASE FW DISKBUS DISKS CONFIRMS FORCE WIN_EULA WIN_DEBLOAT WIN_FB WIN_USER WIN_APPS WIN_C_GB WIN_PARTS WIN_DISKS WIN_DISK_SERIALS EDITION WIN_JOIN WIN_DRIVERS MIN BOOT_ORDER"
    unset $JOB_VARS
    eval "$(echo "$J" | grep -E "^($(echo $JOB_VARS | tr ' ' '|'))='[A-Za-z0-9_./:+=-]*'$")"
    hello busy "running job $id ($type)"
    run_job "$id" "$type"
    hello ready "idle"; beat=0
  else
    beat=$(( ${beat:-0} + 1 )); [ $beat -ge 10 ] && { hello ready "idle"; beat=0; }   # heartbeat ~30 s
    # the server was updated while this machine sat in the RAM OS: load the new agent (idle only, never during a job),
    # so job settings a newer server sends are not silently dropped by an old agent
    upd=$(( ${upd:-0} + 1 ))
    if [ $upd -ge 20 ] && [ -f "$0" ]; then
      upd=0
      if curl -s -m 20 -o /run/kb-agent.new "$SRV/scripts/agent.sh" && [ -s /run/kb-agent.new ] && ! cmp -s /run/kb-agent.new "$0" \
         && head -n1 /run/kb-agent.new | grep -q '^#!/bin/sh' && sh -n /run/kb-agent.new; then
        log "server has a newer agent - reloading"; cp /run/kb-agent.new "$0"; exec sh "$0"
      fi
    fi
  fi
  sleep 3            # job pickup latency; one tiny HTTP request per poll
done
