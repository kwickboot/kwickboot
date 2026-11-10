#!/bin/bash
# Build, SIGN (Ed25519) and PUBLISH the KwickBoot driver catalog to download.kwickboot.com/compat/. Run on the build box.
# SPDX-License-Identifier: AGPL-3.0-or-later
#   bash build-driver-catalog.sh [driver-catalog.src.json]
# Needs: ~/.config/kwickboot-catalog-sign.pem (private signing key) and CF_DEPLOY_TOKEN + R2_ACCOUNT_ID in
# ~/.config/kwickboot-r2.env. The pinned PUBLIC key is baked into drivers_catalog.py on every fleet.
set -euo pipefail
cd "$(dirname "$0")"
SRC=${1:-driver-catalog.src.json}
KEY=~/.config/kwickboot-catalog-sign.pem
OUT=/tmp/drivers.json; RAW=/tmp/drivers.json.rawsig; SIG=/tmp/drivers.json.sig
[ -f "$KEY" ] || { echo "missing signing key $KEY"; exit 1; }
set -a; . ~/.config/kwickboot-r2.env; set +a

# canonical, deterministic bytes (stamp generated time, compact, sorted) - we sign EXACTLY these bytes
python3 - "$SRC" "$OUT" <<'PY'
import json, sys, time
d = json.load(open(sys.argv[1]))
d["generated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
open(sys.argv[2], "w").write(json.dumps(d, separators=(",", ":"), sort_keys=True))
PY

# Ed25519 raw signature over the exact file bytes -> base64 (what drivers_catalog.py verifies)
openssl pkeyutl -sign -inkey "$KEY" -rawin -in "$OUT" -out "$RAW"
base64 -w0 "$RAW" > "$SIG"

# self-check before publishing: verify with the public half
PUB=$(openssl pkey -in "$KEY" -pubout -outform DER | tail -c 32 | base64)
python3 - "$OUT" "$SIG" "$PUB" <<'PY'
import base64, sys
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
data = open(sys.argv[1], "rb").read()
sig = base64.b64decode(open(sys.argv[2]).read().strip())
pk = Ed25519PublicKey.from_public_bytes(base64.b64decode(sys.argv[3]))
pk.verify(sig, data)
print("self-verify OK (pinned pubkey must equal:", sys.argv[3] + ")")
PY

export CLOUDFLARE_API_TOKEN="$CF_DEPLOY_TOKEN" CLOUDFLARE_ACCOUNT_ID="$R2_ACCOUNT_ID"
W="npx --yes wrangler@3"
$W r2 object put kwickboot-downloads/compat/drivers.json     --file "$OUT" --content-type application/json 2>&1 | grep -iE "upload|error" | tail -2
$W r2 object put kwickboot-downloads/compat/drivers.json.sig --file "$SIG" --content-type text/plain        2>&1 | grep -iE "upload|error" | tail -2
echo "published: https://download.kwickboot.com/compat/drivers.json (+ .sig)"
