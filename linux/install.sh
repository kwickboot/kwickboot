#!/bin/sh
# KwickBoot server installer - Debian 13 (trixie), amd64 or arm64. Run as root from the unpacked release:
#   sudo sh install.sh [--iface <nic>] [--firewall] [--cache] [--ai-sdk] [--yes]
#     --iface    the wired NIC that faces the PCs (default: the one with the default route)
#     --firewall also write an nftables firewall (only when the host has none of its own)
#     --cache    package cache for installs (apt-cacher-ng, LAN only) - ON BY DEFAULT: Debian/Ubuntu installs fetch
#                packages at LAN speed and keep working through internet hiccups / fully offline once warm. --no-cache disables it.
#     --no-cache do NOT install the local package cache (e.g. a tiny server, or an external cache already in place)
#     --ai-sdk   Claude SDK for the AI pack (hash-locked from PyPI); local AI services need nothing extra
#     --offline  no internet: skip apt (python3, python3-cryptography, udpcast, iproute2, cabextract, 7zip, innoextract
#                must already be installed)
#     --yes      no questions. A new server starts with the sign-in admin / admin, which must be changed at the first
#                sign-in (KB_NO_DEFAULT_ADMIN=1: none - the console's first-run setup code instead)
#     --own-dhcp <first>-<last>  ADVANCED, off by default: KwickBoot also hands out addresses on --iface (isolated
#                lab / network without DHCP). Never where another DHCP server runs.
#     --own-dhcp-discover  with --own-dhcp: every PC that network-boots there is registered (test lab)
#     --console-from <cidr>  console + SSH also from this network (e.g. the office side of a test lab); repeatable
#     --no-net-tune  leave the kernel / network card settings alone (default: tuned for large transfers)
# Upgrades in place: code, console and boot files are replaced; the database, machines, logs, config, images and
# ISOs are kept. Ports: UDP 67/4011 proxyDHCP, UDP 69 TFTP, TCP 8080 console+HTTP boot, TCP 8444 managed machines,
# UDP 9000-9199 multicast. KwickBoot never hands out IP addresses - your existing DHCP server stays in charge.
set -eu
T=/opt/pxe-toolkit
SRC=$(cd "$(dirname "$0")" && pwd)
IFACE=""; FW=0; CACHE=1; AISDK=0; YES=0; OFFLINE=0; NETTUNE=1; OWNDHCP=""; ALSO=""; ODISC=false   # CACHE on by default (offline-ready sites)
while [ $# -gt 0 ]; do
  case "$1" in
    --iface) IFACE=$2; shift ;; --firewall) FW=1 ;; --cache) CACHE=1 ;; --no-cache) CACHE=0 ;; --ai-sdk) AISDK=1 ;; --yes|-y) YES=1 ;; --offline) OFFLINE=1 ;; --no-net-tune) NETTUNE=0 ;;
    --own-dhcp) OWNDHCP=$2; shift ;; --own-dhcp-discover) ODISC=true ;; --console-from) ALSO="$ALSO $2"; shift ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "unknown option $1 (see --help)"; exit 1 ;;
  esac; shift
done
[ "$OFFLINE" = 1 ] && CACHE=0    # --offline skips apt, so the cache cannot be installed now (pre-install it beforehand)
say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# ---- checks
[ "$(id -u)" = 0 ] || die "run as root (sudo sh install.sh)"
. /etc/os-release 2>/dev/null || die "cannot read /etc/os-release"
[ "${ID:-}" = debian ] && [ "${VERSION_CODENAME:-}" = trixie ] || die "KwickBoot supports Debian 13 (trixie); this is ${PRETTY_NAME:-unknown}"
case "$(dpkg --print-architecture)" in amd64|arm64) ;; *) die "KwickBoot server runs on amd64 or arm64 (this host is $(dpkg --print-architecture))" ;; esac
[ -f "$SRC/pxeserver.py" ] && [ -d "$SRC/http/kbcore" ] || die "run this from the unpacked KwickBoot release"
if [ -f "$SRC/SHA256SUMS" ]; then
  say "Verifying the release files"
  ( cd "$SRC" && sha256sum --quiet -c SHA256SUMS ) || die "release files are damaged - download again"
fi
[ -n "$IFACE" ] || IFACE=$(ip -o route show default 2>/dev/null | awk '{for(i=1;i<NF;i++) if($i=="dev"){print $(i+1); exit}}')
[ -n "$IFACE" ] && ip link show "$IFACE" >/dev/null 2>&1 || die "no network interface found - use --iface <nic>"
IP=$(ip -o -4 addr show dev "$IFACE" | awk '{print $4}' | head -n1)
[ -n "$IP" ] || die "$IFACE has no IPv4 address - give the server a fixed address (DHCP reservation) first"
LAN=$(python3 -c "import ipaddress,sys; print(ipaddress.ip_interface(sys.argv[1]).network)" "$IP")
ADDR=${IP%/*}
say "Server $ADDR on $IFACE (LAN $LAN)"
for c in $ALSO; do python3 -c "import ipaddress,sys; ipaddress.ip_network(sys.argv[1], strict=False)" "$c" 2>/dev/null || die "--console-from: $c is not a network (e.g. 192.168.0.0/24)"; done
if [ -n "$OWNDHCP" ]; then
  python3 - "$OWNDHCP" "$IP" <<'PY' || die "--own-dhcp: give <first>-<last> inside the server's network, e.g. 10.88.0.100-10.88.0.200"
import ipaddress, sys
a, b = (ipaddress.ip_address(x) for x in sys.argv[1].split("-")); n = ipaddress.ip_interface(sys.argv[2]).network
assert a in n and b in n and a <= b
PY
  echo "WARNING: own DHCP on $IFACE ($OWNDHCP) - KwickBoot hands out addresses there. Only on a network with NO other DHCP server."
fi
echo "Notice: KwickBoot installs, restores and wipes disks. You are solely responsible for your data and its backups."
echo "        KwickBoot is free software (GNU AGPL-3.0) and comes with NO WARRANTY. Terms: https://kwickboot.com/terms/"
for p in 67 69 4011; do
  if ss -lunp 2>/dev/null | awk '{print $5}' | grep -qE "[:.]$p\$" && ! systemctl is-active -q pxe-toolkit 2>/dev/null; then
    die "UDP port $p is already in use (another DHCP/TFTP/PXE server?) - stop it first: $(ss -lunp | grep -E "[:.]$p " | head -n1)"
  fi
done

# ---- packages (Debian's own)
say "Installing packages"
# udpcast: multicast; cabextract + 7zip + innoextract: unpack the PC makers' official Windows driver packs (.cab / .exe); timesyncd: correct time (certificates, logs, schedules)
PKGS="python3 python3-cryptography udpcast iproute2 ca-certificates cabextract 7zip innoextract systemd-timesyncd"
[ "$CACHE" = 1 ] && PKGS="$PKGS apt-cacher-ng"
[ "$FW" = 1 ] && PKGS="$PKGS nftables"
[ "$AISDK" = 1 ] && PKGS="$PKGS python3-venv"
OPTPKGS="python3-qrcode python3-ldap3"     # nice to have: two-step sign-in QR code, directory (LDAP/AD) sign-in - never blocks an install
export DEBIAN_FRONTEND=noninteractive
if [ "$OFFLINE" = 1 ]; then
  [ "$AISDK" = 0 ] || die "--ai-sdk needs internet (PyPI)"
  for pk in $PKGS; do dpkg -s "$pk" >/dev/null 2>&1 || die "--offline: package $pk is not installed"; done
else
  apt-get update -q >/dev/null
  # shellcheck disable=SC2086
  apt-get install -y -q $PKGS >/dev/null
  # shellcheck disable=SC2086
  apt-get install -y -q $OPTPKGS >/dev/null 2>&1 || echo "NOTE: optional $OPTPKGS not installed (two-step sign-in then shows a key to type instead of a QR code)"
fi

# ---- user, files
id pxe >/dev/null 2>&1 || useradd --system --home-dir "$T" --no-create-home --shell /usr/sbin/nologin pxe
UPGRADE=0; [ -f "$T/data/kwickboot.db" ] && UPGRADE=1
if [ "$UPGRADE" = 1 ]; then say "Upgrading $T (data kept)"; systemctl stop pxe-toolkit 2>/dev/null || true; else say "Installing to $T"; fi
mkdir -p "$T"
for f in "$SRC"/*.py "$SRC"/README.md "$SRC"/LICENSE "$SRC"/VERSION; do [ -f "$f" ] && install -m 0644 "$f" "$T/"; done
rm -rf "$T/ui" "$T/linux" "$T/http/scripts" "$T/http/recipes" "$T/http/tools" "$T/http/kbcore"
cp -r "$SRC/ui" "$SRC/linux" "$T/"
mkdir -p "$T/http" "$T/tftp"
for d in scripts recipes tools windows kbcore; do [ -d "$SRC/http/$d" ] && cp -r "$SRC/http/$d" "$T/http/"; done
# the OS library's built-in tools (Rescue & Wipe): replaced on every upgrade, the site's own images are never touched
if [ -d "$SRC/http/images/tools" ]; then mkdir -p "$T/http/images/tools"
  for t in "$SRC/http/images/tools"/*; do [ -d "$t" ] && { rm -rf "$T/http/images/tools/${t##*/}"; cp -r "$t" "$T/http/images/tools/"; }; done; fi
for d in debian-installer ipxe pxelinux.cfg; do [ -d "$SRC/tftp/$d" ] && { rm -rf "$T/tftp/$d"; cp -r "$SRC/tftp/$d" "$T/tftp/"; }; done
for f in pxelinux.0 ldlinux.c32 splash.png version.info; do [ -f "$SRC/tftp/$f" ] && cp "$SRC/tftp/$f" "$T/tftp/"; done
mkdir -p "$T/data" "$T/logs" "$T/machines" "$T/http/images" "$T/http/iso" "$T/tftp/alpine"
[ -f "$T/http/authorized_keys" ] || : > "$T/http/authorized_keys"
if [ ! -f "$T/config.json" ]; then
  cat > "$T/config.json" <<EOF
{
  "server_ip": "$ADDR",
  "http_port": 8080,
  "allowed_macs": {},
  "ui_allow": ["$LAN", "127.0.0.1/32"],
  "ui_https_port": 9443$( [ "${KB_NO_DEFAULT_ADMIN:-0}" = 1 ] && printf ',\n  "default_admin": false')
}
EOF
fi
HTTPS=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('ui_https_port') or '')" "$T/config.json" 2>/dev/null || true)
# advanced network options (also on upgrade, only when given): own DHCP, console from other networks
if [ -n "$OWNDHCP$ALSO" ]; then
  python3 - "$T/config.json" "$OWNDHCP" "$ALSO" "$ODISC" <<'PY'
import json, sys
p, rng, also = sys.argv[1], sys.argv[2], sys.argv[3].split()
c = json.load(open(p))
if rng:
    c["own_dhcp"] = {"enabled": True, "range": rng, "lease_s": 14400, "router": "", "dns": [], "discover": sys.argv[4] == "true"}
if also:
    c["ui_allow"] = list(dict.fromkeys(c.get("ui_allow", []) + also))
    c["http_bind"] = "0.0.0.0"          # the console must answer on the other network's card too
json.dump(c, open(p, "w"), indent=2)
PY
fi
# code and boot files: root-owned, world-readable. Site state is never re-permissioned on upgrade (signing keys stay 0600);
# images/ISOs may live on a USB disk mounted with its own permissions, so they are only adjusted when fresh.
for x in "$T"/*.py "$T"/README.md "$T"/LICENSE "$T"/VERSION "$T"/config.json "$T/ui" "$T/linux" "$T/http/scripts" "$T/http/recipes" "$T/http/tools"          "$T/http/kbcore" "$T/http/windows" "$T/http/authorized_keys" "$T/tftp"; do
  [ -e "$x" ] && { chown -R root:root "$x"; chmod -R u=rwX,go=rX "$x"; }
done
chmod 0755 "$T" "$T/http"
for d in "$T/logs" "$T/machines" "$T/tftp/alpine" "$T"/tftp/debian-installer/*/grub; do
  [ -e "$d" ] && chown -R pxe:pxe "$d"
done
chown pxe:pxe "$T/data"; chmod 0750 "$T/data"
[ "$UPGRADE" = 1 ] || chown pxe:pxe "$T/http/images" "$T/http/iso" 2>/dev/null || true
install -m 0755 "$T/linux/pxectl" /usr/local/sbin/pxectl
sed -i 's/\r$//' /usr/local/sbin/pxectl

# ---- service
install -m 0644 "$T/linux/pxe-toolkit.service" /etc/systemd/system/pxe-toolkit.service
# large file transfers (TCP: images, UDP: multicast + TFTP): kernel buffers/BBR + NIC rings. Skipped with --no-net-tune;
# an existing /etc/sysctl.d/90-kwickboot-net.conf is the admin's own and is kept.
if [ "$NETTUNE" = 1 ] && [ -f "$T/linux/kwickboot-net.conf" ]; then
  [ -f /etc/sysctl.d/90-kwickboot-net.conf ] || install -m 0644 "$T/linux/kwickboot-net.conf" /etc/sysctl.d/90-kwickboot-net.conf
  echo tcp_bbr > /etc/modules-load.d/kwickboot-bbr.conf; modprobe -q tcp_bbr 2>/dev/null || true
  sysctl -q --load=/etc/sysctl.d/90-kwickboot-net.conf >/dev/null 2>&1 || sysctl -q -p /etc/sysctl.d/90-kwickboot-net.conf >/dev/null 2>&1 || true
  install -m 0755 "$T/linux/kwickboot-nic-tune" /usr/local/sbin/kwickboot-nic-tune
  install -m 0644 "$T/linux/kwickboot-nic-tune.service" /etc/systemd/system/kwickboot-nic-tune.service
  systemctl daemon-reload; systemctl enable -q --now kwickboot-nic-tune.service 2>/dev/null || true
  say "Network tuned for large transfers (BBR, 16 MB buffers, NIC rings) - undo: rm /etc/sysctl.d/90-kwickboot-net.conf"
fi
grep -q 'http/iso' /etc/systemd/system/pxe-toolkit.service || \
  sed -i "s#^ReadWritePaths=.*#& $T/http/iso $T/http/images#" /etc/systemd/system/pxe-toolkit.service
systemctl daemon-reload
systemctl enable -q pxe-toolkit

# ---- optional: package cache, firewall, AI SDK
if [ "$CACHE" = 1 ]; then
  if systemctl enable -q --now apt-cacher-ng 2>/dev/null; then say "Package cache on TCP 3142 (LAN only, default)"
  else say "Package cache requested but apt-cacher-ng is not installed - skipping (install it, or run without --offline)"; fi
fi
if [ "$FW" = 1 ]; then
  if [ -f /etc/nftables.conf ] && grep -q 'policy drop' /etc/nftables.conf; then
    say "This host already has its own firewall - add these rules to it:"
    FWPRINT=1
  else
    say "Writing /etc/nftables.conf (LAN $LAN may use KwickBoot and SSH; everything else inbound is dropped)"
    FWPRINT=0
  fi
  RULES="    iifname \"$IFACE\" udp dport { 67, 4011 } accept comment \"KwickBoot proxyDHCP\"
    ip saddr $LAN udp dport 69 accept comment \"KwickBoot TFTP\"
    ip saddr $LAN tcp dport { 8080, 8444, 9443 } accept comment \"KwickBoot boot files, managed machines, HTTPS console\"
    ip saddr $LAN udp dport 9000-9199 accept comment \"KwickBoot multicast\"
    ip saddr $LAN tcp dport 3142 accept comment \"KwickBoot package cache\""
  if [ "$FWPRINT" = 1 ]; then echo "$RULES"; else
    cp -a /etc/nftables.conf /etc/nftables.conf.before-kwickboot 2>/dev/null || true
    cat > /etc/nftables.conf <<EOF
#!/usr/sbin/nft -f
flush ruleset
table inet filter {
  chain input {
    type filter hook input priority 0; policy drop;
    ct state established,related accept
    ct state invalid drop
    iif lo accept
    meta l4proto { icmp, ipv6-icmp } accept
    udp sport 67 udp dport 68 accept
    ip saddr $LAN tcp dport 22 accept comment "SSH from the LAN"
$(for c in $ALSO; do echo "    ip saddr $c tcp dport { 22, 8080, 9443 } accept comment \"console + SSH from $c\""; done)
$RULES
  }
  chain forward { type filter hook forward priority 0; policy drop; }
  chain output  { type filter hook output priority 0; policy accept; }
}
EOF
    nft -c -f /etc/nftables.conf && systemctl enable -q --now nftables && systemctl restart nftables
  fi
fi
if [ "$AISDK" = 1 ]; then
  say "Claude SDK for the AI pack (hash-locked)"
  [ -f "$T/linux/ai-requirements.txt" ] || die "linux/ai-requirements.txt missing from the release"
  rm -rf "$T/venv-ai"; python3 -m venv "$T/venv-ai"
  "$T/venv-ai/bin/pip" install -q --only-binary=:all: --require-hashes -r "$T/linux/ai-requirements.txt"
  chmod -R go+rX "$T/venv-ai"
fi

# ---- start
systemctl restart pxe-toolkit
sleep 3
systemctl is-active -q pxe-toolkit || { journalctl -u pxe-toolkit -n 30 --no-pager; die "the service did not start (log above)"; }
if [ -n "$HTTPS" ]; then
  say "KwickBoot is running: https://$ADDR:$HTTPS/ui/"
  echo "    (self-signed certificate: the browser warns once - continue. Your own certificate: $T/data/ui.crt + ui.key)"
else
  say "KwickBoot is running: http://$ADDR:8080/ui/"
  echo "    NOTE: plain-HTTP console. For HTTPS add \"ui_https_port\": 9443 to $T/config.json, allow TCP 9443, restart."
fi
if [ "$UPGRADE" = 0 ]; then
  if [ "$YES" = 0 ] && [ -t 0 ]; then
    say "Create the first admin (the password is typed twice and never shown)"
    pxectl useradd admin admin || echo "Later: pxectl useradd admin admin"
  else
    echo "Create the first admin:  pxectl useradd admin admin"
  fi
fi
cat <<EOF

Next:
  1. Open http://$ADDR:8080/ui/ and sign in.
  2. Machines -> "Discover new machines" opens a time-limited window.
  3. Network-boot a PC (UEFI IPv4 or legacy PXE). It appears for approval with its hardware.
EOF
