"""ai.py - the KwickBoot AI pack, server side: the customer's own AI service reasons over a machine's real hardware.
SPDX-License-Identifier: AGPL-3.0-or-later

Providers are configured by an admin (Settings -> AI services); nothing is preset or shared with KwickBoot:
  - "anthropic": Claude through the official Anthropic Python SDK (installed hash-locked in venv-ai/)
  - "openai":    any OpenAI-compatible chat endpoint - local AI (Ollama, LM Studio, vLLM, llama.cpp server, LocalAI)
                 or another cloud service
API keys stay on the server (settings table), are never sent to browsers, the RAM OS or the logs.
The model PROPOSES: it may only pick systems from the department's catalog and disks the machine really has; every
proposal is validated here, merged with the rule layer's facts, and only pre-fills the install form (all checks apply).
It warns, it never refuses - the user makes the choice.
Privacy: for providers not marked "local", serial numbers and MAC addresses are removed before sending.
"""
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.request

import db

KINDS = ("anthropic", "openai")
FALLBACK_MODELS = ("claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5")   # server-side refusal fallback
VENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "venv-ai", "lib",
                    f"python{sys.version_info.major}.{sys.version_info.minor}", "site-packages")

SYSTEM = """You are the install advisor of KwickBoot, a network boot server that installs operating systems on PCs, \
servers and appliances. You receive the real hardware facts of ONE machine (read by KwickBoot's RAM OS with nothing \
mounted), the catalog of systems this department may install (with notes and minimum disk sizes), the deterministic \
rule-based advice, and what the operator wants the machine for.

Propose the best install for this exact machine and purpose:
- Pick systems ONLY by their catalog "id", and disks ONLY by a "dev" from the facts.
- Ground every reason in a specific fact (RAM, CPU level, disk type/health, GPU driver, NICs, firmware, TPM...).
- Warn, never refuse. The operator decides. Things like Windows 11 on unsupported hardware, low RAM, an old or \
  overclocked CPU, or a disk with SMART warnings are allowed: say clearly what the risk is and how to reduce it. \
  Only something physically impossible (the system does not fit on any disk) is not offered.
- Prefer a healthy internal disk; never choose a USB disk unless nothing else fits, and say so.
- If an existing system is on a disk, say that installing erases it.
- Be concise and concrete; no marketing. Plain language an IT technician understands.
- "questions": short things the operator should check or decide (empty if none)."""

PLAN = {"type": "object",
        "properties": {"os": {"type": "string"}, "disk": {"type": "string"},
                       "reasons": {"type": "array", "items": {"type": "string"}},
                       "caveats": {"type": "array", "items": {"type": "string"}}},
        "required": ["os", "disk", "reasons", "caveats"], "additionalProperties": False}
SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "recommended": PLAN,
        "alternatives": {"type": "array", "items": PLAN},
        "findings": {"type": "array", "items": {
            "type": "object",
            "properties": {"level": {"type": "string", "enum": ["info", "warn", "bad"]}, "text": {"type": "string"}},
            "required": ["level", "text"], "additionalProperties": False}},
        "questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "recommended", "alternatives", "findings", "questions"],
    "additionalProperties": False,
}


class AIError(Exception):
    pass


# ---------------------------------------------------------------- provider configuration: everyone / department / personal
# scope "system" (admins; everyone may use it), "dept" (owner = department id; managers of that department; its machines),
# "user" (owner = username; that user only - bring your own key or your own local model). Each scope has its own default;
# for a request the most personal default wins: yours -> the machine's department -> everyone's.
def _all():
    try:
        items = json.loads(db.get_setting("ai_providers", "[]") or "[]")
    except ValueError:
        return []
    legacy = db.get_setting("ai_default", "")
    for p in items:                                     # providers saved before scopes existed are system-wide
        p.setdefault("scope", "system")
        p.setdefault("owner", None)
        p.setdefault("default", p["id"] == legacy)
    return items


def is_local(p):
    """A service is local only if its address is on a private network (LAN, loopback, link-local) - checked by the
    server, never just believed from a checkbox. The Claude API (no base URL, or anthropic.com) is never local."""
    import ipaddress
    import socket
    from urllib.parse import urlparse
    if not p.get("local") or not p.get("base_url"):
        return False
    host = urlparse(p["base_url"]).hostname or ""
    if not host or host.endswith("anthropic.com"):
        return False
    try:
        addrs = {a[4][0] for a in socket.getaddrinfo(host, None)}
    except OSError:
        return False
    return bool(addrs) and all(ipaddress.ip_address(a.split("%")[0]).is_private or ipaddress.ip_address(a.split("%")[0]).is_loopback
                               or ipaddress.ip_address(a.split("%")[0]).is_link_local for a in addrs)


def _save_all(items):
    db.set_setting("ai_providers", json.dumps(items))


def _public(p):
    q = {k: v for k, v in p.items()}
    q["key_set"] = bool(db.get_setting("ai_key_" + p["id"]))
    q["local"] = is_local(p)                            # effective: what the server verified, not what was ticked
    return q


def visible(user, udepts, machine_dept=None):
    """Providers this user may see (machine_dept=None) or use for a machine of that department."""
    out = []
    for p in _all():
        if p["scope"] == "system":
            ok = True
        elif p["scope"] == "dept":                       # on a machine: that machine's department; listing: your departments
            ok = p["owner"] == machine_dept if machine_dept is not None else (user["role"] == "admin" or p["owner"] in udepts)
        else:
            ok = p["owner"] == user["username"]
        if ok:
            out.append(_public(p))
    return out


def providers(user=None, udepts=(), machine_dept=None):
    return visible(user, udepts, machine_dept) if user else [_public(p) for p in _all()]


def save_provider(a, user, may):
    """may(scope, owner) -> bool decides who may create/edit at that scope (department policy + personal permission)."""
    kind = a.get("kind")
    if kind not in KINDS:
        raise ValueError("kind: anthropic or openai")
    name = str(a.get("name") or "").strip()[:60]
    model = str(a.get("model") or "").strip()[:120]
    url = str(a.get("base_url") or "").strip().rstrip("/")[:300]
    if not name or not model:
        raise ValueError("name and model are required")
    if not re.fullmatch(r"[A-Za-z0-9._:/@+-]{1,120}", model):
        raise ValueError("model: letters, digits and . _ : / @ + -")
    if kind == "openai" and not url:
        raise ValueError("base URL is required, e.g. http://192.168.0.50:11434/v1 for Ollama")
    if url and not re.fullmatch(r"https?://[A-Za-z0-9.\-\[\]:]+(:\d{1,5})?(/[A-Za-z0-9._~/%-]*)?", url):
        raise ValueError("base URL: http(s)://host[:port][/path]")
    effort = str(a.get("effort") or "")
    if effort not in ("", "low", "medium", "high", "xhigh", "max"):
        raise ValueError("effort: low, medium, high, xhigh or max")
    items = _all()
    pid = str(a.get("id") or "")
    cur = next((p for p in items if p["id"] == pid), None)
    if cur:
        if not may(cur["scope"], cur["owner"]):
            raise PermissionError("you may not change this AI service")
        scope, owner = cur["scope"], cur["owner"]          # the scope of an existing service does not move
    else:
        scope = a.get("scope", "user")
        if scope == "system":
            owner = None
        elif scope == "dept":
            owner = int(a.get("owner")) if str(a.get("owner", "")).isdigit() else None
            if owner is None:
                raise ValueError("choose the department")
        elif scope == "user":
            owner = user["username"]
        else:
            raise ValueError("scope: system, dept or user")
        if not may(scope, owner):
            raise PermissionError({"system": "AI services for everyone are added by admins",
                                   "dept": "department AI services are added by that department's managers (if its policy allows)",
                                   "user": "bringing your own AI service is switched off for you"}[scope])
        pid = secrets.token_hex(4)
        cur = {"id": pid, "scope": scope, "owner": owner}
        items.append(cur)
    first = not any(p.get("default") for p in items if p["scope"] == scope and p["owner"] == owner and p["id"] != pid)
    cur.update({"name": name, "kind": kind, "model": model, "base_url": url, "effort": effort,
                "local": bool(a.get("local")) and kind != "anthropic", "enabled": a.get("enabled", True) not in (False, 0, "0", "false"),
                "default": bool(a.get("default")) or first, "added_by": cur.get("added_by") or user["username"]})
    if cur["default"]:                                  # one default per scope
        for p in items:
            if p is not cur and p["scope"] == scope and p["owner"] == owner:
                p["default"] = False
    if cur["local"] and not is_local(cur):
        cur["local"] = False                            # its address is not on a private network: treated as cloud
    _save_all(items)
    key = a.get("api_key")
    if key:                                             # "" keeps the stored key; "-" removes it
        db.set_setting("ai_key_" + pid, None if key == "-" else str(key).strip()[:400])
    return pid, name, scope, owner


def delete_provider(pid, may):
    items = _all()
    cur = next((p for p in items if p["id"] == pid), None)
    if not cur:
        raise ValueError("unknown AI service")
    if not may(cur["scope"], cur["owner"]):
        raise PermissionError("you may not delete this AI service")
    _save_all([p for p in items if p["id"] != pid])
    db.set_setting("ai_key_" + pid, None)
    return cur["name"]


def resolve(pid, user, udepts, machine_dept):
    """The provider for a request: the chosen one (if usable here), else yours -> department's -> everyone's default."""
    usable = [p for p in visible(user, udepts, machine_dept) if p.get("enabled", True)]
    if pid:
        p = next((x for x in usable if x["id"] == pid), None)
        if not p:
            raise AIError("that AI service is not available to you for this machine")
    else:
        rank = {"user": 0, "dept": 1, "system": 2}
        p = next(iter(sorted([x for x in usable if x.get("default")], key=lambda x: rank[x["scope"]])), None) or (usable[0] if usable else None)
    if not p:
        raise AIError("no AI service available - add your own under Settings -> AI services, or ask an admin")
    return p["id"]


def _get(pid):
    p = next((x for x in _all() if x["id"] == pid), None)
    if not p:
        raise AIError("unknown AI service")
    if not p.get("enabled", True):
        raise AIError(f"the AI service '{p['name']}' is switched off")
    return p, db.get_setting("ai_key_" + p["id"]) or ""


# ---------------------------------------------------------------- calling the provider
def _claude(p, key, system, user):
    if VENV not in sys.path and os.path.isdir(VENV):
        sys.path.append(VENV)
    try:
        import anthropic
    except ImportError:
        raise AIError("the Claude SDK is not installed on this server (venv-ai) - see linux/fetch-ai-sdk.sh")
    if not key:
        raise AIError("no API key stored for this Claude service")
    client = anthropic.Anthropic(api_key=key, base_url=p.get("base_url") or None, timeout=180.0, max_retries=2)
    oc = {"format": {"type": "json_schema", "schema": SCHEMA}}
    if p.get("effort"):
        oc["effort"] = p["effort"]
    kw = dict(model=p["model"], max_tokens=16000, system=system, messages=[{"role": "user", "content": user}], output_config=oc)
    try:
        if p["model"] in FALLBACK_MODELS:               # a declined request is re-run on a fallback model, same call
            r = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kw)
        else:
            r = client.messages.create(**kw)
    except anthropic.AuthenticationError:
        raise AIError("Claude rejected the API key")
    except anthropic.PermissionDeniedError:
        raise AIError("this API key may not use that model")
    except anthropic.NotFoundError:
        raise AIError(f"model '{p['model']}' not found")
    except anthropic.RateLimitError:
        raise AIError("rate limited by Claude - try again in a minute")
    except anthropic.BadRequestError as e:
        raise AIError(f"Claude refused the request: {e.message}")
    except anthropic.APIStatusError as e:
        raise AIError(f"Claude API error {e.status_code}")
    except anthropic.APITimeoutError:
        raise AIError("Claude did not answer in time")
    except anthropic.APIConnectionError:
        raise AIError("cannot reach the Claude API from this server (internet / firewall / proxy?)")
    if r.stop_reason == "refusal":
        raise AIError("the model declined this request")
    if r.stop_reason == "max_tokens":
        raise AIError("the answer was cut off (max_tokens)")
    text = next((b.text for b in r.content if b.type == "text"), "")
    return text, getattr(r, "model", p["model"])


def _openai(p, key, system, user):
    url = p["base_url"] + "/chat/completions"
    base = {"model": p["model"], "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    # strongest format first; local servers that don't know json_schema get json_object, then plain instructions
    tries = [{"response_format": {"type": "json_schema", "json_schema": {"name": "install_plan", "schema": SCHEMA, "strict": True}}},
             {"response_format": {"type": "json_object"}}, {}]
    last = ""
    for extra in tries:
        body = json.dumps(dict(base, **extra)).encode()
        req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json",
                                                                            **({"Authorization": "Bearer " + key} if key else {})})
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                d = json.loads(resp.read().decode("utf-8", "replace"))
            return d["choices"][0]["message"]["content"] or "", d.get("model") or p["model"]
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read(300).decode('utf-8', 'replace')}"
            if e.code in (401, 403):
                raise AIError("the AI service rejected the key")
            if e.code == 404:
                raise AIError(f"not found at {url} (check the base URL - usually ends in /v1) or model '{p['model']}'")
            if e.code not in (400, 422, 500):
                break
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise AIError(f"cannot reach {p['base_url']}: {getattr(e, 'reason', e)}")
        except (KeyError, IndexError, ValueError):
            raise AIError("the AI service answered in an unexpected format")
    raise AIError("the AI service refused the request - " + last[:200])


def _parse(text):
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except ValueError:
        m = re.search(r"\{.*\}", t, re.S)            # small local models sometimes wrap the JSON in prose
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                pass
    raise AIError("the AI answer was not valid JSON - try a larger model, or one that supports JSON output")


def ask(system, user, provider_id=None):
    p, key = _get(provider_id)
    t0 = time.time()
    text, model = (_claude if p["kind"] == "anthropic" else _openai)(p, key, system, user)
    return _parse(text), {"provider": p["name"], "kind": p["kind"], "model": model, "local": is_local(p),
                          "seconds": round(time.time() - t0, 1)}


def test(provider_id):
    """A tiny real request: proves URL, key, model and JSON output work."""
    p, key = _get(provider_id)
    sys_ = "Answer with JSON only."
    user = 'Return exactly this JSON with your own short summary: {"summary": "ok", "recommended": {"os": "x", "disk": "/dev/x", ' \
           '"reasons": [], "caveats": []}, "alternatives": [], "findings": [], "questions": []}'
    t0 = time.time()
    text, model = (_claude if p["kind"] == "anthropic" else _openai)(p, key, sys_, user)
    _parse(text)
    return {"ok": True, "model": model, "seconds": round(time.time() - t0, 1)}


# ---------------------------------------------------------------- the install plan
def _redact(facts, local):
    f = json.loads(json.dumps(facts))
    if local:
        return f
    serials = [d.get("serial") for d in f.get("disks", []) if d.get("serial")]
    for d in f.get("disks", []):
        d.pop("serial", None)

    def scrub(x):
        if isinstance(x, str):
            for s in serials:
                x = x.replace(s, "[serial]")
            return re.sub(r"\b([0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}\b", "[mac]", x)
        if isinstance(x, list):
            return [scrub(i) for i in x]
        if isinstance(x, dict):
            return {k: scrub(v) for k, v in x.items()}
        return x
    return scrub(f)


def plan(facts, catalog, rules_result, purpose, request, provider_id=None):
    """Ask the configured AI for a plan; validate it against the catalog and the machine's disks."""
    p, _ = _get(provider_id)
    cat = {c["id"]: c for c in catalog}
    disks = {d["dev"]: d for d in facts.get("disks", [])}
    rr = {"recommended": (rules_result.get("recommended") or {}).get("os"),
          "alternatives": [a["os"] for a in rules_result.get("alternatives", [])],
          "not_possible": rules_result.get("excluded", []),
          "findings": [f["text"] for f in rules_result.get("findings", [])]}
    user = json.dumps({
        "purpose": purpose, "operator_request": (request or "")[:1000],
        "machine_facts": _redact(facts, is_local(p)),
        "catalog": [{"id": c["id"], "name": c["name"], "min_disk_gb": c.get("min_gb") or 0, "note": (c.get("note") or c.get("desc") or "")[:300]}
                    for c in catalog],
        "rule_based_advice": rr}, ensure_ascii=False)
    out, meta = ask(SYSTEM, user, provider_id)

    def clean(x):
        if not isinstance(x, dict) or x.get("os") not in cat:
            return None
        disk = x.get("disk") if x.get("disk") in disks else (rules_result.get("recommended") or {}).get("disk")
        if not disk:
            return None
        cav = [str(c)[:400] for c in (x.get("caveats") or [])][:8]
        need = cat[x["os"]].get("min_gb") or 0
        if need and disks.get(disk, {}).get("gb", 0) < need:
            return None                                 # physics: does not fit on that disk
        return {"os": x["os"], "name": cat[x["os"]]["name"], "disk": disk, "reasons": [str(r)[:400] for r in (x.get("reasons") or [])][:8],
                "caveats": cav, "force": bool(cav)}
    plans = [c for c in [clean(out.get("recommended"))] + [clean(a) for a in (out.get("alternatives") or [])][:6] if c]
    seen, uniq = set(), []
    for c in plans:
        if (c["os"], c["disk"]) not in seen:
            seen.add((c["os"], c["disk"]))
            uniq.append(c)
    if not uniq:
        raise AIError("the AI proposed nothing that this department can install on this machine - the rule-based plan stands")
    findings = [{"level": f.get("level") if f.get("level") in ("info", "warn", "bad") else "info", "text": str(f.get("text", ""))[:500],
                 "evidence": "AI"} for f in (out.get("findings") or []) if isinstance(f, dict)][:10]
    return {"source": "ai", "ai": meta, "summary": str(out.get("summary", ""))[:1500], "recommended": uniq[0], "alternatives": uniq[1:],
            "findings": findings, "questions": [str(q)[:300] for q in (out.get("questions") or [])][:6], "excluded": rules_result.get("excluded", [])}
