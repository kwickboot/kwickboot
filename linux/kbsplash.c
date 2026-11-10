// kbsplash - draw the KwickBoot boot splash (plymouth-style) on the framebuffer, with a progress bar + status line.
// SPDX-License-Identifier: AGPL-3.0-or-later
//   kbsplash <splash.bgra.gz> <splash.info> <font.raw(8x16)> [percent 0-100] [status text ...]
// Blits the splash once, fills the progress track to <percent>, draws <status text> centered on the status line.
// Exits 0 on success, 2 if there is no usable 32bpp framebuffer (the caller then stays on the plain text console).
// Preview on the build box: KBFB_DUMP=out.ppm [KBFB_RES=WxH] kbsplash splash.bgra.gz splash.info font.raw 42 Loading
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/ioctl.h>
#include <linux/fb.h>
#define CW 1024
#define CH 768
static unsigned char img[CW*CH*4];        // BGRA splash
static unsigned char font[4096];          // 256 glyphs, 8x16, 1bpp
static int PX=252,PY=560,PW=520,PH=10,STX=512,STY=592,SPY=496;
static const unsigned char BG[4]={54,34,27,255};    // navy #1b2236 (B,G,R,A)
static const unsigned char ACC[4]={87,119,217,255}; // #d97757
static const unsigned char TXT[4]={228,214,207,255};
static unsigned char *fb; static long fblen;
static int xres,yres,bpp,stride,ox,oy;

static void putpx(int x,int y,const unsigned char*c){
  if(x<0||y<0||x>=xres||y>=yres) return;
  unsigned char*p=fb+(long)y*stride+(long)x*(bpp/8);
  p[0]=c[0];p[1]=c[1];p[2]=c[2]; if(bpp==32)p[3]=c[3];
}
static void blit(void){
  for(int y=0;y<yres;y++)for(int x=0;x<xres;x++){
    int cx=x-ox, cy=y-oy;
    if(cx>=0&&cy>=0&&cx<CW&&cy<CH) putpx(x,y,&img[((long)cy*CW+cx)*4]); else putpx(x,y,BG);
  }
}
static void fillrect(int gx,int gy,int w,int h,const unsigned char*c){
  for(int y=0;y<h;y++)for(int x=0;x<w;x++) putpx(ox+gx+x,oy+gy+y,c);
}
static void glyph(int gx,int gy,unsigned char ch,const unsigned char*fg){
  const unsigned char*g=&font[ch*16];
  for(int r=0;r<16;r++)for(int b=0;b<8;b++) if(g[r]&(0x80>>b)) putpx(ox+gx+b,oy+gy+r,fg);
}
static void textc(int cxp,int gy,const char*s,const unsigned char*fg){   // centered on chrome-x cxp
  int n=strlen(s), gx=cxp-(n*8)/2;
  for(;*s;s++){ glyph(gx,gy,(unsigned char)*s,fg); gx+=8; }
}
// animated spinner ring (12 dots on a circle, a bright "comet" head rotating) - a Windows-style boot animation.
static const int SPOFF[12][2]={{0,-22},{11,-19},{19,-11},{22,0},{19,11},{11,19},{0,22},{-11,19},{-19,11},{-22,0},{-19,-11},{-11,-19}};
static void spinner(int cx,int cy,int phase){
  fillrect(cx-30,cy-30,60,60,BG);                 // clear the ring area back to the splash background
  for(int i=0;i<12;i++){
    int d=((i-phase)%12+12)%12, t=12-d;           // 12 at the head .. 1 at the tail
    unsigned char c[4]={(unsigned char)((BG[0]*(12-t)+ACC[0]*t)/12),(unsigned char)((BG[1]*(12-t)+ACC[1]*t)/12),(unsigned char)((BG[2]*(12-t)+ACC[2]*t)/12),255};
    if(d==0){ c[0]=255;c[1]=255;c[2]=255; }        // head: white
    fillrect(cx+SPOFF[i][0]-3, cy+SPOFF[i][1]-3, 6,6, c);
  }
}

int main(int argc,char**argv){
  if(argc<4){fprintf(stderr,"usage: kbsplash splash.bgra.gz splash.info font.raw [pct] [status...]\n");return 2;}
  char cmd[1024]; snprintf(cmd,sizeof cmd,"gzip -dc '%s'",argv[1]);
  FILE*cf=popen(cmd,"r"); if(!cf)return 2;
  if(fread(img,1,sizeof img,cf)!=sizeof img){pclose(cf);return 2;} pclose(cf);
  FILE*inf=fopen(argv[2],"r"); if(inf){char ln[128];int v;
    while(fgets(ln,sizeof ln,inf)){
      if(sscanf(ln,"PX=%d",&v)==1)PX=v; else if(sscanf(ln,"PY=%d",&v)==1)PY=v;
      else if(sscanf(ln,"PW=%d",&v)==1)PW=v; else if(sscanf(ln,"PH=%d",&v)==1)PH=v;
      else if(sscanf(ln,"STX=%d",&v)==1)STX=v; else if(sscanf(ln,"STY=%d",&v)==1)STY=v;
      else if(sscanf(ln,"SPY=%d",&v)==1)SPY=v; }
    fclose(inf);}
  int ff=open(argv[3],O_RDONLY); if(ff<0||read(ff,font,sizeof font)!=sizeof font){if(ff>=0)close(ff);return 2;} close(ff);
  int pct = argc>4 ? atoi(argv[4]) : -1; if(pct>100)pct=100;
  char status[256]=""; for(int i=5;i<argc;i++){ if(i>5)strncat(status," ",sizeof status-strlen(status)-1); strncat(status,argv[i],sizeof status-strlen(status)-1); }

  char*dump=getenv("KBFB_DUMP");
  if(dump){ xres=1024;yres=768; char*rs=getenv("KBFB_RES"); if(rs)sscanf(rs,"%dx%d",&xres,&yres);
    bpp=32; stride=xres*4; fblen=(long)stride*yres; fb=calloc(1,fblen);
  } else {
    int fd=open("/dev/fb0",O_RDWR); if(fd<0)return 2;
    struct fb_var_screeninfo v; struct fb_fix_screeninfo f;
    if(ioctl(fd,FBIOGET_VSCREENINFO,&v)||ioctl(fd,FBIOGET_FSCREENINFO,&f)){close(fd);return 2;}
    xres=v.xres; yres=v.yres; bpp=v.bits_per_pixel; stride=f.line_length;
    if(bpp!=32&&bpp!=24){close(fd);return 2;}
    fblen=(long)stride*yres; fb=mmap(0,fblen,PROT_READ|PROT_WRITE,MAP_SHARED,fd,0);
    if(fb==MAP_FAILED){close(fd);return 2;}
  }
  ox=(xres-CW)/2; oy=(yres-CH)/2;
  blit();
  if(pct>=0) fillrect(PX,PY,(PW*pct)/100,PH,ACC);        // progress fill over the track
  if(status[0]) textc(STX,STY,status,TXT);
  // KB_SPIN=<seconds>: animate the spinner ring for that long (a visible, Windows-style boot animation + dwell)
  char*sp=getenv("KB_SPIN");
  if(sp && !dump){ double sec=atof(sp); int frames=(int)(sec*12.5);
    for(int fme=0; fme<frames; fme++){ spinner(STX,SPY,fme); usleep(80000); } }
  if(dump){ spinner(STX,SPY,3);                           // show one spinner frame in the preview dump
      FILE*o=fopen(dump,"wb"); if(o){ fprintf(o,"P6\n%d %d\n255\n",xres,yres);
      for(long i=0;i<(long)xres*yres;i++){ unsigned char*p=fb+(i/xres)*stride+(i%xres)*(bpp/8); unsigned char rgb[3]={p[2],p[1],p[0]}; fwrite(rgb,1,3,o);} fclose(o);} }
  return 0;
}
