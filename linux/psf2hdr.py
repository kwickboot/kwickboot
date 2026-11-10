#!/usr/bin/env python3
# psf2hdr.py <out> [psf.gz] - extract a console PSF to a raw font for kbgui, with a 4-byte header [w, h, rowbytes, 0]
# then 256 glyphs (rowbytes*h each). Unlike psf2raw (fixed 8x16, no header), this keeps the cell size so the GUI can
# use a crisp HiDPI font (e.g. Terminus 24). Latin/Cyrillic/Greek coverage. Run on the build box.
# SPDX-License-Identifier: AGPL-3.0-or-later
import sys, gzip, struct, glob
out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/font24.raw"
cands = ([sys.argv[2]] if len(sys.argv) > 2 else []) + [
    "/usr/share/consolefonts/CyrAsia-TerminusBold24x12.psf.gz", "/usr/share/consolefonts/Uni2-TerminusBold24x12.psf.gz",
    "/usr/share/consolefonts/CyrAsia-Terminus24x12.psf.gz"] + sorted(glob.glob("/usr/share/consolefonts/*Terminus*24x12.psf.gz"))
def load(p):
    d = gzip.open(p, "rb").read()
    if d[:4] == b"\x72\xb5\x4a\x86":                     # PSF2
        ver, hs, flags, length, cs, h, w = struct.unpack("<IIIIIII", d[4:32])
        rb = cs // h
        return w, h, rb, cs, d[hs:hs + cs * min(length, 256)]
    if d[0] == 0x36 and d[1] == 0x04:                    # PSF1 (8 wide)
        h = d[3]; cs = h; return 8, h, 1, cs, d[4:4 + cs * 256]
    return None
for p in cands:
    try:
        r = load(p)
    except Exception:
        r = None
    if r:
        w, h, rb, cs, data = r
        buf = bytearray([w, h, rb, 0]) + bytearray(data[:cs * 256])
        if len(buf) < 4 + cs * 256:
            buf += bytearray(4 + cs * 256 - len(buf))     # pad to 256 glyphs
        open(out, "wb").write(buf)
        print(f"font: {p} -> {out}  ({w}x{h}, {rb} bytes/row)")
        sys.exit(0)
sys.exit("no 24px Terminus console font found (apt install console-setup-linux / fonts)")
