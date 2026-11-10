"""multicast.py - one golden-image stream feeds a whole room (udpcast's udp-sender on the server, udp-receiver in the
RAM OS of every machine). SPDX-License-Identifier: AGPL-3.0-or-later

A session sends the image's parts one after another, in manifest order, on the LAN interface only. Each part waits
until every selected machine has joined (or MAX_WAIT seconds, then goes with those present - a machine that missed it
fetches that part by unicast instead, see http/scripts/restore.sh). Machines verify every part's SHA-256 either way.
"""
import json
import os
import secrets
import subprocess
import threading
import time

MAX_WAIT = 180            # seconds a part waits for all machines before sending to those present
PORTS = range(9000, 9200, 2)   # udpcast uses portbase and portbase+1
_sessions = {}            # sid -> {"name", "macs", "port", "state", "part", "started", "log"}
_lock = threading.Lock()


def _free_port():
    with _lock:
        used = {s["port"] for s in _sessions.values() if s["state"] in ("waiting", "sending")}
    return next((p for p in PORTS if p not in used), None)


def start(name, macs, iface, http_root, log=print, publish=None):
    """Start a session for image `name` to `macs`. Returns (sid, portbase) - the restore jobs need the portbase."""
    d = os.path.join(http_root, "images", "captures", name)
    manifest = json.load(open(os.path.join(d, "manifest.json"), encoding="utf-8"))
    files = [os.path.join(d, p["file"]) for p in manifest.get("parts", []) if p.get("file")]
    return start_files(name, files, macs, iface, log, publish)


def link_rate(iface):
    """udp-sender --max-bitrate: 90 % of the link the PCs are on (1G -> 900m, 2.5G -> 2250m, 10G -> 9000m)."""
    try:
        mbit = int(open(f"/sys/class/net/{iface}/speed").read().strip())
        if mbit > 0:
            return f"{int(mbit * 0.9)}m"
    except (OSError, ValueError, TypeError):
        pass
    return "900m"


def start_files(name, files, macs, iface, log=print, publish=None):
    """Send these files (absolute paths), one after another, to `macs`. Also used for a prepared Windows image."""
    port = _free_port()
    if port is None:
        raise RuntimeError("too many multicast sessions at once")
    sid = secrets.token_hex(6)
    s = {"sid": sid, "name": name, "macs": list(macs), "port": port, "state": "waiting", "part": "",
         "started": time.time(), "log": []}
    with _lock:
        _sessions[sid] = s

    def run():
        for i, f in enumerate(files, 1):
            s["state"], s["part"] = "sending", f"{i}/{len(files)} {os.path.basename(f)}"
            if publish:
                publish({"t": "multicast", "sid": sid, "name": name, "state": s["state"], "part": s["part"]})
            cmd = ["udp-sender", "--file", f, "--portbase", str(port), "--nokbd", "--no-progress",
                   "--min-receivers", str(len(macs)), "--min-wait", "3", "--max-wait", str(MAX_WAIT),
                   "--retries-until-drop", "30", "--max-bitrate", link_rate(iface), "--full-duplex",
                   "--start-timeout", str(MAX_WAIT + 60)]          # nobody joined: give up (machines fetch directly)
            if iface:
                cmd[1:1] = ["--interface", iface]          # this LAN only
            t0 = time.time()
            try:
                p = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=MAX_WAIT + 3600)
                rc, tail = p.returncode, (p.stderr or p.stdout).strip().splitlines()[-1:] or [""]
            except (OSError, subprocess.TimeoutExpired) as e:
                rc, tail = -1, [str(e)]
            mb = os.path.getsize(f) / 1e6
            line = f"part {i}/{len(files)} {os.path.basename(f)}: {mb:.0f} MB rc={rc} in {time.time() - t0:.0f}s {tail[0][:120]}"
            s["log"].append(line)
            log(f"[multicast] {name} {sid}: {line}")
        s["state"] = "done"
        if publish:
            publish({"t": "multicast", "sid": sid, "name": name, "state": "done", "part": ""})

    threading.Thread(target=run, daemon=True).start()
    log(f"[multicast] {name} {sid}: started for {len(macs)} machines on portbase {port}"
        f"{' via ' + iface if iface else ''}, {len(files)} parts")
    return sid, port


def sessions():
    with _lock:
        return [dict(s) for s in _sessions.values()]
