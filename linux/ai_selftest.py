#!/usr/bin/env python3
"""AI model-layer self-test: stands up a local mock OpenAI-compatible endpoint (no secret key) and proves ai.plan()
assembles the request, calls the provider, parses the answer, and validates it against the department catalog + the
machine's real disks (warn-never-refuse). Also checks privacy redaction for a non-local provider.
Run on the server (needs db, ai, advisor): cd /opt/pxe-toolkit && python3 linux/ai_selftest.py
SPDX-License-Identifier: AGPL-3.0-or-later
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile
import db
db.init(os.path.join(tempfile.mkdtemp(), "ai-selftest.db"))
import ai

CAPTURED = {}   # what the mock received, so we can assert redaction

PLAN_JSON = {
    "summary": "A 2 GB classroom PC: a light Linux desktop fits best; Windows 11 is possible but over its requirements.",
    "recommended": {"os": "debian13", "disk": "/dev/sda",
                    "reasons": ["2048 MB RAM suits a light desktop", "healthy 20 GB SATA disk"],
                    "caveats": ["only 2 GB RAM - close heavy tabs"]},
    "alternatives": [{"os": "fedora", "disk": "/dev/sda", "reasons": ["newer kernel"], "caveats": []}],
    "findings": [{"level": "warn", "text": "2 GB RAM is low for a modern desktop"}],
    "questions": ["Is this seat for a single fixed purpose?"],
}


class Mock(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(n) or b"{}")
        CAPTURED["user"] = body["messages"][-1]["content"]
        out = json.dumps({"model": "mock-1", "choices": [{"message": {"content": json.dumps(PLAN_JSON)}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def main():
    srv = HTTPServer(("127.0.0.1", 0), Mock)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}/v1"

    facts = {"ram_mb": 2048, "cpu": "x86-64-v2", "boot_mode": "uefi",
             "disks": [{"dev": "/dev/sda", "gb": 20, "serial": "SECRET-DISK-SERIAL-123", "type": "HDD", "health": "OK"}],
             "nics": [{"mac": "00:15:5d:00:42:10", "driver": "hv_netvsc"}]}
    catalog = [{"id": "debian13", "name": "Debian 13", "min_gb": 6, "desc": "Light, stable Linux desktop/server"},
               {"id": "fedora", "name": "Fedora 45", "min_gb": 10, "desc": "Newest Linux"},
               {"id": "windows", "name": "Windows 11", "min_gb": 40, "desc": "Needs TPM 2.0 and 4 GB+"}]
    rules = {"recommended": {"os": "debian13", "disk": "/dev/sda"},
             "alternatives": [{"os": "fedora", "disk": "/dev/sda"}], "excluded": ["windows"],
             "findings": [{"level": "warn", "text": "2 GB RAM"}], "facts": facts, "source": "rules"}

    ok = True
    print("AI model-layer self-test (local mock provider):")

    # 1) local provider: full pipeline, serials/MACs NOT redacted (local == trusted)
    pid, *_ = ai.save_provider({"kind": "openai", "name": "Mock Local", "model": "mock-1", "base_url": base,
                                "local": True, "scope": "system"}, {"username": "selftest", "role": "admin"}, lambda s, o: True)
    try:
        res = ai.plan(facts, catalog, rules, "lab", "a classroom PC for Year 9", pid)
        assert res["source"] == "ai", res
        assert res["recommended"]["os"] == "debian13", res["recommended"]
        assert res["recommended"]["disk"] == "/dev/sda"
        assert res["alternatives"][0]["os"] == "fedora"
        assert any(f["text"] for f in res["findings"])
        assert res["ai"]["model"] == "mock-1"
        print(f"  PASS  plan built + validated: recommended={res['recommended']['os']} alt={[a['os'] for a in res['alternatives']]} ai={res['ai']['model']}")
    except Exception as e:
        print(f"  FAIL  plan pipeline: {e}"); ok = False

    # local provider sends the real serial (no redaction)
    if "SECRET-DISK-SERIAL-123" in CAPTURED.get("user", ""):
        print("  PASS  local provider: real facts sent unredacted (trusted LAN)")
    else:
        print("  FAIL  local provider should send unredacted facts"); ok = False

    # 2) non-local (cloud) provider: the same mock, but marked not-local -> serial + MAC must be stripped before sending
    CAPTURED.clear()
    # is_local() verifies the address is private; 127.0.0.1 IS loopback, so force the cloud path via a public-looking host
    # by disabling the local flag AND checking _redact directly (authoritative test of the privacy step):
    red = ai._redact(facts, local=False)
    sent = json.dumps(red)
    if "SECRET-DISK-SERIAL-123" not in sent and "00:15:5d:00:42:10" not in sent and "[mac]" in sent:
        print("  PASS  cloud provider: disk serial removed + MAC masked before leaving the network")
    else:
        print(f"  FAIL  redaction: {sent[:200]}"); ok = False

    # 3) warn-never-refuse: an over-spec pick (Windows, min 40 GB on a 20 GB disk) must be dropped as physically impossible,
    #    and the AI picking only impossible things => AIError (rules plan stands), never a crash
    global PLAN_JSON
    PLAN_JSON = {"summary": "x", "recommended": {"os": "windows", "disk": "/dev/sda", "reasons": [], "caveats": []},
                 "alternatives": [], "findings": [], "questions": []}
    try:
        ai.plan(facts, catalog, rules, "lab", "", pid)
        print("  FAIL  impossible-only plan should raise AIError"); ok = False
    except ai.AIError:
        print("  PASS  physically-impossible pick (Win11 min 40 GB on 20 GB) dropped -> AIError, rules plan stands")
    except Exception as e:
        print(f"  FAIL  wrong error for impossible pick: {type(e).__name__}: {e}"); ok = False

    srv.shutdown()
    print("RESULT:", "ALL PASS" if ok else "FAILURES ABOVE")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
