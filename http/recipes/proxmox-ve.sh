# recipe: Proxmox VE 9 virtualization host on Debian 13 (prebuilt image) - sourced by install.sh
# SPDX-License-Identifier: AGPL-3.0-or-later
wget -qO $TK/debian13-base.sh "$SRV/recipes/debian13.sh" && . $TK/debian13-base.sh || { echo "debian13 recipe missing on server"; exit 1; }
RECIPE_NAME="Proxmox VE 9"
RECIPE_DESC="Virtual machines and containers with a web console, backups and clustering. Free (no-subscription repository)."
RECIPE_NOTE="Debian 13 + Proxmox VE, the official 'install on Debian' route. Web UI https://<machine>:8006 (log in as root after setting its password with 'passwd' over SSH). Network: bridge vmbr0 over the first wired port, DHCP - reserve its address in the router."
RECIPE_MIN_GB=32
RECIPE_IMAGE=proxmox-ve
recipe_requirements() {
  [ "$(( $(lsblk -bdno SIZE "$DISK") / 1000000000 ))" -ge 32 ] || { echo "need >= 32 GB"; return 1; }
  [ "$BOOT_MODE" = uefi ] || { echo "UEFI only"; return 1; }
  grep -qE 'vmx|svm' /proc/cpuinfo || echo "WARN  CPU virtualization (VT-x/AMD-V) is off or missing - enable it in the firmware setup"
}
recipe_deploy() { image_deploy "$RECIPE_IMAGE"; }
recipe_verify() {
  chk "Proxmox VE installed"            '[ -e $T/usr/bin/pveversion ]'
  chk "Proxmox kernel present"          'ls $T/boot/vmlinuz-*-pve >/dev/null 2>&1'
  chk "first-boot network unit enabled" '[ -L $T/etc/systemd/system/multi-user.target.wants/kb-pve-firstboot.service ]'
  chk "firewall opens 8006 to the LAN"  'grep -q "8006" $T/etc/nftables.conf'
}
