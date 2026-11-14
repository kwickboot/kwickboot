#!/bin/sh
# test-install.sh - test a KwickBoot release on a FRESH Debian 13 in an isolated container (build box, as root).
#   sudo sh test-install.sh <release.tar.gz>
# The container has a private virtual link only (no LAN: its proxyDHCP can't answer any real PC; no internet:
# install.sh --offline). Checks: fresh install, service up, console answers, upgrade keeps data, signing key stays 0600,
# and a UEFI VM (OVMF, on a private bridge) network-boots from the fresh install: shim -> GRUB -> RAM OS agent check-in.
# KB_PXETEST=0 skips that (it needs qemu-system-x86_64 + ovmf on the build box).
set -eu
REL=${1:?release tarball}
W=/tmp/kbct; M=kbtest
cleanup() { [ -f /tmp/kbpxe.pid ] && kill "$(cat /tmp/kbpxe.pid)" 2>/dev/null; rm -f /tmp/kbpxe.pid
  ip link del tap-kbt >/dev/null 2>&1 || true; ip link del br-kbt >/dev/null 2>&1 || true
  machinectl terminate $M >/dev/null 2>&1 || true; sleep 1; ip link del ve-$M >/dev/null 2>&1 || true; }
cleanup; rm -rf $W; mkdir -p $W
echo "== debootstrap Debian 13 (minimal + KwickBoot's runtime packages)"
debootstrap --variant=minbase --include=systemd-sysv,dbus,iproute2,ca-certificates,curl,procps \
  trixie $W/root http://deb.debian.org/debian >$W/debootstrap.log 2>&1 || { tail -20 $W/debootstrap.log; exit 1; }
# runtime packages with apt in the chroot (uses the build box's network; the booted container gets none)
mount --bind /proc $W/root/proc
chroot $W/root sh -c 'export DEBIAN_FRONTEND=noninteractive; apt-get install -y -q python3 python3-cryptography udpcast cabextract 7zip innoextract systemd-timesyncd python3-qrcode python3-ldap3' >$W/apt.log 2>&1 \
  || { umount $W/root/proc; tail -20 $W/apt.log; exit 1; }
umount $W/root/proc
mkdir -p $W/root/root/rel && tar -xzf "$REL" -C $W/root/root/rel
N=$(ls $W/root/root/rel)
echo "== boot container (private veth only)"
systemd-run --quiet --unit=kbtest-nspawn systemd-nspawn --quiet -D $W/root -M $M -b --network-veth
for i in $(seq 1 30); do machinectl show $M >/dev/null 2>&1 && ip link show ve-$M >/dev/null 2>&1 && break; sleep 1; done
sleep 3
ip addr add 10.199.0.1/24 dev ve-$M 2>/dev/null || true; ip link set ve-$M up
run() { systemd-run --quiet --machine=$M --pipe --wait sh -c "$1"; }
run "ip addr add 10.199.0.2/24 dev host0 && ip link set host0 up && ip route add default via 10.199.0.1"
echo "== FRESH INSTALL"
run "sh /root/rel/$N/install.sh --offline --yes" 2>&1 | tail -12
waitweb() { for i in $(seq 1 40); do curl -sk -m 2 -o /dev/null https://10.199.0.2:9443/ui/ && return 0; sleep 1; done; echo "console never answered"; return 1; }
waitweb
code=$(curl -sk -o /dev/null -w '%{http_code}' https://10.199.0.2:9443/ui/ || true); echo "console page (HTTPS): $code"
echo "HTTP console redirects: $(curl -s -o /dev/null -w '%{http_code} -> %{redirect_url}' http://10.199.0.2:8080/ui/)"
echo "login over plain HTTP refused: $(curl -s -o /dev/null -w '%{http_code}' -H 'Origin: http://10.199.0.2:8080' -H 'Content-Type: application/json' -d '{}' http://10.199.0.2:8080/login)"
echo "boot files still on HTTP: $(curl -s -o /dev/null -w '%{http_code}' -r 0-0 http://10.199.0.2:8080/kbcore/vmlinuz)"
r=$(curl -s http://10.199.0.2:8080/api/overview); echo "api without login: $r"
case "$r" in *'"default_login": true'*|*'"default_login":true'*) echo "fresh install: default sign-in admin/admin active: OK" ;; *) echo "FAIL: no default sign-in on a fresh install ($r)"; exit 1 ;; esac
echo "favicon: $(curl -s -o /dev/null -w '%{http_code} %{content_type}' http://10.199.0.2:8080/ui/favicon.svg)"
echo "== FIRST SIGN-IN (admin / admin -> must choose a new password; nothing else allowed before)"
J="-k -c /tmp/kbt.cookies -b /tmp/kbt.cookies -H Origin:https://10.199.0.2:9443 -H Content-Type:application/json"; rm -f /tmp/kbt.cookies
echo "login admin/admin: $(curl -s $J -d '{"username":"admin","password":"admin"}' https://10.199.0.2:9443/login)"
echo "other API before the change: HTTP $(curl -s -o /dev/null -w '%{http_code}' $J https://10.199.0.2:9443/api/terms)  (403 = blocked)"
echo "too short: $(curl -s $J -d '{"current":"admin","new":"short"}' https://10.199.0.2:9443/api/me/password)"
echo "change: $(curl -s $J -d '{"current":"admin","new":"test-only-pw-456"}' https://10.199.0.2:9443/api/me/password)"
echo "other API after the change: HTTP $(curl -s -o /dev/null -w '%{http_code}' $J https://10.199.0.2:9443/api/terms)"
if [ -n "${KB_LOADTEST:-}" ]; then                 # e.g. KB_LOADTEST="200 500": live consoles at scale
  echo "== LIVE CONSOLES LOAD TEST (consoles: $KB_LOADTEST)"
  run "cd /opt/pxe-toolkit && runuser -u pxe -- python3 -c \"import db; db.init('data/kwickboot.db'); [db.upsert_machine('52:54:00:aa:00:%02x' % i, name='lt-0', approved=1) for i in range(1, 21)]\""
  ck=$(awk '$6=="kbsess"{print $7}' /tmp/kbt.cookies | tail -n1)
  MACS=$(for i in $(seq 1 20); do printf '52:54:00:aa:00:%02x,' "$i"; done)
  for n in $KB_LOADTEST; do
    python3 "$(dirname "$0")/load-test.py" --url https://10.199.0.2:9443 --admin-cookie "$ck" --macs "$MACS" --consoles "$n" --rate 10 --secs 30 || echo "load test run failed"
    run "systemctl show pxe-toolkit -p TasksMax -p TasksCurrent -p LimitNOFILE | tr '\n' ' '; echo; grep -hE 'error|Error|limit|refus' /run/kwickboot-logs/pxe-*.log | tail -n 4"
    run "ps -o rss=,pcpu=,nlwp=,args= -C python3 | grep pxeserver | awk '{printf \"server: %.0f MB RAM, %s %% CPU, %s threads\n\", \$1/1024, \$2, \$3}'"
  done
fi
echo "admin/admin no longer works: HTTP $(curl -s -o /dev/null -w '%{http_code}' -k -H Origin:https://10.199.0.2:9443 -H Content-Type:application/json -d '{"username":"admin","password":"admin"}' https://10.199.0.2:9443/login)"
echo "hint gone: $(curl -sk https://10.199.0.2:9443/api/overview)"
echo "== TWO-STEP SIGN-IN (authenticator app, TOTP)"
totp() { python3 -c 'import base64,hmac,hashlib,struct,sys,time; s=sys.argv[1]; k=base64.b32decode(s+"="*(-len(s)%8)); d=hmac.new(k,struct.pack(">Q",int(time.time()//30)),hashlib.sha1).digest(); o=d[-1]&15; print(str((struct.unpack(">I",d[o:o+4])[0]&0x7fffffff)%1000000).zfill(6))' "$1"; }
st=$(curl -s $J -d '{"op":"start"}' https://10.199.0.2:9443/api/me/mfa)
sec=$(printf "%s" "$st" | python3 -c 'import json,sys; print(json.load(sys.stdin)["secret"])')
echo "QR code drawn: $(printf "%s" "$st" | grep -q '<svg' && echo yes || echo 'no (python3-qrcode missing: the key is shown instead)')"
c1=$(totp "$sec"); cf=$(curl -s $J -d "{\"op\":\"confirm\",\"code\":\"$c1\"}" https://10.199.0.2:9443/api/me/mfa)
rc=$(printf "%s" "$cf" | python3 -c 'import json,sys; print(json.load(sys.stdin)["recovery_codes"][0])'); echo "turned on, recovery codes: $(printf "%s" "$cf" | grep -o 'recovery_codes' | head -1)"
curl -s -o /dev/null $J -X POST https://10.199.0.2:9443/logout
lg=$(curl -s $J -d '{"username":"admin","password":"test-only-pw-456"}' https://10.199.0.2:9443/login); echo "password step: $lg" | cut -c1-60
tk=$(printf "%s" "$lg" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("ticket",""))')
echo "same code again (replay): HTTP $(curl -s -o /dev/null -w '%{http_code}' $J -d "{\"ticket\":\"$tk\",\"code\":\"$c1\"}" https://10.199.0.2:9443/login/mfa)  (401 = refused)"
echo "recovery code: $(curl -s $J -d "{\"ticket\":\"$tk\",\"code\":\"$rc\"}" https://10.199.0.2:9443/login/mfa)"
echo "signed in after two steps: HTTP $(curl -s -o /dev/null -w '%{http_code}' $J https://10.199.0.2:9443/api/overview)"
curl -s -o /dev/null $J -X POST https://10.199.0.2:9443/logout
tk=$(curl -s $J -d '{"username":"admin","password":"test-only-pw-456"}' https://10.199.0.2:9443/login | python3 -c 'import json,sys; print(json.load(sys.stdin).get("ticket",""))')
echo "recovery code reused: HTTP $(curl -s -o /dev/null -w '%{http_code}' $J -d "{\"ticket\":\"$tk\",\"code\":\"$rc\"}" https://10.199.0.2:9443/login/mfa)  (401 = refused)"
echo "== FIRST-RUN SETUP CODE (\"default_admin\": false servers) - skipped here: no setup code while an admin exists"
run "test -e /opt/pxe-toolkit/data/setup-code && echo 'setup code exists while an admin exists: FAIL' || echo 'no setup code (admin exists): OK'"
echo "RAM OS kernel served: $(curl -s -o /dev/null -w '%{http_code} %{size_download}' http://10.199.0.2:8080/kbcore/vmlinuz)"
run "test -d /opt/pxe-toolkit/data && ls /opt/pxe-toolkit/data; systemctl is-active pxe-toolkit"
if [ "${KB_PXETEST:-1}" = 1 ] && command -v qemu-system-x86_64 >/dev/null && [ -f /usr/share/OVMF/OVMF_CODE_4M.fd ]; then
  echo "== NETWORK BOOT (a UEFI PC boots from the fresh install: loaders shipped + served)"
  # the container's link goes into a private bridge with a tap for the VM - nothing reaches a real network
  ip link add br-kbt type bridge && ip link set br-kbt up
  ip addr del 10.199.0.1/24 dev ve-$M 2>/dev/null || true
  ip link set ve-$M master br-kbt && ip addr add 10.199.0.1/24 dev br-kbt
  ip tuntap add tap-kbt mode tap && ip link set tap-kbt master br-kbt && ip link set tap-kbt up
  # an isolated link has no DHCP server: let this test install hand out addresses itself (install.sh --own-dhcp)
  run "python3 - <<'PY'
import json
p = '/opt/pxe-toolkit/config.json'
c = json.load(open(p))
c['own_dhcp'] = {'enabled': True, 'range': '10.199.0.100-10.199.0.150', 'lease_s': 600, 'router': '', 'dns': [], 'discover': True}
json.dump(c, open(p, 'w'), indent=2)
PY
systemctl restart pxe-toolkit"
  sleep 4
  cp /usr/share/OVMF/OVMF_VARS_4M.fd $W/vars.fd
  # -cpu host: with QEMU's default CPU model OVMF does not load the NIC's iPXE option ROM -> no PXE, EFI shell
  qemu-system-x86_64 -enable-kvm -cpu host -machine q35 -m 2048 -no-reboot -display none -monitor none \
    -drive if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd -drive if=pflash,format=raw,file=$W/vars.fd \
    -netdev tap,id=n0,ifname=tap-kbt,script=no,downscript=no -device virtio-net-pci,netdev=n0,mac=52:54:00:4b:4b:01,bootindex=1 \
    -serial file:$W/pxe-serial.log -daemonize -pidfile /tmp/kbpxe.pid
  ok=""
  for i in $(seq 1 60); do
    run "grep -h 'RAM OS up' /run/kwickboot-logs/pxe-*.log 2>/dev/null | grep -q 52:54:00:4b:4b:01" >/dev/null 2>&1 && { ok=1; break; }
    sleep 5
  done
  run "grep -h -e 52:54:00:4b:4b:01 -e 'tftp. sent' /run/kwickboot-logs/pxe-*.log | grep -v facts | cut -c1-140 | head -14"
  kill "$(cat /tmp/kbpxe.pid)" 2>/dev/null; rm -f /tmp/kbpxe.pid
  if [ -n "$ok" ]; then echo "network boot: UEFI VM reached the RAM OS from the fresh install: OK"
  else echo "FAIL: the UEFI VM did not reach the RAM OS (serial: $(tr -d '\r' < $W/pxe-serial.log | sed 's/\x1b\[[0-9;=]*[A-Za-z]//g' | grep -a -e PXE -e Fetching -e error | head -4 | tr '\n' ' '))"; exit 1; fi
  ip link set ve-$M nomaster; ip link del tap-kbt; ip link del br-kbt; ip addr add 10.199.0.1/24 dev ve-$M; ip link set ve-$M up
  run "python3 -c \"import json; p='/opt/pxe-toolkit/config.json'; c=json.load(open(p)); c.pop('own_dhcp', None); json.dump(c, open(p, 'w'), indent=2)\"; systemctl restart pxe-toolkit"
  sleep 3
else
  echo "== NETWORK BOOT skipped (KB_PXETEST=0, or no qemu-system-x86_64 / ovmf on this box) - NOT VERIFIED"
fi
echo "== UPGRADE (data must survive, key must stay 0600)"
run "echo secret > /opt/pxe-toolkit/data/wipe-signing.key && chmod 600 /opt/pxe-toolkit/data/wipe-signing.key && chown pxe:pxe /opt/pxe-toolkit/data/wipe-signing.key && echo '{\"server_ip\":\"10.199.0.2\",\"http_port\":8080,\"allowed_macs\":{\"aa:bb:cc:dd:ee:ff\":\"kept\"}}' > /opt/pxe-toolkit/config.json"
run "sh /root/rel/$N/install.sh --offline --yes" 2>&1 | grep -E '==>|ERROR' | head
for i in $(seq 1 40); do curl -s -m 2 -o /dev/null http://10.199.0.2:8080/ui/ && break; sleep 1; done
echo "console after upgrade (old config kept = plain HTTP, as before): $(curl -s -o /dev/null -w '%{http_code}' http://10.199.0.2:8080/ui/)"
run "stat -c '%a %U %n' /opt/pxe-toolkit/data/wipe-signing.key /opt/pxe-toolkit/data/kwickboot.db; grep -c aa:bb:cc:dd:ee:ff /opt/pxe-toolkit/config.json; systemctl is-active pxe-toolkit"
echo "== cleanup"
cleanup; systemctl stop kbtest-nspawn 2>/dev/null || true; rm -rf $W
echo TEST-INSTALL-DONE
