#!/usr/bin/env python3
# gui-chrome.py <out-dir> [--all | --langs en,zh,ar] - render the KwickBoot local GUI "furniture" NATIVELY at several real
# screen resolutions AND in each UI language, so the text is pixel-crisp on each monitor and localised. For every
# (language, target) it emits chrome-<lang>-<W>x<H>.bgra.gz + .info (+ .png), and extracts a matching Terminus console
# font (font-<w>x<h>.raw, the raw kbgui draws the live VALUES with - Latin/Cyrillic only; the baked LABELS can be any
# script because PIL rasterises them here with the right Noto font + raqm shaping). kbgui picks the largest set that fits
# the framebuffer and blits it 1:1. Core ships only the default language (en, all resolutions); kbgui fetches other
# languages on demand from the server. Labels come from http/local/i18n.js (the single source of truth, shared with the
# browser tier). Layout is defined in 1920x1080 space and scaled by s = W/1920 (16:9).
# SPDX-License-Identifier: AGPL-3.0-or-later   Run on the build box (needs python3-pil with raqm + DejaVu/Noto fonts).
import sys, os, re, gzip, struct, json
from PIL import Image, ImageDraw, ImageFont

# ---- args ----
args = sys.argv[1:]
OUT = "."
want = None          # None -> just the default language (en); or a list of codes; "ALL" -> every language
i = 0
rest = []
while i < len(args):
    a = args[i]
    if a == "--all": want = "ALL"
    elif a == "--langs": i += 1; want = [c.strip() for c in args[i].split(",") if c.strip()]
    else: rest.append(a)
    i += 1
if rest: OUT = rest[0]
os.makedirs(OUT, exist_ok=True)

BG=(21,27,44); CARD=(28,36,58); CARD2=(33,42,66); BRAND=(195,43,39); ACC=(217,119,87)
TXT=(222,228,240); MUT=(132,144,172); LINE=(46,57,86); GREEN=(74,200,120); WHT=(255,255,255)
CF="/usr/share/consolefonts/"
TARGETS = [1920, 1600, 1366, 1280, 1024, 800]
try: RAQM = ImageFont.Layout.RAQM
except Exception: RAQM = getattr(ImageFont, "LAYOUT_RAQM", 1)

# ---- fonts: pick a face that covers the language's script (PIL has no auto-fallback) ----
DEJAVU = "/usr/share/fonts/truetype/dejavu/"
NOTO   = "/usr/share/fonts/truetype/noto/"
NOTOCJK= "/usr/share/fonts/opentype/noto/"
# script -> (regular, bold, ttc-index, direction)
FONTSPEC = {
 "latin": (DEJAVU+"DejaVuSans.ttf",        DEJAVU+"DejaVuSans-Bold.ttf",        0, "ltr"),
 "cjk":   (NOTOCJK+"NotoSansCJK-Regular.ttc", NOTOCJK+"NotoSansCJK-Bold.ttc",    0, "ltr"),
 "arab":  (NOTO+"NotoSansArabic-Regular.ttf", NOTO+"NotoSansArabic-Bold.ttf",   0, "rtl"),
 "deva":  (NOTO+"NotoSansDevanagari-Regular.ttf", NOTO+"NotoSansDevanagari-Bold.ttf", 0, "ltr"),
 "thai":  (NOTO+"NotoSansThai-Regular.ttf", NOTO+"NotoSansThai-Bold.ttf",       0, "ltr"),
}
LANG_SCRIPT = {"zh":"cjk","ja":"cjk","ko":"cjk","ar":"arab","hi":"deva","th":"thai"}
def script_of(lang): return LANG_SCRIPT.get(lang, "latin")

_FCACHE = {}
def getfontS(script, size, bold=False):
    size = max(8, int(round(size)))
    reg, bld, idx, _ = FONTSPEC[script]
    path = bld if (bold and os.path.isfile(bld)) else reg
    key = (path, size, idx)
    if key in _FCACHE: return _FCACHE[key]
    try: fnt = ImageFont.truetype(path, size, index=idx, layout_engine=RAQM)
    except Exception:
        try: fnt = ImageFont.truetype(path, size, index=idx)
        except Exception: fnt = ImageFont.load_default()
    _FCACHE[key] = fnt; return fnt
def getfont(lang, size, bold=False): return getfontS(script_of(lang), size, bold)

# Noto Arabic/Devanagari/Thai carry NO Latin glyphs, so brand/acronym tokens (KwickBoot, TPM, AI, the URL) tofu when
# drawn with them. Split a string into Latin vs script runs and draw each with the font that covers it; for RTL (Arabic)
# lay the runs out right-to-left so the mix reads correctly.
_COV = {}
def coverage(path):                                # set of codepoints a font file actually has glyphs for
    if path in _COV: return _COV[path]
    try:
        from fontTools.ttLib import TTFont
        if path.lower().endswith(".ttc"): cov = set(TTFont(path, fontNumber=0).getBestCmap().keys())
        else: cov = set(TTFont(path).getBestCmap().keys())
    except Exception: cov = None
    _COV[path] = cov; return cov
_MIRROR = str.maketrans("()[]{}<>", ")(][}{><")    # RTL: a logical "(" shows as ")" etc.
def has_rtl(s): return any(0x0590 <= ord(c) <= 0x08FF for c in s)   # Hebrew + Arabic blocks (a strong-RTL char)
def runs_for(text, scr, bold):                     # split into (isLatin, text) runs by which font covers each char
    sreg, sbld = FONTSPEC[scr][0], FONTSPEC[scr][1]
    spath = sbld if (bold and os.path.isfile(sbld)) else sreg
    cov = coverage(spath)
    out = []; buf = ""; cls = None
    for ch in text:
        if ch == " ": buf += ch; continue          # space is in both -> keep with the current run
        isl = not (cov is None or ord(ch) in cov)   # Latin (DejaVu) only when the script font lacks the glyph
        if cls is None: cls = isl
        if isl != cls: out.append((cls, buf)); buf = ch; cls = isl
        else: buf += ch
    if buf != "": out.append((cls if cls is not None else False, buf))
    return out

# ---- strings: parse http/local/i18n.js (single source of truth) ----
HERE = os.path.dirname(os.path.abspath(__file__))
I18N_JS = os.path.join(HERE, "..", "http", "local", "i18n.js")
PAIR = re.compile(r'(\w+)\s*:\s*"((?:[^"\\]|\\.)*)"')
def load_i18n(path):
    langs = {}
    try: text = open(path, encoding="utf-8").read()
    except OSError: return langs
    for ln in text.splitlines():
        m = re.match(r'\s*([a-z]{2}):\{', ln)
        if not m: continue
        code = m.group(1); d = langs.setdefault(code, {})
        for k, v in PAIR.findall(ln):
            d.setdefault(k, v.encode().decode("unicode_escape") if "\\u" in v else v)
    return langs
I18N = load_i18n(I18N_JS)

# baked label -> (i18n key, english fallback). Missing key -> English stays (safe).
def T(L, key, default): return (L.get(key) or default)

def psf_to_raw(psf, out):   # extract a PSF console font to kbgui's raw format: [w,h,rowbytes,0] + 256 glyphs
    d = gzip.open(psf, "rb").read()
    if d[:4] == b"\x72\xb5\x4a\x86":
        _,hs,_,length,cs,h,w = struct.unpack("<IIIIIII", d[4:32]); rb = cs//h; glyphs = d[hs:hs+cs*min(length,256)]
    elif d[0]==0x36 and d[1]==0x04:
        h=d[3]; w=8; rb=1; cs=h; glyphs=d[4:4+cs*256]
    else: raise SystemExit("bad PSF "+psf)
    buf = bytearray([w,h,rb,0]) + bytearray(glyphs[:cs*256])
    if len(buf) < 4+cs*256: buf += bytearray(4+cs*256-len(buf))
    open(out,"wb").write(buf); return w,h
def fontpsf(w):
    s = w/1920.0
    for cell, name in [(24,"CyrAsia-TerminusBold24x12"),(20,"CyrAsia-TerminusBold20x10"),
                       (18,"CyrAsia-TerminusBold18x10"),(16,"CyrAsia-TerminusBold16"),(14,"CyrAsia-TerminusBold14")]:
        if round(24*s) >= cell: return CF+name+".psf.gz"
    return CF+"CyrAsia-TerminusBold14.psf.gz"

def logo(size):
    s=size*4; im=Image.new("RGBA",(s,s),(0,0,0,0)); d=ImageDraw.Draw(im)
    d.rounded_rectangle((0,0,s-1,s-1),int(s*0.2),fill=BRAND); w=int(s*0.1); c,r=s/2,s*0.26
    d.arc((c-r,c-r+s*0.04,c+r,c+r+s*0.04),300,240,fill="white",width=w)
    d.line((c,s*0.22,c,s*0.5),fill="white",width=w); d.ellipse((c-w/2,s*0.22-w/2,c+w/2,s*0.22+w/2),fill="white")
    return im.resize((max(1,size),max(1,size)),Image.LANCZOS)

def render(W, lang, L):
    s = W/1920.0; H = round(W*9/16.0)
    scr = script_of(lang); RTL = FONTSPEC[scr][3] == "rtl"; MIX = scr in ("arab","deva","thai")
    def S(v): return int(round(v*s))
    ZONES=[]
    def z(k,x,y,*rest): ZONES.append((k,S(x),S(y))+tuple(S(v) for v in rest))
    img=Image.new("RGBA",(W,H),BG+(255,)); d=ImageDraw.Draw(img)
    def wid(strg, px, bold):                 # measured width of a (possibly mixed) string
        rh = RTL and has_rtl(strg)
        if not MIX:
            try: return d.textlength(strg, font=getfont(lang,px,bold), direction=("rtl" if rh else "ltr"))
            except Exception: return d.textlength(strg, font=getfont(lang,px,bold))
        tot=0
        for isl,run in runs_for(strg, scr, bold):
            f=getfontS("latin" if isl else scr, px, bold)
            try: tot+=d.textlength(run, font=f, direction=("rtl" if (rh and not isl) else "ltr"))
            except Exception: tot+=d.textlength(run, font=f)
        return tot
    def txt(xy, strg, px, bold, fill):       # all translatable text; mixes Latin + script fonts, per-string RTL base
        rh = RTL and has_rtl(strg)           # a pure-Latin brand/URL stays LTR even on an Arabic page
        if not MIX:
            f=getfont(lang,px,bold)
            try: d.text(xy, strg, font=f, fill=fill, direction=("rtl" if rh else "ltr"))
            except Exception: d.text(xy, strg, font=f, fill=fill)
            return
        runs=runs_for(strg, scr, bold); ws=[wid(r,px,bold) for _,r in runs]
        order=list(range(len(runs)))[::-1] if rh else list(range(len(runs)))
        base=getfontS(scr,px,bold).getmetrics()[0]          # align every run to the script font's baseline
        x=xy[0]
        for i in order:
            isl,run=runs[i]; f=getfontS("latin" if isl else scr, px, bold)
            if rh and isl: run=run.translate(_MIRROR)       # mirror brackets that fell back to the Latin font
            yy=xy[1]+base-f.getmetrics()[0]
            try: d.text((x,yy), run, font=f, fill=fill, direction=("rtl" if (rh and not isl) else "ltr"))
            except Exception: d.text((x,yy), run, font=f, fill=fill)
            x+=ws[i]
    img.alpha_composite(logo(S(64)),(S(48),S(40))); d=ImageDraw.Draw(img)
    txt((S(130),S(40)),"KwickBoot",40*s,True,(244,241,234,255))
    txt((S(132),S(88)),T(L,"subtitle","the RAM OS with AI built in"),20*s,False,ACC+(255,))
    z("statuschip",W/s-300,52)
    d.line((S(48),S(130),W-S(48),S(130)),fill=LINE+(255,),width=1)
    def card(x,y,w,hh,title):
        d.rounded_rectangle((S(x),S(y),S(x+w),S(y+hh)),S(14),fill=CARD+(255,),outline=LINE+(255,),width=1)
        txt((S(x+24),S(y+18)),title,24*s,True,TXT+(255,)); return x+24,y+58
    def label(x,y,k): txt((S(x),S(y)),k,18*s,False,MUT+(255,))
    mx,my=card(48,160,900,360,T(L,"thisMachine","This machine"))
    for i,(k,dflt) in enumerate((("model","Model"),("serial","Serial"),("cpu","CPU"),
                                 ("memory","Memory"),("firmware","Firmware"),("tpm","TPM"))):
        label(mx,my+6+i*38,T(L,k,dflt)); z("m_"+k,mx+170,my+4+i*38)
    label(mx,my+240,T(L,"disks","Disks")); z("disks",mx,my+268)
    nx,ny=card(972,160,900,160,T(L,"network","Network"))
    label(nx,ny+6,T(L,"address","Address")); z("net_addr",nx+170,ny+4)
    label(nx,ny+44,T(L,"adapter","Adapter")); z("net_nic",nx+170,ny+42)
    ax,ay=card(972,344,900,176,T(L,"adviceTitle","AI install advice")); z("ai",ax,ay,3)
    d.rounded_rectangle((S(48),S(560),W-S(48),S(660)),S(14),fill=CARD2+(255,),outline=LINE+(255,),width=1)
    z("status",76,584); z("status2",76,628)
    def tile(k,x,w,title,subtitle,primary=False,lock=False):
        fill=BRAND if primary else CARD
        d.rounded_rectangle((S(x),S(720),S(x+w),S(860)),S(16),fill=fill+(255,),outline=(BRAND if primary else LINE)+(255,),width=1)
        txt((S(x+26),S(740)),title,24*s,True,WHT+(255,) if primary else TXT+(255,))
        txt((S(x+26),S(784)),subtitle,15*s,False,(255,255,255,200) if primary else MUT+(255,))
        if lock:
            lx,ly=S(x+w-54),S(748); d.arc((lx,ly,lx+S(18),ly+S(18)),180,360,fill=MUT,width=max(2,S(3))); d.rounded_rectangle((lx-S(3),ly+S(8),lx+S(21),ly+S(26)),S(3),fill=MUT+(255,))
        z("tile_"+k,x,720,w,140)
    tile("install",48,420,T(L,"installTile","Install an OS"),T(L,"installTileD","Inspect, pick, deploy, right here"),primary=True)
    tile("details",492,300,T(L,"detailsTile","Hardware details"),T(L,"detailsTileD","Full inventory & health"))
    tile("rescue",816,300,T(L,"rescueTile","Rescue"),T(L,"rescueTileD","Repair, recover, wipe"),lock=True)
    tile("language",1140,300,T(L,"langTile","Language"),T(L,"chooseLang","Choose a language"))
    tile("connect",1464,408,T(L,"connectTile","Let IT connect"),T(L,"connectTileD","Share an SSH code with your admin"))
    txt((S(48),H-S(34)),T(L,"footer","KwickBoot - any PC, no touch"),15*s,False,MUT+(255,))
    fw=wid("www.kwickboot.com",15*s,False); txt((W-S(48)-fw,H-S(34)),"www.kwickboot.com",15*s,False,MUT+(255,))
    # value font for this resolution
    psf=fontpsf(W); tmp=OUT+"/.probe.raw"; fw2,fh2=psf_to_raw(psf,tmp); fontname="font-%dx%d.raw"%(fw2,fh2)
    os.replace(tmp, OUT+"/"+fontname)
    # write chrome + info (BGRA)
    b=bytearray(img.convert("RGBA").tobytes())
    for i in range(0,len(b),4): b[i],b[i+2]=b[i+2],b[i]
    tag="%s-%dx%d"%(lang,W,H)
    with gzip.open(OUT+"/chrome-%s.bgra.gz"%tag,"wb") as fp: fp.write(bytes(b))
    with open(OUT+"/chrome-%s.info"%tag,"w") as fp:
        fp.write("W=%d\nH=%d\nLANG=%s\nDIR=%s\nFONT=%s\n"%(W,H,lang,("rtl" if RTL else "ltr"),fontname))
        for zz in ZONES: fp.write("Z "+zz[0]+" "+",".join(str(v) for v in zz[1:])+"\n")
    img.convert("RGB").save(OUT+"/chrome-%s.png"%tag)
    return tag,len(ZONES),fontname

# ---- which languages ----
if want == "ALL": langs = sorted(I18N.keys()) or ["en"]
elif want: langs = want
else: langs = ["en"]
# en must render even if i18n.js failed to load (English fallbacks cover it)
if "en" not in I18N: I18N["en"] = {}

mani=[]
for lang in langs:
    L = I18N.get(lang, {})
    for W in TARGETS:
        tag,nz,fn=render(W, lang, L); mani.append(tag)
    print("lang %-3s: %s"%(lang, " ".join("%dx%d"%(W,round(W*9/16)) for W in TARGETS)))
open(OUT+"/manifest","w").write("\n".join(mani)+"\n")
print("languages:", " ".join(langs), "| %d chrome sets"%len(mani))
