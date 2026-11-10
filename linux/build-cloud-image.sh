#!/bin/sh
# build-cloud-image.sh <url> <vendor_sha256> <out_id> [outdir]
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Repack a vendor cloud image (qcow2, or anything qemu-img reads) into the raw+zstd form KwickBoot streams to disk:
#   1. download the vendor image over HTTPS
#   2. verify the VENDOR's published SHA-256 (refuse on mismatch)
#   3. qemu-img convert -> raw
#   4. compress raw -> <out_id>.bin.zst (zstd), write <out_id>.bin.zst.sha256
#   5. print RAW_SHA256 (sha of the unpacked raw) and RAW_BYTES for the recipe constants
#
# Run on the build box, NEVER on the production server. qemu-img, zstd, wget, sha256sum required.
set -e
URL=$1; VSHA=$2; ID=$3; OUT=${4:-/tmp/kbimg}
[ -n "$URL" ] && [ -n "$VSHA" ] && [ -n "$ID" ] || { echo "usage: $0 <url> <vendor_sha256> <out_id> [outdir]"; exit 2; }
mkdir -p "$OUT"; cd "$OUT"
f=vendor-$ID.img
echo ">>> [$ID] download $URL"
wget -q -O "$f" "$URL"
echo ">>> [$ID] verify vendor SHA-256"
echo "$VSHA  $f" | sha256sum -c - || { echo "!!! [$ID] VENDOR CHECKSUM MISMATCH - refusing"; rm -f "$f"; exit 1; }
echo ">>> [$ID] convert -> raw"
qemu-img convert -O raw "$f" "$ID.raw"
RAW_BYTES=$(stat -c %s "$ID.raw")
RAW_SHA256=$(sha256sum "$ID.raw" | cut -c1-64)
echo ">>> [$ID] compress raw -> $ID.bin.zst (zstd -19 -T0)"
zstd -19 -T0 -q -f -o "$ID.bin.zst" "$ID.raw"
sha256sum "$ID.bin.zst" | awk '{print $1}' > "$ID.bin.zst.sha256"
ZBYTES=$(stat -c %s "$ID.bin.zst")
rm -f "$ID.raw" "$f"
echo "=== [$ID] DONE: $ID.bin.zst ($((ZBYTES/1000000)) MB) in $OUT"
echo "RECIPE_VALUES $ID RAW_SHA256=$RAW_SHA256 RAW_BYTES=$RAW_BYTES ZST_SHA256=$(cat "$ID.bin.zst.sha256")"
