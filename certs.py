"""certs.py - certificates of sanitization (NIST SP 800-88 Rev. 2, Sec. 4.6 / Appendix C), signed with this server's
Ed25519 key.
SPDX-License-Identifier: AGPL-3.0-or-later

The RAM OS (http/scripts/wipe.sh) reports one "WIPECERT {json}" line per disk that passed verification. issue() adds who
ordered it, the job and the time, and signs the canonical JSON. Anyone can check a certificate with the server's public
key (/api/certs/pubkey). Certificates are never deleted. pdf() renders a one-page PDF without any library.
"""
import base64
import hashlib
import json
import os
import time

import db

KEY = None            # data/wipe-signing.key (Ed25519 private key, raw 32 bytes, mode 0600)


def init(base):
    global KEY
    KEY = os.path.join(base, "data", "wipe-signing.key")


def _key():
    from cryptography.hazmat.primitives.asymmetric import ed25519
    if not os.path.exists(KEY):
        k = ed25519.Ed25519PrivateKey.generate()
        raw = k.private_bytes_raw()
        fd = os.open(KEY, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
    return ed25519.Ed25519PrivateKey.from_private_bytes(open(KEY, "rb").read())


def public_key():
    """Base64 of the raw 32-byte Ed25519 public key + its fingerprint."""
    raw = _key().public_key().public_bytes_raw()
    return base64.b64encode(raw).decode(), hashlib.sha256(raw).hexdigest()[:16]


def canonical(body):
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def issue(mac, job, ordered_by, rec, server_name):
    """rec: one WIPECERT record from the RAM OS (only verify == PASS is certified)."""
    if rec.get("verify") != "PASS":
        return None
    body = {"kind": "KwickBoot certificate of sanitization", "standard": "NIST SP 800-88 Rev. 2 (September 2025); techniques per IEEE 2883",
            "disk": {k: rec.get(k) for k in ("serial", "model", "manufacturer", "bytes", "type", "device")},
            "media_type": "ISM (storage device)",
            "method": rec.get("method"), "nist_level": rec.get("nist"), "technique": rec.get("technique") or rec.get("method"),
            "tool": rec.get("tool") or "KwickBoot wipe.sh",
            "verification": {"result": rec.get("verify"), "detail": rec.get("verify_detail"), "health": rec.get("health"),
                             "method": "tool completion status, errors, device health, sampled read-back"},
            "started": rec.get("started"), "finished": rec.get("finished"),
            "machine": dict(rec.get("machine") or {}, mac=mac), "job": job, "ordered_by": ordered_by,
            "issued": int(time.time()), "issuer": server_name}
    data = canonical(body)
    cid = hashlib.sha256(data).hexdigest()[:20]
    body["id"] = cid
    data = canonical(body)
    sig = base64.b64encode(_key().sign(data)).decode()
    db.x("INSERT OR IGNORE INTO wipe_certs(id, mac, job, serial, created, body, sig) VALUES(?,?,?,?,?,?,?)",
         cid, mac, job, str(rec.get("serial", "")), db.now(), data.decode(), sig)
    return cid


def get(cid):
    r = db.q1("SELECT * FROM wipe_certs WHERE id=?", cid)
    return dict(r) if r else None


def verify(cid):
    """True if the stored body still matches its signature (detects any edit of the database)."""
    r = get(cid)
    if not r:
        return False
    try:
        _key().public_key().verify(base64.b64decode(r["sig"]), r["body"].encode())
        return True
    except Exception:
        return False


def for_machine(mac):
    return [dict(r, body=json.loads(r["body"])) for r in db.q("SELECT * FROM wipe_certs WHERE mac=? ORDER BY created DESC", mac)]


def _pdf_text(s):
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode("latin-1", "replace").decode("latin-1")


def pdf(cid):
    """A one-page PDF (Helvetica, A4) of the certificate, including the signature and how to verify it."""
    r = get(cid)
    if not r:
        return None
    b = json.loads(r["body"])
    pub, fp = public_key()
    t = lambda ts: time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ts)) if ts else "-"
    d, m = b.get("disk", {}), b.get("machine", {})
    lines = [("KwickBoot - Certificate of Data Sanitization", 16), ("", 10),
             (f"Certificate {b.get('id')}", 11), (f"Standard: {b.get('standard')}", 10), ("", 10),
             ("Media", 12), (f"  Manufacturer: {d.get('manufacturer') or '-'}   Model: {d.get('model') or '-'}", 10),
             (f"  Serial number: {d.get('serial')}   Media type: {b.get('media_type') or 'ISM'} ({d.get('type')})", 10),
             ("  Organizationally assigned media / property number: ______________________", 10),
             (f"  Capacity: {round((d.get('bytes') or 0) / 1e9, 1)} GB", 10), ("", 10),
             ("Sanitization", 12), (f"  Method: {b.get('nist_level')}   Technique: {b.get('technique') or b.get('method')}", 10),
             (f"  Command: {b.get('method')}   Tool: {b.get('tool') or 'KwickBoot wipe.sh'}", 10),
             (f"  Started: {t(b.get('started'))}   Finished: {t(b.get('finished'))}", 10),
             (f"  Verification ({b['verification'].get('method') or 'completion status, sampled read-back'}):", 10),
             (f"    {b['verification'].get('result')} - {b['verification'].get('detail')}; device health: {b['verification'].get('health') or 'not reported'}", 10), ("", 10),
             ("Media source", 12), (f"  {m.get('vendor') or ''} {m.get('product') or ''}  serial {m.get('serial') or '-'}  MAC {m.get('mac')}", 10), ("", 10),
             ("Issued", 12), (f"  {t(b.get('issued'))} by {b.get('issuer')}, ordered by {b.get('ordered_by')} (job {b.get('job')})", 10), ("", 10),
             ("Validation (NIST SP 800-88 Rev. 2, 4.5.2 / 4.6) - to be completed by the organization", 12),
             ("  Name: ____________________________   Position / title: ____________________________", 10),
             ("  Date: ______________   Location: ______________________   Contact: ____________________", 10),
             ("  Signature: _______________________________     Approved [ ]   Rejected [ ]", 10), ("", 10),
             ("Signature (Ed25519 over the canonical JSON record)", 12)]
    sig = r["sig"]
    lines += [(f"  {sig[i:i + 80]}", 8) for i in range(0, len(sig), 80)]
    lines += [("", 8), (f"  Server public key {fp}: {pub}", 8),
              ("  Verify in the KwickBoot web console (Certificates) or with the public key and the record JSON.", 8)]
    y, ops = 800, []
    for text, size in lines:
        ops.append(f"BT /F1 {size} Tf 50 {y} Td ({_pdf_text(text)}) Tj ET")
        y -= size + 6
    stream = "\n".join(ops).encode("latin-1")
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offs = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offs)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)
