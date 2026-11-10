#!/bin/sh
# Fetch the Anthropic Python SDK + dependencies as wheels for the KwickBoot server (Debian 13: CPython 3.13, x86-64),
# verify every wheel against the sha256 PyPI publishes for it, and write a --require-hashes requirements file.
#   sh fetch-ai-sdk.sh <version> <outdir>        (run on the build box, not on the server)
set -eu
VER=${1:?anthropic version}; OUT=${2:-/tmp/kb-ai-wheels}
rm -rf "$OUT"; mkdir -p "$OUT"
python3 -m venv /tmp/kb-ai-dl-venv >/dev/null 2>&1 || true
PIP=/tmp/kb-ai-dl-venv/bin/pip
[ -x "$PIP" ] || PIP="python3 -m pip"
$PIP download -q -d "$OUT" --only-binary=:all: --python-version 3.13 --implementation cp --abi cp313 \
  --platform manylinux_2_17_x86_64 --platform manylinux2014_x86_64 --platform manylinux_2_28_x86_64 --platform any \
  "anthropic==$VER"
python3 - "$OUT" <<'EOF'
import hashlib, json, os, re, sys, urllib.request
out = sys.argv[1]
lines, bad = [], 0
for f in sorted(os.listdir(out)):
    if not f.endswith(".whl"):
        continue
    name, ver = f.split("-")[0], f.split("-")[1]
    sha = hashlib.sha256(open(os.path.join(out, f), "rb").read()).hexdigest()
    meta = json.load(urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{ver}/json", timeout=30))
    pub = {u["filename"]: u["digests"]["sha256"] for u in meta["urls"]}
    ok = pub.get(f) == sha
    bad += not ok
    print(("OK   " if ok else "BAD  ") + f + "  " + sha)
    lines.append(f"{re.sub(r'_', '-', name)}=={ver} --hash=sha256:{sha}")
open(os.path.join(out, "requirements.txt"), "w").write("\n".join(lines) + "\n")
print("WHEELS-VERIFIED" if not bad else f"{bad} WHEEL(S) DID NOT MATCH PYPI")
EOF
