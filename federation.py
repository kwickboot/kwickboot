"""KwickBoot federation: one hub (identity, single UI) + many nodes (local PXE, local database, local enforcement).
SPDX-License-Identifier: AGPL-3.0-or-later

Node side
- `pxectl node-token` creates the node's federation secret (shown once) and prints the TLS fingerprint.
- The node listens for the hub on HTTPS (config "federation_port", default 8443) with a self-signed certificate.
- Every hub request carries a signed identity assertion: user, role, department NAMES, timestamp, nonce.
  The node verifies HMAC-SHA256, a 120 s clock window and nonce reuse, then applies its OWN policies, mapping
  department names to its local departments. The hub can never act beyond what the node would allow that role.
Hub side
- Admin adds a node: name, https URL, secret, expected certificate fingerprint (from `pxectl node-info`).
- The UI talks to a node through /api/fed/proxy?node=<id>&path=/api/... ; the hub signs and forwards.
Secrets live in data/fed-secrets.json (0600), never in the database or API responses.
"""
import base64
import hashlib
import hmac
import http.client
import json
import os
import secrets
import ssl
import subprocess
import threading
import time

import db

BASE = None
_nonces = {}
_lock = threading.Lock()
WINDOW = 120


def init(base):
    global BASE
    BASE = base
    db.x("""CREATE TABLE IF NOT EXISTS nodes (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, url TEXT NOT NULL,
            pin TEXT NOT NULL, created TEXT NOT NULL, last_ok TEXT, last_error TEXT)""")


def _sfile():
    return os.path.join(BASE, "data", "fed-secrets.json")


def _load():
    try:
        with open(_sfile(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _store(d):
    tmp = _sfile() + ".tmp"
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, _sfile())


# ---------------------------------------------------------------- node: certificate + secret
def cert_paths():
    return os.path.join(BASE, "data", "fed.crt"), os.path.join(BASE, "data", "fed.key")


def ensure_cert(cn):
    crt, key = cert_paths()
    if not (os.path.exists(crt) and os.path.exists(key)):
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:3072", "-nodes", "-keyout", key, "-out", crt, "-days", "3650",
                        "-subj", f"/CN=kwickboot-node-{cn}"], check=True, capture_output=True)
        os.chmod(key, 0o600)
    return crt, key


def fingerprint():
    crt, _ = cert_paths()
    with open(crt, encoding="ascii") as f:
        der = ssl.PEM_cert_to_DER_cert(f.read())
    return hashlib.sha256(der).hexdigest()


def new_node_secret():
    s = secrets.token_urlsafe(32)
    with _lock:
        d = _load()
        d["node_secret"] = s
        _store(d)
    return s


def node_enabled():
    return bool(_load().get("node_secret"))


def _body_hash(body):
    return hashlib.sha256(body or b"").hexdigest()


def _sign(secret, method, path, ident_b64, body):
    msg = "\n".join((method, path, ident_b64, _body_hash(body))).encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def verify(method, path, headers, body):
    """Node side. Returns a virtual user dict, or raises ValueError."""
    secret = _load().get("node_secret")
    if not secret:
        raise ValueError("federation not enabled on this node")
    ident_b64, sig = headers.get("X-KB-Fed-Ident", ""), headers.get("X-KB-Fed-Sig", "")
    if not hmac.compare_digest(_sign(secret, method, path, ident_b64, body), sig):
        raise ValueError("bad federation signature")
    ident = json.loads(base64.b64decode(ident_b64))
    now = time.time()
    if abs(now - float(ident["ts"])) > WINDOW:
        raise ValueError("federation request too old (check clocks)")
    with _lock:
        for k in [k for k, t in _nonces.items() if now - t > 2 * WINDOW]:
            _nonces.pop(k, None)
        if ident["nonce"] in _nonces:
            raise ValueError("replayed federation request")
        _nonces[ident["nonce"]] = now
    if ident.get("role") not in ("admin", "manager", "operator", "viewer"):
        raise ValueError("bad role in assertion")
    names = {d["name"]: d["id"] for d in db.departments()}
    return {"id": None, "username": f"{ident['user']}@{ident.get('hub', 'hub')}", "role": ident["role"], "enabled": 1,
            "must_change_pw": 0, "federated": True, "dept_ids": [names[n] for n in ident.get("departments", []) if n in names]}


# ---------------------------------------------------------------- hub: nodes + signed forwarding
def nodes():
    return db.q("SELECT id, name, url, pin, created, last_ok, last_error FROM nodes ORDER BY name")


def add_node(name, url, secret, pin):
    import re
    if not re.fullmatch(r"[\w .-]{2,40}", name or ""):
        raise ValueError("node name: 2-40 letters, digits, space . _ -")
    if not re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d{1,5})?/?", url or ""):
        raise ValueError("node URL: https://host:port")
    pin = (pin or "").replace(":", "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", pin):
        raise ValueError("certificate fingerprint: 64 hex characters (from `pxectl node-info` on the node)")
    if len(secret or "") < 20:
        raise ValueError("join secret missing (from `pxectl node-token` on the node)")
    db.x("INSERT INTO nodes(name, url, pin, created) VALUES(?,?,?,?)", name, url.rstrip("/"), pin, db.now())
    nid = db.q1("SELECT id FROM nodes WHERE name=?", name)["id"]
    with _lock:
        d = _load()
        d.setdefault("nodes", {})[str(nid)] = secret
        _store(d)
    return nid


def remove_node(nid):
    db.x("DELETE FROM nodes WHERE id=?", nid)
    with _lock:
        d = _load()
        d.get("nodes", {}).pop(str(nid), None)
        _store(d)


def forward(nid, method, path, body, user, dept_names, hub_name):
    """Hub side: signed request to a node. Returns (status, bytes)."""
    n = db.q1("SELECT * FROM nodes WHERE id=?", nid)
    secret = _load().get("nodes", {}).get(str(nid))
    if not n or not secret:
        return 404, json.dumps({"error": "unknown node"}).encode()
    ident = base64.b64encode(json.dumps({"user": user["username"], "role": user["role"], "departments": dept_names,
                                         "ts": time.time(), "nonce": secrets.token_hex(12), "hub": hub_name}).encode()).decode()
    host_port = n["url"][len("https://"):]
    host, _, port = host_port.partition(":")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE              # verified by the pinned fingerprint below
    conn = http.client.HTTPSConnection(host, int(port or 443), timeout=20, context=ctx)
    try:
        conn.connect()
        fp = hashlib.sha256(conn.sock.getpeercert(binary_form=True)).hexdigest()
        if not hmac.compare_digest(fp, n["pin"]):
            raise ValueError("node certificate does not match the pinned fingerprint - refusing")
        conn.request(method, path, body=body or None, headers={
            "X-KB-Fed-Ident": ident, "X-KB-Fed-Sig": _sign(secret, method, path, ident, body),
            "Content-Type": "application/json", "Content-Length": str(len(body or b""))})
        r = conn.getresponse()
        data = r.read()
        db.x("UPDATE nodes SET last_ok=?, last_error=NULL WHERE id=?", db.now(), nid)
        return r.status, data
    except (OSError, ssl.SSLError, ValueError) as e:
        db.x("UPDATE nodes SET last_error=? WHERE id=?", f"{db.now()} {e}"[:300], nid)
        return 502, json.dumps({"error": f"node {n['name']} unreachable: {e}"}).encode()
    finally:
        conn.close()
