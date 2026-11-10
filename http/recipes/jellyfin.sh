# recipe: Jellyfin media server on Debian 13 (prebuilt image) - sourced by install.sh
# SPDX-License-Identifier: AGPL-3.0-or-later
wget -qO $TK/debian13-base.sh "$SRV/recipes/debian13.sh" && . $TK/debian13-base.sh || { echo "debian13 recipe missing on server"; exit 1; }
RECIPE_NAME="Jellyfin media server"
RECIPE_DESC="Free media server for movies, TV, music and photos - streams to TVs, phones and browsers. No accounts, no tracking."
RECIPE_NOTE="Debian 13 + Jellyfin from its official repo. Web setup at http://<machine>:8096 (first visit creates the admin). Put media on a second disk or a NAS share."
RECIPE_MIN_GB=8
RECIPE_IMAGE=jellyfin
recipe_deploy() {   # image only: the server needs linux/build-images.sh jellyfin
  [ "$BOOT_MODE" = uefi ] || { echo "UEFI only"; return 1; }
  image_deploy "$RECIPE_IMAGE"
}
recipe_verify() {
  chk "jellyfin installed and enabled"  'grep -q "^Package: jellyfin-server$" $T/var/lib/dpkg/status && [ -L $T/etc/systemd/system/multi-user.target.wants/jellyfin.service ]'
  chk "firewall opens 8096 to the LAN"  'grep -q "dport { 8096 }" $T/etc/nftables.conf'
}
