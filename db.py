"""KwickBoot state store: SQLite (WAL), one connection per thread.

Agents exist only while a machine runs the RAM OS; afterwards a machine is a record
(inventory, preflight, jobs, audit). Job logs stay as files under machines/<mac>/jobs/.
SPDX-License-Identifier: AGPL-3.0-or-later
"""
import json
import os
import sqlite3
import threading
import time
import zlib

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS departments (
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL,
  policy TEXT NOT NULL DEFAULT '{}', created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS machines (
  mac TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', approved INTEGER NOT NULL DEFAULT 0,
  department_id INTEGER REFERENCES departments(id),
  default_action TEXT NOT NULL DEFAULT 'alpine', next_action TEXT, next_used_at REAL,
  discovered TEXT, agent_state TEXT, agent_ip TEXT, agent_msg TEXT, agent_seen REAL,
  token TEXT, preflight TEXT, cpu TEXT, ram TEXT, notes TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY, mac TEXT NOT NULL, kind TEXT NOT NULL, created TEXT NOT NULL, body BLOB NOT NULL);
CREATE INDEX IF NOT EXISTS reports_mac_kind ON reports(mac, kind, id);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, mac TEXT NOT NULL, type TEXT NOT NULL, params TEXT NOT NULL DEFAULT '{}',
  state TEXT NOT NULL, rc INTEGER, error TEXT, created TEXT NOT NULL, started TEXT, finished TEXT,
  created_by TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS jobs_mac ON jobs(mac, created);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, display TEXT NOT NULL DEFAULT '',
  role TEXT NOT NULL CHECK (role IN ('admin','manager','operator','viewer')),
  pw_hash TEXT, pw_salt TEXT, pw_iter INTEGER, enabled INTEGER NOT NULL DEFAULT 1,
  created TEXT NOT NULL, last_login TEXT);
CREATE TABLE IF NOT EXISTS user_departments (
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  department_id INTEGER NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
  PRIMARY KEY (user_id, department_id));
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, who TEXT NOT NULL, ip TEXT, action TEXT NOT NULL, detail TEXT);
CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
"""

_local = threading.local()
DB_PATH = None


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def init(path):
    global DB_PATH
    DB_PATH = path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    c = conn()
    c.executescript(SCHEMA)
    c.commit()
    upgrade()


SCHEMA_VERSION = 3


def upgrade():
    """Additive schema upgrades (never drops data)."""
    v = int(get_setting("schema_version", "1"))
    if v < 2:
        cols = {r["name"] for r in q("PRAGMA table_info(users)")}
        if "must_change_pw" not in cols:
            x("ALTER TABLE users ADD COLUMN must_change_pw INTEGER NOT NULL DEFAULT 0")
        x("CREATE INDEX IF NOT EXISTS machines_dept ON machines(department_id)")
        set_setting("schema_version", 2)
    if v < 3:
        cols = {r["name"] for r in q("PRAGMA table_info(machines)")}
        for c in ("health TEXT", "health_level TEXT", "bmc TEXT"):
            if c.split()[0] not in cols:
                x(f"ALTER TABLE machines ADD COLUMN {c}")
        set_setting("schema_version", 3)
    if v < 4:   # per-machine hand-off to the installed OS: auto | chainload | exit (firmware differences)
        cols = {r["name"] for r in q("PRAGMA table_info(machines)")}
        if "local_boot" not in cols:
            x("ALTER TABLE machines ADD COLUMN local_boot TEXT")
        set_setting("schema_version", 4)
    if v < 5:   # per-machine emergency console in the RAM OS (kb_shell=1 on its kernel command line; admins only)
        cols = {r["name"] for r in q("PRAGMA table_info(machines)")}
        if "kb_shell" not in cols:
            x("ALTER TABLE machines ADD COLUMN kb_shell INTEGER")
        set_setting("schema_version", 5)
    if v < 6:   # first boot of a fresh install: did the new system come up? (JSON: job, os, since, check, state, at)
        cols = {r["name"] for r in q("PRAGMA table_info(machines)")}
        if "firstboot" not in cols:
            x("ALTER TABLE machines ADD COLUMN firstboot TEXT")
        set_setting("schema_version", 6)
    if v < 7:   # secure-wipe certificates (certs.py): the signed record of every verified wipe, never deleted
        x("""CREATE TABLE IF NOT EXISTS wipe_certs (id TEXT PRIMARY KEY, mac TEXT NOT NULL, job TEXT, serial TEXT,
             created TEXT NOT NULL, body TEXT NOT NULL, sig TEXT NOT NULL)""")
        x("CREATE INDEX IF NOT EXISTS wipe_certs_mac ON wipe_certs(mac)")
        x("CREATE INDEX IF NOT EXISTS wipe_certs_serial ON wipe_certs(serial)")
        set_setting("schema_version", 7)
    if v < 8:   # ISO ownership: department_id NULL = shared library (admins); else that department's own upload
        x("""CREATE TABLE IF NOT EXISTS iso_meta (name TEXT PRIMARY KEY, department_id INTEGER, uploaded_by TEXT,
             sha256 TEXT, created TEXT)""")
        set_setting("schema_version", 8)
    if v < 9:   # per-user permission overrides (JSON {key: "allow"|"deny"}; missing = the department decides) + personal ISOs
        cols = {r["name"] for r in q("PRAGMA table_info(users)")}
        if "perms" not in cols:
            x("ALTER TABLE users ADD COLUMN perms TEXT")
        cols = {r["name"] for r in q("PRAGMA table_info(iso_meta)")}
        if "owner_user" not in cols:
            x("ALTER TABLE iso_meta ADD COLUMN owner_user TEXT")
        set_setting("schema_version", 9)
    if v < 10:  # user profiles: contact details + where to find the person; avatar as a small image (served same-origin)
        cols = {r["name"] for r in q("PRAGMA table_info(users)")}
        for c in PROFILE_FIELDS + ("avatar BLOB", "avatar_type TEXT"):
            c = c if " " in c else c + " TEXT"
            if c.split()[0] not in cols:
                x(f"ALTER TABLE users ADD COLUMN {c}")
        set_setting("schema_version", 10)
    if v < 11:  # tamper-evident audit: each row carries sha256(previous chain + its own fields); existing rows chained now
        cols = {r["name"] for r in q("PRAGMA table_info(audit)")}
        if "chain" not in cols:
            x("ALTER TABLE audit ADD COLUMN chain TEXT")
        prev = ""
        for r in q("SELECT * FROM audit ORDER BY id"):
            prev = _chain(prev, r)
            x("UPDATE audit SET chain=? WHERE id=?", prev, r["id"])
        set_setting("schema_version", 11)
    if v < 12:  # sign-in sessions survive restarts; the table holds only sha256(token), never the token itself
        x("CREATE TABLE IF NOT EXISTS sessions (h TEXT PRIMARY KEY, uid INTEGER NOT NULL, exp REAL NOT NULL)")
        set_setting("schema_version", 12)
    if v < 13:  # two-step sign-in (authenticator app): secret, on/off, last used time step, recovery code hashes, required
        cols = {r["name"] for r in q("PRAGMA table_info(users)")}
        for c, t in (("mfa_secret", "TEXT"), ("mfa_enabled", "INTEGER NOT NULL DEFAULT 0"), ("mfa_last", "INTEGER NOT NULL DEFAULT 0"),
                     ("mfa_recovery", "TEXT"), ("mfa_required", "INTEGER NOT NULL DEFAULT 0")):
            if c not in cols:
                x(f"ALTER TABLE users ADD COLUMN {c} {t}")
        set_setting("schema_version", 13)
    if v < 14:  # boot history: every network-boot step per machine (the rest of the service log stays in RAM)
        x("CREATE TABLE IF NOT EXISTS boot_events (id INTEGER PRIMARY KEY, mac TEXT NOT NULL, step TEXT NOT NULL, detail TEXT, t REAL NOT NULL)")
        x("CREATE INDEX IF NOT EXISTS boot_events_mac_t ON boot_events(mac, t)")
        set_setting("schema_version", 14)


# ---------------------------------------------------------------- departments + policy
POLICY_DEFAULT = {"allowed_os": [], "allowed_bundles": [], "hostname_prefix": "", "install_min_role": "manager", "allow_override": True,
                  # Manual install (the vendor's own installer from an ISO): on/off, which shared ISOs ([] = all), own uploads
                  "manual_install": True, "allowed_isos": [], "iso_upload": True,
                  "schedule": True,           # may schedule actions for later / repeating
                  "ai_advice": True,          # may ask the configured AI service for install plans
                  "ai_cloud": True,           # ... also cloud AI services (off = local AI only: strict data policies)
                  "ai_byok": True,            # bring your own AI: department + personal AI services and keys
                  # Windows template: the department's defaults ("" = no default); win_custom off = installs must use them
                  "win_iso": "", "win_edition": "", "win_debloat": "", "win_apps": "", "win_custom": True,
                  "mfa_required": False,      # the department's users must use two-step sign-in (authenticator app)
                  # Windows support information (Settings > System > About) + registered organization/owner
                  "win_oem": {},
                  # boot order after an install/restore: network-first (KwickBoot always reachable) or os-first (network
                  # only when IT starts a reinstall: one-time PXE via the management agent or the BMC; less pre-boot exposure)
                  "boot_order": "network-first"}


def iso_meta():
    return {r["name"]: r for r in q("SELECT * FROM iso_meta")}


def set_iso_meta(name, department_id, who, sha, owner_user=None):
    x("INSERT OR REPLACE INTO iso_meta(name, department_id, uploaded_by, sha256, created, owner_user) "
      "VALUES(?,?,?,?,datetime('now'),?)", name, department_id, who, sha, owner_user)


PROFILE_FIELDS = ("email", "phone", "contact", "building", "office", "location")


def profile(user_id):
    r = q1("SELECT " + ", ".join(PROFILE_FIELDS) + ", avatar IS NOT NULL AS has_avatar FROM users WHERE id=?", user_id) or {}
    return {k: r.get(k) or "" for k in PROFILE_FIELDS} | {"has_avatar": bool(r.get("has_avatar"))}


USER_PERMS = ("allow_override", "manual_install", "iso_upload", "schedule", "ai_advice", "ai_cloud", "ai_byok", "win_custom")   # the switches a user can have set for them personally


def user_perms(user_id):
    r = q1("SELECT perms FROM users WHERE id=?", user_id)
    try:
        p = json.loads((r or {}).get("perms") or "{}")
    except ValueError:
        p = {}
    return {k: v for k, v in p.items() if k in USER_PERMS and v in ("allow", "deny")}


def drop_iso_meta(name):
    x("DELETE FROM iso_meta WHERE name=?", name)


def policy(department_id):
    """Effective policy of a department (unassigned machines get the default policy)."""
    p = dict(POLICY_DEFAULT)
    if department_id:
        r = q1("SELECT policy FROM departments WHERE id=?", department_id)
        if r:
            try:
                p.update({k: v for k, v in json.loads(r["policy"] or "{}").items() if k in POLICY_DEFAULT})
            except ValueError:
                pass
    return p


def departments():
    return q("SELECT d.*, (SELECT COUNT(*) FROM machines m WHERE m.department_id=d.id) AS machines, "
             "(SELECT COUNT(*) FROM user_departments u WHERE u.department_id=d.id) AS users FROM departments d ORDER BY name")


def user_departments(user_id):
    return [r["department_id"] for r in q("SELECT department_id FROM user_departments WHERE user_id=?", user_id)]


def set_user_departments(user_id, dept_ids):
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        c.execute("DELETE FROM user_departments WHERE user_id=?", (user_id,))
        for d in dept_ids:
            c.execute("INSERT INTO user_departments(user_id, department_id) VALUES(?,?)", (user_id, d))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise


def conn():
    c = getattr(_local, "c", None)
    if c is None:
        c = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)   # autocommit; explicit BEGIN where needed
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=10000")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA synchronous=NORMAL")
        _local.c = c
    return c


# Every query closes its cursor at once: an unfinished SELECT keeps a read transaction open on that thread's connection,
# and with hundreds of long-lived threads (live consoles) such stale readers stop WAL checkpoints, so the log grows and
# writers start to wait. Readers never block each other in WAL mode; one writer at a time is plenty for KwickBoot.
def q(sql, *args):
    cur = conn().execute(sql, args)
    try:
        return [dict(r) for r in cur.fetchall()]
    finally:
        cur.close()


def q1(sql, *args):
    cur = conn().execute(sql, args)
    try:
        r = cur.fetchone()
        return dict(r) if r else None
    finally:
        cur.close()


def x(sql, *args):
    cur = conn().execute(sql, args)
    cur.close()
    return cur


def checkpoint():
    """Fold the write-ahead log back into the database (keeps it small; called every few minutes by the server)."""
    try:
        conn().execute("PRAGMA wal_checkpoint(TRUNCATE)").close()
    except sqlite3.OperationalError:
        pass


# ---------------------------------------------------------------- settings
def get_setting(k, default=None):
    r = q1("SELECT value FROM settings WHERE key=?", k)
    return r["value"] if r else default


def set_setting(k, v):
    if v is None:
        x("DELETE FROM settings WHERE key=?", k)
    else:
        x("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", k, str(v))


# ---------------------------------------------------------------- machines
def machine(mac):
    return q1("SELECT * FROM machines WHERE mac=?", mac)


def machine_row(mac):
    """One machine as the console lists it (with its department's name)."""
    return q1("SELECT m.*, d.name AS department FROM machines m LEFT JOIN departments d ON d.id=m.department_id WHERE m.mac=?", mac)


ON_MACHINE_CHANGE = None     # set by the server: called (mac, changed column names, created) after every machine update


# ---- boot history: each network-boot step of each machine (kept 90 days) - the part of the log worth keeping
def boot_event(mac, step, detail=""):
    x("INSERT INTO boot_events(mac, step, detail, t) VALUES(?,?,?,?)", mac, step, (detail or "")[:200], time.time())


def boot_events(mac, limit=50):
    return q("SELECT step, detail, t FROM boot_events WHERE mac=? ORDER BY t DESC LIMIT ?", mac, limit)


def prune_boot_events(days=90):
    x("DELETE FROM boot_events WHERE t < ?", time.time() - days * 86400)


def upsert_machine(mac, **kw):
    created = not machine(mac)
    if created:
        x("INSERT INTO machines(mac, discovered) VALUES(?,?)", mac, kw.pop("discovered", None) or now())
    if kw:
        cols = ", ".join(f"{k}=?" for k in kw)
        x(f"UPDATE machines SET {cols} WHERE mac=?", *kw.values(), mac)
    if ON_MACHINE_CHANGE:
        try:
            ON_MACHINE_CHANGE(mac, set(kw), created)
        except Exception:                                # noqa: BLE001 - a live push must never break a write
            pass


def machines(dept_ids=None, include_unassigned=True):
    """All machines, or only those in dept_ids (+ unassigned ones when include_unassigned)."""
    sql = "SELECT m.*, d.name AS department FROM machines m LEFT JOIN departments d ON d.id=m.department_id"
    args = []
    if dept_ids is not None:
        cond = [f"m.department_id IN ({','.join('?' * len(dept_ids))})"] if dept_ids else []
        if include_unassigned:
            cond.append("m.department_id IS NULL")
        sql += " WHERE " + (" OR ".join(cond) if cond else "0")
        args = list(dept_ids)
    return q(sql + " ORDER BY m.name, m.mac", *args)


# ---------------------------------------------------------------- reports
def add_report(mac, kind, text):
    x("INSERT INTO reports(mac, kind, created, body) VALUES(?,?,?,?)", mac, kind, now(), zlib.compress(text.encode(), 6))


def latest_report(mac, kind):
    r = q1("SELECT body, created FROM reports WHERE mac=? AND kind=? ORDER BY id DESC LIMIT 1", mac, kind)
    return (zlib.decompress(r["body"]).decode(errors="replace"), r["created"]) if r else ("", None)


# ---------------------------------------------------------------- jobs
def add_job(mac, typ, params, created_by=""):
    jid = time.strftime("%Y%m%d-%H%M%S") + "-" + typ
    while q1("SELECT 1 FROM jobs WHERE id=?", jid):
        jid += "x"
    x("INSERT INTO jobs(id, mac, type, params, state, created, created_by) VALUES(?,?,?,?,?,?,?)",
      jid, mac, typ, json.dumps(params), "queued", now(), created_by)
    return jid


def jobs(mac, limit=30):
    out = q("SELECT * FROM jobs WHERE mac=? ORDER BY created DESC, id DESC LIMIT ?", mac, limit)
    for j in out:
        j["params"] = json.loads(j["params"] or "{}")
    return out


def take_next_job(mac):
    """Atomically move the oldest queued job of `mac` to running; returns it or None."""
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        j = q1("SELECT * FROM jobs WHERE mac=? AND state='queued' ORDER BY created, id LIMIT 1", mac)
        if j:
            c.execute("UPDATE jobs SET state='running', started=? WHERE id=?", (now(), j["id"]))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    if j:
        j["params"] = json.loads(j["params"] or "{}")
    return j


def fail_job(jid, error):
    x("UPDATE jobs SET state='failed', error=?, finished=? WHERE id=?", error, now(), jid)


def finish_job(jid, rc):
    x("UPDATE jobs SET state=?, rc=?, finished=? WHERE id=?", "done" if rc == 0 else "failed", rc, now(), jid)
    return q1("SELECT * FROM jobs WHERE id=?", jid)


# ---------------------------------------------------------------- audit
_AUDIT_LOCK = threading.Lock()


def _chain(prev, r):
    import hashlib
    return hashlib.sha256("\x1f".join([prev, str(r["id"]), r["ts"], r["who"], r.get("ip") or "", r["action"], r.get("detail") or ""])
                          .encode("utf-8")).hexdigest()


def audit(who, action, detail="", ip=None):
    """Append one record. Records are chained (sha256 over the previous record's chain + this one), so editing or
    removing any past record is detectable by audit_verify(); retention pruning is itself recorded."""
    with _AUDIT_LOCK:
        c = conn()
        own = not c.in_transaction           # inside a caller's transaction: become part of it
        if own:
            c.execute("BEGIN IMMEDIATE")
        try:
            last = c.execute("SELECT chain FROM audit ORDER BY id DESC LIMIT 1").fetchone()
            cur = c.execute("INSERT INTO audit(ts, who, ip, action, detail) VALUES(?,?,?,?,?)", (now(), who, ip, action, detail))
            r = dict(c.execute("SELECT * FROM audit WHERE id=?", (cur.lastrowid,)).fetchone())
            c.execute("UPDATE audit SET chain=? WHERE id=?", (_chain((last[0] if last else "") or "", r), r["id"]))
            if own:
                c.execute("COMMIT")
        except Exception:
            if own:
                c.execute("ROLLBACK")
            raise


def audit_verify():
    """Walk the chain. The first remaining record is the anchor (earlier ones may have been pruned by retention)."""
    rows = q("SELECT * FROM audit ORDER BY id")
    if not rows:
        return {"ok": True, "records": 0}
    prev = None
    for r in rows:
        if prev is not None and _chain(prev, r) != r["chain"]:
            return {"ok": False, "records": len(rows), "broken_at": r["id"], "ts": r["ts"],
                    "detail": "record changed, inserted or removed here"}
        prev = r["chain"] or ""
    return {"ok": True, "records": len(rows), "first": rows[0]["ts"], "last": rows[-1]["ts"], "head": rows[-1]["chain"]}


def audit_prune(days):
    """Retention: remove records older than `days` (0 = keep forever). Returns the number removed."""
    if not days:
        return 0
    cut = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - days * 86400))
    n = q1("SELECT COUNT(*) AS n FROM audit WHERE ts < ?", cut)["n"]
    if n:
        x("DELETE FROM audit WHERE ts < ?", cut)
    return n


# ---------------------------------------------------------------- one-time import of the file-based state (v1 -> v2)
def migrate_files(base, cfg):
    if get_setting("migrated_files"):
        return 0
    n = 0
    st = os.path.join(base, "machines")

    def rd(p):
        try:
            return open(p, encoding="utf-8", errors="replace").read()
        except OSError:
            return ""

    try:
        reg = json.load(open(os.path.join(st, "registry.json"), encoding="utf-8-sig"))
    except Exception:
        reg = {}
    macs = set(reg) | set(cfg.get("allowed_macs", {}))
    if os.path.isdir(st):
        macs |= {d.replace("-", ":") for d in os.listdir(st) if len(d) == 17 and d.count("-") == 5}
    for mac in sorted(macs):
        d = os.path.join(st, mac.replace(":", "-"))
        info = dict(reg.get(mac, {}))
        v = cfg.get("allowed_macs", {}).get(mac)
        if v is not None:
            info.update(v if isinstance(v, dict) else {"name": v})
            info["approved"] = True
        try:
            info.update(json.load(open(os.path.join(d, "meta.json"), encoding="utf-8")))
        except Exception:
            pass
        try:
            ag = json.load(open(os.path.join(d, "agent.json")))
        except Exception:
            ag = {}
        dflt = rd(os.path.join(d, "default")).strip() or info.get("default", "alpine")
        upsert_machine(mac, name=info.get("name", ""), approved=1 if info.get("approved") else 0,
                       discovered=info.get("discovered") or now(), default_action=dflt,
                       next_action=rd(os.path.join(d, "next")).strip() or None,
                       agent_state=ag.get("state"), agent_ip=ag.get("ip"), agent_msg=ag.get("msg"),
                       token=rd(os.path.join(d, "token")).strip() or None)
        for kind, fn in (("inventory", "latest.txt"), ("preflight", "preflight.txt")):
            t = rd(os.path.join(d, fn))
            if t:
                add_report(mac, kind, t)
        jd = os.path.join(d, "jobs")
        if os.path.isdir(jd):
            for f in sorted(os.listdir(jd)):
                if f.endswith(".json"):
                    try:
                        j = json.load(open(os.path.join(jd, f), encoding="utf-8"))
                        x("INSERT OR IGNORE INTO jobs(id, mac, type, params, state, rc, error, created, started, finished) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?)", j["id"], mac, j["type"], json.dumps(j.get("params", {})),
                          j.get("state", "done"), j.get("rc"), j.get("error"), j.get("created") or now(),
                          j.get("started"), j.get("finished"))
                    except Exception:
                        pass
        n += 1
    try:
        a = json.load(open(os.path.join(base, "ui", "admin.json")))
        if not q1("SELECT 1 FROM users WHERE username='admin'"):
            x("INSERT INTO users(username, display, role, pw_hash, pw_salt, pw_iter, created) VALUES(?,?,?,?,?,?,?)",
              "admin", "Administrator", "admin", a["hash"], a["salt"], int(a["iter"]), now())
    except Exception:
        pass
    du = rd(os.path.join(st, "discovery_until")).strip()
    if du:
        set_setting("discovery_until", du)
    set_setting("migrated_files", now())
    return n
