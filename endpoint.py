"""KwickBoot endpoint control: profiles, commands and screenshots for kiosks / signage / labs (opt-in agent).
SPDX-License-Identifier: AGPL-3.0-or-later

Machines installed with an endpoint-capable recipe get a per-machine token (machines.ep_token) and run `kb-endpoint`,
which checks in every 30 s:  GET /ep/checkin?mac=..  (header X-KB-EP: token)
  -> {"version": n, "profile": {...}, "commands": [{"id", "cmd", "arg"}]}
  POST /ep/result?mac=..&id=..      command result text
  POST /ep/screen?mac=..            PNG screenshot (max 6 MB)
Installed Linux systems (servers, desktops, labs) get the generic agent http/scripts/kb-endpoint.sh (opt-out at install):
admins set/add/lock users and install updates from the web console. Passwords are hashed here (SHA-512 crypt) the moment
they arrive; the machine receives only the hash, and the hash is erased from the database once delivered.
"""
import hmac
import json
import os
import re
import secrets
import subprocess
import time

import db

KINDS = ("kiosk", "signage", "lab")
COMMANDS = ("reboot", "reload", "screenshot", "update", "checkupdates", "setpw", "adduser", "lockuser", "unlockuser", "netboot")
ADMIN_COMMANDS = ("setpw", "adduser", "lockuser", "unlockuser")      # change who can log in: admins only
USER_RE = r"[a-z_][a-z0-9_-]{0,31}"
HASH_RE = r"\$6\$[./A-Za-z0-9]{1,16}\$[./A-Za-z0-9]{86}"
PROFILE_KEYS = {"kiosk": ("url", "allow", "idle")}


def init():
    db.x("""CREATE TABLE IF NOT EXISTS profiles (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
            department_id INTEGER, config TEXT NOT NULL DEFAULT '{}', version INTEGER NOT NULL DEFAULT 1, updated TEXT NOT NULL)""")
    db.x("""CREATE TABLE IF NOT EXISTS commands (id INTEGER PRIMARY KEY, mac TEXT NOT NULL, cmd TEXT NOT NULL, arg TEXT,
            created TEXT NOT NULL, created_by TEXT, sent_at TEXT, done_at TEXT, result TEXT)""")
    db.x("CREATE INDEX IF NOT EXISTS commands_mac ON commands(mac, done_at)")
    cols = {r["name"] for r in db.q("PRAGMA table_info(machines)")}
    for c in ("profile_id INTEGER", "ep_token TEXT", "ep_seen REAL", "ep_info TEXT"):
        if c.split()[0] not in cols:
            db.x(f"ALTER TABLE machines ADD COLUMN {c}")


def clean_config(kind, cfg):
    if kind != "kiosk":
        raise ValueError("only kiosk profiles are available so far")
    url = str(cfg.get("url", "")).strip()
    if not re.fullmatch(r"https?://[^\s'\"<>]{3,500}", url):
        raise ValueError("start page: an http(s) address")
    allow = [d.strip().lower() for d in (cfg.get("allow") or []) if str(d).strip()]
    if any(not re.fullmatch(r"(\*\.)?[a-z0-9-]+(\.[a-z0-9-]+)+", d) for d in allow) or len(allow) > 50:
        raise ValueError("allowed sites: domain names like library.example.edu or *.example.edu")
    idle = int(cfg.get("idle", 10) or 0)
    if not 0 <= idle <= 1440:
        raise ValueError("idle reset: 0-1440 minutes")
    return {"url": url, "allow": allow, "idle": idle}


def save_profile(pid, name, kind, department_id, cfg):
    name = str(name or "").strip()
    if not re.fullmatch(r"[\w .&()/-]{2,60}", name):
        raise ValueError("profile name: 2-60 letters, digits, spaces")
    if kind not in KINDS:
        raise ValueError("kind: kiosk, signage or lab")
    c = json.dumps(clean_config(kind, cfg))
    if pid:
        old = db.q1("SELECT * FROM profiles WHERE id=?", pid)
        if not old:
            raise ValueError("unknown profile")
        db.x("UPDATE profiles SET name=?, department_id=?, config=?, version=version+1, updated=? WHERE id=?",
             name, department_id, c, db.now(), pid)
        return pid
    db.x("INSERT INTO profiles(name, kind, department_id, config, updated) VALUES(?,?,?,?,?)", name, kind, department_id, c, db.now())
    return db.q1("SELECT id FROM profiles WHERE name=?", name)["id"]


def profiles(dept_ids=None):
    rows = db.q("SELECT p.*, (SELECT COUNT(*) FROM machines m WHERE m.profile_id=p.id) AS machines FROM profiles p ORDER BY name")
    out = []
    for p in rows:
        if dept_ids is not None and p["department_id"] not in dept_ids and p["department_id"] is not None:
            continue
        p["config"] = json.loads(p["config"])
        out.append(p)
    return out


def issue_token(mac):
    tok = secrets.token_hex(20)
    db.upsert_machine(mac, ep_token=tok)
    return tok


def token_ok(mac, presented):
    m = db.machine(mac)
    return bool(m and m.get("ep_token") and presented and hmac.compare_digest(m["ep_token"], presented))


def hash_password(pw):
    """SHA-512 crypt ($6$), the format chpasswd -e and /etc/shadow use. The plain text lives only in this call."""
    pw = str(pw or "")
    if not 8 <= len(pw) <= 128 or "\n" in pw or ":" in pw:
        raise ValueError("password: 8-128 characters, no ':' or line breaks")
    try:
        import crypt                                    # Python <= 3.12
        h = crypt.crypt(pw, crypt.mksalt(crypt.METHOD_SHA512))
    except ImportError:                                 # Python 3.13+ (Debian 13): OpenSSL, password on stdin (not in argv)
        h = subprocess.run(["openssl", "passwd", "-6", "-stdin"], input=pw + "\n", capture_output=True, text=True,
                           timeout=10, check=True).stdout.strip()
    if not re.fullmatch(HASH_RE, h):
        raise ValueError("could not hash the password")
    return h


def make_arg(cmd, user="", password="", admin=False):
    """Validated command argument. Only these exact shapes ever reach a machine."""
    if cmd not in ADMIN_COMMANDS:
        return ""
    user = str(user or "").strip()
    if not re.fullmatch(USER_RE, user) or user in ("root", "kiosk", "nobody"):
        raise ValueError("user name: lowercase letters, digits, '-' or '_' (not root/kiosk)")
    if cmd == "setpw":
        return f"{user}:{hash_password(password)}"
    if cmd == "adduser":
        return f"{user}:{hash_password(password)}:{1 if admin else 0}"
    return user


def queue(mac, cmd, arg, who):
    if cmd not in COMMANDS:
        raise ValueError("command: " + ", ".join(COMMANDS))
    if not re.fullmatch(rf"(|{USER_RE}(:{HASH_RE}(:[01])?)?)", arg or ""):
        raise ValueError("bad command argument")
    db.x("INSERT INTO commands(mac, cmd, arg, created, created_by) VALUES(?,?,?,?,?)", mac, cmd, arg or "", db.now(), who)


def checkin(mac, info):
    m = db.machine(mac)
    db.upsert_machine(mac, ep_seen=time.time(), ep_info=json.dumps(info)[:2000])
    prof = db.q1("SELECT * FROM profiles WHERE id=?", m.get("profile_id")) if m.get("profile_id") else None
    cmds = db.q("SELECT id, cmd, arg FROM commands WHERE mac=? AND sent_at IS NULL AND created >= datetime('now','-1 day','localtime') ORDER BY id", mac)
    for c in cmds:      # delivered: keep only the user name (password hashes do not stay in the database)
        db.x("UPDATE commands SET sent_at=?, arg=? WHERE id=?", db.now(), (c["arg"] or "").split(":")[0], c["id"])
    return {"version": prof["version"] if prof else 0, "profile": json.loads(prof["config"]) if prof else None,
            "profile_name": prof["name"] if prof else None, "commands": cmds, "server_time": int(time.time())}


def as_lines(r):
    """checkin() result for the generic shell agent: one 'id cmd arg' line per command (all fields validated)."""
    import base64
    out = ""
    if r.get("profile") is not None or r.get("version") is not None:   # kiosks apply it, other machines ignore it
        p = base64.b64encode(json.dumps(r["profile"]).encode()).decode() if r.get("profile") else "-"
        out += f"0 profile {int(r.get('version') or 0)} {p}\n"
    return out + "".join(f"{c['id']} {c['cmd']} {c['arg'] or '-'}\n" for c in r["commands"])


def result(mac, cid, text):
    db.x("UPDATE commands SET done_at=?, result=? WHERE id=? AND mac=?", db.now(), (text or "")[:2000], cid, mac)


def status(mac):
    m = db.machine(mac) or {}
    last = db.q("SELECT id, cmd, arg, created, created_by, sent_at, done_at, result FROM commands WHERE mac=? ORDER BY id DESC LIMIT 8", mac)
    for c in last:      # never show hashes: user name only
        c["arg"] = (c["arg"] or "").split(":")[0]
    return {"managed": bool(m.get("ep_token")), "profile_id": m.get("profile_id"),
            "seen_age": int(time.time() - m["ep_seen"]) if m.get("ep_seen") else None,
            "info": json.loads(m.get("ep_info") or "null"), "commands": last}
