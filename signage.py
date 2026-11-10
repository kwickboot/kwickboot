"""signage.py - KwickBoot digital signage & lab orchestration (gate #8b), on the same venue spine as netbar.

A venue's displays and lab/exam seats are just machines KwickBoot already knows by MAC. This module adds: a content
library (image / video / web URL), playlists, zones (groups of displays), time-of-day schedules (which zone shows which
playlist when), a resolver a display polls to know what to show right now, a proof-of-play log, and an EXAM/kiosk override
that pins a seat or zone to a single URL for a window. One open box: the same operator console, roles and audit chain as
the rest of KwickBoot - no separate signage server. SPDX-License-Identifier: AGPL-3.0-or-later
"""
import time
import db

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signage_content (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'image',   -- image | video | url
  src TEXT NOT NULL, duration REAL NOT NULL DEFAULT 10, created TEXT, disabled INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS signage_playlists (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, created TEXT, disabled INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS signage_items (
  id INTEGER PRIMARY KEY, playlist_id INTEGER NOT NULL, content_id INTEGER NOT NULL, pos INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS signage_zones (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, note TEXT);
CREATE TABLE IF NOT EXISTS signage_zone_macs (
  zone_id INTEGER NOT NULL, mac TEXT NOT NULL, UNIQUE(zone_id, mac));
CREATE TABLE IF NOT EXISTS signage_schedules (
  id INTEGER PRIMARY KEY, zone_id INTEGER NOT NULL, playlist_id INTEGER NOT NULL,
  days TEXT NOT NULL DEFAULT '0123456',          -- weekday digits Mon=0..Sun=6 that this schedule covers
  start_min INTEGER NOT NULL DEFAULT 0, end_min INTEGER NOT NULL DEFAULT 1440,   -- minutes since local midnight
  priority INTEGER NOT NULL DEFAULT 0, disabled INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS signage_playlog (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, mac TEXT NOT NULL, content_id INTEGER, zone_id INTEGER);
CREATE TABLE IF NOT EXISTS signage_exam (
  scope TEXT NOT NULL, ref TEXT NOT NULL, url TEXT NOT NULL, until REAL, operator TEXT, PRIMARY KEY(scope, ref));
CREATE INDEX IF NOT EXISTS signage_items_pl ON signage_items(playlist_id, pos);
"""


def init():
    for stmt in _SCHEMA.strip().split(";"):
        if stmt.strip():
            db.x(stmt)


def _setting(key, default):
    r = db.q1("SELECT value FROM settings WHERE key=?", key)
    return r["value"] if r else default


def enabled():
    return _setting("signage_enabled", "0") == "1"


def set_enabled(on):
    db.x("INSERT INTO settings(key,value) VALUES('signage_enabled',?) "
         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", "1" if on else "0")


# ---- content library ----
def add_content(name, src, kind="image", duration=10, operator=""):
    name = (name or "").strip()
    if not name or not (src or "").strip():
        raise ValueError("content needs a name and a source")
    if kind not in ("image", "video", "url"):
        raise ValueError("kind must be image, video or url")
    db.x("INSERT INTO signage_content(name,kind,src,duration,created) VALUES(?,?,?,?,?)",
         name, kind, src.strip(), float(duration or 10), db.now())
    return db.q1("SELECT id FROM signage_content WHERE id=last_insert_rowid()")["id"]


def contents(include_disabled=False):
    return db.q("SELECT * FROM signage_content" + ("" if include_disabled else " WHERE disabled=0") + " ORDER BY name")


def update_content(cid, **f):
    cols, vals = [], []
    for k in ("name", "kind", "src", "duration", "disabled"):
        if k in f:
            cols.append(k + "=?"); vals.append(f[k])
    if cols:
        vals.append(cid)
        db.x("UPDATE signage_content SET " + ",".join(cols) + " WHERE id=?", *vals)


# ---- playlists ----
def add_playlist(name, operator=""):
    name = (name or "").strip()
    if not name:
        raise ValueError("playlist needs a name")
    db.x("INSERT INTO signage_playlists(name,created) VALUES(?,?)", name, db.now())
    return db.q1("SELECT id FROM signage_playlists WHERE id=last_insert_rowid()")["id"]


def playlists():
    return db.q("SELECT * FROM signage_playlists WHERE disabled=0 ORDER BY name")


def playlist_items(pid):
    return db.q("SELECT i.id, i.pos, c.* FROM signage_items i JOIN signage_content c ON c.id=i.content_id "
                "WHERE i.playlist_id=? ORDER BY i.pos, i.id", pid)


def set_playlist_items(pid, content_ids):
    """Replace a playlist's items with this ordered list of content ids."""
    db.x("DELETE FROM signage_items WHERE playlist_id=?", pid)
    for pos, cid in enumerate(content_ids):
        db.x("INSERT INTO signage_items(playlist_id,content_id,pos) VALUES(?,?,?)", pid, int(cid), pos)


# ---- zones (groups of displays, by mac) ----
def add_zone(name, note="", operator=""):
    name = (name or "").strip()
    if not name:
        raise ValueError("zone needs a name")
    db.x("INSERT INTO signage_zones(name,note) VALUES(?,?)", name, note or "")
    return db.q1("SELECT id FROM signage_zones WHERE id=last_insert_rowid()")["id"]


def zones():
    return db.q("SELECT z.*, (SELECT COUNT(*) FROM signage_zone_macs m WHERE m.zone_id=z.id) AS displays "
                "FROM signage_zones z ORDER BY z.name")


def set_zone_macs(zone_id, macs):
    db.x("DELETE FROM signage_zone_macs WHERE zone_id=?", zone_id)
    for mac in macs:
        db.x("INSERT OR IGNORE INTO signage_zone_macs(zone_id,mac) VALUES(?,?)", zone_id, mac)


def zone_for_mac(mac):
    return db.q1("SELECT z.* FROM signage_zones z JOIN signage_zone_macs m ON m.zone_id=z.id WHERE m.mac=?", mac)


# ---- schedules ----
def add_schedule(zone_id, playlist_id, days="0123456", start_min=0, end_min=1440, priority=0, operator=""):
    db.x("INSERT INTO signage_schedules(zone_id,playlist_id,days,start_min,end_min,priority) VALUES(?,?,?,?,?,?)",
         int(zone_id), int(playlist_id), str(days), int(start_min), int(end_min), int(priority))
    return db.q1("SELECT id FROM signage_schedules WHERE id=last_insert_rowid()")["id"]


def schedules(zone_id=None):
    if zone_id is None:
        return db.q("SELECT * FROM signage_schedules WHERE disabled=0 ORDER BY zone_id, priority DESC")
    return db.q("SELECT * FROM signage_schedules WHERE zone_id=? AND disabled=0 ORDER BY priority DESC", zone_id)


def active_schedule(zone_id, now=None):
    """The highest-priority enabled schedule covering this weekday + minute-of-day for the zone, else None."""
    now = now or time.time()
    lt = time.localtime(now)
    wd = str(lt.tm_wday)                       # Mon=0..Sun=6
    minute = lt.tm_hour * 60 + lt.tm_min
    for s in schedules(zone_id):
        if wd in s["days"] and s["start_min"] <= minute < s["end_min"]:
            return s
    return None


# ---- exam / kiosk override (pins a seat or zone to one URL) ----
def set_exam(scope, ref, url, minutes=120, operator=""):
    if scope not in ("mac", "zone"):
        raise ValueError("exam scope must be mac or zone")
    until = time.time() + float(minutes) * 60 if minutes else None
    db.x("INSERT INTO signage_exam(scope,ref,url,until,operator) VALUES(?,?,?,?,?) "
         "ON CONFLICT(scope,ref) DO UPDATE SET url=excluded.url, until=excluded.until, operator=excluded.operator",
         scope, str(ref), url, until, operator)


def clear_exam(scope, ref):
    db.x("DELETE FROM signage_exam WHERE scope=? AND ref=?", scope, str(ref))


def _exam_for(mac, zone):
    now = time.time()
    rows = [db.q1("SELECT * FROM signage_exam WHERE scope='mac' AND ref=?", mac)]
    if zone:
        rows.append(db.q1("SELECT * FROM signage_exam WHERE scope='zone' AND ref=?", str(zone["id"])))
    for e in rows:
        if e and (e["until"] is None or e["until"] > now):
            return e
    return None


# ---- what a display polls: what to show right now ----
def now_for(mac, now=None):
    """A display/seat polls this. Returns the content to show now (and seconds to poll again). Exam override wins; else the
    zone's active schedule -> playlist -> the item selected by wall-clock so every display in a zone stays in sync."""
    now = now or time.time()
    if not enabled():
        return {"state": "off"}
    z = zone_for_mac(mac)
    exam = _exam_for(mac, z)
    if exam:
        return {"state": "exam", "kind": "url", "src": exam["url"], "duration": 0, "poll": 15,
                "until": exam["until"]}
    if not z:
        return {"state": "idle", "reason": "no-zone", "poll": 30}
    sch = active_schedule(z["id"], now)
    if not sch:
        return {"state": "idle", "reason": "no-schedule", "zone": z["name"], "poll": 30}
    items = playlist_items(sch["playlist_id"])
    if not items:
        return {"state": "idle", "reason": "empty-playlist", "zone": z["name"], "poll": 30}
    total = sum(max(1.0, it["duration"]) for it in items)
    offset = now % total                      # wall-clock position so a zone's displays are frame-aligned
    for it in items:
        d = max(1.0, it["duration"])
        if offset < d:
            return {"state": "play", "zone": z["name"], "content_id": it["id"], "name": it["name"],
                    "kind": it["kind"], "src": it["src"], "duration": it["duration"],
                    "poll": min(30, max(2, int(d - offset)))}
        offset -= d
    return {"state": "idle", "reason": "wrap", "poll": 10}


# ---- proof of play ----
def played(mac, content_id, zone_id=None):
    db.x("INSERT INTO signage_playlog(ts,mac,content_id,zone_id) VALUES(?,?,?,?)",
         time.time(), mac, int(content_id) if content_id else None, zone_id)


def proof_of_play(days=7):
    since = time.time() - max(1, int(days)) * 86400
    rows = db.q("SELECT p.content_id, c.name, COUNT(*) plays, COUNT(DISTINCT p.mac) displays, MAX(p.ts) last "
                "FROM signage_playlog p LEFT JOIN signage_content c ON c.id=p.content_id "
                "WHERE p.ts>=? GROUP BY p.content_id ORDER BY plays DESC", since)
    return [dict(r) for r in rows]


def overview():
    pls = []
    for p in playlists():
        p = dict(p)
        p["items"] = [{"id": i["id"], "content_id": i["content_id"], "name": i["name"]} for i in playlist_items(p["id"])]
        pls.append(p)
    zs = []
    for z in zones():
        z = dict(z)
        z["macs"] = [r["mac"] for r in db.q("SELECT mac FROM signage_zone_macs WHERE zone_id=? ORDER BY mac", z["id"])]
        zs.append(z)
    return {"enabled": enabled(), "content": contents(True), "playlists": pls,
            "zones": zs, "schedules": schedules(), "proof_of_play": proof_of_play(7)}
