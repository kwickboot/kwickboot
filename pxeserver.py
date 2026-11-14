"""PXE toolkit server: proxyDHCP + TFTP + HTTP (Linux or Windows, Python 3.10+).

* proxyDHCP never hands out IP addresses - the normal router keeps doing DHCP.
  It only adds PXE boot info for KNOWN machines, or for ANY machine while a
  discovery window is open (`pxectl discover 30`). New machines are auto-registered
  and always boot the RAM OS first (never touches disks).
* Every boot is decided by the server - no console selection needed:
    machines/<mac>/next     one-shot action for the next boot (then back to default)
    default (config/registry)  persistent action: alpine | local | menu
  Actions: alpine, local, menu, debian13, memtest, netbootxyz, shell, iso:<file>, windows
* Boot paths:  UEFI -> signed shim -> GRUB -> per-MAC config generated on the fly (TFTP)
               iPXE (ours, embedded script) -> GET /boot.ipxe?mac=...  generated on the fly
               legacy BIOS -> ipxe/undionly.kpxe -> /boot.ipxe
* Templates: tftp/**/*.tmpl rendered at start; HTTP *.cfg/*.sh/*.txt/*.ipxe rendered per request.
"""
import functools
import hashlib
import hmac
import json
import os
import re
import socket
import struct
import sys
import threading
import time
import urllib.parse
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import notify

import db
import endpoint
import certs              # secure-wipe certificates (Ed25519-signed)
import federation
import health
import insights             # fleet-learning knowledge base (opt-in, anonymised) - the data network-effect moat
import live                 # push to open web consoles (WebSocket)
import netbar               # netbar / diskless-seat session management (members, prepaid time, billing)
import homes                # persistent per-member home for diskless seats
import signage              # digital signage / lab orchestration (content, playlists, zones, schedules, exam mode)
import san                  # KwickBoot as the SAN: OS library served as iSCSI LUNs (iPXE sanboot)
import webui

BASE = os.path.dirname(os.path.abspath(__file__))
try:                      # open files: raise the soft limit to the hard one (systemd's soft default 1024 is too low for
    import resource       # hundreds of live consoles: a socket + database files each); also covers older unit files
    _soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(_hard, 65536) if _hard != resource.RLIM_INFINITY else 65536, _hard))
except (ImportError, ValueError, OSError):
    pass
CFG = json.load(open(os.path.join(BASE, "config.json"), encoding="utf-8-sig"))  # -sig: tolerate BOM
TFTP_ROOT = os.path.realpath(os.path.join(BASE, "tftp"))
HTTP_ROOT = os.path.realpath(os.path.join(BASE, "http"))
STATE = os.path.join(BASE, "machines")            # writable by the service user
# the service log lives in RAM when systemd gives us a runtime directory (RuntimeDirectory=kwickboot-logs: kept across
# service restarts, gone at reboot) - small, rotated daily, capped. Boot history and job logs are kept elsewhere
# (database, machines/<mac>/jobs). config "log_dir" overrides.
LOG_DIR = CFG.get("log_dir") or os.environ.get("RUNTIME_DIRECTORY", "").split(":")[0] or os.path.join(BASE, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(STATE, exist_ok=True)
LOG_DAYS, LOG_CAP = int(CFG.get("log_days", 7)), int(CFG.get("log_cap_mb", 64)) * 1048576
UNDER_SYSTEMD = bool(os.environ.get("INVOCATION_ID"))      # journald would keep a second copy of every line
_LOG_DAY = [""]
LOG = os.path.join(LOG_DIR, time.strftime("pxe-%Y%m%d.log"))
HTTP_PORT = int(CFG.get("http_port", 8080))
TEMPLATED_EXT = (".cfg", ".sh", ".txt", ".ipxe", ".yaml")
ONE_SHOT_WINDOW = 180      # seconds a one-shot action stays valid after first use (multi-stage loaders)
ACTIONS = ("alpine", "local", "menu", "debian13", "memtest", "netbootxyz", "shell", "windows", "seat")    # + "tool:<name>" (OS library tools)
DEFAULT_ACTIONS = ("alpine", "local", "menu", "seat")   # persistent: RAM OS, installed OS, menu, diskless gaming seat
LOCK = threading.Lock()


def _log_rotate():
    """New day: keep the newest LOG_DAYS files and at most LOG_CAP bytes in all (oldest go first)."""
    try:
        fs = sorted((f for f in os.listdir(LOG_DIR) if f.startswith("pxe-") and f.endswith(".log")), reverse=True)
        total = 0
        for i, f in enumerate(fs):
            p = os.path.join(LOG_DIR, f)
            total += os.path.getsize(p)
            if i >= LOG_DAYS or (i and total > LOG_CAP):
                os.remove(p)
    except OSError:
        pass


def log(msg):
    global LOG
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    if not UNDER_SYSTEMD:
        print(line, flush=True)
    day = time.strftime("%Y%m%d")
    if day != _LOG_DAY[0]:
        _LOG_DAY[0] = day
        LOG = os.path.join(LOG_DIR, f"pxe-{day}.log")
        _log_rotate()
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------------------------------------------------------------- network identity
def detect_ip():
    if CFG.get("server_ip", "auto") != "auto":
        return CFG["server_ip"]
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("1.1.1.1", 53))       # no packet sent; picks the LAN interface
        return s.getsockname()[0]
    finally:
        s.close()


MY_IP = detect_ip()
IS_LINUX = sys.platform.startswith("linux")


def iface_for_ip(ip):
    try:
        import subprocess
        out = subprocess.run(["ip", "-o", "-4", "addr"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            p = line.split()
            if len(p) > 3 and p[3].split("/")[0] == ip:
                return p[1]
    except Exception:
        pass
    return None


IFACE = iface_for_ip(MY_IP) if IS_LINUX else None


# ---------------------------------------------------------------- machine state
def norm_mac(m):
    return (m or "").strip().lower().replace("-", ":")


def mac_dir(mac):
    d = os.path.join(STATE, norm_mac(mac).replace(":", "-"))
    os.makedirs(d, exist_ok=True)
    return d


def machine(mac):
    """Machine record from the database (None if unknown). Adds 'approved' as bool and 'default' for callers."""
    m = db.machine(norm_mac(mac))
    if m:
        m["approved"] = bool(m["approved"])
        m["default"] = m["default_action"]
    return m


def set_meta(mac, **kw):
    if "approved" in kw:
        kw["approved"] = 1 if kw["approved"] else 0
    db.upsert_machine(norm_mac(mac), **kw)


def discovery_open():
    try:
        v = db.get_setting("discovery_until", "0") or "0"
        if v == "always":   # always-open: answer every PXE client; unknown ones get the RAM OS and wait for approval
            return True
        return float(v) > time.time()
    except ValueError:
        return False


def register_new(mac):
    with LOCK:
        if not db.machine(mac):
            name = "new-" + mac.replace(":", "")[-4:]
            db.upsert_machine(mac, name=name, approved=0, default_action="alpine")
            db.audit("system", "discovered", f"{mac} registered as {name}")
            log(f"[discovery] NEW machine {mac} registered as {name} (RAM OS only until approved)")
    return machine(mac)


def known_or_discover(mac):
    m = machine(mac)
    if m:
        return m
    if discovery_open():
        return register_new(mac)
    return None


def default_action(mac):
    m = db.machine(norm_mac(mac))
    v = (m or {}).get("default_action") or "alpine"
    return v if v in DEFAULT_ACTIONS else "alpine"


def set_default(mac, action):
    db.upsert_machine(norm_mac(mac), default_action=action)
    log(f"[intent] {mac}: default -> {action}")


TOOLS_DIR = os.path.join(HTTP_ROOT, "images", "tools")


def tool_info(name):
    """An OS library tool (images/tools/<name>/: vmlinuz, initrd.img, tool.info KEY=VALUE) or None."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", name or ""):
        return None
    d = os.path.join(TOOLS_DIR, name)
    if not os.path.isfile(os.path.join(d, "tool.info")):
        return None
    info = {"NAME": name}
    for line in open(os.path.join(d, "tool.info"), encoding="utf-8", errors="replace"):
        k, _, v = line.rstrip("\n").partition("=")
        if k in ("TITLE", "DESC", "ROLE", "CMDLINE", "KERNEL", "BUILT", "BIOS_ONLY", "UEFI_ONLY", "KIND"):
            info[k] = v
    # KIND: kernel (vmlinuz + initrd.img, the default) or efi (one UEFI program: boot.efi, e.g. the EFI Shell)
    files = ("boot.efi",) if info.get("KIND") == "efi" else ("vmlinuz", "initrd.img")
    if not all(os.path.isfile(os.path.join(d, f)) for f in files):
        return None
    if re.search(r"[^A-Za-z0-9_=. :/-]", info.get("CMDLINE", "")):   # only plain kernel options reach the boot loader
        info["CMDLINE"] = ""
    info["SIZE"] = sum(os.path.getsize(os.path.join(d, f)) for f in files)
    return info


def tools():
    try:
        return [t for t in (tool_info(n) for n in sorted(os.listdir(TOOLS_DIR))) if t]
    except OSError:
        return []


def set_next(mac, action):
    db.upsert_machine(norm_mac(mac), next_action=action, next_used_at=None)


def current_action(mac, consume=True):
    """One-shot `next` (valid ONE_SHOT_WINDOW s after first use), else persistent default."""
    m = machine(mac) or {}
    action = m.get("next_action")
    if action:
        used = m.get("next_used_at")
        if used:
            if time.time() - used > ONE_SHOT_WINDOW:
                db.upsert_machine(mac, next_action=None, next_used_at=None)
                log(f"[intent] {mac}: one-shot '{action}' expired -> default")
                return default_action(mac)
        elif consume:
            db.upsert_machine(mac, next_used_at=time.time())
            log(f"[intent] {mac}: one-shot '{action}' in use")
        destructive = action.startswith(("debian13", "truenas:", "tool:")) or action == "windows"   # tools edit / wipe disks
        if destructive and not m.get("approved"):
            log(f"[intent] {mac}: '{action}' REFUSED - machine not approved; booting RAM OS")
            return "alpine"
        return action
    return default_action(mac)


# ---------------------------------------------------------------- per-boot agent token
def issue_token(mac):
    """New random token each time a machine is sent the RAM OS; passed on its kernel cmdline."""
    import secrets
    tok = secrets.token_hex(16)
    db.upsert_machine(norm_mac(mac), token=tok)
    return tok


def token_ok(mac, presented):
    import hmac
    expected = (db.machine(norm_mac(mac)) or {}).get("token")
    if not expected:
        return not CFG.get("require_agent_token", True)
    return bool(presented) and hmac.compare_digest(expected, presented)


# ---------------------------------------------------------------- jobs (RAM OS agent)
JOB_TYPES = ("inventory", "preflight", "install", "manual", "capture", "restore", "wipe", "reboot", "poweroff", "winprep", "stress")
WIN_EDITIONS = ("Professional", "Education", "Enterprise", "Core")    # Edition IDs in install.wim (pro, education, ...)
SAFE_VAL = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_./:+=-")


def jobs_dir(mac):
    """Job LOG files live here (job records are in the database)."""
    d = os.path.join(mac_dir(mac), "jobs")
    os.makedirs(d, exist_ok=True)
    return d


def next_job(mac):
    """Oldest queued job -> running. Returns shell-safe KEY='value' text for the agent, or '{}'."""
    while True:
        job = db.take_next_job(mac)
        if not job:
            return "{}"
        params = job.get("params", {})
        err = None
        if job["type"] not in JOB_TYPES:
            err = "job type not allowed"
        elif job["type"] == "install" and not (machine(mac) or {}).get("approved"):
            err = "machine not approved for installs"
        elif any(not set(str(v)) <= SAFE_VAL for v in params.values()):
            err = "unsafe characters in parameters"
        if err:
            db.fail_job(job["id"], err)
            log(f"[job] {mac}: rejected {job['id']}: {err}")
            continue
        log(f"[job] {mac}: started {job['id']} ({job['type']}) {params}")
        lines = [f"JOB_ID='{job['id']}'", f"JOB_TYPE='{job['type']}'"] + [f"{k}='{v}'" for k, v in params.items()]
        return "\n".join(lines) + "\n"


def finish_job(mac, job_id, rc):
    job = db.q1("SELECT * FROM jobs WHERE id=? AND mac=?", os.path.basename(job_id), mac)
    if not job:
        return
    job = db.finish_job(job["id"], rc)
    log(f"[job] {mac}: {job_id} {job['state']} rc={rc}")
    live.publish({"t": "job", "mac": mac, "job": job["id"], "state": job["state"]})
    if job["type"] == "install":
        p = json.loads(job["params"] or "{}")
        notify.send(f"KwickBoot: install of {p.get('OS', '?')} on {p.get('HOSTNAME') or mac} "
                    f"{'finished OK' if rc == 0 else f'FAILED (rc={rc})'} - started by {job.get('created_by') or '?'}")
        insights.record_install(mac, job["id"])    # opt-in, anonymised at source; no-op unless enabled (see insights.py)
    db.audit("agent", f"job {job['state']}", f"{mac} {job_id} rc={rc}")
    if job["type"] == "wipe":
        # one signed certificate per disk that passed verification (also when another disk of the same job failed)
        issued, failed = [], []
        try:
            text = open(os.path.join(jobs_dir(mac), job["id"] + ".log"), encoding="utf-8", errors="replace").read()
        except OSError:
            text = ""
        for line in text.splitlines():
            if not line.startswith("WIPECERT "):
                continue
            try:
                rec = json.loads(line[9:])
            except ValueError:
                continue
            if rec.get("verify") == "PASS":
                cid = certs.issue(mac, job["id"], job.get("created_by") or "?", rec, CFG.get("server_name") or MY_IP)
                issued.append(f"{rec.get('serial')} -> {cid}")
            else:
                failed.append(str(rec.get("serial")))
        if issued or failed:
            name = (machine(mac) or {}).get("name") or mac
            db.audit("server", "WIPE certificates", f"{name} {mac}: issued {', '.join(issued) or 'none'}"
                                                   f"{'; FAILED verification: ' + ', '.join(failed) if failed else ''}")
            log(f"[wipe] {mac}: certificates {issued}, failed {failed}")
            if failed:
                notify.send(f"KwickBoot: {name} ({mac}) secure wipe FAILED verification on disk(s) {', '.join(failed)} - no certificate")
            live.publish({"t": "job", "mac": mac, "job": job["id"], "state": job["state"]})
        return
    if job["type"] == "restore" and rc == 0:
        # golden image written: the machine boots its disk now; what the image contains is unknown here -> hand-off
        p = json.loads(job["params"] or "{}")
        set_default(mac, "local")
        firstboot_start(mac, job["id"], {"OS": "image: " + str(p.get("NAME", "?"))}, check="handoff")
        return
    if job["type"] == "manual" and rc == 0:
        # vendor installer finished in the VM: the machine boots its disk now; nothing is known to probe -> hand-off
        p = json.loads(job["params"] or "{}")
        if p.get("DISKBUS") == "none":       # an ISO was only run (live tool): the machine's boot settings stay as they are
            return
        set_default(mac, "local")
        firstboot_start(mac, job["id"], {"OS": "manual: " + str(p.get("ISO", "?"))}, check="handoff")
        return
    if job["type"] == "install" and rc == 0:
        if p.get("FIN", "reboot") == "reboot":
            firstboot_start(mac, job["id"], p)
        # installed: future PXE boots go to the local disk - or, for recipes with RECIPE_BOOT=seat, to the diskless seat
        boot = "local"
        try:
            src = open(os.path.join(HTTP_ROOT, "recipes", os.path.basename(p.get("OS", "")) + ".sh"), encoding="utf-8").read()
            boot = "seat" if "\nRECIPE_BOOT=seat" in src else "truenas" if "\nRECIPE_BOOT=truenas" in src else "local"
        except OSError:
            pass
        if boot == "truenas":
            # the RAM OS only checked the machine; the next boot runs iXsystems' installer on the confirmed disk, then local
            ser = str(p.get("CONFIRM") or "")
            set_default(mac, "local")
            if serial_ok(ser):
                set_next(mac, f"truenas:{ser}")
                log(f"[intent] {mac}: next boot -> TrueNAS installer for disk serial {ser}")
            return
        set_default(mac, boot)
        h = str(p.get("HOSTNAME") or "")
        if boot == "seat" and h and len(h) <= 63 and all(c.isalnum() or c == "-" for c in h):
            db.upsert_machine(mac, name=h.lower())     # the seat's hostname comes from its machine name at every boot


# ---------------------------------------------------------------- RAM OS auto-start overlay (apkovl)
def build_apkovl():
    """tftp/alpine/tk.apkovl.tar.gz: enables default boot services + starts the agent via local.d."""
    import io
    import tarfile
    out = os.path.join(TFTP_ROOT, "alpine", "tk.apkovl.tar.gz")
    start = ("#!/bin/sh\n# PXE toolkit: fetch and start the RAM OS agent\n"
             "( for i in 1 2 3 4 5 6 7 8 9 10; do "
             f"wget -qO /tmp/agent.sh http://{MY_IP}:{HTTP_PORT}/scripts/agent.sh && break; sleep 3; done; "
             "sh /tmp/agent.sh > /var/log/pxe-agent.log 2>&1 ) &\n")
    import ramos
    files = [("etc/.default_boot_services", b"", 0o644), ("etc/local.d/pxe-agent.start", start.encode(), 0o755)] \
        + ramos.overlay_files(f"{MY_IP}:{HTTP_PORT}")
    with tarfile.open(out, "w:gz") as t:
        for d in ("etc", "etc/local.d", "etc/runlevels", "etc/runlevels/default", "usr", "usr/local", "usr/local/bin"):
            ti = tarfile.TarInfo(d); ti.type = tarfile.DIRTYPE; ti.mode = 0o755; ti.mtime = time.time()
            t.addfile(ti)
        for name, data, mode in files:
            ti = tarfile.TarInfo(name); ti.size = len(data); ti.mode = mode; ti.mtime = time.time()
            t.addfile(ti, io.BytesIO(data))
        ti = tarfile.TarInfo("etc/runlevels/default/local"); ti.type = tarfile.SYMTYPE
        ti.linkname = "/etc/init.d/local"; ti.mtime = time.time()
        t.addfile(ti)
    log(f"built {os.path.relpath(out, TFTP_ROOT)} (agent auto-start, KwickBoot console)")
    try:
        r = ramos.build_init_overlay(os.path.join(TFTP_ROOT, "alpine", "initramfs-lts"), os.path.join(TFTP_ROOT, "alpine", "kb-init.cpio.gz"))
        log(f"built alpine/kb-init.cpio.gz: {r}")
    except Exception as e:                       # never block booting over branding
        log(f"RAM OS init customisation skipped: {e}")


# ---------------------------------------------------------------- templates
def render(text):
    return (text.replace("{{SERVER_IP}}", MY_IP)
                .replace("{{HTTP_PORT}}", str(HTTP_PORT)))


def render_tftp_templates():
    for root, _, files in os.walk(TFTP_ROOT):
        for fn in files:
            if fn.endswith(".tmpl"):
                src = os.path.join(root, fn)
                with open(src, encoding="utf-8") as f:
                    out = render(f.read())
                with open(src[:-5], "w", encoding="utf-8", newline="\n") as f:
                    f.write(out)
                log(f"rendered {os.path.relpath(src[:-5], TFTP_ROOT)}")


H = lambda: f"http://{MY_IP}:{HTTP_PORT}"
ALPINE_ARGS = ("ip=dhcp modules=loop,squashfs,sd-mod,usb-storage alpine_repo=https://dl-cdn.alpinelinux.org/alpine/v3.24/main "
               "modloop={H}/alpine/modloop-lts apkovl={H}/tftp/alpine/tk.apkovl.tar.gz "
               "ssh_key={H}/authorized_keys firmware_class.path=/fw console=tty0")
DEBIAN_ARGS = "netcfg/link_wait_timeout=30 firmware_class.path=/fw"
# installed-OS EFI loaders, tried in order by "boot local disk" (distro first, generic fallback last)
LOCAL_LOADERS = ("/EFI/debian/shimx64.efi /EFI/debian/grubx64.efi /EFI/ubuntu/shimx64.efi /EFI/ubuntu/grubx64.efi "
                 "/EFI/alpine/grubx64.efi /EFI/fedora/shimx64.efi /EFI/rocky/shimx64.efi /EFI/redhat/shimx64.efi "
                 "/EFI/opensuse/shim.efi /EFI/arch/grubx64.efi /EFI/Microsoft/Boot/bootmgfw.efi /EFI/BOOT/BOOTX64.EFI")


def iso_list():
    d = os.path.join(HTTP_ROOT, "iso")
    return sorted(f for f in os.listdir(d) if f.lower().endswith(".iso")) if os.path.isdir(d) else []


def iso_boot(h, name):
    """iPXE lines that boot an ISO from the library on the bare machine. Windows PE / Setup ISOs go through wimboot with
    their boot files served from inside the ISO (/isofile/) - a few large HTTP downloads; BIOS and UEFI each get their
    own BCD. Everything else (Linux, BSD, XP-era PE, tools) is attached as a disk (sanboot)."""
    import isofs
    path = os.path.join(HTTP_ROOT, "iso", name)
    lines = []
    if name in iso_list() and os.path.isfile(os.path.join(HTTP_ROOT, "tools", "wimboot")):
        for efi in (False, True):
            files = isofs.winpe_files(path, efi=efi)
            if not files:
                continue
            q = urllib.parse.quote(name)
            lines += [f"iseq ${{efi}} {int(efi)} || goto kb_not_{int(efi)}", f"kernel {h}/tools/wimboot"]
            lines += [f"initrd {h}/isofile/{q}/{inner} {fn}" for inner, fn in files]
            lines += ["boot", f":kb_not_{int(efi)}"]
        # XP-era Windows PE / BartPE / Setup (SETUPLDR) want a real CD drive: memdisk loads the ISO into RAM and
        # presents it to the BIOS as an El Torito CD (legacy BIOS only; small images)
        xp = any(isofs.lookup(path, p) for p in ("setupldr.bin", "i386/setupldr.bin", "wxpe/setupldr.bin", "minint/setupldr.bin"))
        if xp and os.path.getsize(path) <= 1536 << 20 and os.path.isfile(os.path.join(HTTP_ROOT, "tools", "memdisk")):
            lines += ["iseq ${efi} 1 || goto kb_memdisk", f"sanboot --no-describe {h}/iso/{name}", ":kb_memdisk",
                      f"kernel {h}/tools/memdisk iso raw", f"initrd {h}/iso/{name}", "boot"]
    base = f"{h}/isofile/{urllib.parse.quote(name)}"
    # Red Hat family (CentOS/RHEL 6+, Fedora, Rocky, Alma, Oracle): the ISO's own installer kernel + initrd, and the ISO's
    # contents over HTTP as the package source - the installer never needs a CD it cannot see
    if name in iso_list() and (isofs.lookup(path, ".treeinfo") or isofs.lookup(path, ".discinfo")):
        for k, i in (("images/pxeboot/vmlinuz", "images/pxeboot/initrd.img"), ("isolinux/vmlinuz", "isolinux/initrd.img")):
            if isofs.lookup(path, k) and isofs.lookup(path, i):
                lines += [f"kernel {base}/{k} initrd=initrd.img inst.repo={base}/ repo={base}/ ip=dhcp",
                          f"initrd {base}/{i} initrd.img", "boot"]
                break
    # Ubuntu / casper live images: kernel + initrd from the ISO; casper downloads the whole ISO into RAM (url=)
    if name in iso_list() and isofs.lookup(path, "casper/vmlinuz"):
        ird = next((p for p in ("casper/initrd", "casper/initrd.lz", "casper/initrd.gz") if isofs.lookup(path, p)), None)
        if ird:
            lines += [f"kernel {base}/casper/vmlinuz initrd=initrd boot=casper ip=dhcp url={h}/iso/{name} ---",
                      f"initrd {base}/{ird} initrd", "boot"]
    return lines + [f"sanboot --no-describe {h}/iso/{name}"]


# Windows time zones for the IANA names KwickBoot uses (unknown -> UTC; the user can change it after setup)
def server_tz():
    """IANA zone of this server (e.g. America/Chicago) from /etc/localtime; 'UTC' if unknown."""
    try:
        p = os.path.realpath("/etc/localtime")
        return p.split("/zoneinfo/", 1)[1] if "/zoneinfo/" in p else "UTC"
    except OSError:
        return "UTC"


WIN_TZ = {"UTC": "UTC", "Etc/UTC": "UTC", "America/Chicago": "Central Standard Time", "America/New_York": "Eastern Standard Time",
          "America/Denver": "Mountain Standard Time", "America/Phoenix": "US Mountain Standard Time",
          "America/Los_Angeles": "Pacific Standard Time", "America/Anchorage": "Alaskan Standard Time",
          "Pacific/Honolulu": "Hawaiian Standard Time", "Europe/London": "GMT Standard Time", "Europe/Berlin": "W. Europe Standard Time",
          "Europe/Paris": "Romance Standard Time", "Asia/Shanghai": "China Standard Time", "Asia/Hong_Kong": "China Standard Time",
          "Asia/Taipei": "Taipei Standard Time", "Asia/Tokyo": "Tokyo Standard Time", "Asia/Singapore": "Singapore Standard Time",
          "Australia/Sydney": "AUS Eastern Standard Time"}


def djoin_path(mac):
    """Offline domain join file for one machine (djoin /provision output). Under data/ - never in the web folder."""
    return os.path.join(BASE, "data", "djoin", mac.replace(":", "-") + ".txt")


def djoin_info(mac):
    """Public facts about the stored join file (never its content)."""
    f = djoin_path(mac)
    try:
        meta = json.load(open(f + ".json", encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    return dict(meta, present=True) if os.path.isfile(f) else {"present": False}


def djoin_drop(mac, why):
    for f in (djoin_path(mac), djoin_path(mac) + ".json"):
        try:
            os.remove(f)
        except OSError:
            pass
    log(f"[djoin] {mac}: join file deleted ({why})")


def ppkg_path(dept_id):
    """Entra ID bulk-enrollment provisioning package of a department (Windows Configuration Designer). It holds an
    enrollment token, so it lives under data/ - never in the web folder."""
    return os.path.join(BASE, "data", "ppkg", f"dept-{int(dept_id or 0)}.ppkg")


def ppkg_info(dept_id):
    f = ppkg_path(dept_id)
    if not dept_id or not os.path.isfile(f):
        return {"present": False}
    try:
        meta = json.load(open(f + ".json", encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    return dict(meta, present=True)


def windows_unattend(p, mac=""):
    """unattend.xml for the Windows recipe: computer name, region and time zone, a local admin account WITHOUT a
    password (Windows asks for one at the first sign-in), and an OOBE without questions. The license terms were accepted
    by the operator when the install was started (WIN_EULA=1, recorded in the audit log)."""
    from xml.sax.saxutils import escape as x
    host = x(str(p.get("HOSTNAME") or "windows-pc")[:15].upper())
    user = x(str(p.get("WIN_USER") or "admin")[:20])
    tz = x(WIN_TZ.get(str(p.get("TZ") or server_tz()), "UTC"))   # no zone in the job: the server's own (same site)
    loc = x(str(p.get("WIN_LOCALE") or "en-US"))
    comp = ('processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" '
            'xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State"')
    join = ""
    if p.get("WIN_JOIN") == "djoin" and mac:
        # offline domain join: the blob a domain admin made with `djoin /provision` - Windows joins the domain during
        # specialize without any password on the network. Only this machine's running install gets it (token checked).
        try:
            blob = open(djoin_path(mac), encoding="ascii").read().strip()
        except OSError:
            blob = ""
        if re.fullmatch(r"[A-Za-z0-9+/=]{100,65536}", blob):
            join = f"""
    <component name="Microsoft-Windows-UnattendedJoin" {comp}>
      <Identification>
        <Provisioning>
          <AccountData>{blob}</AccountData>
        </Provisioning>
      </Identification>
    </component>"""
    oem_xml, reg_xml = "", ""
    if p.get("WIN_OEM_B64"):
        # the department's support information: Settings > System > About ("Support"), like a PC maker sets it
        import base64
        try:
            o = json.loads(base64.b64decode(p["WIN_OEM_B64"]).decode())
        except (ValueError, TypeError):
            o = {}
        tags = (("manufacturer", "Manufacturer"), ("model", "Model"), ("phone", "SupportPhone"), ("hours", "SupportHours"),
                ("url", "SupportURL"))
        inner = "".join(f"<{t}>{x(str(o[k])[:255])}</{t}>" for k, t in tags if o.get(k))
        if inner:
            oem_xml = f"\n      <OEMInformation>{inner}</OEMInformation>"
        if o.get("org"):
            reg_xml += f"\n      <RegisteredOrganization>{x(str(o['org'])[:64])}</RegisteredOrganization>"
        if o.get("owner"):
            reg_xml += f"\n      <RegisteredOwner>{x(str(o['owner'])[:64])}</RegisteredOwner>"
    drv = ""
    if p.get("WIN_DRIVERS") == "1":
        # drivers matched from the inventory were put in C:\Drivers: Windows' device installer looks there during setup
        drv = f"""
    <component name="Microsoft-Windows-PnpCustomizationsNonWinPE" {comp}>
      <DriverPaths>
        <PathAndCredentials wcm:action="add" wcm:keyValue="1"><Path>C:\\Drivers</Path></PathAndCredentials>
      </DriverPaths>
    </component>"""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!-- KwickBoot: generated for {host} -->
<unattend xmlns="urn:schemas-microsoft-com:unattend">
  <settings pass="specialize">
    <component name="Microsoft-Windows-Shell-Setup" {comp}>
      <ComputerName>{host}</ComputerName>
      <TimeZone>{tz}</TimeZone>{oem_xml}{reg_xml}
    </component>{join}{drv}
  </settings>
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-International-Core" {comp}>
      <InputLocale>{loc}</InputLocale>
      <SystemLocale>{loc}</SystemLocale>
      <UILanguage>{loc}</UILanguage>
      <UserLocale>{loc}</UserLocale>
    </component>
    <component name="Microsoft-Windows-Shell-Setup" {comp}>
      <OOBE>
        <HideEULAPage>true</HideEULAPage>
        <HideOEMRegistrationScreen>true</HideOEMRegistrationScreen>
        <HideOnlineAccountScreens>true</HideOnlineAccountScreens>
        <HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>
        <ProtectYourPC>3</ProtectYourPC>
        <!-- no setup screens at all (incl. "Checking for updates" / "Keep your PC on"): Windows Update runs after setup -->
        <SkipMachineOOBE>true</SkipMachineOOBE>
        <SkipUserOOBE>true</SkipUserOOBE>
      </OOBE>
      <UserAccounts>
        <LocalAccounts>
          <LocalAccount wcm:action="add">
            <Name>{user}</Name>
            <Group>Administrators</Group>
            <Password><Value></Value><PlainText>true</PlainText></Password>
          </LocalAccount>
        </LocalAccounts>
      </UserAccounts>
      <TimeZone>{tz}</TimeZone>
    </component>
  </settings>
</unattend>
"""


def iso_grub(name):
    """UEFI: boot an ISO from the library with GRUB itself over HTTP (no iPXE stage, no DHCP dependence).
    Windows PE / Setup -> wimboot with the ISO's EFI boot files (GRUB's newc: initrds); Red Hat family and casper ->
    the ISO's own kernel + initrd. None -> caller falls back to the iPXE route (disk emulation)."""
    import isofs
    if name not in iso_list():
        return None
    path = os.path.join(HTTP_ROOT, "iso", name)
    base = f"/isofile/{urllib.parse.quote(name)}"
    # Windows PE / Setup: NOT from GRUB - Debian's GRUB 2.12 loads `linux` images in a way wimboot does not start from
    # (verified 2026-11-03: wimboot never runs). Those go GRUB -> iPXE -> wimboot (iso_boot), which works with
    # KwickBoot's proxyDHCP (iPXE gets boot.ipxe).
    if isofs.lookup(path, ".treeinfo") or isofs.lookup(path, ".discinfo"):
        for k, i in (("images/pxeboot/vmlinuz", "images/pxeboot/initrd.img"), ("isolinux/vmlinuz", "isolinux/initrd.img")):
            if isofs.lookup(path, k) and isofs.lookup(path, i):
                repo = f"{H()}{base}/"
                return (f"menuentry 'KwickBoot: {name}' {{\n  set root=$kbhttp\n"
                        f"  linux {base}/{k} inst.repo={repo} repo={repo} ip=dhcp\n  initrd {base}/{i}\n}}\n")
    if isofs.lookup(path, "casper/vmlinuz"):
        ird = next((p for p in ("casper/initrd", "casper/initrd.lz", "casper/initrd.gz") if isofs.lookup(path, p)), None)
        if ird:
            return (f"menuentry 'KwickBoot: {name}' {{\n  set root=$kbhttp\n"
                    f"  linux {base}/casper/vmlinuz boot=casper ip=dhcp url={H()}/iso/{name} ---\n  initrd {base}/{ird}\n}}\n")
    return None


def isofile_serve(handler, rest):
    """GET /isofile/<iso>/<path inside>: one file out of an ISO in the library (read-only, Range supported)."""
    import isofs
    name, _, inner = urllib.parse.unquote(rest).partition("/")
    if name not in iso_list() or not inner:
        handler.send_error(404)
        return
    path = os.path.join(HTTP_ROOT, "iso", name)
    found = isofs.lookup(path, inner)
    if not found:
        handler.send_error(404)
        return
    size = found[0]
    start, end = 0, size - 1
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", handler.headers.get("Range", "").strip())
    if m and (m.group(1) or m.group(2)):
        if m.group(1):
            start, end = int(m.group(1)), int(m.group(2)) if m.group(2) else size - 1
        else:
            start, end = max(0, size - int(m.group(2))), size - 1
        if start >= size:
            handler.send_response(416)
            handler.send_header("Content-Range", f"bytes */{size}")
            handler.end_headers()
            return
        end = min(end, size - 1)
        handler.send_response(206)
        handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    else:
        handler.send_response(200)
    handler.send_header("Content-Type", "application/octet-stream")
    handler.send_header("Accept-Ranges", "bytes")
    handler.send_header("Content-Length", str(end - start + 1))
    handler.end_headers()
    if handler.command == "HEAD":
        return
    try:
        for chunk in isofs.read_range(path, found, start, end):
            handler.wfile.write(chunk)
    except (BrokenPipeError, ConnectionResetError):
        pass


def windows_ready():
    w = os.path.join(HTTP_ROOT, "windows")
    return all(os.path.exists(os.path.join(w, p)) for p in ("boot/bcd", "boot/boot.sdi", "sources/boot.wim"))


# ---------------------------------------------------------------- GRUB per-MAC config (TFTP, generated)
# Hand-off to the installed system, per machine (machines.local_boot) because firmware differ in what works:
#   auto      (default) the disk's own grub.cfg via configfile (Linux; Secure Boot stays intact - its kernels are signed),
#             else chainload the disk's EFI loader (Windows, BSD, appliances), else back to the firmware
#   chainload only the disk's EFI loader
#   exit      always back to the firmware, which boots the next entry of its boot order (some firmware open setup instead)
LOCAL_BOOT_METHODS = ("auto", "chainload", "exit")
_LB_HEAD = ("menuentry 'KwickBoot: start the installed system' {\n"
            "  insmod part_gpt\n  insmod part_msdos\n  insmod ext2\n  insmod fat\n  insmod chain\n")
_LB_CFG = ("  if search --no-floppy --file --set=diskroot /boot/grub/grub.cfg; then\n"
           "    set root=$diskroot\n    set prefix=($diskroot)/boot/grub\n    configfile /boot/grub/grub.cfg\n  fi\n"
           "  if search --no-floppy --file --set=diskroot /grub/grub.cfg; then\n"
           "    set root=$diskroot\n    set prefix=($diskroot)/grub\n    configfile /grub/grub.cfg\n  fi\n")
_LB_CHAIN = ("  for f in /EFI/Microsoft/Boot/bootmgfw.efi /EFI/BOOT/BOOTX64.EFI; do\n"
             # root MUST be the disk: chainloader gives the loader $root as its device (left on the HTTP server, the
             # loader looks for its files on the network card)
             "    if search --no-floppy --file --set=diskroot $f; then set root=$diskroot; chainloader $f; boot; fi\n  done\n")
_LB_EXIT = "  echo 'KwickBoot: handing over to the firmware - next boot entry (local disk)'\n  exit 1\n}\n"


# DMI placeholders vendors leave in the firmware: never shown as a model or serial
JUNK_DMI = ("default string", "to be filled by o.e.m.", "to be filled by oem", "system product name", "system serial number",
            "system manufacturer", "not specified", "not applicable", "none", "o.e.m.", "oem", "0123456789", "123456789",
            "chassis serial number", "base board serial number", "unknown", "type1productconfigid", "x.x")


def local_ident(mac):
    """model / serial / firmware / TPM for the on-machine page (/local), from the machine's latest inventory report.
    DMI placeholders ('Default string', 'To be filled by O.E.M.') are dropped; the board name stands in for a missing
    product name, e.g. 'Intel NUC32D 2C' on a mini PC whose product field says 'Default string'."""
    inv, _ = db.latest_report(mac, "inventory")

    def g(rx):
        m = re.search(rx, inv, re.M)
        return m.group(1).strip() if m else ""

    def ok(v):
        return v if v and v.strip().lower() not in JUNK_DMI else ""
    vendor, product, board = ok(g(r"vendor=(.*?) product=")), ok(g(r"product=(.*?) version=")), ok(g(r"board=(.*?) rev="))
    name = product or board
    model = name if (name and vendor and name.lower().startswith(vendor.lower())) else " ".join(x for x in (vendor, name) if x)
    serial = ok(g(r"^\s*serial=(.*?) uuid="))
    fw = tpm = ""
    sec = re.search(r"boot=(\w+) secure_boot=(\w+) tpm=(\w+)", inv)
    if sec:
        uefi = sec.group(1).lower() == "uefi"
        sb = sec.group(2).lower() in ("on", "1", "yes", "enabled")
        fw = ("UEFI, Secure Boot " + ("ON" if sb else "off")) if uefi else "legacy BIOS"
        tpm = {"2": "2.0", "2.0": "2.0", "1": "1.2", "1.2": "1.2"}.get(sec.group(3).lower(), "none")
    return {"model": model, "serial": serial, "firmware": fw, "tpm": tpm}


def local_entry(method="auto"):
    return _LB_HEAD + {"chainload": _LB_CHAIN, "exit": ""}.get(method, _LB_CFG + _LB_CHAIN) + _LB_EXIT


LOCAL_ENTRY = local_entry()


def seat_ready():
    return all(os.path.exists(os.path.join(HTTP_ROOT, "seat", f)) for f in ("vmlinuz", "initrd.img", "filesystem.squashfs"))


def seat_args(mac):
    """Diskless gaming seat (linux/build-seat.sh): Debian-signed kernel, root filesystem fetched into RAM by live-boot.
    The machine's approved name becomes its hostname; nothing secret is passed (the seat holds no credentials)."""
    name = ((machine(mac) or {}).get("name") or "") if mac else ""
    name = name if name and len(name) <= 63 and all(c.isalnum() or c == "-" for c in name) else "kb-seat"
    return (f"boot=live fetch={H()}/seat/filesystem.squashfs ip=dhcp kb_server={MY_IP}:{HTTP_PORT} kb_host={name} "
            "quiet splash loglevel=3 systemd.show_status=false")


# ---------------------------------------------------------------- first boot of a fresh install
# "done" in the RAM OS = written and verified on disk. This follows the machine one step further: did the new system come up?
#   ep       managed Linux: its endpoint agent checked in after the install
#   tcp:N    the recipe's RECIPE_CHECK_PORT (or SSH for full Linux installs) answers on the machine's LAN address
#   handoff  nothing to probe (ChromeOS Flex; OPNsense/OpenWrt come up on their own 192.168.1.1 LAN): the machine
#            network-booted after the install and the server handed it to its disk
# Probes go only to the one address the machine reported, and only inside this server's /24 - never other networks.
FIRSTBOOT_TIMEOUT = 900


def _recipe_src(os_id):
    try:
        return open(os.path.join(HTTP_ROOT, "recipes", os.path.basename(str(os_id)) + ".sh"), encoding="utf-8").read()
    except OSError:
        return ""


def fb_public(fb):
    """first-boot record without its secrets (for the live channel and the UI)."""
    return {k: v for k, v in (fb or {}).items() if k not in ("secret", "apps_secret", "join_secret")}


def firstboot_start(mac, job_id, p, check=None):
    src = _recipe_src(p.get("OS", ""))
    port = next((int(l.split("=", 1)[1].split()[0]) for l in src.splitlines()
                 if l.startswith("RECIPE_CHECK_PORT=") and l.split("=", 1)[1].split()[0].isdigit()), None)
    image = "RECIPE_METHOD=image" in src
    if not check and "RECIPE_CHECK=win" in src:
        check = "win"          # Windows: the new system calls /agent/winok with the job's one-time secret
    check = check or ("ep" if p.get("EP_TOKEN") else f"tcp:{port}" if port else "handoff" if image else "tcp:22")
    fb = {"job": job_id, "os": p.get("OS"), "since": time.time(), "check": check, "state": "waiting"}
    if check == "win":
        fb["secret"] = p.get("WIN_FB", "")
        if fb["secret"] and p.get("WIN_APPS") == "bundle":
            # the app bundle reports once when it is done (after setup, in the background), with a value derived from
            # the check-in secret: the PC derives it before it deletes the secret, the server keeps only the derived one
            fb["apps_secret"] = hmac.new(fb["secret"].encode(), b"apps", "sha256").hexdigest()
            fb["apps"] = {"state": "waiting"}
        if fb["secret"] and p.get("WIN_JOIN") == "entra":
            # the Entra join finishes after the check-in (the package restarts the PC): it reports once, same scheme
            fb["join_secret"] = hmac.new(fb["secret"].encode(), b"join", "sha256").hexdigest()
            fb["join"] = {"state": "waiting", "kind": "entra"}
    db.upsert_machine(mac, firstboot=json.dumps(fb))
    live.publish({"t": "firstboot", "mac": mac, **fb_public(fb)})


def firstboot_mark(mac, state, how=""):
    fb = json.loads((db.machine(mac) or {}).get("firstboot") or "null")
    if not fb or fb["state"] in ("ok", "timeout") or fb["state"] == state:
        return
    fb.update(state=state, at=time.time(), how=how)
    if state in ("ok", "timeout"):
        fb.pop("secret", None)            # one-time: a Windows check-in secret is worthless (and gone) once the check is over
        if state == "ok" and fb.get("os") == "windows" and os.path.isfile(djoin_path(mac)):
            djoin_drop(mac, "Windows set up - the file was used")
    db.upsert_machine(mac, firstboot=json.dumps(fb))
    if state in ("ok", "timeout") and fb.get("job"):       # kept per job (the machine keeps only its latest): report cards
        db.set_setting("fb:" + fb["job"], json.dumps({"state": state, "at": fb["at"], "how": how[:120]}))
    log(f"[firstboot] {mac}: {fb['os']} {state} {how}")
    live.publish({"t": "firstboot", "mac": mac, **fb_public(fb)})
    if state in ("ok", "timeout"):
        name = (machine(mac) or {}).get("name") or mac
        db.audit("server", f"first boot {state}", f"{mac} {fb['os']} {how}")
        if state == "timeout":
            notify.send(f"KwickBoot: {name} ({mac}) - {fb['os']} installed, but the new system did not answer within "
                        f"{FIRSTBOOT_TIMEOUT // 60} min ({fb['check']})")


def firstboot_handed(mac):
    """The machine network-booted and is being sent to its disk."""
    fb = json.loads((machine(mac) or {}).get("firstboot") or "null")
    if fb and fb["state"] == "waiting":
        if fb["check"] == "handoff":
            firstboot_mark(mac, "ok", "handed to the installed system (it offers nothing to probe)")
        else:
            firstboot_mark(mac, "handed", "handed to the installed system, waiting for it to answer")


HEARD = {}            # mac -> last time its RAM OS posted anything (screen ~1/s, job log) - in memory, no DB writes
STALL_S = 300        # a running job whose machine is silent this long is failed (paused VM, power loss, hang)
_watch = {}          # job id -> when the watchdog first saw it running


def job_watchdog():
    now = time.time()
    running = db.q("SELECT id, mac, type FROM jobs WHERE state='running'")
    for j in running:
        last = max(HEARD.get(j["mac"], 0), _watch.setdefault(j["id"], now))
        if now - last < STALL_S:
            continue
        tail = ""
        try:
            text = open(os.path.join(jobs_dir(j["mac"]), j["id"] + ".log"), encoding="utf-8", errors="replace").read()
            tail = next((x.strip() for x in reversed(webui.crfold(text[-4000:]).splitlines()) if x.strip()), "")[:160]
        except OSError:
            pass
        why = f"machine stopped answering for {STALL_S // 60} min" + (f" - last line: {tail}" if tail else "")
        db.x("UPDATE jobs SET state='failed', rc=-1, error=?, finished=? WHERE id=? AND state='running'", why, db.now(), j["id"])
        log(f"[job] {j['mac']}: {j['id']} ({j['type']}) failed - {why}")
        live.publish({"t": "job", "mac": j["mac"], "job": j["id"], "state": "failed"})
    for k in set(_watch) - {j["id"] for j in running}:
        _watch.pop(k, None)


def firstboot_loop():
    import socket as _s
    net = MY_IP.rsplit(".", 1)[0] + "."
    while True:
        time.sleep(10)
        try:
            job_watchdog()
        except Exception as e:        # noqa: BLE001 - the watchdog must never stop the first-boot checks
            log(f"[job] watchdog error: {e}")
        try:
            for mac in netbar.tick():                 # draw down live netbar sessions; lock any that ran out of time
                live.publish({"t": "netbar", "mac": mac, "state": "locked"})
        except Exception as e:        # noqa: BLE001
            log(f"[netbar] tick error: {e}")
        try:
            for m in db.q("SELECT * FROM machines WHERE firstboot IS NOT NULL"):
                fb = json.loads(m["firstboot"])
                if fb["state"] not in ("waiting", "handed"):
                    continue
                if time.time() - fb["since"] > (FIRSTBOOT_TIMEOUT * 8 // 3 if fb["check"] == "win" else FIRSTBOOT_TIMEOUT):
                    firstboot_mark(m["mac"], "timeout", f"no answer ({fb['check']})")
                    continue
                if fb["check"] == "ep":
                    if (m.get("ep_seen") or 0) > fb["since"]:
                        firstboot_mark(m["mac"], "ok", "management agent checked in")
                    continue
                if not fb["check"].startswith("tcp:"):
                    continue
                # the RAM OS answers on 22 until it reboots, TrueNAS's installer on 80: count a probe only once the
                # machine was handed to its disk, or the RAM OS said "rebooting" more than 45 s ago
                gone = fb["state"] == "handed" or (m.get("agent_state") == "rebooting" and time.time() - (m.get("agent_seen") or 0) > 45)
                ip = m.get("agent_ip") or ""
                if not gone or not ip.startswith(net):
                    continue
                port = int(fb["check"][4:])
                try:
                    _s.create_connection((ip, port), timeout=2).close()
                    firstboot_mark(m["mac"], "ok", f"{ip}:{port} answers")
                except OSError:
                    pass
        except Exception as e:      # never let the watcher die
            log(f"[firstboot] watcher error: {e}")


def wake(mac):
    """Wake-on-LAN: the magic packet for this one machine, broadcast on this server's own LAN only (UDP 9)."""
    import socket as _s
    pkt = b"\xff" * 6 + bytes.fromhex(norm_mac(mac).replace(":", "")) * 16
    s = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
    s.setsockopt(_s.SOL_SOCKET, _s.SO_BROADCAST, 1)
    if IS_LINUX and IFACE:
        s.setsockopt(_s.SOL_SOCKET, 25, (IFACE + "\0").encode())      # SO_BINDTODEVICE: the LAN port only
    try:
        for dst in (MY_IP.rsplit(".", 1)[0] + ".255", "255.255.255.255"):
            s.sendto(pkt, (dst, 9))
    finally:
        s.close()


def core_ready():
    return CFG.get("ramos", "core") == "core" and all(
        os.path.exists(os.path.join(HTTP_ROOT, "kbcore", f)) for f in ("vmlinuz", "initrd.img"))


def core_args(mac):
    """KwickBoot Core kernel command line (UEFI via GRUB and legacy BIOS via iPXE)."""
    k = f"kb_server={MY_IP}:{HTTP_PORT} pxetoken={issue_token(mac)} quiet loglevel=3 console=tty0"
    if (machine(mac) or {}).get("kb_shell"):     # emergency console on tty2 (set by an admin per machine)
        k += " kb_shell=1"
    return k


def uboot_cfg(mac):
    """extlinux.conf (PXELINUX syntax) that a U-Boot ARM SBC fetches from pxelinux.cfg/ during distro/pxe boot. Boots the
    arm64 Core; FDTDIR lets U-Boot auto-select this board's device tree via ${fdtfile}. Covers the whole U-Boot ARM world
    (Rockchip, Allwinner, Amlogic, NXP, ...) with one tree. Returns None if the U-Boot tree isn't built yet."""
    if not all(os.path.exists(os.path.join(TFTP_ROOT, "uboot", f)) for f in ("vmlinuz", "initrd.img")):
        return None
    return ("DEFAULT kwickboot\nPROMPT 0\nTIMEOUT 10\n"
            "LABEL kwickboot\n  MENU LABEL KwickBoot RAM OS (inspect / rescue / install)\n"
            "  KERNEL /uboot/vmlinuz\n  FDTDIR /uboot/dtbs/\n  INITRD /uboot/initrd.img\n"
            f"  APPEND console=ttyS0,115200 console=tty1 ip=dhcp {core_args(mac)}\n")


def serial_ok(s):
    return 0 < len(s) <= 64 and all(c.isascii() and (c.isalnum() or c in "._-") for c in s)


def truenas_ready():
    return all(os.path.exists(os.path.join(HTTP_ROOT, "truenas", f))
               for f in ("vmlinuz", "initrd.img", "filesystem.squashfs", "TrueNAS-SCALE.update", "kb-truenas.cpio"))


def truenas_args(mac, serial):
    """iXsystems' TrueNAS installer, network-booted unmodified (linux/prepare-truenas.sh). kb_disk = the serial the RAM OS
    confirmed; KwickBoot's initramfs step (kb-truenas.cpio) installs onto exactly that disk via the installer's own API."""
    return (f"gfxpayload=text quiet nomodeset boot=live fetch={H()}/truenas/filesystem.squashfs ip=dhcp console=tty0 "
            f"kb_server={MY_IP}:{HTTP_PORT} kb_disk={serial} pxetoken={issue_token(mac)}")


def grub_for(mac, arch="amd64"):
    action = current_action(mac)
    a = ALPINE_ARGS.format(H=H())
    # Kernels over HTTP (much faster than TFTP). Set root to the HTTP device and use plain paths: GRUB puts the kernel
    # path into BOOT_IMAGE= on the kernel command line, and a "(" there breaks Alpine's firstboot (it evals the cmdline),
    # which is what installs the SSH key into the RAM OS.
    hp = "/tftp"
    head = (f"# generated for {mac}: action={action}\nset timeout=0\nset default=0\n"
            f"set kbhttp=(http,{MY_IP}:{HTTP_PORT})\n")
    cdir = "kbcore" if arch == "amd64" else "kbcore-" + arch   # per-arch Core (arm64 -> kbcore-arm64)
    core = os.path.join(HTTP_ROOT, cdir)
    if action == "alpine" and CFG.get("ramos", "core") == "core" and all(
            os.path.exists(os.path.join(core, f)) for f in ("vmlinuz", "initrd.img")):
        # KwickBoot Core (~30 MB): signed kernel + one small initrd; tools are fetched as packs only when needed
        return head + ("menuentry 'Load KwickBoot to RAM' {\n  set root=$kbhttp\n"
                       f"  linux /{cdir}/vmlinuz {core_args(mac)}\n  initrd /{cdir}/initrd.img\n}}\n")
    t = tool_info(action[5:]) if action.startswith("tool:") else None
    if t and t.get("BIOS_ONLY") == "1":          # GRUB here = a UEFI PC: a BIOS-only tool (FreeDOS) cannot run
        log(f"[tool] {mac}: {t['NAME']} needs legacy BIOS - this PC network-booted in UEFI mode")
        return head + (f"menuentry '{t.get('TITLE', t['NAME'])} needs legacy BIOS' {{\n"
                       f"  echo '{t.get('TITLE', t['NAME'])} needs a PC started in legacy BIOS mode (CSM). This PC started in UEFI mode.'\n"
                       "  sleep 15\n  exit\n}\n")
    if t and t.get("KIND") == "efi":               # one UEFI program (EFI Shell): not Microsoft-signed -> Secure Boot off
        n = t["NAME"]
        return head + (f"menuentry 'KwickBoot {t.get('TITLE', n)}' {{\n"
                       "  if [ \"$lockdown\" = \"y\" ]; then\n"
                       f"    echo '{t.get('TITLE', n)} is not Microsoft-signed: turn Secure Boot off in the firmware settings to use it.'\n"
                       "    sleep 15\n    exit\n  fi\n  set root=$kbhttp\n"
                       f"  chainloader /images/tools/{n}/boot.efi\n}}\n")
    if t:
        # an OS library tool (images/tools/<name>: Wipe, Rescue, ...) - one-shot from the console ("Start on a PC")
        n = t["NAME"]
        return head + (f"menuentry 'KwickBoot {t['TITLE']}' {{\n  set root=$kbhttp\n"
                       f"  linux /images/tools/{n}/vmlinuz {t.get('CMDLINE', '')} {core_args(mac)}\n"
                       f"  initrd /images/tools/{n}/initrd.img\n}}\n")
    kbos = os.path.join(HTTP_ROOT, "kbos")
    if action == "alpine" and CFG.get("ramos", "core") != "alpine" and all(
            os.path.exists(os.path.join(kbos, f)) for f in ("vmlinuz", "initrd.img", "filesystem.squashfs")):
        # Secure Boot capable: Debian-signed kernel (shim -> GRUB verify it), root filesystem fetched into RAM by live-boot
        k = (f"boot=live fetch={H()}/kbos/filesystem.squashfs ip=dhcp kb_server={MY_IP}:{HTTP_PORT} "
             f"pxetoken={issue_token(mac)} quiet loglevel=3 systemd.show_status=false console=tty0")
        return head + ("menuentry 'Load KwickBoot to RAM' {\n  set root=$kbhttp\n"
                       f"  linux /kbos/vmlinuz {k}\n  initrd /kbos/initrd.img\n}}\n")
    if action == "alpine":
        a += f" pxetoken={issue_token(mac)}"
        return head + ("menuentry 'Load KwickBoot to RAM' {\n  set root=$kbhttp\n"
                       f"  linux {hp}/alpine/vmlinuz-lts {a}\n  initrd {hp}/alpine/initramfs-lts {hp}/alpine/fw.cpio.gz"
                       + (f" {hp}/alpine/kb-init.cpio.gz" if os.path.exists(os.path.join(TFTP_ROOT, "alpine", "kb-init.cpio.gz")) else "")
                       + "\n}\n")
    if action.startswith("truenas:") and serial_ok(action[8:]) and truenas_ready():
        return head + ("menuentry 'KwickBoot: TrueNAS install (unattended)' {\n  set root=$kbhttp\n"
                       f"  linux /truenas/vmlinuz {truenas_args(mac, action[8:])}\n"
                       "  initrd /truenas/initrd.img /truenas/kb-truenas.cpio\n}\n")
    if action == "seat" and seat_ready():
        return head + ("menuentry 'KwickBoot gaming seat' {\n  set root=$kbhttp\n"
                       f"  linux /seat/vmlinuz {seat_args(mac)}\n  initrd /seat/initrd.img\n}}\n")
    if action == "signage" and all(os.path.exists(os.path.join(core, f)) for f in ("vmlinuz", "initrd.img")):
        # digital-signage display: the Core RAM OS with kb_signage=1 -> kb-desktop opens the signage player page fullscreen
        return head + ("menuentry 'KwickBoot signage display' {\n  set root=$kbhttp\n"
                       f"  linux /{cdir}/vmlinuz {core_args(mac)} kb_signage=1\n  initrd /{cdir}/initrd.img\n}}\n")
    if action == "local":
        firstboot_handed(mac)
        return head + local_entry((machine(mac) or {}).get("local_boot") or CFG.get("local_boot", "auto"))
    if action == "debian13":
        return head + ("menuentry 'KwickBoot: Debian 13 installer' {\n  set root=$kbhttp\n"
                       f"  linux {hp}/debian-installer/amd64/linux {DEBIAN_ARGS} ---\n"
                       f"  initrd {hp}/debian-installer/amd64/initrd-fw.gz\n}}\n")
    if action == "menu":
        return None   # -> TFTP "not found" -> GRUB's configfile fails -> full menu is shown
    if action.startswith("iso:"):
        e = iso_grub(action[4:])
        if e:
            return head + e
    # everything else is an iPXE action (memtest, iso:, windows, netbootxyz, shell)
    # over HTTP like the kernels (the config itself came over HTTP, so a bare path would be looked up there - and the
    # HTTP server publishes the TFTP folder under /tftp/)
    return head + ("menuentry 'KwickBoot: tools menu (iPXE, needs Secure Boot off)' {\n  set root=$kbhttp\n"
                   "  chainloader /tftp/ipxe/ipxe.efi\n}\n")


# ---------------------------------------------------------------- iPXE script (HTTP, generated)
def ipxe_for(mac):
    action = current_action(mac) if mac else "menu"
    h, a = H(), ALPINE_ARGS.format(H=H())
    s = ["#!ipxe", f"# generated for {mac or 'unknown'}: action={action}",
         "iseq ${platform} efi && set efi 1 || set efi 0"]
    if action == "alpine" and mac:
        a += f" pxetoken={issue_token(mac)}"
    body = {
        "alpine": [f"kernel {h}/tftp/alpine/vmlinuz-lts {a} initrd=initramfs-lts initrd=fw.cpio.gz",
                   f"initrd {h}/tftp/alpine/initramfs-lts", f"initrd {h}/tftp/alpine/fw.cpio.gz", "boot"],
        "local": ["iseq ${efi} 1 && exit || sanboot --no-describe --drive 0x80"],
        "debian13": [f"kernel {h}/tftp/debian-installer/amd64/linux {DEBIAN_ARGS} initrd=initrd-fw.gz ---",
                     f"initrd {h}/tftp/debian-installer/amd64/initrd-fw.gz", "boot"],
        "memtest": [f"iseq ${{efi}} 1 && chain {h}/tools/memtest86+x64.efi || chain {h}/tools/memtest86+x64.bin"],
        "netbootxyz": ["iseq ${efi} 1 && chain --autofree https://boot.netboot.xyz/ipxe/netboot.xyz.efi || "
                       "chain --autofree https://boot.netboot.xyz/ipxe/netboot.xyz.lkrn"],
        "shell": ["shell"],
        "seat": [f"kernel {h}/seat/vmlinuz {seat_args(mac)} initrd=initrd.img", f"initrd {h}/seat/initrd.img", "boot"],
        "windows": [f"kernel {h}/tools/wimboot", f"initrd {h}/windows/boot/bcd BCD",
                    f"initrd {h}/windows/boot/boot.sdi boot.sdi", f"initrd {h}/windows/sources/boot.wim boot.wim", "boot"],
    }
    if action == "local" and mac:
        firstboot_handed(mac)
    if action == "alpine" and mac and core_ready():       # legacy BIOS: the same KwickBoot Core as UEFI machines
        s += [f"kernel {h}/kbcore/vmlinuz {core_args(mac)} initrd=initrd.img", f"initrd {h}/kbcore/initrd.img", "boot"]
    elif action.startswith("iso:"):
        s += iso_boot(h, action[4:])
    elif action.startswith("tool:") and mac and (t := tool_info(action[5:])):   # an OS library tool
        n, k = t["NAME"], f"{h}/images/tools/{t['NAME']}"
        if t.get("KIND") == "efi":              # a UEFI program (EFI Shell): UEFI only
            s += [f"iseq ${{efi}} 1 && chain {k}/boot.efi ||",
                  f"echo {t.get('TITLE', n)} needs a PC started in UEFI mode - this one started in legacy BIOS mode.", "sleep 15", "exit"]
        elif t.get("BIOS_ONLY") == "1":        # e.g. FreeDOS via memdisk: legacy BIOS only
            s += ["iseq ${efi} 1 && goto kbnoefi ||", f"kernel {k}/vmlinuz {t.get('CMDLINE', '')}", f"initrd {k}/initrd.img", "boot",
                  ":kbnoefi", f"echo {t.get('TITLE', n)} needs a PC started in legacy BIOS mode (CSM) - this one started in UEFI mode.",
                  "sleep 15", "exit"]
        else:
            s += [f"kernel {k}/vmlinuz {t.get('CMDLINE', '')} {core_args(mac)} initrd=initrd.img", f"initrd {k}/initrd.img", "boot"]
    elif action.startswith("truenas:") and mac and serial_ok(action[8:]) and truenas_ready():
        s += [f"kernel {h}/truenas/vmlinuz {truenas_args(mac, action[8:])} initrd=initrd.img initrd=kb-truenas.cpio",
              f"initrd {h}/truenas/initrd.img", f"initrd {h}/truenas/kb-truenas.cpio", "boot"]
    elif action.startswith("san:") and mac:        # boot diskless from a KwickBoot-served iSCSI LUN (or an external target)
        s += san.ipxe_boot(action[4:], MY_IP)
    elif action in body:
        s += body[action]
    else:
        s += menu_ipxe(h)
    s += ["echo Action failed - falling back to menu in 5s", "sleep 5", f"chain {h}/boot.ipxe?mac=${{mac}}&menu=1"]
    return "\n".join(s) + "\n"


def menu_ipxe(h):
    m = ["menu PXE toolkit", "item alpine   Alpine in RAM (inspect / rescue / install - touches no disk)",
         "item local    Boot local disk", "item --gap   Tools / installers",
         "item memtest  Memory test (memtest86+)", "item debian13 Debian 13 installer (interactive)",
         "item netbootxyz netboot.xyz (100+ OS installers from the internet)"]
    if windows_ready():
        m.append("item windows  Windows Setup (WinPE via wimboot)")
    isos = iso_list()
    if isos:
        m.append("item --gap   ISO library (http/iso)")
        m += [f"item iso{i} {f}" for i, f in enumerate(isos)]
    m += ["item shell    iPXE shell", "item reboot   Reboot", "choose --default alpine --timeout 30000 sel || goto alpine",
          "goto ${sel}"]
    labels = {
        "alpine": f"chain {h}/boot.ipxe?mac=${{mac}}&force=alpine", "local": f"chain {h}/boot.ipxe?mac=${{mac}}&force=local",
        "memtest": f"chain {h}/boot.ipxe?mac=${{mac}}&force=memtest", "debian13": f"chain {h}/boot.ipxe?mac=${{mac}}&force=debian13",
        "netbootxyz": f"chain {h}/boot.ipxe?mac=${{mac}}&force=netbootxyz", "windows": f"chain {h}/boot.ipxe?mac=${{mac}}&force=windows",
        "shell": "shell", "reboot": "reboot"}
    for k, v in labels.items():
        m += [f":{k}", v]
    for i, f in enumerate(isos):
        m += [f":iso{i}", f"sanboot --no-describe {h}/iso/{f}"]
    return m


# ---------------------------------------------------------------- proxyDHCP
def parse_options(data):
    opts, i = {}, 240
    while i < len(data):
        code = data[i]
        if code == 255:
            break
        if code == 0:
            i += 1
            continue
        ln = data[i + 1]
        opts[code] = data[i + 2:i + 2 + ln]
        i += 2 + ln
    return opts


def client_arch(opts):
    """Client system architecture: opt 93, else the "Arch:NNNNN" in the vendor class (some ROMs send only that)."""
    if len(opts.get(93, b"")) >= 2:
        return struct.unpack("!H", opts[93][:2])[0]
    m = re.search(rb"Arch:(\d{5})", opts.get(60, b""))
    return int(m.group(1)) if m else 0


def ipxe_features(opts):
    """iPXE's encapsulated opt 175: {feature code: value} (19 = HTTP, 20 = HTTPS, 36 = EFI, ...); {} if not sent."""
    ov, j, f = opts.get(175, b""), 0, {}
    while j + 1 < len(ov) and ov[j] != 255:
        if ov[j] == 0:
            j += 1
            continue
        f[ov[j]] = ov[j + 2:j + 2 + ov[j + 1]]
        j += 2 + ov[j + 1]
    return f


# Raspberry Pi network-boot ROM: "PXEClient:Arch:00000:UNDI:002001" exactly like a BIOS PC, never uses port 4011, wants
# "Raspberry Pi Boot" in the opt 43 menu and then TFTPs <serial>/start4.elf (Pi 4/5) or bootcode.bin (Pi 3).
# Recognised by the Raspberry Pi Foundation / Trading MAC prefixes.
PI_OUIS = ("b8:27:eb", "dc:a6:32", "e4:5f:01", "d8:3a:dd", "2c:cf:67", "28:cd:c1")
PI_DIR = os.path.join(TFTP_ROOT, "rpi")      # an operator's own Pi boot tree (optional)


def is_raspberry_pi(mac):
    return mac[:8] in PI_OUIS


def pi_root():
    """Where the Pi boot tree is: tftp/rpi/ if an operator put one there, else the TFTP root, where linux/build-pi-boot.sh
    stages KwickBoot's (Pi firmware + arm64 Core + device trees + config.txt/cmdline.txt). None if neither has one."""
    for d in (PI_DIR, TFTP_ROOT):
        if any(os.path.isfile(os.path.join(d, f)) for f in ("start4.elf", "start.elf", "bootcode.bin")):
            return d
    return None


def pi_files_present():
    return pi_root() is not None


def mac_for_ip(ip):
    """MAC of a LAN client that has no MAC in its request (TFTP): learned from its boot, else the kernel's ARP table -
    filled by the time a Pi asks for cmdline.txt (we already sent it config.txt and start4.elf)."""
    if ip in IP2MAC:
        return IP2MAC[ip]
    try:
        for line in open("/proc/net/arp").read().splitlines()[1:]:
            f = line.split()
            if len(f) >= 4 and f[0] == ip and f[3] != "00:00:00:00:00:00":
                return norm_mac(f[3])
    except OSError:
        pass
    return ""


def pi_cmdline(mac):
    """The Pi's kernel command line: the staged cmdline.txt (rendered from cmdline.txt.tmpl at start) + this boot's agent
    token, so the arm64 Core's agent is accepted like any machine's. An operator's own cmdline (no kb_server=) is untouched."""
    root = pi_root() or TFTP_ROOT
    try:
        base = open(os.path.join(root, "cmdline.txt")).read().strip()
    except OSError:
        base = f"console=serial0,115200 console=tty1 ip=dhcp kb_server={MY_IP}:{HTTP_PORT} loglevel=4"
    if mac and "kb_server=" in base and "pxetoken=" not in base:
        base += f" pxetoken={issue_token(mac)}"
        if (machine(mac) or {}).get("kb_shell"):
            base += " kb_shell=1"
    return base + "\n"


_NOTED = {}


def note_once(key, msg, every=3600):
    """Log a diagnostic at most once per key and period (DHCP retries would otherwise flood the log)."""
    now_ = time.time()
    if now_ - _NOTED.get(key, 0) >= every:
        _NOTED[key] = now_
        log(msg)


# proxyDHCP OFFER layouts - firmware differs in which one it accepts (HP EliteDesk 800 G3 UEFI only takes "binl"):
#   menu   = PXE boot-server discovery menu in opt 43 (legacy BIOS default; EDK2 / Hyper-V / QEMU take it too)
#   binl   = WDS-like: PXEClient only, no file - the PC asks port 4011 and gets the file in the ACK
#   bare   = no opt 43, boot file in the offer        direct = opt 43 discovery-control 0x08 + boot file in the offer
OFFER_STYLES = ("menu", "binl", "bare", "direct")
# Order tried per firmware family. UEFI: binl first - the layout dnsmasq sends every EFI client since 2.76 (UEFI PXE
# menu handling is broken on many PCs: HP, Dell/Intel, ASUS, VMware EFI); EDK2/OVMF takes all four (tested 14 Nov 2026).
# Legacy BIOS PXE ROMs handle the menu well, so it stays first there.
STYLE_ORDER_UEFI = ("binl", "bare", "direct", "menu")
STYLE_ORDER_BIOS = ("menu", "binl", "bare", "direct")
_STYLE_TRY = {}          # mac -> (xid, index into OFFER_STYLES) for the DHCP exchange in progress
_STYLE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "offer_styles.json")
_STYLE_LOCK = threading.Lock()


def _learned_styles():
    try:
        with open(_STYLE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _learn_style(mac, style):
    with _STYLE_LOCK:
        os.makedirs(os.path.dirname(_STYLE_FILE), exist_ok=True)
        d = _learned_styles()
        if d.get(mac) == style:
            return
        d[mac] = style
        tmp = _STYLE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, indent=1, sort_keys=True)
        os.replace(tmp, _STYLE_FILE)
    log(f"[dhcp] {mac}: firmware accepts offer style '{style}' - remembered")


def offer_style(mac, xid, arch=7):
    """Layout of the proxyDHCP OFFER for this PC (see OFFER_STYLES). A re-sent DISCOVER (same xid) means the firmware
    turned down every offer it got, so it gets the next layout; the one it finally REQUESTs on is remembered.
    Start: forced (pxe_offer_test) > learned for this PC > pxe_offer_style (config) > first of the family's order."""
    order = STYLE_ORDER_BIOS if arch == 0 else STYLE_ORDER_UEFI
    forced = (CFG.get("pxe_offer_test") or {}).get(mac)
    if forced and forced in order:
        _STYLE_TRY[mac] = (xid, order, order.index(forced))
        return forced
    start_style = _learned_styles().get(mac) or CFG.get("pxe_offer_style") or order[0]
    start = order.index(start_style) if start_style in order else 0
    prev = _STYLE_TRY.get(mac)
    i = (prev[2] + 1) % len(order) if prev and prev[0] == xid and prev[1] == order else start
    _STYLE_TRY[mac] = (xid, order, i)
    return order[i]


def offer_style_accepted(mac, xid):
    """The PC sent its DHCP REQUEST: it took the offers of this exchange -> remember the layout it last got."""
    prev = _STYLE_TRY.get(mac)
    if prev and prev[0] == xid:
        _learn_style(mac, prev[1][prev[2]])


def build_reply(req, opts, msg_type, boot_file, http=False, style="menu"):
    pkt = bytearray(240)
    pkt[0], pkt[1], pkt[2] = 2, 1, 6
    pkt[4:8] = req[4:8]
    pkt[10:12] = req[10:12]
    pkt[20:24] = socket.inet_aton(MY_IP)
    pkt[28:44] = req[28:44]
    sname = b"" if http else MY_IP.encode()    # HTTP Boot: the boot file is an absolute URL, no TFTP server name
    fb = boot_file.encode()
    binl = (msg_type == 2 and style == "binl" and not http)   # WDS-like offer: no server name / file, PC asks 4011
    if not binl and style != "rpi":
        pkt[44:44 + len(sname)] = sname
        pkt[108:108 + len(fb)] = fb
    pkt[236:240] = b"\x63\x82\x53\x63"
    o = bytearray([53, 1, msg_type]) + bytes([54, 4]) + socket.inet_aton(MY_IP)
    if http:   # UEFI HTTP Boot: firmware accepts a proxy offer whose vendor-class is "HTTPClient"; the URL is in the file field + opt 67
        o += bytes([60, 10]) + b"HTTPClient"
        if 97 in opts:
            o += bytes([97, len(opts[97])]) + opts[97]
        o += bytes([67, len(fb)]) + fb + bytes([255])
        return bytes(pkt) + bytes(o)
    o += bytes([60, 9]) + b"PXEClient"
    if 97 in opts:
        o += bytes([97, len(opts[97])]) + opts[97]
    if binl:
        return bytes(pkt) + bytes(o) + bytes([255])
    if msg_type == 2 and style == "bare":
        o += bytes([66, len(sname)]) + sname + bytes([67, len(fb)]) + fb + bytes([255])
        return bytes(pkt) + bytes(o)
    if msg_type == 2 and style == "direct":
        vend = bytes([6, 1, 0x08, 255])
        o += bytes([43, len(vend)]) + vend
        o += bytes([66, len(sname)]) + sname + bytes([67, len(fb)]) + fb + bytes([255])
        return bytes(pkt) + bytes(o)
    if msg_type == 2 and style == "rpi":    # Pi boot ROM: looks for "Raspberry Pi Boot" in the menu (Pi 3: 3 trailing
        desc = b"Raspberry Pi Boot   "     # spaces), takes the TFTP server from siaddr, ignores file/4011
        vend = bytearray([6, 1, 0x03])
        vend += bytes([9, 3 + len(desc)]) + struct.pack("!HB", 0, len(desc)) + desc
        vend += bytes([10, 4, 0]) + b"PXE" + bytes([255])
        o += bytes([43, len(vend)]) + bytes(vend) + bytes([255])
        return bytes(pkt) + bytes(o)
    if msg_type == 2:
        desc = b"PXE toolkit"
        vend = bytearray([6, 1, 0x07])
        vend += bytes([8, 7]) + struct.pack("!HB", 0x8000, 1) + socket.inet_aton(MY_IP)
        vend += bytes([9, 3 + len(desc)]) + struct.pack("!HB", 0x8000, len(desc)) + desc
        vend += bytes([10, 4, 0]) + b"PXE" + bytes([255])
    else:
        item, ov, j = b"\x80\x00\x00\x00", opts.get(43, b""), 0
        while j + 1 < len(ov) and ov[j] != 255:
            if ov[j] == 71 and ov[j + 1] >= 4:
                item = ov[j + 2:j + 6]
            j += 2 + ov[j + 1]
        vend = bytes([71, 4]) + item + bytes([255])
    o += bytes([43, len(vend)]) + bytes(vend)
    o += bytes([66, len(sname)]) + sname + bytes([67, len(fb)]) + fb + bytes([255])
    return bytes(pkt) + bytes(o)


# ---------------------------------------------------------------- real-time boot steps + machine updates (live.py)
# Every network-boot step of a machine is pushed to the consoles at once and kept in the database (boot_events):
#   dhcp (the PC asked, we answered) -> loader (boot loader fetched) -> menu (its boot menu) -> ramos (RAM OS loading)
#   -> online (RAM OS up, agent says hello) -> report (hardware report in)
# TFTP/HTTP requests carry no MAC: the PC's address is learned from its own boot request (port 4011) and its menu fetch.
BOOT_STEP, IP2MAC = {}, {}


def boot_step(mac, step, detail=""):
    if not mac:
        return
    prev = BOOT_STEP.get(mac)
    now = time.time()
    if prev and prev[0] == step and now - prev[1] < 120:     # same step again (retries, tsize probes): quiet
        return
    BOOT_STEP[mac] = (step, now)
    try:
        db.boot_event(mac, step, detail)
    except Exception:                                      # noqa: BLE001 - history must never break a boot
        pass
    live.publish({"t": "boot", "mac": mac, "step": step, "detail": detail, "at": int(now)})


_DIRTY, _DIRTY_LOCK, _NEW = set(), threading.Lock(), set()
QUIET_FIELDS = {"agent_seen", "ep_seen"}                   # heartbeats: the console ages them itself


def on_machine_change(mac, fields, created):
    """db.upsert_machine hook: remember the machine; the flush loop sends one compact update per machine."""
    if not created and fields and fields <= QUIET_FIELDS:
        return
    with _DIRTY_LOCK:
        _DIRTY.add(mac)
        if created:
            _NEW.add(mac)


def live_flush_loop():
    """Every 0.3 s: one {"t":"m"} per changed machine with the same summary the console lists - consoles update in place
    instead of all re-reading the overview. Also prunes the boot history once a day."""
    pruned = 0
    while True:
        time.sleep(0.3)
        with _DIRTY_LOCK:
            macs, new = list(_DIRTY), set(_NEW)
            _DIRTY.clear()
            _NEW.clear()
        for mac in macs:
            try:
                row = db.machine_row(mac)
                if row:
                    live.publish({"t": "m", "mac": mac, "s": webui.summary(row), "new": mac in new})
            except Exception as e:                         # noqa: BLE001
                log(f"[live] update {mac}: {e!r}")
        if int(time.time()) % 300 == 0:                    # every 5 minutes: keep the write-ahead log small
            db.checkpoint()
        if time.time() - pruned > 86400:
            pruned = time.time()
            try:
                db.prune_boot_events(int(CFG.get("boot_history_days", 90)))
            except Exception:                              # noqa: BLE001
                pass
            try:
                import drivers_catalog                     # daily: opt-in ('share') servers pull the signed driver
                drivers_catalog.auto_sync()                # catalog from the CDN (no-op otherwise; verifies before caching)
            except Exception:                              # noqa: BLE001
                pass


LIBRARY_EXT = {"iso": (".iso",), "image": (".tar.zst", ".wim", ".img", ".qcow2", ".vhdx")}


def library_watch_loop():
    """OS library, live: every 5 s new / changed / removed ISOs and OS images (copied in by SCP, SMB, the USB stick, a
    capture job ...) are announced to every console - once a file's size has stopped changing for 10 s, so a large ISO
    still being copied is never offered half-written. The lists themselves are always read live: no restart needed."""
    dirs = {"iso": os.path.join(HTTP_ROOT, "iso"), "image": os.path.join(HTTP_ROOT, "images")}

    def scan():
        out = {}
        for kind, d in dirs.items():
            try:
                for f in os.listdir(d):
                    if f.startswith(".") or not f.lower().endswith(LIBRARY_EXT[kind]):
                        continue
                    st = os.stat(os.path.join(d, f))
                    out[(kind, f)] = (st.st_size, int(st.st_mtime))
            except OSError:
                pass
        return out
    known, pending = scan(), {}
    while True:
        time.sleep(5)
        cur = scan()
        for k, v in cur.items():
            if known.get(k) == v:
                pending.pop(k, None)
                continue
            last = pending.get(k)
            if last and last[0] == v:                      # unchanged since the last look: complete
                pending.pop(k, None)
                change = "changed" if k in known else "added"
                known[k] = v
                log(f"[library] {k[0]} {change}: {k[1]} ({v[0] / 1e9:.1f} GB)")
                live.publish({"t": "library", "kind": k[0], "name": k[1], "size": v[0], "change": change})
            else:
                pending[k] = (v, time.time())              # new or still growing: look again in 5 s
        for k in [k for k in known if k not in cur]:
            del known[k]
            log(f"[library] {k[0]} removed: {k[1]}")
            live.publish({"t": "library", "kind": k[0], "name": k[1], "size": 0, "change": "removed"})


_TICK = {}


def job_tick(mac, jid, text):
    """Job progress for every console (overview bars, at most every 2 s per job): just the step number + last line.
    The full log goes only to consoles that have this job open (topic job:<id>)."""
    now = time.time()
    if now - _TICK.get(jid, 0) < 2:
        return
    _TICK[jid] = now
    steps = re.findall(r">>> \[(\d+)\]", text[-20000:])
    last = next((ln for ln in reversed(text[-4000:].splitlines()) if ln.strip()), "")[:160]
    live.publish({"t": "jobtick", "mac": mac, "job": jid, "step": int(steps[-1]) if steps else 0, "line": last})


_IPXE_CHAINED = {}


def boot_file_for(mac, arch, is_ipxe, is_http=False, ipxe_http=True):
    # DHCP option 93 (client system architecture) picks the right network boot program:
    #   7/16 x64 UEFI · 11/19 ARM64 UEFI · 6/9/15 x86 UEFI · 10/18 ARM32 UEFI · 0 legacy BIOS  (the odd one of each pair = HTTP)
    if arch in (11, 19):                    # ARM64 UEFI (aarch64): Debian-signed aa64 shim -> GRUB (Secure Boot compatible)
        if is_http:
            return f"http://{MY_IP}:{HTTP_PORT}/" + CFG.get("boot_file_http_aa64", "http/grubaa64.efi")
        return CFG.get("boot_file_uefi_aa64", "debian-installer/arm64/bootnetaa64.efi")
    if arch in (6, 15):                    # 32-bit (IA32) UEFI: its own signed shim+grub. A 64-bit CPU with 32-bit firmware
        if is_http:                        # (Bay Trail etc.) boots a 64-bit kernel from this; a 32-bit-only CPU gets the i386 Core.
            return f"http://{MY_IP}:{HTTP_PORT}/" + CFG.get("boot_file_http_ia32", "http/grubia32.efi")
        return CFG.get("boot_file_uefi_ia32", "debian-installer/i386/bootnetia32.efi")
    if is_http:   # x64 UEFI HTTP Boot (RFC 5970): the firmware fetches the loader over HTTP, not TFTP. Hand it a full URL.
        return f"http://{MY_IP}:{HTTP_PORT}/" + CFG.get("boot_file_http", "http/grubnetx64.efi")
    if is_ipxe and not ipxe_http and arch in (0, 7, 9) and time.time() - _IPXE_CHAINED.get(mac, 0) > 120:
        # an iPXE built without HTTP (some NIC / hypervisor ROMs): load OUR iPXE over TFTP first - it has HTTP and comes
        # straight back for the script below (at most once per 2 min, so a build that lies can't loop)
        _IPXE_CHAINED[mac] = time.time()
        return CFG.get("boot_file_bios", "ipxe/undionly.kpxe") if arch == 0 else "ipxe/ipxe.efi"
    if is_ipxe:   # our iPXE build ignores this and fetches /boot.ipxe itself; harmless for others
        return f"http://{MY_IP}:{HTTP_PORT}/boot.ipxe"
    if arch in (7, 9, 10):                 # x64 UEFI (ARM32 at 10 is unsupported; it gets the x64 NBP, which it ignores)
        m = machine(mac) or {}
        return "ipxe/ipxe.efi" if m.get("chain") == "ipxe" else CFG.get("boot_file_uefi", "debian-installer/amd64/bootnetx64.efi")
    return CFG.get("boot_file_bios", "ipxe/undionly.kpxe")   # legacy BIOS (iPXE undionly, not pxelinux)


# ---------------------------------------------------------------- own DHCP (advanced, OFF by default)
# Default: proxyDHCP only - KwickBoot never hands out addresses, the site's DHCP server stays in charge.
# config.json "own_dhcp": {"enabled": true, "range": "10.88.0.100-10.88.0.200", "lease_s": 14400,
#                          "router": "", "dns": [], "discover": false}   discover: register every PC that boots there (test lab)
# turns on a small DHCP server on the server's own card only (SO_BINDTODEVICE) - for an isolated test lab or a
# network without any DHCP. NEVER turn it on where another DHCP server answers: two servers hand out clashing addresses.
OWN_LOCK = threading.Lock()


def own_dhcp_cfg():
    c = CFG.get("own_dhcp") or {}
    return c if c.get("enabled") and c.get("range") else None


def _own_leases_path():
    return os.path.join(BASE, "data", "own-dhcp-leases.json")


def _own_leases():
    try:
        with open(_own_leases_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def own_lease(mac, want=None):
    """Same address for the same MAC (kept in data/own-dhcp-leases.json); else the requested one if free; else the first free."""
    import ipaddress
    c = own_dhcp_cfg()
    a, b = (ipaddress.ip_address(x.strip()) for x in c["range"].split("-"))
    with OWN_LOCK:
        L = _own_leases()
        if mac in L:
            return L[mac]["ip"]
        used = {v["ip"] for v in L.values()} | {MY_IP}
        pick = None
        if want:
            try:
                w = ipaddress.ip_address(want)
                if a <= w <= b and want not in used:
                    pick = want
            except ValueError:
                pass
        ip = a
        while pick is None and ip <= b:
            if str(ip) not in used:
                pick = str(ip)
            ip += 1
        if pick is None:
            log(f"[owndhcp] range {c['range']} is full - no address for {mac}")
            return None
        L[mac] = {"ip": pick, "t": int(time.time())}
        tmp = _own_leases_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(L, f, indent=1)
        os.replace(tmp, _own_leases_path())
        return pick


def build_lease(req, opts, msg_type, ip, boot_file):
    """A full DHCP answer (address + optional PXE boot file) - own DHCP only."""
    import ipaddress
    c = own_dhcp_cfg()
    net = ipaddress.ip_interface(f"{MY_IP}/{c.get('prefix', 24)}").network
    pkt = bytearray(240)
    pkt[0], pkt[1], pkt[2] = 2, 1, 6
    pkt[4:8] = req[4:8]
    pkt[10:12] = req[10:12]
    pkt[16:20] = socket.inet_aton(ip) if msg_type in (2, 5) else b"\0\0\0\0"
    pkt[20:24] = socket.inet_aton(MY_IP)
    pkt[28:44] = req[28:44]
    sname = MY_IP.encode()
    pkt[44:44 + len(sname)] = sname
    fb = (boot_file or "").encode()
    pkt[108:108 + len(fb)] = fb
    pkt[236:240] = b"\x63\x82\x53\x63"
    o = bytearray([53, 1, msg_type]) + bytes([54, 4]) + socket.inet_aton(MY_IP)
    if msg_type in (2, 5):
        lease = int(c.get("lease_s", 14400))
        o += bytes([51, 4]) + struct.pack("!I", lease) + bytes([58, 4]) + struct.pack("!I", lease // 2)
        o += bytes([1, 4]) + socket.inet_aton(str(net.netmask))
        if c.get("router"):
            o += bytes([3, 4]) + socket.inet_aton(c["router"])
        dns = [d for d in c.get("dns") or [] if d][:3]
        if dns:
            o += bytes([6, 4 * len(dns)]) + b"".join(socket.inet_aton(d) for d in dns)
        if boot_file:
            o += bytes([60, 9]) + b"PXEClient"
            if 97 in opts:
                o += bytes([97, len(opts[97])]) + opts[97]
            o += bytes([66, len(sname)]) + sname + bytes([67, len(fb)]) + fb
    o += bytes([255])
    return bytes(pkt) + bytes(o)


def handle_own_dhcp(sock, data, opts, mac, port):
    """Address for every PC on the server's card; known PXE clients also get the boot file in the same answer."""
    mtype = opts.get(53, b"\x00")[0]
    if mtype not in (1, 3):
        return
    if mtype == 3:
        sid = opts.get(54)
        if sid and sid != socket.inet_aton(MY_IP):
            return                                     # the PC chose another server's offer
    pxe = opts.get(60, b"").startswith(b"PXEClient")
    boot = None
    if pxe:
        m = known_or_discover(mac) or (register_new(mac) if own_dhcp_cfg().get("discover") else None)   # lab: every PC there is a test PC
        if m:
            arch = struct.unpack("!H", opts[93][:2])[0] if 93 in opts and len(opts[93]) >= 2 else 0
            boot = boot_file_for(mac, arch, opts.get(77, b"") == b"iPXE")
    want = socket.inet_ntoa(opts[50]) if len(opts.get(50, b"")) == 4 else (socket.inet_ntoa(data[12:16]) if data[12:16] != b"\0\0\0\0" else None)
    ip = own_lease(mac, want)
    if not ip:
        return
    if mtype == 3 and want and want != ip:
        sock.sendto(build_lease(data, opts, 6, ip, None), ("255.255.255.255", 68))
        log(f"[owndhcp] NAK {mac} asked {want}, has {ip}")
        return
    sock.sendto(build_lease(data, opts, 2 if mtype == 1 else 5, ip, boot), ("255.255.255.255", 68))
    log(f"[owndhcp] {'OFFER' if mtype == 1 else 'ACK'} {mac} {ip}" + (f" file={boot}" if boot else ""))
    if boot:
        IP2MAC[ip] = mac
        boot_step(mac, "dhcp", "own DHCP " + ip)


def handle_dhcp(sock, data, addr, port):
    if len(data) < 240 or data[0] != 1:
        return
    mac = ":".join(f"{b:02x}" for b in data[28:34])
    opts = parse_options(data)
    if port == 67 and own_dhcp_cfg():
        return handle_own_dhcp(sock, data, opts, mac, port)
    vclass = opts.get(60, b"")
    is_http = vclass.startswith(b"HTTPClient")      # UEFI HTTP Boot client (vs PXEClient = TFTP)
    is_uboot = vclass.startswith(b"U-Boot")         # U-Boot ARM SBC (distro/pxe boot) - fetches pxelinux.cfg/ over TFTP
    if not (vclass.startswith(b"PXEClient") or is_http or is_uboot):
        for tag, what in ((b"AAPLBSDPC", "Apple NetBoot (BSDP)"), (b"Etherboot", "Etherboot"), (b"gPXE", "gPXE"),
                          (b"ArmPC", "ARM PC firmware")):
            if vclass.startswith(tag):
                note_once(("vclass", mac), f"[dhcp:{port}] {mac} asks to network-boot as {what} "
                          f"({vclass[:40].decode(errors='replace')}) - not supported, not answered")
        return
    m = known_or_discover(mac)
    if not m:
        log(f"[dhcp:{port}] ignored unknown {mac} (open discovery: pxectl discover 30)")
        return
    mtype = opts.get(53, b"\x00")[0]
    arch = client_arch(opts)
    is_ipxe = opts.get(77, b"") == b"iPXE"
    if arch == 0 and not (is_ipxe or is_http or is_uboot) and is_raspberry_pi(mac):
        if not pi_files_present():
            note_once(("pi", mac), f"[dhcp:{port}] Raspberry Pi {mac} wants to network-boot - no Pi boot tree on this server: "
                      f"run linux/build-pi-boot.sh (KwickBoot's arm64 Core for Pi 3/4/5) or put your own in {PI_DIR}/; not answered")
            return
        if current_action(mac, consume=False) == "local":   # silence = the Pi moves on to its next BOOT_ORDER entry (SD / USB)
            note_once(("pi-local", mac), f"[dhcp:{port}] Raspberry Pi {mac} is set to boot locally - not answered "
                      f"(it falls through to its SD card / USB)", every=600)
            return
        if mtype == 1:
            reply = build_reply(data, opts, 2, "", False, "rpi")
            for dst in ("255.255.255.255", MY_IP.rsplit(".", 1)[0] + ".255"):
                sock.sendto(reply, (dst, 68))
            log(f"[dhcp:{port}] OFFER {mac} ({m.get('name')}) Raspberry Pi -> tftp {pi_root()}/ style=rpi")
            boot_step(mac, "dhcp", "Raspberry Pi")
        return
    feats = ipxe_features(opts) if is_ipxe else {}
    boot = boot_file_for(mac, arch, is_ipxe, is_http, ipxe_http=(not feats) or (19 in feats))
    if mtype == 1:
        style = "menu" if (is_http or is_ipxe or is_uboot) else offer_style(mac, bytes(data[4:8]), arch)
        reply = build_reply(data, opts, 2, boot, is_http, style)
        for dst in ("255.255.255.255", MY_IP.rsplit(".", 1)[0] + ".255"):
            sock.sendto(reply, (dst, 68))
        log(f"[dhcp:{port}] OFFER {mac} ({m.get('name')}) arch={arch}{' HTTP' if is_http else ' iPXE' if is_ipxe else ''} file={boot}"
            + (" (iPXE without HTTP: chain to ours)" if is_ipxe and not boot.startswith("http") else "")
            + ("" if (is_http or is_ipxe or is_uboot) else f" style={style}"))
        boot_step(mac, "dhcp", ("HTTP" if is_http else "iPXE" if is_ipxe else "UEFI" if arch in (6, 7, 9, 10, 11) else "BIOS"))
    elif mtype == 3 and port == 67 and not (is_http or is_ipxe or is_uboot):
        offer_style_accepted(mac, bytes(data[4:8]))      # the PC's DHCP REQUEST (to whichever DHCP server it chose)
    elif mtype == 3 and port == 4011:
        sock.sendto(build_reply(data, opts, 5, boot, is_http), addr)
        log(f"[dhcp:{port}] ACK {mac} file={boot}")
        IP2MAC[addr[0]] = mac


def dhcp_loop(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    if IS_LINUX and port == 67:
        if IFACE:
            s.setsockopt(socket.SOL_SOCKET, 25, (IFACE + "\0").encode())   # SO_BINDTODEVICE
        s.bind(("0.0.0.0", port))
    else:
        s.bind((MY_IP, port))
    od = own_dhcp_cfg() if port == 67 else None
    if od and not IFACE:
        log("own DHCP is on but the server's card is unknown - refusing to hand out addresses on every card")
        CFG["own_dhcp"]["enabled"] = False
        od = None
    log(f"proxyDHCP listening on {MY_IP}:{port}" + (f" (iface {IFACE})" if IFACE and port == 67 else "")
        + (f" + OWN DHCP {od['range']} on {IFACE}" if od else ""))
    while True:
        try:
            data, addr = s.recvfrom(4096)
            handle_dhcp(s, data, addr, port)
        except Exception as e:
            log(f"[dhcp:{port}] error {e!r}")


# ---------------------------------------------------------------- watch other DHCP servers (read-only)
# The site router's / another server's PXE settings silently win over a proxyDHCP on many PCs (opt 66/67 or a boot file,
# a next-server, opt 60 "PXEClient", WDS/SCCM/dnsmasq answering PXE too). Their replies to clients are sniffed with a
# kernel BPF filter (AF_PACKET, CAP_NET_RAW - no port is bound, the server's own DHCP client is not disturbed) and every
# such setting is logged as a warning and kept for the console (setting "dhcp_conflicts").
_BPF_UDP_67_TO_68 = [   # tcpdump -dd "ip and udp src port 67 and udp dst port 68" (Ethernet)
    (0x28, 0, 0, 0x0000000c), (0x15, 0, 10, 0x00000800), (0x30, 0, 0, 0x00000017), (0x15, 0, 8, 0x00000011),
    (0x28, 0, 0, 0x00000014), (0x45, 6, 0, 0x00001fff), (0xb1, 0, 0, 0x0000000e), (0x48, 0, 0, 0x0000000e),
    (0x15, 0, 3, 0x00000043), (0x48, 0, 0, 0x00000010), (0x15, 0, 1, 0x00000044), (0x6, 0, 0, 0x00040000),
    (0x6, 0, 0, 0x00000000)]
DHCP_CONFLICTS = {}
DHCP_WATCH = {"on": False, "error": ""}      # for the console: is the watcher running, and why not


def dhcp_conflicts_current(max_age=86400):
    """Other DHCP servers seen handing out network-boot settings in the last day: [{server, issues, seen}]."""
    now_ = time.time()
    return [{"server": k, "issues": v.get("issues", []), "seen": v.get("seen", 0)}
            for k, v in sorted(DHCP_CONFLICTS.items()) if now_ - v.get("seen", 0) < max_age]


def check_other_dhcp_reply(src, d):
    opts = parse_options(d)
    if opts.get(53, b"\x00")[0] not in (2, 5) or d[236:240] != b"\x63\x82\x53\x63":
        return
    sid = socket.inet_ntoa(opts[54]) if len(opts.get(54, b"")) == 4 else src
    if sid == MY_IP:
        return
    issues = []
    if 66 in opts:
        issues.append(f"opt 66 next-server '{opts[66].decode(errors='replace').strip(chr(0))}'")
    if 67 in opts:
        issues.append(f"opt 67 boot file '{opts[67].decode(errors='replace').strip(chr(0))}'")
    fname = d[108:236].split(b"\0")[0]
    if fname and 67 not in opts:
        issues.append(f"boot file '{fname.decode(errors='replace')}'")
    siaddr = socket.inet_ntoa(d[20:24])
    if siaddr not in ("0.0.0.0", sid, MY_IP):
        issues.append(f"next-server (siaddr) {siaddr}")
    if opts.get(60, b"").startswith(b"PXEClient"):
        issues.append("another PXE server answers PXE clients (WDS / SCCM / dnsmasq?)" if d[16:20] == b"\0\0\0\0"
                      else "opt 60 PXEClient in its address offers")
    if 175 in opts:
        issues.append("iPXE options (opt 175)")
    if not issues:
        if sid in DHCP_CONFLICTS:          # it was fixed: its replies are clean now
            DHCP_CONFLICTS.pop(sid, None)
            for k in [k for k in _NOTED if isinstance(k, tuple) and k[:2] == ("dhcpwatch", sid)]:
                _NOTED.pop(k, None)        # warn again at once if it starts handing them out again
            log(f"[dhcpwatch] DHCP server {sid} no longer hands out network-boot settings - warning cleared")
            _save_dhcp_conflicts()
        return
    DHCP_CONFLICTS[sid] = {"issues": issues, "seen": time.time()}
    note_once(("dhcpwatch", sid, tuple(issues)), f"[dhcpwatch] WARNING DHCP server {sid} hands out network-boot settings: "
              + "; ".join(issues) + " - many PCs then ignore KwickBoot. Remove them from that server (KwickBoot needs "
              "none), or point them at KwickBoot", every=6 * 3600)
    _save_dhcp_conflicts()


def _save_dhcp_conflicts():
    try:
        db.set_setting("dhcp_conflicts", json.dumps(DHCP_CONFLICTS))
    except Exception:
        pass


def dhcp_conflict_dismiss(server):
    """An admin dismissed the warning (e.g. that server is gone). It comes back if the server is seen doing it again."""
    hit = DHCP_CONFLICTS.pop(server, None) is not None
    for k in [k for k in _NOTED if isinstance(k, tuple) and k[:2] == ("dhcpwatch", server)]:
        _NOTED.pop(k, None)
    _save_dhcp_conflicts()
    return hit


def dhcp_watch_loop():
    import ctypes
    try:
        s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0800))
        s.bind((IFACE, 0))
        code = b"".join(struct.pack("HBBI", *ins) for ins in _BPF_UDP_67_TO_68)
        buf = ctypes.create_string_buffer(code, len(code))
        s.setsockopt(socket.SOL_SOCKET, 26, struct.pack("HL", len(_BPF_UDP_67_TO_68), ctypes.addressof(buf)))   # SO_ATTACH_FILTER
    except Exception as e:
        DHCP_WATCH["error"] = repr(e)
        log(f"[dhcpwatch] not watching other DHCP servers ({e!r}) - needs CAP_NET_RAW")
        return
    try:                                   # what was seen before a restart (the console keeps showing it for a day)
        DHCP_CONFLICTS.update(json.loads(db.get_setting("dhcp_conflicts", "{}") or "{}"))
    except Exception:
        pass
    DHCP_WATCH["on"] = True
    log(f"[dhcpwatch] watching other DHCP servers' replies on {IFACE}")
    while True:
        try:
            pkt = s.recv(4096)
            ihl = (pkt[14] & 0x0F) * 4
            src = socket.inet_ntoa(pkt[26:30])
            d = pkt[14 + ihl + 8:]
            if src != MY_IP and len(d) >= 240 and d[0] == 2:
                check_other_dhcp_reply(src, d)
        except Exception as e:
            note_once("dhcpwatch-err", f"[dhcpwatch] error {e!r}")


# ---------------------------------------------------------------- TFTP
TFTP_WINDOW_MAX = 32      # RFC 7440: up to this many blocks in flight per acknowledgement (when the client asks)


def tftp_send(client, payload, label, opts):
    """payload: file path (str) or generated content (bytes). Options: blksize (RFC 2348, capped to one Ethernet frame),
    tsize (RFC 2349), windowsize (RFC 7440: several blocks per ACK - a lot faster where firmware supports it; lost blocks
    are re-sent from the last acknowledged one)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((MY_IP, 0))
    s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
    s.settimeout(2)
    try:
        size = len(payload) if isinstance(payload, bytes) else os.path.getsize(payload)
        blksize, oack, window = 512, {}, 1
        if "blksize" in opts:
            # never more than asked; capped below one Ethernet frame AND below 1432: VMware EFI cannot reassemble IP
            # fragments and breaks above that, routed / tunnelled paths drop 1468 (config "tftp_blksize_max")
            blksize = max(8, min(int(opts["blksize"]), int(CFG.get("tftp_blksize_max", 1432))))
            oack["blksize"] = str(blksize)
        if "timeout" in opts:              # RFC 2349: echo it exactly (EDK2 rejects an OACK with another value)
            try:
                t = int(opts["timeout"])
                if 1 <= t <= 255:
                    oack["timeout"] = str(t)
                    s.settimeout(min(t, 10))
            except ValueError:
                pass
        if "tsize" in opts:
            oack["tsize"] = str(size)
        if "windowsize" in opts:
            try:
                window = max(1, min(int(opts["windowsize"]), TFTP_WINDOW_MAX))
                oack["windowsize"] = str(window)
            except ValueError:
                window = 1

        def send_wait(pkt, block):
            for _ in range(6):
                s.sendto(pkt, client)
                try:
                    while True:
                        resp, a = s.recvfrom(1024)
                        if a != client:
                            continue
                        op = struct.unpack("!H", resp[:2])[0]
                        if op == 5:
                            return "error"
                        if op == 4 and struct.unpack("!H", resp[2:4])[0] == block:
                            return "ok"
                except socket.timeout:
                    continue
            return "timeout"

        if oack:
            pkt = b"\x00\x06" + b"".join(k.encode() + b"\0" + v.encode() + b"\0" for k, v in oack.items())
            if send_wait(pkt, 0) != "ok":
                return
        t0 = time.time()
        if isinstance(payload, str):
            with open(payload, "rb") as f:
                data = f.read()
        else:
            data = payload
        nblocks = len(data) // blksize + 1            # the last block is short (possibly empty): end of file
        blk = lambda n: struct.pack("!HH", 3, n & 0xFFFF) + data[(n - 1) * blksize:n * blksize]
        if window == 1:                               # classic lock-step (every firmware)
            for n in range(1, nblocks + 1):
                if send_wait(blk(n), n & 0xFFFF) != "ok":
                    log(f"[tftp] {label}: aborted at block {n}")
                    return
        else:                                         # RFC 7440 window
            acked, tries = 0, 0
            while acked < nblocks:
                end = min(acked + window, nblocks)
                for n in range(acked + 1, end + 1):
                    s.sendto(blk(n), client)
                try:
                    while True:
                        resp, a = s.recvfrom(1024)
                        if a != client or len(resp) < 4:
                            continue
                        op, k = struct.unpack("!HH", resp[:4])
                        if op == 5:
                            return
                        if op == 4:
                            rel = (k - acked) & 0xFFFF   # block numbers wrap at 65536
                            if 0 < rel <= end - acked:
                                acked += rel
                                tries = 0
                                break
                except socket.timeout:
                    tries += 1
                    if tries > 6:
                        log(f"[tftp] {label}: aborted at block {acked + 1} (window {window})")
                        return
        log(f"[tftp] sent {label} {size/1e6:.1f} MB in {time.time()-t0:.1f}s to {client[0]}" + (f" (window {window})" if window > 1 else ""))
    except Exception as e:
        log(f"[tftp] error {e!r}")
    finally:
        s.close()


GRUB_MAC_PREFIX = "debian-installer/amd64/grub/mac/"


def _ci_path(root, rel):
    """Case-insensitive lookup of rel under root (DOS/Windows-era clients upper-case names); None if absent."""
    cur = root
    for part in [x for x in rel.split("/") if x]:
        try:
            hit = next((e for e in os.listdir(cur) if e.lower() == part.lower()), None)
        except OSError:
            return None
        if hit is None:
            return None
        cur = os.path.join(cur, hit)
    return cur


def tftp_resolve(fname):
    """Path a TFTP name maps to: exact; else case-insensitive; else the Raspberry Pi tree (tftp/rpi/, the Pi asks for
    <serial>/start4.elf then start4.elf). Always inside TFTP_ROOT (realpath-checked by the caller)."""
    full = os.path.realpath(os.path.join(TFTP_ROOT, fname))
    if os.path.isfile(full):
        return full
    cands = [fname]
    parts = fname.split("/", 1)
    serial = len(parts) == 2 and re.fullmatch(r"[0-9a-fA-F]{8}", parts[0])
    if os.path.isdir(PI_DIR):
        cands += ["rpi/" + fname] + (["rpi/" + parts[1]] if serial else [])
    if serial:                              # Pi 4/5 ask <serial>/start4.elf first: the shared tree at the TFTP root
        cands.append(parts[1])
    for c in cands:
        hit = _ci_path(TFTP_ROOT, c)
        if hit and os.path.isfile(hit):
            return os.path.realpath(hit)
    return full


def tftp_loop():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((MY_IP, 69))
    log(f"TFTP listening on {MY_IP}:69")
    cache = {}   # (client ip, name) -> (time, bytes): tsize probe + real request must get identical content
    while True:
        data, client = s.recvfrom(2048)
        if struct.unpack("!H", data[:2])[0] != 1:
            continue
        parts = data[2:].split(b"\0")
        fname = parts[0].decode(errors="replace").replace("\\", "/").lstrip("/")
        opts = {parts[i].decode().lower(): parts[i + 1].decode() for i in range(2, len(parts) - 1, 2) if parts[i]}
        if fname.startswith(GRUB_MAC_PREFIX) and fname.endswith(".cfg"):
            mac = norm_mac(fname[len(GRUB_MAC_PREFIX):-4])
            key = (client[0], fname)
            if key in cache and time.time() - cache[key][0] < 30:
                content = cache[key][1]
            elif known_or_discover(mac) and (g := grub_for(mac)) is not None:
                content = g.encode()
                cache[key] = (time.time(), content)
            else:
                s.sendto(struct.pack("!HH", 5, 1) + b"File not found\0", client)
                continue
            IP2MAC[client[0]] = mac
            boot_step(mac, "menu")
            threading.Thread(target=tftp_send, args=(client, content, fname, opts), daemon=True).start()
            continue
        pi_name = fname.split("/", 1)[1] if re.fullmatch(r"[0-9a-fA-F]{8}/cmdline\.txt", fname) else fname
        if pi_name == "cmdline.txt" and pi_files_present():  # a Raspberry Pi: its own command line with this boot's token
            key = (client[0], fname)
            mac = mac_for_ip(client[0])
            if key in cache and time.time() - cache[key][0] < 30:
                content = cache[key][1]
            else:
                content = pi_cmdline(mac if mac and known_or_discover(mac) else "").encode()
                cache[key] = (time.time(), content)
            if mac:
                IP2MAC[client[0]] = mac
                boot_step(mac, "menu", "Raspberry Pi cmdline")
            threading.Thread(target=tftp_send, args=(client, content, fname, opts), daemon=True).start()
            continue
        if fname.startswith("pxelinux.cfg/"):      # U-Boot ARM SBC distro/pxe boot: generate the extlinux.conf on the fly
            name = fname[len("pxelinux.cfg/"):]
            mac = norm_mac(name[3:].replace("-", ":")) if name[:3].lower() == "01-" else ""
            if not mac:
                mac = IP2MAC.get(client[0], "")
            cfg = uboot_cfg(mac) if (mac and known_or_discover(mac)) else None
            if cfg is None:
                s.sendto(struct.pack("!HH", 5, 1) + b"File not found\0", client)
                continue
            IP2MAC[client[0]] = mac
            boot_step(mac, "menu")
            threading.Thread(target=tftp_send, args=(client, cfg.encode(), fname, opts), daemon=True).start()
            continue
        full = tftp_resolve(fname)
        if not full.startswith(TFTP_ROOT) or not os.path.isfile(full) or full.endswith(".tmpl"):
            if "revocations" not in fname and "shim_certificate" not in fname:
                log(f"[tftp] {client[0]} missing: {fname}")
            s.sendto(struct.pack("!HH", 5, 1) + b"File not found\0", client)
            continue
        if fname.endswith((".efi", ".kpxe", ".0")):          # repeats (size probe + transfer) are merged by boot_step
            boot_step(IP2MAC.get(client[0]), "loader", os.path.basename(fname))
        threading.Thread(target=tftp_send, args=(client, full, os.path.relpath(full, TFTP_ROOT), opts), daemon=True).start()


# ---------------------------------------------------------------- HTTP
class Handler(SimpleHTTPRequestHandler):
    def capture_upload(self, mac, q):
        """One file of a running capture job, streamed raw after an upgrade (no size known in advance): written to
        images/captures/<name>/<file>.part with SHA-256 on the fly, renamed when complete, verdict sent back."""
        import hashlib as _h, re as _re
        if not token_ok(mac, self.headers.get("X-PXE-Token", "")):
            self.send_error(403)
            return
        job = db.q1("SELECT * FROM jobs WHERE id=? AND mac=? AND type IN ('capture','winprep') AND state='running'",
                    os.path.basename(q.get("job", [""])[0]), mac)
        fname = q.get("file", [""])[0]
        jp = json.loads(job["params"] or "{}") if job else {}
        if job and job["type"] == "winprep":
            # prepared Windows edition (winprep.sh on a client's CPU): exactly <EDITION>.wim into the ISO's winimg folder
            name = f"winimg/{jp.get('ISO', '')}"
            ok = jp.get("ISO") in iso_list() and jp.get("EDITION") in WIN_EDITIONS and fname == jp["EDITION"] + ".wim"
            d = os.path.join(HTTP_ROOT, "iso", "winimg", jp.get("ISO", "x"))
        else:
            name = jp.get("NAME", "")
            ok = bool(_re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,60}", name))
            d = os.path.join(HTTP_ROOT, "images", "captures", name)
        if not job or not ok or not _re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}", fname):
            self.send_error(403)
            return
        os.makedirs(d, exist_ok=True)
        part, final = os.path.join(d, fname + ".part"), os.path.join(d, fname)
        reserve = 1_500_000_000                    # never fill the server's disk
        self.protocol_version = "HTTP/1.1"
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "kbpty")
        self.send_header("Connection", "Upgrade")
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        h, n, err = _h.sha256(), 0, ""
        try:
            with open(part, "wb") as f:
                while True:
                    b = self.rfile.read(1 << 20)
                    if not b:
                        break
                    h.update(b); f.write(b); n += len(b)
                    if n % (256 << 20) < len(b):      # every ~256 MB: still room?
                        st = os.statvfs(d)
                        if st.f_bavail * st.f_frsize < reserve:
                            err = "server disk full (1.5 GB reserve reached)"
                            break
        except OSError as e:
            err = str(e)
        if err:
            try:
                os.remove(part)
            except OSError:
                pass
            self.wfile.write(f"FAIL {err}\n".encode())
        else:
            try:
                os.chmod(part, 0o644)
            except OSError:                # exFAT library disk: permissions come from the mount
                pass
            os.replace(part, final)
            if job["type"] == "winprep":
                with open(final + ".sha256", "w") as f:
                    f.write(f"{h.hexdigest()}  {fname}\n")
            self.wfile.write(f"OK {h.hexdigest()} {n}\n".encode())
        self.wfile.flush()
        log(f"[capture] {mac} {name}/{fname}: {'FAIL ' + err if err else f'{n // 1000000} MB'}")

    QUIET = ("/agent/watch?", "/agent/screen?", "/agent/screenfb?", "/agent/log?", "/agent/hello?", "/agent/job?", "/ep/checkin")

    def log_message(self, fmt, *args):
        line = fmt % args
        # the RAM OS and endpoints poll every few seconds: log those only when they fail
        if any(q in line for q in self.QUIET) and '" 200 ' in line + " ":
            return
        log(f"[http] {self.client_address[0]} {line}")

    def translate_path(self, path):
        p = urlparse(path).path
        if p.startswith("/tftp/"):           # serve boot files over HTTP too (much faster than TFTP)
            full = os.path.realpath(os.path.join(TFTP_ROOT, p[len("/tftp/"):]))
            return full if full.startswith(TFTP_ROOT) else HTTP_ROOT + "/__denied__"
        return super().translate_path(path)

    def send_text(self, body, ctype="text/plain; charset=utf-8"):
        b = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def setup(self):
        super().setup()
        try:                                  # big boot files: large send buffer, no Nagle delay
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

    def send_head(self):
        """Static files: Range support (resumable, partial) + zero-copy sendfile in copyfile()."""
        self._kb_range = None
        path = self.translate_path(self.path)
        if os.path.isdir(path) or not os.path.isfile(path) or path.endswith(TEMPLATED_EXT):
            return super().send_head()
        try:
            f = open(path, "rb")
        except OSError:
            self.send_error(404)
            return None
        size = os.fstat(f.fileno()).st_size
        start, end, code = 0, size - 1, 200
        import re as _re
        m = _re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
        if m and size and (m.group(1) or m.group(2)):
            if m.group(1):
                start, end = int(m.group(1)), int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end:
                f.close()
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Last-Modified", self.date_time_string(int(os.fstat(f.fileno()).st_mtime)))
        self.end_headers()
        self._kb_range = (start, end - start + 1)
        return f

    def copyfile(self, source, outputfile):
        rng = getattr(self, "_kb_range", None)
        if not rng:
            return super().copyfile(source, outputfile)
        start, count = rng
        try:
            self.wfile.flush()
            self.connection.sendfile(source, start, count)      # kernel zero-copy
        except (OSError, ValueError, AttributeError):
            source.seek(start)
            while count > 0:
                chunk = source.read(min(1024 * 1024, count))
                if not chunk:
                    break
                outputfile.write(chunk)
                count -= len(chunk)

    def list_directory(self, path):          # no directory listings
        self.send_error(403, "Directory listing disabled")
        return None

    def do_HEAD(self):                         # size checks (wget --spider): files inside ISOs too, the rest as before
        u = urlparse(self.path)
        if u.path.startswith("/isofile/"):
            return isofile_serve(self, u.path[len("/isofile/"):])
        return super().do_HEAD()

    def do_GET(self):
        if webui.handle(self, "GET"):
            return
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/kbcore/vmlinuz", "/alpine/vmlinuz-lts") or u.path.startswith("/kbcore/initrd"):
            boot_step(IP2MAC.get(self.client_address[0]), "ramos", u.path.rsplit("/", 1)[-1])
        elif u.path.startswith("/images/tools/") and u.path.endswith("/vmlinuz"):     # an OS library tool starting
            boot_step(IP2MAC.get(self.client_address[0]), "ramos", "tool " + u.path.split("/")[3])
        if u.path.startswith("/isofile/"):             # one file from inside an ISO in the library (WinPE boot)
            return isofile_serve(self, u.path[len("/isofile/"):])
        if u.path == "/ep/pin":                        # installer: where + which key the managed machine must trust
            port = int(CFG.get("ep_https_port", 8444) or 0)
            return self.send_text(f"{MY_IP}:{port} {ep_pin()}\n" if port else "off\n")
        if u.path == "/ep/checkin":                    # endpoint agent (kiosk / signage / lab), own per-machine token
            mac = norm_mac(q.get("mac", [""])[0])
            if not endpoint.token_ok(mac, self.headers.get("X-KB-EP", "")):
                self.send_error(403)
                return
            info = {k: q.get(k, [""])[0][:80] for k in ("ver", "profile", "up", "ip", "kiosk", "os", "kernel", "updates", "reboot", "users")}
            info = {k: v for k, v in info.items() if v}
            r = endpoint.checkin(mac, info)
            sh = q.get("fmt", [""])[0] == "sh"         # generic agent: plain "id cmd arg" lines, no JSON parser needed
            body = (endpoint.as_lines(r) if sh else json.dumps(r)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain" if sh else "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/agent/job":
            mac = norm_mac(q.get("mac", [""])[0])
            if not token_ok(mac, self.headers.get("X-PXE-Token", "")):
                log(f"[security] {self.client_address[0]} job request for {mac} with bad/missing token - refused")
                self.send_error(403)
                return
            self.send_text(next_job(mac) if known_or_discover(mac) else "{}")
            return
        if u.path == "/agent/oslist":          # installable-OS catalog for the on-machine GUI picker: "id|name|min_gb" lines
            mac = norm_mac(q.get("mac", [""])[0])   # filter by the machine's department policy (allowed_os); unknown mac -> unrestricted default
            try:
                pol = db.policy((machine(mac) or {}).get("department_id"))
                allowed = pol.get("allowed_os") or []
                cat = [r for r in webui.recipes() if r["id"] != "image" and (not allowed or r["id"] in allowed)]
            except Exception:
                cat = []
            self.send_text("".join("%s|%s|%d\n" % (r["id"], r["name"], r.get("min_gb", 0)) for r in cat))
            return
        if u.path == "/agent/toollist":        # OS-library tools catalog for the on-machine GUI rescue picker: "name|title|role|desc" lines
            try:
                cat = tools()
            except Exception:
                cat = []
            self.send_text("".join("%s|%s|%s|%s\n" % (t["NAME"], t.get("TITLE", t["NAME"]),
                                   t.get("ROLE") or "admin", (t.get("DESC", "") or "")[:120]) for t in cat))
            return
        if u.path in ("/local", "/local/"):     # the machine-scoped RICH-tier page (served to the on-machine browser; also opens anywhere for testing)
            try:
                with open(os.path.join(HTTP_ROOT, "local", "index.html"), "rb") as f:
                    self.send_text(f.read().decode("utf-8"), "text/html; charset=utf-8")
            except OSError:
                self.send_error(404)
            return
        if u.path == "/local/facts":            # machine-scoped facts for /local, by ?mac= or the requesting IP
            mac = norm_mac(q.get("mac", [""])[0]) or IP2MAC.get(self.client_address[0], "")
            m = (machine(mac) or {}) if mac else {}
            try:
                dk = webui.disks_from_inventory(mac) if mac else []
            except Exception:
                dk = []
            disklist = [{"dev": d["dev"].rsplit("/", 1)[-1], "size": d.get("size", ""),
                         "type": (d.get("tran") or "disk").upper(), "model": d.get("model", "")} for d in dk]
            disks = ["%s  %s  %s" % (d["dev"], d["size"], d["model"] or d["type"]) for d in disklist]
            dept = webui.dept_names().get(m.get("department_id")) if m.get("department_id") else ""
            nx = m.get("next_action") or ""
            status = (m.get("agent_msg") or "").strip() or ("Scheduled: %s on next boot." % nx if nx else "Idle - no job running.")
            # Live install progress, so the on-machine console mirrors an install started HERE or remotely from the web
            # console (they share the same job log). The install scripts emit ">>> [N] label  (+Ts)" step markers (1..8).
            jobinfo = None
            try:
                job = db.q1("SELECT id,type,state,params,created,finished FROM jobs WHERE mac=? AND type IN ('install','manual','restore','wipe') ORDER BY created DESC LIMIT 1", mac) if mac else None
                recent = False
                if job and job["state"] in ("done", "ok", "failed") and job["finished"]:
                    try:    # now() is a "YYYY-MM-DD HH:MM:SS" string (local time), not epoch; parse both sides
                        recent = (time.time() - time.mktime(time.strptime(job["finished"], "%Y-%m-%d %H:%M:%S"))) < 300
                    except Exception:
                        recent = False
                if job and (job["state"] == "running" or recent):
                    try:
                        jtext = open(os.path.join(jobs_dir(mac), job["id"] + ".log"), encoding="utf-8", errors="replace").read()
                    except Exception:
                        jtext = ""
                    marks = re.findall(r">>> \[(\d+)\] (.+)", jtext)
                    step = int(marks[-1][0]) if marks else 0
                    label = marks[-1][1].rsplit("  (+", 1)[0].strip() if marks else ""
                    line = next((ln for ln in reversed(jtext[-4000:].splitlines()) if ln.strip() and not ln.lstrip().startswith(">>>")), "")[:160]
                    pr = json.loads(job["params"]) if job.get("params") else {}
                    jobinfo = {"type": job["type"], "state": job["state"], "step": step, "total": 8,
                               "label": label, "line": line,
                               "os": pr.get("OS") or pr.get("os") or "", "disk": pr.get("disk") or ""}
            except Exception:
                jobinfo = None
            try:
                ident = local_ident(mac) if mac else {}
            except Exception:
                ident = {}
            data = {"mac": mac, "name": m.get("name") or "", "approved": bool(m.get("approved")),
                    "model": ident.get("model", ""), "serial": ident.get("serial", ""),
                    "firmware": ident.get("firmware", ""), "tpm": ident.get("tpm", ""),
                    "department": dept or "", "cpu": m.get("cpu") or "", "ram": m.get("ram") or "",
                    "health": m.get("health_level") or "", "net_addr": m.get("agent_ip") or "",
                    "disks": disks, "disklist": disklist, "status": status, "job": jobinfo,
                    "status2": "Pick an OS below, or let IT start it from the web console. Both stay in sync.",
                    "advice": ["Read-only facts shown without sign-in; starting an install or rescue needs IT credentials."]}
            self.send_text(json.dumps(data), "application/json")
            return
        if u.path == "/seat/session":         # a diskless seat polls this every few seconds: run (minutes left) or lock
            mac = norm_mac(q.get("mac", [""])[0]) or IP2MAC.get(self.client_address[0], "")
            try:
                st = netbar.session_for_seat(mac) if mac else {"state": "locked", "reason": "unknown-seat"}
            except Exception:
                st = {"state": "run", "reason": "billing-off", "minutes": 0}   # never lock a seat because billing errored
            self.send_text(json.dumps(st), "application/json")
            return
        if u.path == "/seat/home":            # a seat restores the signed-in member's persistent home (docs + settings)
            mac = norm_mac(q.get("mac", [""])[0]) or IP2MAC.get(self.client_address[0], "")
            s = netbar.active_session(mac) if (mac and machine(mac)) else None
            if not s or not s["member_id"]:
                self.send_error(403)           # only a seat with an active member session may read a home
                return
            data = homes.load_home(s["member_id"]) if homes.enabled() else None
            if not data:
                self.send_response(204)        # no home yet (first sign-in) - the seat starts clean
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if u.path == "/signage/now":          # a display/kiosk polls this: what content to show right now (+ seconds to re-poll)
            mac = norm_mac(q.get("mac", [""])[0]) or IP2MAC.get(self.client_address[0], "")
            try:
                r = signage.now_for(mac) if mac else {"state": "idle", "reason": "unknown-display"}
            except Exception:
                r = {"state": "idle", "reason": "error", "poll": 30}
            self.send_text(json.dumps(r), "application/json")
            return
        if u.path == "/agent/unattend":       # Windows recipe: this machine's answer file, from its running install job
            mac = norm_mac(q.get("mac", [""])[0])
            if not token_ok(mac, self.headers.get("X-PXE-Token", "")):
                self.send_error(403)
                return
            j = db.q1("SELECT params FROM jobs WHERE mac=? AND type='install' AND state='running' ORDER BY created DESC LIMIT 1", mac)
            p = json.loads(j["params"]) if j else {}
            if p.get("OS") != "windows" or p.get("WIN_EULA") != "1":
                self.send_error(409)
                return
            body = windows_unattend(p, mac).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/xml; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/agent/drivers":        # Windows recipe: the driver folders matching this machine's PCI devices (tar)
            mac = norm_mac(q.get("mac", [""])[0])
            if not token_ok(mac, self.headers.get("X-PXE-Token", "")):
                self.send_error(403)
                return
            j = db.q1("SELECT params FROM jobs WHERE mac=? AND type='install' AND state='running' ORDER BY created DESC LIMIT 1", mac)
            if not j or json.loads(j["params"]).get("WIN_DRIVERS") != "1":
                self.send_error(409)
                return
            import drivers
            inv, _ = db.latest_report(mac, "inventory")
            try:                               # network catalog: auto-add any vetted pack this machine needs before matching
                import drivers_catalog         # (cached + signature-verified; no-op if nothing matches or no catalog synced)
                added = drivers_catalog.ensure_for(inv or "")
                if added:
                    log(f"[drivers] {mac}: catalog added {added}")
            except Exception as e:             # never let the catalog break the driver stream
                log(f"[drivers] catalog ensure skipped: {e!r}")
            pairs = drivers.match(inv or "")
            self.send_response(200)
            self.send_header("Content-Type", "application/x-tar")
            self.end_headers()                 # streamed: no length
            drivers.tar_stream(pairs, self.wfile)
            log(f"[drivers] {mac}: {len(pairs)} driver folders sent to the running install")
            return
        if u.path == "/agent/ppkg":           # Windows recipe: the department's Entra package, for this machine's running install only
            mac = norm_mac(q.get("mac", [""])[0])
            if not token_ok(mac, self.headers.get("X-PXE-Token", "")):
                self.send_error(403)
                return
            j = db.q1("SELECT params FROM jobs WHERE mac=? AND type='install' AND state='running' ORDER BY created DESC LIMIT 1", mac)
            p = json.loads(j["params"]) if j else {}
            f = ppkg_path((machine(mac) or {}).get("department_id"))
            if p.get("WIN_JOIN") != "entra" or not os.path.isfile(f):
                self.send_error(409)
                return
            body = open(f, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            log(f"[entra] {mac}: provisioning package handed to the running install")
            return
        if u.path == "/agent/winok":          # Windows first boot: the new system checks in with its one-time secret
            mac = norm_mac(q.get("mac", [""])[0])
            fb = json.loads((machine(mac) or {}).get("firstboot") or "null") or {}
            t = q.get("t", [""])[0]
            if fb.get("check") == "win" and fb.get("secret") and hmac.compare_digest(t, fb["secret"]):
                ver = q.get("ver", [""])[0][:80]
                dom = re.sub(r"[^A-Za-z0-9.-]", "", q.get("dom", [""])[0])[:100]     # set when the PC is in a domain
                aad = re.sub(r"[^a-z-]", "", q.get("aad", [""])[0])[:20]            # Entra join: joined | pending | failed
                firstboot_mark(mac, "ok", f"Windows checked in{(' (' + ver + ')') if ver else ''}"
                                          f"{(', domain ' + dom) if dom else ''}{(', Entra ' + aad) if aad else ''}")
                self.send_text("ok\n")
            else:
                self.send_error(403)
            return
        if u.path == "/agent/winjoin":        # Windows Entra join finished (one-time startup task): one report, derived secret
            mac = norm_mac(q.get("mac", [""])[0])
            fb = json.loads((machine(mac) or {}).get("firstboot") or "null") or {}
            t = q.get("t", [""])[0]
            if fb.get("join_secret") and hmac.compare_digest(t, fb["join_secret"]):
                st = q.get("aad", [""])[0]
                st = st if st in ("joined", "notjoined", "failed") else "failed"
                name = re.sub(r"[^A-Za-z0-9-]", "", q.get("name", [""])[0])[:15]
                dev = re.sub(r"[^A-Za-z0-9-]", "", q.get("dev", [""])[0])[:40]       # Entra device ID
                fb.pop("join_secret")
                fb["join"] = {"state": st, "kind": "entra", "name": name, "device": dev, "at": time.time()}
                db.upsert_machine(mac, firstboot=json.dumps(fb))
                live.publish({"t": "firstboot", "mac": mac, **fb_public(fb)})
                db.audit("server", "windows entra join", f"{mac} {st} as {name}" + (f", Entra device {dev}" if dev else ""))
                log(f"[firstboot] {mac}: Entra ID {st} as {name} {dev}")
                self.send_text("ok\n")
            else:
                self.send_error(403)
            return
        if u.path == "/agent/winapps":        # Windows app bundle finished (background task): one report, derived secret
            mac = norm_mac(q.get("mac", [""])[0])
            fb = json.loads((machine(mac) or {}).get("firstboot") or "null") or {}
            t = q.get("t", [""])[0]
            if fb.get("apps_secret") and hmac.compare_digest(t, fb["apps_secret"]):
                okn = q.get("ok", ["0"])[0]
                failed = [n for n in q.get("fail", [""])[0].split(",") if re.fullmatch(r"[A-Za-z0-9._+-]{1,40}", n)][:20]
                fb.pop("apps_secret")
                fb["apps"] = {"state": "done" if not failed else "partial", "ok": int(okn) if okn.isdigit() else 0,
                              "failed": failed, "at": time.time()}
                db.upsert_machine(mac, firstboot=json.dumps(fb))
                live.publish({"t": "firstboot", "mac": mac, **fb_public(fb)})
                db.audit("server", "windows apps", f"{mac} {fb['apps']['ok']} installed" + (f", failed: {', '.join(failed)}" if failed else ""))
                log(f"[firstboot] {mac}: app bundle {fb['apps']['ok']} ok, failed {failed}")
                self.send_text("ok\n")
            else:
                self.send_error(403)
            return
        if u.path == "/agent/watch":          # is anyone looking at this machine's screen in the web console?
            mac = norm_mac(q.get("mac", [""])[0])
            if not token_ok(mac, self.headers.get("X-PXE-Token", "")):
                self.send_error(403)
                return
            # line 1: screen watched (1/0); then "PTY <sid>" for each terminal waiting for this machine to dial in
            self.send_text("\n".join(["1" if live.watched(mac) else "0"] +
                                     [("VNC" if k == "vnc" else "RFB" if k == "rfb" else "PTY") + " " + s
                                      for s, k in live.pty_pending(mac)]) + "\n")   # RFB = remote control of the RAM OS screen
            return
        if u.path == "/agent/pty":            # kbpty in the RAM OS dials in for a terminal an admin opened
            mac = norm_mac(q.get("mac", [""])[0])
            sid = q.get("sid", [""])[0]
            if not token_ok(mac, self.headers.get("X-PXE-Token", "")) or not live.pty_claim(sid, mac):
                self.send_error(403)
                return
            self.protocol_version = "HTTP/1.1"
            self.send_response(101, "Switching Protocols")
            self.send_header("Upgrade", "kbpty")
            self.send_header("Connection", "Upgrade")
            self.end_headers()
            self.wfile.flush()
            self.close_connection = True
            live.pty_attach(sid, self.connection)
            return
        if u.path.startswith("/grub/") and u.path.endswith(".cfg"):   # GRUB per-machine config
            mac = norm_mac(u.path[len("/grub/"):-4])
            if known_or_discover(mac) and q.get("force", [""])[0] in ACTIONS:   # chosen in the static menu
                set_next(mac, q["force"][0])
            arch = q.get("arch", ["amd64"])[0]
            if arch not in ("amd64", "arm64", "i386"):
                arch = "amd64"
            g = grub_for(mac, arch) if known_or_discover(mac) else None
            if g is not None:
                IP2MAC[self.client_address[0]] = mac
                boot_step(mac, "menu")
            if g is None:
                self.send_error(404)   # -> GRUB shows the full menu
                return
            self.send_text(g)
            self.log_message('"GET %s" 200 (generated)', self.path)
            return
        if u.path == "/boot.ipxe":
            mac = norm_mac(q.get("mac", [""])[0])
            if mac and not known_or_discover(mac):
                self.send_text("#!ipxe\necho PXE toolkit: unknown machine and discovery is closed\nexit\n")
                return
            if mac and "force" in q and q["force"][0] in ACTIONS:
                set_next(mac, q["force"][0])
            script = menu_script() if "menu" in q else ipxe_for(mac)
            self.send_text(script)
            self.log_message('"GET %s" 200 (generated)', self.path)
            return
        path = self.translate_path(self.path)
        if os.path.isfile(path) and path.endswith(TEMPLATED_EXT):
            self.send_text(render(open(path, encoding="utf-8").read()))
            self.log_message('"GET %s" 200 (rendered)', self.path)
        else:
            super().do_GET()
        if u.path.endswith("authorized_keys") and CFG.get("stop_after_key_fetch"):
            log("stop_after_key_fetch: authorized_keys fetched -> shutting down in 3s")
            threading.Timer(3, lambda: os._exit(0)).start()

    def do_POST(self):
        if webui.handle(self, "POST"):
            return
        u = urlparse(self.path)
        q = parse_qs(u.query)
        mac = norm_mac(q.get("mac", [""])[0])
        if len(mac) != 17 or not all(c in "0123456789abcdef:" for c in mac) or not machine(mac):
            self.send_error(403)   # only known (configured or discovered) machines may post
            return
        if u.path == "/seat/login":           # a player signs in at the diskless seat with their member code (code in the body)
            n = int(self.headers.get("Content-Length", "0"))
            code = parse_qs(self.rfile.read(min(n, 200)).decode(errors="replace")).get("code", [""])[0][:32]
            try:
                r = netbar.seat_login(mac, code)
            except Exception:
                r = {"ok": False, "error": "billing unavailable"}
            self.send_text(json.dumps(r), "application/json")
            return
        if u.path == "/seat/print":           # the CUPS page-log meter reports a finished print job to charge the seat's member
            n = int(self.headers.get("Content-Length", "0"))
            f = parse_qs(self.rfile.read(min(n, 400)).decode(errors="replace"))
            code = (f.get("code", [""])[0] or "")[:32]
            mid = None
            if code:
                m = netbar.member_by_code(code)
                mid = m["id"] if m else None
            try:
                r = netbar.print_charge(int(f.get("pages", ["0"])[0] or 0), mac=mac, member_id=mid,
                                        copies=int(f.get("copies", ["1"])[0] or 1), doc=f.get("doc", [""])[0], operator="cups")
            except Exception:
                r = {"ok": False, "error": "billing unavailable"}
            self.send_text(json.dumps(r), "application/json")
            return
        if u.path == "/seat/home":            # a seat saves the signed-in member's home back before it locks / wipes
            mid = netbar.member_for_home_save(mac)   # active session, or one ended seconds ago (save-on-lock grace)
            if not mid:
                self.send_error(403)
                return
            if not homes.enabled():
                self.send_text(json.dumps({"ok": True, "reason": "home-off"}), "application/json")
                return
            n = int(self.headers.get("Content-Length", "0"))
            cap = homes.cap_mb() * 1024 * 1024
            if n <= 0 or n > cap:
                self.send_error(413)           # empty or over quota
                return
            data = self.rfile.read(min(n, cap))
            if data[:2] != b"\x1f\x8b":        # must be a gzip archive
                self.send_error(400)
                return
            try:
                saved = homes.save_home(mid, data)
                r = {"ok": True, "bytes": saved}
            except Exception as e:             # noqa: BLE001
                r = {"ok": False, "error": str(e)}
            self.send_text(json.dumps(r), "application/json")
            return
        if u.path == "/signage/played":       # a display reports proof-of-play (what it showed) for the audit log
            n = int(self.headers.get("Content-Length", "0"))
            f = parse_qs(self.rfile.read(min(n, 200)).decode(errors="replace"))
            try:
                z = signage.zone_for_mac(mac)
                signage.played(mac, int(f.get("content", ["0"])[0] or 0) or None, z["id"] if z else None)
            except Exception:
                pass
            self.send_text("ok\n")
            return
        if u.path == "/agent/upload":         # image capture: kbpty put:<file> streams one part of the image
            self.capture_upload(mac, q)
            return
        if u.path in ("/ep/result", "/ep/screen"):
            if not endpoint.token_ok(mac, self.headers.get("X-KB-EP", "")):
                self.send_error(403)
                return
            n = int(self.headers.get("Content-Length", "0"))
            if u.path == "/ep/result":
                endpoint.result(mac, int(q.get("id", ["0"])[0] or 0), self.rfile.read(min(n, 4000)).decode(errors="replace"))
            else:
                data = self.rfile.read(min(n, 6_000_000))
                if data[:8] != b"\x89PNG\r\n\x1a\n":
                    self.send_error(400)
                    return
                with open(os.path.join(mac_dir(mac), "screen.png"), "wb") as f:
                    f.write(data)
                db.upsert_machine(mac, ep_info=json.dumps({**json.loads((db.machine(mac) or {}).get("ep_info") or "{}"), "screen_at": db.now()}))
            self.send_text("ok\n")
            return
        if u.path == "/agent/install":        # the on-machine GUI's "install now", authorised by the IT sign-in (not a token)
            n = int(self.headers.get("Content-Length", "0"))
            try:
                d = json.loads(self.rfile.read(min(n, 100_000)) or b"{}")
            except Exception:
                self.send_text("error: bad request\n")
                return
            who = webui.check_login(str(d.get("user", "")), str(d.get("pass", "")))
            if not who:
                self.send_text("error: sign-in failed\n")
                return
            pol = db.policy((machine(mac) or {}).get("department_id"))
            need = pol.get("install_min_role") or "manager"            # department policy sets the floor (default: manager)
            if webui.RANK.get(who["role"], 0) < webui.RANK.get(need, webui.RANK["manager"]):
                self.send_text("error: this account may not start installs (needs role %s)\n" % need)
                return
            os_id = str(d.get("os", ""))
            if os_id not in [r["id"] for r in webui.recipes()]:
                self.send_text("error: unknown OS\n")
                return
            if pol.get("allowed_os") and os_id not in pol["allowed_os"]:
                self.send_text("error: this department does not allow installing %s\n" % os_id)
                return
            params = {"OS": os_id}
            for k, kk in (("disk", "DISK"), ("locale", "LOCALE"), ("keymap", "KEYMAP")):
                if d.get(k):
                    params[kk] = str(d[k])[:64]
            jid = db.add_job(mac, "install", params, who["username"])
            log(f"[gui-install] {mac}: {os_id} queued by {who['username']} (job {jid})")
            self.send_text("ok %s\n" % jid)
            return
        if u.path == "/agent/rescue":         # the on-machine GUI's "launch rescue tool", authorised by the IT sign-in
            n = int(self.headers.get("Content-Length", "0"))
            try:
                d = json.loads(self.rfile.read(min(n, 100_000)) or b"{}")
            except Exception:
                self.send_text("error: bad request\n")
                return
            who = webui.check_login(str(d.get("user", "")), str(d.get("pass", "")))
            if not who:
                self.send_text("error: sign-in failed\n")
                return
            t = tool_info(str(d.get("tool", "")))
            if not t:
                self.send_text("error: no such rescue tool\n")
                return
            need = t.get("ROLE") if t.get("ROLE") in webui.RANK else "admin"
            if webui.RANK.get(who["role"], 0) < webui.RANK[need]:
                self.send_text("error: this account may not start %s (needs role %s)\n" % (t.get("TITLE", t["NAME"]), need))
                return
            if not (machine(mac) or {}).get("approved"):
                self.send_text("error: machine not approved - ask IT to approve it first\n")
                return
            set_next(mac, "tool:" + t["NAME"])        # one-shot: the machine boots this tool on its next (imminent) reboot
            log(f"[gui-rescue] {mac}: tool:{t['NAME']} armed by {who['username']} ({who['role']})")
            self.send_text("ok %s\n" % t["NAME"])
            return
        if not token_ok(mac, self.headers.get("X-PXE-Token", "")):
            log(f"[security] {self.client_address[0]} POST {u.path} for {mac} with bad/missing token - refused")
            self.send_error(403)
            return
        n = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(min(n, 4_000_000))
        d = mac_dir(mac)
        name = (machine(mac) or {}).get("name", "?")
        if u.path == "/report":
            kind = q.get("kind", ["inventory"])[0]
            if kind not in ("inventory", "preflight", "truenas"):
                self.send_error(400)
                return
            text = body.decode(errors="replace")
            db.add_report(mac, kind, text)
            verdict = ""
            if kind == "preflight":
                last = [l for l in text.splitlines() if "RESULT:" in l]
                verdict = last[-1].strip("= ").replace("RESULT: ", "") if last else ""
                db.upsert_machine(mac, preflight=verdict)
            elif kind == "truenas":
                ok = "INSTALL OK" in text
                verdict = "TrueNAS installed" if ok else "TrueNAS install FAILED"
                notify.send(f"KwickBoot: {name} ({mac}) {verdict}")
            else:
                import re as _re
                cpu = _re.search(r"Model name:\s*(.+)", text)
                ram = _re.search(r"total=([\d.]+ GB)", text)
                h = health.parse(text)
                db.upsert_machine(mac, cpu=cpu.group(1).strip() if cpu else None, ram=ram.group(1) if ram else None,
                                  health=json.dumps(h), health_level=h["level"])
                if h["level"] == "bad":
                    notify.send(f"KwickBoot: {name} ({mac}) hardware problem: " + "; ".join(f["msg"] for f in h["flags"] if f["level"] == "bad"))
            log(f"[report] {kind} from {self.client_address[0]} {mac} ({name}) {verdict}")
            if kind == "inventory":
                boot_step(mac, "report", (cpu.group(1).strip()[:60] if cpu else ""))
            self.send_text("saved\n")
        elif u.path == "/agent/hello":
            st = q.get("state", ["?"])[0][:20]
            ip = q.get("ip", [self.client_address[0]])[0][:45]
            msg = body.decode(errors="replace")[:200]
            mrow = db.machine(mac) or {}
            prev = mrow.get("agent_state")
            db.upsert_machine(mac, agent_state=st, agent_ip=ip, agent_msg=msg, agent_seen=time.time())
            # a one-shot "boot the RAM OS" is spent once the RAM OS is up: the next boot (e.g. after a 40 s install)
            # must follow the default again, not the still-open multi-stage window
            if st == "booting" and mrow.get("next_used_at") and mrow.get("next_action") in ("alpine", "kwickboot", "core", "shell"):
                db.upsert_machine(mac, next_action=None, next_used_at=None)
                log(f"[intent] {mac}: one-shot '{mrow['next_action']}' done (RAM OS up) -> default")
            # a fresh RAM OS start ends whatever job this machine was running (it restarted under it): close it as
            # failed, or the machine would refuse every new job as "already running one"
            if st == "booting":
                for j in db.q("SELECT id, type FROM jobs WHERE mac=? AND state='running'", mac):
                    db.x("UPDATE jobs SET state='failed', rc=-1, error=?, finished=? WHERE id=?",
                         "machine restarted during the job", db.now(), j["id"])
                    log(f"[job] {mac}: {j['id']} ({j['type']}) closed - machine restarted during the job")
                    live.publish({"t": "job", "mac": mac, "job": j["id"], "state": "failed"})
            # TrueNAS installer up: its one-shot is spent, so the reboot after a fast install goes to the installed system
            if st == "busy" and (mrow.get("next_action") or "").startswith("truenas:"):
                db.upsert_machine(mac, next_action=None, next_used_at=None)
                log(f"[intent] {mac}: TrueNAS installer running -> next boots follow the default (local)")
            if st == "booting":
                IP2MAC[self.client_address[0]] = mac
                boot_step(mac, "online", ip)
            if prev != st:
                log(f"[agent] {mac} ({name}) {st} ip={ip}: {msg}")
            if prev != st or msg != mrow.get("agent_msg"):
                live.publish({"t": "agent", "mac": mac, "state": st, "msg": msg})
            self.send_text("ok\n")
        elif u.path == "/agent/log":
            HEARD[mac] = time.time()
            jid = os.path.basename(q.get("job", [""])[0])
            if not jid or not db.q1("SELECT 1 FROM jobs WHERE id=? AND mac=?", jid, mac):
                self.send_error(404)
                return
            with open(os.path.join(jobs_dir(mac), jid + ".log"), "wb") as f:
                f.write(body)
            text = webui.crfold(body[-200_000:].decode(errors="replace"))
            live.publish({"t": "joblog", "mac": mac, "job": jid, "log": text}, topic=f"job:{jid}")   # only consoles showing it
            job_tick(mac, jid, text)
            self.send_text("ok\n")
        elif u.path == "/agent/screen":       # the RAM OS's visible text screen (/dev/vcsa: rows, cols, cursor, char+colour)
            HEARD[mac] = time.time()
            import base64 as _b64, binascii as _ba
            try:                              # sent base64 (busybox wget uploads stop at a zero byte)
                raw = _b64.b64decode(body.strip(), validate=True)
            except (ValueError, _ba.Error):
                raw = b""
            if len(raw) > 4 and len(raw) >= 4 + 2 * raw[0] * raw[1] and len(raw) <= 64_000:
                live.publish({"t": "screen", "mac": mac, "data": _b64.b64encode(raw).decode()}, topic=f"screen:{mac}")
            self.send_text("ok\n")
        elif u.path == "/agent/screenfb":     # the RAM OS's GRAPHICAL screen (web desktop / kbgui): base64(gzip(raw fb0))
            HEARD[mac] = time.time()
            import base64 as _b64, binascii as _ba
            try:
                fw, fh, fs, fbpp = (int(q.get(k, ["0"])[0]) for k in ("w", "h", "s", "b"))
            except ValueError:
                fw = fh = fs = fbpp = 0
            data = body.strip()
            ok = 0 < fw <= 7680 and 0 < fh <= 4320 and fbpp in (24, 32) and fs >= fw * fbpp // 8 and 16 < len(data) <= 4_000_000
            if ok:
                try:                          # relayed as-is (the browser gunzips it); only check it really is gzip
                    ok = _b64.b64decode(data[:8], validate=True)[:2] == b"\x1f\x8b"
                except (ValueError, _ba.Error):
                    ok = False
            if ok:
                live.publish({"t": "screenfb", "mac": mac, "w": fw, "h": fh, "s": fs, "b": fbpp,
                              "gz": data.decode("ascii", "replace")}, topic=f"screen:{mac}")
            self.send_text("ok\n")
        elif u.path == "/agent/done":
            try:
                rc = int(q.get("rc", ["99"])[0])
            except ValueError:
                rc = 99
            finish_job(mac, q.get("job", [""])[0], rc)
            self.send_text("ok\n")
        else:
            self.send_error(404)


def menu_script():
    return "\n".join(["#!ipxe"] + menu_ipxe(H())) + "\n"


class FedHandler(Handler):
    """Federation port: only signed hub requests to /api/* (see federation.py)."""
    fed_listener = True

    def do_GET(self):
        if not webui.handle(self, "GET"):
            self.send_error(404)

    def do_POST(self):
        if not webui.handle(self, "POST"):
            self.send_error(404)


def fed_loop(port):
    import ssl as _ssl
    crt, key = federation.ensure_cert(socket.gethostname())
    srv = ThreadingHTTPServer(("0.0.0.0", port), functools.partial(FedHandler, directory=HTTP_ROOT))
    ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = _ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(crt, key)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    log(f"federation (node) listening on https://0.0.0.0:{port} fingerprint {federation.fingerprint()}")
    srv.serve_forever()


class TLSHandler(Handler):
    tls = True


_CERT_LOCK = threading.Lock()


def ensure_ui_cert():
    """data/ui.crt + ui.key (self-signed on first use; replace with a real certificate any time).
    Several listeners start at once on a new server: one lock, and key + certificate are made in temporary files and
    moved into place together - otherwise two of them make two certificates and serve one while the file holds the
    other (the kiosk's and managed machines' pin would then not match what is served)."""
    crt, key = os.path.join(BASE, "data", "ui.crt"), os.path.join(BASE, "data", "ui.key")
    with _CERT_LOCK:
        if not (os.path.exists(crt) and os.path.exists(key)):
            import subprocess
            tk, tc = key + ".new", crt + ".new"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", tk, "-out", tc, "-days", "3650",
                            "-subj", f"/CN={socket.gethostname()}", "-addext", f"subjectAltName=IP:{MY_IP},DNS:{socket.gethostname()}"],
                           check=True, capture_output=True)
            os.chmod(tk, 0o600)
            os.replace(tk, key)
            os.replace(tc, crt)
    return crt, key


def tls_context(crt, key):
    """Fast, modern TLS for the console and the managed-machine channel: TLS 1.3 preferred (1.2 minimum, AEAD ciphers
    only - AES-GCM uses the CPU's AES-NI, ChaCha20 is fast where there is none), ECDSA certificates (ensure_ui_cert),
    session tickets so a returning browser skips the full handshake, no TLS compression."""
    import ssl as _ssl
    ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = _ssl.TLSVersion.TLSv1_2
    ctx.set_ciphers("ECDHE+AESGCM:ECDHE+CHACHA20")                  # TLS 1.2; TLS 1.3 suites are always AEAD
    ctx.options |= _ssl.OP_NO_COMPRESSION | getattr(_ssl, "OP_CIPHER_SERVER_PREFERENCE", 0)
    try:
        ctx.num_tickets = 2                                         # session resumption (TLS 1.3 tickets)
    except (AttributeError, ValueError):
        pass
    ctx.load_cert_chain(crt, key)
    return ctx


_EP_PIN = None


def ep_pin():
    """curl --pinnedpubkey value (sha256//<base64>) of the server's public key: managed machines trust exactly this key."""
    global _EP_PIN
    if _EP_PIN is None:
        import base64
        import subprocess
        crt, _ = ensure_ui_cert()
        der = subprocess.run(["sh", "-c", f"openssl x509 -in '{crt}' -pubkey -noout | openssl pkey -pubin -outform der"],
                             check=True, capture_output=True).stdout
        _EP_PIN = "sha256//" + base64.b64encode(hashlib.sha256(der).digest()).decode()
    return _EP_PIN


class EPHandler(Handler):
    """HTTPS listener for managed machines: only the /ep/ routes (check-in, results, screenshots)."""
    tls = True

    def do_GET(self):
        if not self.path.startswith("/ep/"):
            return self.send_error(404)
        return super().do_GET()

    def do_POST(self):
        p = urlparse(self.path).path
        if not (p.startswith("/ep/") or p in ("/agent/install", "/agent/rescue")):   # the on-machine GUI's credential POSTs, pinned + encrypted
            return self.send_error(404)
        return super().do_POST()

    do_HEAD = do_GET


class QuietTLSServer(ThreadingHTTPServer):
    """Clients that abort the TLS handshake (wrong pin, scanners) get one log line, not a traceback.
    The listening socket stays plain and each connection does its TLS handshake in its own thread - wrapping the
    listening socket would do every handshake one after the other in the accept loop (hundreds of consoles opening at
    once then queue for minutes). A handshake that never finishes times out after 15 s."""
    ctx = None
    daemon_threads = True
    request_queue_size = 1024                           # Python's default is 5: a burst of consoles would be reset

    def handle_error(self, request, client_address):
        e = sys.exc_info()[1]
        log(f"[tls] {client_address[0]}: {e.__class__.__name__}" + (f": {e}"[:200] if not isinstance(e, OSError) else ""))

    def finish_request(self, request, client_address):
        request.settimeout(15)
        tls = self.ctx.wrap_socket(request, server_side=True)
        tls.settimeout(None)
        try:
            self.RequestHandlerClass(tls, client_address, self)
        finally:
            try:
                tls.close()
            except OSError:
                pass


def ep_https_loop(port):
    import ssl as _ssl
    crt, key = ensure_ui_cert()
    srv = QuietTLSServer((MY_IP, port), functools.partial(EPHandler, directory=HTTP_ROOT))
    ctx = tls_context(crt, key)
    srv.ctx = ctx                                  # handshake per connection, in its thread
    log(f"endpoint channel listening on https://{MY_IP}:{port} (pin {ep_pin()})")
    srv.serve_forever()


def https_ui_loop(port):
    """Web UI over HTTPS (needed for SSO redirect URIs). Uses data/ui.crt + ui.key; self-signed if absent."""
    import ssl as _ssl
    crt, key = ensure_ui_cert()
    srv = QuietTLSServer((CFG.get("http_bind") or MY_IP, port), functools.partial(TLSHandler, directory=HTTP_ROOT))
    ctx = tls_context(crt, key)
    srv.ctx = ctx                                  # handshake per connection, in its thread
    log(f"HTTPS console listening on https://{MY_IP}:{port}/ui/ (self-signed until you replace data/ui.crt + ui.key)"
        + ("" if not CFG.get("ui_http") else " - WARNING: ui_http is on, the plain-HTTP console is allowed too"))
    srv.serve_forever()


class BusyHTTPServer(ThreadingHTTPServer):
    """Plain HTTP (boot files, agents, the classic console): a deep accept queue - a room of PCs booting at once
    (Python's default queue is 5) - and threads that never block a restart."""
    request_queue_size = 1024
    daemon_threads = True


def http_loop():
    bind = CFG.get("http_bind") or MY_IP           # "0.0.0.0": also reachable on other networks (VM lab: the laptop side)
    srv = BusyHTTPServer((bind, HTTP_PORT), functools.partial(Handler, directory=HTTP_ROOT))
    log(f"HTTP listening on {bind}:{HTTP_PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    db.init(os.path.join(BASE, "data", "kwickboot.db"))
    netbar.init()          # netbar session/billing tables (no-op if they already exist)
    homes.init()           # per-member home store (creates the home dir if missing)
    signage.init()         # signage content/playlist/zone/schedule tables
    san.init()             # SAN LUN table + iSCSI target backend (sanboot)
    import bmc
    bmc.init(BASE)
    certs.init(BASE)
    import federation
    federation.init(BASE)
    import sso
    sso.init(BASE)
    insights.init(BASE)
    endpoint.init()
    migrated = db.migrate_files(BASE, CFG)
    if migrated:
        log(f"migrated {migrated} machines from file state into data/kwickboot.db")
    for _mac, _v in CFG.get("allowed_macs", {}).items():          # config.json machines are always approved
        if not db.machine(_mac):
            _v = _v if isinstance(_v, dict) else {"name": _v}
            db.upsert_machine(_mac, name=_v.get("name", ""), approved=1, default_action=_v.get("default", "alpine"))
    for _r in db.q("SELECT mac FROM machines WHERE health IS NULL"):   # backfill health for older inventories
        _t, _ = db.latest_report(_r["mac"], "inventory")
        if _t:
            _h = health.parse(_t)
            db.upsert_machine(_r["mac"], health=json.dumps(_h), health_level=_h["level"])
    n_m = db.q1("SELECT COUNT(*) AS n FROM machines")["n"]
    log(f"=== KwickBoot start on {MY_IP}; machines: {n_m}; discovery {'OPEN' if discovery_open() else 'closed'}")
    render_tftp_templates()
    build_apkovl()
    webui.init(sys.modules[__name__])
    db.ON_MACHINE_CHANGE = on_machine_change               # compact live updates (live_flush_loop)
    threading.Thread(target=live_flush_loop, daemon=True).start()
    threading.Thread(target=library_watch_loop, daemon=True).start()
    import schedules                           # scheduled actions run as their creator, re-checked at run time
    schedules.start(webui)
    if CFG.get("ep_https_port", 8444):        # managed machines: encrypted, key-pinned channel (0 = off)
        threading.Thread(target=ep_https_loop, args=(int(CFG.get("ep_https_port", 8444)),), daemon=True).start()
    if CFG.get("ui_https_port"):
        threading.Thread(target=https_ui_loop, args=(int(CFG["ui_https_port"]),), daemon=True).start()
    else:
        log("WARNING: the console is plain HTTP (passwords cross the network unencrypted) - set \"ui_https_port\": 9443 in config.json")
    if federation.node_enabled():
        threading.Thread(target=fed_loop, args=(int(CFG.get("federation_port", 8443)),), daemon=True).start()
    for target, args in ((dhcp_loop, (67,)), (dhcp_loop, (4011,)), (tftp_loop, ()), (http_loop, ()), (firstboot_loop, ())):
        threading.Thread(target=target, args=args, daemon=True).start()
    if IS_LINUX and IFACE and not own_dhcp_cfg() and CFG.get("dhcp_watch", True):
        threading.Thread(target=dhcp_watch_loop, daemon=True).start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        sys.exit(0)
