#!/usr/bin/env python3
# rescue-chrome.py <out-dir> - render the KwickBoot Rescue menu chrome (static) to a raw BGRA blob for the
# framebuffer UI (kbrescue-fb). 1024x768, boot-menu theme. Dynamic text (disk list, Choose input) is drawn at
# runtime by kbrescue-fb over the reserved zones. Also emits chrome.info with the zone coordinates.
# SPDX-License-Identifier: AGPL-3.0-or-later   Run on the build box (needs python3-pil).
import sys, gzip, struct
from PIL import Image, ImageDraw, ImageFont
OUT = sys.argv[1] if len(sys.argv) > 1 else "."
W, H = 1024, 768
BG=(27,34,54); BRAND=(195,43,39); ACC=(217,119,87); TXT=(207,214,228); MUT=(120,131,158); WHT=(255,255,255)
img = Image.new("RGB", (W, H), BG); d = ImageDraw.Draw(img)
FD = "/usr/share/fonts/truetype/dejavu/"
mono  = ImageFont.truetype(FD+"DejaVuSansMono.ttf", 18)
monob = ImageFont.truetype(FD+"DejaVuSansMono-Bold.ttf", 18)
wm    = ImageFont.truetype(FD+"DejaVuSans-Bold.ttf", 34)
sub   = ImageFont.truetype(FD+"DejaVuSans.ttf", 16)
small = ImageFont.truetype(FD+"DejaVuSans.ttf", 14)

# logo badge (compact) + power icon
bx, by, bs = 40, 30, 56
d.rounded_rectangle([bx,by,bx+bs,by+bs], radius=13, fill=BRAND)   # KwickBoot mark: brand red (matches the boot menu)
cx, cy, r = bx+bs//2, by+bs//2+2, 14
d.arc([cx-r,cy-r,cx+r,cy+r], start=-60, end=240, fill=WHT, width=4)
d.line([cx,cy-r-3,cx,cy+1], fill=WHT, width=4)
d.text((bx+bs+18, by+2),  "KwickBoot",      font=wm,  fill=WHT)
d.text((bx+bs+20, by+42),  "Rescue & Wipe",  font=sub, fill=ACC)
d.text((W-360, by+20), "this PC's disks change only when you confirm", font=small, fill=MUT)
d.line([40, 104, W-40, 104], fill=(60,70,96), width=1)

# --- reserved disk zone (runtime overlay): y 116.. , up to 3 lines
DISK_X, DISK_Y, LINE_H = 48, 116, 26

ITEMS = []    # (num, x, y, w, h) in navigation order - the framebuffer tool highlights these for arrow-key selection
def col(x, y, head, items):
    d.text((x, y), head, font=monob, fill=ACC); yy = y+28
    cw = 48 + max(len(l) for _, l in items)*11 + 16
    for num, label in items:
        d.text((x, yy), f"{num:>3})", font=monob, fill=ACC)
        d.text((x+48, yy), label, font=mono, fill=TXT)
        ITEMS.append((num, x-6, yy-4, cw, 25)); yy += 26

MENU_Y = 210
c1, c2, c3 = 48, 384, 716
col(c1, MENU_Y, "Get the data out first", [("1","Copy a user's files"),("2","Clone a failing disk"),("3","Recover partitions / files"),("4","Open VeraCrypt / TrueCrypt")])
col(c2, MENU_Y, "Windows", [("5","Reset a local password"),("6","Repair start-up"),("7","Stop a bad driver"),("8","Collect crash info"),("9","Schedule a full chkdsk"),("10","Show BitLocker key ID")])
col(c3, MENU_Y, "Linux", [("11","Set a new password"),("12","Repair start-up (GRUB)"),("13","Fix /etc/fstab"),("14","Rebuild initramfs"),("15","Unlock account / sudo")])
MENU_Y2 = MENU_Y + 210
col(c1, MENU_Y2, "Disks, firmware, hardware", [("16","Check / repair a file system"),("17","Partition table (GPT)"),("18","UEFI boot entries"),("19","Hardware check"),("20","Fix the clock")])
col(c3, MENU_Y2, "Help", [("21","Let IT connect (SSH)"),("22","Shell (expert)"),("0","Restart the PC")])
# wipe row
wy = MENU_Y2 + 178
d.rounded_rectangle([48, wy-4, W-40, wy+26], radius=6, fill=(58,33,28))
d.text((58, wy), "23)", font=monob, fill=ACC)
d.text((104, wy), "Secure wipe - NIST SP 800-88 Rev. 2 (erases disks for good)", font=mono, fill=(242,205,195))
ITEMS.append(("23", 44, wy-6, W-84, 32))
# --- reserved Choose zone (runtime overlay)
CHOOSE_X, CHOOSE_Y = 48, wy + 46
# footer
d.text((48, H-30), "KwickBoot 0.1", font=small, fill=MUT)
d.text((W-250, H-30), "www.kwickboot.com", font=small, fill=MUT)

# save raw BGRA (gzipped) + info
raw = img.convert("RGBA")
b = bytearray(raw.tobytes())            # RGBA
for i in range(0, len(b), 4): b[i], b[i+2] = b[i+2], b[i]   # -> BGRA
with gzip.open(OUT+"/rescue-chrome.bgra.gz", "wb") as f: f.write(bytes(b))
with open(OUT+"/rescue-chrome.info", "w") as f:
    f.write(f"W={W}\nH={H}\nDISK_X={DISK_X}\nDISK_Y={DISK_Y}\nLINE_H={LINE_H}\n"
            f"CHOOSE_X={CHOOSE_X}\nCHOOSE_Y={CHOOSE_Y}\n")
    for num, x, y, w, h in ITEMS: f.write(f"ITEM={num},{x},{y},{w},{h}\n")
img.save(OUT+"/rescue-chrome.png")      # for preview only
print("wrote rescue-chrome.bgra.gz, .info, .png to", OUT)
