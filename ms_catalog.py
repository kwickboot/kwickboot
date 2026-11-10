"""ms_catalog.py - Microsoft Update Catalog as a driver source (search by HWID, resolve the SIGNED CAB, mirror it).
SPDX-License-Identifier: AGPL-3.0-or-later

Why this source: Microsoft redistributes already-SIGNED, HWID-indexed driver CABs (deployment-safe, unlike OEM EULA
bundles) and they are per-component (KB-MB, e.g. an Intel NIC driver is ~279 KB) - so they de-dupe to almost nothing and
need no building or self-signing (which modern Secure-Boot Windows forbids anyway). We only ever ship MS-signed bits.

NO official API: this scrapes catalog.update.microsoft.com (ASP.NET). Best-effort; if Microsoft changes the page the
parsing here may need updating. Columns seen 2026-11: C1 title, C2 products, C3 classification, C4 date, C5 version,
C6 size; each result's UpdateID comes from goToDetails("<guid>"). The download URL + MS's own SHA-1 digest come from a
POST to DownloadDialog.aspx. We record MS's SHA-1 as an integrity check and compute our own SHA-256 for the store.

  python3 ms_catalog.py search "<query>"          # e.g. a hardware id or "Intel Ethernet I219"
  python3 ms_catalog.py hwid  "PCI\\VEN_8086&DEV_15F3"   # best driver update for a HWID (latest), with its CAB url
"""
import html
import json
import re
import sys
import urllib.parse
import urllib.request

BASE = "https://www.catalog.update.microsoft.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) KwickBoot-drivers"
GUID = r"[0-9a-fA-F-]{36}"


def _get(url, data=None):
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded"} if data
                                 else {"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def _cell(doc, guid, col):
    m = re.search(r'id="' + re.escape(guid) + "_C" + str(col) + r'_[^"]*"[^>]*>(.*?)</td>', doc, re.S)
    return html.unescape(re.sub(r"<[^>]+>", " ", m.group(1))).strip() if m else ""


def search(query, drivers_only=True):
    """Result rows for a query: [{updateid, title, products, classification, date, version, size}]."""
    doc = _get(BASE + "/Search.aspx?q=" + urllib.parse.quote(query))
    out, seen = [], set()
    for m in re.finditer(r'goToDetails\("(' + GUID + r')"\)', doc):
        g = m.group(1)
        if g in seen:
            continue
        seen.add(g)
        cls = _cell(doc, g, 3)
        if drivers_only and "driver" not in cls.lower():
            continue
        size = _cell(doc, g, 6)
        sb = re.search(r"\b(\d{5,})\b", size)
        out.append({"updateid": g, "title": _cell(doc, g, 1), "products": _cell(doc, g, 2),
                    "classification": cls, "date": _cell(doc, g, 4), "version": _cell(doc, g, 5),
                    "size": int(sb.group(1)) if sb else 0})
    return out


def download(updateid):
    """Resolve an update's downloadable file: {url, sha1_b64, filename} (the CAB on windowsupdate.com), or None."""
    body = urllib.parse.urlencode({"updateIDs": json.dumps(
        [{"size": 0, "languages": "", "uidInfo": updateid, "updateID": updateid}])}).encode()
    js = _get(BASE + "/DownloadDialog.aspx", data=body)
    u = re.search(r"\.url\s*=\s*'(https?://[^']+\.(?:cab|msu|exe))'", js)
    if not u:
        return None
    d = re.search(r"\.digest\s*=\s*'([^']+)'", js)
    fn = re.search(r"\.fileName\s*=\s*'([^']+)'", js)
    return {"url": u.group(1), "sha1_b64": d.group(1) if d else "", "filename": fn.group(1) if fn else ""}


def _getbin(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=180) as r:
        return r.read()


def _classify(classification):
    c = (classification or "").lower()
    for key, cls in (("network", "network"), ("display", "video"), ("video", "video"), ("sound", "audio"),
                     ("audio", "audio"), ("storage", "storage"), ("firmware", "firmware"), ("system", "chipset")):
        if key in c:
            return cls
    return "driver"


def _inf_hwids(folder):
    import os
    import drivers
    ids = set()
    for dp, _dn, fn in os.walk(folder):
        for f in fn:
            if f.lower().endswith(".inf"):
                try:
                    txt = drivers._read_inf(os.path.join(dp, f))
                except Exception:
                    continue
                for m in drivers.HWID_RE.finditer(txt):
                    ids.add(f"pci:{m.group(1).upper()}:{m.group(2).upper()}")
    return sorted(ids)


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:48]


def fetch(hwid, store_dir):
    """Resolve the latest MS-signed driver for a HWID, verify MS's SHA-1, extract, de-dupe into the zstd-19 store, and
    return a schema-v2 catalog entry keyed on EVERY HWID the INF supports. Needs cabextract + zstd. Returns None if no
    driver is found. The binary is Microsoft-signed; we never build or self-sign."""
    import base64
    import hashlib
    import os
    import shutil
    import subprocess
    import tempfile
    import driver_store
    hit = lookup_hwid(hwid)
    if not hit or not hit.get("url"):
        return None
    tmp = tempfile.mkdtemp()
    try:
        data = _getbin(hit["url"])
        if hit.get("sha1_b64"):                    # MS's own integrity digest (SHA-1, base64)
            got = base64.b64encode(hashlib.sha1(data).digest()).decode()
            if got != hit["sha1_b64"]:
                raise ValueError(f"MS SHA-1 mismatch ({got} != {hit['sha1_b64']})")
        cab = os.path.join(tmp, "d.cab")
        open(cab, "wb").write(data)
        files = os.path.join(tmp, "files")
        os.makedirs(files)
        subprocess.run(["cabextract", "-q", "-d", files, cab], check=True, capture_output=True)
        ids = _inf_hwids(files) or [f"pci:{hwid.split('VEN_')[-1][:4]}:{hwid.split('DEV_')[-1][:4]}".upper()]
        name = ("mu-" + _slug(hit["title"]) + "-" + _slug(hit.get("version", "")))[:60].strip("-")
        cls = _classify(hit["classification"])
        meta = {"ids": ids, "target": "win", "kind": "inf", "class": cls, "origin": hit["url"],
                "license": "Microsoft Update Catalog (signed; deployment-redistributable)",
                "sha1_b64": hit.get("sha1_b64"), "version": hit.get("version"), "date": hit.get("date")}
        man = driver_store.add(store_dir, name, files, meta)
        entry = {"name": name, "class": cls, "ids": ids, "arch": ["x86_64"], "target": "win", "kind": "inf",
                 "store": name, "origin": hit["url"], "license": meta["license"], "sha1_b64": hit.get("sha1_b64"),
                 "version": hit.get("version"), "note": hit["title"]}
        return {"entry": entry, "store_files": len(man.get("files", [])), "hwids": len(ids)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _date_key(s):
    m = re.match(r"(\d+)/(\d+)/(\d{4})", s or "")
    return (int(m.group(3)), int(m.group(1)), int(m.group(2))) if m else (0, 0, 0)


def lookup_hwid(hwid, os_hint="Windows 11"):
    """Best (latest) driver update for a hardware id. Prefers the os_hint in products. Returns the row + download, or None."""
    rows = search(hwid, drivers_only=True)
    if not rows:
        return None
    pref = [r for r in rows if os_hint.lower() in (r["products"] + r["title"]).lower()] or rows
    best = max(pref, key=lambda r: _date_key(r["date"]))
    dl = download(best["updateid"])
    return dict(best, **(dl or {})) if dl else None


def main(argv):
    if len(argv) < 3:
        sys.exit(__doc__)
    op, q = argv[1], argv[2]
    if op == "search":
        for r in search(q):
            print(f"{r['date']:>10}  {r['version']:<16} {r['size']:>10}  {r['classification']:<22} {r['title'][:60]}  {r['updateid']}")
    elif op == "hwid":
        hit = lookup_hwid(q)
        print(json.dumps(hit, indent=2) if hit else "no driver found")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
