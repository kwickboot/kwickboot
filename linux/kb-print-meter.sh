#!/bin/sh
# kb-print-meter - KwickBoot print accounting meter for diskless labs.
# Runs on the lab's CUPS print server. Tails the CUPS page log and reports each printed page to the KwickBoot server,
# which charges the seat's signed-in member (the prepaid balance IS the print quota). One open box: seats + billing + print.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Setup (once, on the print server):
#   1) In /etc/cups/cupsd.conf set a PageLogFormat that includes the billing tag and originating host, e.g.:
#        PageLogFormat "%p %u %j %T %P %C %{job-billing} %{job-originating-host-name} %{job-name}"
#      then: systemctl restart cups
#   2) Have each diskless seat tag its jobs with the member code so we know whose quota to charge. The seat already
#      knows the member code from sign-in; set it as the default billing for the session, e.g. at login:
#        lpoptions -o job-billing="$MEMBER_CODE"           # per-user default
#      (Alternatively tag with the seat MAC: job-billing="$(cat /sys/class/net/*/address|head -1)".)
#   3) Run this meter:  KB_SERVER=192.168.0.234:8080  kb-print-meter.sh   (or install the systemd unit below)
#
# Env: KB_SERVER (host:port of the KwickBoot server, required), PAGELOG (default /var/log/cups/page_log).
set -eu
: "${KB_SERVER:?set KB_SERVER=host:port}"
PAGELOG="${PAGELOG:-/var/log/cups/page_log}"
is_mac() { printf '%s' "$1" | grep -qiE '^([0-9a-f]{2}:){5}[0-9a-f]{2}$'; }

report() {   # printer user job date page copies billing host jobname...
    page="$5"; copies="$6"; billing="${7:-}"; host="${8:-}"
    shift 8 2>/dev/null || true
    job="$*"
    [ "$page" = "total" ] && return 0          # the per-job summary line, not a page
    [ -z "$copies" ] && copies=1
    # decide who to charge: a MAC billing tag -> mac=; otherwise treat billing as a member code; fall back to host as mac
    mac=""; code=""
    if is_mac "$billing"; then mac="$billing"
    elif [ -n "$billing" ] && [ "$billing" != "-" ]; then code="$billing"
    elif is_mac "$host"; then mac="$host"
    fi
    [ -z "$mac" ] && [ -z "$code" ] && return 0 # nothing to attribute it to; skip (never block the printer)
    curl -s -m 4 --data-urlencode "pages=1" --data-urlencode "copies=$copies" \
         --data-urlencode "code=$code" --data-urlencode "doc=$job" \
         "http://$KB_SERVER/seat/print?mac=$mac" >/dev/null 2>&1 || true
}

echo "kb-print-meter: tailing $PAGELOG -> http://$KB_SERVER/seat/print"
# -F so it survives log rotation; read each new page line and report it
tail -n0 -F "$PAGELOG" 2>/dev/null | while IFS=' ' read -r a b c d e f g hh rest; do
    report "$a" "$b" "$c" "$d" "$e" "$f" "$g" "$hh" "$rest"
done
