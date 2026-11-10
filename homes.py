"""homes.py - KwickBoot persistent per-member home for diskless seats.

A diskless seat boots a shared read-only base into RAM and is wiped every boot (privacy by design). This module adds the
other half a lab / office needs: a per-MEMBER home that follows them. When a member signs in at any seat, the seat restores
their home (documents + settings) from the server; when the seat locks, it saves the changed home back before the RAM is
wiped. Transport is the same authenticated KwickBoot HTTP API - no NFS trust, no extra daemon: a seat may only read/write
the home of the member currently signed in at THAT seat (resolved from its active session). Storage is one server-side
archive per member; a live-mount backend can slot in later without changing the seat protocol.
SPDX-License-Identifier: AGPL-3.0-or-later
"""
import os
import time
import tempfile
import db

_DEFAULT_CAP_MB = 200


def _setting(key, default):
    r = db.q1("SELECT value FROM settings WHERE key=?", key)
    return r["value"] if r else default


def _set(key, value):
    db.x("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", key, value)


def enabled():
    return _setting("netbar_home_enabled", "0") == "1"


def set_enabled(on):
    _set("netbar_home_enabled", "1" if on else "0")


def home_dir():
    d = _setting("netbar_home_dir", "")
    if not d:
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "homes")
    return d


def set_home_dir(path):
    _set("netbar_home_dir", path or "")


def cap_mb():
    try:
        return max(1, int(float(_setting("netbar_home_cap_mb", str(_DEFAULT_CAP_MB)))))
    except (TypeError, ValueError):
        return _DEFAULT_CAP_MB


def set_cap_mb(mb):
    _set("netbar_home_cap_mb", str(int(mb)))


def _path(member_id):
    return os.path.join(home_dir(), "%d.tgz" % int(member_id))


def init():
    try:
        os.makedirs(home_dir(), exist_ok=True)
    except OSError:
        pass


def load_home(member_id):
    """The member's saved home archive (bytes), or None if they have none yet."""
    p = _path(member_id)
    if os.path.exists(p):
        with open(p, "rb") as f:
            return f.read()
    return None


def save_home(member_id, data):
    """Store the member's home archive atomically. Rejects an empty or over-quota archive. Returns bytes written."""
    if not data:
        raise ValueError("empty home archive")
    if len(data) > cap_mb() * 1024 * 1024:
        raise ValueError("home over the %d MB quota" % cap_mb())
    init()
    fd, tmp = tempfile.mkstemp(dir=home_dir(), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, _path(member_id))
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return len(data)


def home_info(member_id):
    p = _path(member_id)
    if not os.path.exists(p):
        return {"exists": False, "bytes": 0, "updated": None}
    st = os.stat(p)
    return {"exists": True, "bytes": st.st_size,
            "updated": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))}


def reset_home(member_id):
    try:
        os.remove(_path(member_id))
        return True
    except OSError:
        return False


def homes_summary():
    d = home_dir()
    n = tot = 0
    try:
        for fn in os.listdir(d):
            if fn.endswith(".tgz"):
                n += 1
                tot += os.path.getsize(os.path.join(d, fn))
    except OSError:
        pass
    return {"count": n, "bytes": tot, "cap_mb": cap_mb(), "enabled": enabled(), "dir": d}
