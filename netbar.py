"""netbar.py - KwickBoot netbar / diskless-seat session management: members, prepaid time, live sessions per seat, and a
signed-off billing trail. The front desk runs it from the web console; each diskless seat polls `session_for_seat()` to
know whether to run or lock, and how much time is left.

Why this beats the incumbents (CCBoot / iCafe8, and the Western diskless stacks):
  - ONE open box does the whole venue: diskless Linux Steam seats + PC install/repair + session billing - not a separate
    Windows-only, closed, per-seat-licensed suite.
  - privacy by design: the seat is wiped every boot (RAM root), so a member's sign-ins never leak to the next player.
  - prepaid minutes that draw down live, member codes, walk-in blocks, per-minute rate, and a tamper-evident transaction
    ledger (top-ups and charges), all in the same database and audit chain as the rest of KwickBoot.

Model: a MEMBER prepays MINUTES (minutes_balance). A SESSION binds a seat (mac) to a member (or a walk-in block) and draws
minutes down as real time passes; when the balance/allotment hits zero the seat locks. Money is recorded per transaction
(top-up credits minutes; the implied charge is minutes x rate). SPDX-License-Identifier: AGPL-3.0-or-later
"""
import time
import db

# ---- schema (self-contained; created on init, like the rest of KwickBoot's tables) ----
_SCHEMA = """
CREATE TABLE IF NOT EXISTS netbar_members (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, code TEXT UNIQUE, minutes REAL NOT NULL DEFAULT 0,
  created TEXT, note TEXT, disabled INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS netbar_sessions (
  id INTEGER PRIMARY KEY, mac TEXT NOT NULL, member_id INTEGER, who TEXT,
  started REAL NOT NULL, last REAL NOT NULL, used REAL NOT NULL DEFAULT 0,
  allot REAL, state TEXT NOT NULL DEFAULT 'active', ended REAL, operator TEXT);
CREATE TABLE IF NOT EXISTS netbar_tx (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, member_id INTEGER, kind TEXT NOT NULL,
  minutes REAL NOT NULL DEFAULT 0, amount REAL NOT NULL DEFAULT 0, operator TEXT, note TEXT);
CREATE INDEX IF NOT EXISTS netbar_sessions_active ON netbar_sessions(mac, state);
CREATE TABLE IF NOT EXISTS netbar_products (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, price REAL NOT NULL DEFAULT 0,
  stock INTEGER, category TEXT, disabled INTEGER NOT NULL DEFAULT 0);
"""


def init():
    for stmt in _SCHEMA.strip().split(";"):
        if stmt.strip():
            db.x(stmt)
    # migration: per-page print accounting records the page count on each ledger row
    cols = [r["name"] for r in db.q("PRAGMA table_info(netbar_tx)")]
    if "pages" not in cols:
        db.x("ALTER TABLE netbar_tx ADD COLUMN pages INTEGER NOT NULL DEFAULT 0")


# ---- rates / config (stored in the shared settings table) ----
def _setting(key, default):
    r = db.q1("SELECT value FROM settings WHERE key=?", key)
    return r["value"] if r else default


def rate_per_hour():
    try:
        return float(_setting("netbar_rate_per_hour", "3.0"))
    except (TypeError, ValueError):
        return 3.0


def currency():
    return _setting("netbar_currency", "USD")


def enabled():
    return _setting("netbar_enabled", "0") == "1"


def set_enabled(on):
    db.x("INSERT INTO settings(key,value) VALUES('netbar_enabled',?) "
         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", "1" if on else "0")


def set_rate(per_hour, cur=None):
    db.x("INSERT INTO settings(key,value) VALUES('netbar_rate_per_hour',?) "
         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", str(float(per_hour)))
    if cur:
        db.x("INSERT INTO settings(key,value) VALUES('netbar_currency',?) "
             "ON CONFLICT(key) DO UPDATE SET value=excluded.value", cur)


def _money(minutes):
    return round(minutes / 60.0 * rate_per_hour(), 2)


# ---- members ----
def add_member(name, code=None, minutes=0, operator="", note=""):
    name = (name or "").strip()
    if not name:
        raise ValueError("member needs a name")
    code = (code or "").strip() or None
    if code and db.q1("SELECT 1 FROM netbar_members WHERE code=?", code):
        raise ValueError("that member code is already in use")
    db.x("INSERT INTO netbar_members(name,code,minutes,created,note) VALUES(?,?,?,?,?)",
         name, code, float(minutes or 0), db.now(), note or "")
    m = db.q1("SELECT * FROM netbar_members WHERE id=last_insert_rowid()") or member_by_code(code)
    if minutes:
        db.x("INSERT INTO netbar_tx(ts,member_id,kind,minutes,amount,operator,note) VALUES(?,?,?,?,?,?,?)",
             time.time(), m["id"], "open", float(minutes), _money(float(minutes)), operator, "opening balance")
    return m["id"]


def member(mid):
    return db.q1("SELECT * FROM netbar_members WHERE id=?", mid)


def member_by_code(code):
    return db.q1("SELECT * FROM netbar_members WHERE code=? AND disabled=0", (code or "").strip()) if code else None


def members():
    return db.q("SELECT * FROM netbar_members ORDER BY disabled, name")


def topup(mid, minutes, operator="", amount=None, note=""):
    """Credit prepaid minutes to a member and record the transaction (amount defaults to minutes x rate)."""
    m = member(mid)
    if not m:
        raise ValueError("no such member")
    minutes = float(minutes)
    if minutes <= 0:
        raise ValueError("top-up minutes must be positive")
    db.x("UPDATE netbar_members SET minutes=minutes+? WHERE id=?", minutes, mid)
    amt = _money(minutes) if amount is None else float(amount)
    db.x("INSERT INTO netbar_tx(ts,member_id,kind,minutes,amount,operator,note) VALUES(?,?,?,?,?,?,?)",
         time.time(), mid, "topup", minutes, amt, operator, note or "")
    return member(mid)["minutes"]


# ---- snack bar / shop (POS on the same account + ledger) ----
def products(include_disabled=False):
    return db.q("SELECT * FROM netbar_products" + ("" if include_disabled else " WHERE disabled=0") + " ORDER BY category, name")


def add_product(name, price, stock=None, category="", operator=""):
    name = (name or "").strip()
    if not name:
        raise ValueError("product needs a name")
    st = int(stock) if stock not in (None, "", False) else None
    db.x("INSERT INTO netbar_products(name,price,stock,category) VALUES(?,?,?,?)", name, float(price or 0), st, category or "")
    return db.q1("SELECT id FROM netbar_products WHERE id=last_insert_rowid()")["id"]


def update_product(pid, **f):
    cols, vals = [], []
    for k in ("name", "price", "stock", "category", "disabled"):
        if k in f:
            cols.append(k + "=?")
            vals.append(None if (k == "stock" and f[k] in (None, "", False)) else f[k])
    if cols:
        vals.append(pid)
        db.x("UPDATE netbar_products SET " + ",".join(cols) + " WHERE id=?", *vals)


def _basket(lines):
    return ", ".join("%dx %s" % (q, p["name"]) for p, q in lines)


def sell(items, member_id=None, operator="", note=""):
    """Sell snack-bar items. member_id set -> charge the member's prepaid balance (money converted to minutes at the rate);
    else a cash sale (new revenue). Decrements stock. Returns a receipt. items: [{id, qty}]."""
    total, lines = 0.0, []
    for it in items:
        p = db.q1("SELECT * FROM netbar_products WHERE id=? AND disabled=0", int(it["id"]))
        if not p:
            raise ValueError("no such product")
        qty = max(1, int(it.get("qty", 1)))
        if p["stock"] is not None and p["stock"] < qty:
            raise ValueError("not enough %s in stock" % p["name"])
        total += p["price"] * qty
        lines.append((p, qty))
    if not lines:
        raise ValueError("nothing to sell")
    total = round(total, 2)
    for p, qty in lines:
        if p["stock"] is not None:
            db.x("UPDATE netbar_products SET stock=stock-? WHERE id=?", qty, p["id"])
    if member_id:
        m = member(member_id)
        if not m:
            raise ValueError("no such member")
        mins = (total / rate_per_hour() * 60) if rate_per_hour() > 0 else 0
        if m["minutes"] < mins - 0.01:
            raise ValueError("not enough balance - top up or take cash")
        db.x("UPDATE netbar_members SET minutes=max(0, minutes-?) WHERE id=?", mins, member_id)
        db.x("INSERT INTO netbar_tx(ts,member_id,kind,minutes,amount,operator,note) VALUES(?,?,?,?,?,?,?)",
             time.time(), member_id, "purchase", -round(mins, 1), total, operator, note or _basket(lines))
    else:
        db.x("INSERT INTO netbar_tx(ts,member_id,kind,minutes,amount,operator,note) VALUES(?,?,?,?,?,?,?)",
             time.time(), None, "sale", 0, total, operator, note or _basket(lines))
    return {"total": total, "currency": currency(), "paid": "account" if member_id else "cash",
            "items": [{"name": p["name"], "qty": q, "price": p["price"]} for p, q in lines]}


# ---- print accounting (diskless labs: the prepaid balance IS the print quota) ----
def print_price():
    try:
        return float(_setting("netbar_print_per_page", "0"))
    except (TypeError, ValueError):
        return 0.0


def set_print_price(per_page):
    db.x("INSERT INTO settings(key,value) VALUES('netbar_print_per_page',?) "
         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", str(float(per_page)))


def print_enabled():
    return print_price() > 0


def print_quota(member_id):
    """Pages a member can still afford at the current page price - for a pre-print CUPS hold hook. None = accounting off."""
    pp = print_price()
    if pp <= 0:
        return None
    m = member(member_id)
    if not m:
        return 0
    return int((m["minutes"] / 60.0 * rate_per_hour()) // pp)


def print_charge(pages, mac=None, member_id=None, copies=1, doc="", operator="cups"):
    """Charge a completed print job. Resolves the member from member_id or the seat's active session (mac). With a
    per-page price set, deducts the cost from the member's prepaid balance (the quota); an unattributed job is a cash
    sale. Clamps to the available balance and flags any shortfall (page-log tailing is after the fact). Returns a receipt."""
    total = max(0, int(pages)) * max(1, int(copies))
    if total <= 0:
        return {"ok": True, "pages": 0, "amount": 0, "reason": "no pages"}
    pp = print_price()
    if pp <= 0:
        return {"ok": True, "pages": total, "amount": 0, "reason": "print-free"}
    if not member_id and mac:
        s = active_session(mac)
        if s and s["member_id"]:
            member_id = s["member_id"]
    cost = round(total * pp, 2)
    note = (doc or "").strip()[:60]
    label = "%d pages%s" % (total, (": " + note) if note else "")
    if member_id:
        m = member(member_id)
        if not m:
            raise ValueError("no such member")
        mins = (cost / rate_per_hour() * 60) if rate_per_hour() > 0 else 0
        charged = min(mins, m["minutes"])
        db.x("UPDATE netbar_members SET minutes=max(0, minutes-?) WHERE id=?", charged, member_id)
        db.x("INSERT INTO netbar_tx(ts,member_id,kind,minutes,amount,operator,note,pages) VALUES(?,?,?,?,?,?,?,?)",
             time.time(), member_id, "print", -round(charged, 1), cost, operator, label, total)
        short = round(mins - charged, 2)
        return {"ok": True, "pages": total, "amount": cost, "currency": currency(), "paid": "account",
                "member": m["name"], "member_id": member_id, "short_minutes": short if short > 0.01 else 0,
                "balance": round(member(member_id)["minutes"], 1)}
    db.x("INSERT INTO netbar_tx(ts,member_id,kind,minutes,amount,operator,note,pages) VALUES(?,?,?,?,?,?,?,?)",
         time.time(), None, "printsale", 0, cost, operator, label, total)
    return {"ok": True, "pages": total, "amount": cost, "currency": currency(), "paid": "cash"}


# ---- sessions ----
def active_session(mac):
    return db.q1("SELECT * FROM netbar_sessions WHERE mac=? AND state='active'", mac)


def start_session(mac, member_id=None, minutes=None, who="", operator=""):
    """Begin a seat session. member_id: draw from that member's prepaid balance. minutes (walk-in): a fixed prepaid block.
    One of member_id / minutes is required. Fails if the seat already has an active session."""
    if active_session(mac):
        raise ValueError("that seat already has an active session - stop it first")
    now = time.time()
    if member_id:
        m = member(member_id)
        if not m:
            raise ValueError("no such member")
        if m["disabled"]:
            raise ValueError("member is disabled")
        if m["minutes"] <= 0:
            raise ValueError("member has no prepaid minutes - top up first")
        who = who or m["name"]
        allot = None                      # bounded by the member's live balance, not a fixed block
    else:
        if not minutes or float(minutes) <= 0:
            raise ValueError("a walk-in session needs a positive minutes block")
        allot = float(minutes)
        who = who or "walk-in"
    db.x("INSERT INTO netbar_sessions(mac,member_id,who,started,last,used,allot,state,operator) "
         "VALUES(?,?,?,?,?,0,?, 'active', ?)", mac, member_id, who, now, now, allot, operator)
    return active_session(mac)


def _remaining(s):
    """Minutes left for an active session: a walk-in block minus used, or the member's live balance."""
    if s["member_id"]:
        m = member(s["member_id"])
        return max(0.0, m["minutes"] if m else 0.0)
    return max(0.0, (s["allot"] or 0.0) - s["used"])


def tick(now=None):
    """Advance every active session by real elapsed time: charge members' balances, grow walk-in usage, and end any
    session whose time is exhausted. Returns the list of seats that just locked. Call this on a timer (e.g. every 15 s)."""
    now = now or time.time()
    locked = []
    for s in db.q("SELECT * FROM netbar_sessions WHERE state='active'"):
        dt = max(0.0, (now - s["last"]) / 60.0)          # minutes since last tick
        if dt <= 0:
            continue
        if s["member_id"]:
            m = member(s["member_id"])
            charge = min(dt, m["minutes"] if m else 0.0)
            db.x("UPDATE netbar_members SET minutes=max(0, minutes-?) WHERE id=?", charge, s["member_id"])
        else:
            charge = min(dt, max(0.0, (s["allot"] or 0.0) - s["used"]))
        db.x("UPDATE netbar_sessions SET used=used+?, last=? WHERE id=?", charge, now, s["id"])
        if _remaining(member_session(s["id"])) <= 0.01:
            _end(s["id"], now, "timeup")
            locked.append(s["mac"])
    return locked


def member_session(sid):
    return db.q1("SELECT * FROM netbar_sessions WHERE id=?", sid)


def _end(sid, now, reason):
    s = member_session(sid)
    if not s or s["state"] != "active":
        return
    db.x("UPDATE netbar_sessions SET state='ended', ended=? WHERE id=?", now, sid)
    db.x("INSERT INTO netbar_tx(ts,member_id,kind,minutes,amount,operator,note) VALUES(?,?,?,?,?,?,?)",
         now, s["member_id"], "charge", s["used"], _money(s["used"]), s["operator"], f"session {reason} on {s['mac']}")


def stop_session(mac, operator=""):
    s = active_session(mac)
    if not s:
        return None
    tick()                                               # settle time up to now first
    s = active_session(mac)
    if s:
        _end(s["id"], time.time(), "stopped by " + (operator or "operator"))
    return member_session(s["id"]) if s else None


def add_time(mac, minutes, operator=""):
    """Extend the seat now: top up the bound member, or grow a walk-in block."""
    s = active_session(mac)
    if not s:
        raise ValueError("no active session on that seat")
    minutes = float(minutes)
    if s["member_id"]:
        topup(s["member_id"], minutes, operator=operator, note="added at seat " + mac)
    else:
        db.x("UPDATE netbar_sessions SET allot=allot+? WHERE id=?", minutes, s["id"])
    return _remaining(active_session(mac))


# ---- what a seat asks the server every few seconds ----
def session_for_seat(mac):
    """The seat polls this. Returns what to show: run (with minutes left + whose seat) or lock (idle / time up).
    When netbar billing is OFF (the default), every seat simply runs - free diskless gaming is unaffected."""
    if not enabled():
        return {"state": "run", "reason": "netbar-off", "minutes": 0, "who": "",
                "rate_per_hour": rate_per_hour(), "currency": currency()}
    s = active_session(mac)
    if not s:
        last = db.q1("SELECT * FROM netbar_sessions WHERE mac=? ORDER BY ended DESC LIMIT 1", mac)
        reason = "timeup" if (last and last["ended"]) else "idle"
        return {"state": "locked", "reason": reason, "minutes": 0, "who": "", "rate_per_hour": rate_per_hour(), "currency": currency()}
    rem = _remaining(s)
    return {"state": "run" if rem > 0 else "locked", "reason": "" if rem > 0 else "timeup",
            "minutes": round(rem, 2), "who": s["who"], "member_id": s["member_id"],
            "warn": rem <= 5, "rate_per_hour": rate_per_hour(), "currency": currency()}


def seat_login(mac, code):
    """A player signs in AT the seat with their member code: start a session from their own prepaid balance. No front
    desk needed. Returns {ok, name, minutes} or {ok:false, error}. (Billing off -> nothing to sign into.)"""
    if not enabled():
        return {"ok": True, "reason": "netbar-off"}
    s = active_session(mac)
    if s:
        return {"ok": True, "name": s["who"], "minutes": round(_remaining(s), 1)}
    m = member_by_code(code)
    if not m:
        return {"ok": False, "error": "unknown member code"}
    if m["disabled"]:
        return {"ok": False, "error": "account disabled - see the front desk"}
    if m["minutes"] <= 0:
        return {"ok": False, "error": "no time left - top up at the front desk"}
    start_session(mac, member_id=m["id"], operator="self")
    return {"ok": True, "name": m["name"], "minutes": round(m["minutes"], 1)}


def member_for_home_save(mac, grace=180):
    """The member whose home a seat may save back: the active session's member, or a member session that ended within
    `grace` seconds - so save-on-lock still works in the moment just after the server ended the session on time-up."""
    s = active_session(mac)
    if s and s["member_id"]:
        return s["member_id"]
    r = db.q1("SELECT member_id, ended FROM netbar_sessions WHERE mac=? AND member_id IS NOT NULL AND ended IS NOT NULL "
              "ORDER BY ended DESC LIMIT 1", mac)
    if r and r["ended"] and (time.time() - r["ended"]) <= grace:
        return r["member_id"]
    return None


# ---- operator dashboard: every seat + its live session ----
def seats_overview(seat_macs):
    """seat_macs: the venue's seat MACs (the machines flagged as seats). Returns one row per seat with its live session."""
    tick()
    out = []
    for mac in seat_macs:
        s = active_session(mac)
        row = {"mac": mac, "state": "free", "who": "", "minutes": 0, "member_id": None, "started": None}
        if s:
            rem = _remaining(s)
            row.update(state=("active" if rem > 0 else "timeup"), who=s["who"], minutes=round(rem, 2),
                       member_id=s["member_id"], started=s["started"], used=round(s["used"], 2))
        out.append(row)
    return out


def ledger(limit=200):
    return db.q("SELECT t.*, m.name AS member FROM netbar_tx t LEFT JOIN netbar_members m ON m.id=t.member_id "
                "ORDER BY t.ts DESC LIMIT ?", limit)


def takings_today():
    start = time.time() - (time.time() % 86400)
    r = db.q1("SELECT COALESCE(SUM(amount),0) AS amt, COALESCE(SUM(minutes),0) AS mins FROM netbar_tx "
              "WHERE kind IN ('topup','open','sale','printsale') AND ts>=?", start)
    return {"amount": round(r["amt"], 2), "minutes": round(r["mins"], 1), "currency": currency()}


# ---- analytics: aggregates for the reporting dashboard (charts) ----
def report(days=14):
    days = max(1, min(90, int(days)))
    since = time.time() - days * 86400
    DAY = lambda c: "strftime('%%Y-%%m-%%d', %s, 'unixepoch', 'localtime')" % c
    rev = {r["d"]: (r["amt"], r["mins"]) for r in db.q(
        "SELECT " + DAY("ts") + " d, COALESCE(SUM(amount),0) amt, COALESCE(SUM(minutes),0) mins "
        "FROM netbar_tx WHERE kind IN ('topup','open','sale','printsale') AND ts>=? GROUP BY d", since)}
    sess = {r["d"]: r["n"] for r in db.q(
        "SELECT " + DAY("ended") + " d, COUNT(*) n FROM netbar_sessions WHERE state='ended' AND ended>=? GROUP BY d", since)}
    series = []
    t0 = time.time()
    for i in range(days - 1, -1, -1):
        d = time.strftime("%Y-%m-%d", time.localtime(t0 - i * 86400))
        a, m = rev.get(d, (0, 0))
        series.append({"day": d, "revenue": round(a, 2), "minutes": round(m, 1), "sessions": sess.get(d, 0)})
    mw = db.q1("SELECT COALESCE(SUM(CASE WHEN member_id IS NULL THEN used ELSE 0 END),0) walkin, "
               "COALESCE(SUM(CASE WHEN member_id IS NOT NULL THEN used ELSE 0 END),0) member "
               "FROM netbar_sessions WHERE state='ended' AND ended>=?", since)
    seats = [dict(s) for s in db.q(
        "SELECT mac, ROUND(SUM(used)/60.0,2) hours, COUNT(*) sessions FROM netbar_sessions "
        "WHERE state='ended' AND ended>=? GROUP BY mac ORDER BY SUM(used) DESC LIMIT 8", since)]
    hrs = {int(r["h"]): r["n"] for r in db.q(
        "SELECT strftime('%H', started, 'unixepoch','localtime') h, COUNT(*) n FROM netbar_sessions WHERE started>=? GROUP BY h", since)}
    peak = [{"hour": h, "sessions": hrs.get(h, 0)} for h in range(24)]
    top = [dict(t) for t in db.q(
        "SELECT m.name, ROUND(SUM(t.amount),2) spent, ROUND(SUM(t.minutes),0) minutes FROM netbar_tx t "
        "JOIN netbar_members m ON m.id=t.member_id WHERE t.kind IN ('topup','open') AND t.ts>=? "
        "GROUP BY t.member_id ORDER BY SUM(t.amount) DESC LIMIT 8", since)]
    tot = db.q1("SELECT COALESCE(SUM(amount),0) amt, COUNT(*) n FROM netbar_tx WHERE kind IN ('topup','open','sale','printsale') AND ts>=?", since)
    shop = db.q1("SELECT COALESCE(SUM(amount),0) amt, COUNT(*) n FROM netbar_tx WHERE kind IN ('sale','purchase') AND ts>=?", since)
    pr = db.q1("SELECT COALESCE(SUM(pages),0) pg, COALESCE(SUM(amount),0) amt, COUNT(*) n FROM netbar_tx WHERE kind IN ('print','printsale') AND ts>=?", since)
    sn = db.q1("SELECT COUNT(*) n, COALESCE(AVG(used),0) avg FROM netbar_sessions WHERE state='ended' AND ended>=?", since)
    active = db.q1("SELECT COUNT(*) n FROM netbar_sessions WHERE state='active'")
    memN = db.q1("SELECT COUNT(*) n FROM netbar_members WHERE disabled=0")
    return {"days": days, "currency": currency(), "rate_per_hour": rate_per_hour(), "series": series,
            "mix": {"member": round(mw["member"], 1), "walkin": round(mw["walkin"], 1)},
            "seats": seats, "peak": peak, "top_members": top,
            "kpi": {"revenue": round(tot["amt"], 2), "topups": tot["n"], "sessions": sn["n"],
                    "avg_minutes": round(sn["avg"], 1), "active": active["n"], "members": memN["n"],
                    "shop": round(shop["amt"], 2), "shop_items": shop["n"],
                    "print_pages": pr["pg"], "print_amount": round(pr["amt"], 2),
                    "print_per_page": print_price()}}
