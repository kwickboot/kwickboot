"""KwickBoot single sign-on: OpenID Connect (Microsoft Entra ID, Google Workspace, Okta, Keycloak, ...).
SPDX-License-Identifier: AGPL-3.0-or-later

Flow: authorization code + PKCE (S256) + state + nonce, confidential client.
ID token validation: the token comes straight from the provider's token endpoint over HTTPS with certificate
verification (OIDC Core 3.1.3.7 rule 6 allows TLS server validation instead of the JWS signature here);
iss, aud, azp, exp, iat and nonce are checked. Python's standard library has no RSA, so no signature check is attempted.
Mapping: allowed email domains; Entra group object IDs (or any "groups" claim) -> role and department names.
A user that matches no rule and is not already known gets NO access (unless a default role is set).
Client secrets live in data/sso-secrets.json (0600), never in the database or API responses.
Providers require HTTPS redirect URIs, so SSO is offered on the HTTPS UI port (config "ui_https_port", e.g. 9443).
"""
import base64
import hashlib
import json
import os
import re
import secrets
import time
import urllib.parse
import urllib.request

import db

BASE = None
PENDING = {}            # state -> {provider, verifier, nonce, exp, redirect}
_DISCOVERY = {}         # issuer -> (time, document)
RANKS = ("viewer", "operator", "manager", "admin")


class SSOError(Exception):
    pass


def init(base):
    global BASE
    BASE = base
    cols = {r["name"] for r in db.q("PRAGMA table_info(users)")}
    for c in ("email TEXT", "sso_sub TEXT", "sso_provider TEXT"):
        if c.split()[0] not in cols:
            db.x(f"ALTER TABLE users ADD COLUMN {c}")
    db.x("CREATE UNIQUE INDEX IF NOT EXISTS users_sso ON users(sso_provider, sso_sub)")


def _sfile():
    return os.path.join(BASE, "data", "sso-secrets.json")


def _secrets():
    try:
        with open(_sfile(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(d):
    tmp = _sfile() + ".tmp"
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, _sfile())


# ---------------------------------------------------------------- provider configuration
def providers():
    try:
        return json.loads(db.get_setting("sso_providers", "[]"))
    except ValueError:
        return []


def public_providers():
    return [{"id": p["id"], "name": p["name"]} for p in providers() if p.get("enabled", True)]


def admin_view():
    s = _secrets()
    return [dict(p, has_secret=p["id"] in s) for p in providers()]


PRESETS = {
    "entra": "https://login.microsoftonline.com/{tenant}/v2.0",
    "google": "https://accounts.google.com",
}


def save_provider(p, secret=None):
    pid = str(p.get("id") or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9-]{2,30}", pid):
        raise SSOError("id: 2-30 lowercase letters, digits, '-'")
    issuer = str(p.get("issuer", "")).strip().rstrip("/")
    if not re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d{1,5})?(/[A-Za-z0-9._~/-]*)?", issuer):
        raise SSOError("issuer must be an https URL (Entra: https://login.microsoftonline.com/<tenant-id>/v2.0, Google: https://accounts.google.com)")
    if not re.fullmatch(r"[\w.-]{4,200}", str(p.get("client_id", ""))):
        raise SSOError("client ID missing")
    domains = [d.strip().lower() for d in p.get("domains", []) if d.strip()]
    if any(not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", d) for d in domains):
        raise SSOError("email domains like school.edu")
    rules = []
    for r in p.get("group_rules", []):
        if r.get("role") not in RANKS:
            raise SSOError("group rule role: viewer, operator, manager or admin")
        rules.append({"group": str(r.get("group", "")).strip()[:200], "role": r["role"],
                      "departments": [str(x).strip() for x in r.get("departments", []) if str(x).strip()]})
    default = p.get("default_role") or ""
    if default and default not in ("viewer", "operator"):
        raise SSOError("default role for unmatched users: none, viewer or operator (never higher)")
    cfg = {"id": pid, "name": str(p.get("name") or pid)[:60], "issuer": issuer, "client_id": p["client_id"],
           "domains": domains, "group_rules": rules, "default_role": default, "enabled": bool(p.get("enabled", True))}
    lst = [x for x in providers() if x["id"] != pid] + [cfg]
    db.set_setting("sso_providers", json.dumps(lst))
    if secret:
        s = _secrets()
        s[pid] = secret
        _save(s)
    return cfg


def delete_provider(pid):
    db.set_setting("sso_providers", json.dumps([x for x in providers() if x["id"] != pid]))
    s = _secrets()
    s.pop(pid, None)
    _save(s)


def _get(pid):
    p = next((x for x in providers() if x["id"] == pid and x.get("enabled", True)), None)
    if not p:
        raise SSOError("unknown sign-in provider")
    return p


def _http_json(url, data=None):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode() if data else None,
                                 headers={"Accept": "application/json", **({"Content-Type": "application/x-www-form-urlencoded"} if data else {})})
    import ssl
    ctx = ssl.create_default_context(cafile=os.environ.get("KB_SSO_CAFILE") or None)   # extra CA only for test IdPs
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as r:   # CA verification + hostname check always on
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read()[:300].decode(errors="replace")
        raise SSOError(f"provider returned {e.code}: {body}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise SSOError(f"cannot reach provider: {e}")


def discovery(issuer):
    c = _DISCOVERY.get(issuer)
    if c and time.time() - c[0] < 3600:
        return c[1]
    d = _http_json(issuer + "/.well-known/openid-configuration")
    if d.get("issuer", "").rstrip("/") != issuer and "{tenantid}" not in d.get("issuer", ""):
        raise SSOError("discovery document issuer does not match")
    for k in ("authorization_endpoint", "token_endpoint"):
        if not str(d.get(k, "")).startswith("https://"):
            raise SSOError(f"discovery: {k} must be https")
    _DISCOVERY[issuer] = (time.time(), d)
    return d


# ---------------------------------------------------------------- login flow
def start(pid, redirect_uri):
    p = _get(pid)
    d = discovery(p["issuer"])
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    for k in [k for k, v in PENDING.items() if v["exp"] < time.time()]:
        PENDING.pop(k, None)
    PENDING[state] = {"provider": pid, "verifier": verifier, "nonce": nonce, "exp": time.time() + 600, "redirect": redirect_uri}
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    q = {"response_type": "code", "client_id": p["client_id"], "redirect_uri": redirect_uri, "scope": "openid email profile",
         "state": state, "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256", "prompt": "select_account"}
    return d["authorization_endpoint"] + "?" + urllib.parse.urlencode(q)


def _b64json(seg):
    return json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))


def finish(state, code):
    st = PENDING.pop(state or "", None)
    if not st or st["exp"] < time.time():
        raise SSOError("sign-in expired or was not started here - try again")
    p = _get(st["provider"])
    secret = _secrets().get(p["id"])
    if not secret:
        raise SSOError("provider has no client secret configured")
    d = discovery(p["issuer"])
    tok = _http_json(d["token_endpoint"], {"grant_type": "authorization_code", "code": code, "redirect_uri": st["redirect"],
                                           "client_id": p["client_id"], "client_secret": secret, "code_verifier": st["verifier"]})
    idt = tok.get("id_token", "")
    parts = idt.split(".")
    if len(parts) != 3:
        raise SSOError("provider sent no ID token")
    c = _b64json(parts[1])
    now = time.time()
    iss_ok = c.get("iss", "").rstrip("/") == p["issuer"] or (
        "login.microsoftonline.com" in p["issuer"] and c.get("iss", "").rstrip("/") == p["issuer"])
    aud = c.get("aud")
    if not iss_ok:
        raise SSOError("ID token issuer mismatch")
    if not (aud == p["client_id"] or (isinstance(aud, list) and p["client_id"] in aud)):
        raise SSOError("ID token audience mismatch")
    if isinstance(aud, list) and len(aud) > 1 and c.get("azp") != p["client_id"]:
        raise SSOError("ID token azp mismatch")
    if not (c.get("exp", 0) > now - 60 and c.get("iat", now) < now + 300):
        raise SSOError("ID token expired or not yet valid")
    if c.get("nonce") != st["nonce"]:
        raise SSOError("ID token nonce mismatch")
    return p, c


def resolve(p, claims):
    """Map claims to (role, [department names]) or raise if the user gets no access."""
    email = (claims.get("email") or claims.get("preferred_username") or claims.get("upn") or "").lower()
    if claims.get("email_verified") is False:
        raise SSOError("the provider says this email address is not verified")
    if p["domains"] and email.split("@")[-1] not in p["domains"]:
        raise SSOError(f"{email or 'this account'} is not in an allowed domain")
    groups = set(map(str, claims.get("groups") or []))
    role, depts = None, set()
    for r in p["group_rules"]:
        if r["group"] == "*" or r["group"] in groups:
            if role is None or RANKS.index(r["role"]) > RANKS.index(role):
                role = r["role"]
            depts.update(r["departments"])
    return email, role, sorted(depts)


def login_user(p, claims):
    """Find or create the local user for these claims. Returns the users row."""
    email, role, depts = resolve(p, claims)
    sub = str(claims.get("sub") or "")
    if not sub:
        raise SSOError("ID token has no subject")
    u = db.q1("SELECT * FROM users WHERE sso_provider=? AND sso_sub=?", p["id"], sub)
    if not u and email:          # admin pre-created this person: link on first sign-in by verified email
        u = db.q1("SELECT * FROM users WHERE lower(email)=? AND sso_sub IS NULL", email)
        if u:
            db.x("UPDATE users SET sso_provider=?, sso_sub=? WHERE id=?", p["id"], sub, u["id"])
    if u:
        if not u["enabled"]:
            raise SSOError("this account is disabled in KwickBoot")
        if role:                  # group rules are authoritative when they match
            db.x("UPDATE users SET role=? WHERE id=?", role, u["id"])
            names = {d["name"]: d["id"] for d in db.departments()}
            db.set_user_departments(u["id"], [names[n] for n in depts if n in names])
        return db.q1("SELECT * FROM users WHERE id=?", u["id"])
    role = role or p.get("default_role")
    if not role:
        raise SSOError(f"{email} has no KwickBoot access yet - ask an admin to add you or to map your group")
    base = re.sub(r"[^a-z0-9._-]", "", (email.split("@")[0] or "user").lower())[:30] or "user"
    uname, i = base, 1
    while db.q1("SELECT 1 FROM users WHERE username=?", uname):
        i += 1
        uname = f"{base}{i}"
    db.x("INSERT INTO users(username, display, role, created, email, sso_provider, sso_sub) VALUES(?,?,?,?,?,?,?)",
         uname, str(claims.get("name") or "")[:80], role, db.now(), email, p["id"], sub)
    u = db.q1("SELECT * FROM users WHERE username=?", uname)
    names = {d["name"]: d["id"] for d in db.departments()}
    db.set_user_departments(u["id"], [names[n] for n in depts if n in names])
    return u
