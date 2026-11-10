"""drivers_catalog.py - KwickBoot's SIGNED network driver catalog, synced to this boot server from the CDN.
SPDX-License-Identifier: AGPL-3.0-or-later

WHAT: a curated, Ed25519-SIGNED supplement to the per-vendor catalogs in drivers.py (Dell/HP/Lenovo). It carries
network-learned cases those miss - Linux firmware, cross-vendor fixes, "this model+distro needs pack X" - each entry a
VETTED pack {url, sha256}. Crowd data only SUGGESTS needs (compat network); a human vets the actual pack before it lands
in this signed catalog, so crowd data never drives a binary install directly.

TOPOLOGY (set by product): the RAM OS NEVER hits the CDN. Only THIS boot server pulls the catalog from
download.kwickboot.com, and only once in a while. Matching packs are downloaded once (vendor SHA-256 verified by
drivers.py), cached locally, and served to the whole fleet over the LAN at install time - fast + offline-friendly.

TRUST: the catalog is Ed25519-signed; we verify with a PINNED public key BEFORE trusting any url/sha256, because these
drive binary installs. (Packs are still re-verified by their own vendor SHA-256 on download.) the build host may lack gpg, so we
verify with python-cryptography, not GPG - the GPG release key stays for the ISO/release.

GATING: auto-sync happens only on an opt-in ('share') server. A 'local'/'off' server never auto-pulls; its operator can
run `pxectl compat pull` (give-to-get: upload this server's accumulated list, then fetch the latest catalog).
"""
import base64
import json
import os
import re
import ssl
import time
import urllib.request

import db
import drivers

CATALOG_URL = "https://download.kwickboot.com/compat/drivers.json"
CATALOG_SIG_URL = CATALOG_URL + ".sig"
CATALOG_PUBKEY_B64 = "CpACaT5cZLFWb9OmQhUIcz6nGN5ljuwESLGupspGqxw="   # pinned; private key lives only on the build box
USER_AGENT = "KwickBoot-compat/1 (+https://kwickboot.com)"
CACHE = os.path.join(drivers.BASE, "data", "driver-catalog.json")


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
    with urllib.request.urlopen(req, timeout=30, context=ssl.create_default_context()) as r:
        return r.read()


def _verify(data, sig_b64):
    """Raise unless sig_b64 is a valid Ed25519 signature over data from the pinned key. No gpg dependency."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    pk = Ed25519PublicKey.from_public_bytes(base64.b64decode(CATALOG_PUBKEY_B64))
    pk.verify(base64.b64decode(sig_b64), data)       # raises cryptography.exceptions.InvalidSignature on tamper


def sync(max_age=86400, force=False):
    """Pull + VERIFY + cache the signed catalog (once/day unless force). Returns (ok, message). Refuses an unverified
    or tampered catalog (keeps the last good cache)."""
    at = db.get_setting("driver_catalog_at", "")
    if not force:
        try:
            if at and time.time() - float(at) < max_age:
                return True, "fresh"
        except ValueError:
            pass
    try:
        data = _get(CATALOG_URL)
        sig = _get(CATALOG_SIG_URL).decode().strip()
    except Exception as e:
        return False, f"fetch failed: {e}"
    try:
        _verify(data, sig)
    except Exception as e:
        return False, f"SIGNATURE INVALID - refusing catalog ({e})"
    try:
        cat = json.loads(data)
        n = len(cat.get("packs", []))
    except ValueError as e:
        return False, f"bad json ({e})"
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE + ".tmp", "wb") as f:
        f.write(data)
    os.replace(CACHE + ".tmp", CACHE)
    db.set_setting("driver_catalog_at", str(time.time()))
    return True, f"synced + verified {n} entr{'y' if n == 1 else 'ies'}"


def auto_sync():
    """Called from the periodic server job. ONLY an opt-in ('share') server auto-pulls; everyone else is a no-op."""
    import insights
    if insights.mode() != "share":
        return False, "not opted in (no auto-pull)"
    return sync()


def catalog():
    try:
        return json.load(open(CACHE, encoding="utf-8"))
    except (OSError, ValueError):
        return {"packs": []}


def _norm(s):
    return (s or "").strip().upper()


def _arch(inventory_text):
    t = (inventory_text or "").lower()
    return "arm64" if ("aarch64" in t or "arm64" in t) else "x86_64"


def machine_ids(inventory_text):
    """The universal identity set for this machine (schema v2): bus-prefixed ids every entry is matched against.
      PCI:VEN:DEV   USB:VID:PID   DMI:<MAKER>:<KEY>  (Dell SKU / HP board / Lenovo type)
      PCI-CLASS:CCSS  - the device's class/subclass, so a GENERIC class driver can match when no exact one exists.
    Works the same whether the box PXE-booted or booted our USB loader - identity is independent of boot path."""
    ids = set()
    cur = ""
    for line in (inventory_text or "").splitlines():
        if line.startswith("["):
            cur = line.strip()
            continue
        f = line.split()
        if cur == "[pci-ids]" and len(f) >= 4 and all(re.fullmatch(r"[0-9a-fA-F]{4}", x) for x in f[2:4]):
            ids.add(f"PCI:{f[2]}:{f[3]}".upper())
            if re.fullmatch(r"[0-9a-fA-F]{4,6}", f[1]):        # class code -> class/subclass for compatible matching
                ids.add(f"PCI-CLASS:{f[1][:4]}".upper())
        elif cur == "[usb-ids]":
            m = re.search(r"\b([0-9a-fA-F]{4})[: ]([0-9a-fA-F]{4})\b", line)
            if m:
                ids.add(f"USB:{m.group(1)}:{m.group(2)}".upper())
    si = drivers.system_info(inventory_text)
    maker, mkey = drivers.model_key(si)
    if maker and mkey:
        ids.add(f"DMI:{maker}:{mkey}".upper())
    return ids


_CLASS_PREFIX = ("PCI-CLASS:", "USB-CLASS:")


def match(inventory_text, target=None):
    """Schema-v2 match: catalog packs whose universal ids intersect this machine's, filtered by target OS (win|linux)
    and arch (x86_64|arm64) when the entry constrains them. EXACT-first: a pack matched on a device id (or model/sku)
    sorts before a 'compatible' one matched only on a class id (or flagged compatible), so callers/OS prefer exact and
    fall back to the compatible driver only when no exact one exists. target=None matches any target."""
    ids = machine_ids(inventory_text)
    arch = _arch(inventory_text)
    exact, compat = [], []
    for p in catalog().get("packs", []):
        if target and p.get("target", "any") not in (target, "any"):
            continue
        pa = p.get("arch")
        if pa and arch not in pa:
            continue
        pids = {_norm(x) for x in p.get("ids", [])}
        inter = ids & pids
        if not (p.get("all") or inter):
            continue
        class_only = bool(inter) and all(x.startswith(_CLASS_PREFIX) for x in inter)
        (compat if (class_only or p.get("compatible")) else exact).append(p)
    return exact + compat


def ensure_for(inventory_text):
    """Make sure every WINDOWS driver pack matching this machine is in the local drivers.py library (download once from
    the vetted url+sha256, cached + LAN-served to the fleet). Idempotent. Returns the names added. Non-Windows targets
    (e.g. linux firmware) have their own apply path."""
    have = {p.get("name") for p in drivers.packs()}
    added = []
    for p in match(inventory_text, target="win"):
        name = p.get("name")
        if not name or name in have:
            continue
        if p.get("kind", "inf") != "inf":
            continue
        try:
            drivers.add(name, p["url"], p["sha256"], all_=bool(p.get("all")))
            added.append(name)
        except Exception as e:
            try:
                db.audit("drivers_catalog", f"add {name} failed: {e}", "")
            except Exception:
                pass
    return added
