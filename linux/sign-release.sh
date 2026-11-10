#!/bin/sh
# sign-release.sh - checksums + GPG signature for a KwickBoot release directory (run where the release key lives).
# SPDX-License-Identifier: AGPL-3.0-or-later
#   KB_SIGN_KEY=<key id or e-mail> sh sign-release.sh <dir with kwickboot-*.iso / *.tar.gz>
# Writes, next to the files:
#   SHA256SUMS            one line per release file
#   SHA256SUMS.asc        detached, ASCII-armoured signature of SHA256SUMS (gpg --verify SHA256SUMS.asc SHA256SUMS)
#   kwickboot-release-key.asc   the public key (publish it on the site and in the repository)
# Users check:  gpg --import kwickboot-release-key.asc && gpg --verify SHA256SUMS.asc SHA256SUMS && sha256sum -c SHA256SUMS
set -eu
D=${1:?release directory}; K=${KB_SIGN_KEY:?set KB_SIGN_KEY to the release key id or e-mail}
command -v gpg >/dev/null || { echo "gpg missing (apt install gnupg)"; exit 1; }
gpg --list-secret-keys "$K" >/dev/null 2>&1 || { echo "no secret key '$K' in this keyring"; exit 1; }
cd "$D"
ls kwickboot-* >/dev/null 2>&1 || { echo "no kwickboot-* files in $D"; exit 1; }
rm -f SHA256SUMS SHA256SUMS.asc
for f in $({ ls kwickboot-*; [ ! -f KwickBootUSB.exe ] || echo KwickBootUSB.exe; } | grep -vE '\.(sha256|asc)$' | sort); do
  sha256sum "$f"
done > SHA256SUMS
gpg --batch --yes --local-user "$K" --armor --detach-sign --output SHA256SUMS.asc SHA256SUMS
gpg --armor --export "$K" > kwickboot-release-key.asc
gpg --verify SHA256SUMS.asc SHA256SUMS 2>&1 | grep -E 'Good signature|using' || { echo "signature check FAILED"; exit 1; }
sha256sum -c --quiet SHA256SUMS && echo "signed: $(wc -l < SHA256SUMS) files, key $(gpg --with-colons --fingerprint "$K" | awk -F: '/^fpr/{print $10; exit}')"
