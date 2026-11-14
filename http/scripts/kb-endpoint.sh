#!/bin/sh
# KwickBoot endpoint agent for installed Linux systems (servers, desktops, lab PCs).
# SPDX-License-Identifier: AGPL-3.0-or-later
# Runs every 30 s from kb-endpoint.timer: checks in with the KwickBoot server and runs commands queued by an admin in the
# web console. Needs only sh + curl. The server sends plain "id cmd arg" lines (validated server side: no shell syntax).
# Passwords never travel or rest in plain text: the server sends a SHA-512 crypt hash, applied with chpasswd -e.
# Channel: HTTPS to URL, trusting only the server key PIN given at install (-k skips the CA check, the pin replaces it).
# Machines installed before the pinned channel existed have no PIN and keep plain HTTP.
. /etc/kb-endpoint.conf
URL=${URL:-http://$SERVER}
epcurl() { if [ -n "$PIN" ]; then curl -sf -k --pinnedpubkey "$PIN" -H "X-KB-EP: $TOKEN" "$@"; else curl -sf -H "X-KB-EP: $TOKEN" "$@"; fi; }
dev=$(ip route get "${SERVER%:*}" 2>/dev/null | sed -n 's/.* dev \([^ ]*\).*/\1/p' | head -n1); [ -n "$dev" ] || dev=$(ip route show default 2>/dev/null | awk '{print $5; exit}'); MAC=$(cat /sys/class/net/${dev:-none}/address 2>/dev/null) || exit 0
result() { epcurl -m 10 --data-binary "$2" "$URL/ep/result?mac=$MAC&id=$1" >/dev/null; }
# "kb-endpoint report <id> <text>": late result of a long command (update) over the same pinned channel
if [ "$1" = report ]; then case "$2" in ''|*[!0-9]*) exit 1 ;; esac; result "$2" "$3"; exit; fi
. /etc/os-release 2>/dev/null
# pending updates: counted from apt's own lists (apt-daily refreshes them), at most once an hour - no download here
mkdir -p /var/lib/kb-endpoint
[ -n "$(find /var/lib/kb-endpoint/updates -mmin -60 2>/dev/null)" ] || apt-get -s full-upgrade 2>/dev/null | grep -c "^Inst " > /var/lib/kb-endpoint/updates
upd=$(cat /var/lib/kb-endpoint/updates 2>/dev/null)
rb=no; [ -f /var/run/reboot-required ] && rb=yes
users=$(awk -F: '$3>=1000 && $3<60000 {printf "%s%s", s, $1; s=","}' /etc/passwd)
enc() { printf '%s' "$1" | sed 's/%/%25/g; s/+/%2B/g; s/&/%26/g; s/ /+/g'; }     # URL query value
q="mac=$MAC&fmt=sh&up=$(cut -d. -f1 /proc/uptime)&os=$(enc "${PRETTY_NAME:-linux}")&kernel=$(enc "$(uname -r)")&updates=${upd:-}&reboot=$rb&users=$users"
KIOSK=""; [ -x /usr/local/sbin/kiosk-apply ] && KIOSK=1      # kiosk extension: profile, page reload, screenshot
[ -n "$KIOSK" ] && q="$q&kiosk=$(systemctl is-active kiosk.service)&ver=$(cat /run/kb-profile-version 2>/dev/null || echo -1)"
L=$(epcurl -m 15 "$URL/ep/checkin?$q") || exit 0

echo "$L" | while read -r id cmd arg arg2; do
  case "$id" in ''|*[!0-9]*) continue ;; esac
  if [ "$cmd" = profile ]; then        # "0 profile <version> <base64 JSON|->": kiosk start page, allowed sites, idle reset
    [ -n "$KIOSK" ] || continue
    cur=$(cat /run/kb-profile-version 2>/dev/null || echo -1); [ "$arg" = "$cur" ] && continue
    if [ "$arg" = 0 ] || [ "$arg2" = - ]; then cp /etc/kiosk.install.json /etc/kiosk.json
    else printf '%s' "$arg2" | base64 -d > /etc/kiosk.json.new && jq -e .url /etc/kiosk.json.new >/dev/null && mv /etc/kiosk.json.new /etc/kiosk.json || continue; fi
    /usr/local/sbin/kiosk-apply && systemctl try-restart kiosk.service; echo "$arg" > /run/kb-profile-version
    continue
  fi
  u=${arg%%:*}; rest=${arg#*:}
  case "$u" in ''|*[!a-z0-9_-]*) u="" ;; esac
  case "$cmd" in
    reboot)
      result "$id" "rebooting"; systemctl reboot; exit 0 ;;
    netboot)
      # one-time network boot (a disk-first department): the firmware's network entry for the NEXT start only
      n=$(efibootmgr 2>/dev/null | grep -iE '^Boot[0-9A-F]{4}\*? .*(PXE|IPv4|Network|NIC)' | grep -viE 'IPv6|HTTP' | head -n1 | cut -c5-8)
      if [ -n "$n" ] && efibootmgr -q -n "$n"; then result "$id" "network boot set for the next start - rebooting"; systemctl reboot; exit 0
      else result "$id" "no network boot entry in the firmware (or not UEFI) - use the boot menu key"; fi ;;
    update)
      # can take minutes: run outside this 20 s check-in, report when finished
      systemd-run --quiet --no-block --unit="kb-update-$id" --property=TimeoutStartSec=3600 /bin/sh -c '
        export DEBIAN_FRONTEND=noninteractive
        if apt-get update -q >/tmp/kb-upd.log 2>&1 && apt-get -y -q -o Dpkg::Options::=--force-confold full-upgrade >>/tmp/kb-upd.log 2>&1; then
          n=$(grep -o "^[0-9]* upgraded" /tmp/kb-upd.log | tail -n1); r="updates installed: ${n:-0 upgraded}"
        else r="update FAILED: $(tail -n 3 /tmp/kb-upd.log | tr "\n" " ")"; fi
        [ -f /var/run/reboot-required ] && r="$r - reboot required"
        mkdir -p /var/lib/kb-endpoint; apt-get -s full-upgrade 2>/dev/null | grep -c "^Inst " > /var/lib/kb-endpoint/updates
        /usr/local/sbin/kb-endpoint report '"$id"' "$r"' \
        && result "$id" "updating..." || result "$id" "could not start the update" ;;
    setpw)
      if [ -z "$u" ] || ! id "$u" >/dev/null 2>&1; then result "$id" "no such user: $u"
      elif printf '%s:%s\n' "$u" "$rest" | chpasswd -e 2>/dev/null; then usermod -U "$u" 2>/dev/null; result "$id" "password set for $u"
      else result "$id" "password change failed for $u"; fi ;;
    adduser)
      h=${rest%:*}; adm=${rest##*:}
      if [ -z "$u" ]; then result "$id" "bad user name"
      elif id "$u" >/dev/null 2>&1; then result "$id" "user $u already exists (use Set password)"
      elif useradd -m -s /bin/bash "$u" 2>/dev/null && printf '%s:%s\n' "$u" "$h" | chpasswd -e 2>/dev/null; then
        g=""; [ "$adm" = 1 ] && for x in sudo wheel; do getent group $x >/dev/null && usermod -aG $x "$u" && g=" (admin)" && break; done
        result "$id" "user $u created$g"
      else result "$id" "could not create $u"; fi ;;
    lockuser)
      if [ -n "$u" ] && [ "$u" != root ] && id "$u" >/dev/null 2>&1 && usermod -L -e 1 "$u" 2>/dev/null; then
        pkill -KILL -u "$u" 2>/dev/null; result "$id" "user $u locked and logged out"
      else result "$id" "could not lock $u"; fi ;;
    unlockuser)
      if [ -n "$u" ] && id "$u" >/dev/null 2>&1 && usermod -U -e "" "$u" 2>/dev/null; then result "$id" "user $u unlocked"
      else result "$id" "could not unlock $u"; fi ;;
    checkupdates)
      mkdir -p /var/lib/kb-endpoint
      n=$(apt-get update -q >/dev/null 2>&1; apt-get -s full-upgrade 2>/dev/null | grep -c "^Inst ")
      echo "$n" > /var/lib/kb-endpoint/updates; result "$id" "$n updates available" ;;
    reload)
      [ -n "$KIOSK" ] && systemctl restart kiosk.service && result "$id" "kiosk restarted" || result "$id" "not a kiosk" ;;
    screenshot)
      f=/run/kb-screen.png; ku=$(id -u kiosk 2>/dev/null)
      if [ -n "$ku" ] && runuser -u kiosk -- env XDG_RUNTIME_DIR=/run/user/$ku WAYLAND_DISPLAY=wayland-0 grim "$f" 2>/dev/null; then
        epcurl -m 30 --data-binary @"$f" "$URL/ep/screen?mac=$MAC" >/dev/null && result "$id" "screenshot uploaded" || result "$id" "upload failed"
      else result "$id" "screenshot failed (no kiosk display)"; fi ;;
    *) result "$id" "unknown command: $cmd" ;;
  esac
done
