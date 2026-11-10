"""live.py - push events to open web consoles over WebSocket (RFC 6455, standard library only).
SPDX-License-Identifier: AGPL-3.0-or-later

The server calls publish({...,"mac": ...}, topic=None) when something changes. Built for hundreds of open consoles:
  - each event is encoded once and the same bytes go to every connection that may see it
  - topics: job logs ("job:<id>") and live screens ("screen:<mac>") go only to consoles that subscribed (what is on
    screen); subscriptions expire unless renewed, so hidden tabs go quiet by themselves
  - who may see a machine is cached per connection for 30 s (no database lookup per event per console)
  - each connection has its own bounded queue + sender thread: a slow browser never blocks the server; if it falls
    behind it gets one {"t":"resync"} and re-reads once
  - limits: MAX_PER_USER connections per user (the oldest closes when a new tab opens), MAX_TOTAL per server
"""
import base64
import hashlib
import json
import os
import queue
import struct
import threading

import time

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_PER_USER, MAX_TOTAL, SEE_TTL, SUB_TTL = 10, 1000, 30, 20
_clients = set()          # of Client
_lock = threading.Lock()


class Client:
    def __init__(self, sock, may_see, uid=None):
        self.sock, self.may_see, self.uid = sock, may_see, uid   # may_see(mac) -> bool (the user's machine access)
        self.q = queue.Queue(maxsize=300)
        self.alive, self.behind, self.opened = True, False, time.time()
        self.wlock = threading.Lock()                 # sender thread and pong replies share the socket
        self.subs = {}                                # topic -> expiry
        self.seen = {}                                # mac -> (may see, until)

    def send(self, data):
        with self.wlock:
            self.sock.sendall(data)

    def sees(self, mac):
        hit = self.seen.get(mac)
        now = time.time()
        if hit and hit[1] > now:
            return hit[0]
        ok = bool(self.may_see(mac))
        if len(self.seen) > 5000:
            self.seen.clear()
        self.seen[mac] = (ok, now + SEE_TTL)
        return ok

    def subscribe(self, topics, ttl=SUB_TTL):
        """Replace the subscription set (what the page shows now); each lasts ttl seconds unless renewed."""
        until = time.time() + ttl
        self.subs = {t: until for t in list(topics)[:100]}

    def wants(self, topic):
        return self.subs.get(topic, 0) > time.time()

    def offer(self, data, mac=None, topic=None):
        if topic and not self.wants(topic):
            return
        if mac and not self.sees(mac):
            return
        try:
            self.q.put_nowait(data)
        except queue.Full:                            # browser too slow: drop, it gets one resync later
            self.behind = True


def accept_key(key):
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


def frame(payload, opcode=1):
    n = len(payload)
    head = bytes([0x80 | opcode])
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + struct.pack("!H", n)
    else:
        head += bytes([127]) + struct.pack("!Q", n)
    return head + payload


def _read_frame(rfile):
    """One client frame (clients must mask). Returns (opcode, payload) or None when the connection ended."""
    h = rfile.read(2)
    if len(h) < 2:
        return None
    op, ln = h[0] & 0x0F, h[1] & 0x7F
    if ln == 126:
        ln = struct.unpack("!H", rfile.read(2))[0]
    elif ln == 127:
        ln = struct.unpack("!Q", rfile.read(8))[0]
    if ln > 65536:                                    # the page only sends pings/close: refuse anything large
        return None
    mask = rfile.read(4) if h[1] & 0x80 else b"\0\0\0\0"
    data = bytearray(rfile.read(ln))
    for i in range(len(data)):
        data[i] ^= mask[i % 4]
    return op, bytes(data)


def serve(h, may_see, on_text=None, uid=None):
    """Run one WebSocket connection on an http.server handler whose upgrade request is already authorised.
    on_text(dict, client) gets each JSON message the page sends (e.g. {"subs": [...]})."""
    key = h.headers.get("Sec-WebSocket-Key", "")
    if h.headers.get("Upgrade", "").lower() != "websocket" or not key:
        h.send_error(400)
        return
    if count() >= MAX_TOTAL:                          # full: the page falls back to polling and says so
        h.send_error(503, "too many live connections")
        return
    h.protocol_version = "HTTP/1.1"                 # browsers require an HTTP/1.1 101 (http.server defaults to 1.0)
    h.send_response(101, "Switching Protocols")
    h.send_header("Upgrade", "websocket")
    h.send_header("Connection", "Upgrade")
    h.send_header("Sec-WebSocket-Accept", accept_key(key))
    h.end_headers()
    h.wfile.flush()
    h.close_connection = True
    c = Client(h.connection, may_see, uid)
    with _lock:
        _clients.add(c)
        mine = sorted((x for x in _clients if uid is not None and x.uid == uid), key=lambda x: x.opened)
    for old in mine[:-MAX_PER_USER]:                  # a new tab: the user's oldest connection beyond the limit closes
        old.alive = False
        try:
            old.sock.shutdown(2)
        except OSError:
            pass

    def sender():
        while c.alive:
            try:
                data = c.q.get(timeout=25)
            except queue.Empty:
                data = frame(b"", 9)                  # ping keeps proxies and NAT from closing an idle socket
                if c.behind:                          # fell behind earlier: the page re-reads once
                    c.behind = False
                    data = _RESYNC
            try:
                c.send(data)
            except OSError:
                c.alive = False
    threading.Thread(target=sender, daemon=True).start()
    try:
        h.connection.settimeout(90)                   # the browser answers our pings; silence = gone
        while c.alive:
            f = _read_frame(h.rfile)
            if f is None or f[0] == 8:                # closed
                break
            if f[0] == 1 and on_text:                 # text: a small JSON request from the page
                try:
                    on_text(json.loads(f[1].decode()), c)
                except (ValueError, UnicodeDecodeError):
                    pass
            if f[0] == 9:                             # ping -> pong
                c.send(frame(f[1], 10))
    except (OSError, ValueError, struct.error):
        pass
    finally:
        c.alive = False
        with _lock:
            _clients.discard(c)
        pty_close(c)                                  # its terminals end with it


def publish(ev, topic=None):
    """One event to every console that may see it (and subscribed to topic, if given). Encoded once."""
    data = frame(json.dumps(ev, separators=(",", ":")).encode())
    mac = ev.get("mac")
    with _lock:
        cs = list(_clients)
    for c in cs:
        c.offer(data, mac, topic)


def count():
    with _lock:
        return len(_clients)


# ---- who is watching which machine's screen (the page renews every few seconds; the RAM OS streams only while watched)
_watch = {}


def watch(mac, seconds=20):
    import time
    _watch[mac] = time.time() + seconds


def watched(mac):
    import time
    return _watch.get(mac, 0) > time.time()


# ---- web console terminal: browser (admin) <-> this server <-> kbpty in the RAM OS (linux/kbpty.c)
# A session is bound to the one browser connection that opened it. Output goes straight to that connection (never
# dropped like screen frames); keystrokes are written to the machine's connection. Keystrokes are not logged.
import secrets as _secrets
import time as _time

PTY_IDLE = 1800          # seconds without a keystroke before the shell is closed
PTY_ANSWER = 45          # the RAM OS polls every 3 s: no answer within this = not in the RAM OS
_pty = {}                # sid -> {"mac", "client", "sock", "t", "opened", "on_end"}
_plock = threading.Lock()


def _push(c, ev):
    try:
        c.send(frame(json.dumps(ev, separators=(",", ":")).encode()))
    except OSError:
        c.alive = False


def pty_open(client, mac, on_end=None, kind="pty"):
    sid = _secrets.token_hex(8)
    with _plock:
        _pty[sid] = {"mac": mac, "client": client, "sock": None, "t": _time.time(), "opened": _time.time(), "on_end": on_end, "kind": kind}
    return sid


def _end(sid, why):
    with _plock:
        p = _pty.pop(sid, None)
    if not p:
        return
    if p.get("proc"):                                 # a console process on this server (e.g. IPMI serial-over-LAN)
        try:
            p["proc"].terminate()
            p["proc"].wait(timeout=5)
        except Exception:
            try:
                p["proc"].kill()
            except Exception:
                pass
        try:
            os.close(p["fd"])
        except OSError:
            pass
        if p.get("after"):                            # e.g. "sol deactivate", so the BMC frees its console
            try:
                p["after"]()
            except Exception:
                pass
    if p["sock"] and p["sock"] is not True:
        try:
            p["sock"].shutdown(2)
        except OSError:
            pass
    if p.get("kind") == "vnc":                        # noVNC: close its WebSocket
        try:
            p["client"].send(frame(struct.pack("!H", 1000), 8))
        except OSError:
            pass
        p["client"].alive = False
    else:
        _push(p["client"], {"t": "pty_end", "sid": sid, "why": why})
    if p["on_end"]:
        try:
            p["on_end"](why)
        except Exception:
            pass


def pty_pending(mac):
    """(sid, kind) of sessions waiting for this machine's RAM OS to dial in (told to it in the /agent/watch answer)."""
    now = _time.time()
    with _plock:
        late = [s for s, p in _pty.items() if p["sock"] is None and now - p["opened"] > PTY_ANSWER]
        wait = [(s, p["kind"]) for s, p in _pty.items() if p["mac"] == mac and p["sock"] is None and s not in late]
    for s in late:
        _end(s, "the machine did not answer - is it running the KwickBoot RAM OS?")
    return wait


def pty_claim(sid, mac):
    with _plock:
        p = _pty.get(sid)
        return bool(p and p["mac"] == mac and p["sock"] is None)


def pty_attach(sid, sock):
    """Runs in the HTTP thread of the RAM OS's connection after the 101: relays shell output until the end."""
    with _plock:
        p = _pty.get(sid)
        if not p or p["sock"] is not None:
            return
        p["sock"] = sock
    if p["kind"] == "pty":
        _push(p["client"], {"t": "pty_up", "sid": sid})
    sock.settimeout(30)
    why = "shell ended"
    while True:
        try:
            data = sock.recv(16384)
            if not data:
                break
            if p["kind"] == "vnc":                  # raw RFB bytes to noVNC (binary frame)
                try:
                    p["client"].send(frame(data, 2))
                except OSError:
                    p["client"].alive = False
            else:
                _push(p["client"], {"t": "pty", "sid": sid, "data": base64.b64encode(data).decode()})
            if not p["client"].alive:
                why = "browser gone"
                break
        except TimeoutError:
            if _time.time() - p["t"] > PTY_IDLE:
                why = f"closed after {PTY_IDLE // 60} min without input"
                break
            if not p["client"].alive or sid not in _pty:
                why = "browser gone"
                break
        except OSError:
            break
    _end(sid, why)


def pty_input(client, sid, data):
    p = _pty.get(sid)
    if not p or p["client"] is not client or not p["sock"] or p.get("view"):   # viewers' keystrokes go nowhere
        return
    p["t"] = _time.time()
    try:
        if p.get("proc"):
            os.write(p["fd"], data.encode("utf-8", "replace"))
        else:
            p["sock"].sendall(data.encode("utf-8", "replace").replace(b"\0", b"\0\0"))
    except OSError:
        _end(sid, "connection to the machine lost")


def pty_resize(client, sid, cols, rows):
    p = _pty.get(sid)
    if p and p["client"] is client and p["sock"] and 0 < rows < 500 and 0 < cols < 1000:
        try:
            if p.get("proc"):
                import fcntl, termios
                fcntl.ioctl(p["fd"], termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            else:
                p["sock"].sendall(b"\0R" + struct.pack("!HH", rows, cols))
        except OSError:
            pass


def pty_open_local(client, mac, argv, env, on_end=None, after=None, view=False):
    """A console process on THIS server on its own pseudo-terminal (IPMI serial-over-LAN: ipmitool sol activate),
    relayed to the browser like a RAM OS terminal. view=True: the user may only watch (input is dropped here)."""
    import pty as _ptym, subprocess as _sp
    master, slave = _ptym.openpty()
    try:
        proc = _sp.Popen(argv, stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True, start_new_session=True)
    finally:
        os.close(slave)
    sid = _secrets.token_hex(8)
    with _plock:
        _pty[sid] = {"mac": mac, "client": client, "sock": True, "t": _time.time(), "opened": _time.time(),
                     "on_end": on_end, "kind": "pty", "proc": proc, "fd": master, "after": after, "view": view}
    _push(client, {"t": "pty_open", "sid": sid, "mac": mac, "view": view})
    _push(client, {"t": "pty_up", "sid": sid})

    def relay():
        why = "console ended"
        while True:
            try:
                data = os.read(master, 16384)
            except OSError:
                break
            if not data:
                break
            _push(client, {"t": "pty", "sid": sid, "data": base64.b64encode(data).decode()})
            if not client.alive:
                why = "browser gone"
                break
        _end(sid, why)
    threading.Thread(target=relay, daemon=True).start()
    return sid


def pty_close(client, sid=None):
    """sid=None: every session of this browser connection (it closed)."""
    for s in [s for s, p in list(_pty.items()) if p["client"] is client and (sid is None or s == sid)]:
        _end(s, "closed in the web console" if sid else "browser gone")


class RfbViewOnly:
    """Browser -> VM half of an RFB (VNC) stream for a viewer who may only watch, enforced HERE, not in the page.
    Forwards the handshake (version 12 bytes + security choice 1 + ClientInit 1) and the messages needed to receive the
    picture: SetPixelFormat (0), SetEncodings (2), FramebufferUpdateRequest (3). Drops keyboard (4), pointer (5),
    clipboard (6) and QEMU's extended key events (255/0). Anything else or unparsable ends the connection."""
    def __init__(self):
        # handshake from the browser: version (12) + security type (1) + [VNC password response (16) if type 2]
        # + ClientInit (1); the length is only known once the security type byte has arrived
        self.buf, self.hs, self.typed = b"", 13, False

    def feed(self, data):
        self.buf += data
        out = b""
        while self.hs:
            n = min(self.hs, len(self.buf))
            if not n:
                break
            out, self.buf, self.hs = out + self.buf[:n], self.buf[n:], self.hs - n
            if self.hs == 0 and not self.typed:          # the security type byte is the last one just passed
                self.typed = True
                st = out[-1]
                if st == 1:
                    self.hs = 1                          # none: ClientInit follows
                elif st == 2:
                    self.hs = 17                         # VNC password: 16-byte response + ClientInit
                else:
                    return None                          # other security types are not supported for viewers
        if self.hs:
            return out
        while self.buf:
            b, t = self.buf, self.buf[0]
            if t in (0, 3, 4, 5):
                need = {0: 20, 3: 10, 4: 8, 5: 6}[t]
            elif t == 2:
                if len(b) < 4:
                    break
                need = 4 + 4 * struct.unpack("!H", b[2:4])[0]
            elif t == 6:
                if len(b) < 8:
                    break
                need = 8 + struct.unpack("!I", b[4:8])[0]
                if need > 1 << 20:
                    return None
            elif t == 255:
                if len(b) < 2:
                    break
                if b[1] != 0:                          # only QEMU's extended key event is known (and dropped)
                    return None
                need = 12
            else:
                return None
            if len(b) < need:
                break
            if t in (0, 2, 3):
                out += b[:need]
            self.buf = b[need:]
        return out


def serve_vnc(h, mac, on_end=None, view_only=False):
    """noVNC's WebSocket for the manual-install VM's screen on `mac`: raw RFB bytes both ways (authorised by the caller).
    The machine's RAM OS dials in (kbpty tcp:5900) within a few seconds of this session appearing in /agent/watch."""
    key = h.headers.get("Sec-WebSocket-Key", "")
    if h.headers.get("Upgrade", "").lower() != "websocket" or not key:
        h.send_error(400)
        return
    protos = [x.strip() for x in h.headers.get("Sec-WebSocket-Protocol", "").split(",") if x.strip()]
    h.protocol_version = "HTTP/1.1"
    h.send_response(101, "Switching Protocols")
    h.send_header("Upgrade", "websocket")
    h.send_header("Connection", "Upgrade")
    h.send_header("Sec-WebSocket-Accept", accept_key(key))
    if "binary" in protos:                            # browsers drop the connection unless a requested protocol is echoed
        h.send_header("Sec-WebSocket-Protocol", "binary")
    h.end_headers()
    h.wfile.flush()
    h.close_connection = True
    c = Client(h.connection, lambda m: True)
    sid = pty_open(c, mac, on_end, kind="vnc")
    flt = RfbViewOnly() if view_only else None
    try:
        h.connection.settimeout(None)                 # a still installer screen means a silent browser: no timeout
        while c.alive:
            f = _read_frame(h.rfile)
            if f is None or f[0] == 8:
                break
            if f[0] in (1, 2):                        # keyboard / mouse / screen requests -> the VM
                data = flt.feed(f[1]) if flt else f[1]
                if data is None:                      # a viewer sent something the filter does not allow
                    break
                p = _pty.get(sid)
                if data and p and p["sock"]:
                    p["t"] = _time.time()
                    try:
                        p["sock"].sendall(data)
                    except OSError:
                        break
            elif f[0] == 9:
                c.send(frame(f[1], 10))
    except (OSError, ValueError, struct.error):
        pass
    finally:
        c.alive = False
        _end(sid, "browser closed the screen")


def _ws_upgrade(h):
    """WebSocket handshake for noVNC (echoes the 'binary' subprotocol). Returns the Client, or None after a 400."""
    key = h.headers.get("Sec-WebSocket-Key", "")
    if h.headers.get("Upgrade", "").lower() != "websocket" or not key:
        h.send_error(400)
        return None
    protos = [x.strip() for x in h.headers.get("Sec-WebSocket-Protocol", "").split(",") if x.strip()]
    h.protocol_version = "HTTP/1.1"
    h.send_response(101, "Switching Protocols")
    h.send_header("Upgrade", "websocket")
    h.send_header("Connection", "Upgrade")
    h.send_header("Sec-WebSocket-Accept", accept_key(key))
    if "binary" in protos:
        h.send_header("Sec-WebSocket-Protocol", "binary")
    h.end_headers()
    h.wfile.flush()
    h.close_connection = True
    return Client(h.connection, lambda m: True)


def serve_vnc_direct(h, host, port, on_end=None, view_only=False):
    """noVNC <-> a VNC console the SERVER can reach directly: Intel AMT KVM (port 5900) or a BMC's VNC. Raw RFB both ways;
    the VNC password is typed in the browser (noVNC does the VNC authentication), KwickBoot never sees or stores it."""
    import socket as _so
    c = _ws_upgrade(h)
    if not c:
        return
    why = "browser closed the console"
    try:
        s = _so.create_connection((host, int(port)), timeout=10)
    except OSError as e:
        c.send(frame(struct.pack("!H", 1011) + f"cannot reach {host}:{port}".encode()[:100], 8))
        if on_end:
            on_end(f"cannot reach {host}:{port} ({e})")
        return
    s.settimeout(None)
    flt = RfbViewOnly() if view_only else None

    def from_console():
        while c.alive:
            try:
                d = s.recv(16384)
            except OSError:
                break
            if not d:
                break
            try:
                c.send(frame(d, 2))
            except OSError:
                break
        c.alive = False
        try:
            c.send(frame(struct.pack("!H", 1000), 8))
        except OSError:
            pass
    threading.Thread(target=from_console, daemon=True).start()
    try:
        h.connection.settimeout(None)
        while c.alive:
            f = _read_frame(h.rfile)
            if f is None or f[0] == 8:
                break
            if f[0] in (1, 2):
                d = flt.feed(f[1]) if flt else f[1]
                if d is None:
                    break
                if d:
                    s.sendall(d)
            elif f[0] == 9:
                c.send(frame(f[1], 10))
    except (OSError, ValueError, struct.error):
        why = "connection lost"
    finally:
        c.alive = False
        try:
            s.close()
        except OSError:
            pass
        if on_end:
            on_end(why)


_RESYNC = frame(json.dumps({"t": "resync"}).encode())     # sent once to a console that fell behind
