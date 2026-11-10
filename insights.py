"""insights.py - KwickBoot fleet-learning knowledge base (the data network-effect moat), server side.
SPDX-License-Identifier: AGPL-3.0-or-later

FULL DISCLOSURE, NO STEALTH - these are design rules, not options:
  - OFF by default. Collecting locally and sharing to the network are two separate, explicit opt-ins.
  - Every record is anonymised AT SOURCE before it is stored or sent. We keep only hardware MODEL / CLASS identifiers
    that millions of machines share (vendor, product model, CPU model, disk model, PCI device classes, firmware level)
    and the install OUTCOME. We never keep or send: disk/board serials, MAC or IP addresses, hostnames, usernames,
    department or organisation names, the serial-matched install target, or what OS was on the disk before.
  - The operator can see EXACTLY what a record contains before and after it is made (`pxectl insights preview`, and the
    console) - nothing is hidden, and the code is open (AGPL) so anyone can audit it.
  - "local": records stay on this server and improve only this fleet's own advisor. "share": records ALSO go to the
    shared knowledge base at `insights_endpoint` (point it at your own server for full sovereignty; the data that leaves
    is exactly what `preview` shows). Raw inventory never leaves - only the anonymised record.

Why it is the moat: a competitor can fork the AGPL code but not the accumulated hardware -> install-outcome -> failure
dataset; it compounds with every fleet. See site.md "North-star roadmap + the moat".
"""
import json
import threading
import time
import urllib.error
import urllib.request

import db

_SUBMIT_LOCK = threading.Lock()   # serialise uploads so a flush thread + an explicit submit can't double-send

SCHEMA = 1                      # bump only with care - early records must stay comparable for the knowledge base
MODES = ("off", "local", "share")
DEFAULT_MODE = "local"          # local = learn from our OWN fleet, nothing leaves (zero burden, full disclosure).
DEFAULT_WORKER = "https://compat.kwickboot.com"     # hardware-compatibility ingest Worker; records POST to <worker>/ingest
DEFAULT_KNOWN_URL = "https://download.kwickboot.com/compat/known.json"   # CDN-served manifest of what the network knows
USER_AGENT = "KwickBoot-compat/1 (+https://kwickboot.com)"    # a real UA - default python-urllib UA is bot-blocked at the edge


def known_url():
    return db.get_setting("compat_known_url", DEFAULT_KNOWN_URL).strip() or DEFAULT_KNOWN_URL


def refresh_known(max_age=43200):
    """Pull the network's growing known-signature manifest from the CDN (no reinstall needed - it updates live).
    Best-effort + cached ~12h; a stale or missing cache just means we may re-upload something already known."""
    import ssl
    at = db.get_setting("compat_known_at", "")
    try:
        if at and time.time() - float(at) < max_age:
            return
    except ValueError:
        pass
    req = urllib.request.Request(known_url(), headers={"User-Agent": USER_AGENT}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=20, context=ssl.create_default_context()) as r:
            j = json.loads(r.read().decode())
        db.set_setting("compat_known_sigs", json.dumps(list(j.get("known") or [])))
        db.set_setting("compat_known_at", str(time.time()))
    except Exception:
        pass


def _known_set():
    try:
        return set(json.loads(db.get_setting("compat_known_sigs", "[]")))
    except ValueError:
        return set()


def worker():
    return db.get_setting("insights_worker", DEFAULT_WORKER).strip().rstrip("/") or DEFAULT_WORKER


def _key():
    return db.get_setting("insights_key", "").strip()


def register():
    """Silent, no-sign-up opt-in: fetch a unique per-fleet upload key once. Burden-free - the operator never sees
    this. Returns (keyid, error). The key is low-trust/revocable; trust comes from cross-fleet corroboration."""
    if _key():
        return _key(), None
    import ssl
    body = json.dumps({"fleet": fleet_id()}).encode()
    req = urllib.request.Request(worker() + "/register", data=body,
                                 headers={"Content-Type": "application/json", "User-Agent": USER_AGENT}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20, context=ssl.create_default_context()) as r:
            j = json.loads(r.read().decode())
    except Exception as e:
        return "", f"register failed: {e}"
    kid = (j or {}).get("keyid", "")
    if not kid:
        return "", "register: no key returned"
    db.set_setting("insights_key", kid)
    return kid, None


def init(base):
    db.x("""CREATE TABLE IF NOT EXISTS insights (
        id INTEGER PRIMARY KEY, created TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0, body TEXT NOT NULL)""")
    db.x("CREATE INDEX IF NOT EXISTS insights_sent ON insights(sent, id)")
    # LEAN: one row per distinct hardware+outcome signature. Repeats of known-good hardware / working drivers are
    # counted here but neither stored again nor re-uploaded - we only ever send NEW information.
    db.x("""CREATE TABLE IF NOT EXISTS compat_shared (
        sig TEXT PRIMARY KEY, created TEXT NOT NULL, last TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 1)""")


def _signature(rec):
    """Stable hash of what actually matters for compatibility: hardware class + driver/health + outcome. Excludes
    volatile fields (day, deploy time, fleet id) so identical situations collapse to one record."""
    import hashlib
    hw = rec.get("hw") or {}
    o = rec.get("outcome") or {}
    disks = sorted((d.get("model"), d.get("size"), d.get("smart"), bool(d.get("problem"))) for d in hw.get("disks") or [])
    salient = [hw.get("vendor"), hw.get("product"), hw.get("cpu_level"), hw.get("ram"), hw.get("boot"),
               hw.get("secure_boot"), hw.get("tpm"), hw.get("gpu_driver"),
               sorted(hw.get("missing_drivers") or []), sorted(hw.get("health_flags") or []), disks,
               o.get("os"), o.get("ok"), o.get("error_class"), o.get("preflight")]
    return hashlib.sha1(json.dumps(salient, sort_keys=True, default=str).encode()).hexdigest()


def mode():
    m = db.get_setting("insights_mode", DEFAULT_MODE)
    return m if m in MODES else DEFAULT_MODE


def set_mode(m):
    """Operator opt-in/out. 'share' silently registers an upload key the first time - no sign-up, no prompt."""
    if m not in MODES:
        return False, f"mode must be one of {MODES}"
    db.set_setting("insights_mode", m)
    if m == "share":
        register()                      # best-effort; submit_pending also ensures a key before its first upload
    return True, None


def fleet_id():
    """A random, non-identifying id so the shared knowledge base can count distinct fleets without knowing who they are.
    Created once, never derived from anything about the organisation or its hardware."""
    fid = db.get_setting("insights_id", "")
    if not fid:
        import secrets
        fid = secrets.token_hex(8)
        db.set_setting("insights_id", fid)
    return fid


# ---------------------------------------------------------------- anonymisation (at source, before store or send)
def _ram_bucket(gb):
    if not gb:
        return None
    for b in (2, 4, 8, 16, 32, 64, 128):
        if gb <= b:
            return f"<={b}GB"
    return ">128GB"


def _disk_bucket(gb):
    if not gb:
        return None
    for b in (16, 32, 64, 128, 256, 512, 1024, 2048, 4096):
        if gb <= b:
            return f"<={b}GB"
    return ">4TB"


def _error_class(err):
    """Map a raw error to a coarse class - never store the raw text (it can carry paths/serials)."""
    e = (err or "").lower()
    if not e:
        return None
    for key, cls in (("checksum", "checksum_mismatch"), ("mismatch", "checksum_mismatch"), ("download", "download_failed"),
                     ("not signed", "repo_unsigned"), ("gpgv", "repo_unsigned"), ("no space", "disk_too_small"),
                     ("too small", "disk_too_small"), ("timeout", "timeout"), ("timed out", "timeout"),
                     ("firstboot", "firstboot_failed"), ("first boot", "firstboot_failed"), ("driver", "driver"),
                     ("debootstrap", "bootstrap_failed"), ("mount", "mount_failed"), ("efi", "efi_boot")):
        if key in e:
            return cls
    return "other"


def anonymise(facts):
    """Keep hardware MODEL/CLASS + health only. Strip every identifier. Reuses ai._redact for serial/MAC scrubbing."""
    import ai
    f = ai._redact(facts, local=False)      # drops disk serials, masks MAC-shaped strings anywhere
    mac_re = __import__("re").compile(r"\b([0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}\b")

    def clean(s):                            # defence in depth on free-text lists (flags can embed a serial/MAC)
        return mac_re.sub("[mac]", str(s)) if s is not None else s
    disks = [{"kind": d.get("kind"), "size": _disk_bucket(d.get("gb")), "model": clean(d.get("model")),
              "smart": d.get("smart"), "problem": bool(d.get("problems"))} for d in f.get("disks", [])]
    return {
        "vendor": f.get("vendor"), "product": f.get("product"),      # DMI model names - shared by whole production runs
        "cpu": f.get("cpu"), "cpu_level": f.get("cpu_level"), "threads": f.get("threads"),
        "ram": _ram_bucket(f.get("ram_gb")), "boot": f.get("boot"), "secure_boot": f.get("secure_boot"),
        "tpm": f.get("tpm"), "virt": f.get("virt"), "gpu": [clean(g) for g in (f.get("gpu") or [])],
        "gpu_driver": f.get("gpu_driver"), "nics": f.get("nics"), "disks": disks,
        "battery_wear": f.get("battery_wear"), "missing_drivers": [clean(m) for m in (f.get("missing_drivers") or [])],
        "health_flags": [clean(x) for x in (f.get("health_flags") or [])],
        # deliberately NOT included: disk/board serials, dev paths, 'installed' (prior OS on disk), hostnames, IPs, dept.
    }


# ---------------------------------------------------------------- recording an install outcome
def build_record(mac, job, facts):
    """The exact anonymised record for one install. Used both to store and (via preview) to disclose."""
    params = {}
    try:
        params = json.loads(job.get("params") or "{}")
    except (ValueError, AttributeError):
        pass
    deploy_s = None
    try:
        if job.get("started") and job.get("finished"):
            import datetime
            fmt = "%Y-%m-%d %H:%M:%S"
            deploy_s = int((datetime.datetime.strptime(job["finished"], fmt) - datetime.datetime.strptime(job["started"], fmt)).total_seconds())
    except (ValueError, TypeError):
        pass
    rec = {
        "schema": SCHEMA, "fleet": fleet_id(), "day": time.strftime("%Y-%m-%d"),   # coarse date only, never the exact time
        "hw": anonymise(facts),
        "outcome": {"os": params.get("OS") or (job.get("type")), "ok": job.get("rc") == 0,
                    "error_class": _error_class(job.get("error")) if job.get("rc") not in (0, None) else None,
                    "deploy_s": deploy_s, "preflight": facts.get("preflight")},
    }
    rec["sig"] = _signature(rec)             # opaque hash of hw+outcome; drives lean dedupe + the shared known-list
    return rec


def record_install(mac, jid):
    """Called when an install job finishes. No-op unless collection is on. Anonymises, then stores locally (queued)."""
    if mode() == "off":
        return
    try:
        import advisor
        job = db.q1("SELECT * FROM jobs WHERE id=?", jid)
        if not job or job["type"] != "install":
            return
        inv = db.latest_report(mac, "inventory")[0] or ""     # latest_report returns (text, created)
        pf = db.latest_report(mac, "preflight")[0] or ""
        rec = build_record(mac, dict(job), advisor.facts(inv, pf))
        sig = rec["sig"]
        if db.q1("SELECT sig FROM compat_shared WHERE sig=?", sig):
            db.x("UPDATE compat_shared SET count=count+1, last=? WHERE sig=?", db.now(), sig)
            return                           # LEAN: known hardware/outcome already captured - nothing new to send
        db.x("INSERT INTO compat_shared(sig, created, last, count) VALUES(?,?,?,1)", sig, db.now(), db.now())
        db.x("INSERT INTO insights(created, sent, body) VALUES(?,?,?)", db.now(), 0, json.dumps(rec))
        flush_async()                        # opt-in share only; best-effort, off the install critical path
    except Exception as e:                   # insights must never break an install
        try:
            db.audit("insights", f"record skipped: {e}", "")
        except Exception:
            pass


def preview(mac):
    """Full disclosure: the exact record that WOULD be made for this machine's latest install (or hardware if none)."""
    import advisor
    inv = db.latest_report(mac, "inventory")[0] or ""     # latest_report returns (text, created)
    pf = db.latest_report(mac, "preflight")[0] or ""
    job = db.q1("SELECT * FROM jobs WHERE mac=? AND type='install' ORDER BY created DESC LIMIT 1", mac)
    facts = advisor.facts(inv, pf)
    if job:
        return build_record(mac, dict(job), facts)
    return {"schema": SCHEMA, "fleet": fleet_id(), "day": time.strftime("%Y-%m-%d"), "hw": anonymise(facts), "outcome": None}


# ---------------------------------------------------------------- sharing (opt-in) + consuming the knowledge base
def _post_ingest(rows):
    """POST one batch to <worker>/ingest with the auto key. Returns (http_status, error). Best-effort; the caller
    never blocks an install on this. A 401 means our key was revoked -> caller clears it and re-registers once."""
    import ssl
    kid = _key()
    if not kid:
        kid, err = register()
        if err:
            return 0, err
    payload = json.dumps({"schema": SCHEMA, "records": [json.loads(r["body"]) for r in rows]}).encode()
    req = urllib.request.Request(worker() + "/ingest", data=payload, method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + kid,
                                          "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30, context=ssl.create_default_context()) as r:
            return r.status, (None if r.status // 100 == 2 else f"ingest returned {r.status}")
    except urllib.error.HTTPError as e:
        return e.code, f"ingest returned {e.code}"
    except Exception as e:
        return 0, f"cannot reach insight worker: {e}"


def flush_async():
    """Fire-and-forget upload of queued records. No-op unless sharing. Never blocks the caller (install path)."""
    if mode() != "share":
        return
    import threading

    def _run():
        try:
            submit_pending()
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()


def submit_pending(limit=200, force=False):
    """Upload queued records the network does NOT already know. Returns (sent, error). Shares automatically only when
    mode is 'share'; force=True is the explicit give-to-get (`pxectl compat pull`) from a local server. LEAN:
    signatures already in the network's known-list are marked done without sending (sent=2), so as that list grows
    every fleet uploads less - we only ever send genuinely new information."""
    if not force and mode() != "share":     # force = explicit give-to-get (`pxectl compat pull`) even in local mode
        return 0, None
    if not _SUBMIT_LOCK.acquire(blocking=False):
        return 0, None                      # another upload already in progress - don't double-send
    try:
        refresh_known()
        known = _known_set()
        rows = db.q("SELECT id, body FROM insights WHERE sent=0 ORDER BY id LIMIT ?", limit)
        if not rows:
            return 0, None
        send, suppress = [], []
        for r in rows:
            try:
                sig = (json.loads(r["body"]) or {}).get("sig")
            except (ValueError, TypeError):
                sig = None
            (suppress if (sig and sig in known) else send).append(r)
        if suppress:
            db.x(f"UPDATE insights SET sent=2 WHERE id IN ({','.join(str(r['id']) for r in suppress)})")
        if not send:
            return 0, None
        status, err = _post_ingest(send)
        if status == 401:                   # key revoked/unknown: drop it, register fresh, try once more
            db.set_setting("insights_key", "")
            status, err = _post_ingest(send)
        if err:
            return 0, err
        db.x(f"UPDATE insights SET sent=1 WHERE id IN ({','.join(str(r['id']) for r in send)})")
        return len(send), None
    finally:
        _SUBMIT_LOCK.release()


def knowledge_for(facts):
    """Consumption: known issues/outcomes for hardware like this, from whatever records this server holds (local +,
    later, a synced copy of the shared base). Feeds the Install Advisor. Starts small, compounds as records accumulate."""
    hw = anonymise(facts)
    models = {d["model"] for d in hw["disks"] if d.get("model")}
    notes = []
    for r in db.q("SELECT body FROM insights ORDER BY id DESC LIMIT 5000"):
        try:
            rec = json.loads(r["body"])
        except ValueError:
            continue
        o = rec.get("outcome") or {}
        rhw = rec.get("hw") or {}
        if o.get("ok") is False and rhw.get("product") and rhw.get("product") == hw.get("product"):
            notes.append({"kind": "model_install_failure", "product": hw["product"], "error": o.get("error_class"), "os": o.get("os")})
        for d in rhw.get("disks", []):
            if d.get("model") in models and (d.get("smart") not in (None, "OK", "PASSED") or d.get("problem")):
                notes.append({"kind": "disk_health", "model": d["model"], "smart": d.get("smart")})
    # de-duplicate
    seen, out = set(), []
    for n in notes:
        k = json.dumps(n, sort_keys=True)
        if k not in seen:
            seen.add(k)
            out.append(n)
    return out[:20]


def stats():
    """For the console/CLI: how much this fleet has collected and shared (transparency)."""
    tot = db.q1("""SELECT COUNT(*) c,
                          SUM(CASE WHEN sent=1 THEN 1 ELSE 0 END) shared,
                          SUM(CASE WHEN sent=2 THEN 1 ELSE 0 END) suppressed FROM insights""") or {}
    return {"mode": mode(), "fleet": fleet_id(), "worker": worker(), "registered": bool(_key()),
            "records": tot.get("c") or 0, "shared": tot.get("shared") or 0,
            "suppressed": tot.get("suppressed") or 0}   # suppressed = already known to the network, not re-sent
