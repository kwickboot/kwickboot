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


def efi_signature(path):
    """What Secure Boot sees in a UEFI program: {"signed", "ca2011", "ca2023", "debian", "sbat": {component: version}}.
    Reads the PE certificate table (Authenticode PKCS#7, certificate names are plain text in it) and the .sbat section."""
    import struct
    try:
        d = open(path, "rb").read()
        pe = struct.unpack_from("<I", d, 0x3C)[0]
        if d[pe:pe + 4] != b"PE\0\0":
            return None
        ddir = pe + 24 + (112 if struct.unpack_from("<H", d, pe + 24)[0] == 0x20B else 96)
        off, size = struct.unpack_from("<II", d, ddir + 32)
        cert = d[off:off + size] if size else b""
        sbat = {}
        nsec, optsz = struct.unpack_from("<H", d, pe + 6)[0], struct.unpack_from("<H", d, pe + 20)[0]
        for i in range(nsec):
            o = pe + 24 + optsz + 40 * i
            if d[o:o + 8].rstrip(b"\0") == b".sbat":
                vsize, _va, rsize, rptr = struct.unpack_from("<IIII", d, o + 8)
                for line in d[rptr:rptr + min(vsize or rsize, rsize)].split(b"\0")[0].decode(errors="replace").splitlines():
                    f = line.split(",")
                    if len(f) >= 5 and f[0] != "sbat":
                        sbat[f[0]] = f[4]
        return {"signed": bool(cert), "ca2011": b"Microsoft Corporation UEFI CA 2011" in cert,
                "ca2023": b"Microsoft UEFI CA 2023" in cert, "debian": b"Debian Secure Boot" in cert, "sbat": sbat}
    except (OSError, struct.error):
        return None


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

    # network boot: another DHCP server handing out boot settings silently wins over KwickBoot on many PCs;
    # firmware quirks KwickBoot adapted to by itself (per-PC PXE reply layout)
    watch = getattr(S, "DHCP_WATCH", None)
    if watch is not None:
        conflicts = S.dhcp_conflicts_current()
        for c in conflicts:
            add("netboot", "bad", f"Other DHCP server {c['server']}",
                "hands out network-boot settings: " + "; ".join(c["issues"]),
                'Remove these network-boot settings from that server (router: DHCP options 66/67, next-server, boot file; Windows: WDS or the SCCM PXE point) - KwickBoot needs none of them, and many PCs otherwise boot from there or not at all.')
        if not conflicts:
            if watch.get("on"):
                add("netboot", "ok", "Other DHCP servers", "no network-boot settings seen",
                    "KwickBoot watches the DHCP replies on this network; the site's DHCP server must not hand out boot settings")
            else:
                add("netboot", "tip", "Other DHCP servers", "not watched",
                    "needs CAP_NET_RAW in pxe-toolkit.service" if watch.get("error")
                    else "off (config dhcp_watch, or KwickBoot's own DHCP is on)")
        try:
            learned = S._learned_styles()
        except Exception:
            learned = {}
        if learned:
            names = {"binl": "WDS-style", "bare": "no menu", "direct": "direct", "menu": "menu"}
            cnt = {}
            for st in learned.values():
                cnt[names.get(st, st)] = cnt.get(names.get(st, st), 0) + 1
            add("netboot", "ok", "PC firmware quirks",
                f"{len(learned)} PCs boot with an adapted PXE reply ({', '.join(f'{k} {v}' for k, v in sorted(cnt.items()))})",
                "KwickBoot adapts its PXE reply to each PC's firmware by itself - nothing to do")

    # Secure Boot chain handed to PCs: shim must carry Microsoft's UEFI CA 2011 AND 2023 signatures (PCs shipped from 2026
    # may trust only the 2023 CA; older ones only 2011), GRUB the distribution's signature with a current SBAT level
    cfg = getattr(S, "CFG", {}) or {}
    tftp = getattr(S, "TFTP_ROOT", os.path.join(S.BASE, "tftp"))
    for arch, key, dflt in (("x64", "boot_file_uefi", "debian-installer/amd64/bootnetx64.efi"),
                            ("ARM64", "boot_file_uefi_aa64", "debian-installer/arm64/bootnetaa64.efi"),
                            ("x86 (32-bit)", "boot_file_uefi_ia32", "debian-installer/i386/bootnetia32.efi")):
        f = os.path.join(tftp, cfg.get(key, dflt))
        if not os.path.isfile(f):
            continue
        sig = efi_signature(f)
        if not sig:
            add("secureboot", "warn", f"Secure Boot ({arch})", f"{os.path.basename(f)}: not a UEFI program?", "re-run the KwickBoot update")
            continue
        grub = efi_signature(os.path.join(os.path.dirname(f), "grub" + os.path.basename(f)[7:].replace("net", ""))) \
            if os.path.basename(f).startswith("bootnet") else None
        ver = f"shim {sig['sbat'].get('shim.debian') or sig['sbat'].get('shim', '?')}" + (
            f", GRUB {grub['sbat'].get('grub.debian') or grub['sbat'].get('grub', '?')}" if grub else "")
        cas = " + ".join(n for n, k in (("Microsoft UEFI CA 2011", "ca2011"), ("2023", "ca2023")) if sig[k]) or "no Microsoft signature"
        if not sig["signed"]:
            add("secureboot", "bad", f"Secure Boot ({arch})", f"{ver} - UNSIGNED",
                "PCs with Secure Boot on refuse it: restore the distribution's signed shim (shim-signed)")
        elif sig["ca2011"] and sig["ca2023"]:
            add("secureboot", "ok", f"Secure Boot ({arch})", f"{ver} - signed by {cas}",
                "boots with Secure Boot on, on older PCs and on new ones that only trust the 2023 certificate")
        else:
            add("secureboot", "warn", f"Secure Boot ({arch})", f"{ver} - signed by {cas} only",
                "PCs that only trust the " + ("2023" if sig["ca2011"] else "2011") + " Microsoft certificate refuse it with "
                "Secure Boot on: update to a shim signed with both (Debian shim-signed 16 or later)")
        if grub and not grub["signed"]:
            add("secureboot", "bad", f"Secure Boot ({arch}) GRUB", "GRUB is unsigned", "shim refuses it with Secure Boot on: restore the signed grub")
    ipxe = os.path.join(tftp, "ipxe", "ipxe.efi")
    if os.path.isfile(ipxe):
        sig = efi_signature(ipxe)
        if sig and not sig["signed"]:
            add("secureboot", "tip", "Secure Boot (iPXE)", "ipxe.efi is unsigned",
                "used only for iPXE ROMs without HTTP and machines set to chain iPXE: those need Secure Boot off")

    # uptime
    up = float(_read("/proc/uptime", "0").split()[0] or 0)
    add("system", "ok", "Uptime", f"{up / 86400:.1f} days")
    return {"items": items, "at": time.time()}
