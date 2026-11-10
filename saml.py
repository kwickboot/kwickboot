"""KwickBoot SAML 2.0 Web SSO (SP-initiated) - for IdPs that speak SAML rather than OpenID Connect.
SPDX-License-Identifier: AGPL-3.0-or-later

Flow: SP-initiated. AuthnRequest goes to the IdP over the HTTP-Redirect binding (DEFLATE + base64). The IdP posts the
Response back to the Assertion Consumer Service (ACS) over the HTTP-POST binding. We verify the XML signature with
libxmlsec (python3-xmlsec) against the IdP's configured X.509 certificate - no hand-rolled crypto, no canonicalisation
by hand. We then enforce the SAML Web-SSO rules ourselves: status Success, issuer, Conditions (NotBefore/NotOnOrAfter),
AudienceRestriction, SubjectConfirmationData (Recipient + NotOnOrAfter + InResponseTo), and a single signed assertion -
with explicit signature-wrapping defences (claims are read only from the element the signature actually covers).

Mapping and provisioning match sso.py (allowed email domains; attribute/group value -> role + department names; a user
that matches no rule and is not already known gets no access unless a default role is set). The IdP certificate is public;
there is no SP signing key to store (AuthnRequests are sent unsigned, which IdPs accept for Web SSO).
"""
import base64
import re
import time
import urllib.parse
import zlib

import db

try:
    import xmlsec
    from lxml import etree
    HAVE_LIB = True
except ImportError:                      # server without python3-xmlsec/lxml: SAML stays off, OIDC still works
    HAVE_LIB = False

RANKS = ("viewer", "operator", "manager", "admin")
PENDING = {}                             # request_id -> {provider, exp, relay}
SKEW = 120                               # clock skew allowance, seconds
NS = {"samlp": "urn:oasis:names:tc:SAML:2.0:protocol", "saml": "urn:oasis:names:tc:SAML:2.0:assertion"}


class SAMLError(Exception):
    pass


def available():
    return HAVE_LIB


# ---------------------------------------------------------------- provider configuration
def providers():
    try:
        import json
        return json.loads(db.get_setting("saml_providers", "[]"))
    except ValueError:
        return []


def public_providers():
    return [{"id": p["id"], "name": p["name"]} for p in providers() if p.get("enabled", True)]


def admin_view():
    return [dict(p) for p in providers()]


def _clean_cert(pem):
    """Normalise a pasted IdP certificate to PEM; accept bare base64 (no header) too."""
    pem = (pem or "").strip()
    if not pem:
        raise SAMLError("IdP signing certificate (X.509 PEM) required")
    if "BEGIN CERTIFICATE" not in pem:
        b = re.sub(r"\s+", "", pem)
        if not re.fullmatch(r"[A-Za-z0-9+/=]+", b):
            raise SAMLError("certificate must be PEM or base64")
        pem = "-----BEGIN CERTIFICATE-----\n" + "\n".join(b[i:i + 64] for i in range(0, len(b), 64)) + "\n-----END CERTIFICATE-----\n"
    return pem


def save_provider(p):
    import json
    pid = str(p.get("id") or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9-]{2,30}", pid):
        raise SAMLError("id: 2-30 lowercase letters, digits, '-'")
    entity = str(p.get("idp_entity_id", "")).strip()
    if not entity:
        raise SAMLError("IdP entity ID (issuer) required - from the IdP metadata")
    sso_url = str(p.get("idp_sso_url", "")).strip()
    if not re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d{1,5})?(/[^\s]*)?", sso_url):
        raise SAMLError("IdP SSO URL must be an https URL (HTTP-Redirect binding)")
    cert = _clean_cert(p.get("idp_x509"))
    sp_entity = str(p.get("sp_entity_id") or "").strip() or "kwickboot"
    domains = [d.strip().lower() for d in p.get("domains", []) if d.strip()]
    if any(not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", d) for d in domains):
        raise SAMLError("email domains like school.edu")
    rules = []
    for r in p.get("group_rules", []):
        if r.get("role") not in RANKS:
            raise SAMLError("group rule role: viewer, operator, manager or admin")
        rules.append({"group": str(r.get("group", "")).strip()[:200], "role": r["role"],
                      "departments": [str(x).strip() for x in r.get("departments", []) if str(x).strip()]})
    default = p.get("default_role") or ""
    if default and default not in ("viewer", "operator"):
        raise SAMLError("default role for unmatched users: none, viewer or operator (never higher)")
    group_attr = str(p.get("group_attribute") or "").strip()[:100]   # SAML attribute carrying groups/roles (e.g. "groups", "http://schemas.xmlsoap.org/claims/Group")
    cfg = {"id": pid, "name": str(p.get("name") or pid)[:60], "idp_entity_id": entity, "idp_sso_url": sso_url,
           "idp_x509": cert, "sp_entity_id": sp_entity, "domains": domains, "group_rules": rules,
           "group_attribute": group_attr, "default_role": default, "enabled": bool(p.get("enabled", True))}
    lst = [x for x in providers() if x["id"] != pid] + [cfg]
    db.set_setting("saml_providers", json.dumps(lst))
    return cfg


def delete_provider(pid):
    import json
    db.set_setting("saml_providers", json.dumps([x for x in providers() if x["id"] != pid]))


def _get(pid):
    p = next((x for x in providers() if x["id"] == pid and x.get("enabled", True)), None)
    if not p:
        raise SAMLError("unknown sign-in provider")
    return p


# ---------------------------------------------------------------- login flow
def start(pid, acs_url):
    if not HAVE_LIB:
        raise SAMLError("SAML support is not installed on this server")
    p = _get(pid)
    rid = "_" + base64.b32encode(_rand(20)).decode().strip("=").lower()
    now = _utc()
    req = (
        '<samlp:AuthnRequest xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol"'
        ' xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion"'
        f' ID="{rid}" Version="2.0" IssueInstant="{now}"'
        ' ProtocolBinding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"'
        f' AssertionConsumerServiceURL="{_xml(acs_url)}" Destination="{_xml(p["idp_sso_url"])}">'
        f'<saml:Issuer>{_xml(p["sp_entity_id"])}</saml:Issuer>'
        '<samlp:NameIDPolicy Format="urn:oasis:names:tc:SAML:2.0:nameid-format:persistent" AllowCreate="true"/>'
        '</samlp:AuthnRequest>')
    for k in [k for k, v in PENDING.items() if v["exp"] < time.time()]:
        PENDING.pop(k, None)
    PENDING[rid] = {"provider": pid, "exp": time.time() + 600, "acs": acs_url}
    deflated = zlib.compress(req.encode())[2:-4]           # raw DEFLATE (strip zlib header/adler) for the Redirect binding
    q = {"SAMLRequest": base64.b64encode(deflated).decode(), "RelayState": pid}
    sep = "&" if "?" in p["idp_sso_url"] else "?"
    return p["idp_sso_url"] + sep + urllib.parse.urlencode(q)


def acs(saml_response_b64, acs_url):
    """Handle the IdP's POSTed Response. Returns (provider, claims) or raises SAMLError."""
    if not HAVE_LIB:
        raise SAMLError("SAML support is not installed on this server")
    try:
        xml = base64.b64decode(saml_response_b64 or "", validate=False)
    except Exception:
        raise SAMLError("malformed SAMLResponse")
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)   # no DTD/entity/network: block XXE
    try:
        root = etree.fromstring(xml, parser)
    except etree.XMLSyntaxError as e:
        raise SAMLError(f"cannot parse SAMLResponse: {e}")
    if root.tag != "{%s}Response" % NS["samlp"]:
        raise SAMLError("not a SAML Response")

    # top-level status must be Success before we trust anything else
    status = root.find("./samlp:Status/samlp:StatusCode", NS)
    if status is None or not status.get("Value", "").endswith(":Success"):
        sub = root.find(".//samlp:StatusCode/samlp:StatusCode", NS)
        raise SAMLError("IdP refused the sign-in" + (f" ({sub.get('Value')})" if sub is not None else ""))

    rid = root.get("InResponseTo", "")
    st = PENDING.pop(rid, None)
    if not st or st["exp"] < time.time():
        raise SAMLError("sign-in expired or was not started here - try again")
    p = _get(st["provider"])

    signed = _verify_and_select(root, p["idp_x509"])        # xmlsec verifies; returns the covered element (Response or Assertion)
    assertion = _single_assertion(root, signed)
    claims = _validate_assertion(assertion, p, acs_url, rid)
    return p, claims


def _verify_and_select(root, cert_pem):
    """Verify the XML signature with xmlsec against the pinned IdP cert; return the element the signature covers.
    Accept a signature on the Response or on the single Assertion. Reject anything unsigned."""
    xmlsec.tree.add_ids(root, ["ID"])                        # let URI="#id" references resolve to SAML ID attributes
    key = xmlsec.Key.from_memory(cert_pem.encode(), xmlsec.constants.KeyDataFormatCertPem, None)
    # candidate signed elements: direct child Signature of Response, or of an Assertion
    candidates = []
    for el in [root] + root.findall("./saml:Assertion", NS):
        sig = el.find("./{http://www.w3.org/2000/09/xmldsig#}Signature")
        if sig is not None:
            candidates.append((el, sig))
    if not candidates:
        raise SAMLError("the SAML Response is not signed")
    for el, sig in candidates:
        ref = sig.find('.//{http://www.w3.org/2000/09/xmldsig#}Reference')
        uri = (ref.get("URI") if ref is not None else "") or ""
        if uri and uri != "#" + (el.get("ID") or ""):       # the signature must cover THIS element, not some other
            raise SAMLError("signature reference does not cover its enclosing element (possible wrapping)")
        ctx = xmlsec.SignatureContext()
        ctx.key = key
        try:
            ctx.verify(sig)
        except xmlsec.VerificationError:
            raise SAMLError("SAML signature verification failed (wrong IdP certificate or tampered response)")
        return el
    raise SAMLError("no verifiable signature found")


def _single_assertion(root, signed):
    """Return the one assertion to read claims from, ensuring it is the element the signature covered
    (or the sole assertion inside a signed Response). Rejects multiple assertions (wrapping)."""
    assertions = root.findall("./saml:Assertion", NS)
    if len(assertions) != 1:
        raise SAMLError("expected exactly one assertion")
    a = assertions[0]
    if signed is not a and signed is not root:
        raise SAMLError("the signed element is neither the Response nor its assertion")
    if signed is root:
        # Response signed: the assertion must be a direct child of the signed Response (not smuggled elsewhere)
        if a.getparent() is not root:
            raise SAMLError("assertion is not a direct child of the signed Response")
    return a


def _validate_assertion(a, p, acs_url, rid):
    now = time.time()
    iss = a.find("./saml:Issuer", NS)
    if iss is None or (iss.text or "").strip() != p["idp_entity_id"]:
        raise SAMLError("assertion issuer does not match the configured IdP")
    # SubjectConfirmation: Recipient = our ACS, InResponseTo = our request, not expired
    scd = a.find("./saml:Subject/saml:SubjectConfirmation/saml:SubjectConfirmationData", NS)
    if scd is None:
        raise SAMLError("assertion has no SubjectConfirmationData")
    if scd.get("Recipient", "") != acs_url:
        raise SAMLError("assertion Recipient does not match this server's ACS URL")
    if scd.get("InResponseTo", "") != rid:
        raise SAMLError("assertion InResponseTo does not match the request")
    if _past(scd.get("NotOnOrAfter"), now, 0):
        raise SAMLError("assertion confirmation has expired")
    # Conditions: time window + audience
    cond = a.find("./saml:Conditions", NS)
    if cond is not None:
        if cond.get("NotBefore") and _dt(cond.get("NotBefore")) - SKEW > now:
            raise SAMLError("assertion not yet valid (check clocks)")
        if _past(cond.get("NotOnOrAfter"), now, SKEW):
            raise SAMLError("assertion has expired")
        auds = [e.text.strip() for e in cond.findall(".//saml:Audience", NS) if e.text]
        if auds and p["sp_entity_id"] not in auds:
            raise SAMLError("assertion audience does not include this service")
    # NameID + attributes
    nid = a.find("./saml:Subject/saml:NameID", NS)
    name_id = (nid.text or "").strip() if nid is not None else ""
    if not name_id:
        raise SAMLError("assertion has no subject NameID")
    attrs = {}
    for at in a.findall("./saml:AttributeStatement/saml:Attribute", NS):
        key = at.get("Name") or at.get("FriendlyName") or ""
        vals = [v.text.strip() for v in at.findall("./saml:AttributeValue", NS) if v.text and v.text.strip()]
        if key:
            attrs[key] = vals
    return {"name_id": name_id, "attrs": attrs}


# ---------------------------------------------------------------- claim mapping + provisioning (mirrors sso.py)
def _first(attrs, *names):
    for n in names:
        for k, v in attrs.items():
            if (k == n or k.lower().endswith(n.lower())) and v:
                return v[0]
    return ""


def resolve(p, claims):
    attrs = claims["attrs"]
    email = (_first(attrs, "email", "emailaddress", "mail") or claims["name_id"]).lower()
    if p["domains"] and ("@" not in email or email.split("@")[-1] not in p["domains"]):
        raise SAMLError(f"{email or 'this account'} is not in an allowed domain")
    ga = p.get("group_attribute")
    groups = set()
    if ga:
        for k, v in attrs.items():
            if k == ga or k.lower().endswith(ga.lower()):
                groups.update(v)
    else:
        for n in ("groups", "role", "roles", "memberof"):
            groups.update(_all(attrs, n))
    role, depts = None, set()
    for r in p["group_rules"]:
        if r["group"] == "*" or r["group"] in groups:
            if role is None or RANKS.index(r["role"]) > RANKS.index(role):
                role = r["role"]
            depts.update(r["departments"])
    name = _first(attrs, "displayname", "name", "givenname") or email.split("@")[0]
    return email, name, role, sorted(depts)


def _all(attrs, name):
    out = []
    for k, v in attrs.items():
        if k == name or k.lower().endswith(name.lower()):
            out.extend(v)
    return out


def login_user(p, claims):
    email, name, role, depts = resolve(p, claims)
    prov = "saml:" + p["id"]
    sub = claims["name_id"]
    u = db.q1("SELECT * FROM users WHERE sso_provider=? AND sso_sub=?", prov, sub)
    if not u and email:
        u = db.q1("SELECT * FROM users WHERE lower(email)=? AND sso_sub IS NULL", email)
        if u:
            db.x("UPDATE users SET sso_provider=?, sso_sub=? WHERE id=?", prov, sub, u["id"])
    if u:
        if not u["enabled"]:
            raise SAMLError("this account is disabled in KwickBoot")
        if role:
            db.x("UPDATE users SET role=? WHERE id=?", role, u["id"])
            names = {d["name"]: d["id"] for d in db.departments()}
            db.set_user_departments(u["id"], [names[n] for n in depts if n in names])
        return db.q1("SELECT * FROM users WHERE id=?", u["id"])
    role = role or p.get("default_role")
    if not role:
        raise SAMLError(f"{email} has no KwickBoot access yet - ask an admin to add you or to map your group")
    base = re.sub(r"[^a-z0-9._-]", "", (email.split("@")[0] or "user").lower())[:30] or "user"
    uname, i = base, 1
    while db.q1("SELECT 1 FROM users WHERE username=?", uname):
        i += 1
        uname = f"{base}{i}"
    db.x("INSERT INTO users(username, display, role, created, email, sso_provider, sso_sub) VALUES(?,?,?,?,?,?,?)",
         uname, name[:80], role, db.now(), email, prov, sub)
    u = db.q1("SELECT * FROM users WHERE username=?", uname)
    names = {d["name"]: d["id"] for d in db.departments()}
    db.set_user_departments(u["id"], [names[n] for n in depts if n in names])
    return u


# ---------------------------------------------------------------- small helpers
def _rand(n):
    import os
    return os.urandom(n)


def _utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _dt(s):
    s = (s or "").strip().replace("Z", "+0000")
    for f in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            import calendar
            import datetime
            return calendar.timegm(datetime.datetime.strptime(s, f).utctimetuple())
        except ValueError:
            continue
    return 0


def _past(s, now, skew):
    t = _dt(s)
    return bool(t) and t + skew < now


def _xml(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
