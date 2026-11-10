# recipe: OPNsense 26.7 (firewall / router) - official "nano" disk image (runs directly, no installer), checksum verified
# SPDX-License-Identifier: AGPL-3.0-or-later
RECIPE_NAME="OPNsense 26.7 (firewall / router)"
RECIPE_DESC="Open-source firewall and router: VPN, IDS/IPS, traffic shaping, captive portal. Official image from opnsense.org."
RECIPE_NOTE="Legacy-BIOS (CSM) boot required. Needs 2+ network ports. Default LAN 192.168.1.1, web UI https://192.168.1.1 (user root, password opnsense - change it at once). The first port is LAN, the second WAN."
RECIPE_MIN_GB=8
wget -qO /root/tk/image-lib.sh "$SRV/recipes/image.sh" && . /root/tk/image-lib.sh
RECIPE_NAME="OPNsense 26.7 (firewall / router)"
RECIPE_METHOD=image
RECIPE_EFI_LABEL="UEFI OS|BOOT|OPNsense|FreeBSD"
IMAGE=https://pkg.opnsense.org/releases/26.7/OPNsense-26.7-nano-amd64.img.bz2
SHA256=28d5e2f37e40d87468a924e3006ef10e2ddc6de485b85333d9e3958c84d0cb9d
# The official nano image uses a BSD/MBR layout with a legacy-BIOS boot loader and no EFI partition: refuse UEFI
# machines BEFORE anything is erased (enable CSM / legacy boot in the firmware setup, or use OPNsense's own installer).
recipe_requirements() {
  [ "$BOOT_MODE" = bios ] || { echo "OPNsense nano image boots with legacy BIOS only - switch the firmware to legacy/CSM boot and retry"; return 1; }
  [ "$(( $(lsblk -bdno SIZE "$DISK") / 1000000000 ))" -ge 8 ] || { echo "need >= 8 GB"; return 1; }
}
