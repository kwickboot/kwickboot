#!/usr/bin/env python3
# boot-splash.py <out-dir> - KwickBoot boot splash (plymouth-style) -> raw BGRA for kbsplash. Same brand as the GRUB
# boot menu (linux/grub-theme.py): the BRAND-red power-mark badge on the LEFT + "KwickBoot" wordmark on the RIGHT, one
# line. Reserves a spinner zone + a progress-bar zone + a status-line zone that kbsplash animates/fills at runtime.
# SPDX-License-Identifier: AGPL-3.0-or-later   Run on the build box (needs python3-pil).
import sys, os, gzip
from PIL import Image, ImageDraw, ImageFont, ImageFilter
OUT = sys.argv[1] if len(sys.argv) > 1 else "."
W, H = 1024, 768
BG=(27,34,54); BRAND=(195,43,39); ACC=(217,119,87); TXT=(207,214,228); MUT=(120,131,158); WHT=(255,255,255); TRK=(45,54,80)
FD = "/usr/share/fonts/truetype/dejavu/"
def font(p, sz):
    return ImageFont.truetype(p, sz) if os.path.isfile(p) else ImageFont.load_default()
wm   = font(FD+"DejaVuSans-Bold.ttf", 96)
tag  = font(FD+"DejaVuSans.ttf", 26)
sub  = font(FD+"DejaVuSans.ttf", 18)
small= font(FD+"DejaVuSans.ttf", 15)

# the exact brand mark from grub-theme.py: a white power glyph in a rounded BRAND-red square (supersampled, crisp)
def logo(size):
    s = size * 4
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, s-1, s-1), int(s*0.2), fill=BRAND)
    w = int(s*0.1); c, r = s/2, s*0.26
    d.arc((c-r, c-r+s*0.04, c+r, c+r+s*0.04), 300, 240, fill="white", width=w)
    d.line((c, s*0.22, c, s*0.5), fill="white", width=w)
    d.ellipse((c-w/2, s*0.22-w/2, c+w/2, s*0.22+w/2), fill="white")
    return im.resize((size, size), Image.LANCZOS)

img = Image.new("RGBA", (W, H), BG+(255,)); d = ImageDraw.Draw(img); cx = W//2
# --- logo LEFT + "KwickBoot" RIGHT, on one line, centered as a unit (same layout as the boot menu wordmark)
BS, GAP = 120, 28
tw = int(d.textlength("KwickBoot", font=wm)); fh = wm.getbbox("KwickBoot")[3]
uw = BS + GAP + tw; ux = cx - uw//2; uy = 220
glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))     # subtle brand glow under the badge
ImageDraw.Draw(glow).rounded_rectangle((ux+2, uy+4, ux+2+BS, uy+4+BS), int(BS*0.28), fill=(BRAND[0], BRAND[1], BRAND[2], 90))
img = Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(10)))
img.alpha_composite(logo(BS), (ux, uy))
d = ImageDraw.Draw(img)
d.text((ux+BS+GAP, uy + (BS-fh)//2 - 4), "KwickBoot", font=wm, fill=(244, 241, 234, 255))

def ctext(y, s, f, fill):
    w = d.textlength(s, font=f); d.text((cx - w/2, y), s, font=f, fill=fill)
ctext(uy+BS+30, "The RAM OS with AI built in", tag, TXT)
ctext(uy+BS+70, "Boot, inspect & deploy any PC — no touch", sub, MUT)

SPY = 496                                           # spinner centre (kbsplash animates the ring here)
PW, PH = 520, 10; PX, PY = cx-PW//2, 560            # progress track (runtime fill)
d.rounded_rectangle([PX, PY, PX+PW, PY+PH], radius=5, fill=TRK)
STX, STY = cx, 594
d.text((48, H-34), "KwickBoot", font=small, fill=MUT)
fw = d.textlength("www.kwickboot.com", font=small); d.text((W-48-fw, H-34), "www.kwickboot.com", font=small, fill=MUT)

b = bytearray(img.convert("RGBA").tobytes())
for i in range(0, len(b), 4): b[i], b[i+2] = b[i+2], b[i]   # RGBA -> BGRA
with gzip.open(OUT+"/splash.bgra.gz", "wb") as f: f.write(bytes(b))
with open(OUT+"/splash.info", "w") as f:
    f.write(f"W={W}\nH={H}\nPX={PX}\nPY={PY}\nPW={PW}\nPH={PH}\nSTX={STX}\nSTY={STY}\nSPY={SPY}\n")
img.convert("RGB").save(OUT+"/splash.png")
print("wrote splash.bgra.gz, .info, .png to", OUT)
