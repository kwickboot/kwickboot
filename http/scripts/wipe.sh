#!/bin/sh
# KwickBoot secure wipe - runs inside the RAM OS. NIST SP 800-88 Rev. 2 (September 2025): Purge with the strongest
# technique IEEE 2883 lists for the disk's interface (sanitize crypto erase / block erase, secure erase), Clear (one
# overwrite) only when no purge command works; verified (completion status + device health + sampled read-back).
# SPDX-License-Identifier: AGPL-3.0-or-later
#   DISKS=/dev/a+/dev/b CONFIRMS=<serial a>+<serial b> sh wipe.sh
# Per disk: confirm serial -> fingerprint 64 random sectors + first/last MB -> wipe -> re-read and verify ->
# one line "WIPECERT {json}" that the server turns into a signed certificate. A disk that fails verification is reported FAILED.
SRV="http://{{SERVER_IP}}:{{HTTP_PORT}}"
: "${DISKS:?set DISKS}" "${CONFIRMS:?set CONFIRMS}"
KB_T0=$(date +%s)
step() { echo; echo ">>> [$1] $2   (+$(( $(date +%s) - KB_T0 ))s)"; }
die()  { echo "STOP: $*"; exit 1; }
TMPPW=KwickBootWipe   # ATA Secure Erase needs a drive password; the erase removes it again (printed so an interrupted drive can be unlocked)
fails=0

serial_of() { n=$(basename "$1"); s=$(lsblk -dno SERIAL "$1" 2>/dev/null | tr -d ' ')
  for f in /sys/block/$n/serial /sys/block/$n/device/serial; do [ -z "$s" ] && [ -r "$f" ] && s=$(tr -d ' \n' < "$f"); done
  [ -z "$s" ] && [ -r /sys/block/$n/device/vpd_pg80 ] && s=$(tail -c +5 /sys/block/$n/device/vpd_pg80 | tr -d ' \n\000')
  [ -n "$s" ] || s="size-$(lsblk -dno SIZE "$1" | tr -d ' ')"; echo "$s"; }

# fingerprint: sha256 of 64 random 4 KiB blocks + first and last MiB (positions kept to re-read after the wipe)
sample() {   # $1 dev $2 positions-file -> prints one sha per line
  while read -r pos; do dd if="$1" bs=4096 skip="$pos" count=1 iflag=direct 2>/dev/null | sha256sum | cut -c1-16; done < "$2"; }
zero_count() { while read -r pos; do dd if="$1" bs=4096 skip="$pos" count=1 iflag=direct 2>/dev/null | tr -d '\000' | wc -c; done < "$2" | awk '$1==0{z++} END{print z+0}'; }

step 1 "confirm disks"
i=1
for d in $(echo "$DISKS" | tr '+' ' '); do
  want=$(echo "$CONFIRMS" | cut -d+ -f$i); i=$((i + 1))
  [ -b "$d" ] || die "$d is not a block device - nothing wiped"
  [ "$(serial_of "$d")" = "$want" ] || die "CONFIRM '$want' does not match $d serial '$(serial_of "$d")' - nothing wiped"
  lsblk -nro MOUNTPOINTS "$d" | grep -q . && die "$d has mounted filesystems - nothing wiped"
  echo "  OK    $d serial $want $(lsblk -dno SIZE "$d" | tr -d ' ') $(lsblk -dno MODEL "$d" 2>/dev/null)"
done

for d in $(echo "$DISKS" | tr '+' ' '); do
  n=$(basename "$d"); ser=$(serial_of "$d"); model=$(lsblk -dno MODEL "$d" 2>/dev/null | tr -cd 'A-Za-z0-9 ._-')
  bytes=$(( $(cat /sys/class/block/$n/size) * 512 )); blocks=$(( bytes / 4096 ))
  rot=$(cat /sys/block/$n/queue/rotational 2>/dev/null); kind=$([ "$rot" = 1 ] && echo HDD || echo SSD); case "$n" in nvme*) kind=NVMe ;; esac
  step 2 "wipe $d ($kind, serial $ser)"
  t1=$(date +%s)
  # positions: 64 random + first 256 + last 256 blocks
  awk -v n="$blocks" -v seed="$(date +%s)$$" 'BEGIN{srand(seed); for(i=0;i<64;i++) print int(rand()*(n-1)); for(i=0;i<256;i++){print i; print n-1-i}}' > /tmp/kbwipe.pos
  sample "$d" /tmp/kbwipe.pos > /tmp/kbwipe.before
  method=""; level=""; expect=zero
  if [ "$kind" = NVMe ] && command -v nvme >/dev/null; then
    ctrl=$(echo "$n" | sed 's/n[0-9]*$//')                      # nvme0n1 -> nvme0
    cap=$(nvme id-ctrl /dev/$ctrl -H 2>/dev/null)
    if echo "$cap" | grep -qi 'Crypto Erase Sanitize Operation Supported'; then
      nvme sanitize /dev/$ctrl -a 0x04 >/dev/null 2>&1 && method="NVMe Sanitize - Crypto Erase" && level=Purge && expect=changed
    fi
    if [ -z "$method" ] && echo "$cap" | grep -qi 'Block Erase Sanitize Operation Supported'; then
      nvme sanitize /dev/$ctrl -a 0x02 >/dev/null 2>&1 && method="NVMe Sanitize - Block Erase" && level=Purge
    fi
    if [ -n "$method" ]; then
      for t in $(seq 1 3600); do nvme sanitize-log /dev/$ctrl 2>/dev/null | grep -qiE 'Sanitize Progress.*65535|Most Recent Sanitize Command Completed Successfully' && break; sleep 2; done
    else
      if nvme format "$d" -s 2 -f >/dev/null 2>&1; then method="NVMe Format - Cryptographic Erase"; level=Purge; expect=changed
      elif nvme format "$d" -s 1 -f >/dev/null 2>&1; then method="NVMe Format - User Data Erase"; level=Purge; fi
    fi
  elif command -v hdparm >/dev/null && hdparm -I "$d" >/tmp/kbwipe.ata 2>/dev/null && grep -q 'Security:' /tmp/kbwipe.ata; then
    if grep -qiE '^\s*frozen' /tmp/kbwipe.ata && ! grep -qiE 'not\s+frozen' /tmp/kbwipe.ata; then
      echo "  NOTE  the drive's security is frozen by the firmware at boot - ATA erase not possible, overwriting instead"
    elif grep -qi 'CRYPTO_SCRAMBLE_EXT command is supported' /tmp/kbwipe.ata && hdparm --yes-i-know-what-i-am-doing --sanitize-crypto-scramble "$d" >/dev/null 2>&1; then
      method="ATA Sanitize - Crypto Scramble"; level=Purge; expect=changed
    elif grep -qi 'BLOCK_ERASE_EXT command is supported' /tmp/kbwipe.ata && hdparm --yes-i-know-what-i-am-doing --sanitize-block-erase "$d" >/dev/null 2>&1; then
      method="ATA Sanitize - Block Erase"; level=Purge
    elif grep -qi 'supported: enhanced erase' /tmp/kbwipe.ata; then
      echo "  temporary ATA password for the erase: $TMPPW (removed by the erase; use it to unlock if the wipe is interrupted)"
      hdparm --user-master u --security-set-pass "$TMPPW" "$d" >/dev/null 2>&1 && \
        hdparm --user-master u --security-erase-enhanced "$TMPPW" "$d" >/dev/null 2>&1 && method="ATA Secure Erase (enhanced)" && level=Purge && expect=changed
    elif grep -qi 'Security:' /tmp/kbwipe.ata && grep -qiE '^\s*supported' /tmp/kbwipe.ata; then
      echo "  temporary ATA password for the erase: $TMPPW (removed by the erase; use it to unlock if the wipe is interrupted)"
      hdparm --user-master u --security-set-pass "$TMPPW" "$d" >/dev/null 2>&1 && \
        hdparm --user-master u --security-erase "$TMPPW" "$d" >/dev/null 2>&1 && method="ATA Secure Erase" && level=Purge
    fi
    # sanitize runs in the drive's background: wait for it to finish
    case "$method" in "ATA Sanitize"*) for t in $(seq 1 7200); do hdparm --sanitize-status "$d" 2>/dev/null | grep -qi 'in progress' || break; sleep 2; done ;; esac
  fi
  if [ -z "$method" ]; then
    # NIST SP 800-88 Rev. 2 Clear: one full overwrite (multi-pass patterns such as DoD 5220.22-M are obsolete - Rev. 2, 3.1.1)
    echo "  overwriting all $((bytes / 1000000000)) GB with zeros (progress every 30 s)"
    if command -v pv >/dev/null; then pv -n -i 30 -s "$bytes" /dev/zero 2>/tmp/kbwipe.pv | dd of="$d" bs=4M iflag=fullblock oflag=direct count=$(( (bytes + 4194303) / 4194304 )) 2>/dev/null &
      while kill -0 $! 2>/dev/null; do sleep 30; p=$(tail -n1 /tmp/kbwipe.pv 2>/dev/null); [ -n "$p" ] && echo "  ... $p %"; done; wait
    else dd if=/dev/zero of="$d" bs=4M oflag=direct 2>/dev/null; fi
    sync; method="Overwrite - zeros, 1 pass"; level=Clear
  fi
  step 3 "verify $d"
  sample "$d" /tmp/kbwipe.pos > /tmp/kbwipe.after
  total=$(wc -l < /tmp/kbwipe.pos)
  same=$(paste -d' ' /tmp/kbwipe.before /tmp/kbwipe.after | awk '$1==$2{s++} END{print s+0}')
  zeros=$(zero_count "$d" /tmp/kbwipe.pos)
  # sectors that were zero already stay zero: those cannot be told apart for "changed" checks - ignore them there
  before_zero=$(dd if=/dev/zero bs=4096 count=1 2>/dev/null | sha256sum | cut -c1-16)
  if [ "$expect" = zero ]; then ok=$([ "$zeros" = "$total" ] && echo PASS || echo FAIL); detail="$zeros of $total sampled blocks read back as zeros"
  else
    changed_or_zero=$(paste -d' ' /tmp/kbwipe.before /tmp/kbwipe.after | awk -v z="$before_zero" '$1!=$2 || $1==z{c++} END{print c+0}')
    ok=$([ "$changed_or_zero" = "$total" ] && echo PASS || echo FAIL); detail="$changed_or_zero of $total sampled blocks changed (cryptographic erase leaves unreadable data)"
  fi
  # Rev. 2 4.5.1: completion status + errors + the device's health
  health="not reported"
  if command -v smartctl >/dev/null; then
    h=$(smartctl -H "$d" 2>/dev/null | sed -n 's/.*\(overall-health self-assessment test result\|SMART Health Status\): *//p' | head -n1)
    [ -n "$h" ] && health=$h
  fi
  case "$method" in
    *Crypto*|*Cryptographic*) technique="cryptographic erase (CE)" ;; *"Block Erase"*) technique="block erase" ;;
    Overwrite*) technique="overwrite" ;; *"User Data Erase"*|*"Secure Erase"*) technique="device erase command (vendor-defined)" ;; *) technique="$method" ;;
  esac
  vendor=$(lsblk -dno VENDOR "$d" 2>/dev/null | tr -cd 'A-Za-z0-9 ._-' | sed 's/ *$//')
  [ "$ok" = PASS ] || fails=$((fails + 1))
  echo "  $ok  $method ($level, $technique) - $detail; health: $health"
  # one record per disk - the fields of a certificate of sanitization (NIST SP 800-88 Rev. 2, Sec. 4.6)
  printf 'WIPECERT {"device":"%s","serial":"%s","model":"%s","manufacturer":"%s","bytes":%s,"type":"%s","method":"%s","technique":"%s","nist":"%s","verify":"%s","verify_detail":"%s","health":"%s","tool":"%s","started":%s,"finished":%s,"machine":{"vendor":"%s","product":"%s","serial":"%s"}}\n' \
    "$d" "$ser" "$model" "$vendor" "$bytes" "$kind" "$method" "$technique" "$level" "$ok" "$detail" "$(echo "$health" | tr -cd 'A-Za-z0-9 ._-')" \
    "KwickBoot wipe.sh $(cat /etc/kwickboot-version 2>/dev/null)" "$t1" "$(date +%s)" \
    "$(cat /sys/class/dmi/id/sys_vendor 2>/dev/null | tr -cd 'A-Za-z0-9 ._-')" "$(cat /sys/class/dmi/id/product_name 2>/dev/null | tr -cd 'A-Za-z0-9 ._-')" \
    "$(cat /sys/class/dmi/id/product_serial 2>/dev/null | tr -cd 'A-Za-z0-9 ._-')"
done

step 8 "finish"
[ "$fails" = 0 ] || die "$fails disk(s) FAILED verification - no certificate for them, wipe again or destroy physically"
echo "WIPE DONE: $(echo "$DISKS" | tr '+' ' ') - certificates are in the web console"
