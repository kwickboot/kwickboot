"""Append to the d-i initrd a cpio containing:
  /firmware/<deb>            (so the installed system gets firmware-realtek)
  /fw/rtl_nic/*.fw           (raw firmware, loaded instantly via firmware_class.path=/fw)
"""
import gzip
import io
import lzma
import os
import sys
import tarfile
import time

base = os.path.dirname(os.path.abspath(__file__))
deb_path = sys.argv[1]
initrd_in = os.path.join(base, "tftp", "debian-installer", "amd64", "initrd.gz")
initrd_out = os.path.join(base, "tftp", "debian-installer", "amd64", "initrd-fw.gz")
ino = [10]


def entry(name, data, mode):
    ino[0] += 1
    namez = name.encode() + b"\0"
    hdr = "070701" + "".join(f"{v:08X}" for v in (
        ino[0], mode, 0, 0, 1, int(time.time()), len(data), 0, 0, 0, 0, len(namez), 0))
    out = hdr.encode() + namez
    out += b"\0" * ((4 - len(out) % 4) % 4)
    out += data
    out += b"\0" * ((4 - len(data) % 4) % 4)
    return out


def ar_members(blob):
    assert blob[:8] == b"!<arch>\n", "not an ar archive"
    i = 8
    while i + 60 <= len(blob):
        name = blob[i:i + 16].decode().strip().rstrip("/")
        size = int(blob[i + 48:i + 58].decode().strip())
        yield name, blob[i + 60:i + 60 + size]
        i += 60 + size + (size % 2)


with open(deb_path, "rb") as f:
    deb = f.read()

fw_files = {}
for name, data in ar_members(deb):
    if name.startswith("data.tar"):
        if name.endswith(".xz"):
            data = lzma.decompress(data)
        elif name.endswith(".gz"):
            data = gzip.decompress(data)
        elif name.endswith(".zst"):
            raise SystemExit("zstd data.tar not supported")
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            for m in t.getmembers():
                if m.isfile() and "/firmware/rtl_nic/" in m.name:
                    fw_files[os.path.basename(m.name)] = t.extractfile(m).read()

arc = entry("firmware", b"", 0o040755)
arc += entry("firmware/" + os.path.basename(deb_path), deb, 0o100644)
arc += entry("fw", b"", 0o040755)
arc += entry("fw/rtl_nic", b"", 0o040755)
for fn, data in sorted(fw_files.items()):
    arc += entry("fw/rtl_nic/" + fn, data, 0o100644)
arc += entry("TRAILER!!!", b"", 0)
arc += b"\0" * ((512 - len(arc) % 512) % 512)

with open(initrd_in, "rb") as f:
    orig = f.read()
with open(initrd_out, "wb") as f:
    f.write(orig)
    f.write(gzip.compress(arc))
r8168 = sorted(k for k in fw_files if k.startswith("rtl8168"))
print(f"rtl_nic firmware files: {len(fw_files)} (rtl8168*: {len(r8168)}: {' '.join(r8168)})")
print(f"initrd-fw.gz: {os.path.getsize(initrd_out)/1e6:.1f} MB")
