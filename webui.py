"""KwickBoot web UI back end: users/sessions + JSON API. Imported by pxeserver.py.
SPDX-License-Identifier: AGPL-3.0-or-later

Security model
- Users in the database (pbkdf2-sha256, salted). First admin from `pxectl passwd` (migrated) or `pxectl useradd`.
- Roles: admin (everything) > manager (approve, defaults, installs) > operator (scans, preflight, reboot,
  next-boot) > viewer (read only).
- Departments: non-admins only see/act on machines of their departments; managers also see the "unassigned"
  inbox (newly discovered machines) and can claim machines into their departments. Each department has a
  policy (allowed OS recipes + app bundles, hostname prefix, minimum role for installs) enforced server side.
- User + role are re-read from the database on every request: disabling a user or changing a role is immediate.
- Session cookie: random, HttpOnly, SameSite=Strict, 8 h; failed logins slowed down.
- Access only from networks in config "ui_allow" (default: the server's /24 + localhost).
- State-changing calls: POST + session + same-origin check. Every action re-validated server side + audited.
"""
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import time

import bmc
import health
import mfa
import db
import endpoint
import federation
import sso
import saml

S = None                 # pxeserver module (set by init)
class _Sessions:
    """Sign-in sessions, kept in the database so restarts and deploys don't sign people out.
    Stores sha256(token) only: a copy of the database can't be used to take over a session. Dict-like for the callers."""
    @staticmethod
    def _h(tok):
        return hashlib.sha256(tok.encode()).hexdigest()

    def get(self, tok, default=None):
        r = db.q1("SELECT uid, exp FROM sessions WHERE h=?", self._h(tok))
        return {"uid": r["uid"], "exp": r["exp"]} if r else default

    def __setitem__(self, tok, v):
        db.x("DELETE FROM sessions WHERE exp < ?", time.time())          # expired ones go whenever someone signs in
        db.x("INSERT OR REPLACE INTO sessions(h, uid, exp) VALUES(?,?,?)", self._h(tok), v["uid"], v["exp"])

    def pop(self, tok, default=None):
        v = self.get(tok, default)
        db.x("DELETE FROM sessions WHERE h=?", self._h(tok))
        return v

    def drop_user(self, uid):
        db.x("DELETE FROM sessions WHERE uid=?", uid)


SESSIONS = _Sessions()
# data-responsibility notice: shown to admins until one accepts it (who/when/version go to the audit log)
TERMS_VERSION = "1"
TERMS_URL = "https://kwickboot.com/terms/#your-data"


def terms_accepted():
    try:
        return json.loads(db.get_setting("terms_ack") or "{}").get("version") == TERMS_VERSION
    except ValueError:
        return False
TERMS_NOTICE = 'KwickBoot installs, restores and wipes disks. You are solely responsible for your data and its backups. KwickBoot is free software under the GNU AGPL-3.0 and comes with NO WARRANTY.'
SESSION_TTL = 8 * 3600
HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
NEXT_ACTIONS = ("alpine", "local", "menu", "memtest", "netbootxyz", "shell", "debian13", "seat")   # + "tool:<name>"
RANK = {"viewer": 0, "operator": 1, "manager": 2, "admin": 3}
NEEDS = {   # minimum role per action
    "discover": "manager", "approve": "manager", "unapprove": "manager", "default": "manager",
    "next": "operator", "job": "operator", "install": "operator", "assign": "manager",
    "set_profile": "manager", "ep_cmd": "operator", "local_boot": "manager", "kb_shell": "admin", "power": "operator", "manual": "operator", "capture": "manager", "winprep": "manager", "djoin_set": "manager", "djoin_clear": "manager", "driver_fetch": "manager", "netboot_once": "operator", "restore": "operator", "wipe": "manager",
}   # install: operator is the floor; the machine's department policy decides (default: manager)


def init(server_module):
    global S, SETUP_CODE_FILE
    S = server_module
    SETUP_CODE_FILE = os.path.join(S.BASE, "data", "setup-code")
    try:
        if not users_exist():
            if S.CFG.get("default_admin", True) and db.q1("SELECT COUNT(*) AS n FROM users")["n"] == 0:
                create_default_admin()
            else:
                setup_code()      # ready for `sudo pxectl setup-code` before anyone opens the console
    except Exception:             # noqa: BLE001 - a fresh database may not be ready in every caller
        pass


def server_card(h):
    """What the sign-in page shows about this server (the same as the box's own screen says): name, address, console
    address, boot services, version. Nothing about machines or users."""
    try:
        ver = open(os.path.join(S.BASE, "VERSION"), encoding="utf-8").read().strip()
    except OSError:
        ver = "dev"
    hp = S.CFG.get("ui_https_port")
    host = h.headers.get("Host", S.MY_IP).split(":")[0]
    own = (S.CFG.get("own_dhcp") or {}).get("enabled")
    return {"name": socket.gethostname(), "ip": S.MY_IP, "version": ver,
            "console": f"https://{host}:{hp}/ui/" if hp else f"http://{host}:{S.HTTP_PORT}/ui/",
            "boot": ("DHCP + " if own else "proxyDHCP + ") + "TFTP + HTTP"}


MFA_PENDING = {}            # sign-in ticket -> {uid, exp, tries}: password checked, waiting for the authenticator code


def mfa_required_for(user):
    """Two-step sign-in is required when the user is marked, or any of their departments requires it, or the server-wide
    setting says so (mfa_policy: "all" | "admins" | "" = optional)."""
    if not user or user.get("federated"):
        return False                                     # single sign-on: the identity provider does MFA
    if user.get("mfa_required"):
        return True
    pol = db.get_setting("mfa_policy", "") or ""
    if pol == "all" or (pol == "admins" and user.get("role") == "admin"):
        return True
    return any(db.policy(d).get("mfa_required") for d in db.user_departments(user["id"]))


def create_default_admin():
    """A brand-new server (never had an account): admin / admin, which must be changed at the first sign-in - the
    server allows nothing else until then. "default_admin": false in config.json = the first-run setup code instead.
    WARNING (logged): until it is changed, whoever reaches the console first can take the server."""
    db.x("INSERT INTO users(username, display, role, created, must_change_pw) VALUES('admin','Administrator','admin',?,1)", db.now())
    uid = db.q1("SELECT id FROM users WHERE username='admin'")["id"]
    salt, it = secrets.token_bytes(16), 300000
    db.x("UPDATE users SET pw_hash=?, pw_salt=?, pw_iter=? WHERE id=?",
         hashlib.pbkdf2_hmac("sha256", b"admin", salt, it).hex(), salt.hex(), it, uid)
    db.set_setting("default_login_active", "1")
    S.log("first start: default sign-in admin / admin created - it must be changed at the first sign-in "
          "(WARNING: until then anyone who reaches the console can take the server; \"default_admin\": false = setup code)")


def ui_dir():
    return os.path.join(S.BASE, "ui")


def audit(who, msg, ip=None):
    db.audit(who, msg.split(" ", 1)[0], msg, ip)
    S.log(f"[ui] {who}@{ip} {msg}")


# ---------------------------------------------------------------- access + auth
def allowed_ip(ip):
    nets = S.CFG.get("ui_allow") or [S.MY_IP.rsplit(".", 1)[0] + ".0/24", "127.0.0.1/32"]
    try:
        a = ipaddress.ip_address(ip)
        return any(a in ipaddress.ip_network(n, strict=False) for n in nets)
    except ValueError:
        return False


def users_exist():
    return db.q1("SELECT COUNT(*) AS n FROM users WHERE enabled=1 AND pw_hash IS NOT NULL")["n"] > 0


SETUP_CODE_FILE = None    # set in init(): data/setup-code


def setup_code():
    """First-run setup code: exists only while the server has no account. Readable on the server only
    (data/setup-code, 0600, or `sudo pxectl setup-code`) - proves the person creating the first admin owns the server."""
    f = SETUP_CODE_FILE
    if users_exist():
        try:
            os.remove(f)
        except OSError:
            pass
        return None
    try:
        c = open(f, encoding="ascii").read().strip()
        if re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}", c):
            return c
    except OSError:
        pass
    alpha = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    c = "-".join("".join(secrets.choice(alpha) for _ in range(4)) for _ in range(3))
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as o:
        o.write(c + "\n")
    return c


def check_login(username, pw):
    u = db.q1("SELECT * FROM users WHERE username=? AND enabled=1", username.strip().lower())
    if not u or not u["pw_hash"]:
        hashlib.pbkdf2_hmac("sha256", pw.encode(), b"x" * 16, 300000)    # same cost: no username probing by timing
        return None
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(u["pw_salt"]), int(u["pw_iter"])).hex()
    return u if hmac.compare_digest(h, u["pw_hash"]) else None


def user_depts(user):
    return user["dept_ids"] if "dept_ids" in user else db.user_departments(user["id"])


def perm(user, pol, key):
    """Effective permission: the user's own setting (allow/deny, set by an admin) wins, else the department policy.
    Admins always may. Strict organizations: switch it off for the department; open ones: on; exceptions per user."""
    if user["role"] == "admin":
        return True
    own = db.user_perms(user["id"]).get(key)
    return own == "allow" if own else bool(pol.get(key, True))


AVATAR_MAX = 256 * 1024
EMAIL_RE = re.compile(r"[^@\s<>\"']{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}")


def save_profile(uid, p):
    """Validate and store profile fields; avatar = base64 PNG/JPEG/WebP (checked by its bytes, not its name), "" removes it."""
    import base64
    sets, args = [], []
    for k in db.PROFILE_FIELDS:
        if k in p:
            v = str(p[k] or "").strip()[:200]
            if any(ord(c) < 32 for c in v):
                raise ValueError(f"{k}: no control characters")
            if k == "email" and v and not EMAIL_RE.fullmatch(v):
                raise ValueError("email: not a valid address")
            if k == "phone" and v and not re.fullmatch(r"[0-9+()./ x-]{3,40}", v):
                raise ValueError("phone: digits, spaces and + ( ) - . / x")
            sets.append(f"{k}=?")
            args.append(v)
    if "avatar" in p:
        raw = str(p["avatar"] or "")
        if not raw:
            sets += ["avatar=NULL", "avatar_type=NULL"]
        else:
            try:
                b = base64.b64decode(raw.split(",", 1)[-1], validate=True)
            except ValueError:
                raise ValueError("avatar: bad image data")
            typ = "image/png" if b[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg" if b[:3] == b"\xff\xd8\xff" else \
                  "image/webp" if b[:4] == b"RIFF" and b[8:12] == b"WEBP" else None
            if not typ:
                raise ValueError("avatar: PNG, JPEG or WebP only")
            if len(b) > AVATAR_MAX:
                raise ValueError("avatar: 256 KB at most (the page shrinks photos before upload)")
            sets += ["avatar=?", "avatar_type=?"]
            args += [b, typ]
    if sets:
        db.x(f"UPDATE users SET {', '.join(sets)} WHERE id=?", *args, uid)


FAVICON = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="6.5" fill="#C32B27"/>'
           b'<path d="M16 7.6v8.2" stroke="#FFFFFF" stroke-width="3.4" stroke-linecap="round"/>'
           b'<path d="M10.2 11.2a8.2 8.2 0 1 0 11.6 0" fill="none" stroke="#FFFFFF" stroke-width="3.4" stroke-linecap="round"/></svg>')


def scope(user):
    """(department ids or None for all, whether unassigned machines are visible)"""
    if user["role"] == "admin":
        return None, True
    return user_depts(user), user["role"] == "manager"


def can_access(user, m):
    if user["role"] == "admin":
        return True
    if m.get("department_id") is None:
        return user["role"] == "manager"
    return m["department_id"] in user_depts(user)


def dept_names():
    return {d["id"]: d["name"] for d in db.departments()}


def session_of(h):
    m = re.search(r"(?:^|;\s*)kbsess=([0-9a-f]{64})", h.headers.get("Cookie", ""))
    if not m:
        return None
    s = SESSIONS.get(m.group(1))
    if not s or s["exp"] < time.time():
        SESSIONS.pop(m.group(1), None)
        return None
    u = db.q1("SELECT * FROM users WHERE id=? AND enabled=1", s["uid"])
    if not u:                                   # disabled or deleted -> session dies at once
        SESSIONS.pop(m.group(1), None)
        return None
    return u


def same_origin(h):
    host = h.headers.get("Host", "")
    src = h.headers.get("Origin") or h.headers.get("Referer") or ""
    return bool(host) and re.match(rf"^https?://{re.escape(host)}(/|$)", src) is not None


def send_json(h, obj, code=200, extra_headers=()):
    b = json.dumps(obj, separators=(",", ":")).encode()
    gz = len(b) > 1400 and "gzip" in (h.headers.get("Accept-Encoding", "") or "")
    if gz:
        import gzip
        b = gzip.compress(b, 5)
    h.send_response(code)
    h.send_header("Content-Type", "application/json")
    if gz:
        h.send_header("Content-Encoding", "gzip")
        h.send_header("Vary", "Accept-Encoding")
    h.send_header("Cache-Control", "no-store")
    h.send_header("Content-Length", str(len(b)))
    for k, v in extra_headers:
        h.send_header(k, v)
    h.end_headers()
    h.wfile.write(b)


def read_json(h, limit=100_000):
    n = int(h.headers.get("Content-Length", "0"))
    try:
        return json.loads(h.rfile.read(min(n, limit)) or b"{}")
    except Exception:
        return {}


# ---------------------------------------------------------------- data helpers
def summary(m):
    age = int(time.time() - m["agent_seen"]) if m.get("agent_seen") else None
    agent = {"state": m.get("agent_state"), "ip": m.get("agent_ip"), "msg": m.get("agent_msg")} if m.get("agent_state") else {}
    return {"mac": m["mac"], "name": m["name"] or "?", "approved": bool(m["approved"]), "discovered": m.get("discovered") or "",
            "default": m["default_action"], "next": m.get("next_action") or "", "agent": agent, "agent_age": age,
            "preflight": m.get("preflight") or "", "cpu": m.get("cpu") or "", "ram": m.get("ram") or "",
            "department": m.get("department") or "", "health": m.get("health_level") or "",
            "local_boot": m.get("local_boot") or "auto", "kb_shell": bool(m.get("kb_shell")),
            "firstboot": S.fb_public(json.loads(m.get("firstboot") or "null")) or None,
            # the installed system's management agent (managed installs): when it checked in last
            "os_age": int(time.time() - m["ep_seen"]) if m.get("ep_seen") else None}


def disks_from_inventory(mac):
    inv, _ = db.latest_report(mac, "inventory")
    out = []
    for line in inv.splitlines():
        m = re.match(r"\s+(\S+)\s+(\S+)\s+tran=(\S*)\s+rota=\S*\s+model=(\S*)\s+SERIAL=(\S+)", line)
        if m:
            out.append({"dev": "/dev/" + m.group(1), "size": m.group(2), "tran": m.group(3),
                        "model": m.group(4).replace("_", " "), "serial": m.group(5)})
    return out


def size_gb(s):
    """lsblk size text ('80G', '1.8T', '512M') -> GB (float); 0 if unknown."""
    m = re.fullmatch(r"([\d.]+)([KMGTP])?", str(s or "").strip().upper())
    if not m:
        return 0.0
    return float(m.group(1)) * {"K": 1 / 1048576, "M": 1 / 1024, "G": 1, "T": 1024, "P": 1048576}[m.group(2) or "G"]


WIN_EDITION = {"pro": "Professional", "education": "Education", "enterprise": "Enterprise", "home": "Core"}  # = windows.sh
WIN_C_MIN_GB = 30         # Windows 11 applied is ~20 GB; below this it cannot finish setup (physics, not a judgment call)


DRV_FETCH = {}          # pack name -> {"state": downloading|done|failed, "by", "at", "error"} (background downloads)
_cat_refresh = {"at": 0}


def win_driver_offer(mac):
    """The PC maker's official driver pack for this machine when it is not in the library yet (or is downloading).
    Uses the cached catalog only; refreshes it in the background at most once a day."""
    try:
        import drivers
        import threading
        stale = any(not os.path.isfile(c) or time.time() - os.path.getmtime(c) > drivers.CAT_MAX_AGE
                    for c in (os.path.join(drivers.LIB, "_catalogs", f + ".json") for f in ("dell", "hp", "lenovo")))
        if stale and time.time() - _cat_refresh["at"] > 3600:
            _cat_refresh["at"] = time.time()
            threading.Thread(target=drivers.refresh_catalogs, daemon=True).start()
        h = drivers.lookup(db.latest_report(mac, "inventory")[0] or "")
        if not h or (os.path.isdir(os.path.join(drivers.LIB, h["pack"])) and h["pack"] not in DRV_FETCH):
            return None
        return {k: h[k] for k in ("vendor", "name", "pack", "os", "size", "date")} | {"fetch": DRV_FETCH.get(h["pack"])}
    except Exception:          # noqa: BLE001 - a missing catalog or library must never break the machine page
        return None


def win_driver_match(mac):
    """Driver folders from the library that match this machine's PCI devices: [{pack, dir}] (max 50 listed)."""
    try:
        import drivers
        return [{"pack": p, "dir": d} for p, d in drivers.match(db.latest_report(mac, "inventory")[0] or "")][:50]
    except Exception:          # noqa: BLE001 - a broken library must never break the machine page
        return []


WIN_OEM_KEYS = ("manufacturer", "model", "phone", "hours", "url", "org", "owner")


WIN_TPL = (("iso", "win_iso", "ISO"), ("variant", "win_edition", "edition"), ("win_debloat", "win_debloat", "debloat level"),
           ("win_apps", "win_apps", "apps"))


def win_template(a, pol, user):
    """Department Windows template: blank choices take the department's value. When 'may change' (win_custom) is off for
    this user (department switch, per-user exception), a choice that differs from a set template value is refused.
    Returns (request with defaults filled, error or None)."""
    a = dict(a)
    free = perm(user, pol, "win_custom")
    for key, pk, label in WIN_TPL:
        want = pol.get(pk) or ""
        if not want:
            continue
        got = str(a.get(key) or "")
        if key == "win_apps" and a.get(key) in (True, "1"):
            got = "bundle"
        if not got:
            a[key] = want
        elif got != want and not free:
            return a, (f"this department's Windows template sets the {label} to '{want}' and changes are switched off "
                       f"for you - ask an admin for an exception")
    return a, None


def win_layout(a, disk, disks, extra):
    """Windows disk layout. Default: ONE large C: over the whole disk. Data partitions on the same disk (win_parts:
    [{"letter": "D", "gb": 200}, ...], gb 0 = the rest; win_c_gb = size of C:, 0 = the rest) and other physical drives
    as data drives (win_disks: [{"dev": "/dev/sdb", "letter": "E"}] - ERASED) only when IT asks for them.
    Fills extra[WIN_C_GB / WIN_PARTS / WIN_DISKS / WIN_DISK_SERIALS]; returns an error text or None."""
    parts, drives = a.get("win_parts") or [], a.get("win_disks") or []
    if not isinstance(parts, list) or not isinstance(drives, list) or len(parts) > 6 or len(drives) > 8:
        return "disk layout: lists of at most 6 partitions and 8 drives"
    try:
        cgb = int(a.get("win_c_gb") or 0)
        spec = [("C", cgb)] + [(str(p.get("letter", "")).upper().rstrip(":"), int(p.get("gb") or 0)) for p in parts]
    except (TypeError, ValueError, AttributeError):
        return "disk layout: sizes are whole GB"
    letters = [l for l, _ in spec] + [str(d.get("letter", "")).upper().rstrip(":") for d in drives if isinstance(d, dict)]
    if len(drives) != len(letters) - len(spec):
        return "disk layout: each extra drive needs a device and a letter"
    if any(not re.fullmatch(r"[D-Z]", l) for l in letters[1:]) or len(set(letters)) != len(letters):
        return "drive letters: D to Z, each used once (C: is Windows)"
    if any(g < 0 or g > 1_000_000 for _, g in spec):
        return "disk layout: sizes from 1 GB, 0 = the rest of the disk"
    if 0 < size_gb(disk["size"]) - 0.5 < WIN_C_MIN_GB:   # physically impossible: stop at queue time, not halfway through
        return f"the {disk['size']} disk is too small - Windows needs {WIN_C_MIN_GB} GB or more"
    if not parts:
        if cgb:
            return "a size for C: only makes sense with data partitions on the same disk (default: C: takes the whole disk)"
    else:
        rest = [i for i, (_, g) in enumerate(spec) if g == 0]
        if len(rest) > 1:
            return "only one partition can take 'the rest of the disk'"
        total = size_gb(disk["size"])
        asked = sum(g for _, g in spec)
        if asked > total - 0.5:
            return f"partitions add up to {asked} GB - more than the {disk['size']} disk"
        if not rest:                                   # nothing takes the rest: the last partition grows into it
            spec[-1] = (spec[-1][0], 0)
        fixed = sum(g for _, g in spec)
        free = total - 0.5 - fixed                     # EFI + MSR + alignment
        if free < 1:
            return f"partitions add up to {fixed} GB - more than the {disk['size']} disk"
        csize = spec[0][1] or free
        if csize < WIN_C_MIN_GB:
            return f"C: would be {csize:.0f} GB - Windows needs {WIN_C_MIN_GB} GB or more"
        # the last data partition holds the Windows image while it is applied (recipe checks the exact size too) - unless
        # the ISO has a prepared (streamable) image of this edition: then nothing is staged
        last = spec[-1][1] or free
        ed = WIN_EDITION.get(extra.get("VARIANT") or "pro", "Professional")
        prepared = os.path.isfile(os.path.join(S.HTTP_ROOT, "iso", "winimg", extra["ISO"], ed + ".wim"))
        try:
            if prepared:
                raise LookupError
            import isofs
            wim = max((isofs.lookup(os.path.join(S.HTTP_ROOT, "iso", extra["ISO"]), w) or (0,))[0]
                      for w in ("sources/install.wim", "sources/install.esd")) / 2**30
        except Exception:
            wim = 0
        if wim and last < wim + 1.5:
            return f"drive {spec[-1][0]}: is {last:.0f} GB - it holds the Windows image ({wim:.1f} GB) during the install, make it {wim + 2:.0f} GB or more"
        extra["WIN_C_GB"] = str(spec[0][1])
        extra["WIN_PARTS"] = "+".join(f"{l}={g}" for l, g in spec[1:])
    if drives:
        devs, sers = [], []
        known = {d["dev"]: d for d in disks}
        for d, l in zip(drives, letters[len(spec):]):
            dev = str(d.get("dev", ""))
            if dev == disk["dev"]:
                return f"{dev} is the Windows disk"
            if dev not in known:
                return f"{dev} is not in this machine's inventory"
            ser = known[dev]["serial"]
            if not re.fullmatch(r"[A-Za-z0-9_./:=-]{1,80}", ser):
                return f"{dev}: its serial '{ser}' cannot be passed safely - erase it with Secure wipe first, then add it in Windows"
            devs.append(f"{os.path.basename(dev)}={l}")
            sers.append(ser)
        extra["WIN_DISKS"], extra["WIN_DISK_SERIALS"] = "+".join(devs), "+".join(sers)
    return None


def win_layout_text(extra):
    """', layout C: 200 GB + D: rest, drive E: = sdb (serial X, ERASED)' for the audit line; '' for the default one large C:."""
    out = ""
    if extra.get("WIN_PARTS"):
        g = lambda v: "rest" if v == "0" else v + " GB"  # noqa: E731
        out = f", layout C: {g(extra['WIN_C_GB'])} + " + " + ".join(f"{p.split('=')[0]}: {g(p.split('=')[1])}" for p in extra["WIN_PARTS"].split("+"))
    if extra.get("WIN_DISKS"):
        out += ", " + ", ".join(f"drive {d.split('=')[1]}: = {d.split('=')[0]} (serial {s}, ERASED)"
                                for d, s in zip(extra["WIN_DISKS"].split("+"), extra["WIN_DISK_SERIALS"].split("+")))
    return out


def crfold(text):
    """Progress meters (wimlib, wget, pv) rewrite one line with carriage returns: keep only the newest update per line."""
    out = []
    for line in text.split(chr(10)):
        if chr(13) in line:
            parts = [x for x in line.split(chr(13)) if x.strip()]
            line = parts[-1] if parts else ""
        out.append(line)
    return chr(10).join(out)


def rd(path, limit=200_000):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()[-limit:]
    except OSError:
        return ""


def recipes():
    d = os.path.join(S.HTTP_ROOT, "recipes")
    out = []
    for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if f.endswith(".sh"):
            src = rd(os.path.join(d, f))
            if re.search(r"^RECIPE_HIDDEN=1", src, re.M):   # e.g. kbserver: only the installer ISO uses it
                continue
            g = lambda k: (re.search(rf'{k}="([^"]*)"', src) or [None, ""])[1]
            mn = re.search(r"RECIPE_MIN_GB=(\d+)", src)
            out.append({"id": f[:-3], "name": g("RECIPE_NAME") or f[:-3], "desc": g("RECIPE_DESC"),
                        "note": g("RECIPE_NOTE"), "min_gb": int(mn.group(1)) if mn else 0,
                        "method": (re.search(r"RECIPE_METHOD=(\w+)", src) or [None, ""])[1],
                        "bundles": g("RECIPE_BUNDLES").split(), "variants": g("RECIPE_VARIANTS").split(), "params": g("RECIPE_PARAMS").split() + (["endpoint"] if "RECIPE_ENDPOINT=1" in src else [])})
    return out


# ---------------------------------------------------------------- request routing
def handle(h, method, _preauth=False):
    """Returns True if the request was handled here."""
    from urllib.parse import urlparse, parse_qs
    u = urlparse(h.path)
    path, q = u.path, parse_qs(u.query)
    if not (path in ("/", "/ui", "/login", "/login/mfa", "/logout", "/setup") or path.startswith(("/api/", "/ui/", "/sso/"))):
        return False
    ip = h.client_address[0]
    if getattr(h, "fed_listener", False) and not _preauth:   # node: request from a hub on the federation port
        return handle_federated(h, method, path)
    if not _preauth and not allowed_ip(ip):
        h.send_error(403, "UI not allowed from this network")
        return True
    # HTTPS console (config ui_https_port, on for new installs): the HTTP port keeps serving boot files and scripts, but
    # console pages redirect to HTTPS and sign-in / first-run setup are refused there, so no password crosses the network
    # in clear text. "ui_http": true allows the plain-HTTP console again (warned in the log at start).
    hp = S.CFG.get("ui_https_port")
    if hp and not getattr(h, "tls", False) and not S.CFG.get("ui_http") and not getattr(h, "fed_listener", False):
        host = h.headers.get("Host", S.MY_IP).split(":")[0]
        dest = f"https://{host}:{hp}"
        if method == "GET" and (path in ("/", "/ui", "/ui/", "/login", "/setup") or path.endswith(".html")):
            _redirect(h, dest + (path if path.startswith("/ui/") else "/ui/"), None)
            return True
        if method == "POST" and path in ("/login", "/login/mfa", "/setup"):
            send_json(h, {"error": f"sign in over HTTPS: {dest}/ui/", "https": f"{dest}/ui/"}, 403)
            return True
    if path.startswith("/ui/i18n/") and method == "GET":     # console translations (public: the sign-in page needs them too)
        name = path[len("/ui/i18n/"):]
        f = os.path.join(ui_dir(), "i18n", name)
        if not re.fullmatch(r"[a-z]{2}(-[A-Z]{2})?\.json", name) or not os.path.isfile(f):
            h.send_error(404)
            return True
        with open(f, "rb") as fh:
            body = fh.read()
        h.send_response(200)
        h.send_header("Content-Type", "application/json; charset=utf-8")
        h.send_header("Cache-Control", "no-cache")
        h.send_header("X-Content-Type-Options", "nosniff")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)
        return True
    if path == "/ui/favicon.svg" and method == "GET":         # the KwickBoot brand mark (same as kwickboot.com); public
        body = FAVICON
        h.send_response(200)
        h.send_header("Content-Type", "image/svg+xml")
        h.send_header("Cache-Control", "public, max-age=604800")
        h.send_header("X-Content-Type-Options", "nosniff")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)
        return True
    if path.startswith("/ui/vendor/") and method == "GET":   # pinned, verified third-party UI libraries (ui/vendor/SOURCES.txt)
        base = os.path.realpath(os.path.join(ui_dir(), "vendor"))
        f = os.path.realpath(os.path.join(base, path[len("/ui/vendor/"):]))
        types = {".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css", ".txt": "text/plain; charset=utf-8",
                 ".json": "application/json"}
        ext = os.path.splitext(f)[1]
        if not f.startswith(base + os.sep) or ext not in types or not os.path.isfile(f):
            h.send_error(404)
            return True
        with open(f, "rb") as fh:
            body = fh.read()
        h.send_response(200)
        h.send_header("Content-Type", types[ext])
        h.send_header("Cache-Control", "public, max-age=604800")      # versions are pinned; a new version = new files
        h.send_header("X-Content-Type-Options", "nosniff")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)
        return True
    if path == "/api/vnc" and method == "GET":   # noVNC: screen of a running manual install (operator+, own machines)
        user = session_of(h)
        mac = S.norm_mac(q.get("mac", [""])[0])
        m = db.machine(mac) if mac else None
        if not user or not same_origin(h) or not m or not can_access(user, m):
            h.send_error(403)
            return True
        import live
        who_, name = user["username"], m["name"]
        view = RANK[user["role"]] < RANK["operator"]      # viewers watch; keyboard/mouse are filtered out on the server
        if q.get("target", [""])[0] == "bmc":           # the hardware screen: Intel AMT KVM / a BMC's VNC console
            v = (bmc.config(mac) or {}).get("vnc", "")
            host, _, port = v.rpartition(":")
            if not host or not port.isdigit():
                h.send_error(404)
                return True
            audit(who_, f"HARDWARE SCREEN ({v}) opened on {name} {mac}{' (view only)' if view else ''}", ip)
            live.serve_vnc_direct(h, host.strip("[]"), int(port), view_only=view,
                                  on_end=lambda why: audit(who_, f"HARDWARE SCREEN closed {name} {mac}: {why}", ip))
            return True
        if q.get("target", [""])[0] == "screen":        # remote control of the RAM OS's own screen (kbvnc, via kbpty tcp:5901)
            audit(who_, f"REMOTE CONTROL opened on {name} {mac}{' (view only)' if view else ''}", ip)
            live.serve_vnc(h, mac, on_end=lambda why: audit(who_, f"REMOTE CONTROL closed {name} {mac}: {why}", ip),
                           view_only=view, kind="rfb")
            return True
        if not db.q1("SELECT 1 FROM jobs WHERE mac=? AND type='manual' AND state='running'", mac):
            h.send_error(403)
            return True
        audit(who_, f"INSTALLER SCREEN opened on {name} {mac}{' (view only)' if view else ''}", ip)
        live.serve_vnc(h, mac, on_end=lambda why: audit(who_, f"INSTALLER SCREEN closed {name} {mac}: {why}", ip), view_only=view)
        return True
    if path == "/api/ws" and method == "GET":    # live push (live.py): logged-in user, same origin, own machines only
        user = session_of(h)
        if not user or not same_origin(h):
            h.send_error(403)
            return True
        import live

        def on_text(msg, client):
            if not isinstance(msg, dict):
                return
            if "subs" in msg and isinstance(msg["subs"], list):   # what the page shows now: job logs, live screens
                ok = []
                for t in msg["subs"][:100]:
                    t = str(t)
                    if t.startswith("screen:"):
                        mac = S.norm_mac(t[7:])
                        m = db.machine(mac) if mac else None
                        if m and can_access(user, m):
                            ok.append("screen:" + mac)
                            live.watch(mac)                       # the RAM OS streams its screen while watched
                    elif t.startswith("job:"):
                        j = db.q1("SELECT mac FROM jobs WHERE id=?", t[4:][:80])
                        if j and can_access(user, db.machine(j["mac"]) or {}):
                            ok.append(t[:84])
                client.subscribe(ok)
            elif "watch" in msg:                # older pages: this user looks at that machine's screen
                mac = S.norm_mac(str(msg["watch"]))
                m = db.machine(mac) if mac else None
                if m and can_access(user, m):
                    live.watch(mac)
                    client.subscribe(set(client.subs) | {"screen:" + mac})
            elif "pty_open" in msg:             # terminal in the RAM OS: a root shell -> admins only, audited
                mac = S.norm_mac(str(msg["pty_open"]))
                m = db.machine(mac) if mac else None
                if user["role"] != "admin" or not m or not can_access(user, m):
                    live._push(client, {"t": "pty_end", "sid": "", "why": "the terminal needs role admin"})
                    return
                who_, name = user["username"], m["name"]
                sid = live.pty_open(client, mac, on_end=lambda why: audit(who_, f"TERMINAL closed {name} {mac}: {why}", ip))
                audit(who_, f"TERMINAL opened on {name} {mac} (RAM OS root shell)", ip)
                live._push(client, {"t": "pty_open", "sid": sid, "mac": mac})
            elif "bmc_console" in msg:          # the machine's text console via its BMC (IPMI serial-over-LAN)
                mac = S.norm_mac(str(msg["bmc_console"]))
                m = db.machine(mac) if mac else None
                if not m or not can_access(user, m):
                    live._push(client, {"t": "pty_end", "sid": "", "why": "unknown machine"})
                    return
                view = RANK[user["role"]] < RANK["operator"]      # viewers watch; their keystrokes are dropped here
                try:
                    argv, env, deact = bmc.sol_command(mac)
                except bmc.BMCError as e:
                    live._push(client, {"t": "pty_end", "sid": "", "why": str(e)})
                    return
                who_, name = user["username"], m["name"]
                audit(who_, f"HARDWARE CONSOLE (IPMI serial-over-LAN) opened on {name} {mac}{' (view only)' if view else ''}", ip)
                live.pty_open_local(client, mac, argv, env, view=view, after=deact,
                                    on_end=lambda why: audit(who_, f"HARDWARE CONSOLE closed {name} {mac}: {why}", ip))
            elif "pty_in" in msg:
                live.pty_input(client, str(msg["pty_in"]), str(msg.get("data", ""))[:65536])
            elif "pty_resize" in msg:
                try:
                    live.pty_resize(client, str(msg["pty_resize"]), int(msg.get("cols", 0)), int(msg.get("rows", 0)))
                except (TypeError, ValueError):
                    pass
            elif "pty_close" in msg:
                live.pty_close(client, str(msg["pty_close"]))
        live.serve(h, lambda mac: can_access(user, db.machine(mac) or {}), on_text, uid=user["id"])
        return True
    if path in ("/", "/ui", "/ui/"):
        body = rd(os.path.join(ui_dir(), "index.html"), 2_000_000).encode()
        h.send_response(200)
        h.send_header("Content-Type", "text/html; charset=utf-8")
        h.send_header("Cache-Control", "no-store")
        h.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        h.send_header("X-Frame-Options", "DENY")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)
        return True

    if path == "/login" and method == "POST":
        if not same_origin(h):
            send_json(h, {"error": "bad origin"}, 403)
            return True
        if not users_exist():
            setup_code()
            send_json(h, {"error": "No account yet: create the first admin (setup code on the server: sudo pxectl setup-code)", "setup": True}, 503)
            return True
        a = read_json(h)
        user = check_login(str(a.get("username", "admin")), str(a.get("password", "")))
        if not user:
            time.sleep(1.5)
            audit(str(a.get("username", "?"))[:40], "login FAILED", ip)
            send_json(h, {"error": "wrong username or password"}, 401)
            return True
        if user.get("mfa_enabled"):                     # step 2: the code from the authenticator app
            t = secrets.token_hex(24)
            MFA_PENDING[t] = {"uid": user["id"], "exp": time.time() + 300, "tries": 0, "pw_change": bool(user.get("must_change_pw"))}
            send_json(h, {"mfa": True, "ticket": t})
            return True
        tok = secrets.token_hex(32)
        SESSIONS[tok] = {"uid": user["id"], "exp": time.time() + SESSION_TTL}
        db.x("UPDATE users SET last_login=? WHERE id=?", db.now(), user["id"])  # noqa
        audit(user["username"], "login ok", ip)
        send_json(h, {"ok": True, "must_change_pw": bool(user.get("must_change_pw"))}, extra_headers=[("Set-Cookie", f"kbsess={tok}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_TTL}" + ("; Secure" if getattr(h, "tls", False) else ""))])
        return True
    if path == "/api/setup-local" and method == "GET":  # first run, at the server's own screen (kiosk): the code is filled in
        local = {"127.0.0.1", "::1", S.MY_IP}
        if users_exist() or ip not in local:
            send_json(h, {})                             # remote browsers: the code must be typed (proof of owning the server)
            return True
        send_json(h, {"code": setup_code()})
        return True
    if path == "/login/mfa" and method == "POST":       # step 2 of the sign-in: authenticator code or a recovery code
        if not same_origin(h):
            send_json(h, {"error": "bad origin"}, 403)
            return True
        a = read_json(h)
        p = MFA_PENDING.get(str(a.get("ticket", "")))
        if not p or p["exp"] < time.time() or p["tries"] >= 5:
            MFA_PENDING.pop(str(a.get("ticket", "")), None)
            send_json(h, {"error": "the sign-in timed out - enter your password again", "restart": True}, 401)
            return True
        p["tries"] += 1
        u = db.q1("SELECT * FROM users WHERE id=? AND enabled=1", p["uid"])
        code = str(a.get("code", "")).strip()
        ok = False
        if u and "-" in code:                            # recovery code (XXXX-XXXX): used up
            left = mfa.use_recovery_code(code, json.loads(u.get("mfa_recovery") or "[]"))
            if left is not None:
                db.x("UPDATE users SET mfa_recovery=? WHERE id=?", json.dumps(left), u["id"])
                audit(u["username"], f"login with a recovery code ({len(left)} left)", ip)
                ok = True
        elif u:
            step = mfa.verify(u.get("mfa_secret"), code, u.get("mfa_last") or 0)
            if step:
                db.x("UPDATE users SET mfa_last=? WHERE id=?", step, u["id"])
                ok = True
        if not ok:
            time.sleep(1.5)
            audit(u["username"] if u else "?", "login FAILED (two-step code)", ip)
            send_json(h, {"error": "wrong code - try the current one from your authenticator app"}, 401)
            return True
        MFA_PENDING.pop(str(a.get("ticket")), None)
        tok = secrets.token_hex(32)
        SESSIONS[tok] = {"uid": u["id"], "exp": time.time() + SESSION_TTL}
        db.x("UPDATE users SET last_login=? WHERE id=?", db.now(), u["id"])  # noqa
        audit(u["username"], "login ok (two-step)", ip)
        send_json(h, {"ok": True, "must_change_pw": bool(u.get("must_change_pw"))},
                  extra_headers=[("Set-Cookie", f"kbsess={tok}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_TTL}" + ("; Secure" if getattr(h, "tls", False) else ""))])
        return True
    if path == "/setup" and method == "POST":           # first-run: create the first admin with the server's setup code
        if not same_origin(h):
            send_json(h, {"error": "bad origin"}, 403)
            return True
        code = setup_code()
        if code is None:
            send_json(h, {"error": "this server already has an admin - sign in"}, 409)
            return True
        a = read_json(h)
        if not hmac.compare_digest(str(a.get("code", "")).strip().upper(), code):
            time.sleep(2)
            audit("?", "first-run setup FAILED (wrong setup code)", ip)
            send_json(h, {"error": "wrong setup code - on the server run: sudo pxectl setup-code"}, 403)
            return True
        un = str(a.get("username", "")).strip().lower()
        if not re.fullmatch(r"[a-z0-9._-]{2,40}", un):
            send_json(h, {"error": "user name: 2-40 lowercase letters, digits, . _ -"}, 400)
            return True
        db.x("INSERT INTO users(username, display, role, created) VALUES(?,?,?,?)", un, "Administrator", "admin", db.now())
        err = set_password(db.q1("SELECT id FROM users WHERE username=?", un)["id"], str(a.get("password", "")))
        if err:
            db.x("DELETE FROM users WHERE username=?", un)
            send_json(h, {"error": err}, 400)
            return True
        setup_code()                                     # an admin exists now: the code is deleted
        audit(un, "first-run setup: first admin created with the setup code", ip)
        send_json(h, {"ok": True})
        return True
    if path.startswith("/sso/"):
        return sso_routes(h, method, path, q, ip)
    if path == "/logout":
        m = re.search(r"(?:^|;\s*)kbsess=([0-9a-f]{64})", h.headers.get("Cookie", ""))
        if m:
            SESSIONS.pop(m.group(1), None)
        send_json(h, {"ok": True}, extra_headers=[("Set-Cookie", "kbsess=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")])
        return True

    user = h.fed_preauth if _preauth else session_of(h)
    if not user:
        send_json(h, {"error": "login required", "configured": users_exist(),
                      "default_login": bool(db.get_setting("default_login_active")), "server": server_card(h)}, 401)
        return True
    if method == "POST" and not _preauth and not same_origin(h):
        send_json(h, {"error": "bad origin"}, 403)
        return True
    who, role = user["username"], user["role"]
    if user.get("must_change_pw") and not user.get("federated") and not _preauth \
            and path not in ("/api/overview", "/api/me/password"):
        send_json(h, {"error": "choose a new password first", "must_change_pw": True}, 403)
        return True
    if not user.get("federated") and not _preauth and not user.get("mfa_enabled") and mfa_required_for(user) \
            and path not in ("/api/overview", "/api/me/mfa", "/api/me/password"):
        send_json(h, {"error": "set up two-step sign-in first", "mfa_enroll": True}, 403)
        return True

    # ---- SAN: iSCSI LUNs served from the OS library (diskless rack / DC servers), manager+ ----
    if path.startswith("/api/san"):
        import san
        if RANK[role] < RANK["manager"]:
            send_json(h, {"error": "SAN management needs the manager role or higher"}, 403); return True
        if path == "/api/san" and method == "GET":
            send_json(h, {"target": san.TARGET_IQN, "target_up": san.target_up(), "server": S.MY_IP,
                          "backstore": san.DEFAULT_BACKSTORE, "luns": san.luns()}); return True
        try:
            if path == "/api/san/lun" and method == "POST":
                a = read_json(h); op = a.get("op")
                if op == "create":
                    num = san.create_lun(a.get("name"), int(a.get("size_mb") or 0), 1 if a.get("golden") else 0,
                                         a.get("backstore"), a.get("note", ""))
                    audit(who, "san create lun %s #%s" % (a.get("name"), num), ip); send_json(h, {"ok": True, "num": num}); return True
                if op == "clone":
                    mac = S.norm_mac(a["mac"]) if a.get("mac") else None
                    num = san.clone(a.get("golden"), a.get("name"), mac, a.get("note", ""))
                    if mac: S.set_default(mac, "san:" + a.get("name"))
                    audit(who, "san clone %s -> %s #%s" % (a.get("golden"), a.get("name"), num), ip); send_json(h, {"ok": True, "num": num}); return True
                if op == "assign":
                    mac = S.norm_mac(a["mac"]); san.assign(mac, a.get("name")); S.set_default(mac, "san:" + a.get("name"))
                    audit(who, "san assign %s -> %s" % (mac, a.get("name")), ip); send_json(h, {"ok": True}); return True
                if op == "provision":   # write an OS image onto a LUN (seed a golden). src = http(s) URL or a file in the OS image library.
                    src = (a.get("src") or "").strip()
                    if not src.startswith(("http://", "https://")):
                        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "http", "images")
                        cand = os.path.realpath(os.path.join(base, src))
                        if not (cand == base or cand.startswith(base + os.sep)) or not os.path.isfile(cand):
                            send_json(h, {"error": "provision src must be an http(s) URL or a file under the OS image library (http/images/)"}, 400); return True
                        src = cand
                    san.provision(a.get("name"), src)
                    audit(who, "san provision %s <- %s" % (a.get("name"), a.get("src")), ip); send_json(h, {"ok": True}); return True
                if op == "delete":
                    if role != "admin":
                        send_json(h, {"error": "only an admin deletes a LUN"}, 403); return True
                    san.delete_lun(a.get("name")); audit(who, "san delete lun %s" % a.get("name"), ip); send_json(h, {"ok": True}); return True
                send_json(h, {"error": "unknown san op"}, 400); return True
        except (ValueError, KeyError) as e:
            send_json(h, {"error": str(e)}, 400); return True
        send_json(h, {"error": "not found"}, 404); return True

    # ---- netbar / diskless-seat session management (front desk: operator+) ----
    if path.startswith("/api/netbar"):
        import netbar
        import homes
        if RANK[role] < RANK["operator"]:
            send_json(h, {"error": "the front desk needs the operator role or higher"}, 403); return True
        seat_macs = sorted(set([m["mac"] for m in db.q("SELECT mac FROM machines WHERE next_action='seat'")]
                                + [s["mac"] for s in db.q("SELECT DISTINCT mac FROM netbar_sessions WHERE state='active'")]))
        if path == "/api/netbar" and method == "GET":
            mems = netbar.members()
            for m in mems:
                hi = homes.home_info(m["id"])
                m["home_bytes"] = hi["bytes"]; m["home_updated"] = hi["updated"]
            send_json(h, {"enabled": netbar.enabled(), "seats": netbar.seats_overview(seat_macs), "members": mems,
                          "products": netbar.products(True), "rate_per_hour": netbar.rate_per_hour(), "currency": netbar.currency(),
                          "print_per_page": netbar.print_price(), "homes": homes.homes_summary(),
                          "takings": netbar.takings_today(), "ledger": netbar.ledger(50)})
            return True
        if path == "/api/netbar/report" and method == "GET":
            try: rdays = int(q.get("days", ["14"])[0])
            except Exception: rdays = 14
            send_json(h, netbar.report(rdays)); return True
        try:
            if path == "/api/netbar/member" and method == "POST":
                a = read_json(h); op = a.get("op")
                if op == "add": netbar.add_member(a.get("name"), a.get("code") or None, float(a.get("minutes") or 0), who)
                elif op == "topup": netbar.topup(int(a["id"]), float(a["minutes"]), who)
                elif op == "disable": db.x("UPDATE netbar_members SET disabled=? WHERE id=?", 1 if a.get("disabled", True) else 0, int(a["id"]))
                elif op == "resethome": homes.reset_home(int(a["id"]))
                else: send_json(h, {"error": "unknown member op"}, 400); return True
                audit(who, "netbar member " + str(op), ip); send_json(h, {"ok": True}); return True
            if path == "/api/netbar/session" and method == "POST":
                a = read_json(h); op = a.get("op"); mac = S.norm_mac(a.get("mac", ""))
                if op == "start": netbar.start_session(mac, int(a["member_id"]) if a.get("member_id") else None,
                                                        float(a["minutes"]) if a.get("minutes") else None, a.get("who", ""), who)
                elif op == "stop": netbar.stop_session(mac, who)
                elif op == "addtime": netbar.add_time(mac, float(a["minutes"]), who)
                else: send_json(h, {"error": "unknown session op"}, 400); return True
                audit(who, "netbar session " + str(op) + " " + mac, ip); send_json(h, {"ok": True, "seat": netbar.session_for_seat(mac)}); return True
            if path == "/api/netbar/product" and method == "POST":
                a = read_json(h); op = a.get("op")
                if op == "add": netbar.add_product(a.get("name"), a.get("price"), a.get("stock"), a.get("category", ""), who)
                elif op == "update": netbar.update_product(int(a["id"]), **{k: a[k] for k in ("name", "price", "stock", "category", "disabled") if k in a})
                else: send_json(h, {"error": "unknown product op"}, 400); return True
                audit(who, "netbar product " + str(op), ip); send_json(h, {"ok": True}); return True
            if path == "/api/netbar/sell" and method == "POST":
                a = read_json(h)
                r = netbar.sell(a.get("items") or [], int(a["member_id"]) if a.get("member_id") else None, who)
                audit(who, "netbar sale %s %s" % (r["total"], r["paid"]), ip); send_json(h, {"ok": True, "receipt": r}); return True
            if path == "/api/netbar/print" and method == "POST":
                a = read_json(h)
                r = netbar.print_charge(int(a.get("pages") or 0), S.norm_mac(a["mac"]) if a.get("mac") else None,
                                        int(a["member_id"]) if a.get("member_id") else None,
                                        int(a.get("copies") or 1), a.get("doc", ""), who)
                audit(who, "netbar print %s pages %s" % (r.get("pages"), r.get("amount")), ip)
                send_json(h, {"ok": True, "receipt": r}); return True
            if path == "/api/netbar/rate" and method == "POST":
                if role != "admin": send_json(h, {"error": "only an admin sets the rate / enables billing"}, 403); return True
                a = read_json(h)
                if "per_hour" in a: netbar.set_rate(float(a["per_hour"]), a.get("currency"))
                if "enabled" in a: netbar.set_enabled(bool(a["enabled"]))
                if "print_per_page" in a: netbar.set_print_price(float(a["print_per_page"]))
                if "home_enabled" in a: homes.set_enabled(bool(a["home_enabled"]))
                if "home_cap_mb" in a: homes.set_cap_mb(int(a["home_cap_mb"]))
                if "home_dir" in a: homes.set_home_dir((a.get("home_dir") or "").strip())
                if "home_enabled" in a or "home_cap_mb" in a or "home_dir" in a: homes.init()
                audit(who, "netbar rate/enable set", ip); send_json(h, {"ok": True, "enabled": netbar.enabled()}); return True
        except Exception as e:       # noqa: BLE001 - surface the engine's validation message to the operator
            send_json(h, {"error": str(e)}, 400); return True
        send_json(h, {"error": "no such netbar endpoint"}, 404); return True

    # ---- signage / lab orchestration (front desk: operator+) ----
    if path.startswith("/api/signage"):
        import signage
        if RANK[role] < RANK["operator"]:
            send_json(h, {"error": "signage needs the operator role or higher"}, 403); return True
        if path == "/api/signage" and method == "GET":
            send_json(h, signage.overview()); return True
        if path == "/api/signage/report" and method == "GET":
            try: sdays = int(q.get("days", ["7"])[0])
            except Exception: sdays = 7
            send_json(h, {"proof_of_play": signage.proof_of_play(sdays)}); return True
        try:
            if path == "/api/signage/content" and method == "POST":
                a = read_json(h); op = a.get("op")
                if op == "add": signage.add_content(a.get("name"), a.get("src"), a.get("kind", "image"), float(a.get("duration") or 10), who)
                elif op == "update": signage.update_content(int(a["id"]), **{k: a[k] for k in ("name", "kind", "src", "duration", "disabled") if k in a})
                else: send_json(h, {"error": "unknown content op"}, 400); return True
                audit(who, "signage content " + str(op), ip); send_json(h, {"ok": True}); return True
            if path == "/api/signage/playlist" and method == "POST":
                a = read_json(h); op = a.get("op")
                if op == "add": r = {"id": signage.add_playlist(a.get("name"), who)}
                elif op == "items": signage.set_playlist_items(int(a["id"]), a.get("content_ids") or []); r = {"ok": True}
                else: send_json(h, {"error": "unknown playlist op"}, 400); return True
                audit(who, "signage playlist " + str(op), ip); send_json(h, {"ok": True, **r}); return True
            if path == "/api/signage/zone" and method == "POST":
                a = read_json(h); op = a.get("op")
                if op == "add": r = {"id": signage.add_zone(a.get("name"), a.get("note", ""), who)}
                elif op == "macs": signage.set_zone_macs(int(a["id"]), [S.norm_mac(m) for m in (a.get("macs") or [])]); r = {"ok": True}
                else: send_json(h, {"error": "unknown zone op"}, 400); return True
                audit(who, "signage zone " + str(op), ip); send_json(h, {"ok": True, **r}); return True
            if path == "/api/signage/schedule" and method == "POST":
                a = read_json(h)
                signage.add_schedule(int(a["zone_id"]), int(a["playlist_id"]), a.get("days", "0123456"),
                                     int(a.get("start_min") or 0), int(a.get("end_min") or 1440), int(a.get("priority") or 0), who)
                audit(who, "signage schedule add", ip); send_json(h, {"ok": True}); return True
            if path == "/api/signage/exam" and method == "POST":
                a = read_json(h)
                if a.get("op") == "clear": signage.clear_exam(a.get("scope", "mac"), a.get("ref"))
                else: signage.set_exam(a.get("scope", "mac"), a.get("ref"), a.get("url"), float(a.get("minutes") or 120), who)
                audit(who, "signage exam " + str(a.get("op", "set")), ip); send_json(h, {"ok": True}); return True
            if path == "/api/signage/enabled" and method == "POST":
                if role != "admin": send_json(h, {"error": "only an admin turns signage on/off"}, 403); return True
                a = read_json(h); signage.set_enabled(bool(a.get("enabled")))
                audit(who, "signage enabled set", ip); send_json(h, {"ok": True, "enabled": signage.enabled()}); return True
        except Exception as e:       # noqa: BLE001
            send_json(h, {"error": str(e)}, 400); return True
        send_json(h, {"error": "no such signage endpoint"}, 404); return True

    # ---- own account: two-step sign-in (authenticator app)
    if path == "/api/me/mfa" and not user.get("federated"):
        a = read_json(h) if method == "POST" else {}
        op = a.get("op")
        if method == "GET":
            send_json(h, {"enabled": bool(user.get("mfa_enabled")), "required": mfa_required_for(user),
                          "recovery_left": len(json.loads(user.get("mfa_recovery") or "[]")),
                          "policy": (db.get_setting("mfa_policy", "") or "") if role == "admin" else None})
            return True
        if op == "start":                                # a new secret, pending until a code confirms it
            if user.get("mfa_enabled"):
                send_json(h, {"error": "two-step sign-in is already on - turn it off first"}, 409)
                return True
            sec = mfa.new_secret()
            db.x("UPDATE users SET mfa_secret=? WHERE id=?", sec, user["id"])
            u = mfa.uri(sec, f"{who}@{S.CFG.get('server_name') or socket.gethostname()}")
            send_json(h, {"secret": sec, "uri": u, "qr": mfa.qr_svg(u)})
            return True
        if op == "confirm":
            step = mfa.verify(user.get("mfa_secret"), a.get("code", ""), 0)
            if not step:
                send_json(h, {"error": "wrong code - check the phone's time is automatic, then type the current code"}, 400)
                return True
            codes, hashes = mfa.new_recovery_codes()
            db.x("UPDATE users SET mfa_enabled=1, mfa_last=?, mfa_recovery=? WHERE id=?", step, json.dumps(hashes), user["id"])
            audit(who, "two-step sign-in turned on", ip)
            send_json(h, {"ok": True, "recovery_codes": codes})
            return True
        if op in ("disable", "new_codes"):               # both need a current code (or a recovery code)
            code = str(a.get("code", "")).strip()
            ok = mfa.verify(user.get("mfa_secret"), code, user.get("mfa_last") or 0) or \
                ("-" in code and mfa.use_recovery_code(code, json.loads(user.get("mfa_recovery") or "[]")) is not None)
            if not ok:
                send_json(h, {"error": "wrong code"}, 400)
                return True
            if op == "disable":
                if mfa_required_for(user):
                    send_json(h, {"error": "two-step sign-in is required for your account - an admin can reset it"}, 403)
                    return True
                db.x("UPDATE users SET mfa_enabled=0, mfa_secret=NULL, mfa_recovery=NULL, mfa_last=0 WHERE id=?", user["id"])
                audit(who, "two-step sign-in turned off", ip)
                send_json(h, {"ok": True})
            else:
                codes, hashes = mfa.new_recovery_codes()
                db.x("UPDATE users SET mfa_recovery=? WHERE id=?", json.dumps(hashes), user["id"])
                audit(who, "new two-step recovery codes", ip)
                send_json(h, {"ok": True, "recovery_codes": codes})
            return True
        send_json(h, {"error": "unknown operation"}, 400)
        return True

    # ---- own account
    if path == "/api/me/profile" and method == "POST":         # own contact details + avatar (anyone signed in)
        a = read_json(h, limit=600_000)
        try:
            save_profile(user["id"], a)
            if "display" in a:
                d = str(a.get("display") or "").strip()[:80]
                if any(ord(c) < 32 for c in d):
                    raise ValueError("display name: no control characters")
                db.x("UPDATE users SET display=? WHERE id=?", d, user["id"])
        except ValueError as e:
            send_json(h, {"error": str(e)}, 400)
            return True
        audit(who, "profile updated (own)", ip)
        send_json(h, {"ok": True, "profile": db.profile(user["id"])})
        return True
    if path == "/api/avatar" and method == "GET":               # ?u=<username>: the avatar image, or 404 (the page draws initials)
        r = db.q1("SELECT avatar, avatar_type FROM users WHERE username=?", q.get("u", [""])[0])
        if not r or not r["avatar"]:
            send_json(h, {"error": "no avatar"}, 404)
            return True
        h.send_response(200)
        h.send_header("Content-Type", r["avatar_type"])
        h.send_header("X-Content-Type-Options", "nosniff")
        h.send_header("Cache-Control", "private, max-age=300")
        h.send_header("Content-Length", str(len(r["avatar"])))
        h.end_headers()
        h.wfile.write(r["avatar"])
        return True
    if path == "/api/me/password" and method == "POST" and not user.get("federated"):
        a = read_json(h)
        if not check_login(who, str(a.get("current", ""))):
            time.sleep(1.5)
            send_json(h, {"error": "current password is wrong"}, 403)
            return True
        err = set_password(user["id"], str(a.get("new", "")))
        if err:
            send_json(h, {"error": err}, 400)
            return True
        db.x("UPDATE users SET must_change_pw=0 WHERE id=?", user["id"])
        if who == "admin":
            db.set_setting("default_login_active", None)    # the default admin / admin is gone
        audit(who, "password changed (own)", ip)
        send_json(h, {"ok": True})
        return True
    if path.startswith("/api/fed/"):
        return hub_api(h, method, path, q, user, ip)
    if path == "/api/admin/mfa-policy" and role == "admin" and method == "POST":   # whole server: "" optional | admins | all
        pol = str(read_json(h).get("policy", ""))
        if pol not in ("", "admins", "all"):
            send_json(h, {"error": "policy: '', admins or all"}, 400)
            return True
        db.set_setting("mfa_policy", pol or None)
        audit(who, f"two-step sign-in policy: {pol or 'optional'}", ip)
        send_json(h, {"ok": True})
        return True
    if path == "/api/admin/sso" and role == "admin":
        try:
            if method == "GET":
                https_port = S.CFG.get("ui_https_port")
                send_json(h, {"providers": sso.admin_view(), "https_port": https_port,
                              "redirect_uri": f"https://{h.headers.get('Host', S.MY_IP).split(':')[0]}:{https_port}/sso/callback" if https_port else None})
                return True
            a = read_json(h)
            if a.get("op") == "save":
                cfg = sso.save_provider(a.get("provider") or {}, a.get("secret") or None)
                audit(who, f"SSO provider saved: {cfg['id']} {cfg['issuer']} domains {cfg['domains']} rules {len(cfg['group_rules'])}"
                           f"{' (secret changed)' if a.get('secret') else ''}", ip)
            elif a.get("op") == "delete":
                sso.delete_provider(str(a.get("id", "")))
                audit(who, f"SSO provider deleted: {a.get('id')}", ip)
            else:
                raise sso.SSOError("op: save or delete")
            send_json(h, {"ok": True})
        except sso.SSOError as e:
            send_json(h, {"error": str(e)}, 400)
        return True
    if path == "/api/admin/saml" and role == "admin":
        try:
            if not saml.available():
                send_json(h, {"error": "SAML support is not installed on this server (python3-xmlsec, python3-lxml)", "providers": []}, 200 if method == "GET" else 400)
                return True
            if method == "GET":
                https_port = S.CFG.get("ui_https_port")
                host = h.headers.get("Host", S.MY_IP).split(":")[0]
                send_json(h, {"providers": saml.admin_view(), "https_port": https_port,
                              "acs_url": f"https://{host}:{https_port}/sso/saml/acs" if https_port else None,
                              "sp_entity_id": "kwickboot"})
                return True
            a = read_json(h)
            if a.get("op") == "save":
                cfg = saml.save_provider(a.get("provider") or {})
                audit(who, f"SAML provider saved: {cfg['id']} issuer {cfg['idp_entity_id']} domains {cfg['domains']} rules {len(cfg['group_rules'])}", ip)
            elif a.get("op") == "delete":
                saml.delete_provider(str(a.get("id", "")))
                audit(who, f"SAML provider deleted: {a.get('id')}", ip)
            else:
                raise saml.SAMLError("op: save or delete")
            send_json(h, {"ok": True})
        except saml.SAMLError as e:
            send_json(h, {"error": str(e)}, 400)
        return True
    if path.startswith("/api/admin/"):
        if role != "admin":
            send_json(h, {"error": "admin only"}, 403)
            return True
        if path == "/api/admin/ppkg" and method == "POST":     # raw body: a department's Entra provisioning package
            code, obj = ppkg_upload(h, q, who, ip)
            send_json(h, obj, code)
            return True
        return admin_api(h, method, path, who, ip)

    # ---- read API
    if path == "/api/overview" and method == "GET":
        disc = S.discovery_open()
        until = db.get_setting("discovery_until")
        ids, unassigned = scope(user)
        names = dept_names()
        mine = [{"id": d, "name": names.get(d, "?")} for d in (ids if ids is not None else names)]
        send_json(h, {"server": f"{S.MY_IP}:{S.HTTP_PORT}",
                      "me": {"user": who, "role": role, "must_change_pw": bool(user.get("must_change_pw")),
                             "mfa_enabled": bool(user.get("mfa_enabled")), "mfa_enroll": bool(not user.get("federated") and not user.get("mfa_enabled") and mfa_required_for(user)),
                             "display": user.get("display") or "", "profile": db.profile(user["id"]) if user.get("id") else {}},
                      "departments": mine, "discovery": disc, "discovery_always": until == "always",
                      "discovery_until": int(float(until)) if disc and until and until != "always" else None,
                      "machines": [summary(m) for m in db.machines(ids, unassigned)], "recipes": recipes(),
                      "log": rd(S.LOG, 6000).splitlines()[-25:] if role == "admin" else [],
                      "boot_conflicts": S.dhcp_conflicts_current() if role == "admin" and hasattr(S, "dhcp_conflicts_current") else [],
                      "terms": {"version": TERMS_VERSION, "notice": TERMS_NOTICE, "url": TERMS_URL,
                                "accepted": terms_accepted()}})
        return True
    if path == "/api/terms" and method == "POST":       # an admin accepts the data-responsibility notice (audit = evidence)
        if role != "admin":
            send_json(h, {"error": "an admin accepts this notice for the organisation"}, 403)
            return True
        db.set_setting("terms_ack", json.dumps({"version": TERMS_VERSION, "by": who, "at": time.time()}))
        audit(who, f"accepted the data-responsibility notice v{TERMS_VERSION} ({TERMS_URL}) for this organisation: \"{TERMS_NOTICE}\"", ip)
        send_json(h, {"ok": True})
        return True
    mac = S.norm_mac(q.get("mac", [""])[0])
    if path == "/api/machine" and method == "GET":
        m = db.q1("SELECT m.*, d.name AS department FROM machines m LEFT JOIN departments d ON d.id=m.department_id WHERE mac=?", mac)
        if not m or not can_access(user, m):
            send_json(h, {"error": "unknown machine"}, 404)
            return True
        inv, _ = db.latest_report(mac, "inventory")
        pf, _ = db.latest_report(mac, "preflight")
        send_json(h, {"summary": dict(summary(m), department_id=m["department_id"]), "inventory": inv, "preflight": pf,
                      "policy": dict(db.policy(m["department_id"]), win_custom_me=perm(user, db.policy(m["department_id"]), "win_custom")), "health": json.loads(m.get("health") or "null"), "tuning": health.tune(inv),
                      "bmc": bmc.public(mac), "endpoint": endpoint.status(mac), "djoin": S.djoin_info(mac), "entra": S.ppkg_info(m["department_id"])["present"], "drivers": win_driver_match(mac), "driver_offer": win_driver_offer(mac),
                      "disks": disks_from_inventory(mac), "jobs": db.jobs(mac)})
        return True
    if path == "/api/joblog" and method == "GET":
        jid = os.path.basename(q.get("job", [""])[0])
        m = db.machine(mac)
        if not m or not can_access(user, m):
            send_json(h, {"error": "unknown machine"}, 404)
            return True
        send_json(h, {"log": crfold(rd(os.path.join(S.STATE, mac.replace(":", "-"), "jobs", jid + ".log"), 400_000))})
        return True

    # ---- actions (POST)
    if path == "/api/action" and method == "POST":
        code, obj = do_action(user, read_json(h), ip)
        send_json(h, obj, code)
        return True
    if path == "/api/schedules":                              # later / repeating actions (schedules.py)
        import schedules
        if method == "GET":
            send_json(h, {"schedules": schedules.visible(user, None), "now": int(time.time())})
            return True
        a = read_json(h)
        try:
            if a.get("op") == "create":
                sid, name = schedules.create(user, a)
                audit(who, f"SCHEDULE created '{name}' [{sid}] {a.get('repeat', 'once')} at "
                           f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(int(float(a['run_at']))))}", ip)
                send_json(h, {"ok": True, "id": sid})
            else:
                name, op = schedules.change(user, a)
                audit(who, f"SCHEDULE '{name}' {op}", ip)
                send_json(h, {"ok": True})
        except ValueError as e:
            send_json(h, {"error": str(e)}, 400)
        return True
    if path == "/api/bulk" and method == "POST":
        code, obj = do_bulk(user, read_json(h), ip)
        send_json(h, obj, code)
        return True
    if path == "/api/jobs" and method == "GET":
        ids, unassigned = scope(user)
        vis = {m["mac"]: m["name"] for m in db.machines(ids, unassigned)}
        rows = db.q("SELECT * FROM jobs ORDER BY created DESC, id DESC LIMIT 400")
        out = []
        for j in rows:
            if j["mac"] in vis:
                j["params"] = {k: v for k, v in json.loads(j["params"] or "{}").items() if k not in ("WIN_FB", "EP_TOKEN")}
                j["machine"] = vis[j["mac"]]
                out.append(j)
        send_json(h, {"jobs": out[:200]})
        return True
    if path == "/api/bmc":
        code, obj = bmc_api(user, method, q, read_json(h) if method == "POST" else {}, ip)
        send_json(h, obj, code)
        return True
    if path == "/api/profiles":
        ids, _ = scope(user)
        if method == "GET":
            send_json(h, {"profiles": endpoint.profiles(ids)})
            return True
        if RANK[role] < RANK["manager"]:
            send_json(h, {"error": "needs role manager"}, 403)
            return True
        a = read_json(h)
        try:
            dept = a.get("department_id")
            dept = int(dept) if str(dept or "").isdigit() else None
            if role != "admin" and dept not in (ids or []):
                raise ValueError("managers create profiles for their own departments")
            if a.get("op") == "delete":
                p = db.q1("SELECT * FROM profiles WHERE id=?", int(a.get("id", 0)))
                if not p or (role != "admin" and p["department_id"] not in (ids or [])):
                    raise ValueError("unknown profile")
                db.x("UPDATE machines SET profile_id=NULL WHERE profile_id=?", p["id"])
                db.x("DELETE FROM profiles WHERE id=?", p["id"])
                audit(who, f"profile deleted: {p['name']}", ip)
            else:
                pid = endpoint.save_profile(int(a.get("id") or 0) or None, a.get("name"), a.get("kind", "kiosk"), dept, a.get("config") or {})
                audit(who, f"profile saved: {a.get('name')} {a.get('config')}", ip)
            send_json(h, {"ok": True})
        except (ValueError, TypeError) as e:
            send_json(h, {"error": str(e)}, 400)
        return True
    if path == "/api/screen" and method == "GET":
        m = db.machine(mac := S.norm_mac(q.get("mac", [""])[0]))
        f = os.path.join(S.STATE, mac.replace(":", "-"), "screen.png")
        if not m or not can_access(user, m) or not os.path.isfile(f):
            send_json(h, {"error": "no screenshot"}, 404)
            return True
        data = open(f, "rb").read()
        h.send_response(200)
        h.send_header("Content-Type", "image/png")
        h.send_header("Cache-Control", "no-store")
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)
        return True
    if path == "/api/tools" and method == "GET":            # OS library tools (images/tools): what this user may start
        send_json(h, {"tools": [{"name": t["NAME"], "title": t.get("TITLE", t["NAME"]), "desc": t.get("DESC", ""),
                                 "role": t.get("ROLE") or "admin", "size": t["SIZE"], "built": t.get("BUILT", ""),
                                 "may": RANK[role] >= RANK.get(t.get("ROLE") or "admin", 3)} for t in S.tools()]})
        return True
    if path == "/api/isos" and method == "GET":             # ?mac= -> what this user may use on that machine
        if q.get("mac"):
            m = db.machine(S.norm_mac(q.get("mac", [""])[0]))
            if not m or not can_access(user, m):
                send_json(h, {"error": "unknown machine"}, 404)
                return True
            pol = db.policy(m["department_id"])
            up = perm(user, pol, "iso_upload") and RANK[role] >= RANK["operator"]
            send_json(h, {"isos": isos_for(m["department_id"], pol, user), "manual_install": perm(user, pol, "manual_install"),
                          "iso_upload": up, "department_id": m["department_id"],
                          "upload_to": ([{"to": "shared", "text": "Shared library (all departments)"}] if role == "admin" else []) +
                                       ([{"to": f"dept:{m['department_id']}", "text": "This department"}] if up and m["department_id"] is not None
                                        and (role == "admin" or RANK[role] >= RANK["manager"]) else []) +
                                       ([{"to": "me", "text": "Only me (personal)"}] if up else [])})
            return True
        mine = None if role == "admin" else set(user_depts(user))
        send_json(h, {"isos": [x for x in iso_library() if mine is None or (x["department_id"] is None and not x["owner_user"])
                               or x["department_id"] in mine or x["owner_user"] == who]})
        return True
    if path == "/api/advice" and method == "GET":             # install advisor: a proposal from the real hardware; the user decides
        import advisor
        mac = S.norm_mac(q.get("mac", [""])[0])
        m = db.machine(mac)
        if not m or not can_access(user, m):
            send_json(h, {"error": "unknown machine"}, 404)
            return True
        inv, _ = db.latest_report(mac, "inventory")
        if not inv:
            send_json(h, {"error": "no hardware scan yet - boot the machine into the RAM OS"}, 409)
            return True
        pf, _ = db.latest_report(mac, "preflight")
        purpose = q.get("purpose", ["auto"])[0]
        if purpose != "auto" and purpose not in advisor.PURPOSES:
            purpose = "auto"
        pol = db.policy(m["department_id"])
        cat = [r for r in recipes() if not pol["allowed_os"] or r["id"] in pol["allowed_os"]]
        out = advisor.rules(advisor.facts(inv, pf), cat, purpose)
        out["purposes"] = advisor.PURPOSES
        out["allow_override"] = pol.get("allow_override", True)
        send_json(h, out)
        return True
    if path == "/api/ai/providers":                          # AI services: everyone's (admins), department's, personal (BYOK)
        import ai
        udepts = user_depts(user)

        def may(scope, owner):
            if role == "admin":
                return scope != "user" or owner == who
            if scope == "dept":
                return RANK[role] >= RANK["manager"] and owner in udepts and perm(user, db.policy(owner), "ai_byok")
            if scope == "user":
                return owner == who and RANK[role] >= RANK["operator"] and \
                    any(perm(user, db.policy(d), "ai_byok") for d in (udepts or [None]))
            return False
        if method == "GET":
            if q.get("mac"):
                m = db.machine(S.norm_mac(q.get("mac", [""])[0]))
                if not m or not can_access(user, m):
                    send_json(h, {"error": "unknown machine"}, 404)
                    return True
                ps = [p for p in ai.providers(user, udepts, m["department_id"]) if p.get("enabled", True)]
            else:
                ps = ai.providers(user, udepts)
            names = dept_names()
            for p in ps:
                p["can_edit"] = may(p["scope"], p["owner"])
                p["scope_label"] = {"system": "Everyone", "user": "Only me"}.get(p["scope"]) or names.get(p["owner"], "?")
            send_json(h, {"providers": ps, "can_add": {"system": role == "admin", "user": may("user", who),
                                                       "dept": [{"id": d, "name": names.get(d, "?")} for d in (names if role == "admin" else udepts) if may("dept", d)]}})
            return True
        a = read_json(h)
        try:
            if a.get("op") == "save":
                pid, name, sc, own = ai.save_provider(a, user, may)
                where = {"system": "everyone", "user": "personal"}.get(sc) or f"department {dept_names().get(own, own)}"
                audit(who, f"AI service saved ({where}): {name} ({a.get('kind')}, {a.get('model')}, {'local' if a.get('local') else 'cloud'})"
                           f"{' - key updated' if a.get('api_key') else ''}", ip)
                send_json(h, {"ok": True, "id": pid})
            elif a.get("op") == "delete":
                name = ai.delete_provider(str(a.get("id")), may)
                audit(who, f"AI service deleted: {name}", ip)
                send_json(h, {"ok": True})
            elif a.get("op") == "test":
                p = next((x for x in ai.providers(user, udepts) if x["id"] == str(a.get("id"))), None)
                if not p or not may(p["scope"], p["owner"]):
                    raise PermissionError("you may only test AI services you manage")
                send_json(h, ai.test(p["id"]))
            else:
                send_json(h, {"error": "op: save, delete or test"}, 400)
        except PermissionError as e:
            send_json(h, {"error": str(e)}, 403)
        except ValueError as e:
            send_json(h, {"error": str(e)}, 400)
        except ai.AIError as e:
            send_json(h, {"error": str(e)}, 502)
        return True
    if path == "/api/advice/ai" and method == "POST":         # the AI's plan for one machine (proposal only)
        import advisor
        import ai
        a = read_json(h)
        mac = S.norm_mac(a.get("mac", ""))
        m = db.machine(mac)
        if not m or not can_access(user, m):
            send_json(h, {"error": "unknown machine"}, 404)
            return True
        pol = db.policy(m["department_id"])
        if not perm(user, pol, "ai_advice"):
            send_json(h, {"error": "AI advice is switched off for you in this department"}, 403)
            return True
        try:
            pid = ai.resolve(str(a.get("provider") or ""), user, user_depts(user), m["department_id"])
        except ai.AIError as e:
            send_json(h, {"error": str(e)}, 409)
            return True
        prov = next(p for p in ai.providers() if p["id"] == pid)
        if not prov.get("local") and not perm(user, pol, "ai_cloud"):
            send_json(h, {"error": f"'{prov['name']}' is a cloud service; this department allows local AI only"}, 403)
            return True
        inv, _ = db.latest_report(mac, "inventory")
        if not inv:
            send_json(h, {"error": "no hardware scan yet - boot the machine into the RAM OS"}, 409)
            return True
        pf, _ = db.latest_report(mac, "preflight")
        purpose = a.get("purpose") if a.get("purpose") in advisor.PURPOSES else "auto"
        cat = [r for r in recipes() if (not pol["allowed_os"] or r["id"] in pol["allowed_os"]) and r["id"] != "image"]
        f = advisor.facts(inv, pf)
        try:
            out = ai.plan(f, cat, advisor.rules(f, cat, purpose), purpose, str(a.get("request") or ""), pid)
        except ai.AIError as e:
            send_json(h, {"error": str(e)}, 502)
            return True
        out["allow_override"] = perm(user, pol, "allow_override")
        audit(who, f"AI plan for {m['name'] or mac} via {out['ai']['provider']} ({out['ai']['model']}, "
                   f"{'local' if out['ai']['local'] else 'cloud'}, {prov['scope']}, {out['ai']['seconds']} s): "
                   f"{out['recommended']['os']} on {out['recommended']['disk']}", ip)
        send_json(h, out)
        return True
    if path == "/api/certs/pubkey" and method == "GET":       # anyone with a login can verify certificates offline
        import certs
        pub, fp = certs.public_key()
        send_json(h, {"algorithm": "Ed25519", "public_key": pub, "fingerprint": fp})
        return True
    if path in ("/api/certs", "/api/cert", "/api/cert.pdf") and method == "GET":
        import certs
        if path == "/api/certs":
            mac = S.norm_mac(q.get("mac", [""])[0])
            m = db.machine(mac)
            if not m or not can_access(user, m):
                send_json(h, {"error": "unknown machine"}, 404)
                return True
            send_json(h, {"certs": [{"id": c["id"], "created": c["created"], "serial": c["serial"], "job": c["job"],
                                     "method": c["body"].get("method"), "nist": c["body"].get("nist_level"),
                                     "model": c["body"].get("disk", {}).get("model"), "valid": certs.verify(c["id"])}
                                    for c in certs.for_machine(mac)]})
            return True
        c = certs.get(q.get("id", [""])[0])
        m = db.machine(c["mac"]) if c else None
        if not c or not m or not can_access(user, m):
            send_json(h, {"error": "unknown certificate"}, 404)
            return True
        if path == "/api/cert":
            send_json(h, {"record": json.loads(c["body"]), "signature": c["sig"], "valid": certs.verify(c["id"])})
            return True
        data = certs.pdf(c["id"])
        h.send_response(200)
        h.send_header("Content-Type", "application/pdf")
        h.send_header("Content-Disposition", f'attachment; filename="kwickboot-wipe-{c["id"]}.pdf"')
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)
        return True
    if path == "/api/captures" and method == "GET":
        send_json(h, {"captures": captures()})
        return True
    if path == "/api/isos/upload" and method == "POST":     # browser upload of an installer ISO, in sequential chunks
        code, obj = iso_upload(h, user, q, ip)
        send_json(h, obj, code)
        return True
    if path == "/api/isos" and method == "POST":             # {"op": "delete", "name": ...}: admins any; managers their
        a = read_json(h)                                       # department's uploads; everyone their personal ones
        name = str(a.get("name", ""))
        iso = next((x for x in iso_library() if x["name"] == name), None)
        if not iso:
            send_json(h, {"error": "no such ISO"}, 404)
            return True
        ok = role == "admin" or iso["owner_user"] == who or (iso["department_id"] is not None and RANK[role] >= RANK["manager"]
                                                               and iso["department_id"] in user_depts(user))
        if a.get("op") != "delete" or not ok:
            send_json(h, {"error": "you may delete your own ISOs (managers: their department's); shared ones need role admin"}, 403)
            return True
        os.remove(os.path.join(S.HTTP_ROOT, "iso", name))
        db.drop_iso_meta(name)
        audit(who, f"ISO deleted: {name} ({iso['owner']})", ip)
        send_json(h, {"ok": True})
        return True
    if path == "/api/reports" and method == "GET":
        ids, unassigned = scope(user)
        rows = []
        for m in db.machines(ids, unassigned):
            hh = json.loads(m.get("health") or "null") or {}
            a = hh.get("asset", {})
            rows.append({"mac": m["mac"], "name": m["name"], "department": m.get("department") or "", "approved": bool(m["approved"]),
                         "vendor": a.get("vendor", ""), "product": a.get("product", ""), "serial": a.get("serial", ""),
                         "bios": " ".join(x for x in (a.get("bios_version", ""), a.get("bios_date", "")) if x),
                         "boot_mode": a.get("boot_mode", ""), "secure_boot": a.get("secure_boot", ""), "cpu": m.get("cpu") or "",
                         "ram": m.get("ram") or "", "disks": [f"{d['dev']} {d['size']} {d['model']} ({d['serial']})" for d in hh.get("disks", []) if d.get("tran") != "usb"],
                         "battery": [f"{b['health_pct']}%" for b in hh.get("battery", []) if b.get("health_pct") is not None],
                         "health": m.get("health_level") or "", "flags": hh.get("flags", []), "preflight": m.get("preflight") or "",
                         "discovered": m.get("discovered") or ""})
        vis = {r["mac"] for r in rows}
        inst = [j for j in db.q("SELECT mac, params, state, created, finished, created_by FROM jobs WHERE type='install' "
                                "AND created >= datetime('now', '-90 days', 'localtime') ORDER BY created DESC") if j["mac"] in vis]
        for j in inst:
            j["params"] = {k: v for k, v in json.loads(j["params"] or "{}").items() if k not in ("WIN_FB", "EP_TOKEN")}
        send_json(h, {"machines": rows, "installs": inst})
        return True
    if path == "/api/deployments/cards" and method == "GET":   # bulk deployments (same queue second, 2+ PCs) as report cards
        ids, unassigned = scope(user)
        vis = {m["mac"]: m for m in db.machines(ids, unassigned)}
        groups = {}
        for j in db.q("SELECT id, mac, type, params, state, created, finished, created_by FROM jobs WHERE type IN ('install','restore') "
                      "AND created >= datetime('now', '-30 days', 'localtime') ORDER BY created"):
            if j["mac"] not in vis:
                continue
            key = (j["id"][:15], j["type"], j["created_by"])          # YYYYMMDD-HHMMSS: one bulk action
            groups.setdefault(key, []).append(j)
        cards = []
        for (stamp, typ, by), js in groups.items():
            if len(js) < 2:
                continue
            p0 = json.loads(js[0]["params"] or "{}")
            what = (p0.get("NAME") if typ == "restore" else
                    ("Windows 11 " + {"pro": "Pro", "education": "Education", "enterprise": "Enterprise", "home": "Home"}.get(p0.get("VARIANT") or "pro", "")
                     if p0.get("OS") == "windows" else next((r["name"] for r in recipes() if r["id"] == p0.get("OS")), p0.get("OS", ""))))
            t0 = min(time.mktime(time.strptime(j["created"], "%Y-%m-%d %H:%M:%S")) for j in js)
            ends, verified, failed = [], 0, 0
            for j in js:
                fb = json.loads(vis[j["mac"]].get("firstboot") or "null") or {}
                if fb.get("job") != j["id"]:                      # an older install: its result was kept per job
                    try:
                        fb = dict(json.loads(db.get_setting("fb:" + j["id"]) or "{}"), job=j["id"])
                    except ValueError:
                        fb = {}
                if fb.get("job") == j["id"] and fb.get("state") == "ok":
                    verified += 1
                    ends.append(fb.get("at") or 0)
                elif j["state"] == "failed" or (fb.get("job") == j["id"] and fb.get("state") == "timeout"):
                    failed += 1
                elif j["state"] == "done" and j.get("finished") and typ == "restore":
                    verified += 1
                    ends.append(time.mktime(time.strptime(j["finished"], "%Y-%m-%d %H:%M:%S")))
            done = verified + failed == len(js)
            cards.append({"id": stamp, "type": typ, "what": what, "by": by, "pcs": len(js), "verified": verified, "failed": failed,
                          "multicast": p0.get("MODE") == "multicast", "started": t0, "done": done,
                          "minutes": round((max(ends) - t0) / 60, 1) if ends else None})
        cards.sort(key=lambda c: c["started"], reverse=True)
        send_json(h, {"cards": cards[:10]})
        return True
    if path in ("/api/reports/win11", "/api/reports/win11.csv") and method == "GET":   # Windows 11 readiness, one row per PC
        ids, unassigned = scope(user)
        rows = []
        for m in db.machines(ids, unassigned):
            inv, _ = db.latest_report(m["mac"], "inventory")
            if not inv:
                continue
            r = health.readiness(inv, m.get("health_level") or "")
            a = (json.loads(m.get("health") or "null") or {}).get("asset", {})
            rows.append({"name": m["name"], "mac": m["mac"], "department": m.get("department") or "",
                         "model": " ".join(x for x in (a.get("vendor", ""), a.get("product", "")) if x), "serial": a.get("serial", ""),
                         "cpu": a.get("cpu", ""), "lane": r["lane"], **r["checks"], "why": "; ".join(r["why"])})
        rows.sort(key=lambda x: ({"upgrade": 0, "second-life": 1, "retire": 2}[x["lane"]], x["name"]))
        if path.endswith(".csv"):
            import csv
            import io
            buf = io.StringIO()
            cols = ["name", "department", "model", "serial", "cpu", "lane", "cpu", "tpm2", "uefi", "secure_boot", "sb_2023",
                    "ram_gb", "disk_gb", "why"]
            cols = list(dict.fromkeys(cols))
            w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
            buf.write("\nGenerated by KwickBoot - kwickboot.com\n")
            body = buf.getvalue().encode("utf-8-sig")       # Excel opens UTF-8 with a BOM correctly
            h.send_response(200)
            h.send_header("Content-Type", "text/csv; charset=utf-8")
            h.send_header("Content-Disposition", 'attachment; filename="windows11-readiness.csv"')
            h.send_header("Content-Length", str(len(body)))
            h.end_headers()
            h.wfile.write(body)
            return True
        send_json(h, {"rows": rows})
        return True
    if path in ("/api/audit", "/api/audit.csv") and method == "GET":   # search: q (text), from/to (YYYY-MM-DD), limit
        if role != "admin":
            send_json(h, {"error": "admin only"}, 403)
            return True
        g = lambda k, d="": (q.get(k, [d])[0] or d).strip()
        where, args = [], []
        if g("q"):
            where.append("(who LIKE ? OR ip LIKE ? OR action LIKE ? OR detail LIKE ?)")
            args += ["%" + g("q").replace("%", "") + "%"] * 4
        for k, op, suf in (("from", ">=", " 00:00:00"), ("to", "<=", " 23:59:59")):
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", g(k)):
                where.append(f"ts {op} ?")
                args.append(g(k) + suf)
        sql = "SELECT id, ts, who, ip, action, detail, chain FROM audit" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC"
        if path == "/api/audit.csv":            # the full matching record set, for archives and auditors
            import csv
            import io
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["id", "time", "who", "from", "action", "detail", "chain_sha256"])
            for r in db.q(sql, *args):
                w.writerow([r["id"], r["ts"], r["who"], r["ip"] or "", r["action"],
                            ("'" + r["detail"]) if (r["detail"] or "")[:1] in "=+-@" else r["detail"] or "", r["chain"] or ""])
            data = buf.getvalue().encode("utf-8-sig")
            audit(who, f"audit export: {g('q') or 'all'} {g('from')}..{g('to')}", ip)
            h.send_response(200)
            h.send_header("Content-Type", "text/csv; charset=utf-8")
            h.send_header("Content-Disposition", f"attachment; filename=kwickboot-audit-{time.strftime('%Y%m%d-%H%M')}.csv")
            h.send_header("Content-Length", str(len(data)))
            h.end_headers()
            h.wfile.write(data)
            return True
        n = min(int(g("limit", "500")) if g("limit", "500").isdigit() else 500, 5000)
        send_json(h, {"audit": db.q(sql + " LIMIT ?", *args, n), "retention_days": int(db.get_setting("audit_keep_days", "0") or 0)})
        return True
    if path == "/api/audit/verify" and method == "GET":
        if role != "admin":
            send_json(h, {"error": "admin only"}, 403)
            return True
        send_json(h, db.audit_verify())
        return True
    if path == "/api/audit/retention" and method == "POST":
        if role != "admin":
            send_json(h, {"error": "admin only"}, 403)
            return True
        a = read_json(h)
        days = int(a.get("days", 0)) if str(a.get("days", 0)).isdigit() else -1
        if days < 0 or (days and days < 90):
            send_json(h, {"error": "keep records forever (0) or at least 90 days"}, 400)
            return True
        db.set_setting("audit_keep_days", days)
        audit(who, f"record retention set to {'forever' if not days else str(days) + ' days'}", ip)
        send_json(h, {"ok": True})
        return True
    if path == "/api/history" and method == "GET":            # one machine's record: jobs, actions on it, wipe certificates
        mac = S.norm_mac(q.get("mac", [""])[0])
        m = db.machine(mac)
        if not m or not can_access(user, m):
            send_json(h, {"error": "unknown machine"}, 404)
            return True
        ev = [{"ts": j["created"], "kind": "job", "who": j["created_by"], "text": f"{j['type']} {json.loads(j['params'] or '{}').get('OS', '')}".strip(),
               "state": j["state"], "error": j["error"] or "", "id": j["id"]}
              for j in db.q("SELECT * FROM jobs WHERE mac=? ORDER BY created DESC LIMIT 500", mac)]
        ev += [{"ts": r["ts"], "kind": "action", "who": r["who"], "text": r["detail"] or r["action"]}
               for r in db.q("SELECT * FROM audit WHERE detail LIKE ? ORDER BY id DESC LIMIT 500", "%" + mac + "%")]
        ev += [{"ts": c["created"], "kind": "certificate", "who": "", "text": f"secure wipe certificate {c['id']} for disk {c['serial']}", "id": c["id"]}
               for c in db.q("SELECT id, created, serial FROM wipe_certs WHERE mac=? ORDER BY created DESC", mac)]
        ev.sort(key=lambda e: e["ts"], reverse=True)
        send_json(h, {"events": ev[:800]})
        return True

    send_json(h, {"error": "not found"}, 404)
    return True


def do_action(user, a, ip):
    """One machine action, fully validated. Returns (http code, json object)."""
    who, role = user["username"], user["role"]
    jid = None
    act, mac = a.get("action", ""), S.norm_mac(a.get("mac", ""))
    if act not in NEEDS:
        return 400, {"error": "unknown action"}
    if RANK[role] < RANK[NEEDS[act]]:
        return 403, {"error": f"'{act}' needs role {NEEDS[act]} (you are {role})"}
    if act == "discover":
        if a.get("minutes") == "always":
            db.set_setting("discovery_until", "always")
            audit(who, "discovery set to ALWAYS open", ip)
            return 200, {"ok": True}
        mins = int(a.get("minutes", 0))
        if mins <= 0:
            db.set_setting("discovery_until", None)
            audit(who, "discovery closed", ip)
        else:
            db.set_setting("discovery_until", time.time() + min(mins, 240) * 60)
            audit(who, f"discovery opened for {min(mins, 240)} min", ip)
        return 200, {"ok": True}
    m = db.machine(mac)
    if not m or not can_access(user, m):
        return 404, {"error": "unknown machine"}
    pol = db.policy(m["department_id"])
    if act == "set_profile":
        pid = a.get("profile_id")
        pid = int(pid) if str(pid or "").isdigit() else None
        if pid and not db.q1("SELECT 1 FROM profiles WHERE id=?", pid):
            return 400, {"error": "unknown profile"}
        db.upsert_machine(mac, profile_id=pid)
        audit(who, f"profile of {m['name']} {mac} -> {pid or 'none (install settings)'}", ip)
        return 200, {"ok": True}
    if act == "ep_cmd":
        if not m.get("ep_token"):
            return 400, {"error": "this machine is not managed by KwickBoot (installed without the endpoint agent)"}
        cmd = str(a.get("cmd", ""))
        if cmd in endpoint.ADMIN_COMMANDS and role != "admin":
            return 403, {"error": "managing user accounts needs role admin"}
        try:      # the password is hashed inside make_arg and never logged or stored in plain text
            arg = endpoint.make_arg(cmd, a.get("user"), a.get("password"), bool(a.get("admin")))
            endpoint.queue(mac, cmd, arg, who)
        except ValueError as e:
            return 400, {"error": str(e)}
        except Exception:
            return 500, {"error": "could not prepare the command"}
        u = f" user {arg.split(':')[0]}" if arg else ""
        audit(who, f"endpoint command {cmd}{u}{' (admin)' if cmd == 'adduser' and a.get('admin') else ''} -> {m['name']} {mac}", ip)
        return 200, {"ok": True}
    if act == "assign":
        d = a.get("department")
        d = int(d) if str(d or "").isdigit() else None
        if d is not None and not db.q1("SELECT 1 FROM departments WHERE id=?", d):
            return 400, {"error": "unknown department"}
        if role != "admin" and (d is None or d not in user_depts(user)):
            return 403, {"error": "you can only move machines into your own departments"}
        db.upsert_machine(mac, department_id=d)
        audit(who, f"assign {mac} -> {dept_names().get(d, 'unassigned')}", ip)
        return 200, {"ok": True}
    if act == "approve":
        name = str(a.get("name", "")).lower()
        if not HOST_RE.match(name) or (pol["hostname_prefix"] and not name.startswith(pol["hostname_prefix"])):
            return 400, {"error": "name: lowercase letters, digits, '-' (max 63)"
                          + (f", starting with '{pol['hostname_prefix']}'" if pol["hostname_prefix"] else "")}
        db.upsert_machine(mac, name=name, approved=1)
        audit(who, f"approved {mac} as {name}", ip)
    elif act == "local_boot":    # how the server hands this machine to its installed OS (firmware differences)
        meth = a.get("method")
        if meth not in ("auto", "chainload", "exit"):
            return 400, {"error": "method: auto, chainload or exit"}
        db.upsert_machine(mac, local_boot=None if meth == "auto" else meth)
        audit(who, f"local boot {m['name']} {mac} -> {meth}", ip)
    elif act == "power":         # one set of buttons; the best route this machine offers
        op = a.get("op")
        if op not in ("on", "off", "reset"):
            return 400, {"error": "op: on, off or reset"}
        if op == "off" and RANK[role] < RANK["manager"]:
            return 403, {"error": "power off needs role manager"}
        ram = m.get("agent_state") in ("ready", "busy") and (time.time() - (m.get("agent_seen") or 0)) < 120
        try:
            if (bmc.config(mac) or {}).get("type") in ("redfish", "ipmi"):   # out-of-band: works whatever the OS is doing
                how = "BMC " + str(bmc.power(mac, {"on": "on", "off": "shutdown", "reset": "reset"}[op]))
            elif op == "on":
                S.wake(mac)
                how = "Wake-on-LAN"
            elif ram and m.get("agent_state") == "ready":         # in the RAM OS and idle
                db.add_job(mac, "reboot" if op == "reset" else "poweroff", {}, who)
                how = "RAM OS"
            elif op == "reset" and m.get("ep_token") and (time.time() - (m.get("ep_seen") or 0)) < 120:
                endpoint.queue(mac, "reboot", "", who)
                how = "installed system's agent"
            elif ram:
                return 409, {"error": "the RAM OS is busy with a job - wait for it to finish, or use a BMC/AMT to force it"}
            else:
                return 409, {"error": "no way to reach this machine: it is not in the RAM OS, not managed, and has no BMC/AMT configured"}
        except bmc.BMCError as e:
            return 502, {"error": f"BMC: {e}"}
        audit(who, f"POWER {op} {m['name']} {mac} via {how}", ip)
        return 200, {"ok": True, "how": how}
    elif act == "kb_shell":      # root console on the machine's own screen: whoever sits there can wipe its disks
        on = bool(a.get("on"))
        db.upsert_machine(mac, kb_shell=1 if on else None)
        audit(who, f"emergency console {m['name']} {mac} -> {'on' if on else 'off'}", ip)
    elif act == "unapprove":
        db.upsert_machine(mac, approved=0)
        audit(who, f"un-approved {mac}", ip)
    elif act == "default":
        if a.get("mode") not in ("alpine", "local", "menu", "seat"):
            return 400, {"error": "bad mode"}
        S.set_default(mac, a["mode"])
        audit(who, f"default {mac} -> {a['mode']}", ip)
    elif act == "next":
        nx = str(a.get("next", ""))
        if nx.startswith("iso:"):                 # boot an ISO straight on the hardware (iPXE sanboot): live tools, firmware
            if not perm(user, pol, "manual_install"):   # updaters, installers - a vendor ISO can wipe disks, so it is gated
                return 403, {"error": "booting ISOs is switched off for you in this department (Manual install)"}
            if nx[4:] not in [x["name"] for x in isos_for(m["department_id"], pol, user)]:
                return 400, {"error": "choose an ISO you may use for this machine"}
        elif nx.startswith("tool:"):               # an OS library tool (Rescue, Secure wipe, ...): its own minimum role
            t = S.tool_info(nx[5:])
            if not t:
                return 400, {"error": "no such tool in the OS library"}
            need = t.get("ROLE") if t.get("ROLE") in RANK else "admin"
            if RANK[user["role"]] < RANK[need]:
                return 403, {"error": f"starting {t.get('TITLE', nx[5:])} on a PC needs role {need}"}
            if not m.get("approved"):
                return 400, {"error": "approve the machine first (tools change its disks)"}
        elif nx not in NEXT_ACTIONS:
            return 400, {"error": "bad action"}
        S.set_next(mac, nx)
        audit(who, f"next boot {mac} -> {nx}", ip)
    elif act == "job":
        typ = a.get("type")
        if typ not in ("inventory", "preflight", "reboot", "poweroff", "stress"):
            return 400, {"error": "bad job type"}
        params = {}
        if typ == "stress":                    # CPU + memory under load in the RAM OS (no disk is touched)
            mins = str(a.get("minutes", "10"))
            if not mins.isdigit() or not 1 <= int(mins) <= 120:
                return 400, {"error": "stress test: 1 to 120 minutes"}
            if m.get("agent_state") != "ready":
                return 409, {"error": "machine's RAM OS agent is not ready (boot it into the RAM OS first)"}
            params["MIN"] = mins
        if typ == "preflight" and a.get("disk"):
            disk = str(a["disk"])
            if disk not in [x["dev"] for x in disks_from_inventory(mac)]:
                return 400, {"error": "disk not in inventory"}
            params["DISK"] = disk
        jid = db.add_job(mac, typ, params, who)
        audit(who, f"job {typ} queued for {mac} ({jid})", ip)
    elif act == "install":
        if not m["approved"]:
            return 400, {"error": "approve (and name) the machine first"}
        if RANK[role] < RANK.get(pol["install_min_role"], 2):
            return 403, {"error": f"installs in this department need role {pol['install_min_role']} (you are {role})"}
        os_id = str(a.get("os", ""))
        if os_id not in [r["id"] for r in recipes()]:
            return 400, {"error": "unknown OS recipe"}
        if os_id == "windows":                  # the department's Windows template fills blanks, and may be binding
            a, err = win_template(a, pol, user)
            if err:
                return 403, {"error": err}
        if pol["allowed_os"] and os_id not in pol["allowed_os"]:
            return 403, {"error": f"department policy allows only: {', '.join(pol['allowed_os'])}"}
        extra = {}
        if os_id == "image":                 # user's own disk image: URL or file in http/images + SHA-256
            img, sha = str(a.get("image", "")).strip(), str(a.get("sha256", "")).strip().lower()
            if not re.fullmatch(r"[0-9a-f]{64}", sha):
                return 400, {"error": "SHA-256 must be 64 hex characters"}
            local = os.path.isfile(os.path.join(S.HTTP_ROOT, "images", os.path.basename(img)))
            if not (re.fullmatch(r"https?://[A-Za-z0-9._/:+=-]+", img) or (local and re.fullmatch(r"[A-Za-z0-9._+-]+", img))):
                return 400, {"error": "image: an http(s) URL without ?&% characters, or a file name from the images folder"}
            extra = {"IMAGE": img, "SHA256": sha}
        bundles = next(r["bundles"] for r in recipes() if r["id"] == os_id)
        if bundles:                          # desktop recipes: app bundle chosen at install time
            b = str(a.get("bundle") or bundles[0])
            if b not in bundles:
                return 400, {"error": f"app bundle must be one of: {', '.join(bundles)}"}
            if pol["allowed_bundles"] and b not in pol["allowed_bundles"]:
                return 403, {"error": f"department policy allows bundles: {', '.join(pol['allowed_bundles'])}"}
            extra["BUNDLE"] = b
        rec = next(r for r in recipes() if r["id"] == os_id)
        if rec["variants"] and a.get("variant"):   # image size / edition choice declared by the recipe (e.g. full | lts)
            v = str(a.get("variant"))
            if v not in rec["variants"]:
                return 400, {"error": f"variant must be one of: {', '.join(rec['variants'])}"}
            extra["VARIANT"] = v
        rparams = rec["params"]
        if "KIOSK_URL" in rparams:              # kiosk / signage: the page to show (any URL, base64 so it passes the shell-safe filter)
            url = str(a.get("kiosk_url", "")).strip()
            if not re.fullmatch(r"https?://[^\s'\"<>]{3,500}", url):
                return 400, {"error": "kiosk URL: an http(s) address (max 500 characters)"}
            import base64
            extra["KIOSK_URL_B64"] = base64.b64encode(url.encode()).decode()
            allow = [d.strip().lower() for d in str(a.get("kiosk_allow", "")).replace(" ", ",").split(",") if d.strip()]
            if any(not re.fullmatch(r"(\*\.)?[a-z0-9-]+(\.[a-z0-9-]+)+", d) for d in allow) or len(allow) > 50:
                return 400, {"error": "allowed sites: domain names like library.example.edu or *.example.edu, separated by commas"}
            if allow:
                extra["KIOSK_ALLOW_B64"] = base64.b64encode(",".join(allow).encode()).decode()
            idle = str(a.get("kiosk_idle", "10")).strip() or "10"
            if not idle.isdigit() or int(idle) > 1440:
                return 400, {"error": "idle reset: minutes between 0 (off) and 1440"}
            extra["KIOSK_IDLE"] = str(int(idle))
            extra["KIOSK_RO"] = "0" if str(a.get("kiosk_ro", "1")) in ("0", "false", "False") else "1"
        if "WIN_ISO" in rparams:                # Windows: an ISO this user may use, license acceptance, debloat level
            iso = str(a.get("iso", ""))
            if iso not in [x["name"] for x in isos_for(m["department_id"], pol, user)]:
                return 400, {"error": "choose a Windows ISO you may use for this machine"}
            if a.get("win_eula") not in (True, 1, "1", "true", "yes"):
                return 400, {"error": "accept the Microsoft license terms for your organisation to install Windows unattended"}
            deb = str(a.get("win_debloat") or "standard")
            if deb not in ("off", "standard", "full"):
                return 400, {"error": "debloat: off, standard or full"}
            apps = "bundle" if a.get("win_apps") in ("bundle", True, "1") else "none"
            extra.update(ISO=iso, WIN_EULA="1", WIN_DEBLOAT=deb, WIN_APPS=apps, WIN_FB=secrets.token_hex(16))
            if pol.get("win_oem"):             # support information: free text -> base64 JSON (job values must be shell-safe)
                import base64
                extra["WIN_OEM_B64"] = base64.b64encode(json.dumps(pol["win_oem"]).encode()).decode()
            import drivers
            dm = drivers.match(db.latest_report(mac, "inventory")[0] or "")
            if dm and a.get("win_drivers", True) not in (False, "0", "false", "no"):
                extra["WIN_DRIVERS"] = "1"             # matched from the inventory; the folders are fetched by the install
                extra["WIN_DRIVER_N"] = str(len(dm))
            if a.get("win_join", True) not in (False, "0", "false", "no"):
                if S.djoin_info(mac)["present"]:      # Active Directory (this machine's offline join file) wins
                    extra["WIN_JOIN"] = "djoin"      # the file itself stays on the server; only the flag goes into the job
                elif S.ppkg_info(m["department_id"])["present"]:
                    extra["WIN_JOIN"] = "entra"      # the department's Entra package, handed to the running install only
        disk = next((x for x in disks_from_inventory(mac) if x["dev"] == a.get("disk")), None)
        if not disk:
            return 400, {"error": "disk not in this machine's inventory"}
        if "WIN_ISO" in rparams:
            err = win_layout(a, disk, disks_from_inventory(mac), extra)
            if err:
                return 400, {"error": err}
        host = str(a.get("hostname", "")).lower()
        if not HOST_RE.match(host):
            return 400, {"error": "hostname: lowercase letters, digits, '-'"}
        if pol["hostname_prefix"] and not host.startswith(pol["hostname_prefix"]):
            return 403, {"error": f"department policy: hostname must start with '{pol['hostname_prefix']}'"}
        if str(a.get("confirm", "")).lower() != host:
            return 400, {"error": "type the hostname exactly to confirm the wipe"}
        if m.get("agent_state") != "ready":
            return 409, {"error": "machine's RAM OS agent is not ready (boot it into the RAM OS first)"}
        if db.q1("SELECT 1 FROM jobs WHERE mac=? AND state IN ('queued','running')", mac):
            return 409, {"error": "machine already has a queued/running job"}
        fin = "reboot" if a.get("finish", "reboot") == "reboot" else "none"
        # managed from the web console (users, passwords, updates; kiosks also profiles): per-machine token, after all checks.
        # On by default; "managed": false installs a machine KwickBoot never talks to again.
        # (disk-image appliances - OPNsense, Home Assistant OS, ... - run their own OS: no agent possible)
        if rec["method"] != "image" and ("endpoint" in rparams or a.get("managed", True) not in (False, "0", "false", "no")):
            extra["EP_TOKEN"] = endpoint.issue_token(mac)
        else:
            db.upsert_machine(mac, ep_token=None)
        # "install anyway": judgment-call checks (low RAM, failing SMART, Secure Boot on...) become logged warnings.
        # Physical impossibilities (no such disk, mounted, image does not fit) still stop. We provide the tool, the user decides.
        if a.get("force") in (True, 1, "1", "true", "yes"):
            if not perm(user, pol, "allow_override"):
                return 403, {"error": "'install anyway' is switched off for you in this department - fix the warnings, or ask an admin"}
            extra["FORCE"] = "1"
        pb = a.get("portbase")
        if "WIN_ISO" in rparams and a.get("mode") == "multicast" and isinstance(pb, int) and 9000 <= pb < 9200:
            extra.update(MODE="multicast", PORTBASE=str(pb))       # set by the bulk Windows install only
        if pol.get("boot_order") == "os-first":
            extra["BOOT_ORDER"] = "os-first"   # the department keeps PCs disk-first; network boot only when IT asks
        jid = db.add_job(mac, "install", {"OS": os_id, "DISK": disk["dev"], "CONFIRM": disk["serial"],
                                          "HOSTNAME": host, "FIN": fin, **extra}, who)
        audit(who, f"INSTALL {os_id}{' ' + extra['IMAGE'] if 'IMAGE' in extra else ''}{' bundle ' + extra['BUNDLE'] if 'BUNDLE' in extra else ''} queued for {mac} on {disk['dev']} "
                   f"({disk['size']} serial {disk['serial']}) as {host}{' - INSTALL ANYWAY (warnings accepted)' if extra.get('FORCE') else ''}{' - multicast' if extra.get('MODE') == 'multicast' else ''}"
                   f"{(' - Windows from ' + extra['ISO'] + ', edition ' + (extra.get('VARIANT') or 'pro') + ', debloat ' + extra['WIN_DEBLOAT'] + ', Microsoft license terms accepted by ' + who + win_layout_text(extra) + {'djoin': ', joins the domain (offline join file)', 'entra': ', joins Entra ID (department package)'}.get(extra.get('WIN_JOIN'), '') + (f", {extra['WIN_DRIVER_N']} driver folders from the library" if extra.get('WIN_DRIVERS') else '')) if 'WIN_EULA' in extra else ''} [{jid}]", ip)
    elif act == "wipe":          # NIST 800-88 secure wipe of chosen disks, verified, one signed certificate per disk
        devs = [str(x) for x in (a.get("disks") or [])][:16]
        inv = {x["dev"]: x for x in disks_from_inventory(mac)}
        if not devs or any(d not in inv for d in devs):
            return 400, {"error": "choose disks from this machine's inventory"}
        if str(a.get("confirm", "")).strip().lower() != (m["name"] or "").lower():
            return 400, {"error": "type the machine's name exactly to confirm erasing its disks for good"}
        if m.get("agent_state") != "ready":
            return 409, {"error": "machine's RAM OS agent is not ready (boot it into the RAM OS first)"}
        if db.q1("SELECT 1 FROM jobs WHERE mac=? AND state IN ('queued','running')", mac):
            return 409, {"error": "machine already has a queued/running job"}
        if any("+" in inv[d]["serial"] for d in devs):
            return 400, {"error": "a disk serial contains '+' - wipe that disk on its own"}
        fin = "poweroff" if a.get("finish") == "poweroff" else "none"
        jid = db.add_job(mac, "wipe", {"DISKS": "+".join(devs), "CONFIRMS": "+".join(inv[d]["serial"] for d in devs), "FIN": fin}, who)
        audit(who, f"SECURE WIPE queued for {m['name']} {mac}: " + ", ".join(f"{d} ({inv[d]['size']} serial {inv[d]['serial']})" for d in devs) + f" [{jid}]", ip)
    elif act == "netboot_once":  # a disk-first PC: boot the network ONCE (reinstall, rescue) - management agent or BMC
        if RANK[role] < RANK["operator"]:
            return 403, {"error": "needs role operator"}
        how = None
        if (bmc.config(mac) or {}).get("type") in ("redfish", "ipmi"):
            try:
                bmc.pxe_once(mac)
                how = "BMC one-time network boot set"
            except Exception as e:     # noqa: BLE001
                return 502, {"error": f"BMC: {e}"}
        elif m.get("ep_token") and (endpoint.status(mac).get("seen_age") or 10 ** 9) < 300:   # agent checked in recently
            endpoint.queue(mac, "netboot", "", who)
            how = "the management agent sets a one-time network boot and restarts the PC"
        if not how:
            return 409, {"error": "no remote way to start a network boot on this PC: press the boot-menu key (often F12) "
                                  "at power-on and pick the network entry, or set up its BMC / Intel AMT"}
        audit(who, f"one-time network boot for {m['name']} {mac}: {how}", ip)
        return 200, {"ok": True, "how": how}
    elif act == "driver_fetch":   # download the PC maker's official driver pack for this model into the library (background)
        import drivers
        import threading
        inv = db.latest_report(mac, "inventory")[0] or ""
        h = drivers.lookup(inv)
        if not h:
            return 404, {"error": "no official driver pack known for this model"}
        if h["pack"] in DRV_FETCH and DRV_FETCH[h["pack"]]["state"] == "downloading":
            return 409, {"error": "already downloading"}
        DRV_FETCH[h["pack"]] = {"state": "downloading", "by": who, "at": time.time()}

        def run():
            try:
                m = drivers.auto_add(inv)
                DRV_FETCH[h["pack"]] = {"state": "done", "by": who, "at": time.time(), "infs": m.get("infs", 0)}
                audit(who, f"driver pack {h['pack']} ({h['vendor']} {h['name']}, {h['size'] // 1000000} MB) added to the library: "
                           f"{m.get('infs', 0)} drivers", ip)
            except Exception as e:     # noqa: BLE001 - reported in the console
                DRV_FETCH[h["pack"]] = {"state": "failed", "by": who, "at": time.time(), "error": str(e)[:300]}
                S.log(f"[drivers] {h['pack']}: download failed: {e}")
        threading.Thread(target=run, daemon=True).start()
        audit(who, f"driver pack {h['pack']} download started ({h['vendor']} {h['name']}, official URL, SHA-256 from the catalog)", ip)
        return 200, {"ok": True}
    elif act in ("djoin_set", "djoin_clear"):   # offline domain join file (djoin /provision) for this machine's next Windows install
        if act == "djoin_clear":
            S.djoin_drop(mac, f"removed by {who}")
            audit(who, f"domain join file removed for {m['name']} {mac}", ip)
            return 200, {"ok": True, "djoin": S.djoin_info(mac)}
        import base64
        try:
            raw = base64.b64decode(str(a.get("file_b64", "")), validate=True)
        except (ValueError, TypeError):
            return 400, {"error": "could not read the file"}
        # djoin /savefile writes UTF-16 (with a BOM); accept UTF-8 too. The content is one base64 blob.
        txt = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8", "replace")
        blob = re.sub(r"\s+", "", txt.replace("\x00", ""))
        if not re.fullmatch(r"[A-Za-z0-9+/=]{100,65536}", blob):
            return 400, {"error": "this is not a djoin /provision file (expected one base64 blob)"}
        dom = str(a.get("domain", "")).strip().lower()
        if dom and not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", dom):
            return 400, {"error": "domain: a DNS name like corp.example.com"}
        d = os.path.dirname(S.djoin_path(mac))
        os.makedirs(d, mode=0o700, exist_ok=True)
        f = S.djoin_path(mac)
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as o:
            o.write(blob)
        meta = {"domain": dom, "by": who, "at": time.time(), "for": (m["name"] or "").upper()[:15]}
        json.dump(meta, open(f + ".json", "w", encoding="utf-8"))
        audit(who, f"domain join file uploaded for {m['name']} {mac}{' (' + dom + ')' if dom else ''} - used by its next Windows install, deleted once Windows is set up", ip)
        return 200, {"ok": True, "djoin": S.djoin_info(mac)}
    elif act == "winprep":
        # one-time per ISO + edition, on THIS machine's CPU (the boot server may be a small box): streamable image of the
        # edition -> server; later installs of it stream straight onto the disk. The chosen scratch disk is ERASED.
        iso = str(a.get("iso", ""))
        if iso not in [x["name"] for x in isos_for(m["department_id"], pol, user)]:
            return 400, {"error": "choose a Windows ISO you may use"}
        ed = WIN_EDITION.get(str(a.get("edition") or "pro"))
        if not ed:
            return 400, {"error": "edition: pro, education, enterprise or home"}
        if os.path.isfile(os.path.join(S.HTTP_ROOT, "iso", "winimg", iso, ed + ".wim")):
            return 409, {"error": f"{iso} {ed} is already prepared"}
        disk = next((x for x in disks_from_inventory(mac) if x["dev"] == a.get("disk")), None)
        if not disk:
            return 400, {"error": "scratch disk not in this machine's inventory"}
        if size_gb(disk["size"]) < 20:
            return 400, {"error": "the scratch disk needs 20 GB or more (it holds the source and the result)"}
        if str(a.get("confirm", "")).strip().lower() != (m["name"] or "").lower():
            return 400, {"error": "type the machine's name exactly to confirm erasing the scratch disk"}
        if m.get("agent_state") != "ready":
            return 409, {"error": "machine's RAM OS agent is not ready (boot it into the RAM OS first)"}
        if db.q1("SELECT 1 FROM jobs WHERE mac=? AND state IN ('queued','running')", mac):
            return 409, {"error": "machine already has a queued/running job"}
        jid = db.add_job(mac, "winprep", {"ISO": iso, "EDITION": ed, "DISK": disk["dev"], "CONFIRM": disk["serial"], "FIN": "none"}, who)
        audit(who, f"PREPARE {iso} {ed} for streaming on {m['name']} {mac}, scratch {disk['dev']} ({disk['size']} serial {disk['serial']}) ERASED [{jid}]", ip)
        return 200, {"ok": True, "job": jid}
    elif act in ("capture", "restore"):
        disk = next((x for x in disks_from_inventory(mac) if x["dev"] == a.get("disk")), None)
        if not disk:
            return 400, {"error": "disk not in this machine's inventory"}
        if m.get("agent_state") != "ready":
            return 409, {"error": "machine's RAM OS agent is not ready (boot it into the RAM OS first)"}
        if db.q1("SELECT 1 FROM jobs WHERE mac=? AND state IN ('queued','running')", mac):
            return 409, {"error": "machine already has a queued/running job"}
        names = [x["name"] for x in captures()]
        if act == "capture":     # golden image of this machine's disk -> the server; read-only on the machine
            name = str(a.get("name", "")).strip().lower()
            if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,60}", name):
                return 400, {"error": "image name: lowercase letters, digits, . _ - (e.g. lab204-2026-11)"}
            if name in names or os.path.isdir(os.path.join(S.HTTP_ROOT, "images", "captures", name)):
                return 409, {"error": f"an image called {name} already exists"}
            jid = db.add_job(mac, "capture", {"NAME": name, "DISK": disk["dev"], "CONFIRM": disk["serial"], "FIN": "none"}, who)
            audit(who, f"CAPTURE of {m['name']} {mac} {disk['dev']} ({disk['size']} serial {disk['serial']}) as image {name} [{jid}]", ip)
        else:                    # a golden image -> this machine's disk (wipes it)
            if not m["approved"]:
                return 400, {"error": "approve (and name) the machine first"}
            if RANK[role] < RANK.get(pol["install_min_role"], 2):
                return 403, {"error": f"installs in this department need role {pol['install_min_role']} (you are {role})"}
            name = str(a.get("image", ""))
            if name not in names:
                return 400, {"error": "choose a captured image"}
            if str(a.get("confirm", "")).strip().lower() != (m["name"] or "").lower():
                return 400, {"error": "type the machine's name exactly to confirm the wipe"}
            db.upsert_machine(mac, ep_token=None)
            params = {"NAME": name, "DISK": disk["dev"], "CONFIRM": disk["serial"], "FIN": "reboot", "MODE": "unicast"}
            # each restored copy gets its own name (default: the machine's approved name; bulk: the wizard's pattern)
            hn = str(a.get("hostname") or m["name"] or "").lower()
            if hn and not HOST_RE.match(hn):
                return 400, {"error": "hostname: lowercase letters, digits, '-'"}
            if hn:
                params["HOSTNAME"] = hn
            if pol.get("boot_order") == "os-first":
                params["BOOT_ORDER"] = "os-first"
            pb = a.get("portbase")
            if a.get("mode") == "multicast" and isinstance(pb, int) and 9000 <= pb < 9200:   # set by the bulk restore only
                params.update(MODE="multicast", PORTBASE=str(pb))
            jid = db.add_job(mac, "restore", params, who)
            audit(who, f"RESTORE image {name} queued for {m['name']} {mac} on {disk['dev']} ({disk['size']} serial {disk['serial']}){' as ' + params['HOSTNAME'] if params.get('HOSTNAME') else ''} [{jid}]", ip)
    elif act == "manual":        # the vendor's own installer from the ISO library, in a VM on the machine (screen via noVNC)
        if not m["approved"]:
            return 400, {"error": "approve (and name) the machine first"}
        if RANK[role] < RANK.get(pol["install_min_role"], 2):
            return 403, {"error": f"installs in this department need role {pol['install_min_role']} (you are {role})"}
        if not perm(user, pol, "manual_install"):
            return 403, {"error": "Manual install (ISO) is switched off for you in this department - ask an admin"}
        iso = str(a.get("iso", ""))
        if iso not in [x["name"] for x in isos_for(m["department_id"], pol, user)]:
            return 400, {"error": "choose an ISO you may use for this machine"}
        fw, bus = str(a.get("fw") or "auto"), str(a.get("bus") or "auto")
        if fw not in ("auto", "bios", "uefi") or bus not in ("auto", "ide", "sata", "nvme", "none"):
            return 400, {"error": "firmware: auto, bios or uefi; disk controller: auto, ide, sata, nvme or none"}
        if bus == "none":            # run the ISO only (live tools, firmware updates): no disk, nothing written
            disk = {"dev": "", "serial": "", "size": ""}
        else:
            disk = next((x for x in disks_from_inventory(mac) if x["dev"] == a.get("disk")), None)
            if not disk:
                return 400, {"error": "disk not in this machine's inventory"}
            if str(a.get("confirm", "")).strip().lower() != (m["name"] or "").lower():
                return 400, {"error": "type the machine's name exactly to confirm (its disk is handed to the installer)"}
        if m.get("agent_state") != "ready":
            return 409, {"error": "machine's RAM OS agent is not ready (boot it into the RAM OS first)"}
        if db.q1("SELECT 1 FROM jobs WHERE mac=? AND state IN ('queued','running')", mac):
            return 409, {"error": "machine already has a queued/running job"}
        params = {"ISO": iso, "FW": fw, "DISKBUS": bus, "FIN": "none" if bus == "none" else "reboot"}
        if bus != "none":
            db.upsert_machine(mac, ep_token=None)    # a vendor's installer: KwickBoot cannot manage what it installs
            params.update(DISK=disk["dev"], CONFIRM=disk["serial"])
        jid = db.add_job(mac, "manual", params, who)
        where = "" if bus == "none" else f" on {disk['dev']} ({disk['size']} serial {disk['serial']})"
        kind = "RUN" if bus == "none" else "INSTALL"
        audit(who, f"MANUAL {kind} {iso} (firmware {fw}, disk {bus}) queued for {m['name']} {mac}{where} [{jid}]", ip)
    return 200, {"ok": True, "job": jid}


ISO_MAX = 16 << 30                 # a Windows ISO is ~6-7 GB; nothing bootable is near this
ISO_CHUNK_MAX = 64 << 20
UPLOADS = {}                       # name -> {"sha": running sha256, "size": bytes so far, "who": user}


def iso_upload(h, user, q, ip):
    """POST /api/isos/upload?name=x.iso&offset=N[&total=T][&done=1]  body = the next chunk (raw bytes).
    Chunks must arrive in order (offset = bytes already received), so an interrupted upload can continue where it
    stopped. The SHA-256 is computed while the bytes arrive and shown at the end to compare with the vendor's."""
    if not same_origin(h):
        return 403, {"error": "bad origin"}
    # to=shared (admins: the library every department can be given) | dept:<id> (managers: their department's own)
    # | me (personal: only the uploader and admins see it). Department policy + the user's own setting decide.
    to = q.get("to", ["me"])[0]
    dept, owner = None, None
    if to == "shared":
        if user["role"] != "admin":
            return 403, {"error": "the shared library is for admins - upload to your department or as personal"}
    elif to.startswith("dept:") and to[5:].isdigit():
        dept = int(to[5:])
        if user["role"] != "admin" and (RANK[user["role"]] < RANK["manager"] or dept not in user_depts(user)):
            return 403, {"error": "department uploads need role manager in that department"}
    elif to == "me":
        owner = user["username"]
    else:
        return 400, {"error": "to: shared, dept:<id> or me"}
    if user["role"] != "admin":
        deps = [dept] if dept is not None else user_depts(user)
        if RANK[user["role"]] < RANK["operator"] or not any(perm(user, db.policy(d), "iso_upload") for d in deps or [None]):
            return 403, {"error": "uploading ISOs is switched off for you - ask an admin"}
    name = q.get("name", [""])[0]
    if not re.fullmatch(r"[A-Za-z0-9._+-]{1,120}\.iso", name):
        return 400, {"error": "file name: letters, digits, . _ + - and ending in .iso"}
    d = os.path.join(S.HTTP_ROOT, "iso")
    os.makedirs(d, exist_ok=True)
    final, part = os.path.join(d, name), os.path.join(d, "." + name + ".part")   # hidden until complete
    if os.path.exists(final):
        return 409, {"error": f"{name} is already in the library"}
    try:
        off, n = int(q.get("offset", ["0"])[0]), int(h.headers.get("Content-Length", "0") or 0)
    except ValueError:
        return 400, {"error": "bad offset"}
    have = os.path.getsize(part) if os.path.exists(part) else 0
    if off == 0 and "total" in q:                      # a new upload: room for it + 2 GB reserve?
        total = int(q["total"][0]) if q["total"][0].isdigit() else 0
        st = os.statvfs(d)
        if total <= 0 or total > ISO_MAX:
            return 400, {"error": "ISO size must be between 1 byte and 16 GB"}
        if total + (2 << 30) > st.f_bavail * st.f_frsize - (0 if not have else have):
            return 507, {"error": f"not enough space on the server: {total // 10**9} GB + 2 GB reserve needed, "
                                 f"{st.f_bavail * st.f_frsize // 10**9} GB free"}
        if have:
            os.remove(part)
            have = 0
        UPLOADS[name] = {"sha": hashlib.sha256(), "size": 0, "who": user["username"]}
        audit(user["username"], f"ISO upload started: {name} ({total // 10**6} MB)", ip)
    if off != have:
        return 409, {"error": "out of order", "offset": have}           # the page continues from here
    if n > ISO_CHUNK_MAX or have + n > ISO_MAX:
        return 413, {"error": "chunk too large"}
    up = UPLOADS.get(name)
    if up and up["size"] != have:                      # server restarted mid-upload: hash again at the end
        up = UPLOADS[name] = None
    with open(part, "ab") as f:
        left = n
        while left:
            b = h.rfile.read(min(left, 1 << 20))
            if not b:
                return 400, {"error": "upload interrupted", "offset": f.tell()}
            f.write(b)
            if up:
                up["sha"].update(b)
                up["size"] += len(b)
            left -= len(b)
    if q.get("done", [""])[0] != "1":
        return 200, {"ok": True, "offset": have + n}
    if up:
        sha = up["sha"].hexdigest()
    else:
        s2 = hashlib.sha256()
        with open(part, "rb") as f:
            for b in iter(lambda: f.read(1 << 20), b""):
                s2.update(b)
        sha = s2.hexdigest()
    UPLOADS.pop(name, None)
    try:
        os.chmod(part, 0o644)
    except OSError:                    # exFAT library disk: permissions come from the mount
        pass
    os.replace(part, final)
    db.set_iso_meta(name, dept, user["username"], sha, owner)
    audit(user["username"], f"ISO uploaded ({'personal' if owner else dept_names().get(dept, dept) if dept else 'shared library'}): {name} ({os.path.getsize(final) // 10**6} MB) sha256 {sha}", ip)
    return 200, {"ok": True, "name": name, "size": os.path.getsize(final), "sha256": sha}


def captures():
    """Golden images captured from machines (http/images/captures/<name>/manifest.json, written last by capture.sh)."""
    d = os.path.join(S.HTTP_ROOT, "images", "captures")
    out = []
    for n in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        mf = os.path.join(d, n, "manifest.json")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,60}", n) or not os.path.isfile(mf):
            continue
        try:
            j = json.load(open(mf, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({"name": n, "created": j.get("created", ""), "bytes": sum(p.get("bytes", 0) for p in j.get("parts", [])),
                    "disk_bytes": j.get("disk_bytes", 0), "model": (j.get("source") or {}).get("model", ""),
                    "boot": (j.get("source") or {}).get("boot", ""), "parts": len(j.get("parts", []))})
    return out


def iso_library():
    """Installer ISOs on this server (http/iso), offered for manual installs. Each is shared (admins' library, given to
    departments by policy), one department's own upload, or one user's personal ISO."""
    d = os.path.join(S.HTTP_ROOT, "iso")
    meta, names = db.iso_meta(), dept_names()
    out = []
    for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        p = os.path.join(d, f)
        if f.lower().endswith(".iso") and re.fullmatch(r"[A-Za-z0-9._+-]+", f) and os.path.isfile(p):
            mt = meta.get(f) or {}
            dep, own = mt.get("department_id"), mt.get("owner_user") or ""
            out.append({"name": f, "size": os.path.getsize(p), "department_id": dep, "owner_user": own,
                        "owner": ("personal: " + own) if own else (names.get(dep, "?") if dep is not None else "shared"),
                        "sha256": mt.get("sha256") or "", "uploaded_by": mt.get("uploaded_by") or "",
                        # Windows editions already prepared for streaming (iso/winimg/<iso>/<Edition>.wim)
                        "prepared": sorted(k for k, e in WIN_EDITION.items()
                                           if os.path.isfile(os.path.join(d, "winimg", f, e + ".wim")))})
    return out


def isos_for(department_id, pol, user):
    """ISOs this user may use on a machine of this department: the shared ones the policy allows ([] = all),
    the department's own, and the user's personal ones."""
    allow = pol.get("allowed_isos") or []
    return [x for x in iso_library()
            if (x["department_id"] is None and not x["owner_user"] and (not allow or x["name"] in allow))
            or (department_id is not None and x["department_id"] == department_id)
            or (x["owner_user"] and x["owner_user"] == user["username"])]



# ---------------------------------------------------------------- bulk actions (fleet board, deployment wizard)
BULK_SIMPLE = ("job", "default", "next", "assign", "approve")


def auto_disk(mac):
    """The one internal (non-USB) disk of a machine, else None (several internal disks -> choose per machine)."""
    internal = [d for d in disks_from_inventory(mac) if d["tran"] != "usb"]
    return internal[0] if len(internal) == 1 else None


def do_bulk(user, a, ip):
    macs = [S.norm_mac(x) for x in (a.get("macs") or [])][:500]
    if not macs:
        return 400, {"error": "no machines selected"}
    act = a.get("action")
    results = []
    if act in BULK_SIMPLE:
        for mac in macs:
            one = dict(a, mac=mac)
            if act == "approve":            # approve keeps each machine's current name
                m = db.machine(mac)
                one["name"] = (m or {}).get("name", "")
            code, obj = do_action(user, one, ip)
            results.append({"mac": mac, "ok": code == 200, "error": obj.get("error")})
    elif act == "install":
        if str(a.get("confirm", "")).strip().upper() != f"WIPE {len(macs)}":
            return 400, {"error": f"type WIPE {len(macs)} to confirm wiping {len(macs)} machine{'s' if len(macs) > 1 else ''}"}
        pat = str(a.get("hostname_pattern", "")).lower()
        if "{n}" not in pat and "{nn}" not in pat and "{nnn}" not in pat and len(macs) > 1:
            return 400, {"error": "hostname pattern needs {n}, {nn} or {nnn}, e.g. lab204-{nn}"}
        n0 = int(a.get("start", 1) or 1)
        plan = []
        for i, mac in enumerate(macs):
            n = n0 + i
            host = pat.replace("{nnn}", f"{n:03}").replace("{nn}", f"{n:02}").replace("{n}", str(n))
            disk = auto_disk(mac)
            if not disk:
                results.append({"mac": mac, "ok": False, "error": "no single internal disk - install this one individually"})
                continue
            m = db.machine(mac)
            if a.get("multicast") and (not m or m.get("agent_state") != "ready"
                                       or db.q1("SELECT 1 FROM jobs WHERE mac=? AND state IN ('queued','running')", mac)):
                results.append({"mac": mac, "ok": False, "error": "not ready in the RAM OS (or has a job running)"})
                continue
            plan.append((mac, host, disk))
        mode, port = "unicast", None
        win = a.get("os") == "windows"
        if win and a.get("win_eula") not in (True, 1, "1", "true", "yes"):
            return 400, {"error": "accept the Microsoft license terms for your organisation to install Windows unattended"}
        if win and a.get("multicast") and len(plan) > 1:
            # one stream of the prepared edition for the whole room (machines that miss it fetch it directly). Blank
            # choices come from each machine's department template: multicast only when they all resolve the same
            picks = set()
            for mac, _h, _d in plan:
                r, _e = win_template(a, db.policy((db.machine(mac) or {}).get("department_id")), user)
                picks.add((str(r.get("iso") or ""), str(r.get("variant") or "pro")))
            iso, var = next(iter(picks)) if len(picks) == 1 else ("", "")
            ed = WIN_EDITION.get(var, "")
            iso = os.path.basename(iso)
            wim = os.path.join(S.HTTP_ROOT, "iso", "winimg", iso, ed + ".wim")
            import multicast
            try:
                if len(picks) > 1:
                    raise ValueError("the machines' department templates pick different ISOs or editions")
                if not (ed and iso and os.path.isfile(wim)):
                    raise ValueError("this edition is not prepared for streaming")
                _sid, port = multicast.start_files(f"{iso} {ed}", [wim], [p[0] for p in plan], S.IFACE, S.log, live_publish)
                mode = "multicast"
            except (OSError, ValueError, RuntimeError) as e:
                S.log(f"[multicast] {iso} {ed}: not started ({e}) - unicast instead")
        for mac, host, disk in plan:
            one = {"action": "install", "mac": mac, "os": a.get("os"), "bundle": a.get("bundle"),
                   "variant": a.get("variant"), "disk": disk["dev"], "hostname": host, "confirm": host,
                   "finish": a.get("finish", "reboot")}
            if win:
                one.update({k: a.get(k) for k in ("iso", "win_eula", "win_debloat", "win_apps")}, mode=mode, portbase=port)
            code, obj = do_action(user, one, ip)
            results.append({"mac": mac, "ok": code == 200, "error": obj.get("error"), "hostname": host,
                            "disk": f"{disk['dev']} {disk['size']} {disk['serial']}"})
        audit(user["username"], f"BULK INSTALL {a.get('os')}{' (' + mode + ')' if win else ''} on {len(macs)} machines: "
                                f"{sum(r['ok'] for r in results)} queued", ip)
    elif act == "restore":       # one golden image onto many machines; multicast = one stream for the whole room
        if str(a.get("confirm", "")).strip().upper() != f"WIPE {len(macs)}":
            return 400, {"error": f"type WIPE {len(macs)} to confirm wiping {len(macs)} machine{'s' if len(macs) > 1 else ''}"}
        name = str(a.get("image", ""))
        if name not in [x["name"] for x in captures()]:
            return 400, {"error": "choose a captured image"}
        plan = []
        for mac in macs:                        # who can take part: approved, in the RAM OS, idle, one internal disk
            m, disk = db.machine(mac), auto_disk(mac)
            why = ("unknown machine" if not m else "not approved" if not m["approved"] else
                   "not ready in the RAM OS" if m.get("agent_state") != "ready" else
                   "has a job running" if db.q1("SELECT 1 FROM jobs WHERE mac=? AND state IN ('queued','running')", mac) else
                   "no single internal disk - restore this one individually" if not disk else "")
            if why:
                results.append({"mac": mac, "ok": False, "error": why})
            else:
                plan.append((mac, m, disk))
        mode, port = "unicast", None
        if a.get("multicast") and len(plan) > 1:
            import multicast
            try:
                _sid, port = multicast.start(name, [p[0] for p in plan], S.IFACE, S.HTTP_ROOT, S.log, live_publish)
                mode = "multicast"
            except (OSError, ValueError, RuntimeError) as e:
                S.log(f"[multicast] {name}: not started ({e}) - unicast instead")
        pat = str(a.get("hostname_pattern", "")).lower()
        n0 = int(a.get("start", 1) or 1)
        for i, (mac, m, disk) in enumerate(plan):
            hn = ""
            if any(t in pat for t in ("{n}", "{nn}", "{nnn}")):
                n = n0 + i
                hn = pat.replace("{nnn}", f"{n:03}").replace("{nn}", f"{n:02}").replace("{n}", str(n))
            code, obj = do_action(user, {"action": "restore", "mac": mac, "image": name, "disk": disk["dev"],
                                         "confirm": m["name"] or "", "mode": mode, "portbase": port, "hostname": hn}, ip)
            results.append({"mac": mac, "ok": code == 200, "error": obj.get("error"), "disk": f"{disk['dev']} {disk['size']} {disk['serial']}"})
        audit(user["username"], f"BULK RESTORE image {name} ({mode}) on {len(macs)} machines: {sum(r['ok'] for r in results)} queued", ip)
    else:
        return 400, {"error": "bulk action: job, default, next, assign, approve, install or restore"}
    return 200, {"results": results, "ok": sum(r["ok"] for r in results), "failed": sum(not r["ok"] for r in results)}


def live_publish(ev):
    import live
    live.publish(ev)


# ---------------------------------------------------------------- passwords + admin API
def set_password(user_id, pw):
    if len(pw) < 10:
        return "password must be at least 10 characters"
    salt, it = secrets.token_bytes(16), 300000
    db.x("UPDATE users SET pw_hash=?, pw_salt=?, pw_iter=? WHERE id=?",
         hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, it).hex(), salt.hex(), it, user_id)
    return None


DEPT_RE = re.compile(r"[\w .&()/-]{2,60}")


def clean_policy(p):
    ids = {r["id"]: r for r in recipes()}
    out = dict(db.POLICY_DEFAULT)
    out["allowed_os"] = [o for o in (p.get("allowed_os") or []) if o in ids]
    allb = {b for r in ids.values() for b in r["bundles"]}
    out["allowed_bundles"] = [b for b in (p.get("allowed_bundles") or []) if b in allb]
    pre = str(p.get("hostname_prefix") or "").lower()
    if pre and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,30}", pre):
        raise ValueError("hostname prefix: lowercase letters, digits, '-'")
    out["hostname_prefix"] = pre
    r = p.get("install_min_role") or "manager"
    if r not in ("operator", "manager", "admin"):
        raise ValueError("install_min_role: operator, manager or admin")
    out["install_min_role"] = r
    # "install anyway": strict organizations switch it off (soft checks then block); open ones leave it on (the default)
    off = (False, 0, "0", "false", "no", "off")
    for k in ("allow_override", "manual_install", "iso_upload", "schedule", "ai_advice", "ai_cloud", "ai_byok", "win_custom"):
        out[k] = p.get(k, True) not in off
    out["mfa_required"] = bool(p.get("mfa_required", False)) and p.get("mfa_required") not in off   # two-step sign-in: off unless switched on
    # Windows template (department defaults; "" = none)
    wi = str(p.get("win_iso") or "")
    if wi and wi not in {x["name"] for x in iso_library()}:
        raise ValueError("Windows template: choose an ISO from the library")
    we, wd, wa = str(p.get("win_edition") or ""), str(p.get("win_debloat") or ""), str(p.get("win_apps") or "")
    if we and we not in WIN_EDITION:
        raise ValueError("Windows template: edition " + ", ".join(WIN_EDITION))
    if wd and wd not in ("off", "standard", "full"):
        raise ValueError("Windows template: debloat off, standard or full")
    if wa and wa not in ("bundle", "none"):
        raise ValueError("Windows template: apps bundle or none")
    out.update(win_iso=wi, win_edition=we, win_debloat=wd, win_apps=wa)
    # Windows support information: shown in Settings > System > About ("Support") on every PC of the department
    oem = p.get("win_oem") or {}
    if not isinstance(oem, dict):
        raise ValueError("Windows support information: a set of fields")
    clean = {}
    for k in WIN_OEM_KEYS:
        v = str(oem.get(k) or "").strip()
        if len(v) > (255 if k == "url" else 64) or any(ord(c) < 32 for c in v):
            raise ValueError(f"Windows support information: {k} is too long (or has control characters)")
        if v:
            clean[k] = v
    if clean.get("url") and not re.fullmatch(r"https?://[^\s<>\"']+", clean["url"]):
        raise ValueError("Windows support information: the website must start with https:// or http://")
    out["win_oem"] = clean
    bo = str(p.get("boot_order") or "network-first")
    if bo not in ("network-first", "os-first"):
        raise ValueError("boot order: network-first or os-first")
    out["boot_order"] = bo
    shared = {x["name"] for x in iso_library() if x["department_id"] is None and not x["owner_user"]}
    out["allowed_isos"] = [i for i in (p.get("allowed_isos") or []) if i in shared]
    return out


PPKG_MAX = 20 * 1024 * 1024


def ppkg_upload(h, q, who, ip):
    """POST /api/admin/ppkg?dept=<id>[&op=delete][&tenant=label]  body = the .ppkg file (Windows Configuration Designer,
    "Provision desktop devices" with an Entra bulk token). Stored 0600 under data/ppkg; every Windows install in the
    department then joins Entra ID right after setup; the PC deletes its copy once applied."""
    try:
        did = int(q.get("dept", ["0"])[0])
    except ValueError:
        did = 0
    d = db.q1("SELECT * FROM departments WHERE id=?", did)
    if not d:
        return 400, {"error": "unknown department"}
    f = S.ppkg_path(did)
    if q.get("op", [""])[0] == "delete":
        for x in (f, f + ".json"):
            try:
                os.remove(x)
            except OSError:
                pass
        audit(who, f"Entra provisioning package removed from department {d['name']}", ip)
        return 200, {"ok": True, "ppkg": S.ppkg_info(did)}
    n = int(h.headers.get("Content-Length", "0") or 0)
    if not 1024 <= n <= PPKG_MAX:
        return 400, {"error": "a provisioning package is 1 KB to 20 MB"}
    body = h.rfile.read(n)
    tenant = re.sub(r"[^A-Za-z0-9._ -]", "", q.get("tenant", [""])[0])[:80]
    os.makedirs(os.path.dirname(f), mode=0o700, exist_ok=True)
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as o:
        o.write(body)
    meta = {"by": who, "at": time.time(), "bytes": n, "sha256": hashlib.sha256(body).hexdigest(), "tenant": tenant,
            "looks_like_ppkg": body[:5] == b"MSWIM"}
    json.dump(meta, open(f + ".json", "w", encoding="utf-8"))
    audit(who, f"Entra provisioning package uploaded for department {d['name']}{' (' + tenant + ')' if tenant else ''}, "
               f"{n} bytes sha256 {meta['sha256'][:16]}", ip)
    return 200, {"ok": True, "ppkg": S.ppkg_info(did)}


def admin_api(h, method, path, who, ip):
    a = read_json(h) if method == "POST" else {}
    op = a.get("op")
    try:
        if path == "/api/admin/server-health":     # the server's own health + tuning advice; POST {"op": "measure"}
            import serverhealth
            if method == "POST" and op == "measure":
                for d in (os.path.join(S.HTTP_ROOT, "iso"), os.path.join(S.HTTP_ROOT, "images")):
                    if serverhealth.measure_disk(d):
                        break
            send_json(h, serverhealth.check(S))
            return True
        if path == "/api/admin/departments":
            if method == "GET":
                send_json(h, {"departments": [dict(d, policy=db.policy(d["id"]), ppkg=S.ppkg_info(d["id"])) for d in db.departments()]})
                return True
            name = str(a.get("name", "")).strip()
            if op == "create":
                if not DEPT_RE.fullmatch(name):
                    raise ValueError("department name: 2-60 letters, digits, spaces, . & ( ) / -")
                if db.q1("SELECT 1 FROM departments WHERE name=?", name):
                    raise ValueError("department exists")
                db.x("INSERT INTO departments(name, policy, created) VALUES(?,?,?)", name,
                     json.dumps(clean_policy(a.get("policy") or {})), db.now())
                audit(who, f"department created: {name}", ip)
            elif op == "update":
                d = db.q1("SELECT * FROM departments WHERE id=?", int(a.get("id", 0)))
                if not d:
                    raise ValueError("unknown department")
                pol = clean_policy(a.get("policy") or {})
                if name and name != d["name"]:
                    if not DEPT_RE.fullmatch(name):
                        raise ValueError("bad department name")
                    db.x("UPDATE departments SET name=? WHERE id=?", name, d["id"])
                db.x("UPDATE departments SET policy=? WHERE id=?", json.dumps(pol), d["id"])
                audit(who, f"department updated: {name or d['name']} policy {pol}", ip)
            elif op == "delete":
                d = db.q1("SELECT * FROM departments WHERE id=?", int(a.get("id", 0)))
                if not d:
                    raise ValueError("unknown department")
                if db.q1("SELECT 1 FROM machines WHERE department_id=?", d["id"]):
                    raise ValueError("move its machines to another department first")
                db.x("DELETE FROM departments WHERE id=?", d["id"])
                audit(who, f"department deleted: {d['name']}", ip)
            else:
                raise ValueError("op: create, update or delete")
            send_json(h, {"ok": True})
            return True

        if path == "/api/admin/settings":
            if method == "GET":
                url = db.get_setting("notify_webhook") or ""
                send_json(h, {"notify_kind": db.get_setting("notify_kind", "slack"),
                              "notify_webhook_set": bool(url), "notify_webhook_host": urlparse_host(url)})
                return True
            if op == "notify":
                url, kind = str(a.get("webhook", "")).strip(), a.get("kind", "slack")
                if kind not in ("slack", "teams", "generic"):
                    raise ValueError("kind: slack, teams or generic")
                if url and not re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d+)?/\S{0,1500}", url):
                    raise ValueError("webhook must be an https:// URL")
                db.set_setting("notify_webhook", url or None)
                db.set_setting("notify_kind", kind)
                audit(who, f"notifications {'set to ' + kind + ' webhook on ' + urlparse_host(url) if url else 'disabled'}", ip)
                if url and a.get("test"):
                    import notify
                    notify.send(f"KwickBoot test message from {S.MY_IP} (set by {who})")
            else:
                raise ValueError("op: notify")
            send_json(h, {"ok": True})
            return True

        if path == "/api/admin/users":
            if method == "GET":
                names = dept_names()
                users = db.q("SELECT id, username, display, role, enabled, created, last_login, must_change_pw, mfa_enabled, mfa_required, "
                             "pw_hash IS NOT NULL AS has_pw FROM users ORDER BY username")
                for u in users:
                    u["departments"] = [{"id": d, "name": names.get(d, "?")} for d in db.user_departments(u["id"])]
                    u["perms"] = db.user_perms(u["id"])
                    u["profile"] = db.profile(u["id"])
                send_json(h, {"users": users})
                return True
            uname = str(a.get("username", "")).strip().lower()
            depts = [int(d) for d in (a.get("departments") or []) if str(d).isdigit()]
            if any(not db.q1("SELECT 1 FROM departments WHERE id=?", d) for d in depts):
                raise ValueError("unknown department")
            role = a.get("role")
            if op == "create":
                if not re.fullmatch(r"[a-z0-9._-]{2,40}", uname):
                    raise ValueError("username: 2-40 lowercase letters, digits, . _ -")
                if role not in RANK:
                    raise ValueError("role: admin, manager, operator or viewer")
                if db.q1("SELECT 1 FROM users WHERE username=?", uname):
                    raise ValueError("user exists")
                pw = str(a.get("password", ""))
                if len(pw) < 10:
                    raise ValueError("initial password must be at least 10 characters")
                db.x("INSERT INTO users(username, display, role, created, must_change_pw) VALUES(?,?,?,?,1)",
                     uname, str(a.get("display", ""))[:80], role, db.now())
                uid = db.q1("SELECT id FROM users WHERE username=?", uname)["id"]
                set_password(uid, pw)
                db.set_user_departments(uid, depts)
                audit(who, f"user created: {uname} ({role}) departments {depts}", ip)
            elif op in ("update", "reset_password", "reset_mfa"):
                u = db.q1("SELECT * FROM users WHERE id=?", int(a.get("id", 0)))
                if not u:
                    raise ValueError("unknown user")
                if op == "reset_mfa":                       # lost phone: they set it up again at the next sign-in (if required)
                    db.x("UPDATE users SET mfa_enabled=0, mfa_secret=NULL, mfa_recovery=NULL, mfa_last=0 WHERE id=?", u["id"])
                    SESSIONS.drop_user(u["id"])
                    audit(who, f"two-step sign-in reset for {u['username']}", ip)
                elif op == "reset_password":
                    err = set_password(u["id"], str(a.get("password", "")))
                    if err:
                        raise ValueError(err)
                    db.x("UPDATE users SET must_change_pw=1 WHERE id=?", u["id"])
                    SESSIONS.drop_user(u["id"])                 # a reset signs them out everywhere
                    audit(who, f"password reset for {u['username']}", ip)
                else:
                    if role is not None and role not in RANK:
                        raise ValueError("bad role")
                    enabled = a.get("enabled", u["enabled"])
                    if u["username"] == who and (role not in (None, "admin") or not enabled):
                        raise ValueError("you cannot demote or disable yourself")
                    if u["role"] == "admin" and (role not in (None, "admin") or not enabled) and \
                            db.q1("SELECT COUNT(*) AS n FROM users WHERE role='admin' AND enabled=1")["n"] <= 1:
                        raise ValueError("cannot remove the last admin")
                    db.x("UPDATE users SET role=?, enabled=?, display=? WHERE id=?", role or u["role"],
                         1 if enabled else 0, str(a.get("display", u["display"]))[:80], u["id"])
                    if "departments" in a:
                        db.set_user_departments(u["id"], depts)
                    if "mfa_required" in a:
                        db.x("UPDATE users SET mfa_required=? WHERE id=?", 1 if a.get("mfa_required") else 0, u["id"])
                        audit(who, f"two-step sign-in {'required' if a.get('mfa_required') else 'not required (personally)'} for {u['username']}", ip)
                    if isinstance(a.get("profile"), dict):
                        save_profile(u["id"], a["profile"])
                        audit(who, f"profile of {u['username']} updated", ip)
                    if isinstance(a.get("perms"), dict):   # {key: "allow"|"deny"|"inherit"}; inherit = the department decides
                        pp = {k: v for k, v in a["perms"].items() if k in db.USER_PERMS and v in ("allow", "deny")}
                        db.x("UPDATE users SET perms=? WHERE id=?", json.dumps(pp, sort_keys=True), u["id"])
                        audit(who, f"permissions of {u['username']}: {pp or 'as the department'}", ip)
                    audit(who, f"user updated: {u['username']} role {role or u['role']} enabled {bool(enabled)} "
                               f"departments {depts if 'departments' in a else 'unchanged'}", ip)
            else:
                raise ValueError("op: create, update or reset_password")
            send_json(h, {"ok": True})
            return True
    except (ValueError, TypeError) as e:
        send_json(h, {"error": str(e)}, 400)
        return True
    send_json(h, {"error": "not found"}, 404)
    return True


def urlparse_host(url):
    from urllib.parse import urlparse
    try:
        return urlparse(url).hostname or ""
    except ValueError:
        return ""


# ---------------------------------------------------------------- out-of-band (BMC / iLO / iDRAC)
def bmc_api(user, method, q, a, ip):
    who, role = user["username"], user["role"]
    mac = S.norm_mac((q.get("mac", [""])[0] if method == "GET" else a.get("mac", "")))
    m = db.machine(mac)
    if not m or not can_access(user, m):
        return 404, {"error": "unknown machine"}
    try:
        if method == "GET":
            what = q.get("what", ["config"])[0]
            if what == "config":
                return 200, {"bmc": bmc.public(mac)}
            if RANK[role] < RANK["operator"]:
                return 403, {"error": "needs role operator"}
            if what == "status":
                return 200, {"status": bmc.status(mac)}
            if what == "sensors":
                return 200, {"sensors": bmc.sensors(mac)}
            if what == "events":
                return 200, {"events": bmc.events(mac)}
            return 400, {"error": "what: config, status, sensors or events"}
        op = a.get("op")
        need = {"set": "manager", "remove": "manager", "repin": "admin", "pxe_once": "operator",
                "power": "manager" if a.get("power") in ("off", "shutdown") else "operator"}.get(op)
        if not need:
            return 400, {"error": "op: set, remove, repin, power or pxe_once"}
        if RANK[role] < RANK[need]:
            return 403, {"error": f"'{op}' needs role {need} (you are {role})"}
        if op == "set":
            bmc.set_config(mac, a.get("type"), str(a.get("host", "")).strip(), str(a.get("user", "")).strip(), a.get("password") or None,
                           str(a.get("vnc", "")).strip() or None)
            audit(who, f"BMC for {mac} set: {a.get('type')} {a.get('host')} user {a.get('user')}{' (password changed)' if a.get('password') else ''}", ip)
        elif op == "remove":
            bmc.remove(mac)
            audit(who, f"BMC for {mac} removed", ip)
        elif op == "repin":
            bmc.repin(mac)
            audit(who, f"BMC certificate for {mac} will be re-trusted on next connection", ip)
        elif op == "power":
            done = bmc.power(mac, a.get("power"))
            audit(who, f"POWER {a.get('power')} ({done}) {m['name']} {mac} via BMC", ip)
        elif op == "pxe_once":
            bmc.pxe_once(mac)
            if a.get("then_reset"):
                bmc.power(mac, "reset" if bmc.status(mac)["power"].lower() == "on" else "on")
            audit(who, f"BMC: next boot from network for {m['name']} {mac}{' + reset' if a.get('then_reset') else ''}", ip)
        return 200, {"ok": True}
    except bmc.BMCError as e:
        return 502, {"error": str(e)}


# ---------------------------------------------------------------- federation
FED_BLOCKED = ("/api/admin/", "/api/me/", "/api/fed/")


def handle_federated(h, method, path):
    """Node side: only /api/* with a valid hub signature; the hub's user acts under THIS node's policies."""
    from urllib.parse import urlparse
    import io
    if not path.startswith("/api/") or path.startswith(FED_BLOCKED):
        send_json(h, {"error": "not available over federation"}, 403)
        return True
    n = int(h.headers.get("Content-Length", "0") or 0)
    body = h.rfile.read(min(n, 200_000)) if n else b""
    try:
        user = federation.verify(method, h.path, h.headers, body)
    except (ValueError, KeyError, TypeError) as e:
        S.log(f"[federation] refused {h.client_address[0]} {method} {path}: {e}")
        send_json(h, {"error": f"federation: {e}"}, 401)
        return True
    h.rfile = io.BytesIO(body)
    h.fed_user = user
    return handle_api_as(h, method, user)


def handle_api_as(h, method, user):
    """Run the normal API for an already-authenticated (federated) user."""
    h.fed_preauth = user
    return handle(h, method, _preauth=True)


def hub_api(h, method, path, q, user, ip):
    who, role = user["username"], user["role"]
    if path == "/api/fed/nodes":
        if method == "GET":
            send_json(h, {"hub": db.get_setting("hub_name", "") or S.MY_IP,
                          "nodes": [{k: n[k] for k in ("id", "name", "url", "last_ok", "last_error")}
                                    | ({"pin": n["pin"]} if role == "admin" else {}) for n in federation.nodes()]})
            return True
        if role != "admin":
            send_json(h, {"error": "admin only"}, 403)
            return True
        a = read_json(h)
        try:
            if a.get("op") == "add":
                nid = federation.add_node(str(a.get("name", "")).strip(), str(a.get("url", "")).strip(), str(a.get("secret", "")), str(a.get("pin", "")))
                audit(who, f"federation: node added {a.get('name')} {a.get('url')}", ip)
                code, _ = federation.forward(nid, "GET", "/api/overview", b"", user, [], db.get_setting("hub_name", "") or S.MY_IP)
                send_json(h, {"ok": True, "id": nid, "reachable": code == 200, "status": code})
                return True
            if a.get("op") == "remove":
                federation.remove_node(int(a.get("id", 0)))
                audit(who, f"federation: node {a.get('id')} removed", ip)
                send_json(h, {"ok": True})
                return True
            raise ValueError("op: add or remove")
        except (ValueError, TypeError) as e:
            send_json(h, {"error": str(e)}, 400)
            return True
    if path == "/api/fed/proxy":
        nid = int((q.get("node", ["0"])[0] or "0") or 0)
        target = q.get("path", [""])[0]
        if not target.startswith("/api/") or target.startswith(FED_BLOCKED) or "\n" in target or "\r" in target:
            send_json(h, {"error": "path not allowed"}, 400)
            return True
        body = b""
        if method == "POST":
            n = int(h.headers.get("Content-Length", "0") or 0)
            body = h.rfile.read(min(n, 200_000))
        names = [] if role == "admin" else [d["name"] for d in db.departments() if d["id"] in user_depts(user)]
        code, data = federation.forward(nid, method, target, body, user, names, db.get_setting("hub_name", "") or S.MY_IP)
        if method == "POST":
            audit(who, f"via node {nid}: POST {target} {body[:300].decode(errors='replace')}", ip)
        h.send_response(code)
        h.send_header("Content-Type", "application/json")
        h.send_header("Cache-Control", "no-store")
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)
        return True
    send_json(h, {"error": "not found"}, 404)
    return True


# ---------------------------------------------------------------- single sign-on (OIDC)
def _redirect(h, url, cookie=None):
    h.send_response(302)
    h.send_header("Location", url)
    h.send_header("Cache-Control", "no-store")
    if cookie:
        h.send_header("Set-Cookie", cookie)
    h.send_header("Content-Length", "0")
    h.end_headers()


def sso_routes(h, method, path, q, ip):
    tls = getattr(h, "tls", False)
    https_port = S.CFG.get("ui_https_port")
    host = h.headers.get("Host", S.MY_IP).split(":")[0]
    if path == "/sso/providers":
        provs = [dict(p, start=(f"https://{host}:{https_port}/sso/start?p={p['id']}" if https_port else None)) for p in sso.public_providers()]
        if https_port and saml.available():
            provs += [{"id": p["id"], "name": p["name"], "start": f"https://{host}:{https_port}/sso/saml/start?p={p['id']}"} for p in saml.public_providers()]
        send_json(h, {"providers": provs, "start": f"https://{host}:{https_port}/sso/start" if https_port else None})
        return True
    if path == "/sso/saml/start" and method == "GET":
        if not tls:
            send_json(h, {"error": "single sign-on needs the HTTPS port (config ui_https_port)"}, 400)
            return True
        try:
            url = saml.start(q.get("p", [""])[0], f"https://{h.headers.get('Host')}/sso/saml/acs")
        except saml.SAMLError as e:
            send_json(h, {"error": str(e)}, 400)
            return True
        _redirect(h, url)
        return True
    if path == "/sso/saml/acs" and method == "POST" and tls:
        n = int(h.headers.get("Content-Length", "0"))
        from urllib.parse import parse_qs as _pq
        form = _pq(h.rfile.read(min(n, 2_000_000)).decode("utf-8", "replace"))
        try:
            prov, claims = saml.acs(form.get("SAMLResponse", [""])[0], f"https://{h.headers.get('Host')}/sso/saml/acs")
            user = saml.login_user(prov, claims)
        except saml.SAMLError as e:
            audit("saml", f"SAML login FAILED: {e}", ip)
            from urllib.parse import quote
            _redirect(h, "/#/login-error/" + quote(str(e)[:200]))
            return True
        tok = secrets.token_hex(32)
        SESSIONS[tok] = {"uid": user["id"], "exp": time.time() + SESSION_TTL}
        db.x("UPDATE users SET last_login=? WHERE id=?", db.now(), user["id"])
        audit(user["username"], f"login ok via SAML {prov['id']} ({user.get('email') or ''}, role {user['role']})", ip)
        _redirect(h, "/", f"kbsess={tok}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age={SESSION_TTL}")
        return True
    if path == "/sso/start" and method == "GET":
        if not tls:
            send_json(h, {"error": "single sign-on needs the HTTPS port (config ui_https_port)"}, 400)
            return True
        try:
            url = sso.start(q.get("p", [""])[0], f"https://{h.headers.get('Host')}/sso/callback")
        except sso.SSOError as e:
            send_json(h, {"error": str(e)}, 400)
            return True
        _redirect(h, url)
        return True
    if path == "/sso/callback" and method == "GET" and tls:
        err = q.get("error_description", q.get("error", [""]))[0]
        try:
            if err:
                raise sso.SSOError(err[:200])
            prov, claims = sso.finish(q.get("state", [""])[0], q.get("code", [""])[0])
            user = sso.login_user(prov, claims)
        except sso.SSOError as e:
            audit(str(q.get("state", ["?"])[0])[:8], f"SSO login FAILED: {e}", ip)
            from urllib.parse import quote
            _redirect(h, "/#/login-error/" + quote(str(e)[:200]))
            return True
        tok = secrets.token_hex(32)
        SESSIONS[tok] = {"uid": user["id"], "exp": time.time() + SESSION_TTL}
        db.x("UPDATE users SET last_login=? WHERE id=?", db.now(), user["id"])
        audit(user["username"], f"login ok via SSO {prov['id']} ({user.get('email') or ''}, role {user['role']})", ip)
        _redirect(h, "/", f"kbsess={tok}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age={SESSION_TTL}")
        return True
    send_json(h, {"error": "not found"}, 404)
    return True
