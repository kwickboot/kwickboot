"""advisor.py - the KwickBoot install advisor: the right system for THIS machine, from its real hardware.
SPDX-License-Identifier: AGPL-3.0-or-later

Layer 1 (always, offline, deterministic): the RAM OS inventory + preflight -> a fact sheet -> rules per catalog system
(what it is for, what it needs) -> ranked candidates with reasons, excluded systems with the reason, findings
(failing disk, low RAM, worn battery, data that would be erased...), the disk to use.
Layer 2 (optional, ai_plan): a model with the customer's own key reasons over the same facts and the request; it may only
pick from the catalog. Both are PROPOSALS: a person applies them through the normal install form (all checks apply).
"""
import re

import health

# what each catalog system is for and what it needs (min RAM in GB; flags)
KNOW = {
    "ubuntu2404-desktop": {"roles": ["desktop", "office", "lab"], "ram": 4, "note": "GNOME desktop with an app bundle; widest hardware support"},
    "debian13":           {"roles": ["server", "desktop", "lab"], "ram": 1, "note": "lean and stable; also fits old or small machines"},
    "ubuntu2404-server":  {"roles": ["server"], "ram": 2, "note": "server with long vendor support"},
    "chromeos-flex":      {"roles": ["desktop", "office"], "ram": 4, "uefi": True, "note": "managed from Google Admin; nothing to maintain locally"},
    "proxmox-ve":         {"roles": ["virtualization", "server"], "ram": 8, "virt": True, "note": "virtual machines and containers"},
    "truenas":            {"roles": ["nas"], "ram": 8, "sb_off": True, "note": "ZFS storage server"},
    "haos":               {"roles": ["home-automation"], "ram": 2, "note": "Home Assistant appliance"},
    "jellyfin":           {"roles": ["media"], "ram": 2, "note": "media server"},
    "libreelec":          {"roles": ["media-tv"], "ram": 2, "note": "Kodi on a TV"},
    "opnsense":           {"roles": ["firewall"], "ram": 2, "nics": 2, "bios": True, "note": "firewall/router (needs 2 network ports)"},
    "openwrt":            {"roles": ["firewall"], "ram": 1, "nics": 2, "note": "small router OS"},
    "freebsd":            {"roles": ["server"], "ram": 1, "note": "BSD server"},
    "kiosk":              {"roles": ["kiosk"], "ram": 2, "note": "one locked web page"},
    "gaming-seat":        {"roles": ["gaming"], "ram": 8, "gpu": True, "note": "diskless Steam seat"},
}
PURPOSES = {"desktop": "Desktop / office PC", "lab": "School or training lab PC", "server": "Server",
            "virtualization": "Virtual machine host", "nas": "File server / NAS", "media": "Media server",
            "media-tv": "TV / media player", "home-automation": "Home automation", "firewall": "Firewall / router",
            "kiosk": "Kiosk / signage", "gaming": "Gaming seat (netbar)"}


def _section(text, name):
    m = re.search(r"^\[" + re.escape(name) + r"[^\]]*\]\n(.*?)(?=^\[|\Z)", text or "", re.S | re.M)
    return m.group(1) if m else ""


def _size_gb(s):
    m = re.match(r"([\d.]+)([KMGT])", s or "")
    return float(m.group(1)) * {"K": 1e-6, "M": 1e-3, "G": 1, "T": 1e3}[m.group(2)] if m else 0


def facts(inventory, preflight=""):
    """The fact sheet the rules (and the AI layer) work from - small, factual, no secrets."""
    h = health.parse(inventory)
    sec = dict(re.findall(r"(\w+)=(\S+)", _section(inventory, "security")))
    cpu = _section(inventory, "cpu")
    threads = re.search(r"CPU\(s\):\s+(\d+)", cpu)
    gpu = [l.strip() for l in _section(inventory, "gpu").splitlines() if l.strip() and ("VGA" in l or "3D" in l or "Display" in l)]
    gpu_drv = re.findall(r"driver=(\S+)", _section(inventory, "gpu"))
    nics = [l for l in _section(inventory, "network").splitlines() if re.search(r"^\s+(en|eth)\S*", l)]
    nodrv = [l.strip() for l in _section(inventory, "pci devices without a driver").splitlines() if l.strip()]
    installed = [l.strip() for l in _section(inventory, "installed systems").splitlines() if l.strip()]
    bat = h.get("battery") or []
    disks = []
    for d in h["disks"]:
        bad = [f["msg"] for f in h["flags"] if d["serial"] in f["msg"]]
        disks.append({"dev": "/dev/" + d["dev"], "size": d["size"], "gb": round(_size_gb(d["size"]), 1),
                      "kind": "USB" if d["tran"] == "usb" else "NVMe" if d["dev"].startswith("nvme") else "HDD" if d["rotational"] else "SSD",
                      "model": d["model"], "serial": d["serial"], "smart": d.get("smart"), "problems": bad})
    return {"vendor": h["asset"].get("vendor"), "product": h["asset"].get("product"), "cpu": h["asset"].get("cpu"),
            "threads": int(threads.group(1)) if threads else None, "ram_gb": h["memory"].get("total_gb"),
            "boot": sec.get("boot"), "secure_boot": sec.get("secure_boot"), "tpm": sec.get("tpm"),
            "cpu_level": sec.get("cpu_level"), "virt": sec.get("virt"),
            "gpu": gpu[:2], "gpu_driver": gpu_drv[:2], "nics": len(nics), "disks": disks,
            "battery_wear": [b.get("wear_pct") for b in bat if b.get("wear_pct") is not None],
            "missing_drivers": nodrv[:6], "installed": installed[:8], "health_flags": [f["msg"] for f in h["flags"]][:8],
            "preflight": (re.findall(r"RESULT: (.*?) =", preflight) or [""])[-1]}


def rules(f, catalog, purpose="auto"):
    """Deterministic advice. catalog: [{"id", "name", "min_gb", ...}] (only systems that exist on this server).
    We provide the tool, the user makes the choice: nothing is refused for being unwise (old hardware, low RAM,
    Windows 11 on an unsupported PC, overclocking, a worn disk) - it is ranked lower and every caveat is spelled out.
    Only physical impossibilities exclude a system (no disk it fits on)."""
    cat = {c["id"]: c for c in catalog}
    findings = []
    add = lambda lvl, text, ev: findings.append({"level": lvl, "text": text, "evidence": ev})
    ram = f.get("ram_gb") or 0
    # ---- findings about the machine itself
    for d in f["disks"]:
        for p in d["problems"]:
            add("bad", f"{d['dev']} ({d['model'] or d['kind']}) looks unhealthy - back up, and consider replacing it before installing on it", p)
    if ram and ram < 4:
        add("warn", f"Only {ram:g} GB RAM: a desktop will feel slow; a light system or more memory helps", f"memory total={ram:g} GB")
    for w in f.get("battery_wear") or []:
        if w >= 40:
            add("warn", f"Battery {w}% worn: replace it if this is a mobile machine", f"battery wear {w}%")
    if f.get("missing_drivers"):
        add("info", "Some devices have no driver in the RAM OS (often Wi-Fi or card readers); installed systems usually bring them", "; ".join(f["missing_drivers"][:3]))
    if f.get("installed"):
        add("warn", "There is an existing system on this machine - an install erases the chosen disk", "; ".join(f["installed"][:3]))
    # Windows 11: always possible through Manual install with the customer's own ISO; unsupported hardware is a warning, not a no
    known = f.get("boot") is not None                                 # inventories from before the [security] section
    gaps = [g for g, bad in (("TPM 2.0", f.get("tpm") != "2"), ("UEFI", f.get("boot") == "bios"), ("4 GB RAM", ram < 4),
                             ("Secure Boot capable firmware", f.get("boot") == "bios"),
                             ("a CPU from Microsoft's list (x86-64-v2 or newer at least)", f.get("cpu_level") == "x86-64-v1")) if bad]
    if not known:
        add("info", "Windows 11 readiness unknown - rescan the hardware (Overview, Scan) to check TPM, UEFI and CPU level", "inventory has no [security] section")
    elif not gaps:
        add("info", "Windows 11: hardware requirements look met - install it via Manual install with your own ISO",
            f"tpm={f.get('tpm')} boot={f.get('boot')} ram={ram:g} GB cpu={f.get('cpu_level')}")
    else:
        add("warn", "Windows 11 is possible via Manual install, but this PC is outside Microsoft's requirements (missing: " + ", ".join(dict.fromkeys(gaps)) +
            "). Setup checks them: in Setup press Shift+F10 and add the LabConfig keys BypassTPMCheck / BypassSecureBootCheck / BypassCPUCheck / BypassRAMCheck "
            "(HKLM\\SYSTEM\\Setup\\LabConfig, DWORD 1), or use an ISO prepared to skip the checks. Microsoft does not support such installs and may hold back updates; "
            "Windows 10 LTSC or Linux are alternatives. Your choice.",
            f"tpm={f.get('tpm')} boot={f.get('boot')} ram={ram:g} GB cpu={f.get('cpu_level')}")
    if f.get("overclock"):
        add("info", "The CPU runs above its rated clock (overclocked). Installs proceed as usual - run the stress test in Diagnostics first if stability matters", f.get("overclock"))
    # ---- the disk to use: healthy internal first (NVMe > SSD > HDD), then anything else - with a caveat
    order = {"NVMe": 0, "SSD": 1, "HDD": 2}
    disks = sorted(f["disks"], key=lambda d: (bool(d["problems"]), d["kind"] == "USB", order.get(d["kind"], 3), -d["gb"]))
    want = PURPOSES.keys() if purpose == "auto" else [purpose]
    cands, excluded = [], []
    for sid, k in KNOW.items():
        if sid not in cat or not any(r in want for r in k["roles"]):
            continue
        need_gb = cat[sid].get("min_gb") or 8
        disk = next((d for d in disks if d["gb"] >= need_gb), None)
        if not disk:                                   # physics: the system does not fit on any disk here
            excluded.append({"os": sid, "name": cat[sid]["name"], "why": [f"needs a disk of {need_gb} GB or more - the largest here is "
                                                                           f"{max([d['size'] for d in f['disks']] or ['none'])}"]})
            continue
        caveats, penalty = [], 0
        def cav(text, cost):
            nonlocal penalty
            caveats.append(text); penalty += cost
        if ram and ram < k["ram"]:
            cav(f"recommends {k['ram']} GB RAM, this machine has {ram:g} GB - it will run, but slower", 25)
        if k.get("uefi") and f.get("boot") == "bios":
            cav("expects UEFI boot - switch the firmware from legacy/CSM to UEFI first", 30)
        if k.get("bios") and f.get("boot") == "uefi":
            cav("its image boots legacy BIOS only - switch the firmware to legacy/CSM boot first", 30)
        if k.get("virt") and f.get("virt") == "none":
            cav("CPU virtualization (VT-x/AMD-V) is off or missing - enable it in the firmware setup, or VMs will not start", 30)
        if k.get("sb_off") and f.get("secure_boot") == "on":
            cav("not signed for Secure Boot - turn Secure Boot off in the firmware setup, or it will not start", 20)
        if k.get("nics") and (f.get("nics") or 0) < k["nics"]:
            cav(f"wants {k['nics']} network ports, this machine has {f.get('nics') or 0} - add a USB or PCIe network adapter, or use VLANs", 25)
        if k.get("gpu") and not any(x in (f.get("gpu_driver") or []) for x in ("i915", "xe", "amdgpu", "radeon", "nouveau", "nvidia")):
            cav("no graphics card with a driver was found - games will be slow or will not start", 30)
        if disk["problems"]:
            cav(f"the only disk big enough ({disk['dev']}) looks unhealthy: " + "; ".join(disk["problems"]), 40)
        elif disk["kind"] == "USB":
            cav(f"the disk is a USB disk ({disk['dev']}) - fine for testing, slower and less reliable than an internal one", 15)
        score = 50 + min(30, int((ram - k["ram"]) * 5)) + (10 if disk["kind"] in ("NVMe", "SSD") else 0) - penalty
        reasons = [k["note"], f"{ram:g} GB RAM (recommended {k['ram']})", f"disk {disk['dev']}: {disk['kind']} {disk['size']} {disk['model']}".strip()]
        if purpose == "auto":
            score -= 10 * k["roles"].index(next(r for r in k["roles"] if r in want))   # its main role first
        cands.append({"os": sid, "name": cat[sid]["name"], "score": score, "disk": disk["dev"], "reasons": reasons,
                      "caveats": caveats, "force": bool(caveats)})
    cands.sort(key=lambda c: (bool(c["caveats"]), -c["score"]))
    return {"purpose": purpose, "recommended": cands[0] if cands else None, "alternatives": cands[1:8],
            "excluded": excluded, "findings": findings, "facts": f, "source": "rules"}
