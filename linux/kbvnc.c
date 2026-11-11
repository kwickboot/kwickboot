/*
 * kbvnc - remote control of the KwickBoot RAM OS screen (web console "Remote control").
 *
 * A minimal RFB 3.8 (VNC) server: shares /dev/fb0 on 127.0.0.1:5901 and feeds the viewer's mouse + keyboard into a
 * virtual tablet + keyboard created with uinput, so the web desktop (kbweb) and kbgui treat them like real USB input.
 * Only localhost: the web console reaches it through kbpty's authenticated dial-back relay (tcp:5901), never directly.
 *
 * Started at boot BEFORE the screens, because kbweb/kbgui pick their input devices when they start: the uinput device
 * is created first, its /dev/input/event* node waited for, then /run/kb-uinput is touched (init waits for it).
 *
 * Updates: only screen tiles that changed since the last update, as zlib-compressed rectangles (one deflate stream per
 * connection, RFB encoding 6, which noVNC decodes) or raw when the viewer cannot do zlib. Pixels are converted to
 * whatever true-colour format the viewer asks for. One viewer at a time - a new viewer takes over and the previous one
 * is closed (its held keys/buttons released). Build without zlib with -DKB_NO_ZLIB.
 *
 *   kbvnc [port]     (default 5901)
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/fb.h>
#include <linux/input.h>
#include <linux/uinput.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <sys/types.h>
#include <arpa/inet.h>
#include <dirent.h>
#include <time.h>
#include <unistd.h>
#ifndef KB_NO_ZLIB
#include <zlib.h>
#endif

#define TILE 64

/* ---------------------------------------------------------------- framebuffer */
static unsigned char *fb, *shadow;
static int W, H, FBSTRIDE, FBBPP;
static struct fb_bitfield RO, GO, BO;

static int fb_open(void) {
    int fd = open("/dev/fb0", O_RDONLY);
    if (fd < 0) return -1;
    struct fb_var_screeninfo v; struct fb_fix_screeninfo f;
    if (ioctl(fd, FBIOGET_VSCREENINFO, &v) || ioctl(fd, FBIOGET_FSCREENINFO, &f)) { close(fd); return -1; }
    W = v.xres; H = v.yres; FBSTRIDE = f.line_length; FBBPP = v.bits_per_pixel;
    RO = v.red; GO = v.green; BO = v.blue;
    if (W <= 0 || H <= 0 || (FBBPP != 32 && FBBPP != 24 && FBBPP != 16)) { close(fd); return -1; }
    fb = mmap(0, (size_t)FBSTRIDE * H, PROT_READ, MAP_SHARED, fd, 0);
    close(fd);
    if (fb == MAP_FAILED) return -1;
    shadow = calloc(1, (size_t)FBSTRIDE * H);
    return shadow ? 0 : -1;
}

static inline void fb_rgb(const unsigned char *p, unsigned *r, unsigned *g, unsigned *b) {
    uint32_t v = FBBPP == 32 ? (uint32_t)p[0] | p[1] << 8 | p[2] << 16 | (uint32_t)p[3] << 24
               : FBBPP == 24 ? (uint32_t)p[0] | p[1] << 8 | p[2] << 16 : (uint32_t)p[0] | p[1] << 8;
    #define CH(bf) ((bf).length ? (((v >> (bf).offset) & ((1u << (bf).length) - 1)) * 255 / ((1u << (bf).length) - 1)) : 0)
    *r = CH(RO); *g = CH(GO); *b = CH(BO);
    #undef CH
}

/* ---------------------------------------------------------------- viewer pixel format */
static struct { int bpp, big, rmax, gmax, bmax, rsh, gsh, bsh; } pf = {32, 0, 255, 255, 255, 16, 8, 0};

static unsigned char *conv;   /* converted rect buffer */
static size_t convcap;

static size_t convert_rect(int x0, int y0, int w, int h) {
    int obpp = pf.bpp / 8; size_t need = (size_t)w * h * obpp;
    if (need > convcap) { free(conv); conv = malloc(need); convcap = conv ? need : 0; if (!conv) return 0; }
    unsigned char *o = conv; int ibpp = FBBPP / 8;
    for (int y = y0; y < y0 + h; y++) {
        const unsigned char *p = fb + (size_t)y * FBSTRIDE + (size_t)x0 * ibpp;
        for (int x = 0; x < w; x++, p += ibpp) {
            unsigned r, g, b; fb_rgb(p, &r, &g, &b);
            uint32_t v = (r * pf.rmax / 255) << pf.rsh | (g * pf.gmax / 255) << pf.gsh | (b * pf.bmax / 255) << pf.bsh;
            if (obpp == 4) { if (pf.big) { o[0] = v >> 24; o[1] = v >> 16; o[2] = v >> 8; o[3] = v; } else { o[0] = v; o[1] = v >> 8; o[2] = v >> 16; o[3] = v >> 24; } }
            else if (obpp == 2) { if (pf.big) { o[0] = v >> 8; o[1] = v; } else { o[0] = v; o[1] = v >> 8; } }
            else o[0] = v;
            o += obpp;
        }
    }
    return need;
}

/* ---------------------------------------------------------------- uinput: virtual tablet + keyboard */
static int ui = -1;

static void emit(int type, int code, int val) {
    if (ui < 0) return;
    struct input_event e; memset(&e, 0, sizeof e);
    e.type = type; e.code = code; e.value = val;
    if (write(ui, &e, sizeof e) < 0) { /* device gone: ignore */ }
}
static void syn(void) { emit(EV_SYN, SYN_REPORT, 0); }

static void wait_event_node(void) {   /* the /dev/input/eventN node of our device, so kbweb/kbgui see it at start */
    char name[64] = {0}, path[160];
    if (ioctl(ui, UI_GET_SYSNAME(sizeof name), name) < 0) { usleep(300000); return; }
    for (int t = 0; t < 40; t++) {    /* up to 2 s for mdev; then make the node ourselves */
        snprintf(path, sizeof path, "/sys/devices/virtual/input/%s", name);
        DIR *d = opendir(path); struct dirent *de; int found = 0;
        while (d && (de = readdir(d))) {
            if (strncmp(de->d_name, "event", 5)) continue;
            char dev[300], devf[600]; snprintf(dev, sizeof dev, "/dev/input/%s", de->d_name);
            if (access(dev, F_OK) == 0) { found = 1; break; }
            if (t >= 20) {
                snprintf(devf, sizeof devf, "%s/%s/dev", path, de->d_name);
                FILE *f = fopen(devf, "r"); unsigned ma, mi;
                if (f && fscanf(f, "%u:%u", &ma, &mi) == 2) { mkdir("/dev/input", 0755); mknod(dev, S_IFCHR | 0600, makedev(ma, mi)); found = 1; }
                if (f) fclose(f);
            }
        }
        if (d) closedir(d);
        if (found) return;
        usleep(50000);
    }
}

static int uinput_open(void) {
    int fd = open("/dev/uinput", O_WRONLY | O_NONBLOCK);
    if (fd < 0 && errno == ENOENT) { mknod("/dev/uinput", S_IFCHR | 0600, makedev(10, 223)); fd = open("/dev/uinput", O_WRONLY | O_NONBLOCK); }
    if (fd < 0) return -1;
    ioctl(fd, UI_SET_EVBIT, EV_KEY); ioctl(fd, UI_SET_EVBIT, EV_ABS); ioctl(fd, UI_SET_EVBIT, EV_REL); ioctl(fd, UI_SET_EVBIT, EV_SYN);
    for (int k = 1; k < 249; k++) ioctl(fd, UI_SET_KEYBIT, k);
    ioctl(fd, UI_SET_KEYBIT, BTN_LEFT); ioctl(fd, UI_SET_KEYBIT, BTN_RIGHT); ioctl(fd, UI_SET_KEYBIT, BTN_MIDDLE);
    ioctl(fd, UI_SET_ABSBIT, ABS_X); ioctl(fd, UI_SET_ABSBIT, ABS_Y); ioctl(fd, UI_SET_RELBIT, REL_WHEEL);
    struct uinput_user_dev u; memset(&u, 0, sizeof u);
    snprintf(u.name, UINPUT_MAX_NAME_SIZE, "KwickBoot remote control");
    u.id.bustype = BUS_VIRTUAL; u.id.vendor = 0x4b42; u.id.product = 0x0001; u.id.version = 1;
    u.absmin[ABS_X] = 0; u.absmax[ABS_X] = W > 1 ? W - 1 : 1;
    u.absmin[ABS_Y] = 0; u.absmax[ABS_Y] = H > 1 ? H - 1 : 1;
    if (write(fd, &u, sizeof u) != (ssize_t)sizeof u || ioctl(fd, UI_DEV_CREATE) < 0) { close(fd); return -1; }
    ui = fd; wait_event_node();
    return 0;
}

/* X11 keysym -> Linux key code (US layout; the viewer sends Shift itself, so 'A' and 'a' share KEY_A) */
static int keymap(uint32_t ks) {
    if (ks >= 'a' && ks <= 'z') ks -= 32;
    static const unsigned char letters[26] = {KEY_A,KEY_B,KEY_C,KEY_D,KEY_E,KEY_F,KEY_G,KEY_H,KEY_I,KEY_J,KEY_K,KEY_L,KEY_M,
        KEY_N,KEY_O,KEY_P,KEY_Q,KEY_R,KEY_S,KEY_T,KEY_U,KEY_V,KEY_W,KEY_X,KEY_Y,KEY_Z};
    if (ks >= 'A' && ks <= 'Z') return letters[ks - 'A'];
    if (ks >= '1' && ks <= '9') return KEY_1 + (ks - '1');
    switch (ks) {
    case '0': case ')': return KEY_0;
    case '!': return KEY_1; case '@': return KEY_2; case '#': return KEY_3; case '$': return KEY_4; case '%': return KEY_5;
    case '^': return KEY_6; case '&': return KEY_7; case '*': return KEY_8; case '(': return KEY_9;
    case ' ': return KEY_SPACE; case '-': case '_': return KEY_MINUS; case '=': case '+': return KEY_EQUAL;
    case '[': case '{': return KEY_LEFTBRACE; case ']': case '}': return KEY_RIGHTBRACE; case '\\': case '|': return KEY_BACKSLASH;
    case ';': case ':': return KEY_SEMICOLON; case '\'': case '"': return KEY_APOSTROPHE; case '`': case '~': return KEY_GRAVE;
    case ',': case '<': return KEY_COMMA; case '.': case '>': return KEY_DOT; case '/': case '?': return KEY_SLASH;
    case 0xff08: return KEY_BACKSPACE; case 0xff09: return KEY_TAB; case 0xff0d: return KEY_ENTER; case 0xff1b: return KEY_ESC;
    case 0xffff: return KEY_DELETE; case 0xff50: return KEY_HOME; case 0xff51: return KEY_LEFT; case 0xff52: return KEY_UP;
    case 0xff53: return KEY_RIGHT; case 0xff54: return KEY_DOWN; case 0xff55: return KEY_PAGEUP; case 0xff56: return KEY_PAGEDOWN;
    case 0xff57: return KEY_END; case 0xff63: return KEY_INSERT; case 0xff13: return KEY_PAUSE; case 0xff61: return KEY_SYSRQ;
    case 0xff14: return KEY_SCROLLLOCK; case 0xff7f: return KEY_NUMLOCK; case 0xff67: return KEY_COMPOSE; case 0xff8d: return KEY_KPENTER;
    case 0xffe1: return KEY_LEFTSHIFT; case 0xffe2: return KEY_RIGHTSHIFT; case 0xffe3: return KEY_LEFTCTRL; case 0xffe4: return KEY_RIGHTCTRL;
    case 0xffe5: return KEY_CAPSLOCK; case 0xffe9: case 0xffe7: return KEY_LEFTALT; case 0xffea: case 0xffe8: case 0xfe03: return KEY_RIGHTALT;
    case 0xffeb: return KEY_LEFTMETA; case 0xffec: return KEY_RIGHTMETA;
    }
    if (ks >= 0xffbe && ks <= 0xffc7) return KEY_F1 + (int)(ks - 0xffbe);   /* F1..F10 */
    if (ks == 0xffc8) return KEY_F11;
    if (ks == 0xffc9) return KEY_F12;
    return -1;
}

static unsigned char down[256];   /* keys we pressed, released when the viewer goes away */
static int lastmask;

static void release_all(void) {
    for (int k = 0; k < 256; k++) if (down[k]) { emit(EV_KEY, k, 0); down[k] = 0; }
    if (lastmask & 1) emit(EV_KEY, BTN_LEFT, 0);
    if (lastmask & 2) emit(EV_KEY, BTN_MIDDLE, 0);
    if (lastmask & 4) emit(EV_KEY, BTN_RIGHT, 0);
    lastmask = 0; syn();
}

/* ---------------------------------------------------------------- socket helpers */
static int rdall(int s, void *b, size_t n) {
    unsigned char *p = b;
    while (n) { ssize_t r = recv(s, p, n, 0); if (r <= 0) { if (r < 0 && errno == EINTR) continue; return -1; } p += r; n -= r; }
    return 0;
}
static int wrall(int s, const void *b, size_t n) {
    const unsigned char *p = b;
    while (n) { ssize_t r = send(s, p, n, MSG_NOSIGNAL); if (r <= 0) { if (r < 0 && errno == EINTR) continue; return -1; } p += r; n -= r; }
    return 0;
}
static void put16(unsigned char *p, unsigned v) { p[0] = v >> 8; p[1] = v; }
static void put32(unsigned char *p, uint32_t v) { p[0] = v >> 24; p[1] = v >> 16; p[2] = v >> 8; p[3] = v; }

/* ---------------------------------------------------------------- one viewer */
#ifndef KB_NO_ZLIB
static z_stream zs; static int zs_ready;
static unsigned char *zout; static size_t zcap;
#endif
static int use_zlib;

static int send_rects(int s, int n, const int (*r)[4]) {
    unsigned char hdr[4] = {0, 0, 0, 0}; put16(hdr + 2, n);
    if (wrall(s, hdr, 4)) return -1;
    for (int i = 0; i < n; i++) {
        int x = r[i][0], y = r[i][1], w = r[i][2], h = r[i][3];
        size_t len = convert_rect(x, y, w, h); if (!len) return -1;
        unsigned char rh[12]; put16(rh, x); put16(rh + 2, y); put16(rh + 4, w); put16(rh + 6, h);
#ifndef KB_NO_ZLIB
        if (use_zlib) {
            put32(rh + 8, 6);
            size_t bound = deflateBound(&zs, len) + 64;
            if (bound > zcap) { free(zout); zout = malloc(bound); zcap = zout ? bound : 0; if (!zout) return -1; }
            zs.next_in = conv; zs.avail_in = len; zs.next_out = zout; zs.avail_out = zcap;
            if (deflate(&zs, Z_SYNC_FLUSH) != Z_OK) return -1;
            uint32_t clen = zcap - zs.avail_out; unsigned char lb[4]; put32(lb, clen);
            if (wrall(s, rh, 12) || wrall(s, lb, 4) || wrall(s, zout, clen)) return -1;
            continue;
        }
#endif
        put32(rh + 8, 0);
        if (wrall(s, rh, 12) || wrall(s, conv, len)) return -1;
    }
    return 0;
}

/* changed tiles since the shadow copy -> merged horizontal runs per tile row; refreshes the shadow for what is sent */
static int (*rects)[4]; static int rectcap;
static int collect(int full) {
    int n = 0, bpp = FBBPP / 8, ntx = (W + TILE - 1) / TILE;
    for (int ty = 0; ty < H; ty += TILE) {
        int th = ty + TILE > H ? H - ty : TILE, run = -1;
        for (int i = 0; i <= ntx; i++) {          /* i == ntx: sentinel that flushes a run reaching the right edge */
            int tx = i * TILE, dirty = 0;
            if (i < ntx) {
                int tw = tx + TILE > W ? W - tx : TILE;
                if (full) dirty = 1;
                else for (int y = ty; y < ty + th && !dirty; y++)
                    if (memcmp(fb + (size_t)y * FBSTRIDE + (size_t)tx * bpp, shadow + (size_t)y * FBSTRIDE + (size_t)tx * bpp, (size_t)tw * bpp)) dirty = 1;
            }
            if (dirty && run < 0) run = tx;
            if (!dirty && run >= 0) {
                if (n >= rectcap) { rectcap = rectcap ? rectcap * 2 : 256; rects = realloc(rects, sizeof *rects * rectcap); if (!rects) return -1; }
                int rw = (i < ntx ? tx : W) - run;
                rects[n][0] = run; rects[n][1] = ty; rects[n][2] = rw; rects[n][3] = th; n++;
                for (int y = ty; y < ty + th; y++) memcpy(shadow + (size_t)y * FBSTRIDE + (size_t)run * bpp, fb + (size_t)y * FBSTRIDE + (size_t)run * bpp, (size_t)rw * bpp);
                run = -1;
            }
        }
    }
    return n;
}

static void serve(int s, int ls) {   /* ls: the listener - a NEW viewer connecting takes over from this one */
    int one = 1; setsockopt(s, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
    unsigned char b[64];
    if (wrall(s, "RFB 003.008\n", 12) || rdall(s, b, 12)) return;
    unsigned char sec[2] = {1, 1};                       /* one security type: None (the relay authenticated the user) */
    if (wrall(s, sec, 2) || rdall(s, b, 1) || b[0] != 1) return;
    unsigned char ok[4] = {0, 0, 0, 0};
    if (wrall(s, ok, 4) || rdall(s, b, 1)) return;        /* SecurityResult OK, ClientInit (shared flag) */
    const char *name = "KwickBoot RAM OS";
    unsigned char si[24 + 32]; memset(si, 0, sizeof si);
    put16(si, W); put16(si + 2, H);
    si[4] = 32; si[5] = 24; si[6] = 0; si[7] = 1; put16(si + 8, 255); put16(si + 10, 255); put16(si + 12, 255);
    si[14] = 16; si[15] = 8; si[16] = 0; put32(si + 20, strlen(name)); memcpy(si + 24, name, strlen(name));
    if (wrall(s, si, 24 + strlen(name))) return;
    pf.bpp = 32; pf.big = 0; pf.rmax = pf.gmax = pf.bmax = 255; pf.rsh = 16; pf.gsh = 8; pf.bsh = 0;
    use_zlib = 0;
#ifndef KB_NO_ZLIB
    if (zs_ready) deflateEnd(&zs);
    memset(&zs, 0, sizeof zs); zs_ready = deflateInit(&zs, 1) == Z_OK;
#endif
    int want = 0, full = 1;
    for (;;) {
        struct pollfd p[2] = {{s, POLLIN, 0}, {ls, POLLIN, 0}};
        int r = poll(p, 2, want ? 40 : 1000);
        if (r < 0 && errno != EINTR) break;
        if (r > 0 && (p[1].revents & POLLIN)) break;   /* newest viewer wins: close this one, main() accepts the new one */
        if (r > 0 && (p[0].revents & (POLLIN | POLLHUP | POLLERR))) {
            if (rdall(s, b, 1)) break;
            switch (b[0]) {
            case 0:  /* SetPixelFormat */
                if (rdall(s, b, 19)) goto out;
                if (b[3 + 3] && (b[3] == 32 || b[3] == 16 || b[3] == 8)) {   /* true-colour only */
                    pf.bpp = b[3]; pf.big = b[5]; pf.rmax = b[7] << 8 | b[8]; pf.gmax = b[9] << 8 | b[10]; pf.bmax = b[11] << 8 | b[12];
                    pf.rsh = b[13]; pf.gsh = b[14]; pf.bsh = b[15]; full = 1;
                }
                break;
            case 2: { /* SetEncodings */
                if (rdall(s, b, 3)) goto out;
                int n = b[1] << 8 | b[2]; use_zlib = 0;
                for (int i = 0; i < n; i++) { unsigned char e[4]; if (rdall(s, e, 4)) goto out; int32_t enc = (int32_t)(e[0] << 24 | e[1] << 16 | e[2] << 8 | e[3]);
#ifndef KB_NO_ZLIB
                    if (enc == 6 && zs_ready) use_zlib = 1;
#else
                    (void)enc;
#endif
                }
                break; }
            case 3:  /* FramebufferUpdateRequest */
                if (rdall(s, b, 9)) goto out;
                want = 1; if (!b[0]) full = 1;
                break;
            case 4: { /* KeyEvent */
                if (rdall(s, b, 7)) goto out;
                uint32_t ks = (uint32_t)b[3] << 24 | b[4] << 16 | b[5] << 8 | b[6]; int k = keymap(ks);
                if (k > 0 && k < 256) { emit(EV_KEY, k, b[0] ? 1 : 0); down[k] = b[0] ? 1 : 0; syn(); }
                break; }
            case 5: { /* PointerEvent */
                if (rdall(s, b, 5)) goto out;
                int m = b[0], x = b[1] << 8 | b[2], y = b[3] << 8 | b[4];
                if (x >= W) x = W - 1;
                if (y >= H) y = H - 1;
                emit(EV_ABS, ABS_X, x); emit(EV_ABS, ABS_Y, y);
                if ((m ^ lastmask) & 1) emit(EV_KEY, BTN_LEFT, m & 1 ? 1 : 0);
                if ((m ^ lastmask) & 2) emit(EV_KEY, BTN_MIDDLE, m & 2 ? 1 : 0);
                if ((m ^ lastmask) & 4) emit(EV_KEY, BTN_RIGHT, m & 4 ? 1 : 0);
                if ((m & 8) && !(lastmask & 8)) emit(EV_REL, REL_WHEEL, 1);
                if ((m & 16) && !(lastmask & 16)) emit(EV_REL, REL_WHEEL, -1);
                lastmask = m & 7; syn();
                break; }
            case 6: { /* ClientCutText: read and ignore */
                if (rdall(s, b, 7)) goto out;
                uint32_t n = (uint32_t)b[3] << 24 | b[4] << 16 | b[5] << 8 | b[6];
                while (n) { size_t c = n > sizeof b ? sizeof b : n; if (rdall(s, b, c)) goto out; n -= c; }
                break; }
            default: goto out;      /* unknown message: we cannot know its length */
            }
            continue;               /* drain all pending input before sending a frame */
        }
        if (want) {
            int n = collect(full);
            if (n < 0) break;
            if (n > 0) { if (send_rects(s, n, (const int (*)[4])rects)) break; want = 0; full = 0; }
        }
    }
out:
    release_all();
}

int main(int argc, char **argv) {
    int port = argc > 1 ? atoi(argv[1]) : 5901;
    signal(SIGPIPE, SIG_IGN);
    if (fb_open()) { fprintf(stderr, "kbvnc: no usable /dev/fb0\n"); return 1; }
    if (uinput_open()) fprintf(stderr, "kbvnc: no uinput - view only\n");
    { int f = open("/run/kb-uinput", O_WRONLY | O_CREAT, 0644); if (f >= 0) close(f); }   /* init waits for this */
    int ls = socket(AF_INET, SOCK_STREAM, 0), one = 1;
    setsockopt(ls, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    struct sockaddr_in a; memset(&a, 0, sizeof a);
    a.sin_family = AF_INET; a.sin_port = htons(port); a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    while (bind(ls, (struct sockaddr *)&a, sizeof a)) {   /* loopback may not be up yet this early in boot: keep trying */
        if (errno != EADDRNOTAVAIL && errno != EADDRINUSE) { perror("kbvnc: bind"); return 1; }
        sleep(1);                                          /* (exiting would also destroy the uinput device the GUI uses) */
    }
    if (listen(ls, 2)) { perror("kbvnc: listen"); return 1; }
    for (;;) {
        int s = accept(ls, 0, 0);
        if (s < 0) { if (errno == EINTR) continue; sleep(1); continue; }
        serve(s, ls);
        close(s);
    }
}
