"""schedules.py - KwickBoot scheduling: run any console action (one machine or a group) later, once or repeating.
SPDX-License-Identifier: AGPL-3.0-or-later

A schedule stores exactly what the console would POST (/api/action or /api/bulk) plus who created it. When it is due, it
runs AS ITS CREATOR through the same validated code (webui.do_action / do_bulk): role, department policy, personal
permissions, disk serial confirmations - all checked again at run time, so a schedule can never do more than its creator
may do *then* (a disabled or demoted user's schedules fail and say why).
Jobs that need the RAM OS (install, wipe, capture, restore, manual, scans): LEAD seconds before, the machines are set to
boot the RAM OS once and woken (Wake-on-LAN); at the time, the action is retried until the machine is ready (GRACE).
"""
import json
import threading
import time
import traceback

import db

LEAD = 10 * 60            # wake + set RAM-OS boot this long before the run time
GRACE = 45 * 60           # keep trying this long after the run time while machines are still booting
REPEATS = ("once", "daily", "weekdays", "weekly")
RAMOS_ACTIONS = ("install", "wipe", "capture", "restore", "manual", "job")
W = None                  # webui module (set by start)


def init_schema():
    db.x("""CREATE TABLE IF NOT EXISTS schedules (id INTEGER PRIMARY KEY, name TEXT NOT NULL, created_by TEXT NOT NULL,
            kind TEXT NOT NULL, payload TEXT NOT NULL, macs TEXT NOT NULL, run_at INTEGER NOT NULL, repeat TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL DEFAULT 'waiting', woke INTEGER NOT NULL DEFAULT 0,
            last_run TEXT, last_result TEXT, created TEXT NOT NULL, progress TEXT)""")


def next_time(t, repeat):
    """The next run after t (epoch s) for a repeating schedule, same local wall-clock time."""
    if repeat == "daily":
        return t + 86400
    if repeat == "weekly":
        return t + 7 * 86400
    if repeat == "weekdays":
        t += 86400
        while time.localtime(t).tm_wday >= 5:
            t += 86400
        return t
    return None


def macs_of(kind, payload):
    if kind == "bulk":
        return [str(m) for m in payload.get("macs") or []]
    return [str(payload.get("mac", ""))]


def creator(username):
    return db.q1("SELECT * FROM users WHERE username=? AND enabled=1", username)


def _record(s, state, result, enabled=None):
    db.x("UPDATE schedules SET state=?, last_result=?, last_run=?, woke=0, progress=NULL, enabled=COALESCE(?, enabled) WHERE id=?",
         state, result[:2000], db.now(), enabled, s["id"])


def _finish(s, ok, result):
    nxt = next_time(s["run_at"], s["repeat"])
    now = time.time()
    while nxt is not None and nxt < now:          # server was off: skip missed repeats, do not run them all at once
        nxt = next_time(nxt, s["repeat"])
    _record(s, "done" if ok else "failed", result, enabled=None if nxt else 0)
    if nxt:
        db.x("UPDATE schedules SET run_at=?, state='waiting' WHERE id=?", int(nxt), s["id"])
    W.audit(s["created_by"], f"SCHEDULE '{s['name']}' [{s['id']}] ran: {'ok' if ok else 'FAILED'} - {result[:300]}", "scheduler")
    if not ok:
        try:
            import notify
            notify.send(f"KwickBoot: scheduled '{s['name']}' (by {s['created_by']}) failed: {result[:300]}")
        except Exception:
            pass


def _prepare(s, user):
    """LEAD before: machines boot the RAM OS once, then a Wake-on-LAN packet (and BMC power-on where configured)."""
    for mac in json.loads(s["macs"]):
        try:
            W.do_action(user, {"action": "next", "mac": mac, "next": "alpine"}, "scheduler")
            W.S.wake(mac)
        except Exception:
            pass
    db.x("UPDATE schedules SET woke=1, state='preparing' WHERE id=?", s["id"])


def _run(s, user):
    """One attempt. Returns (finished, ok, text). A group remembers which machines are done, so a retry only
    repeats the ones that were not ready yet (never queues a second job on a machine)."""
    p = json.loads(s["payload"])
    if s["kind"] == "bulk":
        pr = json.loads(s["progress"] or "null") or {"pending": json.loads(s["macs"]), "ok": 0, "failed": []}
        code, obj = W.do_bulk(user, dict(p, macs=pr["pending"]), "scheduler")
        if code != 200:
            return True, False, obj.get("error", f"HTTP {code}")
        pending = []
        for r in obj.get("results") or []:
            err = str(r.get("error") or "")
            if r.get("ok"):
                pr["ok"] += 1
            elif "not ready" in err:
                pending.append(r["mac"])
            else:
                pr["failed"].append(f"{r['mac']}: {err}")
        pr["pending"] = pending
        db.x("UPDATE schedules SET progress=? WHERE id=?", json.dumps(pr), s["id"])
        text = f"{pr['ok']} of {len(json.loads(s['macs']))} machines ok" + (f"; {len(pending)} not in the RAM OS yet" if pending else "") +             (f"; failed: {'; '.join(pr['failed'][:3])}" if pr["failed"] else "")
        return not pending, not pending and not pr["failed"], text
    code, obj = W.do_action(user, p, "scheduler")
    err = str(obj.get("error", ""))
    if code == 409 and "not ready" in err:
        return False, False, err
    return True, code == 200, ((f"job {obj['job']} queued" if obj.get("job") else "done") if code == 200 else err)


_last_prune = [0]


def tick():
    now = time.time()
    if now - _last_prune[0] > 6 * 3600:            # record retention (Audit log page; 0 = keep forever)
        _last_prune[0] = now
        days = int(db.get_setting("audit_keep_days", "0") or 0)
        n = db.audit_prune(days)
        if n:
            W.audit("system", f"retention: {n} audit records older than {days} days removed (policy set by an admin)", "scheduler")
    for s in db.q("SELECT * FROM schedules WHERE enabled=1 AND state IN ('waiting','preparing') AND run_at <= ?", int(now + LEAD)):
        user = creator(s["created_by"])
        if not user:
            _finish(dict(s, repeat="once"), False, f"the creator {s['created_by']} no longer exists or is disabled - schedule stopped")
            continue
        p = json.loads(s["payload"])
        act = p.get("action", "")
        if not s["woke"] and act in RAMOS_ACTIONS and s["run_at"] - now <= LEAD:
            _prepare(s, user)
        if s["run_at"] > now:
            continue
        try:
            finished, ok, text = _run(s, user)
        except Exception as e:                       # never let one schedule stop the loop
            traceback.print_exc()
            finished, ok, text = True, False, f"internal error: {e}"
        if not finished and now < s["run_at"] + GRACE:
            continue                                  # machines still booting: try again on the next tick
        _finish(s, ok, text if finished else "not every machine reached the RAM OS in time - " + text)


def loop():
    while True:
        try:
            tick()
        except Exception:
            traceback.print_exc()
        time.sleep(20)


def start(webui_module):
    global W
    W = webui_module
    init_schema()
    threading.Thread(target=loop, daemon=True, name="schedules").start()


# ---------------------------------------------------------------- API helpers (called from webui)
def visible(user, ids):
    """Schedules this user can see: admins all; others their own + those touching machines they can access."""
    rows = db.q("SELECT * FROM schedules ORDER BY enabled DESC, run_at")
    out = []
    for r in rows:
        macs = json.loads(r["macs"])
        if user["role"] == "admin" or r["created_by"] == user["username"] or \
                any((m := db.machine(x)) and W.can_access(user, m) for x in macs):
            p = json.loads(r["payload"])
            out.append({"id": r["id"], "name": r["name"], "created_by": r["created_by"], "kind": r["kind"],
                        "action": p.get("action"), "detail": {k: v for k, v in p.items() if k not in ("macs", "mac", "confirm", "password")},
                        "machines": [{"mac": x, "name": (db.machine(x) or {}).get("name") or x} for x in macs],
                        "run_at": r["run_at"], "repeat": r["repeat"], "enabled": bool(r["enabled"]), "state": r["state"],
                        "last_run": r["last_run"], "last_result": r["last_result"],
                        "can_edit": user["role"] == "admin" or r["created_by"] == user["username"]})
    return out


def create(user, a):
    """Validate and store. The action itself is fully checked again when it runs."""
    kind = "bulk" if a.get("kind") == "bulk" else "action"
    payload = a.get("payload") if isinstance(a.get("payload"), dict) else {}
    act = str(payload.get("action", ""))
    allowed = W.BULK_SIMPLE + ("install", "restore") if kind == "bulk" else tuple(W.NEEDS)
    if act not in allowed or act == "discover":
        raise ValueError(f"cannot schedule '{act}'")
    if W.RANK[user["role"]] < W.RANK[W.NEEDS.get(act, "admin")]:
        raise ValueError(f"'{act}' needs role {W.NEEDS.get(act)}")
    macs = macs_of(kind, payload)
    if not macs or len(macs) > 500:
        raise ValueError("choose 1 to 500 machines")
    for mac in macs:
        m = db.machine(W.S.norm_mac(mac))
        if not m or not W.can_access(user, m):
            raise ValueError(f"unknown machine {mac}")
        if not W.perm(user, db.policy(m["department_id"]), "schedule"):
            raise ValueError(f"scheduling is switched off for you in the department of {m['name'] or mac}")
    try:
        run_at = int(float(a.get("run_at")))
    except (TypeError, ValueError):
        raise ValueError("run_at: a time")
    if run_at < time.time() - 60 or run_at > time.time() + 366 * 86400:
        raise ValueError("the time must be in the next 12 months")
    repeat = a.get("repeat", "once")
    if repeat not in REPEATS:
        raise ValueError("repeat: once, daily, weekdays or weekly")
    name = str(a.get("name") or "").strip()[:80] or f"{act} on {len(macs)} machine(s)"
    db.x("INSERT INTO schedules(name, created_by, kind, payload, macs, run_at, repeat, created) VALUES(?,?,?,?,?,?,?,?)",
         name, user["username"], kind, json.dumps(payload), json.dumps([W.S.norm_mac(x) for x in macs]), run_at, repeat, db.now())
    return db.q1("SELECT id FROM schedules ORDER BY id DESC LIMIT 1")["id"], name


def change(user, a):
    s = db.q1("SELECT * FROM schedules WHERE id=?", int(a.get("id", 0)))
    if not s:
        raise ValueError("unknown schedule")
    if user["role"] != "admin" and s["created_by"] != user["username"]:
        raise ValueError("only its creator or an admin can change a schedule")
    op = a.get("op")
    if op == "delete":
        db.x("DELETE FROM schedules WHERE id=?", s["id"])
    elif op in ("pause", "resume"):
        db.x("UPDATE schedules SET enabled=?, state=CASE WHEN ?=1 THEN 'waiting' ELSE state END WHERE id=?",
             1 if op == "resume" else 0, 1 if op == "resume" else 0, s["id"])
    elif op == "run_now":
        db.x("UPDATE schedules SET run_at=?, enabled=1, state='waiting', woke=0, progress=NULL WHERE id=?", int(time.time()), s["id"])
    else:
        raise ValueError("op: delete, pause, resume or run_now")
    return s["name"], op
