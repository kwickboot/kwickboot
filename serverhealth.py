"""serverhealth.py - the KwickBoot server's own health and tuning advice (admin "Server health" page).
SPDX-License-Identifier: AGPL-3.0-or-later

check(S) -> {"items": [{"area", "level": ok|tip|warn|bad, "name", "value", "advice"}], "at"}
Read-only: /proc, /sys, statvfs, timedatectl. measure_disk(path) reads up to 256 MB of the largest image once
(O_DIRECT where the file system allows it) to estimate the image disk's speed - the ceiling for every install.
"""
import os
import re
import subprocess
import time

_last_disk = {}          # path -> (MB/s, when)


def _read(p, default=""):
    try:
        return open(p).read().strip()
    except OSError:
        return default


def _largest_file(root, exts=(".wim", ".iso", ".img", ".zst", ".xz")):
    best, size = None, 0
    for dp, _dn, fn in os.walk(root):
        for f in fn:
            if f.lower().endswith(exts):
                try:
                    s = os.path.getsize(os.path.join(dp, f))
                except OSError:
                    continue
                if s > size:
                    best, size = os.path.join(dp, f), s
    return best


def measure_disk(root):
    """MB/s reading the largest image under root (first 256 MB). Cached result kept for the page."""
    f = _largest_file(root)
    if not f:
        return None
    n, chunk = 0, 4 * 1024 * 1024
    flags = os.O_RDONLY | getattr(os, "O_DIRECT", 0)
    try:
        fd = os.open(f, flags)
    except OSError:
        fd = os.open(f, os.O_RDONLY)             # no O_DIRECT on this file system: page cache may flatter the number
    try:
        import mmap
        buf = mmap.mmap(-1, chunk)                # aligned buffer (O_DIRECT needs one)
        t = time.time()
        while n < 256 * 1024 * 1024:
            try:
                got = os.readv(fd, [buf])
            except OSError:                       # O_DIRECT refused by this file system: plain reads
                os.close(fd)
                fd = os.open(f, os.O_RDONLY)
                os.lseek(fd, n, 0)
                got = os.readv(fd, [buf])
            if got <= 0:
                break
            n += got
        dt = max(time.time() - t, 0.001)
    finally:
        os.close(fd)
    mbs = n / 1e6 / dt
    _last_disk[root] = (round(mbs), time.time())
    return round(mbs)


def check(S):
    items = []
    add = lambda area, level, name, value, advice="": items.append(  # noqa: E731
        {"area": area, "level": level, "name": name, "value": value, "advice": advice})

    # CPU + load
    cpu = re.search(r"model name\s*:\s*(.+)", _read("/proc/cpuinfo"))
    ncpu = os.cpu_count() or 1
    load = [float(x) for x in _read("/proc/loadavg", "0 0 0").split()[:3]]
    lvl = "warn" if load[1] > ncpu * 0.8 else "ok"
    add("cpu", lvl, "CPU", f"{cpu.group(1).strip() if cpu else '?'} - {ncpu} threads, load {load[0]:.2f} / {load[1]:.2f} / {load[2]:.2f}",
        "the server is busy: heavy work (image conversion, captures) belongs on a client PC, which KwickBoot already does"
        if lvl == "warn" else "")

    # memory
    mi = dict(re.findall(r"(\w+):\s+(\d+)", _read("/proc/meminfo")))
    tot, av = int(mi.get("MemTotal", 0)) / 1048576, int(mi.get("MemAvailable", 0)) / 1048576
    add("memory", "warn" if av < 0.5 else "ok", "Memory", f"{av:.1f} GB free of {tot:.1f} GB",
        "under 0.5 GB free: close other services on this server or add RAM" if av < 0.5 else "")

    # disks: system, ISO library, image library (+ driver library when configured elsewhere)
    seen = set()
    for label, path in (("System disk", S.BASE), ("ISO library", os.path.join(S.HTTP_ROOT, "iso")),
                        ("Image library", os.path.join(S.HTTP_ROOT, "images"))):
        try:
            st = os.statvfs(path)
        except OSError:
            continue
        key = (st.f_blocks, st.f_bsize, st.f_fsid if hasattr(st, "f_fsid") else path)
        if key in seen:
            continue
        seen.add(key)
        free, size = st.f_bavail * st.f_frsize / 1e9, st.f_blocks * st.f_frsize / 1e9
        pct = free / size * 100 if size else 0
        level = "bad" if pct < 5 else "warn" if pct < 15 else "ok"
        add("disk", level, label, f"{free:.0f} GB free of {size:.0f} GB ({pct:.0f}%)",
            "almost full: remove old images/ISOs (pxectl iso rm, captures) or move the library to a bigger disk" if level != "ok" else "")

    # image disk speed (measured on request)
    img = os.path.join(S.HTTP_ROOT, "images")
    iso = os.path.join(S.HTTP_ROOT, "iso")
    m = _last_disk.get(iso) or _last_disk.get(img)
    if m:
        mbs = m[0]
        level = "warn" if mbs < 80 else "ok"
        add("disk", level, "Image disk read speed", f"{mbs} MB/s (measured {time.strftime('%H:%M', time.localtime(m[1]))})",
            "slower than a gigabit network (~110 MB/s): every install waits on the disk - use a USB 3 port (blue) or an SSD"
            if level == "warn" else "fast enough to fill a gigabit network")
    else:
        add("disk", "tip", "Image disk read speed", "not measured yet", "press 'Measure' (reads 256 MB of the largest image)")

    # network: link speed, duplex, MTU, multicast budget
    iface = getattr(S, "IFACE", None)
    if iface:
        n = f"/sys/class/net/{iface}"
        sp = int(_read(n + "/speed", "0") or 0)
        dup = _read(n + "/duplex", "?")
        mtu = _read(n + "/mtu", "?")
        level = "bad" if 0 < sp < 1000 else "ok"
        add("network", level, f"Network ({iface})", f"{sp or '?'} Mb/s, {dup} duplex, MTU {mtu}",
            "the server links below gigabit: check the cable and the switch port - this caps every install and multicast"
            if level == "bad" else ("half duplex: set the switch port to auto-negotiate" if dup == "half" else ""))
        if sp >= 1000:
            add("network", "ok", "Multicast rate", "up to 900 Mb/s per session (udp-sender --max-bitrate 900m)",
                "leaves ~10% of the link for the console and other traffic; receivers set the real pace")
        if sp >= 2500:
            add("network", "tip", "Multicast rate", f"the link is {sp} Mb/s - multicast is capped at 900 Mb/s",
                "a 2.5/10 GbE server can send faster: raise the cap in multicast.py (--max-bitrate)")

    # time sync (certificates, logs, schedules depend on it)
    try:
        ok = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"], capture_output=True, text=True,
                            timeout=5).stdout.strip()
        add("time", "ok" if ok == "yes" else "warn", "Time sync", "synchronized" if ok == "yes" else "NOT synchronized",
            "" if ok == "yes" else "enable NTP (timedatectl set-ntp true): wipe certificates, logs and schedules need the right time")
    except (OSError, subprocess.TimeoutExpired):
        pass

    # package cache (faster Linux installs, survives internet hiccups)
    cache = subprocess.run(["systemctl", "is-active", "apt-cacher-ng"], capture_output=True, text=True).stdout.strip() \
        if os.path.exists("/bin/systemctl") or os.path.exists("/usr/bin/systemctl") else ""
    add("cache", "ok" if cache == "active" else "tip", "Package cache", "running" if cache == "active" else "not running",
        "" if cache == "active" else "install with --cache (apt-cacher-ng): Debian/Ubuntu installs then fetch packages at LAN speed")

    # uptime
    up = float(_read("/proc/uptime", "0").split()[0] or 0)
    add("system", "ok", "Uptime", f"{up / 86400:.1f} days")
    return {"items": items, "at": time.time()}
