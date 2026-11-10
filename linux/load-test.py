#!/usr/bin/python3
"""load-test.py - how many open web consoles can one KwickBoot server keep live? (standard library only)
SPDX-License-Identifier: AGPL-3.0-or-later

Opens N console WebSockets (spread over test users: the server allows 10 per user), then changes machines at a set rate
through the admin API - each change carries its send time in the machine name ("lt-<ms>") - and measures how long every
change takes to reach every console. Prints delivery (received / expected) and delay percentiles.

  python3 load-test.py --url https://10.199.0.2:9443 --admin-cookie <kbsess> --macs aa:..,bb:.. --consoles 200 --rate 10 --secs 30
Test servers only: it creates users "loadtest-<run>-NN" (role admin) and renames the given machines.
"""
import argparse
import base64
import json
import os
import socket
import ssl
import struct
import threading
import time
import urllib.parse
import urllib.request

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE                       # test server, self-signed


def http(url, method="GET", body=None, cookie=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Content-Type", "application/json")
    o = urllib.parse.urlparse(url)
    req.add_header("Origin", f"{o.scheme}://{o.netloc}")
    if cookie:
        req.add_header("Cookie", f"kbsess={cookie}")
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=20) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers
    except (OSError, urllib.error.URLError) as e:     # reset / refused: counted, not fatal
        return 0, str(e).encode(), None


def login(base, user, pw):
    st, body, hdr = http(base + "/login", "POST", {"username": user, "password": pw})
    ck = hdr.get("Set-Cookie", "") if hdr else ""
    return ck.split("kbsess=")[1].split(";")[0] if "kbsess=" in ck else None


class Console(threading.Thread):
    def __init__(self, base, cookie, stats):
        super().__init__(daemon=True)
        self.base, self.cookie, self.stats = base, cookie, stats
        self.ok = False

    def run(self):
        o = urllib.parse.urlparse(self.base)
        try:
            raw = socket.create_connection((o.hostname, o.port or 443), timeout=30)
            s = CTX.wrap_socket(raw, server_hostname=o.hostname)
            key = base64.b64encode(os.urandom(16)).decode()
            s.sendall((f"GET /api/ws HTTP/1.1\r\nHost: {o.netloc}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                       f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nOrigin: {o.scheme}://{o.netloc}\r\n"
                       f"Cookie: kbsess={self.cookie}\r\n\r\n").encode())
            head = b""
            while b"\r\n\r\n" not in head:
                c = s.recv(1)
                if not c:
                    return
                head += c
            if b" 101 " not in head.split(b"\r\n")[0]:
                self.stats["refused"] += 1
                return
            self.ok = True
            self.stats["open"] += 1
            s.settimeout(None)
            f = s.makefile("rb")
            while True:
                h = f.read(2)
                if len(h) < 2:
                    break
                op, ln = h[0] & 0x0F, h[1] & 0x7F
                if ln == 126:
                    ln = struct.unpack("!H", f.read(2))[0]
                elif ln == 127:
                    ln = struct.unpack("!Q", f.read(8))[0]
                data = f.read(ln)
                if op == 9:                           # ping -> pong (client frames are masked)
                    mask = os.urandom(4)
                    s.sendall(bytes([0x8A, 0x80 | len(data)]) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
                    continue
                if op != 1:
                    continue
                now = time.time() * 1000
                try:
                    ev = json.loads(data)
                except ValueError:
                    continue
                self.stats["events"] += 1
                if ev.get("t") == "m" and str(ev.get("s", {}).get("name", "")).startswith("lt-"):
                    sent = int(ev["s"]["name"][3:])
                    with LOCK:
                        self.stats["delays"].append(now - sent)
        except OSError:
            pass
        finally:
            if self.ok:
                self.stats["open"] -= 1
                self.stats["closed"] += 1


LOCK = threading.Lock()


def pct(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, int(len(v) * p / 100))] if v else float("nan")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--admin-cookie", required=True)
    ap.add_argument("--macs", required=True)
    ap.add_argument("--consoles", type=int, default=200)
    ap.add_argument("--rate", type=float, default=10)
    ap.add_argument("--secs", type=int, default=30)
    a = ap.parse_args()
    base, admin = a.url.rstrip("/"), a.admin_cookie
    macs = [m for m in a.macs.split(",") if m]
    users = (a.consoles + 9) // 10
    pw = "loadtest-" + base64.b32encode(os.urandom(10)).decode().lower()
    cookies, run = [], base64.b32encode(os.urandom(3)).decode().lower()[:4]
    for i in range(users):                            # test users (10 consoles each), new names each run
        name = f"loadtest-{run}-{i:02d}"
        http(base + "/api/admin/users", "POST", {"op": "create", "username": name, "role": "admin", "password": pw, "departments": []}, admin)
        ck = login(base, name, pw)
        if not ck:
            print(f"login failed for {name}")
            return
        cookies.append(ck)
    stats = {"open": 0, "refused": 0, "closed": 0, "events": 0, "delays": []}
    t0 = time.time()
    cons = [Console(base, cookies[i // 10], stats) for i in range(a.consoles)]
    for c in cons:
        c.start()
    while stats["open"] + stats["refused"] < a.consoles and time.time() - t0 < 60:
        time.sleep(0.2)
    print(f"consoles open: {stats['open']}/{a.consoles} in {time.time() - t0:.1f}s (refused {stats['refused']})")
    time.sleep(2)
    sent = 0
    end = time.time() + a.secs
    while time.time() < end:                          # machine changes, each stamped with its send time
        mac = macs[sent % len(macs)]
        st, _, _ = http(base + "/api/action", "POST", {"mac": mac, "action": "approve", "name": f"lt-{int(time.time() * 1000)}"}, admin)
        if st == 200:
            sent += 1
        else:
            stats["api_errors"] = stats.get("api_errors", 0) + 1
        time.sleep(max(0, 1 / a.rate))
    time.sleep(5)                                     # let the last ones arrive
    d = stats["delays"]
    exp = sent * stats["open"]
    print(f"changes sent: {sent} at ~{a.rate}/s for {a.secs}s (API errors {stats.get('api_errors', 0)}); consoles still open: {stats['open']}")
    print(f"delivered: {len(d)}/{exp} ({100 * len(d) / exp if exp else 0:.1f} %)")
    print(f"delay ms: p50 {pct(d, 50):.0f}  p95 {pct(d, 95):.0f}  p99 {pct(d, 99):.0f}  max {max(d) if d else float('nan'):.0f}")


if __name__ == "__main__":
    main()
