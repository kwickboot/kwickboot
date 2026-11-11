#!/usr/bin/env python3
"""kbctl - KwickBoot database CLI (called by pxectl, runs as the service user so WAL files stay pxe-owned).
SPDX-License-Identifier: AGPL-3.0-or-later
"""
import getpass
import hashlib
import json
import os
import re
import secrets
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import db  # noqa: E402
import endpoint  # noqa: E402

MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")
HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
ROLES = ("admin", "manager", "operator", "viewer")
WHO = "cli:" + (os.environ.get("SUDO_USER") or os.environ.get("KB_CALLER") or "root")


def die(msg):
    raise SystemExit(msg)


def mac_arg(s):
    m = (s or "").strip().lower().replace("-", ":")
    if not MAC_RE.match(m):
        die(f"bad MAC: {s}")
    return m


def known(mac):
    m = db.machine(mac)
    if not m:
        die(f"unknown machine {mac} (boot it with discovery open, or `pxectl approve <mac> <name>`)")
    return m


def ago(ts):
    if not ts:
        return "-"
    s = int(time.time() - ts)
    return f"{s}s" if s < 120 else f"{s // 60}m" if s < 7200 else f"{s // 3600}h"


def cmd_machines(a):
    rows = db.machines()
    print(f"{'MAC':17}  {'NAME':20} {'OK':3} {'DEFAULT':7} {'NEXT':10} {'AGENT':14} {'PREFLIGHT':10} LAST INVENTORY")
    for m in rows:
        _, inv = db.latest_report(m["mac"], "inventory")
        agent = f"{m['agent_state'] or '-'} {ago(m['agent_seen'])}" if m["agent_state"] else "-"
        print(f"{m['mac']:17}  {m['name'][:20]:20} {'yes' if m['approved'] else 'no':3} {m['default_action']:7} "
              f"{(m['next_action'] or '-')[:10]:10} {agent[:14]:14} {(m['preflight'] or '-')[:10]:10} {inv or '-'}")
    print(f"{len(rows)} machines")


def cmd_discover(a):
    if not a or a[0] == "off":
        db.set_setting("discovery_until", None)
        db.audit(WHO, "discovery", "closed")
        print("discovery closed")
        return
    if a[0] == "always":
        db.set_setting("discovery_until", "always")
        db.audit(WHO, "discovery", "set to ALWAYS open")
        print("discovery ALWAYS OPEN: every machine that network-boots gets the RAM OS, is registered and waits for approval")
        return
    mins = max(1, min(int(a[0]), 240))
    db.set_setting("discovery_until", time.time() + mins * 60)
    db.audit(WHO, "discovery", f"opened for {mins} min")
    print(f"discovery OPEN for {mins} min: any machine that network-boots now gets the RAM OS and is registered")


def cmd_discovery_status(a):
    v = db.get_setting("discovery_until", "0") or "0"
    if v == "always":
        print("  discovery: ALWAYS OPEN"); return
    u = float(v)
    print(f"  discovery: OPEN until {time.strftime('%H:%M:%S', time.localtime(u))}" if u > time.time() else "  discovery: closed")


def cmd_approve(a):
    if len(a) != 2 or not HOST_RE.match(a[1]):
        die("usage: pxectl approve <mac> <name>   (name: lowercase letters, digits, '-')")
    mac = mac_arg(a[0])
    db.upsert_machine(mac, name=a[1], approved=1)
    db.audit(WHO, "approve", f"{mac} as {a[1]}")
    print(f"approved {mac} as {a[1]}")


def cmd_deny(a):
    mac = mac_arg(a[0] if a else "")
    known(mac)
    db.upsert_machine(mac, approved=0, default_action="alpine")
    db.audit(WHO, "deny", mac)
    print(f"{mac} un-approved (record kept; RAM OS only).  Note: MACs listed in config.json are re-approved at start.")


def cmd_default(a):
    if len(a) != 2 or a[1] not in ("alpine", "local", "menu", "seat"):
        die("usage: pxectl default <mac> alpine|local|menu|seat   (seat = diskless gaming seat, linux/build-seat.sh)")
    mac = mac_arg(a[0])
    known(mac)
    db.upsert_machine(mac, default_action=a[1])
    db.audit(WHO, "default", f"{mac} -> {a[1]}")
    print(f"{mac} default -> {a[1]}")


def cmd_next(a):
    ok = ("alpine", "local", "menu", "debian13", "memtest", "netbootxyz", "shell", "windows", "seat")
    if len(a) != 2 or not (a[1] in ok or re.fullmatch(r"iso:[A-Za-z0-9._+-]+\.iso", a[1])):
        die("usage: pxectl next <mac> " + "|".join(ok) + "|iso:<file.iso>")
    mac = mac_arg(a[0])
    known(mac)
    db.upsert_machine(mac, next_action=a[1], next_used_at=None)
    db.audit(WHO, "next", f"{mac} -> {a[1]}")
    print(f"next boot of {mac} -> {a[1]} (one time, then back to its default)")


def cmd_job(a):
    if len(a) < 2 or a[1] not in ("inventory", "preflight", "install", "reboot", "poweroff"):
        die("usage: pxectl job <mac> inventory|preflight|install|reboot|poweroff [KEY=value ...]")
    mac = mac_arg(a[0])
    known(mac)
    params = {}
    for kv in a[2:]:
        if "=" not in kv:
            die(f"bad parameter {kv} (KEY=value)")
        k, v = kv.split("=", 1)
        params[k] = v
    jid = db.add_job(mac, a[1], params, WHO)
    db.audit(WHO, "job", f"{a[1]} queued for {mac} ({jid}) {params}")
    print("queued", jid, params)


def cmd_install(a):
    if len(a) < 2:
        die("usage: pxectl install <mac> <os> serial=<SERIAL> host=<name> [disk=/dev/x] [tz=..] [lan=..] [bundle=..] [locale=de_DE.UTF-8] [keymap=de] [tz=..] [finish=reboot|none]")
    mac, os_id = mac_arg(a[0]), a[1]
    m = known(mac)
    if not m["approved"]:
        die("approve the machine first: pxectl approve <mac> <name>")
    if not os.path.isfile(os.path.join(BASE, "http", "recipes", os_id + ".sh")):
        die(f"no recipe http/recipes/{os_id}.sh")
    kv = dict(x.split("=", 1) for x in a[2:] if "=" in x)
    ser, host = kv.get("serial", ""), kv.get("host", "").lower()
    if not ser or not HOST_RE.match(host):
        die("serial= and host= (lowercase hostname) are required")
    disk = kv.get("disk")
    if not disk:
        inv, _ = db.latest_report(mac, "inventory")
        hit = [l.split()[0] for l in inv.splitlines() if re.search(rf"SERIAL={re.escape(ser)}$", l)]
        if not hit:
            die(f"serial {ser} not in the last inventory of {mac} - run `pxectl job {mac} inventory` or give disk=")
        disk = "/dev/" + hit[0]
    params = {"OS": os_id, "DISK": disk, "CONFIRM": ser, "HOSTNAME": host, "FIN": kv.get("finish", "reboot")}
    for k in ("tz", "lan", "image", "sha256", "bundle", "locale", "keymap"):
        if kv.get(k):
            params[k.upper()] = kv[k]
    if kv.get("url"):
        import base64
        params["KIOSK_URL_B64"] = base64.b64encode(kv["url"].encode()).decode()
        if kv.get("allow"):
            params["KIOSK_ALLOW_B64"] = base64.b64encode(kv["allow"].encode()).decode()
        params["KIOSK_IDLE"] = kv.get("idle", "10") if kv.get("idle", "10").isdigit() else "10"
        params["KIOSK_RO"] = "0" if kv.get("ro") in ("0", "no", "false") else "1"
    rsrc = open(os.path.join(BASE, "http", "recipes", os_id + ".sh"), encoding="utf-8").read()
    if kv.get("variant"):                    # only values the recipe declares in RECIPE_VARIANTS="..."
        vs = (re.search(r'RECIPE_VARIANTS="([^"]*)"', rsrc) or [None, ""])[1].split()
        if kv["variant"] not in vs:
            die(f"variant= must be one of: {' '.join(vs) or '(this recipe has no variants)'}")
        params["VARIANT"] = kv["variant"]
    if "RECIPE_METHOD=image" not in rsrc and kv.get("managed", "yes") not in ("0", "no", "false"):   # web console management
        endpoint.init(); params["EP_TOKEN"] = endpoint.issue_token(mac)
    print(f"install {os_id} on {mac}: disk={disk} serial={ser} host={host} finish={params['FIN']} managed={'yes' if 'EP_TOKEN' in params else 'no'}")
    jid = db.add_job(mac, "install", params, WHO)
    db.audit(WHO, "INSTALL", f"{os_id} queued for {mac} on {disk} serial {ser} as {host} [{jid}]")
    print("queued", jid)


def cmd_localboot(a):
    """pxectl localboot <mac> auto|chainload|exit - how this machine is handed to its installed OS"""
    if len(a) < 2 or a[1] not in ("auto", "chainload", "exit"):
        die("usage: pxectl localboot <mac> auto|chainload|exit   (auto = disk GRUB config, else back to the firmware)")
    mac = mac_arg(a[0]); known(mac)
    db.upsert_machine(mac, local_boot=None if a[1] == "auto" else a[1])
    db.audit(WHO, "LOCALBOOT", f"{mac} -> {a[1]}")
    print(f"{mac}: local boot = {a[1]}")


def cmd_shell(a):
    """pxectl shell <mac> on|off - emergency console (Alt+F2, root shell) in this machine's RAM OS from its next boot"""
    if len(a) < 2 or a[1] not in ("on", "off"):
        die("usage: pxectl shell <mac> on|off   (a root shell on the machine's screen: anyone at it can wipe its disks)")
    mac = mac_arg(a[0]); known(mac)
    db.upsert_machine(mac, kb_shell=1 if a[1] == "on" else None)
    db.audit(WHO, "KBSHELL", f"{mac} emergency console {a[1]}")
    print(f"{mac}: emergency console {a[1]} (takes effect at the next RAM OS boot)")


def cmd_ep(a):
    """pxectl ep <mac> <command> [user=<name>] [admin=1]   - command for an installed, managed machine (runs within ~30 s)
       setpw/adduser ask for the password (typed twice, never shown or stored; only a SHA-512 hash goes to the machine)."""
    if len(a) < 2:
        die("usage: pxectl ep <mac> " + "|".join(endpoint.COMMANDS) + " [user=<name>] [admin=1]")
    mac, cmd = mac_arg(a[0]), a[1]
    m = known(mac)
    if not m.get("ep_token"):
        die("this machine is not managed by KwickBoot (no endpoint agent)")
    kv = dict(x.split("=", 1) for x in a[2:] if "=" in x)
    pw = ""
    if cmd in ("setpw", "adduser"):
        pw = getpass.getpass(f"new password for {kv.get('user', '?')}: ")
        if pw != getpass.getpass("again: "):
            die("passwords differ")
    try:
        arg = endpoint.make_arg(cmd, kv.get("user"), pw, kv.get("admin") == "1")
        endpoint.queue(mac, cmd, arg, WHO)
    except ValueError as e:
        die(str(e))
    db.audit(WHO, "ENDPOINT", f"{cmd}{' user ' + arg.split(':')[0] if arg else ''} -> {mac}")
    print(f"queued {cmd} for {mac} - the machine picks it up within 30 s (pxectl ep-status {mac})")


def cmd_ep_status(a):
    mac = mac_arg(a[0] if a else "")
    known(mac)
    st = endpoint.status(mac)
    print(f"managed={st['managed']} seen {st['seen_age']}s ago  info={json.dumps(st['info'])}")
    for c in st["commands"]:
        print(f"  {c['id']:>4} {c['created'][11:19]} {c['cmd']:<12} {c['arg'] or '':<12} "
              f"{'done ' + (c['result'] or '') if c['done_at'] else 'sent' if c['sent_at'] else 'waiting'}")


def cmd_jobs(a):
    mac = mac_arg(a[0] if a else "")
    for j in db.jobs(mac):
        print(f"{j['id']:34} {j['state']:8} rc={j['rc'] if j['rc'] is not None else '-':3} {j['created_by']:14} {j['error'] or ''}")


def cmd_lastjob(a):
    mac = mac_arg(a[0] if a else "")
    j = db.jobs(mac, 1)
    print(j[0]["id"] if j else "")


def cmd_agent(a):
    m = known(mac_arg(a[0] if a else ""))
    if not m["agent_state"]:
        print("no agent check-in yet")
    else:
        print(f"state={m['agent_state']} ip={m['agent_ip']} seen {ago(m['agent_seen'])} ago: {m['agent_msg']}")


def cmd_report(a):
    mac = mac_arg(a[0] if a else "")
    text, created = db.latest_report(mac, a[1] if len(a) > 1 else "inventory")
    print(f"# {created}\n{text}" if created else "no report")


def set_password(username):
    if os.environ.get("KB_PW_STDIN") == "1" and not sys.stdin.isatty():   # unattended (ISO installer's first boot)
        p1 = p2 = sys.stdin.readline().rstrip(chr(10))
    else:
        p1 = getpass.getpass(f"New password for {username} (min 10 chars): ")
        p2 = getpass.getpass("Repeat: ")
    if p1 != p2 or len(p1) < 10:
        die("passwords differ or too short - unchanged")
    salt, it = secrets.token_bytes(16), 300000
    h = hashlib.pbkdf2_hmac("sha256", p1.encode(), salt, it).hex()
    db.x("UPDATE users SET pw_hash=?, pw_salt=?, pw_iter=? WHERE username=?", h, salt.hex(), it, username)


def cmd_useradd(a):
    if len(a) != 2 or a[1] not in ROLES or not re.fullmatch(r"[a-z0-9._-]{2,40}", a[0]):
        die("usage: pxectl useradd <username> admin|manager|operator|viewer")
    if db.q1("SELECT 1 FROM users WHERE username=?", a[0]):
        die("user exists (use pxectl passwd / pxectl role)")
    db.x("INSERT INTO users(username, role, created) VALUES(?,?,?)", a[0], a[1], db.now())
    set_password(a[0])
    db.audit(WHO, "useradd", f"{a[0]} role {a[1]}")
    print(f"user {a[0]} ({a[1]}) created")


def cmd_passwd(a):
    user = a[0] if a else "admin"
    if not db.q1("SELECT 1 FROM users WHERE username=?", user):
        if user != "admin":
            die("no such user")
        db.x("INSERT INTO users(username, display, role, created) VALUES('admin','Administrator','admin',?)", db.now())
    set_password(user)
    db.audit(WHO, "passwd", user)
    print(f"password for {user} set")


def cmd_role(a):
    if len(a) != 2 or a[1] not in ROLES:
        die("usage: pxectl role <username> admin|manager|operator|viewer")
    if not db.x("UPDATE users SET role=? WHERE username=?", a[1], a[0]).rowcount:
        die("no such user")
    db.audit(WHO, "role", f"{a[0]} -> {a[1]}")
    print(f"{a[0]} is now {a[1]} (takes effect at next login)")


def cmd_userdel(a):
    if not a or not db.x("UPDATE users SET enabled=0 WHERE username=?", a[0]).rowcount:
        die("usage: pxectl userdel <username>")
    db.audit(WHO, "userdel", a[0])
    print(f"{a[0]} disabled (kept for the audit trail)")


def cmd_users(a):
    names = {d["id"]: d["name"] for d in db.departments()}
    for u in db.q("SELECT * FROM users ORDER BY username"):
        depts = ",".join(names.get(d, "?") for d in db.user_departments(u["id"])) or ("(all)" if u["role"] == "admin" else "-")
        print(f"{u['username']:20} {u['role']:9} {'enabled' if u['enabled'] else 'DISABLED':8} "
              f"{'pw' if u['pw_hash'] else 'NO PW':5} {depts[:30]:30} last login {u['last_login'] or '-'}")


def cmd_node_token(a):
    """Enable this server as a federation NODE: new join secret (shown once) + TLS fingerprint for the hub."""
    import federation
    federation.init(BASE)
    secret = federation.new_node_secret()
    federation.ensure_cert(os.uname().nodename)
    db.audit(WHO, "federation", "node join secret (re)created")
    print("Federation node enabled. Enter these on the HUB (Sites > Add site); the secret is shown only now:")
    print(f"  secret:      {secret}")
    print(f"  fingerprint: {federation.fingerprint()}")
    print("Then: pxectl restart, and allow TCP 8443 from the hub in the firewall.")


def cmd_node_info(a):
    import federation
    federation.init(BASE)
    if not federation.node_enabled():
        die("not a federation node (pxectl node-token)")
    print(f"node fingerprint: {federation.fingerprint()}")
    for n in federation.nodes():
        print(f"hub view - site {n['id']}: {n['name']} {n['url']} last ok {n['last_ok'] or '-'} {n['last_error'] or ''}")


def dept_arg(name):
    d = db.q1("SELECT * FROM departments WHERE name=? OR CAST(id AS TEXT)=?", name, name)
    if not d:
        die(f"unknown department '{name}' (pxectl depts)")
    return d


def cmd_depts(a):
    for d in db.departments():
        p = db.policy(d["id"])
        print(f"{d['id']:3} {d['name'][:28]:28} machines {d['machines']:5} users {d['users']:4}  "
              f"os={','.join(p['allowed_os']) or 'any'} bundles={','.join(p['allowed_bundles']) or 'any'} "
              f"prefix={p['hostname_prefix'] or '-'} installs>={p['install_min_role']}")
    n = db.q1("SELECT COUNT(*) AS n FROM machines WHERE department_id IS NULL")["n"]
    print(f"    {'(unassigned)':28} machines {n:5}")


def cmd_dept_add(a):
    if len(a) != 1 or not re.fullmatch(r"[\w .&()/-]{2,60}", a[0]):
        die('usage: pxectl dept-add "<name>"')
    if db.q1("SELECT 1 FROM departments WHERE name=?", a[0]):
        die("department exists")
    db.x("INSERT INTO departments(name, policy, created) VALUES(?,?,?)", a[0], json.dumps(db.POLICY_DEFAULT), db.now())
    db.audit(WHO, "dept-add", a[0])
    print(f"department '{a[0]}' created")


def cmd_dept_del(a):
    d = dept_arg(a[0] if a else "")
    if db.q1("SELECT 1 FROM machines WHERE department_id=?", d["id"]):
        die("move its machines first (pxectl assign <mac> <dept>|none)")
    db.x("DELETE FROM departments WHERE id=?", d["id"])
    db.audit(WHO, "dept-del", d["name"])
    print(f"department '{d['name']}' deleted")


def cmd_policy(a):
    """pxectl policy <dept> [os=a,b|any] [bundles=x,y|any] [prefix=lab-|none] [installs=operator|manager|admin]"""
    if not a:
        die("usage: pxectl policy <dept> [os=a,b|any] [bundles=x,y|any] [prefix=lab-|none] [installs=operator|manager|admin]")
    d = dept_arg(a[0])
    p = db.policy(d["id"])
    recipes = {f[:-3] for f in os.listdir(os.path.join(BASE, "http", "recipes")) if f.endswith(".sh")}
    for kv in a[1:]:
        k, _, v = kv.partition("=")
        if k == "os":
            p["allowed_os"] = [] if v == "any" else v.split(",")
            bad = set(p["allowed_os"]) - recipes
            if bad:
                die(f"unknown recipes: {', '.join(sorted(bad))}")
        elif k == "bundles":
            p["allowed_bundles"] = [] if v == "any" else v.split(",")
        elif k == "prefix":
            p["hostname_prefix"] = "" if v == "none" else v.lower()
            if p["hostname_prefix"] and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,30}", p["hostname_prefix"]):
                die("prefix: lowercase letters, digits, '-'")
        elif k == "installs":
            if v not in ("operator", "manager", "admin"):
                die("installs=operator|manager|admin")
            p["install_min_role"] = v
        else:
            die(f"unknown setting {k}")
    if len(a) > 1:
        db.x("UPDATE departments SET policy=? WHERE id=?", json.dumps(p), d["id"])
        db.audit(WHO, "policy", f"{d['name']} {p}")
    print(f"{d['name']}: {p}")


def cmd_assign(a):
    if len(a) != 2:
        die("usage: pxectl assign <mac> <dept>|none")
    mac = mac_arg(a[0])
    known(mac)
    d = None if a[1] == "none" else dept_arg(a[1])
    db.upsert_machine(mac, department_id=d["id"] if d else None)
    db.audit(WHO, "assign", f"{mac} -> {d['name'] if d else 'unassigned'}")
    print(f"{mac} -> {d['name'] if d else 'unassigned'}")


def cmd_user_depts(a):
    if len(a) < 1:
        die("usage: pxectl user-depts <username> [dept ...]   (no departments = none)")
    u = db.q1("SELECT * FROM users WHERE username=?", a[0])
    if not u:
        die("no such user")
    ids = [dept_arg(n)["id"] for n in a[1:]]
    db.set_user_departments(u["id"], ids)
    db.audit(WHO, "user-depts", f"{a[0]} -> {a[1:]}")
    print(f"{a[0]} departments: {', '.join(a[1:]) or 'none'}")


def cmd_audit(a):
    n = int(a[0]) if a else 30
    for r in reversed(db.q("SELECT * FROM audit ORDER BY id DESC LIMIT ?", n)):
        print(f"{r['ts']}  {r['who'][:16]:16} {(r['ip'] or ''):15} {r['detail'] or r['action']}")


def cmd_compat(a):
    """pxectl compat [status | local | share | off | worker <url> | preview <mac> | submit]
    Hardware-compatibility reporting (install outcomes + hardware class, NOT user data). LOCAL by default
    (nothing leaves); share is opt-in, no sign-up. See insights.py."""
    import insights
    op = a[0] if a else "status"
    if op == "status":
        print(json.dumps(insights.stats(), indent=2)); return
    if op in ("off", "local", "share"):
        ok, err = insights.set_mode(op)          # 'share' silently registers an upload key - no sign-up, no prompt
        if not ok:
            die(err)
        msg = {"off": "collection stopped", "local": "reports stay on THIS server (improve only your own advisor)",
               "share": "anonymised compatibility reports ALSO uploaded - `pxectl compat preview <mac>` shows exactly what leaves"}[op]
        extra = ""
        if op == "share":
            extra = "  [registered]" if insights.stats().get("registered") else "  [will register on first upload]"
        print(f"compat mode -> {op}  ({msg}){extra}"); return
    if op == "worker":
        if len(a) < 2:
            die("usage: pxectl compat worker <https-url>   (ingest Worker; your own for full sovereignty)")
        db.set_setting("insights_worker", a[1]); db.set_setting("insights_key", ""); print(f"compat worker -> {a[1]}"); return
    if op == "preview":
        if len(a) < 2:
            die("usage: pxectl compat preview <mac>   (shows the EXACT anonymised report - nothing else leaves)")
        print(json.dumps(insights.preview(mac_arg(a[1])), indent=2)); return
    if op == "submit":
        n, err = insights.submit_pending()
        print(f"shared {n} report(s)" + (f"; {err}" if err else "")); return
    if op == "pull":
        # give-to-get: upload this server's accumulated list, then fetch the latest known-list + signed driver catalog
        n, err = insights.submit_pending(force=True)
        print(f"contributed {n} report(s)" + (f"; {err}" if err else ""))
        insights.refresh_known(max_age=0)
        print(f"known-list: {len(insights._known_set())} signatures")
        try:
            import drivers_catalog
            ok, msg = drivers_catalog.sync(force=True)
            print(f"driver catalog: {msg}")
        except Exception as e:
            print(f"driver catalog: {e}")
        return
    die("usage: pxectl compat [status | local | share | off | worker <url> | preview <mac> | submit | pull]")


def cmd_san(a):
    import san
    san.init()
    if a and a[0] == "provision":
        if len(a) < 3:
            die("usage: pxectl san provision <lun> <https-url|image-file>")
        san.provision(a[1], a[2]); print(f"provisioned {a[1]} <- {a[2]}")
    elif not a or a[0] in ("list", "luns"):
        for l in san.luns():
            kind = "golden" if l["golden"] else ("clone" if l.get("parent") else "lun")
            print(f"#{l['num']:<3} {l['name']:<22} {l.get('backstore', 'file'):<6} {kind:<7} {l.get('mac') or '-'}")
    else:
        die("usage: pxectl san list | provision <lun> <https-url|image-file>")


CMDS = {k[4:].replace("_", "-"): v for k, v in globals().items() if k.startswith("cmd_")}

if __name__ == "__main__":
    db.init(os.path.join(BASE, "data", "kwickboot.db"))
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS:
        die("kbctl commands: " + " ".join(sorted(CMDS)))
    CMDS[sys.argv[1]](sys.argv[2:])
