#!/usr/bin/env python3
# NIC coverage gate: nicgate.py <Core lib/modules/KV dir> <nic-must-have.txt>
# Exits 1 listing every must-have chip the Core cannot drive. A chip is covered when a module alias in the Core's
# modules.alias matches its PCI modalias (class 02/00 = Ethernet), exactly as the kernel matches it. Drivers built into
# the kernel (=y) publish no PCI aliases, so a list line may end in "=<module>": then that module must be present in
# the Core, either as a loadable .ko (modules.dep) or built in (modules.builtin).
import fnmatch, os, re, sys
kdir, list_file = sys.argv[1], sys.argv[2]
pats = []
for line in open(os.path.join(kdir, "modules.alias")):
    p = line.split()
    if len(p) == 3 and p[0] == "alias" and p[1].startswith("pci:"):
        pats.append(p[1].upper())
present = set()
for f in ("modules.dep", "modules.builtin"):
    fp = os.path.join(kdir, f)
    if os.path.exists(fp):
        for line in open(fp):
            m = line.split(":")[0].strip()
            if m:
                present.add(re.sub(r"\.ko(\.\w+)?$", "", os.path.basename(m)).replace("-", "_"))
need, miss, gaps, closed = 0, [], [], []
for line in open(list_file):
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    gap = line.startswith("~")   # "~" = known gap we cannot fix ourselves (e.g. Debian does not build the driver): reported, not fatal
    pid, rest = (line.lstrip("~").split(None, 1) + [""])[:2]
    mod = None
    if " =" in rest:
        rest, mod = rest.rsplit(" =", 1)
        mod = mod.strip().replace("-", "_")
    if not gap:
        need += 1
    if mod:
        ok = mod in present
    else:
        v, d = pid.upper().split(":")
        ma = f"PCI:V0000{v}D0000{d}SV00000000SD00000000BC02SC00I00"
        ok = any(fnmatch.fnmatchcase(ma, p) for p in pats)
    if gap:
        (closed if ok else gaps).append(f"  {pid}  {rest.strip()}")
    elif not ok:
        miss.append(f"  {pid}  {rest.strip()}")
print(f"NIC coverage: {need - len(miss)}/{need} must-have chips covered" + (f", {len(gaps)} known gap(s)" if gaps else ""))
if gaps:
    print("Known gaps (not fatal):"); print("\n".join(gaps))
if closed:
    print("Known gaps NOW COVERED - remove the '~' in nic-must-have.txt:"); print("\n".join(closed))
if miss:
    print("NOT covered:")
    print("\n".join(miss))
    sys.exit(1)
