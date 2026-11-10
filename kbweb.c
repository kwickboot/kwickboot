// kbweb - KwickBoot framebuffer WPE browser (RICH tier, CPU-only).
// WebKit (WPE) renders our /local console on the CPU (Skia) to an offscreen SHM buffer via the wpebackend-fdo
// SHM exportable backend; each exported frame is blitted straight to /dev/fb0 - no DRM master, no KMS modeset,
// no GL/GBM/EGL, no dmabuf. Works on ANY machine with a framebuffer (hyperv_drm, simpledrm, virtio-gpu, real GPUs),
// exactly where kbgui already works. Input: /dev/input/event* (evdev) -> WPE pointer/keyboard events.
//
// Build (in the WPE chroot):
//   gcc -O2 -o kbweb kbweb.c $(pkg-config --cflags --libs wpe-webkit-2.0 wpebackend-fdo-1.0) -lwayland-server
// Usage: kbweb <url>

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <dirent.h>
#include <poll.h>
#include <sys/mman.h>
#include <sys/ioctl.h>
#include <linux/fb.h>
#include <linux/input.h>

#include <wayland-server.h>
#include <xkbcommon/xkbcommon.h>
#include <wpe/webkit.h>
#include <wpe/wpe.h>   // also provides wpe_loader_init()
#include <wpe/fdo.h>
#include <wpe/unstable/fdo-shm.h>

// ---- framebuffer ----
static unsigned char *g_fb; static long g_fblen;
static int g_xres, g_yres, g_bpp, g_stride, g_fbfd=-1;
static unsigned char *g_framebuf;     // last composited page frame (screen-sized BGRA); re-blitted so the cursor can move over it
static int g_cx, g_cy, g_btnmods;     // pointer position + button modifier state
static int g_vtfd = -1;               // active VT fd, put in KD_GRAPHICS so fbcon's cursor/blanker don't draw over us

static int fb_open(void){
    g_fbfd = open("/dev/fb0", O_RDWR);
    if (g_fbfd < 0) { perror("open /dev/fb0"); return -1; }
    struct fb_var_screeninfo v; struct fb_fix_screeninfo f;
    if (ioctl(g_fbfd, FBIOGET_VSCREENINFO, &v) || ioctl(g_fbfd, FBIOGET_FSCREENINFO, &f)) { perror("fb ioctl"); return -1; }
    g_xres = v.xres; g_yres = v.yres; g_bpp = v.bits_per_pixel; g_stride = f.line_length;
    if (g_bpp != 32 && g_bpp != 24) { fprintf(stderr, "kbweb: unsupported bpp %d\n", g_bpp); return -1; }
    g_fblen = (long)g_stride * g_yres;
    g_fb = mmap(0, g_fblen, PROT_READ|PROT_WRITE, MAP_SHARED, g_fbfd, 0);
    if (g_fb == MAP_FAILED) { perror("mmap fb"); return -1; }
    g_framebuf = calloc((long)g_xres*g_yres, 4);
    if (!g_framebuf) { fprintf(stderr, "kbweb: framebuf alloc failed\n"); return -1; }
    fprintf(stderr, "kbweb: fb %dx%d bpp%d stride%d\n", g_xres, g_yres, g_bpp, g_stride);
    return 0;
}

// Hide the console text cursor and disable console blanking so fbcon's blinking cursor doesn't sit over our framebuffer
// and the kernel console-blanker doesn't clear the screen after idle. NB: do NOT use KD_GRAPHICS - on virtio_gpu /
// hyperv_drm that stops the fbdev->DRM-scanout flush and the whole screen goes black. Plain tty escapes keep the fb path.
static void setup_vt(void){
    g_vtfd = open("/dev/tty0", O_RDWR);
    if (g_vtfd < 0) return;
    static const char seq[] = "\033[?25l\033[9;0]";   // ESC[?25l = hide cursor; ESC[9;0] = blank timeout 0 (never)
    if (write(g_vtfd, seq, sizeof seq - 1) < 0) { /* ignore */ }
}

// A simple arrow cursor: 'X' = black outline, '.' = white fill, ' ' = transparent. Composited on top of the page.
static const char *const CURSOR[] = {
  "X          ","XX         ","X.X        ","X..X       ","X...X      ",
  "X....X     ","X.....X    ","X......X   ","X.......X  ","X........X ",
  "X.........X","X....XXXXXX","X..X..X    ","X.X X..X   ","XX  X..X   ",
  "    X..X   ","    X..X   ","     XX    " };
static void put_px(int x, int y, unsigned char r, unsigned char g, unsigned char b){
    if (x<0||y<0||x>=g_xres||y>=g_yres) return;
    unsigned char *p = g_fb + (long)y*g_stride + (long)x*(g_bpp/8);
    p[0]=b; p[1]=g; p[2]=r; if (g_bpp==32) p[3]=255;
}
static void draw_cursor(int cx, int cy){
    int n = (int)(sizeof CURSOR / sizeof *CURSOR);
    for (int y=0; y<n; y++){ const char *row = CURSOR[y];
        for (int x=0; row[x]; x++){
            if (row[x]=='X') put_px(cx+x, cy+y, 0,0,0);
            else if (row[x]=='.') put_px(cx+x, cy+y, 255,255,255);
        } }
}
// Keep the last page frame (screen-sized) so a pointer move can re-composite the cursor without a fresh WebKit paint.
static void store_frame(const unsigned char *src, int sw, int sh, int sstride){
    if (!g_framebuf) return;
    int w = sw<g_xres?sw:g_xres, h = sh<g_yres?sh:g_yres;
    int ox = (g_xres-w)/2, oy = (g_yres-h)/2;
    if (w<g_xres || h<g_yres) memset(g_framebuf, 0, (long)g_xres*g_yres*4);
    for (int y=0; y<h; y++) memcpy(g_framebuf + ((long)(y+oy)*g_xres + ox)*4, src + (long)y*sstride, (size_t)w*4);
}
static void blit(const unsigned char *src, int sw, int sh, int sstride);   // defined just below
static void present(void){
    if (!g_framebuf) return;
    blit(g_framebuf, g_xres, g_yres, g_xres*4);
    draw_cursor(g_cx, g_cy);
}

// Blit a source ARGB/XRGB8888 (byte order B,G,R,A little-endian) buffer onto the framebuffer, centered.
static void blit(const unsigned char *src, int sw, int sh, int sstride){
    int w = sw < g_xres ? sw : g_xres, h = sh < g_yres ? sh : g_yres;
    int ox = (g_xres - w)/2, oy = (g_yres - h)/2;
    int dpp = g_bpp/8;
    for (int y = 0; y < h; y++){
        const unsigned char *s = src + (long)y*sstride;
        unsigned char *d = g_fb + (long)(y+oy)*g_stride + (long)ox*dpp;
        if (dpp == 4) {
            memcpy(d, s, (size_t)w*4);                 // BGRA -> BGRA direct
        } else {                                        // 24bpp: drop alpha
            for (int x = 0; x < w; x++){ d[0]=s[0]; d[1]=s[1]; d[2]=s[2]; d+=3; s+=4; }
        }
    }
}

// ---- WPE SHM exportable backend ----
static struct wpe_view_backend_exportable_fdo *g_exportable;

static void on_export_shm_buffer(void *data, struct wpe_fdo_shm_exported_buffer *buffer){
    (void)data;
    struct wl_shm_buffer *shm = wpe_fdo_shm_exported_buffer_get_shm_buffer(buffer);
    if (shm) {
        wl_shm_buffer_begin_access(shm);
        unsigned char *pix = wl_shm_buffer_get_data(shm);
        int w = wl_shm_buffer_get_width(shm), h = wl_shm_buffer_get_height(shm);
        int st = wl_shm_buffer_get_stride(shm);
        if (pix) { store_frame(pix, w, h, st); present(); }
        wl_shm_buffer_end_access(shm);
    }
    wpe_view_backend_exportable_fdo_dispatch_frame_complete(g_exportable);
    wpe_view_backend_exportable_fdo_dispatch_release_shm_exported_buffer(g_exportable, buffer);
}

static const struct wpe_view_backend_exportable_fdo_client s_client = {
    .export_buffer_resource = NULL,
    .export_dmabuf_resource = NULL,
    .export_shm_buffer = on_export_shm_buffer,
    ._wpe_reserved0 = NULL,
    ._wpe_reserved1 = NULL,
};

// ---- input (evdev -> WPE) ----
static struct wpe_view_backend *g_vb;
static struct xkb_context *g_xkb_ctx;
static struct xkb_keymap  *g_xkb_keymap;
static struct xkb_state   *g_xkb_state;

static uint32_t kbd_mods(void){
    uint32_t m = 0;
    if (xkb_state_mod_name_is_active(g_xkb_state, XKB_MOD_NAME_CTRL,  XKB_STATE_MODS_EFFECTIVE) > 0) m |= wpe_input_keyboard_modifier_control;
    if (xkb_state_mod_name_is_active(g_xkb_state, XKB_MOD_NAME_SHIFT, XKB_STATE_MODS_EFFECTIVE) > 0) m |= wpe_input_keyboard_modifier_shift;
    if (xkb_state_mod_name_is_active(g_xkb_state, XKB_MOD_NAME_ALT,   XKB_STATE_MODS_EFFECTIVE) > 0) m |= wpe_input_keyboard_modifier_alt;
    if (xkb_state_mod_name_is_active(g_xkb_state, XKB_MOD_NAME_LOGO,  XKB_STATE_MODS_EFFECTIVE) > 0) m |= wpe_input_keyboard_modifier_meta;
    return m;
}

static void handle_key(uint32_t evcode, int value){     // value: 0 up, 1 down, 2 autorepeat
    if (!g_xkb_state) return;
    xkb_keycode_t xkc = evcode + 8;                     // evdev keycode -> xkb keycode
    xkb_keysym_t sym = xkb_state_key_get_one_sym(g_xkb_state, xkc);
    struct wpe_input_keyboard_event ke = { 0, (uint32_t)sym, evcode, value != 0, kbd_mods() };
    wpe_view_backend_dispatch_keyboard_event(g_vb, &ke);
    if (value == 1)      xkb_state_update_key(g_xkb_state, xkc, XKB_KEY_DOWN);
    else if (value == 0) xkb_state_update_key(g_xkb_state, xkc, XKB_KEY_UP);
}

static void send_motion(void){
    if (g_cx<0)g_cx=0; if(g_cy<0)g_cy=0; if(g_cx>=g_xres)g_cx=g_xres; if(g_cy>=g_yres)g_cy=g_yres;
    struct wpe_input_pointer_event pe = { wpe_input_pointer_event_type_motion, 0, g_cx, g_cy, 0, 0, g_btnmods };
    wpe_view_backend_dispatch_pointer_event(g_vb, &pe);
}

// Per-device absolute axis calibration (mouse/keyboard leave is_abs=0; tablets & touchscreens fill the ranges).
struct indev { int is_abs; long xmin, xmax, ymin, ymax; };

static gboolean input_cb(GIOChannel *src, GIOCondition cond, gpointer udata){
    (void)cond; int fd = g_io_channel_unix_get_fd(src); struct indev *d = udata;
    struct input_event ev; int rd; int moved=0, redraw=0;
    while ((rd = read(fd, &ev, sizeof ev)) == sizeof ev){
        if (ev.type == EV_REL){ if(ev.code==REL_X){g_cx+=ev.value;moved=1;redraw=1;} else if(ev.code==REL_Y){g_cy+=ev.value;moved=1;redraw=1;} }
        else if (ev.type == EV_ABS && d->is_abs){     // VM tablet OR touchscreen (single-touch ABS_* or multitouch ABS_MT_POSITION_*)
            if ((ev.code==ABS_X || ev.code==ABS_MT_POSITION_X) && d->xmax>d->xmin){ g_cx=(int)((long)(ev.value-d->xmin)*(g_xres-1)/(d->xmax-d->xmin)); moved=1; redraw=1; }
            else if ((ev.code==ABS_Y || ev.code==ABS_MT_POSITION_Y) && d->ymax>d->ymin){ g_cy=(int)((long)(ev.value-d->ymin)*(g_yres-1)/(d->ymax-d->ymin)); moved=1; redraw=1; }
        }
        else if (ev.type == EV_KEY){
            if (ev.code == BTN_LEFT || ev.code == BTN_TOUCH){     // mouse click OR finger down/up -> a tap is a click
                if (moved){ send_motion(); moved=0; }
                int down = ev.value ? 1 : 0;
                g_btnmods = down ? (g_btnmods|wpe_input_pointer_modifier_button1) : (g_btnmods&~wpe_input_pointer_modifier_button1);
                struct wpe_input_pointer_event pe = { wpe_input_pointer_event_type_button, 0, g_cx, g_cy, 1, down, g_btnmods };
                wpe_view_backend_dispatch_pointer_event(g_vb, &pe);
                redraw=1;
            } else if (ev.code < BTN_MISC){            // a keyboard key, not a pointer button
                handle_key(ev.code, ev.value);
            }
        }
    }
    if (moved) send_motion();
    if (redraw) present();     // move the cursor over the page without waiting for a new WebKit paint
    return TRUE;
}

static void setup_input(void){
    DIR *dp = opendir("/dev/input"); struct dirent *de; if(!dp) return;
    while ((de = readdir(dp))){
        if (strncmp(de->d_name, "event", 5)) continue;
        char path[300]; snprintf(path, sizeof path, "/dev/input/%s", de->d_name);
        int fd = open(path, O_RDONLY|O_NONBLOCK); if (fd < 0) continue;
        unsigned long evbit=0, absbit=0;
        ioctl(fd, EVIOCGBIT(0, sizeof evbit), &evbit);
        if (!(evbit & ((1UL<<EV_KEY)|(1UL<<EV_REL)|(1UL<<EV_ABS)))){ close(fd); continue; }  // keyboard, mouse, tablet or touchscreen
        struct indev *d = calloc(1, sizeof *d); if(!d){ close(fd); continue; }
        d->xmax = g_xres-1; d->ymax = g_yres-1;
        if (evbit & (1<<EV_ABS)){
            ioctl(fd, EVIOCGBIT(EV_ABS, sizeof absbit), &absbit);
            struct input_absinfo ai;
            int xc = (absbit & (1UL<<ABS_X)) ? ABS_X : (absbit & (1UL<<ABS_MT_POSITION_X)) ? ABS_MT_POSITION_X : -1;
            int yc = (absbit & (1UL<<ABS_Y)) ? ABS_Y : (absbit & (1UL<<ABS_MT_POSITION_Y)) ? ABS_MT_POSITION_Y : -1;
            if (xc>=0 && ioctl(fd,EVIOCGABS(xc),&ai)>=0 && ai.maximum>ai.minimum){ d->is_abs=1; d->xmin=ai.minimum; d->xmax=ai.maximum; }
            if (yc>=0 && ioctl(fd,EVIOCGABS(yc),&ai)>=0 && ai.maximum>ai.minimum){ d->ymin=ai.minimum; d->ymax=ai.maximum; }
        }
        GIOChannel *ch = g_io_channel_unix_new(fd);
        g_io_add_watch(ch, G_IO_IN, (GIOFunc)input_cb, d);
    }
    closedir(dp);
}

static void setup_keymap(void){     // default us layout; evdev rules. Used to turn evdev keycodes into keysyms.
    g_xkb_ctx = xkb_context_new(XKB_CONTEXT_NO_FLAGS);
    if (!g_xkb_ctx) return;
    g_xkb_keymap = xkb_keymap_new_from_names(g_xkb_ctx, NULL, XKB_KEYMAP_COMPILE_NO_FLAGS);
    if (g_xkb_keymap) g_xkb_state = xkb_state_new(g_xkb_keymap);
    if (!g_xkb_state) fprintf(stderr, "kbweb: WARNING xkb keymap unavailable - keyboard disabled\n");
}

int main(int argc, char **argv){
    const char *url = argc > 1 ? argv[1] : "about:blank";
    if (fb_open() != 0) return 2;
    setup_vt();   // graphics mode: no fbcon cursor, no console blanking over our framebuffer
    // libwpe looks for libWPEBackend-default.so by default; the pack ships only the fdo backend, so name it explicitly.
    wpe_loader_init("libWPEBackend-fdo-1.0.so.1");
    if (!wpe_fdo_initialize_shm()) { fprintf(stderr, "kbweb: wpe_fdo_initialize_shm failed\n"); return 3; }

    g_exportable = wpe_view_backend_exportable_fdo_create(&s_client, NULL, (uint32_t)g_xres, (uint32_t)g_yres);
    g_vb = wpe_view_backend_exportable_fdo_get_view_backend(g_exportable);
    g_cx = g_xres/2; g_cy = g_yres/2;

    WebKitWebViewBackend *vbe = webkit_web_view_backend_new(g_vb, NULL, NULL);
    WebKitWebView *view = webkit_web_view_new(vbe);
    WebKitColor bg = { 0.07f, 0.086f, 0.16f, 1.0f };   // navy, matches the page, avoids a white flash
    webkit_web_view_set_background_color(view, &bg);
    webkit_web_view_load_uri(view, url);

    setup_keymap();
    setup_input();
    fprintf(stderr, "kbweb: loading %s\n", url);
    GMainLoop *loop = g_main_loop_new(NULL, FALSE);
    g_main_loop_run(loop);
    return 0;
}
