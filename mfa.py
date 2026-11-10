"""Two-step sign-in with an authenticator app (Google Authenticator, Microsoft Authenticator, 1Password, ...).
SPDX-License-Identifier: AGPL-3.0-or-later

TOTP (RFC 6238): HMAC-SHA1, 30-second steps, 6 digits - the format every authenticator app reads from a QR code.
Works fully offline and on private networks: nothing leaves the server. Each code is accepted once (replay-safe), with
one step of clock drift either way. Ten one-time recovery codes (stored hashed) for a lost phone; an admin can also
reset a user's two-step sign-in.
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
import urllib.parse

STEP, DIGITS, DRIFT = 30, 6, 1


def new_secret():
    """160-bit secret, base32 without padding (what the apps expect)."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _code(secret, counter):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    d = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = d[-1] & 0x0F
    return str((struct.unpack(">I", d[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** DIGITS).zfill(DIGITS)


def verify(secret, code, last_counter=0, now=None):
    """Returns the matched time step (store it: a code is never accepted twice) or None."""
    code = "".join(c for c in str(code) if c.isdigit())
    if len(code) != DIGITS or not secret:
        return None
    t = int((now if now is not None else time.time()) // STEP)
    for c in range(t - DRIFT, t + DRIFT + 1):
        if c > int(last_counter or 0) and hmac.compare_digest(_code(secret, c), code):
            return c
    return None


def uri(secret, account, issuer="KwickBoot"):
    """otpauth:// link - the content of the QR code."""
    label = urllib.parse.quote(f"{issuer}:{account}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={urllib.parse.quote(issuer)}&algorithm=SHA1&digits={DIGITS}&period={STEP}"


def qr_svg(text):
    """The QR code as an SVG string (python3-qrcode, a Debian package). None if it is not installed: the console then
    shows the secret to type into the app instead."""
    try:
        import io
        import qrcode
        import qrcode.image.svg
    except ImportError:
        return None
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, border=2, box_size=10)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode()


def _hash(code):
    return hashlib.sha256(("kwickboot-recovery:" + code.replace("-", "").upper()).encode()).hexdigest()


def new_recovery_codes(n=10):
    """(codes to show once, hashes to store). Format XXXX-XXXX, no look-alike characters."""
    alpha = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    codes = ["-".join("".join(secrets.choice(alpha) for _ in range(4)) for _ in range(2)) for _ in range(n)]
    return codes, [_hash(c) for c in codes]


def use_recovery_code(code, hashes):
    """Returns the remaining hashes if the code matched (it is used up), else None."""
    h = _hash(str(code).strip())
    for i, x in enumerate(hashes or []):
        if hmac.compare_digest(x, h):
            return hashes[:i] + hashes[i + 1:]
    return None
