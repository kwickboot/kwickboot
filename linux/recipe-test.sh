#!/bin/bash
# KwickBoot recipe test on the KVM bench:  recipe-test.sh <recipe> <hostname> [bundle=..] [more install args]
# fresh VM -> RAM OS agent -> install <recipe> via the engine -> PXE redirect boots the installed OS -> checks + screenshot.
# WIPES the test VM's virtual disk only (serial TESTDISK01).
set -u
# one test at a time: both scripts share the test VM (a second run would switch it off mid-install)
exec 9>/run/kb-testbench.lock; flock -n 9 || { echo "another test run is using the test VM - not starting"; exit 2; }
R=${1:?recipe}; HN=${2:?hostname}; shift 2
VM=52:54:00:12:34:56; DB=/opt/pxe-toolkit/data/kwickboot.db; D=/opt/pxe-toolkit/machines/52-54-00-12-34-56
SSHVM=(ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=8 -p 2222 root@127.0.0.1)
T0=$SECONDS; fails=0
step() { echo; echo "=== $* ($(date +%T), +$((SECONDS-T0))s) ==="; }
ok() { echo "OK   $*"; }; bad() { echo "FAIL $*"; fails=$((fails+1)); }
kq() { runuser -u pxe -- python3 -c "import sqlite3,sys;c=sqlite3.connect(sys.argv[1],isolation_level=None);r=c.execute(sys.argv[2],sys.argv[3:]).fetchone();print('' if not r or r[0] is None else r[0])" "$DB" "$@"; }
vm() { "${SSHVM[@]}" "$@" 2>/dev/null; }

step "0. fresh VM (${TB_MEM:-2048} MB), default=alpine"
kq "UPDATE machines SET agent_state=NULL, agent_seen=NULL, next_action=NULL, next_used_at=NULL, token=NULL WHERE mac=?" $VM
kq "UPDATE jobs SET state='failed', error='test reset' WHERE mac=? AND state IN ('queued','running')" $VM
pxectl default $VM alpine >/dev/null
testbench stop >/dev/null 2>&1; testbench reset >/dev/null; TB_MEM=${TB_MEM:-2048} testbench start net >/dev/null

step "1. RAM OS agent"
for i in $(seq 1 40); do [ "$(kq 'SELECT agent_state FROM machines WHERE mac=?' $VM)" = ready ] && break; sleep 5; done
[ "$(kq 'SELECT agent_state FROM machines WHERE mac=?' $VM)" = ready ] && ok "agent ready" || { bad "agent never ready"; testbench log; exit 1; }

step "2. install $R $*"
pxectl install $VM "$R" serial=TESTDISK01 host="$HN" finish=reboot "$@" | tail -n1
JID=$(pxectl jobs $VM | head -n1 | awk '{print $1}')
for i in $(seq 1 360); do st=$(pxectl jobs $VM | head -n1 | awk '{print $2}'); case "$st" in done|failed) break;; esac; sleep 10; done
grep -E '^>>> \[|  (OK|FAIL)  |INSTALL OK|STOP|WARN|verified|bundle|firefox|CONFIGURE DONE|debootstrap ok' "$D/jobs/$JID.log" | tail -n 40
[ "$st" = done ] && ok "install job done after $(( (SECONDS-T0)/60 )) min" || { bad "install job $st"; tail -n 40 "$D/jobs/$JID.log"; }
[ "$(kq 'SELECT default_action FROM machines WHERE mac=?' $VM)" = local ] && ok "default -> local" || bad "default not switched"

step "3. first boot of the installed OS (VM reboots itself; PXE redirects to disk)"
r=""; for i in $(seq 1 50); do r=$(vm '. /etc/os-release; echo "$PRETTY_NAME"; hostname' | paste -sd' ') && [ -n "$r" ] && break; sleep 6; done
case "$r" in *"$HN"*) ok "booted: $r" ;; *) bad "installed OS not reachable (got: ${r:-nothing})" ;; esac
sleep 30
vm 'echo "  system: $(systemctl is-system-running)   failed units: $(systemctl --failed --no-legend | awk "{print \$2}" | paste -sd, -)"
    echo "  services: ssh=$(systemctl is-active ssh) gdm3=$(systemctl is-active gdm3 2>/dev/null) nftables=$(systemctl is-active nftables) NetworkManager=$(systemctl is-active NetworkManager 2>/dev/null)"
    echo "  apps: $(command -v firefox >/dev/null && firefox --version 2>/dev/null | head -n1) | $(dpkg-query -W -f="\${Package} \${Version}" libreoffice-writer 2>/dev/null)"
    echo "  snaps: $(command -v snap >/dev/null && snap list 2>/dev/null | tail -n +2 | awk "{print \$1}" | paste -sd, - || echo none)"
    echo "  users >=1000: $(awk -F: "\$3>=1000 && \$3<60000{print \$1}" /etc/passwd | paste -sd, -)  disk used: $(df -h / | awk "NR==2{print \$3\"/\"\$2}")"
    echo "  secure boot shim: $(ls /boot/efi/EFI/ubuntu/ 2>/dev/null | paste -sd, -)"'
sleep 20; testbench screenshot /root/kb-first-boot.png
echo; echo "===== RECIPE TEST $R: $([ $fails -eq 0 ] && echo PASS || echo "FAIL ($fails)") in $(( (SECONDS-T0)/60 )) min ====="
