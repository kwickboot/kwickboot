# recipe: the KwickBoot server itself - used by the installer ISO (sourced by install.sh, served from the ISO)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Debian 13 from the prebuilt "kbserver" image (Debian + everything KwickBoot needs, fully offline), configured like
# the Debian 13 recipe, plus: the KwickBoot release in /opt/kwickboot-setup, a one-time first-boot service that runs
# install.sh --offline --firewall and creates the first console admin, a Linux admin account (console login + sudo)
# and an optional static address.
#   env: KB_ADMIN=<user>  KB_PWFILE=<file with the password, RAM OS>  [KB_IP=192.168.1.10/24 KB_GW=.. KB_DNS=..]
RECIPE_NAME="KwickBoot server (Debian 13)"
RECIPE_DESC="The KwickBoot boot server on Debian 13, installed offline from the KwickBoot ISO."
RECIPE_NOTE="UEFI machines. Console admin + Linux admin from the installer; SSH by key only."
RECIPE_MIN_GB=16
RECIPE_METHOD=rootfs
RECIPE_EFI_LABEL=debian
RECIPE_IMAGE=kbserver
RECIPE_HIDDEN=1                # not in the console's OS library: the installer ISO uses it

recipe_requirements() {
  [ "$(( $(lsblk -bdno SIZE "$DISK") / 1000000000 ))" -ge 16 ] || { echo "need a disk of 16 GB or more (images and ISOs need much more)"; return 1; }
  [ "$BOOT_MODE" = uefi ] || { echo "this PC started the ISO in legacy BIOS mode: the KwickBoot server needs UEFI - switch the firmware to UEFI and start the ISO again"; return 1; }
  if [ -n "$KB_ADMIN" ]; then
    [ -s "${KB_PWFILE:-/nonexistent}" ] || { echo "no admin password given"; return 1; }
    echo "$KB_ADMIN" | grep -qE '^[a-z][a-z0-9_-]{1,30}$' || { echo "admin name: lowercase letters, digits, - _"; return 1; }
  fi
}

recipe_deploy() {
  image_deploy "$RECIPE_IMAGE" || { echo "the server image on the ISO could not be written"; return 1; }
}

recipe_configure() {
  # no SSH keys yet: root SSH stays key-only; the admin logs in at the console (or adds keys to this file later)
  HOSTNAME="$HOSTNAME" TZ="$TZ" LAN="$LAN" KEYS="# KwickBoot server: add SSH public keys for root here (one per line)" \
    BOOT_MODE="$BOOT_MODE" DISK="$DISK" KB_FROM_IMAGE="$KB_FROM_IMAGE" sh /root/tk/debian13-configure.sh $T || return 1
  # KwickBoot writes its own complete firewall at first boot (install.sh --firewall: SSH + KwickBoot ports from the LAN)
  rm -f $T/etc/nftables.conf
  # static address (the installer asked); otherwise DHCP from the image stays
  if [ -n "$KB_IP" ]; then
    printf '[Match]\nName=en* eth*\n\n[Network]\nAddress=%s\nGateway=%s\n%s\n' "$KB_IP" "$KB_GW" \
      "$(for d in $KB_DNS; do echo "DNS=$d"; done)" > $T/etc/systemd/network/20-wired.network
    echo "network: static $KB_IP via $KB_GW"
  else echo "network: DHCP (give the server a reservation on your DHCP server)"; fi
  # Linux admin: console login + sudo (SSH stays key-only). Unattended installs have none yet: the web console's
  # setup code creates the first console admin, and a local login is added later (sudo is reachable from the console's terminal).
  if [ -n "$KB_ADMIN" ]; then
    chroot $T useradd -m -s /bin/bash -G sudo "$KB_ADMIN" 2>/dev/null || true
    printf '%s:%s\n' "$KB_ADMIN" "$(cat "$KB_PWFILE")" | chroot $T chpasswd || return 1
    echo "linux admin: $KB_ADMIN (sudo)"
  fi
  # isolated test lab (the installer asked; never on a normal server): the lab card gets 10.88.0.1/24 and KwickBoot's
  # own DHCP (advanced, off by default) answers on that card only; the console stays reachable from the office side
  if [ "$KB_LAB" = 1 ] && [ -n "$KB_LABMAC" ]; then
    printf '[Match]\nMACAddress=%s\n\n[Network]\nAddress=10.88.0.1/24\nConfigureWithoutCarrier=yes\n' "$KB_LABMAC" > $T/etc/systemd/network/10-kwickboot-lab.network
    : > $T/root/.kb-lab                                            # first boot: install.sh --iface <lab card> --own-dhcp
    echo "test lab: 10.88.0.1/24 on $KB_LABMAC, DHCP there only"
  fi
  # ISOs from the USB stick's data partition (KWICKDATA/iso) into the server's library - only while 8 GB stay free
  if [ -n "$KB_IMPORT" ] && ls "$KB_IMPORT"/*.iso >/dev/null 2>&1; then
    mkdir -p $T/opt/pxe-toolkit/http/iso
    need=$(du -sm "$KB_IMPORT" | cut -f1); free=$(df -m $T | awk 'NR==2{print $4}')
    if [ "$need" -lt $((free - 8192)) ]; then
      for f in "$KB_IMPORT"/*.iso; do cp "$f" $T/opt/pxe-toolkit/http/iso/ && echo "iso: $(basename "$f")"; done
      chmod 755 $T/opt/pxe-toolkit/http/iso; chmod 644 $T/opt/pxe-toolkit/http/iso/*.iso
    else echo "NOTE: ISOs on the stick need $need MB, the disk has $free MB free - not copied (add them in the console later)"; fi
  fi
  # ready-made OS images from the stick (KWICKDATA/images: <name>.tar.zst + .sha256 + .info), same free-space rule
  if [ -n "$KB_IMPORT" ] && ls /kbdata/images/*.tar.zst >/dev/null 2>&1; then
    mkdir -p $T/opt/pxe-toolkit/http/images
    need=$(du -sm /kbdata/images | cut -f1); free=$(df -m $T | awk 'NR==2{print $4}')
    if [ "$need" -lt $((free - 8192)) ]; then
      for f in /kbdata/images/*.tar.zst; do b=${f%.tar.zst}
        if [ -f "$f.sha256" ] && [ "$(sha256sum "$f" | cut -d' ' -f1)" = "$(cut -d' ' -f1 "$f.sha256")" ]; then
          cp "$f" "$f.sha256" $T/opt/pxe-toolkit/http/images/; [ -f "$b.info" ] && cp "$b.info" $T/opt/pxe-toolkit/http/images/
          echo "image: $(basename "$f")"
        else echo "NOTE: $(basename "$f") skipped - no matching .sha256 next to it"; fi
      done
      chmod 755 $T/opt/pxe-toolkit/http/images; chmod 644 $T/opt/pxe-toolkit/http/images/*
    else echo "NOTE: images on the stick need $need MB, the disk has $free MB free - not copied"; fi
  fi
  # the release + first-boot setup
  mkdir -p $T/opt/kwickboot-setup
  wget -q -O - "$SRV/release/kwickboot.tar.gz" | tar -xz -C $T/opt/kwickboot-setup || { echo "release missing on the ISO"; return 1; }
  rel=$(ls -d $T/opt/kwickboot-setup/kwickboot-* | head -n1)
  # ISO-embedded releases omit duplicated runtime payloads. Restore them from the medium's authoritative copies.
  if [ -d /iso/http/runtime/http/kbcore ]; then
    mkdir -p "$rel/http"
    cp -a /iso/http/runtime/http/kbcore* "$rel/http/" || return 1
  fi
  if [ -d /iso/tools ]; then
    mkdir -p "$rel/http/images/tools"
    for d in /iso/tools/*; do [ -d "$d" ] && cp -a "$d" "$rel/http/images/tools/"; done
  fi
  [ -d "$rel/http/kbcore" ] || { echo "KwickBoot Core missing from the installer medium"; return 1; }
  [ -n "$KB_ADMIN" ] && install -m 0600 "$KB_PWFILE" $T/root/.kb-admin-pw
  [ -n "$KB_ADMIN" ] || : > $T/root/.kb-setup-pending            # the setup still has to run (no admin to create)
  rel=${rel#$T}
  # first-boot setup: needs the network (install.sh binds to the LAN card). No address yet -> say so on the login
  # screen and try again at the next start; the password file is shredded and the service disabled only on success.
  cat > $T/usr/local/sbin/kwickboot-setup <<SETUP
#!/bin/sh
L=/var/log/kwickboot-setup.log
issue() { sed -i '/^KwickBoot server/d' /etc/issue; printf 'KwickBoot server - %s\n\n' "\$1" >> /etc/issue; }
for i in \$(seq 1 60); do ip -4 route show default | grep -q . && ip -4 -o addr show scope global | grep -q . && break; sleep 5; done
if ! ip -4 route show default | grep -q .; then
  issue "SETUP WAITING: plug in the network cable (or fix the address) - setup continues at the next start"
  echo "no network yet (\$(date)) - will retry at the next start" >> \$L; exit 1
fi
issue "setting up... (log: \$L)"
OPT=""
if [ -f /root/.kb-lab ]; then                  # test lab: serve the lab card, own DHCP there, console from the office side
  LABIF=\$(ip -o link | awk -v m="$KB_LABMAC" '\$0 ~ m {sub(/:\$/, "", \$2); print \$2; exit}')
  OFF=\$(ip -o -4 route show default | awk '{for(i=1;i<NF;i++) if(\$i=="dev"){print \$(i+1); exit}}')
  OFFNET=\$(ip -o -4 route show dev "\$OFF" scope link | awk '{print \$1; exit}')
  OPT="--iface \$LABIF --own-dhcp 10.88.0.100-10.88.0.200 --own-dhcp-discover \${OFFNET:+--console-from \$OFFNET}"
fi
admin_ok() { [ ! -f /root/.kb-admin-pw ] || KB_PW_STDIN=1 /usr/local/sbin/pxectl useradd $KB_ADMIN admin < /root/.kb-admin-pw >> \$L 2>&1; }
if KB_NO_DEFAULT_ADMIN=\$([ -f /root/.kb-admin-pw ] && echo 1 || echo 0) sh $rel/install.sh --offline --yes --firewall \$OPT >> \$L 2>&1 && admin_ok; then
  [ -f /root/.kb-admin-pw ] && shred -u /root/.kb-admin-pw
  rm -f /root/.kb-setup-pending /root/.kb-lab; systemctl disable kwickboot-setup.service
  for i in \$(seq 1 30); do [ -n "$KB_ADMIN" ] || [ -s /opt/pxe-toolkit/data/setup-code ] && break; sleep 1; done
  if [ -n "$KB_ADMIN" ]; then issue "console: https://\\\\4:9443/ui/  (sign in as '$KB_ADMIN'; the browser warns once about the self-signed certificate)"
  else issue "console: https://\\\\4:9443/ui/  - first sign-in admin / admin, then choose your own password"; fi
  echo "SETUP DONE \$(date)" >> \$L
else
  issue "SETUP FAILED - see \$L (it runs again at the next start)"; exit 1
fi
SETUP
  chmod 700 $T/usr/local/sbin/kwickboot-setup
  cat > $T/etc/systemd/system/kwickboot-setup.service <<UNIT
[Unit]
Description=KwickBoot: first-boot setup (install the server, create the console admin)
After=network-online.target
Wants=network-online.target
ConditionPathExists=|/root/.kb-admin-pw
ConditionPathExists=|/root/.kb-setup-pending

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/kwickboot-setup
StandardOutput=journal+console

[Install]
WantedBy=multi-user.target
UNIT
  ln -sf /etc/systemd/system/kwickboot-setup.service $T/etc/systemd/system/multi-user.target.wants/kwickboot-setup.service
  printf '\nKwickBoot server - setting up at first start (log: /var/log/kwickboot-setup.log)\n\n' >> $T/etc/issue
  echo "kwickboot: release $(basename $rel) staged; first boot installs it$( [ -n "$KB_ADMIN" ] && echo " and creates console admin '$KB_ADMIN'")"
}

recipe_bootloader() { :; }   # done inside debian13-configure.sh (chroot)

recipe_verify() {
  chk "release staged"          "[ -f $T/opt/kwickboot-setup/*/install.sh ]"
  chk "first-boot setup enabled" "[ -L $T/etc/systemd/system/multi-user.target.wants/kwickboot-setup.service ]"
  [ -n "$KB_ADMIN" ] && chk "admin password handed over (0600)" "[ \"\$(stat -c %a $T/root/.kb-admin-pw)\" = 600 ]"
  [ "$KB_LAB" = 1 ] && chk "test lab card configured" "[ -f $T/etc/systemd/network/10-kwickboot-lab.network ]"
  return 0
}
