"""KwickBoot hardware health + asset facts from a RAM OS inventory report (pure parsing, no I/O).
SPDX-License-Identifier: AGPL-3.0-or-later

parse(text) -> {"asset": {...}, "disks": [...], "battery": [...], "memory": {...}, "flags": [...], "level": "ok|warn|bad"}
Thresholds are deliberately conservative: "bad" = replace now, "warn" = plan a replacement.
"""
import re


def _kv(line):
    return dict(re.findall(r"(\w+)=(\S*)", line))


def _section(text, name):
    m = re.search(r"^\[" + re.escape(name) + r"[^\]]*\]\n(.*?)(?=^\[|\Z)", text, re.S | re.M)
    return m.group(1) if m else ""


def parse(text):
    text = text or ""
    out = {"asset": {}, "disks": [], "battery": [], "memory": {}, "flags": [], "level": "ok"}
    flag = lambda lvl, msg: out["flags"].append({"level": lvl, "msg": msg})

    sysl = _section(text, "system").splitlines()
    if sysl:
        a = _kv(sysl[0])
        out["asset"].update(vendor=a.get("vendor", ""), product=a.get("product", ""))
        if len(sysl) > 1:
            b = _kv(sysl[1])
            out["asset"].update(serial=b.get("serial", ""), uuid=b.get("uuid", ""), chassis=b.get("chassis", ""))
        m = re.search(r"product=(.*?) version=", sysl[0])
        if m:
            out["asset"]["product"] = m.group(1).strip()
        m = re.search(r"vendor=(.*?) product=", sysl[0])
        if m:
            out["asset"]["vendor"] = m.group(1).strip()
    bios = _section(text, "bios")
    m = re.search(r"vendor=(.*?) version=(.*?) date=(\S*)", bios)
    if m:
        out["asset"].update(bios_vendor=m.group(1).strip(), bios_version=m.group(2).strip(), bios_date=m.group(3))
    out["asset"]["boot_mode"] = "UEFI" if "mode=UEFI" in bios else "BIOS" if "legacy BIOS" in bios else ""
    sb = re.search(r"secureboot=(\S+)", bios)
    out["asset"]["secure_boot"] = {"1": "on", "0": "off"}.get(sb.group(1), "unknown") if sb else ""
    cpu = re.search(r"Model name:\s*(.+)", text)
    out["asset"]["cpu"] = cpu.group(1).strip() if cpu else ""

    mem = _section(text, "memory")
    t = re.search(r"total=([\d.]+) GB", mem)
    out["memory"]["total_gb"] = float(t.group(1)) if t else None
    ce = sum(int(x) for x in re.findall(r"corrected=(\d+)", mem))
    ue = sum(int(x) for x in re.findall(r"uncorrected=(\d+)", mem))
    out["memory"].update(ecc_corrected=ce, ecc_uncorrected=ue, ecc="EDAC mc" in mem)
    if ue:
        flag("bad", f"memory: {ue} uncorrectable ECC errors - replace the DIMM")
    elif ce > 100:
        flag("warn", f"memory: {ce} corrected ECC errors - a DIMM is degrading")

    disks = _section(text, "disks")
    cur = None
    for line in disks.splitlines():
        m = re.match(r"\s{2}(\S+)\s+(\S+)\s+tran=(\S*)\s+rota=(\S*)\s+model=(\S*)\s+SERIAL=(\S+)", line)
        if m:
            cur = {"dev": m.group(1), "size": m.group(2), "tran": m.group(3), "rotational": m.group(4) == "1",
                   "model": m.group(5).replace("_", " "), "serial": m.group(6), "smart": None}
            out["disks"].append(cur)
            continue
        if not cur:
            continue
        l = line.strip()
        if re.search(r"overall-health|SMART Health Status", l):
            cur["smart"] = "PASSED" if re.search(r"PASSED|OK", l) else "FAILED"
        n = lambda: int(re.findall(r"(\d+)", l.replace(",", ""))[-1]) if re.findall(r"\d+", l) else None
        if re.search(r"Power_On_Hours|Power On Hours", l):
            cur["power_on_hours"] = n()
        elif "Reallocated_Sector" in l:
            cur["reallocated"] = n()
        elif "Current_Pending" in l:
            cur["pending"] = n()
        elif "Offline_Uncorrectable" in l:
            cur["uncorrectable"] = n()
        elif "Percentage Used" in l:
            cur["wear_pct"] = n()
        elif "Media and Data Integrity" in l:
            cur["media_errors"] = n()
        elif "Available Spare:" in l:
            cur["spare_pct"] = n()
        elif re.search(r"Temperature_Celsius|^Temperature:", l):
            nums = re.findall(r"\b(\d{2,3})\b", l)
            cur["temp_c"] = int(nums[0] if "Temperature:" in l else (re.search(r"-\s+(\d+)", l) or [None, nums[-1] if nums else 0])[1]) if nums else None
    for d in out["disks"]:
        name = f"disk {d['dev']} ({d['model'] or d['tran']}, {d['serial']})"
        if d["tran"] == "usb":
            continue
        if d["smart"] == "FAILED":
            flag("bad", f"{name}: SMART health FAILED - replace now")
        if (d.get("reallocated") or 0) > 0 or (d.get("pending") or 0) > 0 or (d.get("uncorrectable") or 0) > 0:
            flag("bad" if (d.get("pending") or 0) + (d.get("uncorrectable") or 0) > 0 else "warn",
                 f"{name}: {d.get('reallocated') or 0} reallocated / {d.get('pending') or 0} pending / {d.get('uncorrectable') or 0} uncorrectable sectors")
        if (d.get("media_errors") or 0) > 0:
            flag("warn", f"{name}: {d['media_errors']} media/data integrity errors")
        if d.get("wear_pct") is not None and d["wear_pct"] >= 80:
            flag("bad" if d["wear_pct"] >= 100 else "warn", f"{name}: SSD wear {d['wear_pct']}% used")
        if d.get("spare_pct") is not None and d["spare_pct"] < 20:
            flag("warn", f"{name}: only {d['spare_pct']}% spare blocks left")
        if (d.get("power_on_hours") or 0) > 50000 and d["rotational"]:
            flag("warn", f"{name}: {d['power_on_hours']} power-on hours (over 5.7 years)")
        if (d.get("temp_c") or 0) >= 60:
            flag("warn", f"{name}: {d['temp_c']} °C")

    for line in _section(text, "battery").splitlines():
        m = re.match(r"\s+(BAT\d+): health=(\S+)% cycles=(\S*)", line)
        if m:
            hp = int(m.group(2)) if m.group(2).isdigit() else None
            b = {"name": m.group(1), "health_pct": hp, "cycles": int(m.group(3)) if m.group(3).isdigit() else None}
            out["battery"].append(b)
            if hp is not None and hp < 50:
                flag("bad", f"battery {b['name']}: {hp}% of design capacity - replace")
            elif hp is not None and hp < 70:
                flag("warn", f"battery {b['name']}: {hp}% of design capacity")

    for m in re.finditer(r"^\s+(Package id \d+|Tctl|Tdie|CPU|Core \d+):\s+\+([\d.]+)°C", _section(text, "sensors"), re.M):
        if float(m.group(2)) >= 90:
            flag("warn", f"temperature {m.group(1)} {m.group(2)} °C while idle - check cooling")
            break
    if _section(text, "firmware / hardware errors").strip() and re.search(r"(?i)mce|hardware error|i/o error", _section(text, "firmware / hardware errors")):
        flag("warn", "kernel reported hardware errors (see Hardware tab)")

    lv = {f["level"] for f in out["flags"]}
    out["level"] = "bad" if "bad" in lv else "warn" if "warn" in lv else "ok"
    return out


if __name__ == "__main__":
    import json
    import sys
    print(json.dumps(parse(open(sys.argv[1], encoding="utf-8", errors="replace").read()), indent=2))


# ---------------------------------------------------------------- tuning tips (not failures): what would make this PC
# faster or easier to manage. Each: {"area", "level": "tip"|"warn", "msg", "fix"}. Virtual machines get no hardware tips.
_VM_VENDORS = ("qemu", "vmware", "innotek", "xen", "microsoft corporation virtual", "parallels", "bochs")


def tune(text):
    text = text or ""
    tips = []
    add = lambda area, lvl, msg, fix: tips.append({"area": area, "level": lvl, "msg": msg, "fix": fix})  # noqa: E731
    sysl = _section(text, "system").splitlines()
    a = _kv(sysl[0]) if sysl else {}
    vm_line = (sysl[0] if sysl else "").lower()
    is_vm = any(v in vm_line for v in _VM_VENDORS) or "virtual machine" in vm_line

    # memory: one module where the board has two or more slots -> single channel
    mem = _section(text, "memory")
    mods = [ln for ln in mem.splitlines() if re.match(r"\s+size=\d+\s*[MG]B", ln)]
    slots = re.search(r"Number Of Devices:\s*(\d+)", mem)
    if not is_vm and len(mods) == 1 and slots and int(slots.group(1)) >= 2:
        add("memory", "tip", "one memory module in a board with " + slots.group(1) + " slots: memory runs single-channel",
            "add a matching module (same size and speed) - up to twice the memory bandwidth, a big gain with built-in graphics")

    # storage: the system disk on a spinning disk; SATA links below their maximum
    disks = [ln for ln in _section(text, "disks").splitlines() if "rota=" in ln and "tran=usb" not in ln]
    if not is_vm and disks:
        def gb(ln):
            m = re.search(r"\s(\d+(?:\.\d+)?)([GT])\s", ln)
            return float(m.group(1)) * (1024 if m.group(2) == "T" else 1) if m else 0
        main = max(disks, key=gb)
        if "rota=1" in main:
            add("storage", "tip", f"the main disk ({main.split()[0]}) is a hard disk",
                "an SSD is the biggest single speed-up for a PC (boot, updates, apps) - KwickBoot reinstalls it in minutes")
    for ln in _section(text, "sata-links").splitlines():
        m = re.match(r"\s+(\S+)\s.*?(\d+\.\d+) Gb/s \(current: (\d+\.\d+) Gb/s\)", ln)
        if m and float(m.group(3)) < float(m.group(2)):
            add("storage", "warn", f"{m.group(1)} links at {m.group(3)} Gb/s, it supports {m.group(2)} Gb/s",
                "use a SATA 3 (6 Gb/s) port and a good SATA cable - the disk runs at half speed now")

    # network: gigabit card linked slower; Wake-on-LAN off
    for ln in _section(text, "network").splitlines():
        m = re.match(r"\s+(\S+) mac=", ln)
        if not m or is_vm:
            continue
        k = _kv(ln)
        sp = int(re.sub(r"\D", "", k.get("speed", "")) or 0)
        mx = int(re.sub(r"\D", "", k.get("max", "")) or 0)
        if k.get("link") == "up" and 0 < sp < 1000 <= mx:
            add("network", "warn", f"{m.group(1)} linked at {sp} Mb/s, the card supports {mx} Mb/s",
                "replace the patch cable (gigabit needs all 8 wires) or check the switch port - installs and images run 10x slower")
        wol = k.get("wol", "-")
        if k.get("link") == "up" and wol not in ("-", "") and "g" not in wol:
            add("network", "tip", f"Wake-on-LAN is off on {m.group(1)}",
                "turn on Wake-on-LAN (and off 'deep sleep / ErP') in the firmware so KwickBoot can power this PC on for scheduled installs")

    # firmware: boot mode, Secure Boot, TPM, virtualization, firmware age, RAID/VMD storage mode
    sec = _kv(_section(text, "security").strip().splitlines()[0]) if _section(text, "security").strip() else {}
    bios = _section(text, "bios")
    dbl = re.search(r"sb_db=(.*)", bios)
    kek = re.search(r"sb_KEK=(.*)", bios)
    if dbl and "mode=UEFI" in bios and not is_vm:
        has23 = "2023" in dbl.group(1)
        if not has23:
            add("firmware", "tip", "Secure Boot trusts only Microsoft's 2011 certificates (no 2023 CA in the firmware)",
                "install the PC maker's firmware update or Windows' Secure Boot certificate update: boot loaders signed only with "
                "the 2023 CAs will not start on this PC" + ("" if (kek and "2023" in kek.group(1)) else " (the 2023 KEK is missing too)"))
    if "mode=legacy" in bios:
        add("firmware", "tip", "the PC boots in legacy BIOS mode",
            "switch the firmware to UEFI: needed for Windows 11, Secure Boot and the fastest network boot")
    if not is_vm and sec.get("tpm") in ("none", "0", "", "1.2"):
        add("firmware", "tip", "no TPM 2.0 visible", "enable the firmware TPM (Intel PTT / AMD fTPM) - Windows 11 and BitLocker need it")
    if not is_vm and sec.get("virt") == "none":
        add("firmware", "tip", "CPU virtualization (VT-x / AMD-V) is off or missing",
            "enable it in the firmware if this PC runs virtual machines, WSL or Windows security features (VBS)")
    m = re.search(r"date=(\d{2})/(\d{2})/(\d{4})", bios)
    if m and not is_vm:
        import datetime
        age = (datetime.date.today() - datetime.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))).days / 365.25
        if age >= 4:
            add("firmware", "tip", f"the firmware is from {m.group(3)} ({age:.0f} years old)",
                "check the PC maker's site for a firmware update (security and stability fixes, newer CPU microcode)")
    for ln in _section(text, "pci-ids").splitlines():
        f = ln.split()
        if len(f) >= 4 and f[1].lower() == "0104" and f[2].lower() == "8086":
            add("storage", "tip", "the storage controller runs in Intel RAID / RST (VMD) mode",
                "for a single disk, AHCI mode in the firmware is simpler and faster to deploy; KwickBoot's Windows installs bring the driver if you keep RAID")
            break
    return tips


# ---------------------------------------------------------------- Windows 11 readiness (one row per PC, a lane to act on)
def _cpu_win11(cpu):
    """True / False / None (unknown: check Microsoft's list) for the CPU name."""
    c = cpu or ""
    m = re.search(r"Core\(TM\) i[3579]-(\d{4,5})", c)
    if m:
        n = m.group(1)
        return int(n[:2]) >= 10 if len(n) == 5 else int(n[0]) >= 8
    if re.search(r"Core\(TM\) Ultra|Core\(TM\) [3579] \d{3}|Intel\(R\) Core\(TM\) [3579] ", c):
        return True
    m = re.search(r"Ryzen (?:Threadripper )?(?:PRO )?[3579] (\d)\d{3}", c)
    if m:
        return int(m.group(1)) >= 2
    if re.search(r"Ryzen (?:AI )?[3579] (?:PRO )?\d{3}", c):   # Ryzen 7 7840U style / AI 300
        return True
    if re.search(r"Core\(TM\)2|Pentium\(R\) [DM4]|Athlon\(tm\) (?:64|II)|Phenom", c):
        return False
    return None


def readiness(text, health_level=""):
    """{"lane": upgrade|second-life|retire, "checks": {...}, "why": [...]} for Windows 11."""
    text = text or ""
    p = parse(text)
    sec = _kv(_section(text, "security").strip().splitlines()[0]) if _section(text, "security").strip() else {}
    bios = _section(text, "bios")
    a = p["asset"]
    ram = p["memory"].get("total_gb") or 0
    disks = [d for d in p["disks"] if d.get("tran") != "usb"]
    def gb(s):
        m = re.match(r"([\d.]+)([GT])", str(s or ""))
        return float(m.group(1)) * (1024 if m.group(2) == "T" else 1) if m else 0
    big = max((gb(d.get("size")) for d in disks), default=0)
    cpu_ok = _cpu_win11(a.get("cpu"))
    tpm2 = sec.get("tpm") == "2"
    lvl = sec.get("cpu_level", "")
    checks = {
        "cpu": "yes" if cpu_ok else "no" if cpu_ok is False else "check list",
        "tpm2": "yes" if tpm2 else "no",
        "uefi": "yes" if "mode=UEFI" in bios else "switch to UEFI" if "legacy" in bios else "?",
        "secure_boot": a.get("secure_boot") or "?",
        "sb_2023": "yes" if "2023" in (re.search(r"sb_db=(.*)", bios) or [None, ""])[1] else "no" if "sb_db=" in bios else "?",
        "ram_gb": ram, "disk_gb": round(big), "cpu_level": lvl,
    }
    why = []
    if cpu_ok is False:
        why.append("CPU not supported by Windows 11")
    if cpu_ok is None:
        why.append("CPU: check Microsoft's supported list")
    if not tpm2:
        why.append("no TPM 2.0 (or it is switched off in the firmware)")
    if ram and ram < 3.8:
        why.append(f"{ram:.0f} GB RAM (needs 4 GB)")
    if big and big < 64:
        why.append(f"largest disk {big:.0f} GB (needs 64 GB)")
    if lvl in ("x86-64", "x86-64-v1"):
        why.append("CPU lacks the instructions Windows 11 24H2 requires (POPCNT/SSE4.2)")
    hl = health_level or p["level"]
    if hl == "bad":
        lane = "retire"
        why.insert(0, "hardware is failing (see Health)")
    elif not why or why == ["CPU: check Microsoft's supported list"]:
        lane = "upgrade"
        if checks["uefi"] != "yes":
            why.append("switch the firmware to UEFI first")
    else:
        lane = "second-life"
        why.append("healthy: reuse with Linux / ChromeOS Flex / kiosk, or keep Windows 10 on ESU")
    return {"lane": lane, "checks": checks, "why": why}
