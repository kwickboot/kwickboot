"""drivers.py - Windows driver library for unattended installs ("driver injection from inventory").
SPDX-License-Identifier: AGPL-3.0-or-later

IT adds vendor driver packs once (official URL + the vendor's SHA-256, or a folder they unpacked themselves). Every .inf
is indexed by the PCI hardware IDs it supports. At install time the server matches those IDs against the PCI devices
in the machine's inventory and hands the running install only the matching driver folders (a tar stream); Windows'
device installer picks them up during setup (C:\\Drivers, DevicePath) and the first boot installs any left over with
pnputil. Packs live under data/drivers (not the web folder); nothing here is secret, but only installs fetch them.

  python3 drivers.py list
  python3 drivers.py add <name> <https-url> <sha256> [--all]      .zip, .cab (cabextract) or self-extracting .exe (7zip / innoextract)
  python3 drivers.py import <name> <folder> [--all]                a folder already unpacked (copied, then indexed)
  python3 drivers.py rm <name>
  python3 drivers.py match <inventory.txt>                         which packs/folders a machine would get
--all: the pack goes to every Windows install (for devices the inventory cannot see, e.g. behind a USB hub)
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile

BASE = os.path.dirname(os.path.abspath(__file__))


def _lib_dir():
    """config.json "drivers_dir" (e.g. on the big image disk - Dell packs are 1-3 GB each), else data/drivers."""
    try:
        d = json.load(open(os.path.join(BASE, "config.json"), encoding="utf-8")).get("drivers_dir")
    except (OSError, ValueError):
        d = None
    return os.path.join(BASE, d) if d and not os.path.isabs(d) else (d or os.path.join(BASE, "data", "drivers"))


LIB = _lib_dir()
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,60}")
HWID_RE = re.compile(r"PCI\\VEN_([0-9A-F]{4})&DEV_([0-9A-F]{4})(?:&SUBSYS_([0-9A-F]{8}))?", re.I)
MAX_PACK = 4 * 1024 ** 3          # 4 GB download limit


def _read_inf(path):
    raw = open(path, "rb").read(4 * 1024 * 1024)
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff") or raw[1:2] == b"\x00":
        return raw.decode("utf-16", "replace")
    return raw.decode("latin-1")


def index_pack(name):
    """Scan a pack's .inf files -> index.json: [{dir, inf, ids: [[ven, dev, subsys|""], ...]}]."""
    root = os.path.join(LIB, name, "files")
    infs = []
    for dp, _dn, fn in os.walk(root):
        for f in fn:
            if f.lower().endswith(".inf"):
                p = os.path.join(dp, f)
                ids = sorted({(m.group(1).upper(), m.group(2).upper(), (m.group(3) or "").upper())
                              for m in HWID_RE.finditer(_read_inf(p))})
                if ids:
                    infs.append({"dir": os.path.relpath(dp, root).replace("\\", "/"), "inf": f, "ids": [list(i) for i in ids]})
    meta = _meta(name)
    meta.update(infs=len(infs), hwids=sum(len(i["ids"]) for i in infs),
                bytes=sum(os.path.getsize(os.path.join(dp, f)) for dp, _d, fn in os.walk(root) for f in fn))
    json.dump(infs, open(os.path.join(LIB, name, "index.json"), "w"))
    json.dump(meta, open(os.path.join(LIB, name, "pack.json"), "w"))
    return meta


def _meta(name):
    try:
        return json.load(open(os.path.join(LIB, name, "pack.json")))
    except (OSError, ValueError):
        return {"name": name}


def packs():
    if not os.path.isdir(LIB):
        return []
    return [_meta(n) for n in sorted(os.listdir(LIB)) if os.path.isfile(os.path.join(LIB, n, "index.json"))]


def _new_pack(name, all_):
    if not NAME_RE.fullmatch(name):
        raise ValueError("pack name: letters, digits, . _ + - (max 61)")
    d = os.path.join(LIB, name)
    if os.path.exists(d):
        raise ValueError(f"pack {name} exists (rm it first)")
    os.makedirs(os.path.join(d, "files"))
    json.dump({"name": name, "all": bool(all_), "added": time.time()}, open(os.path.join(d, "pack.json"), "w"))
    return d


def _safe_extract_zip(zpath, dest):
    with zipfile.ZipFile(zpath) as z:
        for m in z.infolist():
            target = os.path.realpath(os.path.join(dest, m.filename))
            if not target.startswith(os.path.realpath(dest) + os.sep) and target != os.path.realpath(dest):
                raise ValueError(f"unsafe path in the archive: {m.filename}")
        z.extractall(dest)


def add(name, url, sha, all_=False):
    if not re.fullmatch(r"https://[A-Za-z0-9./_+:%?=&-]+", url):
        raise ValueError("an https:// URL from the vendor")
    if not re.fullmatch(r"[0-9a-f]{64}", sha.lower()):
        raise ValueError("the vendor's SHA-256 (64 hex characters) is required")
    d = _new_pack(name, all_)
    try:
        tmp = os.path.join(d, "download")
        h, n = hashlib.sha256(), 0
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "KwickBoot-drivers"}), timeout=60) as r, open(tmp, "wb") as o:
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                n += len(b)
                if n > MAX_PACK:
                    raise ValueError("larger than 4 GB")
                h.update(b)
                o.write(b)
        if h.hexdigest() != sha.lower():
            raise ValueError(f"CHECKSUM MISMATCH ({h.hexdigest()}) - discarded")
        files = os.path.join(d, "files")
        head = open(tmp, "rb").read(4)
        if head[:2] == b"PK":
            _safe_extract_zip(tmp, files)
        elif head == b"MSCF":
            if not shutil.which("cabextract"):
                raise ValueError(".cab packs need cabextract on this server (apt install cabextract), "
                                 "or unpack it yourself and use: pxectl driver import <name> <folder>")
            subprocess.run(["cabextract", "-q", "-d", files, tmp], check=True)
        elif head[:2] == b"MZ":
            # a self-extracting .exe (Dell and HP packs): 7-Zip opens most of them, innoextract the Inno Setup ones (Lenovo)
            ok = False
            if shutil.which("7z"):
                ok = subprocess.run(["7z", "x", "-y", "-bd", "-o" + files, tmp], capture_output=True).returncode == 0
            if not ok and shutil.which("innoextract"):
                shutil.rmtree(files, ignore_errors=True)
                os.makedirs(files)
                ok = subprocess.run(["innoextract", "-s", "-d", files, tmp], capture_output=True).returncode == 0
            if not ok:
                raise ValueError("could not unpack this .exe (needs 7zip or innoextract on the server) - unpack it on a "
                                 "Windows PC and use: pxectl driver import <name> <folder>")
        else:
            raise ValueError("not a .zip, .cab or .exe - unpack the vendor's installer yourself and use: pxectl driver import")
        os.remove(tmp)
        meta = _meta(name)
        meta.update(url=url, sha256=sha.lower())
        json.dump(meta, open(os.path.join(d, "pack.json"), "w"))
        return index_pack(name)
    except BaseException:
        shutil.rmtree(d, ignore_errors=True)
        raise


def import_dir(name, folder, all_=False):
    if not os.path.isdir(folder):
        raise ValueError(f"{folder} is not a folder")
    d = _new_pack(name, all_)
    try:
        shutil.rmtree(os.path.join(d, "files"))
        shutil.copytree(folder, os.path.join(d, "files"), symlinks=False)
        meta = _meta(name)
        meta.update(source="imported folder")
        json.dump(meta, open(os.path.join(d, "pack.json"), "w"))
        return index_pack(name)
    except BaseException:
        shutil.rmtree(d, ignore_errors=True)
        raise


def rm(name):
    if not NAME_RE.fullmatch(name) or not os.path.isdir(os.path.join(LIB, name)):
        raise ValueError(f"no pack {name}")
    shutil.rmtree(os.path.join(LIB, name))


def pci_ids(inventory_text):
    """PCI devices from the inventory's [pci-ids] section: 'slot class ven dev subven subdev' -> {(ven, dev, subsys)}."""
    out, on = set(), False
    for line in (inventory_text or "").splitlines():
        if line.startswith("["):
            on = line.strip() == "[pci-ids]"
            continue
        if on:
            f = line.split()
            if len(f) >= 4 and all(re.fullmatch(r"[0-9a-fA-F]{4}", x) for x in f[2:4]):
                sub = (f[5] + f[4]).upper() if len(f) >= 6 and all(re.fullmatch(r"[0-9a-fA-F]{4}", x) for x in f[4:6]) else ""
                out.add((f[2].upper(), f[3].upper(), sub))
    return out


def match(inventory_text):
    """[(pack, dir)] the machine gets: every folder with an .inf for one of its PCI devices (VEN+DEV, and SUBSYS when
    the .inf names one), plus every folder of packs marked 'all'."""
    have = pci_ids(inventory_text)
    vd = {(v, d) for v, d, _s in have}
    si = system_info(inventory_text)
    out = []
    for p in packs():
        if p.get("skus") and model_key(si, p.get("vendor", "Dell"))[1] not in p["skus"]:
            continue                # a PC maker's model pack goes to that maker's models only (it carries generic drivers too)
        try:
            infs = json.load(open(os.path.join(LIB, p["name"], "index.json")))
        except (OSError, ValueError):
            continue
        for i in infs:
            if p.get("all") or any((v, d) in vd and (not s or (v, d, s) in have) for v, d, s in i["ids"]):
                if (p["name"], i["dir"]) not in out:
                    out.append((p["name"], i["dir"]))
    return out


def tar_stream(pairs, wfile):
    """Write the matched folders as one uncompressed tar stream: <pack>/<dir>/... (driver files compress badly)."""
    with tarfile.open(fileobj=wfile, mode="w|", format=tarfile.GNU_FORMAT) as t:   # busybox tar: GNU long names
        for pack, rel in pairs:
            src = os.path.realpath(os.path.join(LIB, pack, "files", rel))
            if not src.startswith(os.path.realpath(os.path.join(LIB, pack, "files"))):
                continue
            for f in sorted(os.listdir(src)):           # this folder's files only (an .inf references its own folder)
                fp = os.path.join(src, f)
                if os.path.isfile(fp):
                    t.add(fp, arcname=f"{pack}/{rel}/{f}".replace("/./", "/"), recursive=False)


# ---------------------------------------------------------------- automatic: the PC makers' official driver-pack catalogs
# Dell: https://downloads.dell.com/catalog/DriverPackCatalog.cab (signed .cab with XML: one pack per model + Windows
# version, keyed by the System SKU in the PC's firmware, with the pack's SHA-256). Fetched over HTTPS, cached a day.
DELL_CAT = "https://downloads.dell.com/catalog/DriverPackCatalog.cab"
DELL_NS = "{openmanage/cm/dm}"
CAT_MAX_AGE = 24 * 3600


def _cat_dir():
    d = os.path.join(LIB, "_catalogs")
    os.makedirs(d, exist_ok=True)
    return d


def system_info(inventory_text):
    """vendor, product, sku, board from the inventory's [system] section."""
    out = {"vendor": "", "product": "", "sku": "", "board": ""}
    for line in (inventory_text or "").splitlines():
        m = re.match(r"\s+vendor=(.*?) product=(.*?) version=", line)
        if m:
            out["vendor"], out["product"] = m.group(1).strip(), m.group(2).strip()
        m = re.match(r"\s+sku=(.*)$", line)
        if m:
            out["sku"] = m.group(1).strip()
        m = re.match(r"\s+board=\S*\s+(\S+)", line)
        if m:
            out["board"] = m.group(1).strip()
    return out


def dell_catalog(refresh=False):
    """[{skus, os, arch, url, sha256, size, date, release, name}] from Dell's catalog (cached JSON, refreshed daily)."""
    cache = os.path.join(_cat_dir(), "dell.json")
    if not refresh and os.path.isfile(cache) and time.time() - os.path.getmtime(cache) < CAT_MAX_AGE:
        return json.load(open(cache))
    if not shutil.which("cabextract"):
        raise ValueError("Dell's catalog is a .cab: this server needs cabextract (apt install cabextract)")
    import xml.etree.ElementTree as ET
    with tempfile.TemporaryDirectory() as td:
        cab = os.path.join(td, "c.cab")
        with urllib.request.urlopen(urllib.request.Request(DELL_CAT, headers={"User-Agent": "KwickBoot-drivers"}), timeout=60) as r:
            open(cab, "wb").write(r.read(64 * 1024 * 1024))
        subprocess.run(["cabextract", "-q", "-d", td, cab], check=True, capture_output=True)
        root = ET.parse(os.path.join(td, "DriverPackCatalog.xml")).getroot()
    packs_ = []
    for p in root.findall(DELL_NS + "DriverPackage"):
        sha = next((h.text for h in p.iter(DELL_NS + "Hash") if h.get("algorithm") == "SHA256"), None)
        oss = [(o.get("osCode", ""), o.get("osArch", "")) for o in p.iter(DELL_NS + "OperatingSystem")]
        skus = sorted({m.get("systemID", "").upper() for m in p.iter(DELL_NS + "Model") if m.get("systemID")})
        if not sha or not skus or p.get("format") not in ("cab", "exe"):
            continue
        for os_, arch in oss:
            if os_ in ("Windows11", "Windows10"):
                packs_.append({"skus": skus, "os": os_, "arch": arch, "url": "https://downloads.dell.com/" + p.get("path", ""),
                               "sha256": sha.lower(), "size": int(p.get("size") or 0), "date": p.get("dateTime", ""),
                               "release": p.get("releaseID", ""),
                               "name": " / ".join(sorted({m.get("name", "") for m in p.iter(DELL_NS + "Model")}))[:120]})
    tmp = cache + ".tmp"
    json.dump(packs_, open(tmp, "w"))
    os.replace(tmp, cache)
    return packs_


# HP: https://hpia.hpcloud.hp.com/downloads/driverpackcatalog/HPClientDriverPackCatalog.cab - packs keyed by the
# board ID in the firmware (SystemId), SoftPaq .exe with SHA-256. Lenovo: https://download.lenovo.com/cdrt/td/catalogv2.xml
# - packs keyed by machine type (first 4 characters of the product name), .exe with SHA-256 (the "crc" attribute).
HP_CAT = "https://hpia.hpcloud.hp.com/downloads/driverpackcatalog/HPClientDriverPackCatalog.cab"
LENOVO_CAT = "https://download.lenovo.com/cdrt/td/catalogv2.xml"


def _fetch(url, limit=64 * 1024 * 1024):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "KwickBoot-drivers"}), timeout=60) as r:
        return r.read(limit)


def _save_cat(name, entries):
    cache = os.path.join(_cat_dir(), name + ".json")
    json.dump(entries, open(cache + ".tmp", "w"))
    os.replace(cache + ".tmp", cache)
    return entries


def hp_catalog():
    import xml.etree.ElementTree as ET
    if not shutil.which("cabextract"):
        raise ValueError("HP's catalog is a .cab: this server needs cabextract (apt install cabextract)")
    with tempfile.TemporaryDirectory() as td:
        open(os.path.join(td, "c.cab"), "wb").write(_fetch(HP_CAT))
        subprocess.run(["cabextract", "-q", "-d", td, os.path.join(td, "c.cab")], check=True, capture_output=True)
        root = ET.parse(os.path.join(td, "HPClientDriverPackCatalog.xml")).getroot()
    sp = {}
    for e in root.iter("SoftPaq"):
        g = lambda t: (e.findtext(t) or "").strip()  # noqa: E731
        d = re.match(r"(\d+)/(\d+)/(\d{4})", g("DateReleased"))
        sp[g("Id")] = {"url": g("Url").replace("http://", "https://"), "sha256": g("SHA256").lower(), "size": int(g("Size") or 0),
                       "date": f"{d.group(3)}-{int(d.group(1)):02}-{int(d.group(2)):02}" if d else "", "name": g("Name")}
    out = []
    for e in root.iter("ProductOSDriverPack"):
        g = lambda t: (e.findtext(t) or "").strip()  # noqa: E731
        p = sp.get(g("SoftPaqId"))
        osn = g("OSName")
        if not p or not p["sha256"] or "64-bit" not in g("Architecture") + osn:
            continue
        os_ = "Windows11" if "Windows 11" in osn else "Windows10" if "Windows 10" in osn else ""
        if not os_:
            continue
        out.append(dict(p, skus=sorted({x.strip().upper() for x in g("SystemId").split(",") if x.strip()}), os=os_, arch="x64",
                        release=g("SoftPaqId"), name=g("SystemName") or p["name"]))
    return _save_cat("hp", out)


def lenovo_catalog():
    import xml.etree.ElementTree as ET
    raw = _fetch(LENOVO_CAT)
    root = ET.fromstring(raw.decode("utf-8-sig").lstrip())
    out = []
    for m in root.iter("Model"):
        types = sorted({(t.text or "").strip().upper() for t in m.iter("Type") if t.text})
        for sc in m.iter("SCCM"):
            os_ = {"win11": "Windows11", "win10": "Windows10"}.get(sc.get("os", ""))
            sha = (sc.get("crc") or "").lower()
            url = (sc.text or "").strip()
            if os_ and re.fullmatch(r"[0-9a-f]{64}", sha) and url.startswith("http"):
                out.append({"skus": types, "os": os_, "arch": "x64", "url": url.replace("http://", "https://"), "sha256": sha,
                            "size": 0, "date": sc.get("date", ""), "release": (sc.get("version") or "x").replace("*", "any"),
                            "name": m.get("name", "")})
    return _save_cat("lenovo", out)


CATALOGS = {"Dell": ("dell", lambda: dell_catalog(refresh=True)), "HP": ("hp", hp_catalog), "Lenovo": ("lenovo", lenovo_catalog)}


def model_key(si, vendor=None):
    """(maker, model key) for this machine: Dell = System SKU, HP = board ID, Lenovo = machine type."""
    v = (vendor or si["vendor"] or "").lower()
    if v.startswith("dell"):
        return "Dell", (si["sku"] or "").upper()
    if v.startswith("hp") or "hewlett" in v:
        return "HP", (si["board"] or "").upper()
    if v.startswith("lenovo"):
        return "Lenovo", (si["product"] or "")[:4].upper()
    return "", ""


def refresh_catalogs():
    got = {}
    for maker, (_f, fn) in CATALOGS.items():
        try:
            got[maker] = len(fn())
        except Exception as e:        # noqa: BLE001 - one maker's outage must not stop the others
            got[maker] = f"failed: {e}"
    return got


def lookup(inventory_text, cached_only=True):
    """The PC maker's official driver pack for this machine (Windows 11 x64, else Windows 10 x64), or None.
    cached_only: never fetch the catalog here (the console asks often); 'pxectl driver catalogs' / auto refresh it."""
    si = system_info(inventory_text)
    maker, key = model_key(si)
    if not maker or not re.fullmatch(r"[0-9A-Z]{4}", key or ""):
        return None
    f, fn = CATALOGS[maker]
    try:
        cache = os.path.join(_cat_dir(), f + ".json")
        fresh = os.path.isfile(cache) and time.time() - os.path.getmtime(cache) < CAT_MAX_AGE
        cat = json.load(open(cache)) if (cached_only or fresh) else fn()
    except (OSError, ValueError):
        return None
    for want in ("Windows11", "Windows10"):
        hits = [p for p in cat if key in p["skus"] and p["os"] == want and p["arch"] in ("x64", "")]
        if hits:
            best = max(hits, key=lambda p: p["date"])
            rel = re.sub(r"[^a-z0-9]", "", best["release"].lower())[:20]
            return dict(best, vendor=maker, model=si["product"], pack=f"{f}-{key.lower()}-{best['os'][-2:]}-{rel}")
    return None


def auto_add(inventory_text):
    """Download the maker's pack for this machine into the library (once per model). Returns the pack's metadata."""
    hit = lookup(inventory_text, cached_only=False)
    if not hit:
        si = system_info(inventory_text)
        raise ValueError(f"no official driver pack found for {si['vendor']} {si['product']} (sku {si['sku'] or '?'}) - "
                         "Windows' own drivers and Windows Update apply")
    if os.path.isdir(os.path.join(LIB, hit["pack"])):
        return _meta(hit["pack"])
    m = add(hit["pack"], hit["url"], hit["sha256"])
    m.update(vendor=hit["vendor"], model=hit["name"], os=hit["os"], skus=hit["skus"])
    json.dump(m, open(os.path.join(LIB, hit["pack"], "pack.json"), "w"))
    return m


def _inventory_of(mac):
    sys.path.insert(0, BASE)
    import db
    db.init(os.path.join(BASE, "data", "kwickboot.db"))
    t, _ = db.latest_report(mac.lower().replace("-", ":"), "inventory")
    if not t:
        raise ValueError(f"no hardware report for {mac}")
    return t


if __name__ == "__main__":
    a = sys.argv[1:]
    try:
        if not a or a[0] == "list":
            for p in packs():
                print(f"{p['name']:30} {p.get('infs', 0):4} drivers {p.get('hwids', 0):5} hardware IDs "
                      f"{p.get('bytes', 0) / 1e6:7.1f} MB{'  (every install)' if p.get('all') else ''}")
        elif a[0] == "add" and len(a) >= 4:
            m = add(a[1], a[2], a[3], "--all" in a)
            print(f"added {m['name']}: {m['infs']} drivers, {m['hwids']} hardware IDs")
        elif a[0] == "import" and len(a) >= 3:
            m = import_dir(a[1], a[2], "--all" in a)
            print(f"imported {m['name']}: {m['infs']} drivers, {m['hwids']} hardware IDs")
        elif a[0] == "rm" and len(a) == 2:
            rm(a[1])
            print(f"removed {a[1]}")
        elif a[0] == "catalogs":
            for k, v in refresh_catalogs().items():
                print(f"{k}: {v} Windows 10/11 x64 entries" if isinstance(v, int) else f"{k}: {v}")
        elif a[0] == "lookup" and len(a) == 2:
            h = lookup(_inventory_of(a[1]), cached_only=False)
            print(f"{h['vendor']} {h['name']}: {h['pack']} ({h['os']} {h['arch']}, {h['size'] / 1e9:.1f} GB, {h['date'][:10]})\n  {h['url']}"
                  if h else "no official driver pack for this machine (Windows' own drivers + Windows Update)")
        elif a[0] == "auto" and len(a) == 2:
            m = auto_add(_inventory_of(a[1]))
            print(f"in the library: {m['name']} ({m.get('infs', 0)} drivers, {m.get('hwids', 0)} hardware IDs)")
        elif a[0] == "match" and len(a) == 2:
            for pk, d in match(open(a[1], encoding="utf-8", errors="replace").read()):
                print(f"{pk}/{d}")
        else:
            print(__doc__.split("\n\n")[1])
            sys.exit(1)
    except (ValueError, OSError, subprocess.CalledProcessError) as e:
        print(f"ERROR: {e}")
        sys.exit(1)
