#!/usr/bin/env python3
# gui-mock.py <out.png> - a visual mockup of the KwickBoot local machine GUI (framebuffer, mouse-driven). Not shipped;
# it exists to agree on the look before building the engine. Same brand as the splash / boot menu.
import sys, os
from PIL import Image, ImageDraw, ImageFont
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/gui.png"
W, H = 1920, 1080
BG=(21,27,44); CARD=(28,36,58); CARD2=(33,42,66); BRAND=(195,43,39); ACC=(217,119,87)
TXT=(222,228,240); MUT=(132,144,172); LINE=(46,57,86); GREEN=(74,200,120); WHT=(255,255,255)
FD="/usr/share/fonts/truetype/dejavu/"
def f(p,s): return ImageFont.truetype(FD+p,s) if os.path.isfile(FD+p) else ImageFont.load_default()
h1=f("DejaVuSans-Bold.ttf",40); h2=f("DejaVuSans-Bold.ttf",24); lab=f("DejaVuSans.ttf",18)
val=f("DejaVuSans-Bold.ttf",22); big=f("DejaVuSans-Bold.ttf",30); slog=f("DejaVuSans.ttf",20); tiny=f("DejaVuSans.ttf",15)

def logo(size):
    s=size*4; im=Image.new("RGBA",(s,s),(0,0,0,0)); d=ImageDraw.Draw(im)
    d.rounded_rectangle((0,0,s-1,s-1),int(s*0.2),fill=BRAND); w=int(s*0.1); c,r=s/2,s*0.26
    d.arc((c-r,c-r+s*0.04,c+r,c+r+s*0.04),300,240,fill="white",width=w)
    d.line((c,s*0.22,c,s*0.5),fill="white",width=w); d.ellipse((c-w/2,s*0.22-w/2,c+w/2,s*0.22+w/2),fill="white")
    return im.resize((size,size),Image.LANCZOS)

img=Image.new("RGBA",(W,H),BG+(255,)); d=ImageDraw.Draw(img)
# header
img.alpha_composite(logo(64),(48,40)); d=ImageDraw.Draw(img)
d.text((130,40),"KwickBoot",font=h1,fill=(244,241,234,255)); d.text((132,88),"the RAM OS with AI built in",font=slog,fill=ACC+(255,))
# status chip (top-right)
d.rounded_rectangle((W-300,52,W-48,100),10,fill=(18,46,30,255))
d.ellipse((W-284,70,W-268,86),fill=GREEN); d.text((W-258,64),"READY · managed",font=lab,fill=GREEN+(255,))
d.line((48,130,W-48,130),fill=LINE+(255,),width=1)

def card(x,y,w,hh,title):
    d.rounded_rectangle((x,y,x+w,y+hh),14,fill=CARD+(255,),outline=LINE+(255,),width=1)
    d.text((x+24,y+18),title,font=h2,fill=TXT+(255,)); return x+24,y+58
def row(x,y,k,v,vc=TXT):
    d.text((x,y),k,font=lab,fill=MUT+(255,)); d.text((x+170,y-2),v,font=val,fill=vc+(255,))

# This machine
mx,my=card(48,160,900,360,"This machine")
row(mx,my+6,"Model","Dell OptiPlex 7090"); row(mx,my+44,"Serial","7KQ2X13")
row(mx,my+82,"CPU","Intel Core i7-11700  ·  8c/16t"); row(mx,my+120,"Memory","32 GB")
row(mx,my+158,"Firmware","UEFI · Secure Boot ON",GREEN); row(mx,my+196,"TPM","2.0")
d.text((mx,my+240),"Disks",font=lab,fill=MUT+(255,))
row(mx,my+268,"nvme0n1","512 GB NVMe  ·  SK hynix  ·  SMART OK",TXT)
# Network + AI advice
nx,ny=card(972,160,900,160,"Network")
row(nx,ny+6,"Address","192.168.0.105  ·  1 Gb/s",GREEN); row(nx,ny+44,"Adapter","Intel I219-LM")
ax,ay=card(972,344,900,176,"AI install advice")
d.text((ax,ay),"★  Recommended: Windows 11 (unattended) — this model + TPM 2.0 + Secure Boot",font=lab,fill=(236,224,198,255))
d.text((ax,ay+34),"•  Drivers: Dell pack matched by SKU, injected at setup",font=lab,fill=MUT+(255,))
d.text((ax,ay+64),"•  No disk problems found — safe to install",font=lab,fill=GREEN+(255,))

# big status / progress strip
d.rounded_rectangle((48,560,W-48,660),14,fill=CARD2+(255,),outline=LINE+(255,),width=1)
d.text((76,584),"Idle — no job running.",font=big,fill=TXT+(255,))
d.text((76,626),"Walk up, pick a disk and an OS below, or let IT start it from the web console. Both stay in sync.",font=lab,fill=MUT+(255,))

# action tiles (bottom) - primary brand-red Install, then benign tiles
def tile(x,w,title,subtitle,primary=False,lock=False):
    fill=BRAND if primary else CARD
    d.rounded_rectangle((x,720,x+w,860),16,fill=fill+(255,),outline=(LINE if not primary else BRAND)+(255,),width=1)
    d.text((x+26,740),title,font=h2,fill=WHT+(255,) if primary else TXT+(255,))
    d.text((x+26,784),subtitle,font=tiny,fill=(255,255,255,200) if primary else MUT+(255,))
    if lock:
        lx,ly=x+w-54,748
        d.arc((lx,ly,lx+18,ly+18),180,360,fill=MUT,width=3); d.rounded_rectangle((lx-3,ly+8,lx+21,ly+26),3,fill=MUT+(255,))
tile(48,420,"Install an OS","Inspect → pick → deploy, right here",primary=True)
tile(492,300,"Hardware details","Full inventory & health")
tile(816,300,"Rescue","Repair, recover, wipe",lock=True)
tile(1140,300,"Language","Aa · العربية · Рус — 34 languages")
tile(1464,408,"Let IT connect","Share an SSH code with your admin")

d.text((48,H-34),"KwickBoot — any PC, no touch",font=tiny,fill=MUT+(255,))
fw=d.textlength("www.kwickboot.com",font=tiny); d.text((W-48-fw,H-34),"www.kwickboot.com",font=tiny,fill=MUT+(255,))

# mouse cursor (arrow) hovering the Install tile
cxp,cyp=360,790
d.polygon([(cxp,cyp),(cxp,cyp+26),(cxp+7,cyp+19),(cxp+12,cyp+30),(cxp+16,cyp+28),(cxp+11,cyp+17),(cxp+20,cyp+17)],fill=(0,0,0,255))
d.polygon([(cxp+2,cyp+3),(cxp+2,cyp+22),(cxp+7,cyp+17),(cxp+11,cyp+25),(cxp+13,cyp+24),(cxp+9,cyp+16),(cxp+16,cyp+16)],fill=WHT+(255,))

img.convert("RGB").save(OUT); print("wrote", OUT)
