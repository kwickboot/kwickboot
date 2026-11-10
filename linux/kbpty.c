/* kbpty - a shell of the KwickBoot RAM OS in the web console's terminal (admins only, opened from the web console).
 * SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * kbpty <server-ip> <port> <mac> <token> <session-id> [tcp:<local-port> | put:<file>]
 *   (no mode) a shell on a pseudo-terminal; tcp: relay a local port (VNC); put: send stdin to the server (image capture)
 * Dials OUT to the KwickBoot server (the RAM OS opens no port): "GET /agent/pty" with the machine's per-boot token on the
 * normal HTTP port, waits for "101", then relays bytes between that connection and a login shell on a real
 * pseudo-terminal (line editing, Ctrl+C, top, vi work). Ends when either side closes; the shell gets SIGHUP.
 *
 * Server -> kbpty: keystrokes; byte 0x00 is an escape: 0x00 0x00 = a real NUL, 0x00 'R' r1 r0 c1 c0 = window size
 * (rows, columns, big-endian). kbpty -> server: raw terminal output.
 */
#include <errno.h>
#include <arpa/inet.h>
#include <netinet/in.h>
#include <poll.h>
#include <pty.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>

static int write_all(int fd, const char *p, size_t n) {
    while (n) {
        ssize_t w = write(fd, p, n);
        if (w < 0) { if (errno == EINTR) continue; return -1; }
        p += w; n -= (size_t)w;
    }
    return 0;
}

static int ok_arg(const char *s) {          /* nothing that could break the request line */
    if (!*s || strlen(s) > 128) return 0;
    for (; *s; s++) if (!(('a' <= *s && *s <= 'z') || ('A' <= *s && *s <= 'Z') || ('0' <= *s && *s <= '9') || strchr(".:-_", *s))) return 0;
    return 1;
}

int main(int argc, char **argv) {
    if (argc != 6 && argc != 7) { fprintf(stderr, "usage: kbpty <server> <port> <mac> <token> <session> [tcp:<local-port>]\n"); return 2; }
    for (int i = 1; i < argc; i++) if (!ok_arg(argv[i])) { fprintf(stderr, "kbpty: bad argument %d\n", i); return 2; }
    signal(SIGPIPE, SIG_IGN);

    /* tcp mode: the local port first - if nothing listens yet (VM still starting), exit before the server sees anything */
    int l = -1;
    const char *put = (argc == 7 && strncmp(argv[6], "put:", 4) == 0) ? argv[6] + 4 : NULL;   /* stdin -> server file */
    if (argc == 7 && !put) {
        if (strncmp(argv[6], "tcp:", 4) != 0) return 2;
        struct sockaddr_in la = {0};
        la.sin_family = AF_INET;
        la.sin_port = htons((unsigned short)atoi(argv[6] + 4));
        la.sin_addr.s_addr = htonl(INADDR_LOOPBACK);           /* only this machine's own port */
        l = socket(AF_INET, SOCK_STREAM, 0);
        if (l < 0 || connect(l, (struct sockaddr *)&la, sizeof la) != 0) return 9;
    }

    /* the server is always given as an IPv4 address: no resolver (and no NSS libraries in a static binary) */
    struct sockaddr_in sa = {0};
    sa.sin_family = AF_INET;
    sa.sin_port = htons((unsigned short)atoi(argv[2]));
    if (inet_pton(AF_INET, argv[1], &sa.sin_addr) != 1) return 3;
    int s = socket(AF_INET, SOCK_STREAM, 0);
    if (s < 0 || connect(s, (struct sockaddr *)&sa, sizeof sa) != 0) return 4;

    char buf[8192];
    int n = put
        ? snprintf(buf, sizeof buf, "POST /agent/upload?mac=%s&job=%s&file=%s HTTP/1.1\r\nHost: %s:%s\r\nX-PXE-Token: %s\r\n"
                   "Connection: Upgrade\r\nUpgrade: kbpty\r\n\r\n", argv[3], argv[5], put, argv[1], argv[2], argv[4])
        : snprintf(buf, sizeof buf, "GET /agent/pty?mac=%s&sid=%s HTTP/1.1\r\nHost: %s:%s\r\nX-PXE-Token: %s\r\n"
                   "Connection: Upgrade\r\nUpgrade: kbpty\r\n\r\n", argv[3], argv[5], argv[1], argv[2], argv[4]);
    if (write_all(s, buf, (size_t)n)) return 5;
    /* response head, byte by byte: nothing after the blank line may be lost */
    size_t h = 0;
    while (h < sizeof buf - 1) {
        if (read(s, buf + h, 1) != 1) return 6;
        h++;
        if (h >= 4 && memcmp(buf + h - 4, "\r\n\r\n", 4) == 0) break;
    }
    buf[h] = 0;
    if (strncmp(buf, "HTTP/1.1 101", 12) != 0 && strncmp(buf, "HTTP/1.0 101", 12) != 0) return 7;

    if (put) {                /* put:<file> - stdin to the server (image capture), then its verdict "OK <sha256> <bytes>" */
        for (;;) {
            ssize_t r = read(0, buf, sizeof buf);
            if (r < 0 && errno == EINTR) continue;
            if (r <= 0) break;
            if (write_all(s, buf, (size_t)r)) return 11;
        }
        shutdown(s, SHUT_WR);                                   /* end of data: the server finishes and answers */
        size_t got = 0;
        ssize_t r;
        while (got < sizeof buf - 1 && (r = read(s, buf + got, sizeof buf - 1 - got)) > 0) got += (size_t)r;
        buf[got] = 0;
        fputs(buf, stdout);
        close(s);
        return strncmp(buf, "OK ", 3) == 0 ? 0 : 10;
    }
    if (l >= 0) {             /* tcp:<port> - raw relay to the local port (the manual-install VM's VNC screen), no escapes */
        struct pollfd q[2] = {{s, POLLIN, 0}, {l, POLLIN, 0}};
        for (;;) {
            if (poll(q, 2, -1) < 0) { if (errno == EINTR) continue; break; }
            int done = 0;
            for (int k = 0; k < 2 && !done; k++)
                if (q[k].revents & (POLLIN | POLLHUP | POLLERR)) {
                    ssize_t r = read(q[k].fd, buf, sizeof buf);
                    if (r <= 0 || write_all(q[1 - k].fd, buf, (size_t)r)) done = 1;
                }
            if (done) break;
        }
        close(l); close(s);
        return 0;
    }

    struct winsize ws = {24, 80, 0, 0};
    int m;
    pid_t pid = forkpty(&m, NULL, NULL, &ws);
    if (pid < 0) return 8;
    if (pid == 0) {
        setenv("TERM", "xterm-256color", 1);
        setenv("HOME", "/root", 1);
        setenv("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", 1);
        chdir("/root");
        execl("/bin/sh", "-sh", (char *)NULL);
        _exit(127);
    }

    /* escape parser state for server -> pty */
    int esc = 0, wantsz = 0; unsigned char sz[4];
    struct pollfd p[2] = {{s, POLLIN, 0}, {m, POLLIN, 0}};
    for (;;) {
        if (poll(p, 2, -1) < 0) { if (errno == EINTR) continue; break; }
        if (p[1].revents & (POLLIN | POLLHUP | POLLERR)) {          /* shell output -> server */
            ssize_t r = read(m, buf, sizeof buf);
            if (r <= 0 || write_all(s, buf, (size_t)r)) break;
        }
        if (p[0].revents & (POLLIN | POLLHUP | POLLERR)) {          /* keystrokes / resize -> shell */
            unsigned char in[4096], out[4096];
            ssize_t r = read(s, in, sizeof in);
            if (r <= 0) break;
            size_t o = 0;
            for (ssize_t i = 0; i < r; i++) {
                unsigned char c = in[i];
                if (wantsz) {
                    sz[4 - wantsz] = c;
                    if (--wantsz == 0) {
                        ws.ws_row = (unsigned short)(sz[0] << 8 | sz[1]); ws.ws_col = (unsigned short)(sz[2] << 8 | sz[3]);
                        if (ws.ws_row > 0 && ws.ws_col > 0 && ws.ws_row < 500 && ws.ws_col < 1000) ioctl(m, TIOCSWINSZ, &ws);
                    }
                } else if (esc) {
                    esc = 0;
                    if (c == 'R') wantsz = 4; else out[o++] = 0;           /* 0x00 0x00 = a NUL keystroke */
                } else if (c == 0) esc = 1;
                else out[o++] = c;
            }
            if (o && write_all(m, (char *)out, o)) break;
        }
    }
    close(s);
    close(m);
    kill(pid, SIGHUP);
    waitpid(pid, NULL, 0);
    return 0;
}
