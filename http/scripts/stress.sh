#!/bin/sh
# KwickBoot stress test - runs inside the RAM OS, touches no disk. CPU + memory under full load for MIN minutes:
# memory errors (stress-ng verifies every page it writes), peak temperature, CPU thermal throttling, clock under load.
# SPDX-License-Identifier: AGPL-3.0-or-later
#   MIN=10 sh stress.sh
MIN=${MIN:-10}
case "$MIN" in *[!0-9]*|"") MIN=10 ;; esac
[ "$MIN" -ge 1 ] && [ "$MIN" -le 120 ] || MIN=10
command -v stress-ng >/dev/null || { echo "STOP: stress-ng missing (diagnostics pack not loaded)"; exit 1; }
thr() { cat /sys/devices/system/cpu/cpu*/thermal_throttle/*_throttle_count 2>/dev/null | awk '{s+=$1} END{print s+0}'; }
temp() { cat /sys/class/thermal/thermal_zone*/temp /sys/class/hwmon/hwmon*/temp*_input 2>/dev/null | awk '$1>1000 && $1<150000{if($1>m)m=$1} END{printf "%d", m/1000}'; }
mhz() { awk '/^cpu MHz/{s+=$4; n++} END{if(n) printf "%d", s/n}' /proc/cpuinfo; }
cpus=$(nproc); memk=$(awk '/MemAvailable/{print $2}' /proc/meminfo)
vmk=$(( memk * 70 / 100 / 2 ))                       # two memory workers, 70% of the free RAM in total (RAM OS keeps the rest)
t0=$(temp); th0=$(thr); f0=$(mhz)
echo ">>> stress test: $cpus CPU threads + 2 memory workers x $((vmk / 1024)) MB, $MIN min (idle clock ${f0:-?} MHz)"
peak=$t0; low=999999
( stress-ng --cpu "$cpus" --vm 2 --vm-bytes "${vmk}k" --vm-method all --verify --timeout "${MIN}m" --metrics-brief > /tmp/kbstress.log 2>&1; echo "rc=$?" >> /tmp/kbstress.log ) &
sp=$!
i=0
while kill -0 $sp 2>/dev/null; do
  sleep 15; i=$((i + 15))
  t=$(temp); f=$(mhz)
  [ -n "$t" ] && [ "$t" -gt "${peak:-0}" ] && peak=$t
  [ -n "$f" ] && [ "$f" -lt "$low" ] && low=$f
  echo "  +${i}s  $([ "${t:-0}" -gt 0 ] && echo "${t} C  ")${f:-?} MHz"
done
th1=$(thr); rc=$(sed -n 's/^rc=//p' /tmp/kbstress.log | tail -n1)
errs=$(grep -cE 'stress-ng: fail|failed: [1-9]|miscompar|verification (error|failure)' /tmp/kbstress.log)   # 'failed: 0' is a pass
echo
grep -E 'stress-ng: (info|fail|warn).*(cpu|vm) ' /tmp/kbstress.log | tail -n 6 | sed 's/^/  /'
if [ "${peak:-0}" -gt 0 ]; then echo "  peak temperature: ${peak} C (idle ${t0} C)"; else echo "  temperature: no sensor readable (virtual machine or unsupported board)"; fi
echo "  lowest clock under load: ${low} MHz (idle ${f0:-?} MHz)"
echo "  thermal throttling events: $((th1 - th0))"
verdict=OK; why=""
[ "${rc:-1}" != 0 ] || [ "${errs:-0}" -gt 0 ] && { verdict=FAIL; why="$why memory/CPU errors under load (replace RAM / check CPU);"; }
[ $((th1 - th0)) -gt 0 ] && { [ "$verdict" = OK ] && verdict=WARN; why="$why the CPU throttled from heat (clean the fans / heatsink, renew thermal paste);"; }
[ "${peak:-0}" -ge 95 ] && { [ "$verdict" = OK ] && verdict=WARN; why="$why ${peak} C peak - cooling is at its limit;"; }
echo "STRESS $verdict after $MIN min${why:+ -$why}"
[ "$verdict" = FAIL ] && exit 1
exit 0
