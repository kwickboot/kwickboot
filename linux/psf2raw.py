#!/usr/bin/env python3
# psf2raw.py <out font.raw> - extract the first 256 glyphs of an 8x16 console font as raw 16-bytes-each
# (ASCII index = byte, MSB = leftmost pixel) for kbrescue-fb. Run on the build box (kbd-consolefonts).
# SPDX-License-Identifier: AGPL-3.0-or-later
import sys, gzip, struct, glob
out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/font.raw"
cands = ["/usr/share/consolefonts/Lat15-VGA16.psf.gz", "/usr/share/consolefonts/Lat2-VGA16.psf.gz",
         "/usr/share/consolefonts/Lat15-Fixed16.psf.gz", "/usr/share/consolefonts/default8x16.psf.gz"]
cands += sorted(glob.glob("/usr/share/consolefonts/*VGA16.psf.gz"))
def load(p):
    d = gzip.open(p, "rb").read()
    if d[0] == 0x36 and d[1] == 0x04:                    # PSF1
        if d[3] != 16: return None
        return d[4:]
    if d[:4] == b"\x72\xb5\x4a\x86":                      # PSF2
        ver, hs, flags, length, cs, h, w = struct.unpack("<IIIIIII", d[4:32])
        if w != 8 or h != 16 or cs != 16: return None
        return d[hs:]
    return None
for p in cands:
    try: g = load(p)
    except Exception: g = None
    if g:
        b = bytearray(4096)
        for i in range(256):
            c = g[i*16:(i+1)*16]; b[i*16:i*16+len(c)] = c[:16]
        open(out, "wb").write(b); print("font:", p, "->", out); sys.exit(0)
sys.exit("no 8x16 console font found (apt install kbd)")
