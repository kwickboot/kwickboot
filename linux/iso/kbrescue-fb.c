// kbrescue-fb - draw the KwickBoot Rescue menu on the framebuffer (branded chrome + dynamic text), read the choice.
// SPDX-License-Identifier: AGPL-3.0-or-later
//   kbrescue-fb <chrome.bgra.gz> <chrome.info> <font.raw(8x16)> <disks.txt>
// Prints the typed choice to stdout and exits 0. Exits 2 if there is no usable 32bpp framebuffer (caller then uses text).
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/ioctl.h>
#include <linux/fb.h>
#include <termios.h>

#define CW 1024
#define CH 768
static unsigned char chrome[CW*CH*4];          // BGRA
static unsigned char font[4096];               // 256 glyphs, 8x16, 1bpp
static int DISK_X=48, DISK_Y=116, LINE_H=26, CHOOSE_X=48, CHOOSE_Y=644;
static char inum[64][8]; static int ix[64],iy[64],iw[64],ih[64],ni=0;   // selectable menu items (arrow navigation)
static const unsigned char BG[4]={54,34,27,255};   // navy #1b2236 as B,G,R,A
static const unsigned char ACC[4]={87,119,217,255};// #d97757
static const unsigned char TXT[4]={228,214,207,255};//#cfd6e4
static const unsigned char WHT[4]={255,255,255,255};

static unsigned char *fb; static long fblen;
static int xres, yres, bpp, stride, ox, oy;

static void putpx(int x,int y,const unsigned char*c){
  if(x<0||y<0||x>=xres||y>=yres) return;
  unsigned char*p=fb+(long)y*stride+(long)x*(bpp/8);
  p[0]=c[0];p[1]=c[1];p[2]=c[2]; if(bpp==32)p[3]=c[3];
}
// copy one chrome pixel (cx,cy) to the screen (restores background under a zone)
static void restore(int cx,int cy){
  if(cx<0||cy<0||cx>=CW||cy>=CH) return;
  putpx(ox+cx,oy+cy,&chrome[((long)cy*CW+cx)*4]);
}
static void blit_chrome(void){
  for(int y=0;y<yres;y++)for(int x=0;x<xres;x++){
    int cx=x-ox, cy=y-oy;
    if(cx>=0&&cy>=0&&cx<CW&&cy<CH) putpx(x,y,&chrome[((long)cy*CW+cx)*4]);
    else putpx(x,y,BG);
  }
}
// draw a glyph at chrome-space (gx,gy); fg over the existing chrome background
static void glyph(int gx,int gy,unsigned char ch,const unsigned char*fg){
  const unsigned char*g=&font[ch*16];
  for(int r=0;r<16;r++)for(int b=0;b<8;b++)
    if(g[r]&(0x80>>b)) putpx(ox+gx+b,oy+gy+r,fg);
}
static void text(int gx,int gy,const char*s,const unsigned char*fg){
  for(;*s;s++){ glyph(gx,gy,(unsigned char)*s,fg); gx+=8; }
}
// clear a chrome-space rectangle back to the original chrome (so typed text can be rewritten)
static void clearzone(int gx,int gy,int w,int h){
  for(int y=0;y<h;y++)for(int x=0;x<w;x++) restore(gx+x,gy+y);
}
// selection highlight over item i: a warm accent tint (text shows through) + a solid accent bar on the left
static void hl(int i){
  for(int y=0;y<ih[i];y++)for(int x=0;x<iw[i];x++){
    int cx=ix[i]+x, cy=iy[i]+y; if(cx<0||cy<0||cx>=CW||cy>=CH) continue;
    unsigned char*c=&chrome[((long)cy*CW+cx)*4];
    unsigned char px[4]={(c[0]*10+ACC[0]*6)/16,(c[1]*10+ACC[1]*6)/16,(c[2]*10+ACC[2]*6)/16,255};
    putpx(ox+cx,oy+cy, x<4?ACC:px);
  }
}
static void unhl(int i){ for(int y=0;y<ih[i];y++)for(int x=0;x<iw[i];x++) restore(ix[i]+x,iy[i]+y); }

int main(int argc,char**argv){
  if(argc<5){fprintf(stderr,"usage: kbrescue-fb chrome.gz info font disks\n");return 2;}
  // chrome (gunzip via gzip -dc)
  char cmd[1024]; snprintf(cmd,sizeof cmd,"gzip -dc '%s'",argv[1]);
  FILE*cf=popen(cmd,"r"); if(!cf)return 2;
  if(fread(chrome,1,sizeof chrome,cf)!=sizeof chrome){pclose(cf);return 2;} pclose(cf);
  // info
  FILE*inf=fopen(argv[2],"r"); if(inf){char ln[256];int v;
    while(fgets(ln,sizeof ln,inf)){
      if(sscanf(ln,"DISK_X=%d",&v)==1)DISK_X=v; else if(sscanf(ln,"DISK_Y=%d",&v)==1)DISK_Y=v;
      else if(sscanf(ln,"LINE_H=%d",&v)==1)LINE_H=v; else if(sscanf(ln,"CHOOSE_X=%d",&v)==1)CHOOSE_X=v;
      else if(sscanf(ln,"CHOOSE_Y=%d",&v)==1)CHOOSE_Y=v;
      else if(ni<64 && sscanf(ln,"ITEM=%7[^,],%d,%d,%d,%d",inum[ni],&ix[ni],&iy[ni],&iw[ni],&ih[ni])==5) ni++; }
    fclose(inf);}
  // font (raw 4096)
  int ff=open(argv[3],O_RDONLY); if(ff<0||read(ff,font,sizeof font)!=sizeof font){if(ff>=0)close(ff);return 2;} close(ff);
  // test mode: KBFB_DUMP=out.ppm [KBFB_RES=WxH] renders to memory and dumps, no /dev/fb0, no input
  char*dump=getenv("KBFB_DUMP"); int fd=-1;
  if(dump){
    xres=1024; yres=768; char*rs=getenv("KBFB_RES"); if(rs)sscanf(rs,"%dx%d",&xres,&yres);
    bpp=32; stride=xres*4; fblen=(long)stride*yres; fb=malloc(fblen);
  } else {
  // framebuffer
  fd=open("/dev/fb0",O_RDWR); if(fd<0)return 2;
  struct fb_var_screeninfo vi; struct fb_fix_screeninfo fi;
  if(ioctl(fd,FBIOGET_VSCREENINFO,&vi)||ioctl(fd,FBIOGET_FSCREENINFO,&fi)){close(fd);return 2;}
  xres=vi.xres; yres=vi.yres; bpp=vi.bits_per_pixel; stride=fi.line_length; fblen=fi.smem_len;
  if(bpp!=32&&bpp!=24){close(fd);return 2;}
  fb=mmap(0,fblen,PROT_READ|PROT_WRITE,MAP_SHARED,fd,0);
  if(fb==MAP_FAILED){close(fd);return 2;}
  }
  ox=(xres-CW)/2; if(ox<0)ox=0; oy=(yres-CH)/2; if(oy<0)oy=0;
  blit_chrome();
  // disk lines (up to 3)
  FILE*df=fopen(argv[4],"r"); if(df){char ln[128];int i=0;
    while(i<3&&fgets(ln,sizeof ln,df)){ln[strcspn(ln,"\n")]=0; text(DISK_X,DISK_Y+i*LINE_H,ln,TXT); i++;} fclose(df);}
  // test mode: draw the highlight on item 1 + a sample Choose line and dump BGRA->PPM, then exit
  if(dump){
    if(ni>0) hl(0);
    text(CHOOSE_X,CHOOSE_Y,"Choose: ",WHT); text(CHOOSE_X+64,CHOOSE_Y,"Up/Down move   Enter select   Esc boot menu",TXT);
    FILE*o=fopen(dump,"wb"); fprintf(o,"P6\n%d %d\n255\n",xres,yres);
    for(long i=0;i<(long)xres*yres;i++){unsigned char*p=fb+i*4; unsigned char rgb[3]={p[2],p[1],p[0]}; fwrite(rgb,1,3,o);} fclose(o);
    return 0;
  }
  // input loop: arrow keys move the highlight (default item 1); typing a number also works; Enter picks.
  struct termios old,raw; tcgetattr(0,&old); raw=old; raw.c_lflag&=~(ICANON|ECHO); tcsetattr(0,TCSANOW,&raw);
  char buf[16]={0}; int n=0, sel=0;
  if(ni>0) hl(sel);
  for(;;){
    clearzone(CHOOSE_X,CHOOSE_Y-2,460,22);
    text(CHOOSE_X,CHOOSE_Y,"Choose: ",WHT);
    if(n){ text(CHOOSE_X+64,CHOOSE_Y,buf,ACC); text(CHOOSE_X+64+n*8,CHOOSE_Y,"_",ACC); }
    else text(CHOOSE_X+64,CHOOSE_Y,"Up/Down move   Enter select   Esc boot menu   (or type a number)",TXT);
    unsigned char c; if(read(0,&c,1)!=1) break;
    if(c==0x1b){                                   // arrow key (Esc [ X) or a lone Esc (= back to the boot menu)
      struct termios t2=raw; t2.c_cc[VMIN]=0; t2.c_cc[VTIME]=1; tcsetattr(0,TCSANOW,&t2);  // peek with a 0.1s timeout
      unsigned char a=0; int r=read(0,&a,1); tcsetattr(0,TCSANOW,&raw);
      if(r<=0){ strcpy(buf,"back"); n=4; break; }                                          // lone Esc
      unsigned char b=0; read(0,&b,1);
      if(a=='['&&ni>0){ unhl(sel);
        if(b=='A'||b=='D') sel=(sel-1+ni)%ni; else if(b=='B'||b=='C') sel=(sel+1)%ni;
        hl(sel); n=0; buf[0]=0; }
    }
    else if(c=='\r'||c=='\n') break;
    else if((c==127||c==8)&&n>0) buf[--n]=0;
    else if(n<8 && ((c>='0'&&c<='9')||(c>='a'&&c<='z')||(c>='A'&&c<='Z'))) { buf[n++]=c; buf[n]=0; }
  }
  tcsetattr(0,TCSANOW,&old);
  munmap(fb,fblen); close(fd);
  if(n==0 && ni>0) printf("%s\n", inum[sel]); else printf("%s\n", buf);
  return 0;
}
