#!/usr/bin/python3
"""grub-theme.py - the KwickBoot boot menu theme (GRUB gfxmenu), drawn at build time. SPDX-License-Identifier: AGPL-3.0-or-later

  python3 grub-theme.py <theme-dir> <version>      (needs python3-pil; fonts are made by make-iso.sh with grub-mkfont)

Writes theme.txt, background.png, logo.png, the menu panel + highlight 9-slice pixmaps and one icon per menu class
(run, install, wipe, rescue, text, safe, next). All artwork is drawn here - original KwickBoot design, no third-party art.
Colours follow the console's dark theme: deep navy, brand red mark, warm accent highlight. Laid out in percentages: it is
seen at full HD on screens and at 1024x768 through the web console (live screen, screen wall, BMC / VNC).
"""
import math
import os
import random
import sys

from PIL import Image, ImageDraw, ImageFilter

OUT, VER = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "")
os.makedirs(os.path.join(OUT, "icons"), exist_ok=True)
W, H = 1920, 1080
BRAND = (195, 43, 39)          # KwickBoot mark (#C32B27)
ACC = (217, 119, 87)           # accent (#d97757)
ACC2 = (240, 160, 110)


def save(img, name):
    img.save(os.path.join(OUT, name), optimize=True)


# ---------------------------------------------------------------- background: gradient, glow, grid, network motif
bg = Image.new("RGB", (W, H))
px = bg.load()
for y in range(H):
    t = y / (H - 1)
    for x in range(W):
        u = x / (W - 1)
        r = int(11 * (1 - t) + 5 * t + 6 * (1 - u) * (1 - t))
        g = int(16 * (1 - t) + 7 * t + 4 * (1 - u) * (1 - t))
        b = int(32 * (1 - t) + 13 * t + 10 * (1 - u) * (1 - t))
        px[x, y] = (r, g, b)
glow = Image.new("RGB", (W, H), (0, 0, 0))
gd = ImageDraw.Draw(glow)
for cx, cy, rad, col in ((300, 180, 520, (120, 40, 30)), (1700, 980, 600, (20, 45, 90)), (960, 540, 700, (18, 22, 44))):
    gd.ellipse((cx - rad, cy - rad, cx + rad, cy + rad), fill=col)
glow = glow.filter(ImageFilter.GaussianBlur(220))
bg = Image.blend(bg, glow, 0.55)
over = Image.new("RGBA", (W, H), (0, 0, 0, 0))
od = ImageDraw.Draw(over)
for x in range(0, W, 48):                                   # faint grid, fading towards the edges
    od.line((x, 0, x, H), fill=(255, 255, 255, 7))
for y in range(0, H, 48):
    od.line((0, y, W, y), fill=(255, 255, 255, 7))
random.seed(7)                                             # the network motif: a server and the PCs it boots
hub = (1520, 300)
nodes = [(hub[0] + int(260 * math.cos(a)), hub[1] + int(170 * math.sin(a))) for a in [i * 2 * math.pi / 9 + 0.3 for i in range(9)]]
for n in nodes:
    od.line((hub, n), fill=(ACC[0], ACC[1], ACC[2], 38), width=2)
    od.rounded_rectangle((n[0] - 22, n[1] - 13, n[0] + 22, n[1] + 13), 6, outline=(255, 255, 255, 40), width=2, fill=(255, 255, 255, 10))
    for k in (0.33, 0.66):
        p = (int(hub[0] + (n[0] - hub[0]) * k), int(hub[1] + (n[1] - hub[1]) * k))
        od.ellipse((p[0] - 3, p[1] - 3, p[0] + 3, p[1] + 3), fill=(ACC2[0], ACC2[1], ACC2[2], 90))
od.rounded_rectangle((hub[0] - 46, hub[1] - 24, hub[0] + 46, hub[1] + 24), 10, fill=(ACC[0], ACC[1], ACC[2], 70))
for rr in (70, 110):
    od.ellipse((hub[0] - rr, hub[1] - rr * 0.7, hub[0] + rr, hub[1] + rr * 0.7), outline=(ACC[0], ACC[1], ACC[2], 28), width=2)
mask = Image.new("L", (W, H), 0)
ImageDraw.Draw(mask).ellipse((-W * 0.2, -H * 0.3, W * 1.2, H * 1.25), fill=255)
mask = mask.filter(ImageFilter.GaussianBlur(160))
over.putalpha(Image.composite(over.getchannel("A"), Image.new("L", (W, H), 0), mask))
bg = Image.alpha_composite(bg.convert("RGBA"), over).convert("RGB")
save(bg, "background.png")


# ---------------------------------------------------------------- logo: the power mark in a rounded red square
def logo(size):
    s = size * 4
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, s - 1, s - 1), int(s * 0.2), fill=BRAND)
    w = int(s * 0.1)
    c, r = s / 2, s * 0.26
    d.arc((c - r, c - r + s * 0.04, c + r, c + r + s * 0.04), 300, 240, fill="white", width=w)
    d.line((c, s * 0.22, c, s * 0.5), fill="white", width=w)
    d.ellipse((c - w / 2, s * 0.22 - w / 2, c + w / 2, s * 0.22 + w / 2), fill="white")
    return im.resize((size, size), Image.LANCZOS)


def wordmark():
    """The top of the menu: a compact mark + "KwickBoot" in one PNG (a real font, anti-aliased - crisper than GRUB text).
    An official logo can replace it: KB_LOGO=/path/logo.png (scaled to 68 px high)."""
    from PIL import ImageFont
    H, BS, FS, GAP = 68, 56, 50, 16          # overall height, badge size, font size, gap mark->text (compact, refined)
    own = os.environ.get("KB_LOGO")
    if own and os.path.isfile(own):
        im = Image.open(own).convert("RGBA")
        return im.resize((int(im.width * H / im.height), H), Image.LANCZOS)
    font = None
    for f in ("/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        if os.path.isfile(f):
            font = ImageFont.truetype(f, FS)
            break
    font = font or ImageFont.load_default()
    mark = logo(BS); by = (H - BS) // 2
    tw = int(ImageDraw.Draw(Image.new("RGBA", (1, 1))).textlength("KwickBoot", font=font))
    im = Image.new("RGBA", (BS + GAP + tw + 6, H), (0, 0, 0, 0))
    shadow = Image.new("RGBA", im.size, (0, 0, 0, 0))     # a soft, subtle glow under the mark (not a heavy drop shadow)
    ImageDraw.Draw(shadow).rounded_rectangle((2, by + 3, 2 + BS, by + 3 + BS), int(BS * 0.28), fill=(BRAND[0], BRAND[1], BRAND[2], 90))
    im = Image.alpha_composite(im, shadow.filter(ImageFilter.GaussianBlur(8)))
    im.alpha_composite(mark, (0, by))
    ImageDraw.Draw(im).text((BS + GAP, (H - FS) // 2 - 5), "KwickBoot", font=font, fill=(244, 241, 234, 255))
    return im


_wm = wordmark()
LW = _wm.width
WMH = _wm.height
save(_wm, "logo.png")


# ---------------------------------------------------------------- 9-slice pixmaps: menu panel + selected item
def nine(prefix, w, h, radius, fill, outline=None, glow_col=None, grad=None):
    s = 4
    big = Image.new("RGBA", (w * s, h * s), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    if grad:
        g = Image.new("RGBA", (w * s, h * s))
        gp = g.load()
        for x in range(w * s):
            t = x / (w * s - 1)
            gp_col = tuple(int(grad[0][i] * (1 - t) + grad[1][i] * t) for i in range(4))
            for y in range(h * s):
                gp[x, y] = gp_col
        m = Image.new("L", (w * s, h * s), 0)
        ImageDraw.Draw(m).rounded_rectangle((0, 0, w * s - 1, h * s - 1), radius * s, fill=255)
        big = Image.composite(g, big, m)
        d = ImageDraw.Draw(big)
    else:
        d.rounded_rectangle((0, 0, w * s - 1, h * s - 1), radius * s, fill=fill)
    if outline:
        d.rounded_rectangle((0, 0, w * s - 1, h * s - 1), radius * s, outline=outline, width=s)
    im = big.resize((w, h), Image.LANCZOS)
    c = radius + 2 if radius > 10 else radius   # thin borders: GRUB adds the edge slices around each item
    parts = {"nw": (0, 0, c, c), "n": (c, 0, w - c, c), "ne": (w - c, 0, w, c), "w": (0, c, c, h - c), "c": (c, c, w - c, h - c),
             "e": (w - c, c, w, h - c), "sw": (0, h - c, c, h), "s": (c, h - c, w - c, h), "se": (w - c, h - c, w, h)}
    for k, box in parts.items():
        save(im.crop(box), f"{prefix}_{k}.png")


nine("menu", 120, 120, 22, (14, 19, 36, 214), outline=(255, 255, 255, 26))
nine("select", 120, 40, 6, None, outline=(255, 200, 170, 110), grad=((ACC[0], ACC[1], ACC[2], 235), (196, 72, 52, 235)))


# ---------------------------------------------------------------- icons (one per menu class), drawn as simple line art
def icon(name, draw_fn, size=36):
    s = size * 4
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    draw_fn(d, s, (230, 236, 245, 255), int(s * 0.085))
    save(im.resize((size, size), Image.LANCZOS), f"icons/{name}.png")


def i_run(d, s, c, w):          # play triangle in a circle
    d.ellipse((w, w, s - w, s - w), outline=c, width=w)
    d.polygon([(s * 0.42, s * 0.32), (s * 0.42, s * 0.68), (s * 0.70, s * 0.5)], fill=c)


def i_install(d, s, c, w):      # arrow down into a drive
    d.line((s / 2, s * 0.12, s / 2, s * 0.56), fill=c, width=w)
    d.polygon([(s * 0.30, s * 0.44), (s * 0.70, s * 0.44), (s / 2, s * 0.66)], fill=c)
    d.rounded_rectangle((s * 0.12, s * 0.70, s * 0.88, s * 0.90), s * 0.05, outline=c, width=w)


def i_wipe(d, s, c, w):         # shield with a check
    pts = [(s / 2, s * 0.08), (s * 0.86, s * 0.22), (s * 0.80, s * 0.62), (s / 2, s * 0.92), (s * 0.20, s * 0.62), (s * 0.14, s * 0.22)]
    d.line(pts + [pts[0]], fill=c, width=w, joint="curve")
    d.line([(s * 0.33, s * 0.50), (s * 0.46, s * 0.63), (s * 0.68, s * 0.38)], fill=c, width=w, joint="curve")


def i_rescue(d, s, c, w):       # life ring
    d.ellipse((w, w, s - w, s - w), outline=c, width=w)
    d.ellipse((s * 0.32, s * 0.32, s * 0.68, s * 0.68), outline=c, width=w)
    for a in (45, 135, 225, 315):
        r1, r2 = s * 0.18, s * 0.40
        x1, y1 = s / 2 + r1 * math.cos(math.radians(a)), s / 2 + r1 * math.sin(math.radians(a))
        x2, y2 = s / 2 + r2 * math.cos(math.radians(a)), s / 2 + r2 * math.sin(math.radians(a))
        d.line((x1, y1, x2, y2), fill=c, width=int(w * 1.6))


def i_text(d, s, c, w):         # terminal prompt
    d.rounded_rectangle((w, s * 0.16, s - w, s * 0.84), s * 0.08, outline=c, width=w)
    d.line([(s * 0.26, s * 0.38), (s * 0.40, s * 0.50), (s * 0.26, s * 0.62)], fill=c, width=w)
    d.line((s * 0.48, s * 0.64, s * 0.72, s * 0.64), fill=c, width=w)


def i_safe(d, s, c, w):         # monitor
    d.rounded_rectangle((s * 0.08, s * 0.14, s * 0.92, s * 0.70), s * 0.06, outline=c, width=w)
    d.line((s / 2, s * 0.70, s / 2, s * 0.84), fill=c, width=w)
    d.line((s * 0.30, s * 0.86, s * 0.70, s * 0.86), fill=c, width=w)


def i_next(d, s, c, w):         # arrow right
    d.line((s * 0.14, s / 2, s * 0.80, s / 2), fill=c, width=w)
    d.line([(s * 0.56, s * 0.26), (s * 0.84, s / 2), (s * 0.56, s * 0.74)], fill=c, width=w, joint="curve")


def i_dos(d, s, c, w):          # floppy disk
    d.rounded_rectangle((s * 0.14, s * 0.14, s * 0.86, s * 0.86), s * 0.06, outline=c, width=w)
    d.rectangle((s * 0.32, s * 0.14, s * 0.66, s * 0.36), outline=c, width=w)
    d.rounded_rectangle((s * 0.28, s * 0.54, s * 0.72, s * 0.86), s * 0.03, outline=c, width=w)


def i_shell(d, s, c, w):        # "EFI" chip with a prompt
    d.rounded_rectangle((s * 0.18, s * 0.18, s * 0.82, s * 0.82), s * 0.08, outline=c, width=w)
    for k in (0.32, 0.5, 0.68):
        d.line((s * k, s * 0.06, s * k, s * 0.18), fill=c, width=w)
        d.line((s * k, s * 0.82, s * k, s * 0.94), fill=c, width=w)
    d.line([(s * 0.32, s * 0.40), (s * 0.44, s * 0.50), (s * 0.32, s * 0.60)], fill=c, width=w)
    d.line((s * 0.50, s * 0.62, s * 0.66, s * 0.62), fill=c, width=w)


def i_disk(d, s, c, w):         # installed disk: a hard drive with a platter
    d.rounded_rectangle((s * 0.14, s * 0.22, s * 0.86, s * 0.78), s * 0.09, outline=c, width=w)
    d.ellipse((s * 0.38, s * 0.38, s * 0.62, s * 0.62), outline=c, width=w)
    d.ellipse((s * 0.47, s * 0.47, s * 0.53, s * 0.53), fill=c)


for n, f in (("run", i_run), ("install", i_install), ("wipe", i_wipe), ("rescue", i_rescue), ("text", i_text), ("safe", i_safe),
             ("next", i_next), ("dos", i_dos), ("shell", i_shell), ("disk", i_disk)):
    icon(n, f)

# ---------------------------------------------------------------- text as pictures
# Under UEFI Secure Boot, Debian's signed GRUB refuses to load font files ("prohibited by secure boot policy"), so only
# its small built-in font exists there. All fixed text is therefore drawn here with a real font - it looks the same with
# or without Secure Boot. Only the menu entries use GRUB's font (ASCII titles, see make-iso.sh).
from PIL import ImageFont


def font(size, bold=False):
    for f in ("/usr/share/fonts/truetype/noto/NotoSans-%s.ttf" % ("Bold" if bold else "Regular"),
              "/usr/share/fonts/truetype/dejavu/DejaVuSans%s.ttf" % ("-Bold" if bold else "")):
        if os.path.isfile(f):
            return ImageFont.truetype(f, size)
    return ImageFont.load_default()


def text_png(name, text, size, color, bold=False):
    f = font(size, bold)
    w = int(ImageDraw.Draw(Image.new("RGBA", (1, 1))).textlength(text, font=f)) + 4
    im = Image.new("RGBA", (w, int(size * 1.45)), (0, 0, 0, 0))
    ImageDraw.Draw(im).text((2, 0), text, font=f, fill=color)
    save(im, name)
    return im.width


def keys_png(name, parts, size=19):
    """Key hints as keycaps: [("↑ ↓", "choose"), ("Enter", "start"), ...]"""
    f, fk = font(size), font(size - 2, True)
    d0 = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    gap, pad, hgt = 34, 10, int(size * 1.7)
    w = sum(int(d0.textlength(k, font=fk)) + 2 * pad + 10 + int(d0.textlength(t, font=f)) + gap for k, t in parts)
    im = Image.new("RGBA", (w, hgt + 4), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    x = 0
    for k, t in parts:
        kw = int(d.textlength(k, font=fk)) + 2 * pad
        d.rounded_rectangle((x, 2, x + kw, hgt), 7, fill=(255, 255, 255, 22), outline=(255, 255, 255, 60), width=1)
        d.text((x + pad, (hgt - size) // 2 - 1), k, font=fk, fill=(230, 234, 242, 255))
        x += kw + 10
        d.text((x, (hgt - size) // 2), t, font=f, fill=(154, 163, 181, 255))
        x += int(d.textlength(t, font=f)) + gap
    save(im, name)
    return im.width


SW = text_png("subtitle.png", "AI deployment system", 26, (170, 178, 196, 255))
KW = keys_png("keys.png", [("↑ ↓", "choose"), ("Enter", "start"), ("E", "edit"), ("C", "command line")])
FL = text_png("footer-left.png", f"KwickBoot {VER}      www.kwickboot.com", 18, (125, 133, 153, 255))
FR = text_png("footer-right.png", "UEFI  ·  Secure Boot  ·  legacy BIOS", 16, (93, 101, 121, 255))

# ---------------------------------------------------------------- theme.txt
open(os.path.join(OUT, "theme.txt"), "w").write(f"""# KwickBoot boot menu (GRUB gfxmenu) - generated by linux/grub-theme.py
title-text: ""
desktop-image: "background.png"
desktop-image-scale-method: "stretch"
desktop-color: "#070a14"
terminal-box: "menu_*.png"

+ image {{ left = 50%-{LW // 2} top = 7% file = "logo.png" }}
+ image {{ left = 50%-{SW // 2} top = 7%+{WMH + 6} file = "subtitle.png" }}

+ boot_menu {{
    left = 14% top = 24% width = 72% height = 54%
    menu_pixmap_style = "menu_*.png"
    item_font = "KB Sans Regular 20" selected_item_font = "KB Sans Bold 20"
    item_color = "#cfd6e4" selected_item_color = "#ffffff"
    item_height = 40 item_padding = 14 item_spacing = 14
    icon_width = 32 icon_height = 32 item_icon_space = 16
    selected_item_pixmap_style = "select_*.png"
    scrollbar = true scrollbar_width = 6 scrollbar_thumb = "select_*.png"
}}

+ label {{ id = "__timeout__" left = 0 top = 81% width = 100% align = "center" color = "#7d8599" text = "Starting in %d s" }}
+ progress_bar {{
    id = "__timeout__" left = 36% top = 84% width = 28% height = 5
    fg_color = "#d97757" bg_color = "#1b2236" border_color = "#1b2236" show_text = false
}}
+ image {{ left = 50%-{KW // 2} top = 89% file = "keys.png" }}
+ image {{ left = 2% top = 96% file = "footer-left.png" }}
+ image {{ left = 98%-{FR} top = 96%+2 file = "footer-right.png" }}
""")
print("theme:", OUT)
