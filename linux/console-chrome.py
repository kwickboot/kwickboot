#!/usr/bin/env python3
# console-chrome.py <out-dir> - the KwickBoot graphical status console chrome (static): navy background, a logo + wordmark
# header with the slogan, a rule, and a footer. kbfb-console blits this once and draws the live status text (from the
# shell's /run/kb-frame) into the body area every refresh. Same brand as the splash / GRUB boot menu.
# SPDX-License-Identifier: AGPL-3.0-or-later   Run on the build box (needs python3-pil).
import sys, os, gzip
from PIL import Image, ImageDraw, ImageFont, ImageFilter
OUT = sys.argv[1] if len(sys.argv) > 1 else "."
W, H = 1024, 768
BG=(27,34,54); BRAND=(195,43,39); ACC=(217,119,87); TXT=(207,214,228); MUT=(120,131,158); LINE=(50,60,88)
FD = "/usr/share/fonts/truetype/dejavu/"
def font(p, sz): return ImageFont.truetype(p, sz) if os.path.isfile(p) else ImageFont.load_default()
wm   = font(FD+"DejaVuSans-Bold.ttf", 34)
sub  = font(FD+"DejaVuSans.ttf", 16)
small= font(FD+"DejaVuSans.ttf", 14)

def logo(size):
    s = size*4; im = Image.new("RGBA",(s,s),(0,0,0,0)); d=ImageDraw.Draw(im)
    d.rounded_rectangle((0,0,s-1,s-1), int(s*0.2), fill=BRAND)
    w=int(s*0.1); c,r=s/2,s*0.26
    d.arc((c-r,c-r+s*0.04,c+r,c+r+s*0.04),300,240,fill="white",width=w)
    d.line((c,s*0.22,c,s*0.5),fill="white",width=w)
    d.ellipse((c-w/2,s*0.22-w/2,c+w/2,s*0.22+w/2),fill="white")
    return im.resize((size,size), Image.LANCZOS)

img = Image.new("RGBA", (W, H), BG+(255,)); d = ImageDraw.Draw(img)
# header: logo + KwickBoot + slogan
BS = 48; HX, HY = 40, 30
img.alpha_composite(logo(BS), (HX, HY)); d = ImageDraw.Draw(img)
d.text((HX+BS+16, HY-2), "KwickBoot", font=wm, fill=(244,241,234,255))
d.text((HX+BS+18, HY+40), "the RAM OS with AI built in", font=sub, fill=ACC+(255,))
d.line((40, HY+BS+22, W-40, HY+BS+22), fill=LINE+(255,), width=1)
# footer
d.text((40, H-32), "KwickBoot", font=small, fill=MUT+(255,))
fw = d.textlength("www.kwickboot.com", font=small); d.text((W-40-fw, H-32), "www.kwickboot.com", font=small, fill=MUT+(255,))

BX, BY, LH = 40, HY+BS+44, 30        # body: where kbfb-console draws the live status lines (16x32 font, 30px lines)
b = bytearray(img.convert("RGBA").tobytes())
for i in range(0, len(b), 4): b[i], b[i+2] = b[i+2], b[i]
with gzip.open(OUT+"/console-chrome.bgra.gz","wb") as f: f.write(bytes(b))
with open(OUT+"/console-chrome.info","w") as f: f.write(f"W={W}\nH={H}\nBX={BX}\nBY={BY}\nLH={LH}\nFOOT={H-44}\n")
img.convert("RGB").save(OUT+"/console-chrome.png")
print("wrote console-chrome.bgra.gz, .info, .png")
