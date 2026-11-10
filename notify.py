"""KwickBoot notifications: post a one-line message to a Teams / Slack / generic webhook (admin setting).
SPDX-License-Identifier: AGPL-3.0-or-later

Settings (database): notify_webhook = https URL, notify_kind = slack|teams|generic.
Sending happens in a background thread and never blocks or breaks PXE/agent handling.
"""
import json
import threading
import urllib.request

import db


def send(text):
    url = db.get_setting("notify_webhook")
    if not url or not url.startswith("https://"):
        return
    kind = db.get_setting("notify_kind", "slack")
    body = {"text": text} if kind in ("slack", "teams") else {"event": "kwickboot", "text": text}
    threading.Thread(target=_post, args=(url, body), daemon=True).start()


def _post(url, body):
    try:
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:      # notifications must never break the server
        try:
            db.audit("system", "notify failed", str(e)[:200])
        except Exception:
            pass
