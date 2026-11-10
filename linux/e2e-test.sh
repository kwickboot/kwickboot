#!/bin/bash
# KwickBoot v2 end-to-end test (SQLite state) on the KVM test bench (no SSH into the VM needed):
#  power on -> RAM OS -> agent auto check-in (token) -> inventory + preflight on server ->
#  install job via engine -> verify -> server default=local -> PXE boot redirects to installed OS.
# WIPES the test VM's virtual disk only (serial TESTDISK01).
set -u
# one test at a time: both scripts share the test VM (a second run would switch it off mid-install)
exec 9>/run/kb-testbench.lock; flock -n 9 || { echo "another test run is using the test VM - not starting"; exit 2; }
VM=52:54:00:12:34:56; D=/opt/pxe-toolkit/machines/52-54-00-12-34-56; DB=/opt/pxe-toolkit/data/kwickboot.db
KB_TEST_URL=${KB_TEST_URL:-http://127.0.0.1:8080}
SSHVM=(ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=8 -p 2222 root@127.0.0.1)
T0=$SECONDS; fails=0
step() { echo; echo "=== $* ($(date +%T), +$((SECONDS-T0))s) ==="; }
ok() { echo "OK   $*"; }; bad() { echo "FAIL $*"; fails=$((fails+1)); }
kq() { runuser -u pxe -- python3 -c "import sqlite3,sys;c=sqlite3.connect(sys.argv[1],isolation_level=None);r=c.execute(sys.argv[2],sys.argv[3:]).fetchone();print('' if not r or r[0] is None else r[0])" "$DB" "$@"; }
agent_state() { kq "SELECT agent_state FROM machines WHERE mac=?" $VM; }
lastjob() { pxectl jobs $VM | head -n1; }

step "0. fresh VM, default=alpine"
kq "UPDATE machines SET agent_state=NULL, agent_seen=NULL, next_action=NULL, next_used_at=NULL, token=NULL WHERE mac=?" $VM
kq "UPDATE jobs SET state='failed', error='e2e reset' WHERE mac=? AND state IN ('queued','running')" $VM
pxectl default $VM alpine >/dev/null
testbench stop >/dev/null 2>&1; testbench reset >/dev/null; testbench start net >/dev/null

step "1. power on -> RAM OS -> agent check-in (no console, no SSH)"
for i in $(seq 1 40); do [ "$(agent_state)" = ready ] && break; sleep 5; done
[ "$(agent_state)" = ready ] && ok "agent ready after $((SECONDS-T0))s" || { bad "agent never became ready"; tail -n 20 /var/lib/pxe-testbench/serial.log; exit 1; }
[ -n "$(kq 'SELECT token FROM machines WHERE mac=?' $VM)" ] && ok "per-boot token issued" || bad "no token"
PF=$(pxectl report $VM preflight); echo "$PF" | grep -q 'RESULT: GO' && ok "preflight in db: $(echo "$PF" | grep RESULT | tr -d '=')" || bad "no preflight report"
pxectl report $VM inventory | grep -q 'SERIAL=TESTDISK01' && ok "inventory in db lists TESTDISK01" || bad "inventory missing disk"

step "2. security: forged agent call without token must be refused"
code=$(curl -s -o /dev/null -w '%{http_code}' "$KB_TEST_URL/agent/job?mac=$VM")
[ "$code" = 403 ] && ok "no-token job request refused (403)" || bad "no-token request got $code"

step "3. install job (engine + debian13 recipe)"
pxectl install $VM debian13 serial=TESTDISK01 host=testvm finish=none | tail -n1
for i in $(seq 1 150); do st=$(lastjob | awk '{print $2}'); case "$st" in done|failed) break;; esac; sleep 8; done
lastjob
grep -E '^>>> \[|  (OK|FAIL)  |INSTALL OK|STOP' "$(ls -t $D/jobs/*.log | head -n1)" | tail -n 22
[ "$st" = done ] && ok "install job done" || { bad "install job $st"; pxectl joblog $VM | tail -n 25; }
[ "$(kq 'SELECT default_action FROM machines WHERE mac=?' $VM)" = local ] && ok "server switched default -> local" || bad "default not switched"

kq "SELECT detail FROM audit WHERE action='INSTALL' AND detail LIKE ? ORDER BY id DESC LIMIT 1" "%$VM%" | grep -q TESTDISK01 && ok "install recorded in audit log" || bad "no audit entry"

step "4. reboot via network: PXE must redirect to the installed Debian"
testbench stop >/dev/null; sleep 2; testbench start net >/dev/null
for i in $(seq 1 40); do r=$("${SSHVM[@]}" 'cat /etc/debian_version 2>/dev/null && hostname' 2>/dev/null | paste -sd' ') && [ -n "$r" ] && break; sleep 6; done
case "$r" in 13*testvm) ok "booted installed Debian via PXE redirect: $r" ;; *) bad "did not reach installed Debian (got: ${r:-nothing})" ;; esac
"${SSHVM[@]}" 'echo "  failed units: $(systemctl --failed --no-legend | wc -l)  services: $(systemctl is-active ssh nftables systemd-networkd | paste -sd,)"' 2>/dev/null

step "5. cleanup: default back to alpine, VM off"
pxectl default $VM alpine >/dev/null; testbench stop >/dev/null
echo; echo "===== E2E v2 RESULT: $([ $fails -eq 0 ] && echo PASS || echo "FAIL ($fails)") in $(( (SECONDS-T0)/60 )) min ====="
