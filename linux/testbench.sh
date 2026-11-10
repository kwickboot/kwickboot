#!/bin/sh
# PXE toolkit test bench: a UEFI KVM virtual machine that network-boots the toolkit menu.
# Uses QEMU's private user-mode network (does NOT touch the LAN). The VM's TFTP comes straight
# from /opt/pxe-toolkit/tftp; Alpine then fetches modloop/keys/scripts from the toolkit HTTP server.
#
#   testbench start [net|disk]   boot from network (default) or from the virtual disk
#   testbench stop | status | reset (wipe virtual disk + UEFI vars) | log | screenshot [out.png]
#   TB_MEM=<MB> testbench start ...   (default 1536)     TB_SB=1 testbench start ...  -> Secure Boot ON (Microsoft keys)
#   ssh into the VM from the operator PC:  ssh -J root@<this host> -p 2222 root@127.0.0.1
set -e
D=/var/lib/pxe-testbench
T=/opt/pxe-toolkit
mkdir -p "$D"
R=/run/pxe-testbench; mkdir -p "$R"     # sockets + pid on tmpfs (VM storage may be exFAT, which has no sockets)
[ -f "$D/disk.qcow2" ] || qemu-img create -q -f qcow2 "$D/disk.qcow2" 16G
CODE=/usr/share/OVMF/OVMF_CODE_4M.fd; VARS=/usr/share/OVMF/OVMF_VARS_4M.fd; SMM=""
if [ "${TB_SB:-0}" = 1 ]; then CODE=/usr/share/OVMF/OVMF_CODE_4M.secboot.fd; VARS=/usr/share/OVMF/OVMF_VARS_4M.ms.fd
  SMM="-global driver=cfi.pflash01,property=secure,value=on"; [ -f "$D/vars.sb" ] || { rm -f "$D/vars.fd"; touch "$D/vars.sb"; }
else [ -f "$D/vars.sb" ] && { rm -f "$D/vars.fd" "$D/vars.sb"; }; fi
[ -f "$D/vars.fd" ] || cp $VARS "$D/vars.fd"
running() { [ -f "$R/qemu.pid" ] && kill -0 "$(cat "$R/qemu.pid")" 2>/dev/null; }

case "${1:-status}" in
  start)
    running && { echo "test VM already running (pid $(cat $R/qemu.pid))"; exit 0; }
    ORDER=nc; [ "$2" = "disk" ] && ORDER=c
    : > "$D/serial.log"
    qemu-system-x86_64 -enable-kvm -cpu host -machine q35${SMM:+,smm=on} $SMM -smp 2 -m ${TB_MEM:-1536} \
      -drive if=pflash,format=raw,readonly=on,file=$CODE \
      -drive if=pflash,format=raw,file="$D/vars.fd" \
      -drive file="$D/disk.qcow2",if=none,id=d0,format=qcow2 -device virtio-blk-pci,drive=d0,serial=TESTDISK01 \
      -netdev user,id=n0,tftp="$T/tftp",bootfile=debian-installer/amd64/bootnetx64.efi,hostfwd=tcp:127.0.0.1:2222-:22 \
      -device virtio-net-pci,netdev=n0,mac=52:54:00:12:34:56 \
      -boot order=$ORDER -display none -serial file:"$D/serial.log" -monitor unix:"$R/mon.sock",server,nowait \
      -daemonize -pidfile "$R/qemu.pid"
    echo "test VM started (boot order: $ORDER), pid $(cat $R/qemu.pid); ssh: -J root@$(hostname -I | awk '{print $1}') -p 2222 root@127.0.0.1" ;;
  stop)   if running; then p=$(cat $R/qemu.pid); kill "$p"; i=0
            while kill -0 "$p" 2>/dev/null && [ $i -lt 40 ]; do sleep 0.25; i=$((i+1)); done   # wait: ports + disk released
            kill -0 "$p" 2>/dev/null && kill -9 "$p"; echo "test VM stopped"; else echo "not running"; fi
          rm -f "$R/qemu.pid" ;;
  status) running && echo "running (pid $(cat $R/qemu.pid))" || echo "stopped"; ls -lh "$D" | tail -n +2 ;;
  reset)  running && { echo "stop it first"; exit 1; }; rm -f "$D/disk.qcow2" "$D/vars.fd"; echo "virtual disk + UEFI vars wiped" ;;
  log)    tail -n 40 "$D/serial.log" ;;
  screenshot)  # VGA screen -> PNG (pure python, no extra packages)
    running || { echo "not running"; exit 1; }
    OUT=${2:-$D/screen.png}; rm -f "$D/screen.ppm"
    python3 - "$R/mon.sock" "$D/screen.ppm" "$OUT" <<'PY'
import socket, struct, sys, time, zlib
s = socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); time.sleep(0.3); s.recv(4096)
s.sendall(("screendump %s\n" % sys.argv[2]).encode()); time.sleep(1.5); s.close()
d = open(sys.argv[2], "rb").read(); parts = d.split(b"\n", 3)
w, h = map(int, parts[1].split()); px = parts[3]
raw = b"".join(b"\0" + px[y * w * 3:(y + 1) * w * 3] for y in range(h))
chunk = lambda t, b: struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b) & 0xffffffff)
open(sys.argv[3], "wb").write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                              + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))
print("screenshot %dx%d -> %s" % (w, h, sys.argv[3]))
PY
    ;;
  *) echo "usage: testbench start [net|disk] | stop | status | reset | log | screenshot [out.png]" ;;
esac
