"""san.py - KwickBoot as the SAN: serve the OS library as iSCSI LUNs (M1-M3).

A diskless rack/DC server gets BOTH its boot config and its OS from KwickBoot itself - no third-party SAN: a
LUN exported over iSCSI, booted by iPXE `sanboot iscsi:`. One golden image, cloned copy-on-write per server, is
the scalable DC pattern ("room = N servers from one golden image"). Backstores:
  * file  (default)  - a flat .img under $KB_SAN_DIR; clones use reflink (instant on btrfs/XFS) or a full copy.
  * zvol             - set $KB_SAN_ZPOOL; LUNs are ZFS zvols, clones are instant `zfs clone` of a snapshot.
The iSCSI target is managed with tgt (tgtadm; the pxe service user needs NOPASSWD sudo for /usr/sbin/tgtadm,
and /usr/sbin/zfs for zvol - see linux/kwickboot-san.sudoers). Same db, roles and audit chain as the rest of
KwickBoot - one open box, boot server AND SAN. An EXISTING SAN is reachable with action `san:ext:<iscsi-uri>`.
SPDX-License-Identifier: AGPL-3.0-or-later
"""
import os
import subprocess
import db

TARGET_IQN = "iqn.2026-11.com.kwickboot:san"
TID = "1"
SAN_DIR = os.environ.get("KB_SAN_DIR") or "/opt/pxe-toolkit/san/luns"
ZPOOL = (os.environ.get("KB_SAN_ZPOOL") or "").strip()     # ZFS pool for zvol backstore; empty = flat-file
DEFAULT_BACKSTORE = "zvol" if ZPOOL else "file"
# NVMe-oF/TCP export (M4): the SAME backing files, also served as NVMe namespaces. Set KB_SAN_NVME_IP to enable.
# (Serving is Linux nvmet-tcp; BOOTING over NVMe-oF needs firmware support - iPXE sanboots iSCSI, not NVMe-oF.)
NVMET = "/sys/kernel/config/nvmet"
NVME_NQN = "nqn.2026-11.com.kwickboot:san"
NVME_IP = (os.environ.get("KB_SAN_NVME_IP") or "").strip()
NVME_PORT = os.environ.get("KB_SAN_NVME_PORT") or "4420"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS san_lun (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, num INTEGER NOT NULL UNIQUE,
  size_mb INTEGER NOT NULL, path TEXT NOT NULL, backstore TEXT NOT NULL DEFAULT 'file',
  golden INTEGER NOT NULL DEFAULT 0, parent TEXT, mac TEXT, created TEXT, note TEXT);
"""


def init():
    for stmt in _SCHEMA.strip().split(";"):
        if stmt.strip():
            db.x(stmt)
    try:
        os.makedirs(SAN_DIR, exist_ok=True)
    except OSError:
        pass


def _run(argv):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        class _R:
            returncode = 1
            stdout = ""
            stderr = "command unavailable: %s" % (argv[0] if argv else "?")
        return _R()


def _tgt(*args):   # tgtadm needs root; pxe service user gets NOPASSWD sudo for /usr/sbin/tgtadm
    return _run(["sudo", "-n", "tgtadm", "--lld", "iscsi", *args])


def _zfs(*args):   # zvol backstore; NOPASSWD sudo for /usr/sbin/zfs
    return _run(["sudo", "-n", "zfs", *args])


def target_up():
    return _tgt("--op", "show", "--mode", "target").returncode == 0


def ensure_target():
    """Idempotent: create+bind the target and (re)register every known LUN by its path. False if tgtd is down."""
    show = _tgt("--op", "show", "--mode", "target")
    if show.returncode != 0:
        return False
    if TARGET_IQN not in show.stdout:
        _tgt("--op", "new", "--mode", "target", "--tid", TID, "-T", TARGET_IQN)
        _tgt("--op", "bind", "--mode", "target", "--tid", TID, "-I", "ALL")
    have = _tgt("--op", "show", "--mode", "target").stdout
    for r in db.q("SELECT num, path FROM san_lun"):
        if ("LUN: %d" % r["num"]) not in have:
            _tgt("--op", "new", "--mode", "logicalunit", "--tid", TID, "--lun", str(r["num"]), "-b", r["path"])
    ensure_nvmet()
    return True


def _valid_name(name):
    name = (name or "").strip()
    return name if name and all(c.isalnum() or c in "-_" for c in name) else ""


def _next_num():
    return (db.q1("SELECT COALESCE(MAX(num),0)+1 AS n FROM san_lun") or {}).get("n") or 1


def _alloc_backing(name, size_mb, backstore):
    """Create the backing store and return its path. file -> sparse .img; zvol -> /dev/zvol/<pool>/<name>."""
    if backstore == "zvol":
        if not ZPOOL:
            raise ValueError("zvol backstore needs KB_SAN_ZPOOL")
        r = _zfs("create", "-s", "-V", "%dM" % size_mb, "%s/%s" % (ZPOOL, name))
        if r.returncode != 0:
            raise ValueError("zfs create failed: %s" % (r.stderr or "").strip())
        return "/dev/zvol/%s/%s" % (ZPOOL, name)
    os.makedirs(SAN_DIR, exist_ok=True)
    path = os.path.join(SAN_DIR, name + ".img")
    with open(path, "wb") as f:
        f.truncate(int(size_mb) * 1024 * 1024)
    return path


def create_lun(name, size_mb, golden=0, backstore=None, note=""):
    name = _valid_name(name)
    if not name:
        raise ValueError("lun name must be alphanumeric (plus - _)")
    if db.q1("SELECT 1 FROM san_lun WHERE name=?", name):
        raise ValueError("lun already exists")
    size_mb = int(size_mb)
    if not (16 <= size_mb <= 8_000_000):
        raise ValueError("size must be 16 MB .. 8 TB")
    backstore = backstore or DEFAULT_BACKSTORE
    num = _next_num()
    path = _alloc_backing(name, size_mb, backstore)
    db.x("INSERT INTO san_lun(name,num,size_mb,path,backstore,golden,parent,created,note) VALUES(?,?,?,?,?,?,?,?,?)",
         name, num, size_mb, path, backstore, 1 if golden else 0, None, db.now(), note or "")
    ensure_target()
    return num


def clone(golden_name, new_name, mac=None, note=""):
    """Copy-on-write clone of a golden LUN into a per-server LUN. Instant on zvol / reflink filesystems."""
    g = db.q1("SELECT * FROM san_lun WHERE name=?", golden_name)
    if not g:
        raise ValueError("no such golden lun")
    new_name = _valid_name(new_name)
    if not new_name:
        raise ValueError("clone name must be alphanumeric (plus - _)")
    if db.q1("SELECT 1 FROM san_lun WHERE name=?", new_name):
        raise ValueError("clone name already exists")
    num = _next_num()
    bs = g["backstore"]
    if bs == "zvol":
        snap = "%s/%s@base" % (ZPOOL, golden_name)
        _zfs("snapshot", snap)                               # ok if it already exists
        r = _zfs("clone", snap, "%s/%s" % (ZPOOL, new_name))
        if r.returncode != 0:
            raise ValueError("zfs clone failed: %s" % (r.stderr or "").strip())
        path = "/dev/zvol/%s/%s" % (ZPOOL, new_name)
    else:
        path = os.path.join(SAN_DIR, new_name + ".img")
        r = subprocess.run(["cp", "--reflink=auto", g["path"], path], capture_output=True, text=True)
        if r.returncode != 0:                                # fs without reflink: fall back to a full copy
            subprocess.run(["cp", g["path"], path])
    db.x("INSERT INTO san_lun(name,num,size_mb,path,backstore,golden,parent,created,note) VALUES(?,?,?,?,?,?,?,?,?)",
         new_name, num, g["size_mb"], path, bs, 0, golden_name, db.now(), note or "")
    ensure_target()
    if mac:
        assign(mac, new_name)
    return num


def assign(mac, name):
    mac = (mac or "").lower().strip()
    if not db.q1("SELECT 1 FROM san_lun WHERE name=?", name):
        raise ValueError("no such lun")
    db.x("UPDATE san_lun SET mac=NULL WHERE mac=?", mac)
    db.x("UPDATE san_lun SET mac=? WHERE name=?", mac, name)


def lun_for_mac(mac):
    return db.q1("SELECT * FROM san_lun WHERE mac=?", (mac or "").lower().strip())


def sanboot_line(server_ip, lun_num):
    # iPXE parses the LUN field of a sanboot iSCSI URI as HEX (scsi_parse_lun -> strtoul base 16), while tgt numbers
    # LUNs in decimal. So the decimal LUN number must be emitted as hex, or LUN >= 10 attaches the WRONG disk
    # (e.g. decimal 10 -> iPXE reads 0x10 = 16). LUNs 1-9 are identical in both bases, which hid this until LUN 10.
    return "sanboot iscsi:%s:::%x:%s" % (server_ip, int(lun_num), TARGET_IQN)


def ipxe_boot(action_arg, server_ip):
    """iPXE lines for action 'san:<lun-name>'. External SAN: 'san:ext:<iscsi-uri>'."""
    arg = (action_arg or "").strip()
    if arg.startswith("ext:"):
        return ["echo KwickBoot SAN: booting from an external target...", "sanboot " + arg[4:]]
    r = db.q1("SELECT num FROM san_lun WHERE name=?", arg)
    if not r:
        return ["echo KwickBoot SAN: no such LUN '%s'" % arg, "sleep 5", "exit"]
    ensure_target()
    return ["echo KwickBoot SAN: booting from iSCSI LUN %s..." % arg, sanboot_line(server_ip, r["num"])]


def luns():
    return db.q("SELECT name,num,size_mb,backstore,golden,parent,mac,created,note FROM san_lun ORDER BY num")


def delete_lun(name):
    r = db.q1("SELECT num,path,backstore FROM san_lun WHERE name=?", name)
    if not r:
        return
    _tgt("--op", "delete", "--mode", "logicalunit", "--tid", TID, "--lun", str(r["num"]))
    _nvmet_del(r["num"])
    if r["backstore"] == "zvol":
        _zfs("destroy", "-r", "%s/%s" % (ZPOOL, name))       # -r also removes the @base snapshot / clones
    else:
        try:
            os.remove(r["path"])
        except OSError:
            pass
    db.x("DELETE FROM san_lun WHERE name=?", name)


def _sh(cmd):          # a small root shell snippet (configfs needs root)
    return _run(["sudo", "-n", "sh", "-c", cmd])


def nvmet_ready():
    if not NVME_IP:
        return False
    if os.path.isdir(NVMET):
        return True
    return _sh("modprobe nvmet nvmet-tcp 2>/dev/null; [ -d %s ]" % NVMET).returncode == 0


def ensure_nvmet():
    """Also export every LUN over NVMe-oF/TCP from the SAME backing files. No-op unless KB_SAN_NVME_IP is set."""
    if not nvmet_ready():
        return False
    sub = "%s/subsystems/%s" % (NVMET, NVME_NQN)
    _sh("mkdir -p %s && echo 1 > %s/attr_allow_any_host" % (sub, sub))
    for r in db.q("SELECT num, path FROM san_lun"):
        ns = "%s/namespaces/%d" % (sub, r["num"])
        _sh("mkdir -p %s && printf '%%s' '%s' > %s/device_path && echo 1 > %s/enable" % (ns, r["path"], ns, ns))
    port = "%s/ports/1" % NVMET
    _sh("mkdir -p %s; echo ipv4 > %s/addr_adrfam; echo tcp > %s/addr_trtype; printf '%%s' '%s' > %s/addr_traddr; "
        "echo %s > %s/addr_trsvcid; ln -sf %s %s/subsystems/%s 2>/dev/null"
        % (port, port, port, NVME_IP, port, NVME_PORT, port, sub, port, NVME_NQN))
    return True


def _nvmet_del(num):
    if not NVME_IP or not os.path.isdir(NVMET):
        return
    ns = "%s/subsystems/%s/namespaces/%d" % (NVMET, NVME_NQN, num)
    _sh("[ -d %s ] && { echo 0 > %s/enable 2>/dev/null; rmdir %s 2>/dev/null; }" % (ns, ns, ns))


def provision(name, src):
    """Write a raw OS image (local path or http(s) URL) onto a LUN's backing - seeds a golden master."""
    r = db.q1("SELECT path FROM san_lun WHERE name=?", name)
    if not r:
        raise ValueError("no such lun")
    path = r["path"]
    # A flat-file LUN we can already write needs no root; only a zvol/device backing needs sudo.
    dd = ["dd"] if os.access(path, os.W_OK) else ["sudo", "-n", "dd"]
    if src.startswith(("http://", "https://")):
        p1 = subprocess.Popen(["curl", "-fsSL", src], stdout=subprocess.PIPE)
        p2 = subprocess.run(dd + ["of=" + path, "bs=4M", "conv=notrunc"], stdin=p1.stdout)
        if p1.stdout:
            p1.stdout.close()
        p1.wait()
        if p2.returncode != 0:
            raise ValueError("provision: write failed")
    else:
        if not os.path.exists(src):
            raise ValueError("source image not found")
        if subprocess.run(dd + ["if=" + src, "of=" + path, "bs=4M", "conv=notrunc"]).returncode != 0:
            raise ValueError("provision: dd failed")
    ensure_target()
    return True


def selftest():
    """create golden -> clone -> sanboot line -> assign -> external passthrough -> cleanup. (ok, msg)."""
    import random
    g = "goldtest%d" % random.randint(1000, 9999)
    c = g + "clone"
    try:
        gn = create_lun(g, 16, golden=1, backstore="file", note="selftest golden")
        cn = clone(g, c, mac="de:ad:be:ef:00:02")
        assert cn != gn, "clone got a distinct LUN number"
        row = db.q1("SELECT parent,golden FROM san_lun WHERE name=?", c)
        assert row and row["parent"] == g and not row["golden"], "clone parent/golden flags"
        line = sanboot_line("10.0.0.1", cn)
        assert line == "sanboot iscsi:10.0.0.1:::%x:%s" % (cn, TARGET_IQN), line
        # regression for the iPXE hex-LUN bug: LUN >= 10 must be emitted as HEX in the sanboot URI, never decimal
        assert sanboot_line("10.0.0.1", 10) == "sanboot iscsi:10.0.0.1:::a:%s" % TARGET_IQN, "LUN 10 must be hex 'a'"
        assert sanboot_line("10.0.0.1", 26) == "sanboot iscsi:10.0.0.1:::1a:%s" % TARGET_IQN, "LUN 26 must be hex '1a'"
        assert (lun_for_mac("de:ad:be:ef:00:02") or {}).get("name") == c, "assign/lun_for_mac"
        ext = ipxe_boot("ext:iscsi:1.2.3.4:::1:iqn.x:y", "10.0.0.1")
        assert ext[-1] == "sanboot iscsi:1.2.3.4:::1:iqn.x:y", "external-SAN passthrough"
        up = target_up()
        delete_lun(c)
        delete_lun(g)
        assert not db.q1("SELECT 1 FROM san_lun WHERE name IN (?,?)", g, c), "cleanup"
        return True, "OK: golden#%d, clone#%d (parent ok), sanboot line, assign, external passthrough, tgtd=%s, cleanup" % (gn, cn, "up" if up else "down")
    except Exception as e:  # noqa: BLE001
        for nm in (c, g):
            try:
                delete_lun(nm)
            except Exception:
                pass
        return False, "FAIL: %s" % e


if __name__ == "__main__":
    init()
    ok, msg = selftest()
    print(("PASS " if ok else "FAIL ") + msg)
