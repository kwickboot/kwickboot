"""KwickBoot out-of-band control: Redfish (iLO, iDRAC, XCC, Supermicro, OpenBMC) and IPMI (ipmitool).
SPDX-License-Identifier: AGPL-3.0-or-later

- BMC settings (type, host, user, TLS pin) live in machines.bmc (JSON); passwords live ONLY in
  data/bmc-secrets.json (mode 0600, service user) - never in the database, API responses or logs.
- Redfish over HTTPS: BMCs use self-signed certificates, so the first successful connection pins the
  certificate's SHA-256; any later change is refused until an admin re-pins (trust on first use).
- IPMI: ipmitool -I lanplus, password passed via environment (-E), never on the command line.
"""
import base64
import hashlib
import http.client
import json
import os
import re
import shutil
import ssl
import subprocess
import threading

import db

SECRETS = None
_lock = threading.Lock()
HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}(:\d{1,5})?$|^\[[0-9a-fA-F:]+\](:\d{1,5})?$")
POWER = {"on": ("On", "on"), "off": ("ForceOff", "off"), "shutdown": ("GracefulShutdown", "soft"),
         "reset": ("ForceRestart", "reset"), "cycle": ("PowerCycle", "cycle")}


class BMCError(Exception):
    pass


def init(base):
    global SECRETS
    SECRETS = os.path.join(base, "data", "bmc-secrets.json")


def _secrets():
    try:
        with open(SECRETS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_secrets(d):
    tmp = SECRETS + ".tmp"
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, SECRETS)


def config(mac):
    m = db.machine(mac) or {}
    try:
        return json.loads(m.get("bmc") or "null")
    except ValueError:
        return None


def public(mac):
    """BMC settings safe to show in the UI (no password)."""
    c = config(mac)
    if not c:
        return None
    return {"type": c["type"], "host": c["host"], "user": c["user"], "pinned": bool(c.get("pin")),
            "has_password": mac in _secrets(), "vnc": c.get("vnc") or "",
            "consoles": (["text"] if c["type"] == "ipmi" else []) + (["screen"] if c.get("vnc") else [])}


def set_config(mac, typ, host, user, password=None, vnc=None):
    """typ: redfish | ipmi (power, boot, sensors) | amt (Intel AMT: hardware screen only, via its VNC port).
    vnc: host:port of a VNC console the server can reach (Intel AMT KVM: <amt-ip>:5900) - its password is typed in the
    browser at connect time and never stored."""
    if typ not in ("redfish", "ipmi", "amt"):
        raise BMCError("type must be redfish, ipmi or amt")
    if not HOST_RE.match(host or ""):
        raise BMCError("host: name or IP address (optionally :port)")
    if typ != "amt" and not re.fullmatch(r"[\w.@-]{1,64}", user or ""):
        raise BMCError("user: letters, digits, . _ @ -")
    if typ == "amt" and not vnc:
        vnc = host.split(":")[0] + ":5900"           # AMT's KVM on the standard VNC port
    if vnc and not (HOST_RE.match(vnc) and ":" in vnc):
        raise BMCError("VNC console: host:port, e.g. 192.168.0.50:5900")
    old = config(mac) or {}
    pin = old.get("pin") if old.get("host") == host else None     # new host -> pin again
    db.upsert_machine(mac, bmc=json.dumps({"type": typ, "host": host, "user": user or "", "pin": pin, "vnc": vnc or ""}))
    if password:
        with _lock:
            s = _secrets()
            s[mac] = password
            _save_secrets(s)


def remove(mac):
    db.upsert_machine(mac, bmc=None)
    with _lock:
        s = _secrets()
        s.pop(mac, None)
        _save_secrets(s)


def repin(mac):
    c = config(mac)
    if c:
        c["pin"] = None
        db.upsert_machine(mac, bmc=json.dumps(c))


# ---------------------------------------------------------------- Redfish
def _rf(mac, method, path, body=None):
    c = config(mac)
    pw = _secrets().get(mac)
    if not c or not pw:
        raise BMCError("BMC not configured (host, user and password)")
    host, _, port = c["host"].partition(":")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE          # self-signed BMC certs: verified by the pin below instead
    conn = http.client.HTTPSConnection(host.strip("[]"), int(port or 443), timeout=20, context=ctx)
    try:
        conn.connect()
        fp = hashlib.sha256(conn.sock.getpeercert(binary_form=True)).hexdigest()
        if c.get("pin") and c["pin"] != fp:
            raise BMCError("BMC certificate CHANGED since it was first trusted - refusing. If the BMC was "
                           "re-flashed or got a new certificate, an admin can re-trust it.")
        auth = base64.b64encode(f"{c['user']}:{pw}".encode()).decode()
        data = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=data, headers={"Authorization": "Basic " + auth, "Accept": "application/json",
                                                       **({"Content-Type": "application/json"} if data else {})})
        r = conn.getresponse()
        raw = r.read()
        if r.status == 401:
            raise BMCError("BMC rejected the user name or password")
        if r.status >= 400:
            msg = ""
            try:
                msg = json.loads(raw)["error"]["@Message.ExtendedInfo"][0]["Message"]
            except Exception:
                msg = raw[:200].decode(errors="replace")
            raise BMCError(f"BMC error {r.status}: {msg}")
        if not c.get("pin"):                  # trust on first successful, authenticated use
            c["pin"] = fp
            db.upsert_machine(mac, bmc=json.dumps(c))
        return json.loads(raw) if raw.strip() else {}
    except (OSError, ssl.SSLError) as e:
        raise BMCError(f"cannot reach BMC {c['host']}: {e}")
    finally:
        conn.close()


def _rf_system(mac):
    coll = _rf(mac, "GET", "/redfish/v1/Systems")
    members = coll.get("Members") or []
    if not members:
        raise BMCError("BMC lists no systems")
    path = members[0]["@odata.id"]
    return path, _rf(mac, "GET", path)


# ---------------------------------------------------------------- IPMI
def sol_command(mac):
    """(argv, env, deactivate) for IPMI serial-over-LAN - the machine's text console (BIOS, boot loader, OS console).
    The password goes in the environment (-E), never on the command line."""
    c = config(mac)
    pw = _secrets().get(mac)
    if not c or c.get("type") != "ipmi" or not pw:
        raise BMCError("the text console needs an IPMI BMC with its password saved")
    tool = shutil.which("ipmitool")
    if not tool:
        raise BMCError("ipmitool is not installed on the KwickBoot server (apt install ipmitool)")
    host, _, port = c["host"].partition(":")
    base = [tool, "-I", "lanplus", "-H", host, "-U", c["user"], "-E"] + (["-p", port] if port else [])
    env = {"IPMI_PASSWORD": pw, "PATH": "/usr/bin:/bin", "TERM": "xterm-256color"}

    def deactivate():            # free the BMC's single SOL slot when the browser closes
        subprocess.run(base + ["sol", "deactivate"], capture_output=True, timeout=15, env=env)
    return base + ["sol", "activate"], env, deactivate


def _ipmi(mac, *args):
    c = config(mac)
    if c and c.get("type") == "amt":
        raise BMCError("Intel AMT is set up for its hardware screen only (power: the buttons under the screen)")
    pw = _secrets().get(mac)
    if not c or not pw:
        raise BMCError("BMC not configured (host, user and password)")
    tool = shutil.which("ipmitool")
    if not tool:
        raise BMCError("ipmitool is not installed on the KwickBoot server (apt install ipmitool)")
    host, _, port = c["host"].partition(":")
    cmd = [tool, "-I", "lanplus", "-H", host, "-U", c["user"], "-E"] + (["-p", port] if port else []) + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env={"IPMI_PASSWORD": pw, "PATH": "/usr/bin:/bin"})
    except subprocess.TimeoutExpired:
        raise BMCError("BMC did not answer (IPMI timeout)")
    if r.returncode != 0:
        raise BMCError("IPMI: " + (r.stderr.strip().splitlines() or ["failed"])[-1][:200])
    return r.stdout


# ---------------------------------------------------------------- operations
def status(mac):
    c = config(mac)
    if not c:
        raise BMCError("no BMC configured")
    if c["type"] == "redfish":
        _, s = _rf_system(mac)
        boot = s.get("Boot") or {}
        return {"power": s.get("PowerState", "?"), "health": (s.get("Status") or {}).get("Health", ""),
                "model": " ".join(x for x in (s.get("Manufacturer"), s.get("Model")) if x), "serial": s.get("SerialNumber", ""),
                "bios": s.get("BiosVersion", ""), "boot_override": f"{boot.get('BootSourceOverrideTarget', '')} ({boot.get('BootSourceOverrideEnabled', '')})",
                "console": f"https://{c['host']}/"}
    out = _ipmi(mac, "chassis", "status")
    pw = re.search(r"System Power\s*:\s*(\w+)", out)
    return {"power": pw.group(1).capitalize() if pw else "?", "health": "", "model": "", "serial": "", "bios": "",
            "boot_override": "", "console": f"https://{c['host'].split(':')[0]}/"}


def power(mac, op):
    if op not in POWER:
        raise BMCError("power operation: on, off, shutdown, reset, cycle")
    c = config(mac)
    if not c:
        raise BMCError("no BMC configured")
    if c["type"] == "redfish":
        path, s = _rf_system(mac)
        allowed = (((s.get("Actions") or {}).get("#ComputerSystem.Reset") or {}).get("ResetType@Redfish.AllowableValues")) or []
        rt = POWER[op][0]
        if op == "cycle" and allowed and "PowerCycle" not in allowed:
            rt = "ForceRestart"
        _rf(mac, "POST", path + "/Actions/ComputerSystem.Reset", {"ResetType": rt})
        return rt
    _ipmi(mac, "chassis", "power", POWER[op][1])
    return POWER[op][1]


def pxe_once(mac):
    """Next boot from the network (once) - combined with the server's per-machine action this boots the RAM OS."""
    c = config(mac)
    if not c:
        raise BMCError("no BMC configured")
    if c["type"] == "redfish":
        path, _ = _rf_system(mac)
        _rf(mac, "PATCH", path, {"Boot": {"BootSourceOverrideTarget": "Pxe", "BootSourceOverrideEnabled": "Once"}})
    else:
        _ipmi(mac, "chassis", "bootdev", "pxe", "options=efiboot")


def sensors(mac):
    c = config(mac)
    if not c:
        raise BMCError("no BMC configured")
    rows = []
    if c["type"] == "redfish":
        ch = (_rf(mac, "GET", "/redfish/v1/Chassis").get("Members") or [])
        if ch:
            base = ch[0]["@odata.id"]
            try:
                th = _rf(mac, "GET", base + "/Thermal")
                for t in th.get("Temperatures", []):
                    if t.get("ReadingCelsius") is not None:
                        rows.append({"name": t.get("Name", "temp"), "value": f"{t['ReadingCelsius']} °C", "state": (t.get("Status") or {}).get("Health", "")})
                for f in th.get("Fans", []):
                    v = f.get("Reading", f.get("ReadingRPM"))
                    if v is not None:
                        rows.append({"name": f.get("Name", f.get("FanName", "fan")), "value": f"{v} {f.get('ReadingUnits', 'RPM')}", "state": (f.get("Status") or {}).get("Health", "")})
            except BMCError:
                pass
            try:
                pw = _rf(mac, "GET", base + "/Power")
                for p in pw.get("PowerControl", []):
                    if p.get("PowerConsumedWatts") is not None:
                        rows.append({"name": p.get("Name", "power"), "value": f"{p['PowerConsumedWatts']} W", "state": ""})
                for p in pw.get("PowerSupplies", []):
                    rows.append({"name": p.get("Name", "PSU"), "value": (p.get("Status") or {}).get("State", ""), "state": (p.get("Status") or {}).get("Health", "")})
            except BMCError:
                pass
        return rows
    for line in _ipmi(mac, "sdr", "elist").splitlines():
        p = [x.strip() for x in line.split("|")]
        if len(p) >= 5 and p[4] and p[4] != "No Reading":
            rows.append({"name": p[0], "value": p[4], "state": p[2]})
    return rows[:60]


def events(mac):
    c = config(mac)
    if not c:
        raise BMCError("no BMC configured")
    if c["type"] == "ipmi":
        return [l for l in _ipmi(mac, "sel", "elist", "last", "25").splitlines() if l.strip()]
    mg = (_rf(mac, "GET", "/redfish/v1/Managers").get("Members") or [])
    if not mg:
        return []
    ls = _rf(mac, "GET", mg[0]["@odata.id"] + "/LogServices").get("Members") or []
    out = []
    for svc in ls[:2]:
        try:
            ent = _rf(mac, "GET", svc["@odata.id"] + "/Entries")
            for e in (ent.get("Members") or [])[-25:]:
                out.append(f"{e.get('Created', '')}  {e.get('Severity', '')}  {e.get('Message', '')}")
        except BMCError:
            continue
    return out[-25:]
