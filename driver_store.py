#!/usr/bin/env python3
"""build-driver-store.py - content-addressed, de-duplicated, zstd-19 driver store. Minimum disk, maximum coverage.
SPDX-License-Identifier: AGPL-3.0-or-later

Each driver FILE is stored ONCE, keyed by its SHA-256 (objects/<sha>.zst, zstd -19). The same component driver that
sits inside hundreds of vendor model packs is stored a single time. A "driver" is just a manifest (relpath -> file
hash); the index maps device/class ids -> manifest. A boot server later fetches only the few unique objects a device
needs, decompresses, reassembles the folder and serves it over the LAN. De-dup removes redundancy ACROSS packs; zstd-19
removes it WITHIN files; compatible class drivers (one driver, many devices) remove it at the device level.

  build-driver-store.py <store-dir> add   <driver-name> <src-folder> [--ids pci:VEN:DEV,pci-class:0200 --target win --origin URL --license TXT]
  build-driver-store.py <store-dir> stats
Provenance (origin/license) is recorded per driver so an option-C mirror stays auditable and takedown-ready.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

ZSTD = shutil.which("zstd")


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _put_object(store, src):
    """zstd -19 the file into objects/<sha>.zst exactly once (dedup). Returns (sha, stored_bytes, was_new)."""
    sha = _sha(src)
    obj = os.path.join(store, "objects", sha[:2], sha + ".zst")
    if os.path.exists(obj):
        return sha, os.path.getsize(obj), False
    os.makedirs(os.path.dirname(obj), exist_ok=True)
    if not ZSTD:
        sys.exit("need the `zstd` tool (apt install zstd)")
    subprocess.run([ZSTD, "-19", "-q", "-T0", "-f", "-o", obj, src], check=True)
    return sha, os.path.getsize(obj), True


def add(store, name, folder, meta):
    files, logical, stored_new, added = [], 0, 0, 0
    for dp, _dn, fn in os.walk(folder):
        for f in fn:
            p = os.path.join(dp, f)
            if os.path.islink(p):
                continue
            rel = os.path.relpath(p, folder).replace("\\", "/")
            sz = os.path.getsize(p)
            sha, zsz, new = _put_object(store, p)
            files.append({"path": rel, "sha256": sha, "size": sz})
            logical += sz
            if new:
                stored_new += zsz
                added += 1
    man = {"name": name, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "files": sorted(files, key=lambda x: x["path"]), "logical_bytes": logical}
    man.update({k: v for k, v in meta.items() if v})
    md = os.path.join(store, "drivers")
    os.makedirs(md, exist_ok=True)
    json.dump(man, open(os.path.join(md, name + ".json"), "w"), indent=2)
    print(f"{name}: {len(files)} files, {added} new unique ({stored_new} B added zstd-19), "
          f"{logical} B logical -> stored once")
    return man


def stats(store):
    objs = 0
    comp = 0
    for dp, _dn, fn in os.walk(os.path.join(store, "objects")):
        for f in fn:
            objs += 1
            comp += os.path.getsize(os.path.join(dp, f))
    logical = 0
    drivers = 0
    seen_in_manifests = 0
    md = os.path.join(store, "drivers")
    if os.path.isdir(md):
        for f in os.listdir(md):
            if f.endswith(".json"):
                drivers += 1
                m = json.load(open(os.path.join(md, f)))
                logical += m.get("logical_bytes", 0)
                seen_in_manifests += len(m.get("files", []))
    ratio = (logical / comp) if comp else 0
    print(json.dumps({"drivers": drivers, "file_refs": seen_in_manifests, "unique_objects": objs,
                      "stored_bytes_zstd19": comp, "logical_bytes": logical,
                      "space_saving": f"{ratio:.1f}x (dedup + zstd-19)"}, indent=2))


def main(argv):
    if len(argv) < 3:
        sys.exit(__doc__)
    store, op = argv[1], argv[2]
    os.makedirs(store, exist_ok=True)
    if op == "add":
        name, folder = argv[3], argv[4]
        meta = {}
        a = argv[5:]
        for i in range(0, len(a) - 1, 2):
            if a[i].startswith("--"):
                meta[a[i][2:]] = a[i + 1]
        if "ids" in meta:
            meta["ids"] = [x for x in meta["ids"].split(",") if x]
        add(store, name, folder, meta)
    elif op == "stats":
        stats(store)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
