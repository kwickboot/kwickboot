// kbgui - the KwickBoot local machine GUI (framebuffer + mouse). Picks the branded chrome rendered NATIVELY for the actual
// screen resolution (crisp text, no scaling), blits it, draws the live facts into its zones, and runs a mouse cursor +
// clickable tiles. A front-end only: a click writes the chosen action to /run/kb-gui-action for the agent/shell to carry
// out (gated by policy + local unlock elsewhere). The web console keeps working unchanged - both drive the same job.
// Exits 2 if there is no usable 32bpp framebuffer (-> text console).
// SPDX-License-Identifier: AGPL-3.0-or-later
//   kbgui <share-dir> [facts-file]        (share-dir holds chrome-<W>x<H>.{bgra.gz,info}, font-*.raw, manifest)
//   preview on the build box: KBFB_DUMP=out.ppm [KBFB_RES=WxH] kbgui <share-dir>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/ioctl.h>
#include <dirent.h>
#include <poll.h>
#include <signal.h>
#include <time.h>
#include <linux/fb.h>
#include <linux/kd.h>
#include <linux/input.h>
static int CW,CH;                            // chosen chrome dimensions (native render for this screen)
static unsigned char *chrome;                // BGRA furniture (pre-rendered, CW*CH*4)
static unsigned char *frame;                 // composed chrome + live values (CW*CH*4)
static unsigned char fontbuf[80000]; static int FW,FH,FRB; static unsigned char *FG;   // value font [w,h,rowbytes,0]+glyphs
struct Z { char n[24]; int x,y,a; };  static struct Z zones[80]; static int nz=0;
struct T { char n[24]; int x,y,w,h; } static_tiles[12]; static int nt=0;               // hit rects (chrome space)
static int tcur=-1;                                                                     // keyboard-focused dashboard tile (-1 = none / mouse only)
struct F { char k[24]; char v[200]; } facts[64]; static int nf=0;
static char fontname[64];
static char uilang[8]="en";                  // kbgui's OWN UI language (which chrome-<lang>-WxH set is shown)
static const char *SHARE_DIR=0, *FACTS_FILE=0;
static int switch_lang(const char*lang);     // defined below (reloads chrome for a language); used by the overlay handler
static unsigned char *fb; static long fblen; static int xres,yres,bpp,stride,ttyfd=-1;
static unsigned char *fbframe; static int DX,DY,DW,DH;   // where/how the chrome lands on the real framebuffer
static const unsigned char BG[4]={44,27,21,255};     // #151b2c  (BGRA, i.e. B,G,R,A)
static const unsigned char VAL[4]={240,228,222,255}, GRN[4]={120,200,74,255}, MUT[4]={172,144,132,255}, WHT[4]={255,255,255,255};
// overlay palette (BGR), matching the dashboard tokens. Red (BRANDc) is the LOGO colour only - the popup uses navy cards,
// a thin LINE border and the terracotta ACCent for emphasis/primary, never red chrome.
static const unsigned char CARDc[4]={58,36,28,255}, CARD2c[4]={66,42,33,255}, LINEc[4]={86,57,46,255};
static const unsigned char ACCc[4]={87,119,217,255}, BRANDc[4]={39,43,195,255};   // ACC #d97757, BRAND #c32b27
// modal overlay state: 0 none, else which tile opened it. Content is built into ovT/ovB from the live facts.
static int overlay=0; static char ovT[80], ovB[16][100]; static int ovN;
// clickable regions inside the overlay (screen coords), each tagged with an action: -1 Close/Cancel, -2 Apply, >=0 option index
static struct { int x,y,w,h,act; } ovhit[64]; static int novh; static int ov_sel=0; static int ovbtn=0;  // ovbtn: focused button in the confirm dialog (0=Cancel safe default, 1=Erase)
static void add_hit(int x,int y,int w,int h,int act){ if(novh<64){ ovhit[novh].x=x;ovhit[novh].y=y;ovhit[novh].w=w;ovhit[novh].h=h;ovhit[novh].act=act; novh++; } }
// languages the picker offers: label (romanised, Latin font), locale, keymap. The chosen locale applies the real language
// to the installed OS even though the on-screen label is romanised.
static const char* LANGS[][3] = {
  {"English","en_US.UTF-8","us"},{"Espanol","es_ES.UTF-8","es"},{"Francais","fr_FR.UTF-8","fr"},{"Deutsch","de_DE.UTF-8","de"},
  {"Italiano","it_IT.UTF-8","it"},{"Portugues","pt_BR.UTF-8","br"},{"Polski","pl_PL.UTF-8","pl"},{"Nederlands","nl_NL.UTF-8","nl"},
  {"Turkce","tr_TR.UTF-8","tr"},{"Cesky","cs_CZ.UTF-8","cz"},{"Russian","ru_RU.UTF-8","ru"},{"Ukrainian","uk_UA.UTF-8","ua"},
  {"Arabic","ar_SA.UTF-8","ara"},{"Hebrew","he_IL.UTF-8","il"},{"Chinese","zh_CN.UTF-8","cn"},{"Japanese","ja_JP.UTF-8","jp"},
  {"Korean","ko_KR.UTF-8","kr"},{"Thai","th_TH.UTF-8","th"},{"Vietnam","vi_VN.UTF-8","vn"},{"Hindi","hi_IN.UTF-8","in"},
  {"Svenska","sv_SE.UTF-8","se"} };
// The UI switches to the picked language's chrome when a chrome-<code>-WxH set exists (code = locale's first 2 letters);
// if not (e.g. Hebrew, which has no translation), the install locale still applies and the UI stays in its current language.
#define NLANGS ((int)(sizeof LANGS/sizeof LANGS[0]))
// installable-OS catalog, fetched by kb-gui-agent into /run/kb-oslist ("id|name|min_gb" per line)
static struct { char id[40]; char name[60]; int min_gb; } oslist[48]; static int nos=0, os_sel=0;
static void load_oslist(void){ nos=0; FILE*f=fopen("/run/kb-oslist","r"); if(!f)return; char ln[200];
  while(fgets(ln,sizeof ln,f)&&nos<48){ char*p1=strchr(ln,'|'); if(!p1)continue; *p1=0; char*nm=p1+1; char*p2=strchr(nm,'|'); int mg=0;
    if(p2){ *p2=0; mg=atoi(p2+1); } nm[strcspn(nm,"\n")]=0;
    strncpy(oslist[nos].id,ln,39); oslist[nos].id[39]=0; strncpy(oslist[nos].name,nm,59); oslist[nos].name[59]=0; oslist[nos].min_gb=mg; nos++; } fclose(f); }
// install-target disks, one per line "dev|size|type|model" from /run/kb-disks (written by kb-facts)
static struct { char dev[24]; char desc[80]; } disks[16]; static int ndisks=0, disk_sel=0;
static void load_disks(void){ ndisks=0; FILE*f=fopen("/run/kb-disks","r"); if(!f)return; char ln[160];
  while(fgets(ln,sizeof ln,f)&&ndisks<16){ ln[strcspn(ln,"\n")]=0; char*p1=strchr(ln,'|'); if(!p1)continue; *p1=0;
    char sz[24]="",ty[12]="",md[72]=""; char*p2=strchr(p1+1,'|'); char*p3=p2?strchr(p2+1,'|'):0;
    if(p2)*p2=0; if(p3)*p3=0; strncpy(sz,p1+1,23); if(p2)strncpy(ty,p2+1,11); if(p3)strncpy(md,p3+1,71);
    strncpy(disks[ndisks].dev,ln,23); disks[ndisks].dev[23]=0;
    snprintf(disks[ndisks].desc,80,"%-8s %8s  %-4s %s",ln,sz,ty,md); ndisks++; } fclose(f); }
// rescue/OS-library tools, one per line "name|title|role|desc" from /run/kb-tools (fetched by kb-gui-agent)
static struct { char name[40]; char title[60]; } rtools[8]; static int nrtools=0, rtool_sel=0;
static void load_tools(void){ nrtools=0; FILE*f=fopen("/run/kb-tools","r"); if(!f)return; char ln[200];
  while(fgets(ln,sizeof ln,f)&&nrtools<8){ ln[strcspn(ln,"\n")]=0; char*p1=strchr(ln,'|'); if(!p1)continue; *p1=0;
    char*ti=p1+1; char*p2=strchr(ti,'|'); if(p2)*p2=0;
    strncpy(rtools[nrtools].name,ln,39); rtools[nrtools].name[39]=0;
    strncpy(rtools[nrtools].title,ti,59); rtools[nrtools].title[59]=0; nrtools++; } fclose(f); }
// text-entry fields (install account step). focus moves on click or Tab/Enter; password field is masked.
static struct { char label[20]; char val[48]; int masked; } flds[4]; static int nfld=0, fcur=0; static int kbd_shift=0, osk_shift=0;
static int login_next=1;   // after sign-in: 1 = install (OS pick), 2 = rescue
// US keycode -> {unshifted, shifted} for the fields; 0 = not a text key
static const char KMAP[128][2] = {
  [2]={'1','!'},[3]={'2','@'},[4]={'3','#'},[5]={'4','$'},[6]={'5','%'},[7]={'6','^'},[8]={'7','&'},[9]={'8','*'},[10]={'9','('},[11]={'0',')'},
  [12]={'-','_'},[13]={'=','+'},
  [16]={'q','Q'},[17]={'w','W'},[18]={'e','E'},[19]={'r','R'},[20]={'t','T'},[21]={'y','Y'},[22]={'u','U'},[23]={'i','I'},[24]={'o','O'},[25]={'p','P'},
  [26]={'[','{'},[27]={']','}'},
  [30]={'a','A'},[31]={'s','S'},[32]={'d','D'},[33]={'f','F'},[34]={'g','G'},[35]={'h','H'},[36]={'j','J'},[37]={'k','K'},[38]={'l','L'},
  [39]={';',':'},[40]={'\'','"'},[41]={'`','~'},[43]={'\\','|'},
  [44]={'z','Z'},[45]={'x','X'},[46]={'c','C'},[47]={'v','V'},[48]={'b','B'},[49]={'n','N'},[50]={'m','M'},
  [51]={',','<'},[52]={'.','>'},[53]={'/','?'},[57]={' ',' '} };
static void field_putc(char c){ if(fcur<0||fcur>=nfld)return; char*v=flds[fcur].val; int n=strlen(v); if(n<47){ v[n]=c; v[n+1]=0; } }
static void field_back(void){ if(fcur<0||fcur>=nfld)return; char*v=flds[fcur].val; int n=strlen(v); if(n>0)v[n-1]=0; }
static void field_key(int code){   // a physical key while the account overlay is focused
  if(code==14){ field_back(); return; }                              // Backspace
  if(code==15||code==28){ fcur=(fcur+1)%nfld; return; }              // Tab / Enter -> next field
  if(code<128 && KMAP[code][0]){ field_putc(KMAP[code][kbd_shift?1:0]); } }

static void fput(int x,int y,const unsigned char*c){ if(x<0||y<0||x>=CW||y>=CH)return; unsigned char*p=&frame[((long)y*CW+x)*4]; p[0]=c[0];p[1]=c[1];p[2]=c[2];p[3]=255; }
static void fglyph(int x,int y,unsigned char ch,const unsigned char*c){ const unsigned char*g=&FG[(int)ch*FH*FRB]; for(int r=0;r<FH;r++)for(int b=0;b<FW;b++) if(g[r*FRB+(b>>3)]&(0x80>>(b&7))) fput(x+b,y+r,c); }
static void ftext(int x,int y,const char*s,const unsigned char*c){ for(;*s;s++){ fglyph(x,y,(unsigned char)*s,c); x+=FW; } }
static void fbox(int x,int y,int w,int h,const unsigned char*c,int t){   // t-pixel-thick rectangle outline into the chrome-space frame
  for(int i=0;i<t;i++){ for(int xx=x;xx<x+w;xx++){ fput(xx,y+i,c); fput(xx,y+h-1-i,c); } for(int yy=y;yy<y+h;yy++){ fput(x+i,yy,c); fput(x+w-1-i,yy,c); } } }

static struct Z* zone(const char*n){ for(int i=0;i<nz;i++) if(!strcmp(zones[i].n,n)) return &zones[i]; return 0; }
static const char* fact(const char*k){ for(int i=0;i<nf;i++) if(!strcmp(facts[i].k,k)) return facts[i].v; return ""; }

static void load_chrome(const char*gz){ char cmd[1024]; snprintf(cmd,sizeof cmd,"gzip -dc '%s'",gz); FILE*f=popen(cmd,"r"); if(f){ (void)!fread(chrome,1,(long)CW*CH*4,f); pclose(f);} }
// parse chrome-WxH.info: sets CW/CH, the value-font name, and the zones + tile hit-rects
static void load_info(const char*p){ nz=0; nt=0; fontname[0]=0; FILE*f=fopen(p,"r"); if(!f)return; char ln[256];
  while(fgets(ln,sizeof ln,f)){ int w,h; char n[24]; int x,y,a=0;
    if(sscanf(ln,"W=%d",&w)==1){ CW=w; continue; }
    if(sscanf(ln,"H=%d",&h)==1){ CH=h; continue; }
    if(sscanf(ln,"FONT=%63s",fontname)==1) continue;
    if(sscanf(ln,"Z %23s %d,%d,%d",n,&x,&y,&a)>=3){
      if(!strncmp(n,"tile_",5)&&nt<12){ strcpy(static_tiles[nt].n,n+5); static_tiles[nt].x=x; static_tiles[nt].y=y; int tw,th; if(sscanf(ln,"Z %*s %*d,%*d,%d,%d",&tw,&th)==2){static_tiles[nt].w=tw;static_tiles[nt].h=th;} nt++; }
      if(nz<80){ strcpy(zones[nz].n,n); zones[nz].x=x; zones[nz].y=y; zones[nz].a=a; nz++; } } }
  fclose(f); }
static void load_font(const char*p){ int fd=open(p,O_RDONLY); if(fd<0)return; int n=read(fd,fontbuf,sizeof fontbuf); close(fd); if(n<4)return; FW=fontbuf[0]; FH=fontbuf[1]; FRB=fontbuf[2]; FG=fontbuf+4; }

// default sample facts so the preview looks like the mockup; real facts come from the facts file
static void sample_facts(void){ const char*kv[][2]={{"statuschip","READY|g"},{"m_model","Dell OptiPlex 7090"},{"m_serial","7KQ2X13"},
  {"m_cpu","Intel Core i7-11700  8c/16t"},{"m_memory","32 GB"},{"m_firmware","UEFI, Secure Boot ON|g"},{"m_tpm","2.0"},
  {"disks","nvme0n1  512 GB NVMe  SK hynix  SMART OK"},{"net_addr","192.168.0.105  1 Gb/s|g"},{"net_nic","Intel I219-LM"},
  {"ai","Recommended: Windows 11 (unattended) - this model + TPM 2.0 + Secure Boot|Drivers: Dell pack matched by SKU, injected at setup|No disk problems found - safe to install"},
  {"status","Idle - no job running."},{"status2","Pick a disk and an OS below, or let IT start it from the web console. Both stay in sync."}};
  nf=0; for(unsigned i=0;i<sizeof kv/sizeof kv[0];i++){ strcpy(facts[nf].k,kv[i][0]); strncpy(facts[nf].v,kv[i][1],199); nf++; } }
static void load_facts(const char*p){ nf=0; FILE*f=p?fopen(p,"r"):0; if(!f){ sample_facts(); return; } char ln[300];
  while(fgets(ln,sizeof ln,f)&&nf<64){ char*e=strchr(ln,'='); if(!e)continue; *e=0; char*v=e+1; v[strcspn(v,"\n")]=0; strncpy(facts[nf].k,ln,23); strncpy(facts[nf].v,v,199); nf++; } fclose(f); }

static void draw_val(const char*zn,const char*key){ struct Z*z=zone(zn); if(!z)return; const char*v=fact(key); char buf[200]; strncpy(buf,v,199); buf[199]=0;
  const unsigned char*col=VAL; char*bar=strchr(buf,'|'); if(bar){ *bar=0; if(bar[1]=='g')col=GRN; } ftext(z->x,z->y,buf,col); }

static void compose(void){ memcpy(frame,chrome,(long)CW*CH*4);
  const char*m[]={"m_model","m_serial","m_cpu","m_memory","m_firmware","m_tpm","disks","net_addr","net_nic","status","status2"};
  for(unsigned i=0;i<sizeof m/sizeof m[0];i++) draw_val(m[i],m[i]);
  struct Z*az=zone("ai"); if(az){ char buf[200]; strncpy(buf,fact("ai"),199); buf[199]=0; int y=az->y; char*s=buf,*nl;
    do{ nl=strchr(s,'|'); if(nl)*nl=0; ftext(az->x,y,s,MUT); y+=FH+8; s=nl?nl+1:0; }while(s&&(y<az->y+(FH+8)*3)); }
  struct Z*cz=zone("statuschip"); if(cz){ char buf[64]; strncpy(buf,fact("statuschip"),63); buf[63]=0; char*bar=strchr(buf,'|'); const unsigned char*c=MUT; if(bar){*bar=0; if(bar[1]=='g')c=GRN;} ftext(cz->x+40,cz->y+12,buf,c);
    for(int yy=-7;yy<8;yy++)for(int xx=-7;xx<8;xx++) if(xx*xx+yy*yy<=49) fput(cz->x+16+xx,cz->y+19+yy,c); }
  if(tcur>=0 && tcur<nt){ struct T*t=&static_tiles[tcur];                 // keyboard focus ring around the selected tile
    fbox(t->x-4,t->y-4,t->w+8,t->h+8,ACCc,2); fbox(t->x-2,t->y-2,t->w+4,t->h+4,WHT,1); } }

static void putfb(int x,int y,const unsigned char*c){ if(x<0||y<0||x>=xres||y>=yres)return; unsigned char*p=fb+(long)y*stride+(long)x*(bpp/8); p[0]=c[0];p[1]=c[1];p[2]=c[2]; if(bpp==32)p[3]=255; }
// place the chrome on the real framebuffer. Native pick => DW==CW,DH==CH (1:1, pixel-crisp). Only the rare no-fit fallback
// scales, and then area-averages so text stays smooth.
static void compute_fit(void){ if(CW<=xres && CH<=yres){ DW=CW; DH=CH; }   // native: no scaling
  else { long a=(long)xres*CH, b=(long)yres*CW; if(a<=b){ DW=xres; DH=(int)((long)xres*CH/CW);} else { DH=yres; DW=(int)((long)yres*CW/CH);} if(DW>xres)DW=xres; if(DH>yres)DH=yres; }
  DX=(xres-DW)/2; DY=(yres-DH)/2; }
static int to_chrome(int sx,int sy,int*cxp,int*cyp){ int rx=sx-DX, ry=sy-DY; if(rx<0||ry<0||rx>=DW||ry>=DH)return 0; *cxp=(int)((long)rx*CW/DW); *cyp=(int)((long)ry*CH/DH); return 1; }
static void scale_fbframe(void){ for(int y=0;y<yres;y++)for(int x=0;x<xres;x++){ unsigned char*d=&fbframe[((long)y*xres+x)*4];
    int rx=x-DX, ry=y-DY; if(rx<0||ry<0||rx>=DW||ry>=DH){ d[0]=BG[0];d[1]=BG[1];d[2]=BG[2];d[3]=255; continue; }
    if(DW==CW&&DH==CH){ unsigned char*s=&frame[((long)ry*CW+rx)*4]; d[0]=s[0];d[1]=s[1];d[2]=s[2];d[3]=255; continue; }  // 1:1 fast path
    long sx0=(long)rx*CW/DW, sx1=(long)(rx+1)*CW/DW, sy0=(long)ry*CH/DH, sy1=(long)(ry+1)*CH/DH;
    if(sx1<=sx0)sx1=sx0+1; if(sy1<=sy0)sy1=sy0+1; if(sx1>CW)sx1=CW; if(sy1>CH)sy1=CH;
    unsigned long b=0,g=0,r=0,cnt=0;
    for(long sy=sy0;sy<sy1;sy++)for(long sx=sx0;sx<sx1;sx++){ unsigned char*s=&frame[((long)sy*CW+sx)*4]; b+=s[0];g+=s[1];r+=s[2];cnt++; }
    if(!cnt)cnt=1; d[0]=b/cnt; d[1]=g/cnt; d[2]=r/cnt; d[3]=255; } }
static void push_fbframe(void){ for(int y=0;y<yres;y++)for(int x=0;x<xres;x++) putfb(x,y,&fbframe[((long)y*xres+x)*4]); }
static void blit_frame(void){ scale_fbframe(); push_fbframe(); }
static const char CUR[19][12]={ "X","XX","X.X","X..X","X...X","X....X","X.....X","X......X","X.......X","X........X","X.....XXXX","X..X.X","X.X X.X","XX  X.X","X    X.X","      X.X","       X.X","       X.X","        X"};
static void restore(int sx,int sy,int w,int h){ for(int y=sy;y<sy+h&&y<yres;y++)for(int x=sx;x<sx+w&&x<xres;x++){ if(x<0||y<0)continue; putfb(x,y,&fbframe[((long)y*xres+x)*4]); } }
static void cursor(int sx,int sy){ for(int r=0;r<19;r++)for(int b=0;CUR[r][b];b++){ char ch=CUR[r][b]; if(ch=='X')putfb(sx+b,sy+r,WHT); else if(ch=='.')putfb(sx+b,sy+r,BG);} }

// ---- modal overlay: drawn into the scaled backbuffer (fbframe) so the cursor restore composites over it correctly ----
static void sput(int x,int y,const unsigned char*c){ if(x<0||y<0||x>=xres||y>=yres)return; unsigned char*p=&fbframe[((long)y*xres+x)*4]; p[0]=c[0];p[1]=c[1];p[2]=c[2];p[3]=255; }
static void sglyph(int x,int y,unsigned char ch,const unsigned char*c){ const unsigned char*g=&FG[(int)ch*FH*FRB]; for(int r=0;r<FH;r++)for(int b=0;b<FW;b++) if(g[r*FRB+(b>>3)]&(0x80>>(b&7))) sput(x+b,y+r,c); }
static void stext(int x,int y,const char*s,const unsigned char*c){ for(;*s;s++){ sglyph(x,y,(unsigned char)*s,c); x+=FW; } }
static void sfill(int x,int y,int w,int h,const unsigned char*c){ for(int yy=y;yy<y+h&&yy<yres;yy++)for(int xx=x;xx<x+w&&xx<xres;xx++) sput(xx,yy,c); }
static void sborder(int x,int y,int w,int h,const unsigned char*c){ sfill(x,y,w,1,c); sfill(x,y+h-1,w,1,c); sfill(x,y,1,h,c); sfill(x+w-1,y,1,h,c); }
static void factclean(const char*k,char*out,int n){ strncpy(out,fact(k),n-1); out[n-1]=0; char*b=strchr(out,'|'); if(b)*b=0; }

static void build_overlay(int which){ overlay=which; ovN=0; char v[128], ip[80];
  #define ADD(...) do{ if(ovN<15){ snprintf(ovB[ovN],100,__VA_ARGS__); ovN++; } }while(0)
  if(which==1){ snprintf(ovT,80,"Hardware details");
    factclean("m_model",v,128);   ADD("Model      %s",v);
    factclean("m_serial",v,128);  ADD("Serial     %s",v);
    factclean("m_cpu",v,128);     ADD("CPU        %s",v);
    factclean("m_memory",v,128);  ADD("Memory     %s",v);
    factclean("m_firmware",v,128);ADD("Firmware   %s",v);
    factclean("m_tpm",v,128);     ADD("TPM        %s",v);
    factclean("disks",v,128);     ADD("Disks      %s",v);
    factclean("net_addr",v,128);  ADD("Network    %s",v);
    factclean("net_nic",v,128);   ADD("Adapter    %s",v);
  } else if(which==2){ snprintf(ovT,80,"Let IT connect");
    factclean("net_addr",ip,80); char*sp=strchr(ip,' '); if(sp)*sp=0; char*sl=strchr(ip,'/'); if(sl)*sl=0;
    ADD("Your IT admin can connect to this machine:"); ADD("");
    ADD("     ssh root@%s",ip); ADD("");
    factclean("m_model",v,128);  ADD("Machine    %s",v);
    factclean("m_serial",v,128); ADD("Serial     %s",v); ADD("");
    ADD("Share this address or the machine name with IT.");
  } else if(which==3){ snprintf(ovT,80,"Install an OS");
    ADD("Starting an install needs IT authorization."); ADD("");
    ADD("Ask IT to unlock this here, or start the install"); ADD("from the KwickBoot web console. Both stay in sync.");
    ADD(""); ADD("Local unlock + disk/OS picker: coming soon.");
  } else if(which==4){ snprintf(ovT,80,"Rescue");
    ADD("Rescue tools - repair, recover, wipe - need IT"); ADD("authorization before they can run."); ADD("");
    ADD("Ask IT to unlock, or start Rescue from the console.");
  } else if(which==5){ snprintf(ovT,80,"Language & keyboard"); }   // interactive picker: drawn from LANGS
  else if(which==7){ load_oslist(); os_sel=0; snprintf(ovT,80,"Choose an OS to install"); }   // catalog from /run/kb-oslist
  else { snprintf(ovT,80, login_next==2 ? "Sign in for Rescue" : "Sign in to install");   // login gate: typed, with on-screen keyboard
    nfld=2; fcur=0; kbd_shift=0; osk_shift=0;
    strcpy(flds[0].label,"Username"); flds[0].val[0]=0; flds[0].masked=0;
    strcpy(flds[1].label,"Password"); flds[1].val[0]=0; flds[1].masked=1;
  }
  #undef ADD
}
static const int OSKROW[4][11] = {   // on-screen keyboard: Linux keycodes per row (-1 = empty)
  {2,3,4,5,6,7,8,9,10,11,-1},{16,17,18,19,20,21,22,23,24,25,-1},
  {30,31,32,33,34,35,36,37,38,-1,-1},{44,45,46,47,48,49,50,-1,-1,-1,-1} };
static void draw_button(int x,int y,int w,int h,const char*label,const unsigned char*fill,const unsigned char*txt,int act){
  sfill(x,y,w,h,fill); sborder(x,y,w,h,LINEc); stext(x+(w-FW*(int)strlen(label))/2,y+(h-FH)/2,label,txt); add_hit(x,y,w,h,act); }
// darken the dashboard in fbframe, draw the panel (navy card, thin accent rule, NO red chrome), then push to the screen.
static void draw_overlay(void){ novh=0; scale_fbframe();   // rebuild the dashboard first, so repeated redraws don't compound the dim
  for(long i=0;i<(long)xres*yres;i++){ unsigned char*p=&fbframe[i*4]; p[0]=p[0]*2/5; p[1]=p[1]*2/5; p[2]=p[2]*2/5; }
  int lang=(overlay==5), acct=(overlay==6), os=(overlay==7), dpk=(overlay==10), rpk=(overlay==11), th=FH+18, rh=FH+8, bh=FH+16, kh=FH+12;
  int osrows = nos? (nos+1)/2 : 1, dprows = ndisks? ndisks : 1, rprows = nrtools? nrtools : 1;
  int pw = (lang||acct||os||dpk||rpk) ? xres*86/100 : xres*78/100; if(!lang&&!acct&&!os&&!dpk&&!rpk && pw>FW*78)pw=FW*78;
  int ph = lang ? th+18 + ((NLANGS+3)/4)*(FH+22) + 16 + bh + 18
         : acct ? th+16 + nfld*(FH+32) + 12 + 5*(FH+26) + bh + 24
         : os   ? th+16 + osrows*(FH+16) + 16 + bh + 18
         : dpk  ? th+16 + dprows*(FH+16) + 16 + bh + 18
         : rpk  ? th+16 + rprows*(FH+16) + 16 + bh + 18
         : th+18 + ovN*rh + 16 + bh + 16;
  if(ph>yres*94/100)ph=yres*94/100;
  int px=(xres-pw)/2, py=(yres-ph)/2;
  sfill(px,py,pw,ph,CARDc); sborder(px,py,pw,ph,LINEc);
  stext(px+24,py+11,ovT,WHT); sfill(px+24,py+th,pw-48,2,ACCc);   // title + thin terracotta accent rule
  if(lang){
    int cols=4, cellw=(pw-48-(cols-1)*12)/cols, cellh=FH+14, gx=px+24, gy=py+th+14;
    for(int i=0;i<NLANGS;i++){ int r=i/cols,c=i%cols, cx=gx+c*(cellw+12), cy=gy+r*(cellh+8);
      sfill(cx,cy,cellw,cellh,(i==ov_sel)?CARD2c:CARDc); sborder(cx,cy,cellw,cellh,(i==ov_sel)?ACCc:LINEc);
      stext(cx+12,cy+(cellh-FH)/2,LANGS[i][0],(i==ov_sel)?WHT:VAL); add_hit(cx,cy,cellw,cellh,i); }
    int by=py+ph-bh-16;
    draw_button(px+pw-24-(FW*5+28),by,FW*5+28,bh,"Apply",ACCc,WHT,-2);
    draw_button(px+pw-24-(FW*5+28)-(FW*6+28)-12,by,FW*6+28,bh,"Cancel",CARD2c,VAL,-1);
  } else if(os){
    if(nos==0){ stext(px+26,py+th+24,"No OS catalog from the server yet - please try again shortly.",MUT); }
    else { int cols=2, cellw=(pw-48-12)/cols, cellh=FH+14, gx=px+24, gy=py+th+14;
      for(int i=0;i<nos;i++){ int r=i/cols,c=i%cols, cx=gx+c*(cellw+12), cy=gy+r*(cellh+6);
        if(cy+cellh > py+ph-bh-20) break;
        sfill(cx,cy,cellw,cellh,(i==os_sel)?CARD2c:CARDc); sborder(cx,cy,cellw,cellh,(i==os_sel)?ACCc:LINEc);
        stext(cx+12,cy+(cellh-FH)/2,oslist[i].name,(i==os_sel)?WHT:VAL); add_hit(cx,cy,cellw,cellh,i); } }
    int by=py+ph-bh-16;
    draw_button(px+pw-24-(FW*7+28),by,FW*7+28,bh,"Install",ACCc,WHT,-2);
    draw_button(px+pw-24-(FW*7+28)-(FW*6+28)-12,by,FW*6+28,bh,"Cancel",CARD2c,VAL,-1);
  } else if(dpk){
    if(ndisks==0){ stext(px+26,py+th+24,"No fixed disk found to install on - check the hardware.",MUT); }
    else { int cellw=pw-48, cellh=FH+14, gx=px+24, gy=py+th+14;                             // one disk per full-width row
      for(int i=0;i<ndisks;i++){ int cy=gy+i*(cellh+6);
        if(cy+cellh > py+ph-bh-20) break;
        sfill(gx,cy,cellw,cellh,(i==disk_sel)?CARD2c:CARDc); sborder(gx,cy,cellw,cellh,(i==disk_sel)?ACCc:LINEc);
        stext(gx+12,cy+(cellh-FH)/2,disks[i].desc,(i==disk_sel)?WHT:VAL); add_hit(gx,cy,cellw,cellh,i); } }
    int by=py+ph-bh-16;
    draw_button(px+pw-24-(FW*4+28),by,FW*4+28,bh,"Next",ACCc,WHT,-2);
    draw_button(px+pw-24-(FW*4+28)-(FW*6+28)-12,by,FW*6+28,bh,"Cancel",CARD2c,VAL,-1);
  } else if(rpk){
    if(nrtools==0){ stext(px+26,py+th+24,"No rescue tool published on the server yet.",MUT); }
    else { int cellw=pw-48, cellh=FH+14, gx=px+24, gy=py+th+14;                             // one tool per full-width row
      for(int i=0;i<nrtools;i++){ int cy=gy+i*(cellh+6);
        if(cy+cellh > py+ph-bh-20) break;
        sfill(gx,cy,cellw,cellh,(i==rtool_sel)?CARD2c:CARDc); sborder(gx,cy,cellw,cellh,(i==rtool_sel)?ACCc:LINEc);
        stext(gx+12,cy+(cellh-FH)/2,rtools[i].title,(i==rtool_sel)?WHT:VAL); add_hit(gx,cy,cellw,cellh,i); } }
    int by=py+ph-bh-16;
    draw_button(px+pw-24-(FW*4+28),by,FW*4+28,bh,"Next",ACCc,WHT,-2);
    draw_button(px+pw-24-(FW*4+28)-(FW*6+28)-12,by,FW*6+28,bh,"Cancel",CARD2c,VAL,-1);
  } else if(acct){
    int y=py+th+14, bxw=pw-60-FW*14, bx=px+30+FW*14, fh=FH+14;
    for(int i=0;i<nfld;i++){ stext(px+30,y+(fh-FH)/2,flds[i].label,VAL);
      sfill(bx,y,bxw,fh,CARD2c); sborder(bx,y,bxw,fh,(i==fcur)?ACCc:LINEc);
      char disp[64]; int n=strlen(flds[i].val); if(flds[i].masked){ int k; for(k=0;k<n&&k<63;k++)disp[k]='*'; disp[k]=0; } else { strncpy(disp,flds[i].val,63); disp[63]=0; }
      stext(bx+10,y+(fh-FH)/2,disp,WHT);
      if(i==fcur){ sfill(bx+10+FW*(int)strlen(disp),y+6,2,fh-12,ACCc); }   // caret
      add_hit(bx,y,bxw,fh,200+i); y+=fh+18; }
    y+=10; int gap=8, kx0=px+30, kbw=pw-60, kw=(kbw-9*gap)/10, okh=FH+18;    // 10 keys fill the panel width
    int rowoff[4]={0,0,kw/2,(kw*3)/2};                                        // classic staggered keyboard rows
    for(int r=0;r<4;r++){ int kx=kx0+rowoff[r];
      for(int c=0;c<11&&OSKROW[r][c]>=0;c++){ int code=OSKROW[r][c];
        sfill(kx,y,kw,okh,CARDc); sborder(kx,y,kw,okh,LINEc);
        char lbl[2]={KMAP[code][osk_shift?1:0],0}; stext(kx+(kw-FW)/2,y+(okh-FH)/2,lbl,VAL);
        add_hit(kx,y,kw,okh,2000+code); kx+=kw+gap; }
      y+=okh+gap; }
    int sx=kx0;
    draw_button(sx,y,kw*2+gap,okh,"Shift",osk_shift?ACCc:CARDc,osk_shift?WHT:VAL,-10); sx+=kw*2+gap*2;
    draw_button(sx,y,kw*4+gap*3,okh,"Space",CARDc,VAL,2057); sx+=kw*4+gap*4;   // 2000+KEY_SPACE
    draw_button(sx,y,kw*2+gap,okh,"Del",CARDc,VAL,-11);
    int by=py+ph-bh-14;
    draw_button(px+pw-24-(FW*7+28),by,FW*7+28,bh,"Sign in",ACCc,WHT,-3);
    draw_button(px+pw-24-(FW*7+28)-(FW*6+28)-12,by,FW*6+28,bh,"Cancel",CARD2c,VAL,-1);
  } else if(overlay==9){   // destructive confirm: erase disk & install (red = danger, the one place red is warranted)
    int y=py+th+18; for(int i=0;i<ovN;i++){ stext(px+26,y,ovB[i],(i==0)?WHT:VAL); y+=rh; }
    int by=py+ph-bh-16, ew=FW*15+28, cw=FW*6+28, ex=px+pw-24-ew, cxb=ex-cw-12;
    draw_button(ex,by,ew,bh,"Erase & install",BRANDc,WHT,-2);
    draw_button(cxb,by,cw,bh,"Cancel",CARD2c,VAL,-1);
    int fx=(ovbtn==1)?ex:cxb, fw=(ovbtn==1)?ew:cw;                 // keyboard focus ring on the active button (default Cancel)
    sborder(fx-2,by-2,fw+4,bh+4,WHT); sborder(fx-3,by-3,fw+6,bh+6,ACCc);
  } else if(overlay==12){   // rescue launch confirm: reboot into the tool (accent, not red - launching is not itself a wipe)
    int y=py+th+18; for(int i=0;i<ovN;i++){ stext(px+26,y,ovB[i],(i==0)?WHT:VAL); y+=rh; }
    int by=py+ph-bh-16, ew=FW*18+28, cw=FW*6+28, ex=px+pw-24-ew, cxb=ex-cw-12;
    draw_button(ex,by,ew,bh,"Reboot into Rescue",ACCc,WHT,-2);
    draw_button(cxb,by,cw,bh,"Cancel",CARD2c,VAL,-1);
    int fx=(ovbtn==1)?ex:cxb, fw=(ovbtn==1)?ew:cw;                 // keyboard focus ring (default Cancel)
    sborder(fx-2,by-2,fw+4,bh+4,WHT); sborder(fx-3,by-3,fw+6,bh+6,ACCc);
  } else {
    int y=py+th+18; for(int i=0;i<ovN;i++){ stext(px+26,y,ovB[i],VAL); y+=rh; }
    draw_button(px+pw-24-(FW*5+28),py+ph-bh-16,FW*5+28,bh,"Close",CARD2c,VAL,-1);
  }
  push_fbframe();
}
static void close_overlay(void){ overlay=0; scale_fbframe(); push_fbframe(); }

// build the disk-erase confirmation (overlay 9) for the chosen OS + disk; focus defaults to Cancel
static void build_confirm(void){
  char dk[96]; if(ndisks>=1) snprintf(dk,96,"%s",disks[disk_sel].desc); else snprintf(dk,96,"the primary disk");
  overlay=9; ovbtn=0; snprintf(ovT,80,"Erase this disk and install?");
  snprintf(ovB[0],100,"This ERASES everything on:  %s",dk);
  snprintf(ovB[1],100,"and installs %s in its place.",oslist[os_sel].name);
  snprintf(ovB[2],100,"All data and any OS on that disk are lost - this cannot be undone.");
  snprintf(ovB[3],100,"Choose Cancel to go back, or Erase & install to proceed."); ovN=4;
}
// build the rescue-launch confirmation (overlay 12) for the chosen tool; focus defaults to Cancel
static void build_rescue_confirm(void){
  const char*tt = (nrtools>=1)?rtools[rtool_sel].title:"the rescue tool";
  overlay=12; ovbtn=0; snprintf(ovT,80,"Start %s?",tt);
  snprintf(ovB[0],100,"This machine will REBOOT into %s now.",tt);
  snprintf(ovB[1],100,"Anything unsaved in this RAM session is lost; disks are untouched");
  snprintf(ovB[2],100,"until you choose an action inside the tool.");
  snprintf(ovB[3],100,"Choose Cancel to stay, or Reboot into Rescue to proceed."); ovN=4;
}

// carry out one overlay action (from a mouse click or a keyboard shortcut); returns 1 to keep the overlay open, 0 to close it
static int handle_overlay_act(int act){
  int keepopen=0;
  if(overlay==5){
    if(act>=0){ ov_sel=act; keepopen=1; }                                               // pick a language
    else if(act==-2){ FILE*a=fopen("/run/kb-gui-action","w"); if(a){ fprintf(a,"language:%s:%s\n",LANGS[ov_sel][1],LANGS[ov_sel][2]); fclose(a);}
      char nl[8]; nl[0]=LANGS[ov_sel][1][0]; nl[1]=LANGS[ov_sel][1][1]; nl[2]=0;   // locale -> 2-letter UI code
      int uiok=switch_lang(nl);                                                     // switch kbgui's own UI too
      overlay=8; snprintf(ovT,80,"Language set"); snprintf(ovB[0],100,"%s  -  keymap %s",LANGS[ov_sel][0],LANGS[ov_sel][2]); ovB[1][0]=0;
      snprintf(ovB[2],100, uiok?"Applied to this screen, your typing and the installed OS."
                               :"Applied to your typing and the installed OS."); ovN=3; keepopen=1; }
  } else if(overlay==7){                                                                 // OS picker
    if(act>=0 && act<nos){ os_sel=act; keepopen=1; }                                      // pick an OS
    else if(act==-2 && nos>0){ load_disks(); disk_sel=0;                             // Install -> disk picker (2+ disks) or straight to confirm
      if(ndisks>=2){ overlay=10; snprintf(ovT,80,"Choose the disk to install on"); } else build_confirm();
      keepopen=1; }
  } else if(overlay==10){                                                            // disk picker
    if(act>=0 && act<ndisks){ disk_sel=act; keepopen=1; }                            // pick a disk
    else if(act==-2 && ndisks>0){ build_confirm(); keepopen=1; }                     // Next -> confirm
    else if(act==-1){ overlay=7; snprintf(ovT,80,"Choose an OS to install"); keepopen=1; }   // back to OS picker
  } else if(overlay==9){                                                             // destructive confirm
    if(act==-2 && nos>0){ FILE*a=fopen("/run/kb-gui-action","w"); if(a){
        if(ndisks>=1) fprintf(a,"install:os=%s:disk=%s\n",oslist[os_sel].id,disks[disk_sel].dev);
        else fprintf(a,"install:os=%s\n",oslist[os_sel].id); fclose(a);}
      overlay=8; snprintf(ovT,80,"Install starting"); snprintf(ovB[0],100,"OS: %s",oslist[os_sel].name);
      if(ndisks>=1) snprintf(ovB[1],100,"Disk: %s",disks[disk_sel].dev); else ovB[1][0]=0;
      snprintf(ovB[2],100,"It is erasing the disk and installing; IT can watch in the web console."); ovN=3; keepopen=1; }
    else if(act==-1){ if(ndisks>=2){ overlay=10; snprintf(ovT,80,"Choose the disk to install on"); }   // Cancel -> back to disk picker or OS picker
      else { overlay=7; snprintf(ovT,80,"Choose an OS to install"); } keepopen=1; }
  } else if(overlay==11){                                                           // rescue tool picker
    if(act>=0 && act<nrtools){ rtool_sel=act; keepopen=1; }                          // pick a tool
    else if(act==-2 && nrtools>0){ build_rescue_confirm(); keepopen=1; }             // Next -> rescue confirm
  } else if(overlay==12){                                                           // rescue launch confirm
    if(act==-2 && nrtools>0){ FILE*a=fopen("/run/kb-gui-action","w"); if(a){ fprintf(a,"rescue:tool=%s\n",rtools[rtool_sel].name); fclose(a);}
      overlay=8; snprintf(ovT,80,"Rescue starting"); snprintf(ovB[0],100,"Tool: %s",rtools[rtool_sel].title);
      snprintf(ovB[1],100,"If IT authorises it, the machine reboots into the tool now."); ovB[2][0]=0; ovN=2; keepopen=1; }
    else if(act==-1){ if(nrtools>=2){ overlay=11; snprintf(ovT,80,"Choose a rescue tool"); keepopen=1; } }   // Cancel -> back to picker (else close)
  } else if(overlay==6){                                                                 // login gate
    if(act>=200 && act<200+nfld){ fcur=act-200; keepopen=1; }                            // focus a field
    else if(act>=2000 && act<2128){ field_putc(KMAP[act-2000][osk_shift?1:0]); keepopen=1; }  // on-screen key
    else if(act==-10){ osk_shift^=1; keepopen=1; }                                        // Shift
    else if(act==-11){ field_back(); keepopen=1; }                                        // Del
    else if(act==-3){ FILE*a=fopen("/run/kb-gui-action","w"); if(a){ fprintf(a,"login:user=%s:pass=%s:then=%s\n",flds[0].val,flds[1].val,login_next==2?"rescue":"install"); fclose(a);}
      if(login_next==1){ build_overlay(7); }                                              // install -> straight into the OS picker
      else { load_tools(); rtool_sel=0;                                                   // rescue -> tool picker (2+) or launch confirm
        if(nrtools==0){ overlay=8; snprintf(ovT,80,"Rescue unavailable"); snprintf(ovB[0],100,"No rescue tool is published on the server yet."); ovB[1][0]=0; snprintf(ovB[2],100,"Ask IT to add one, then try again."); ovN=3; }
        else if(nrtools>=2){ overlay=11; snprintf(ovT,80,"Choose a rescue tool"); }
        else build_rescue_confirm(); }
      keepopen=1; }
  }
  return keepopen;
}

// a physical key while a non-login overlay is open; returns 1 if it changed something that needs a redraw.
// Safety: in the confirm dialog Enter triggers only the *focused* button, which defaults to Cancel - never Erase by accident.
static int overlay_nav_key(int code){
  if(code==KEY_ESC){ int k=handle_overlay_act(-1); if(k)draw_overlay(); else close_overlay(); return 1; }  // Esc = Cancel/Close everywhere
  if(overlay==9||overlay==12){                                                           // confirm dialogs: arrows/Tab move focus, Enter fires it
    if(code==KEY_LEFT||code==KEY_RIGHT||code==KEY_TAB){ ovbtn^=1; draw_overlay(); return 1; }
    if(code==KEY_ENTER){ int k=handle_overlay_act(ovbtn==1?-2:-1); if(k)draw_overlay(); else close_overlay(); return 1; }
    return 0; }
  if(overlay==5){                                                                        // language: arrows move the 4-col grid, Enter = Apply
    int c=4, n=NLANGS;
    if(code==KEY_LEFT){ if(ov_sel>0)ov_sel--; draw_overlay(); return 1; }
    if(code==KEY_RIGHT){ if(ov_sel<n-1)ov_sel++; draw_overlay(); return 1; }
    if(code==KEY_UP){ if(ov_sel>=c)ov_sel-=c; draw_overlay(); return 1; }
    if(code==KEY_DOWN){ if(ov_sel+c<n)ov_sel+=c; draw_overlay(); return 1; }
    if(code==KEY_TAB){ ov_sel=(ov_sel+1)%n; draw_overlay(); return 1; }
    if(code==KEY_ENTER){ int k=handle_overlay_act(-2); if(k)draw_overlay(); else close_overlay(); return 1; }
    return 0; }
  if(overlay==7){                                                                        // OS picker: arrows move the 2-col grid, Enter = Install
    int c=2, n=nos; if(n<1)return 0;
    if(code==KEY_LEFT){ if(os_sel>0)os_sel--; draw_overlay(); return 1; }
    if(code==KEY_RIGHT){ if(os_sel<n-1)os_sel++; draw_overlay(); return 1; }
    if(code==KEY_UP){ if(os_sel>=c)os_sel-=c; draw_overlay(); return 1; }
    if(code==KEY_DOWN){ if(os_sel+c<n)os_sel+=c; draw_overlay(); return 1; }
    if(code==KEY_TAB){ os_sel=(os_sel+1)%n; draw_overlay(); return 1; }
    if(code==KEY_ENTER){ int k=handle_overlay_act(-2); if(k)draw_overlay(); else close_overlay(); return 1; }
    return 0; }
  if(overlay==10||overlay==11){                                                          // single-column list pickers (disk / rescue tool)
    int *sel=(overlay==10)?&disk_sel:&rtool_sel, n=(overlay==10)?ndisks:nrtools; if(n<1)return 0;
    if(code==KEY_UP||code==KEY_LEFT){ if(*sel>0)(*sel)--; draw_overlay(); return 1; }
    if(code==KEY_DOWN||code==KEY_RIGHT){ if(*sel<n-1)(*sel)++; draw_overlay(); return 1; }
    if(code==KEY_TAB){ *sel=(*sel+1)%n; draw_overlay(); return 1; }
    if(code==KEY_ENTER){ int k=handle_overlay_act(-2); if(k)draw_overlay(); else close_overlay(); return 1; }
    return 0; }
  if(code==KEY_ENTER){ close_overlay(); return 1; }                                      // info panels (details/connect/notice): Enter = Close
  return 0;
}

// open the overlay for dashboard tile i (same effect as a mouse click on it); returns the overlay type opened, or 0
static int open_tile(int i){
  if(i<0||i>=nt)return 0;
  const char*nm=static_tiles[i].n;
  if(!strcmp(nm,"install")) login_next=1; else if(!strcmp(nm,"rescue")) login_next=2;    // both go through Sign in first
  int which = !strcmp(nm,"details")?1 : !strcmp(nm,"connect")?2 : !strcmp(nm,"install")?6 : !strcmp(nm,"rescue")?6 : !strcmp(nm,"language")?5 : 0;
  if(which){ build_overlay(which); draw_overlay(); }
  return which;
}
// a physical key on the dashboard (no overlay open). Returns 0=ignored, 1=focus moved (redraw dashboard), 2=tile opened (overlay drawn)
static int dash_nav_key(int code){
  if(nt<1)return 0;
  if(code==KEY_TAB||code==KEY_RIGHT||code==KEY_DOWN){ tcur=(tcur<0)?0:(tcur+1)%nt; return 1; }
  if(code==KEY_LEFT||code==KEY_UP){ tcur=(tcur<=0)?nt-1:tcur-1; return 1; }
  if(code==KEY_ESC){ if(tcur<0)return 0; tcur=-1; return 1; }                             // Esc clears the focus ring
  if(code==KEY_ENTER && tcur>=0){ if(open_tile(tcur))return 2; }
  return 0;
}

static void restore_text(int sig){ (void)sig; if(ttyfd>=0) ioctl(ttyfd,KDSETMODE,KD_TEXT); _exit(0); }

// choose the chrome rendered for this screen: the largest whose W<=xres and H<=yres (native, crisp); if none fits (screen
// smaller than the smallest render) take the smallest and let it scale down. Returns the WxH tag, or empty on none.
static void pick_chrome(const char*dir,const char*lang,char*tagout){ tagout[0]=0; char pfx[24]; int pl=snprintf(pfx,sizeof pfx,"chrome-%s-",lang);
  DIR*dp=opendir(dir); struct dirent*de; int bestFit=-1,bestSmall=1<<30; char fitTag[32]={0},smallTag[32]={0};
  while(dp&&(de=readdir(dp))){ if(strncmp(de->d_name,pfx,pl))continue; int w,h; if(sscanf(de->d_name+pl,"%dx%d.info",&w,&h)!=2)continue;
    if(w<=xres && h<=yres){ if(w>bestFit){ bestFit=w; snprintf(fitTag,sizeof fitTag,"%dx%d",w,h);} }
    if(w<bestSmall){ bestSmall=w; snprintf(smallTag,sizeof smallTag,"%dx%d",w,h);} }
  if(dp)closedir(dp);
  if(fitTag[0]) strcpy(tagout,fitTag); else if(smallTag[0]) strcpy(tagout,smallTag); }

// load the chrome set for `lang` at this screen's resolution. On success swaps in the new chrome/frame; on failure (no such
// language, e.g. Hebrew) leaves the current set untouched and returns 0.
static int load_set(const char*dir,const char*lang){ char tag[32]; pick_chrome(dir,lang,tag); if(!tag[0])return 0;
  char info[512],gz[512],fnt[512];
  int sCW=CW,sCH=CH; char sfont[64]; strcpy(sfont,fontname);
  snprintf(info,sizeof info,"%s/chrome-%s-%s.info",dir,lang,tag); load_info(info);
  if(!CW||!CH||!fontname[0]){ CW=sCW;CH=sCH;strcpy(fontname,sfont); return 0; }
  unsigned char*nc=malloc((long)CW*CH*4), *nf=malloc((long)CW*CH*4);
  if(!nc||!nf){ free(nc); free(nf); CW=sCW;CH=sCH;strcpy(fontname,sfont); return 0; }
  if(chrome)free(chrome); if(frame)free(frame); chrome=nc; frame=nf;
  snprintf(gz,sizeof gz,"%s/chrome-%s-%s.bgra.gz",dir,lang,tag); load_chrome(gz);
  snprintf(fnt,sizeof fnt,"%s/%s",dir,fontname); load_font(fnt); if(!FW)return 0;
  return 1; }

// switch kbgui's own UI language: reload chrome for `lang` and repaint the dashboard. Keeps the old UI on failure.
static int switch_lang(const char*lang){ if(!load_set(SHARE_DIR,lang)) return 0;
  strncpy(uilang,lang,7); uilang[7]=0; compute_fit(); load_facts(FACTS_FILE); compose(); blit_frame(); return 1; }

int main(int argc,char**argv){
  if(argc<2){ fprintf(stderr,"usage: kbgui <share-dir> [facts]\n"); return 2; }
  const char*dir=argv[1]; const char*factsf=argc>2?argv[2]:0;
  SHARE_DIR=dir; FACTS_FILE=factsf;
  char*el=getenv("KBFB_LANG"); if(el&&*el){ strncpy(uilang,el,7); uilang[7]=0; }   // test/preview: pick the initial UI language
  char*dump=getenv("KBFB_DUMP");
  if(dump){ xres=1920;yres=1080; char*rs=getenv("KBFB_RES"); if(rs)sscanf(rs,"%dx%d",&xres,&yres); bpp=32;stride=xres*4; fblen=(long)stride*yres; fb=calloc(1,fblen);
    if(!load_set(dir,uilang) && !load_set(dir,"en")){ fprintf(stderr,"no chrome set in %s\n",dir); return 2; }
    compute_fit(); fbframe=calloc(1,(long)xres*yres*4); load_facts(factsf); compose(); blit_frame(); cursor(xres/2-60,yres/2+140);
    FILE*o=fopen(dump,"wb"); if(o){ fprintf(o,"P6\n%d %d\n255\n",xres,yres); for(long i=0;i<(long)xres*yres;i++){unsigned char*p=fb+(i/xres)*stride+(i%xres)*(bpp/8); unsigned char rgb[3]={p[2],p[1],p[0]}; fwrite(rgb,1,3,o);} fclose(o);} return 0; }
  int fd=open("/dev/fb0",O_RDWR); if(fd<0)return 2; struct fb_var_screeninfo v; struct fb_fix_screeninfo f;
  if(ioctl(fd,FBIOGET_VSCREENINFO,&v)||ioctl(fd,FBIOGET_FSCREENINFO,&f)){close(fd);return 2;}
  xres=v.xres;yres=v.yres;bpp=v.bits_per_pixel;stride=f.line_length; if(bpp!=32&&bpp!=24){close(fd);return 2;}
  fblen=(long)stride*yres; fb=mmap(0,fblen,PROT_READ|PROT_WRITE,MAP_SHARED,fd,0); if(fb==MAP_FAILED){close(fd);return 2;}
  if(!load_set(dir,uilang) && !load_set(dir,"en")){close(fd);return 2;}
  compute_fit(); fbframe=malloc((long)xres*yres*4); if(!fbframe){close(fd);return 2;}
  load_facts(factsf);
  ttyfd=open("/dev/tty1",O_RDWR); if(ttyfd>=0) ioctl(ttyfd,KDSETMODE,KD_GRAPHICS);
  signal(SIGTERM,restore_text); signal(SIGINT,restore_text);
  compose(); blit_frame();
  // open pointer devices: relative mice (USB/PS2) AND absolute pointers (VM tablets - Hyper-V/QEMU - and touchscreens)
  struct pollfd pf[16]; int npf=0; int pabs[16], pkbd[16]; int axmin[16],axmax[16],aymin[16],aymax[16];
  DIR*dp=opendir("/dev/input"); struct dirent*de;
  while(dp&&(de=readdir(dp))&&npf<16){ if(strncmp(de->d_name,"event",5))continue; char path[300]; snprintf(path,sizeof path,"/dev/input/%s",de->d_name);
    int e=open(path,O_RDONLY|O_NONBLOCK); if(e<0)continue;
    unsigned long rel=0, abs=0, kb[24]={0}; ioctl(e,EVIOCGBIT(EV_REL,sizeof rel),&rel); ioctl(e,EVIOCGBIT(EV_ABS,sizeof abs),&abs); ioctl(e,EVIOCGBIT(EV_KEY,sizeof kb),kb);
    int iskbd = (kb[KEY_A/(8*sizeof(long))]>>(KEY_A%(8*sizeof(long))))&1;   // has a letter key -> a keyboard
    if(rel&(1<<REL_X)){ pf[npf].fd=e; pf[npf].events=POLLIN; pabs[npf]=0; pkbd[npf]=0; npf++; }
    else if(abs&(1<<ABS_X)){ struct input_absinfo ai; axmin[npf]=0;axmax[npf]=xres-1;aymin[npf]=0;aymax[npf]=yres-1;
      if(ioctl(e,EVIOCGABS(ABS_X),&ai)>=0 && ai.maximum>ai.minimum){ axmin[npf]=ai.minimum; axmax[npf]=ai.maximum; }
      if(ioctl(e,EVIOCGABS(ABS_Y),&ai)>=0 && ai.maximum>ai.minimum){ aymin[npf]=ai.minimum; aymax[npf]=ai.maximum; }
      pf[npf].fd=e; pf[npf].events=POLLIN; pabs[npf]=1; pkbd[npf]=0; npf++; }
    else if(iskbd){ pf[npf].fd=e; pf[npf].events=POLLIN; pabs[npf]=0; pkbd[npf]=1; npf++; }
    else close(e); }
  if(dp)closedir(dp);
  int cx=xres/2, cy=yres/2, ox2=cx, oy2=cy; cursor(cx,cy);
  time_t lastf=time(0);
  for(;;){
    int pr=poll(pf,npf,80); int moved=0, click=0, keyed=0, navkeyed=0, dashmoved=0;
    if(pr>0) for(int i=0;i<npf;i++) if(pf[i].revents&POLLIN){ struct input_event ev; int rd; while((rd=read(pf[i].fd,&ev,sizeof ev))==sizeof ev){
      if(ev.type==EV_REL){ if(ev.code==REL_X)cx+=ev.value; else if(ev.code==REL_Y)cy+=ev.value; moved=1; }
      else if(ev.type==EV_ABS&&pabs[i]){ if(ev.code==ABS_X&&axmax[i]>axmin[i]){ cx=(int)((long)(ev.value-axmin[i])*(xres-1)/(axmax[i]-axmin[i])); moved=1; }
        else if(ev.code==ABS_Y&&aymax[i]>aymin[i]){ cy=(int)((long)(ev.value-aymin[i])*(yres-1)/(aymax[i]-aymin[i])); moved=1; } }
      else if(ev.type==EV_KEY){
        if((ev.code==BTN_LEFT||ev.code==BTN_TOUCH)&&ev.value==1) click=1;
        else if(pkbd[i]){ if(ev.code==KEY_LEFTSHIFT||ev.code==KEY_RIGHTSHIFT) kbd_shift=(ev.value!=0);
          else if(ev.value==1||ev.value==2){
            if(overlay==6){                                                                 // login gate keyboard
              if(ev.code==KEY_ESC){ navkeyed|=overlay_nav_key(KEY_ESC); }                   // Esc -> Cancel
              else if(ev.code==KEY_ENTER && nfld>0 && fcur==nfld-1){ int k=handle_overlay_act(-3); if(k)draw_overlay(); else close_overlay(); navkeyed=1; }  // Enter on last field -> Sign in
              else { field_key(ev.code); keyed=1; } }                                       // text / Backspace / Tab / Enter-between-fields
            else if(overlay){ navkeyed|=overlay_nav_key(ev.code); }                        // other overlays: arrows/Tab/Enter/Esc
            else { int r=dash_nav_key(ev.code); if(r==1)dashmoved=1; else if(r==2)navkeyed=1; } } } } } }   // dashboard: Tab/arrows focus a tile, Enter opens it
    if(cx<0)cx=0; if(cy<0)cy=0; if(cx>=xres)cx=xres-1; if(cy>=yres)cy=yres-1;
    time_t now=time(0);
    if(now-lastf>=2 && !overlay){ load_facts(factsf); compose(); blit_frame(); lastf=now; ox2=-1; }   // refresh live values (paused while a modal is open)
    if(moved||ox2<0){ if(ox2>=0) restore(ox2,oy2,12,20); cursor(cx,cy); ox2=cx; oy2=cy; }
    if(click){
      if(overlay){
        int act=-9; for(int i=0;i<novh;i++) if(cx>=ovhit[i].x&&cy>=ovhit[i].y&&cx<ovhit[i].x+ovhit[i].w&&cy<ovhit[i].y+ovhit[i].h){ act=ovhit[i].act; break; }
        int keepopen=handle_overlay_act(act);
        if(keepopen) draw_overlay(); else close_overlay();                                      // Cancel / Install / click outside -> close
        cursor(cx,cy); ox2=cx; oy2=cy;
      }
      else { int mcx,mcy; if(to_chrome(cx,cy,&mcx,&mcy)) for(int i=0;i<nt;i++) if(mcx>=static_tiles[i].x&&mcy>=static_tiles[i].y&&mcx<static_tiles[i].x+static_tiles[i].w&&mcy<static_tiles[i].y+static_tiles[i].h){
        const char*nm=static_tiles[i].n;
        if(!strcmp(nm,"install")) login_next=1; else if(!strcmp(nm,"rescue")) login_next=2;   // both go through Sign in first
        int which = !strcmp(nm,"details")?1 : !strcmp(nm,"connect")?2 : !strcmp(nm,"install")?6 : !strcmp(nm,"rescue")?6 : !strcmp(nm,"language")?5 : 0;
        if(which){ build_overlay(which); draw_overlay(); cursor(cx,cy); ox2=cx; oy2=cy; }
        break; } }
    }
    if(keyed && overlay==6){ draw_overlay(); cursor(cx,cy); ox2=cx; oy2=cy; }   // live-update the focused field as you type
    if(navkeyed){ cursor(cx,cy); ox2=cx; oy2=cy; }                              // nav key already redrew the overlay; put the cursor back on top
    if(dashmoved && !overlay){ compose(); blit_frame(); cursor(cx,cy); ox2=cx; oy2=cy; lastf=now; }   // dashboard tile focus moved -> redraw the ring
  }
}
