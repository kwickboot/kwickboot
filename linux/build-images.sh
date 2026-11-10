#!/bin/bash
# Build prebuilt OS images for fast installs (run on a fast build machine - not on the KwickBoot server).
# SPDX-License-Identifier: AGPL-3.0-or-later
#   sudo KB_HOME=~/kwickboot linux/build-images.sh debian13      ->  $KB_HOME/http/images/debian13.tar.zst (+ .sha256, .info)
# The image is the finished, fully updated system WITHOUT anything machine-specific (hostname, SSH host keys,
# machine-id, fstab, boot loader on disk). The install engine streams it onto the disk and runs only the
# per-machine steps. Rebuild nightly to ship current security updates.
set -euo pipefail
PATH=/usr/sbin:/usr/bin:/sbin:/bin:$PATH; export PATH
trap 'echo "IMAGE BUILD FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
NAME=${1:?usage: build-images.sh debian13|kbserver|ubuntu2404-server|ubuntu2404-desktop-<bundle>}
T=${KB_HOME:-/opt/pxe-toolkit}; OUT=$T/http/images; B=${KB_BUILD:-/tmp}/kbimg-$NAME
SERVER_PROFILE=${KB_SERVER_PROFILE:-headless}
case "$SERVER_PROFILE" in headless|full) ;; *) echo "bad KB_SERVER_PROFILE: $SERVER_PROFILE (use headless or full)"; exit 1 ;; esac
MIRROR=http://deb.debian.org/debian
mkdir -p $OUT
umount -l $B/{dev/pts,dev,proc,sys} 2>/dev/null || true; rm -rf --one-file-system $B; mkdir -p $B

case "$NAME" in
  debian13|debian13-min|jellyfin|proxmox-ve|kbserver)
    # Debian 13 base = same package set as http/recipes/debian13.sh (UEFI + Secure Boot chain); appliances add their vendor repo
    # debian13-min: the minimal recipe's list and no large firmware bundle (CPU microcode + Realtek NIC firmware only)
    REC=debian13
    [ "$NAME" = debian13-min ] && REC=debian13-minimal
    [ "$NAME" = kbserver ] && [ "$SERVER_PROFILE" = headless ] && REC=debian13-minimal
    PKGS=$(sed -n 's/^DEB_PKGS="\(.*\)"/\1/p' $T/http/recipes/$REC.sh)
    [ -n "$PKGS" ] || { echo "DEB_PKGS not found in recipe"; exit 1; }
    if [ "$NAME" = debian13-min ] || { [ "$NAME" = kbserver ] && [ "$SERVER_PROFILE" = headless ]; }; then
      PKGS="$PKGS,grub-efi-amd64,grub-efi-amd64-signed,shim-signed,intel-microcode,amd64-microcode,firmware-realtek"
    else
      PKGS="$PKGS,grub-efi-amd64,grub-efi-amd64-signed,shim-signed,intel-microcode,amd64-microcode,firmware-misc-nonfree,firmware-realtek"
    fi
    echo ">>> debootstrap trixie ($(echo $PKGS | tr ',' '\n' | wc -l) packages)"
    debootstrap --arch=amd64 --components=main,non-free-firmware --include="$PKGS" trixie $B $MIRROR > $B.debootstrap.log 2>&1 \
      || { tail -n 30 $B.debootstrap.log; exit 1; }
    mount --bind $B $B                      # the root must be a mount point (Proxmox boot-sync uses unshare)
    mount --bind /dev $B/dev; mount --bind /dev/pts $B/dev/pts; mount -t proc proc $B/proc; mount -t sysfs sys $B/sys
    trap 'umount -l $B/dev/pts $B/dev $B/proc $B/sys $B 2>/dev/null || true' EXIT
    cp /etc/resolv.conf $B/etc/resolv.conf
    echo ">>> configure (everything that is the same on every machine)"
    chroot $B /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root DEBIAN_FRONTEND=noninteractive \
      IMG="$NAME" KB_SERVER_PROFILE="$SERVER_PROFILE" /bin/bash -s <<'CHROOT'
set -e
rm -f /etc/apt/sources.list
cat > /etc/apt/sources.list.d/debian.sources <<EOF
Types: deb
URIs: http://deb.debian.org/debian
Suites: trixie trixie-updates
Components: main non-free-firmware
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg

Types: deb
URIs: http://security.debian.org/debian-security
Suites: trixie-security
Components: main non-free-firmware
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
EOF
echo "force-unsafe-io" > /etc/dpkg/dpkg.cfg.d/kb-build-unsafe-io        # build only: no per-file fsync
apt-get update -q >/dev/null
apt-get -y -q full-upgrade >/dev/null
apt-get install -y -q dosfstools zram-tools >/dev/null
sed -i 's/^# *en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen; locale-gen >/dev/null; echo 'LANG=en_US.UTF-8' > /etc/default/locale
mkdir -p /etc/systemd/network
printf '[Match]\nName=en* eth*\n\n[Network]\nDHCP=yes\n\n[DHCPv4]\nClientIdentifier=mac\n' > /etc/systemd/network/20-wired.network
printf 'PermitRootLogin prohibit-password\nPasswordAuthentication no\nKbdInteractiveAuthentication no\nX11Forwarding no\nMaxAuthTries 4\n' \
  > /etc/ssh/sshd_config.d/10-hardening.conf
mkdir -p /etc/systemd/system/ssh.service.d
printf '[Service]\nExecStartPre=\nExecStartPre=/usr/bin/ssh-keygen -A\nExecStartPre=/usr/sbin/sshd -t\n' > /etc/systemd/system/ssh.service.d/kb-hostkeys.conf   # keys first, then Debian's config check
printf 'ALGO=zstd\nPERCENT=50\n' > /etc/default/zramswap
sed -i 's/^GRUB_TIMEOUT=.*/GRUB_TIMEOUT=2/; s/^GRUB_CMDLINE_LINUX_DEFAULT=.*/GRUB_CMDLINE_LINUX_DEFAULT="quiet"/' /etc/default/grub
systemctl enable ssh nftables systemd-networkd systemd-resolved zramswap >/dev/null 2>&1
passwd -l root >/dev/null
mkdir -p /etc/kwickboot; echo "$IMG" > /etc/kwickboot/image
case "$IMG" in
  kbserver)
    # the KwickBoot server itself (installer ISO): Debian 13 + everything install.sh --offline needs
    apt-get install -y -q python3 python3-cryptography udpcast cabextract 7zip innoextract systemd-timesyncd iproute2 nftables \
      firmware-linux-free openssl python3-qrcode python3-ldap3 ethtool smartmontools pciutils usbutils lm-sensors >/dev/null
    systemctl enable systemd-timesyncd >/dev/null 2>&1
    # ---- local screen: a box with a monitor is self-sufficient. tty1 logs in automatically as user kwickboot; its
    # .profile starts X (startx/xinit), .xinitrc starts Openbox and Chromium in kiosk mode on the status page, which turns
    # into the console's admin sign-in. No display, /etc/kwickboot/no-gui or kb_gui=0: plain text console. Ctrl+Alt+F2 = text.
    # Noto fonts for every script, so every console language (and the language list itself) renders.
    if [ "$KB_SERVER_PROFILE" = full ]; then
      apt-get install -y -q --no-install-recommends xserver-xorg-core xserver-xorg-input-libinput xserver-xorg-video-fbdev \
        xserver-xorg-video-vesa xinit x11-xserver-utils openbox chromium \
        fonts-noto-core fonts-noto-cjk fonts-noto-color-emoji fonts-dejavu-core >/dev/null
    useradd --create-home --shell /bin/bash --comment "KwickBoot local screen" kwickboot
    for g in video input render audio; do getent group $g >/dev/null && usermod -aG $g kwickboot; done
    passwd -l kwickboot >/dev/null                                     # no password: only the tty1 autologin
    mkdir -p /etc/systemd/system/getty@tty1.service.d
    printf '[Service]\nExecStart=\nExecStart=-/sbin/agetty --autologin kwickboot --noclear %%I $TERM\n' \
      > /etc/systemd/system/getty@tty1.service.d/autologin.conf
    mkdir -p /home/kwickboot/.config/openbox
    # Openbox with no menu and no key bindings: nothing but the browser (no terminal is installed either)
    printf '<?xml version="1.0"?>\n<openbox_config xmlns="http://openbox.org/3.4/rc"><keyboard/><mouse/><applications><application class="Chromium*" type="normal"><decor>no</decor><maximized>yes</maximized></application><application type="dialog"><decor>yes</decor><position force="yes"><x>center</x><y>center</y></position><focus>yes</focus></application></applications></openbox_config>\n' \
      > /home/kwickboot/.config/openbox/rc.xml
    printf '<?xml version="1.0"?>\n<openbox_menu xmlns="http://openbox.org/3.4/menu"/>\n' > /home/kwickboot/.config/openbox/menu.xml
    cat > /home/kwickboot/.profile <<'PROFILE'
# KwickBoot local screen: tty1 logs in automatically as kwickboot and starts the console in a kiosk browser
if [ -z "$DISPLAY" ] && [ "$(tty)" = /dev/tty1 ] && [ ! -f /etc/kwickboot/no-gui ] && ! grep -qw kb_gui=0 /proc/cmdline \
   && { ls /dev/dri/card* >/dev/null 2>&1 || [ -e /dev/fb0 ]; }; then
  startx -- -nolisten tcp >/tmp/kwickboot-x.log 2>&1
  echo "The local screen stopped (log: /tmp/kwickboot-x.log) - it starts again in 10 seconds."; sleep 10; exit
fi
PROFILE
    cat > /home/kwickboot/.xinitrc <<'XINITRC'
xset s off s noblank -dpms
openbox --config-file "$HOME/.config/openbox/rc.xml" &
exec /usr/local/bin/kwickboot-kiosk >/tmp/kwickboot-kiosk.log 2>&1
XINITRC
    chown -R kwickboot:kwickboot /home/kwickboot
    cat > /usr/local/sbin/kwickboot-screen-state <<'STATE'
#!/bin/sh
# KwickBoot local screen, root side: a status page for the kiosk (waiting for network -> starting -> the console) and
# the console's address + certificate pin, so Chromium trusts exactly this server's own certificate.
D=/run/kwickboot; mkdir -p $D; chmod 755 $D; T=/opt/pxe-toolkit
page() { { printf '<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="refresh" content="%s"><title>KwickBoot</title>' "$1"
  printf '<style>body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;background:#0f172a;color:#e2e8f0;font:20px system-ui,sans-serif}'
  printf 'main{max-width:760px;padding:32px}h1{font-size:34px;margin:0 0 16px}p{line-height:1.5;color:#cbd5e1}code{color:#7dd3fc;font-size:22px}</style></head>'
  printf '<body><main><h1>KwickBoot</h1>%s</main></body></html>\n' "$2"; } > $D/screen.new && mv $D/screen.new $D/screen.html; }
while :; do
  ip=$(ip -4 -o route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p')
  [ -n "$ip" ] || ip=$(ip -4 -o addr show scope global 2>/dev/null | awk '{sub(/\/.*/, "", $4); print $4; exit}')
  hp=$(python3 -c "import json; print(json.load(open('$T/config.json')).get('ui_https_port') or '')" 2>/dev/null)
  if [ -n "$hp" ]; then url="https://$ip:$hp/ui/"; else url="http://$ip:8080/ui/"; fi
  spki=""; [ -f $T/data/ui.crt ] && spki=$(openssl x509 -in $T/data/ui.crt -pubkey -noout 2>/dev/null | openssl pkey -pubin -outform der 2>/dev/null | openssl dgst -sha256 -binary | base64)
  printf '%s\n%s\n' "$url" "$spki" > $D/console.new && mv $D/console.new $D/console
  if [ -z "$ip" ]; then
    page 3 "<p>Waiting for the network.</p><p>Plug in the network cable. This PC needs an address from your DHCP server (or a static address set at install / in kwickboot.cfg).</p>"
  elif systemctl -q is-active pxe-toolkit && python3 -c "import ssl,sys,urllib.request; urllib.request.urlopen(sys.argv[1], context=ssl._create_unverified_context(), timeout=2)" "$url" 2>/dev/null; then
    page "0;url=$url" "<p>Opening the console...</p><p>Other PCs on this network: <code>$url</code></p>"
  else
    page 3 "<p>Starting the KwickBoot server...</p><p>Its console will be at <code>$url</code> - for this screen and for any PC on the network.</p>"
  fi
  sleep 3
done
STATE
    chmod 755 /usr/local/sbin/kwickboot-screen-state
    cat > /etc/systemd/system/kwickboot-screen-state.service <<'UNIT'
[Unit]
Description=KwickBoot local screen: status page and console address
ConditionPathExists=!/etc/kwickboot/no-gui
ConditionKernelCommandLine=!kb_gui=0

[Service]
ExecStart=/usr/local/sbin/kwickboot-screen-state
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
    systemctl enable kwickboot-screen-state.service >/dev/null 2>&1
    # system logs stay small and in RAM: journald volatile + capped; KwickBoot keeps boot history in its own database
    mkdir -p /etc/systemd/journald.conf.d
    printf '[Journal]\nStorage=volatile\nRuntimeMaxUse=48M\nRuntimeMaxFileSize=8M\nMaxRetentionSec=7day\nForwardToSyslog=no\nCompress=yes\n' \
      > /etc/systemd/journald.conf.d/10-kwickboot.conf
      rm -rf /var/log/journal
    else
      # The normal appliance is administered from any browser on the LAN. Keep tty1 as a conventional text login;
      # Chromium/X11/Openbox and worldwide GUI fonts are an optional full-offline image, not a cost every user pays.
      mkdir -p /etc/kwickboot; : > /etc/kwickboot/no-gui
    fi ;;
  jellyfin)
    # Jellyfin media server from Jellyfin's own repo. The docs publish no fingerprint: this one was observed on
    # 2026-11-02 from https://repo.jellyfin.org/jellyfin_team.gpg.key ("Jellyfin Team") - a different key stops the build.
    apt-get install -y -q gpg >/dev/null
    install -d -m 0755 /etc/apt/keyrings
    curl -fsSL -o /tmp/jf.asc https://repo.jellyfin.org/jellyfin_team.gpg.key
    gpg --show-keys --with-colons /tmp/jf.asc 2>/dev/null | grep -q '^fpr:*4918AABC486CA052358D778D49023CD01DE21A7B:' \
      || { echo "Jellyfin signing key MISMATCH - refusing"; exit 1; }
    gpg --dearmor < /tmp/jf.asc > /etc/apt/keyrings/jellyfin.gpg; rm -f /tmp/jf.asc
    printf 'Types: deb\nURIs: https://repo.jellyfin.org/debian\nSuites: trixie\nComponents: main\nArchitectures: amd64\nSigned-By: /etc/apt/keyrings/jellyfin.gpg\n' \
      > /etc/apt/sources.list.d/jellyfin.sources
    apt-get update -q >/dev/null; apt-get install -y -q jellyfin >/dev/null
    systemctl enable jellyfin >/dev/null 2>&1
    echo 8096 > /etc/kwickboot/lan-ports                 # web UI http://<machine>:8096 (first visit = setup wizard)
    ;;
  proxmox-ve)
    # Proxmox VE 9 on Debian 13, the official route (pve.proxmox.com/wiki/Install_Proxmox_VE_on_Debian_13_Trixie).
    # Key file pinned to the SHA-256 published on that page.
    curl -fsSL -o /usr/share/keyrings/proxmox-archive-keyring.gpg https://enterprise.proxmox.com/debian/proxmox-archive-keyring-trixie.gpg
    echo "136673be77aba35dcce385b28737689ad64fd785a797e57897589aed08db6e45  /usr/share/keyrings/proxmox-archive-keyring.gpg" | sha256sum -c --quiet \
      || { echo "Proxmox keyring checksum MISMATCH - refusing"; exit 1; }
    printf 'Types: deb\nURIs: http://download.proxmox.com/debian/pve\nSuites: trixie\nComponents: pve-no-subscription\nSigned-By: /usr/share/keyrings/proxmox-archive-keyring.gpg\n' \
      > /etc/apt/sources.list.d/pve-install-repo.sources
    echo "postfix postfix/main_mailer_type select Local only" | debconf-set-selections
    echo "postfix postfix/mailname string pve.localdomain" | debconf-set-selections
    apt-get update -q >/dev/null; apt-get -y -q full-upgrade >/dev/null
    apt-get install -y -q proxmox-default-kernel >/dev/null
    apt-get install -y -q proxmox-ve postfix open-iscsi chrony >/dev/null
    apt-get remove -y -q linux-image-amd64 'linux-image-6.12*' os-prober >/dev/null 2>&1 || true
    # network: Proxmox manages it (ifupdown2, bridge vmbr0 for the VMs). The NIC name is only known on the machine,
    # so a first-boot unit writes /etc/network/interfaces (vmbr0 over the first wired NIC, DHCP - reserve the address
    # in the router) and puts the hostname on that address in /etc/hosts, which pve-cluster requires.
    touch /etc/kwickboot/own-network
    cat > /usr/local/sbin/kb-pve-firstboot <<'EOF'
#!/bin/sh
nic=$(ls /sys/class/net | grep -E '^(en|eth)' | head -n1)
[ -n "$nic" ] || exit 0
printf 'auto lo\niface lo inet loopback\n\niface %s inet manual\n\nauto vmbr0\niface vmbr0 inet dhcp\n    bridge-ports %s\n    bridge-stp off\n    bridge-fd 0\n\nsource /etc/network/interfaces.d/*\n' "$nic" "$nic" > /etc/network/interfaces
ifreload -a 2>/dev/null || systemctl restart networking
for i in $(seq 1 30); do ip=$(ip -4 -o addr show vmbr0 | awk '{print $4}' | cut -d/ -f1); [ -n "$ip" ] && break; sleep 1; done
h=$(hostname); sed -i "/[[:space:]]$h\(\.lan\)\?\([[:space:]]\|$\)/d" /etc/hosts
[ -n "$ip" ] && echo "$ip $h.lan $h" >> /etc/hosts
systemctl disable kb-pve-firstboot.service
EOF
    chmod 755 /usr/local/sbin/kb-pve-firstboot
    printf '[Unit]\nDescription=KwickBoot: Proxmox network + hosts for this machine\nAfter=network-pre.target\nBefore=pve-cluster.service network-online.target\n[Service]\nType=oneshot\nExecStart=/usr/local/sbin/kb-pve-firstboot\n[Install]\nWantedBy=multi-user.target\n' \
      > /etc/systemd/system/kb-pve-firstboot.service
    systemctl enable kb-pve-firstboot.service >/dev/null 2>&1
    echo "8006 3128 5900-5999" > /etc/kwickboot/lan-ports   # web UI https://<machine>:8006, SPICE proxy, VNC consoles
    ;;
esac
# one kernel only: debootstrap installs one, the full-upgrade above adds the newer one - the old one would stay
# (~150 MB of modules + initramfs + kernel in every image)
cur=$(ls /boot/vmlinuz-* 2>/dev/null | sed 's#/boot/vmlinuz-##' | sort -V | tail -n1)
for k in $(dpkg-query -W -f '${Package}\n' 'linux-image-[0-9]*' 2>/dev/null); do
  [ "$k" = "linux-image-$cur" ] || apt-get -y -q purge "$k" >/dev/null 2>&1
done
# minimal image: no translations, manual pages or documentation (licence files stay), now and for later apt installs
if [ "$IMG" = debian13-min ] || { [ "$IMG" = kbserver ] && [ "$KB_SERVER_PROFILE" = headless ]; }; then
  printf 'path-exclude=/usr/share/doc/*\npath-include=/usr/share/doc/*/copyright\npath-exclude=/usr/share/man/*\npath-exclude=/usr/share/info/*\npath-exclude=/usr/share/locale/*\npath-include=/usr/share/locale/en*\npath-include=/usr/share/locale/locale.alias\npath-exclude=/usr/share/lintian/*\n' \
    > /etc/dpkg/dpkg.cfg.d/01-kwickboot-minimal
  find /usr/share/doc -type f ! -name copyright -delete 2>/dev/null; find /usr/share/doc -type d -empty -delete 2>/dev/null
  rm -rf /usr/share/man/* /usr/share/info/* /usr/share/lintian
  find /usr/share/locale -mindepth 1 -maxdepth 1 ! -name 'en*' ! -name locale.alias -exec rm -rf {} + 2>/dev/null
fi
update-initramfs -u -k all >/dev/null 2>&1          # generic initramfs (MODULES=most): boots on any hardware
# nothing machine-specific may remain in the image
rm -f /etc/ssh/ssh_host_* /etc/dpkg/dpkg.cfg.d/kb-build-unsafe-io
: > /etc/machine-id; rm -f /var/lib/dbus/machine-id
apt-get clean; rm -rf /var/lib/apt/lists/* /var/log/*.log /var/log/apt/* /var/cache/debconf/*-old /tmp/* /root/.bash_history
CHROOT
    if [ "$NAME" = kbserver ]; then
      if [ "$SERVER_PROFILE" = full ]; then
        install -m 0755 $T/linux/kwickboot-kiosk $B/usr/local/bin/kwickboot-kiosk        # local screen watchdog
      fi
      install -m 0644 $T/linux/kwickboot-net.conf $B/etc/sysctl.d/90-kwickboot-net.conf   # large transfers (TCP/UDP)
      install -m 0755 $T/linux/kwickboot-nic-tune $B/usr/local/sbin/kwickboot-nic-tune
      install -m 0644 $T/linux/kwickboot-nic-tune.service $B/etc/systemd/system/kwickboot-nic-tune.service
      ln -sf /etc/systemd/system/kwickboot-nic-tune.service $B/etc/systemd/system/multi-user.target.wants/kwickboot-nic-tune.service
      echo tcp_bbr > $B/etc/modules-load.d/kwickboot-bbr.conf
    fi
    rm -f $B/etc/resolv.conf; ln -s ../run/systemd/resolve/stub-resolv.conf $B/etc/resolv.conf
    : > $B/etc/hostname
    umount -l $B/dev/pts $B/dev $B/proc $B/sys; umount -l $B; trap - EXIT
    ;;
  ubuntu2404-server|ubuntu2404-desktop-*)
    # same package phase the recipe runs (http/scripts/ubuntu-configure.sh PHASE=packages); per-machine steps stay in the install
    case "$NAME" in ubuntu2404-server) PROFILE=server; BUNDLE=basic ;; *) PROFILE=desktop; BUNDLE=${NAME#ubuntu2404-desktop-} ;; esac
    case "$BUNDLE" in office|creative|stem|basic) ;; *) echo "unknown bundle $BUNDLE"; exit 1 ;; esac
    FPR=F6ECB3762474EDA9D21B7022871920D1991BC93C      # Ubuntu Archive Automatic Signing Key (2018), as in the recipes
    G=$(mktemp -d); export GNUPGHOME=$G
    wget -qO $G/key.asc "https://keyserver.ubuntu.com/pks/lookup?op=get&options=mr&search=0x$FPR"
    gpg -q --import $G/key.asc 2>/dev/null
    gpg --with-colons --fingerprint 2>/dev/null | grep -q "^fpr:*$FPR:" || { echo "Ubuntu key fingerprint MISMATCH"; exit 1; }
    gpg -q --export $FPR > $G/ubuntu.gpg; unset GNUPGHOME
    INC=ubuntu-keyring,ca-certificates,locales,systemd-resolved,openssh-server,sudo,nftables,curl,wget,gnupg,less,nano,dosfstools,efibootmgr
    S=/usr/share/debootstrap/scripts/noble; [ -e $S ] || S=/usr/share/debootstrap/scripts/gutsy
    echo ">>> debootstrap noble ($PROFILE, bundle $BUNDLE)"
    debootstrap --arch=amd64 --components=main,restricted,universe --include="$INC" --keyring=$G/ubuntu.gpg \
      noble $B http://archive.ubuntu.com/ubuntu $S > $B.debootstrap.log 2>&1 || { tail -n 30 $B.debootstrap.log; exit 1; }
    rm -rf $G
    trap 'for m in $(awk -v t="$B" "index(\$2, t\"/\")==1 {print \$2}" /proc/mounts | sort -r); do umount -l "$m"; done' EXIT
    echo ">>> configure: packages phase"
    PHASE=packages PROFILE=$PROFILE BUNDLE=$BUNDLE sh $T/http/scripts/ubuntu-configure.sh $B
    for m in $(awk -v t="$B" 'index($2, t"/")==1 {print $2}' /proc/mounts | sort -r); do umount -l "$m"; done; trap - EXIT
    # nothing machine-specific may remain in the image
    rm -f $B/etc/ssh/ssh_host_* $B/etc/resolv.conf; : > $B/etc/machine-id; rm -f $B/var/lib/dbus/machine-id
    ln -s ../run/systemd/resolve/stub-resolv.conf $B/etc/resolv.conf; : > $B/etc/hostname
    rm -rf $B/var/lib/apt/lists/* $B/var/cache/apt/*.bin $B/var/log/*.log $B/var/log/apt/* $B/tmp/* $B/root/.bash_history
    ;;
  mint-xfce)
    # Linux Mint Xfce: Ubuntu noble base + Mint's repo. Built here (chroot works on a full build host) so the install
    # uses the fast image_deploy path; the RAM-OS debootstrap fall-through is only a last resort.
    FPR=F6ECB3762474EDA9D21B7022871920D1991BC93C      # Ubuntu Archive Automatic Signing Key (2018)
    G=$(mktemp -d); export GNUPGHOME=$G
    wget -qO $G/key.asc "https://keyserver.ubuntu.com/pks/lookup?op=get&options=mr&search=0x$FPR"
    gpg -q --import $G/key.asc 2>/dev/null
    gpg --with-colons --fingerprint 2>/dev/null | grep -q "^fpr:*$FPR:" || { echo "Ubuntu key fingerprint MISMATCH"; exit 1; }
    gpg -q --export $FPR > $G/ubuntu.gpg; unset GNUPGHOME
    # Mint repo signing key ("Linux Mint Repository Signing Key"), fetched + fingerprint-pinned
    MINT_KEY_FPR=302F0738F465C1535761F965A6616109451BBBF2
    MKR=$B.mint-keyring.gpg; GK=$(mktemp -d); export GNUPGHOME=$GK
    wget -qO $GK/mint.asc "https://keyserver.ubuntu.com/pks/lookup?op=get&options=mr&search=0x$MINT_KEY_FPR"
    gpg -q --import $GK/mint.asc 2>/dev/null
    gpg --with-colons --fingerprint 2>/dev/null | grep -q "^fpr:*$MINT_KEY_FPR:" || { echo "Mint repo key fingerprint MISMATCH"; exit 1; }
    gpg -q --export "$MINT_KEY_FPR" > "$MKR"; unset GNUPGHOME; rm -rf $GK
    INC=ubuntu-keyring,ca-certificates,gpgv,gnupg,locales,systemd-resolved,openssh-server,sudo,nftables,curl,wget,less,nano,dosfstools,efibootmgr
    S=/usr/share/debootstrap/scripts/noble; [ -e $S ] || S=/usr/share/debootstrap/scripts/gutsy
    echo ">>> debootstrap noble (Mint Xfce base)"
    debootstrap --arch=amd64 --components=main,restricted,universe --include="$INC" --keyring=$G/ubuntu.gpg \
      noble $B http://archive.ubuntu.com/ubuntu $S > $B.debootstrap.log 2>&1 || { tail -n 30 $B.debootstrap.log; exit 1; }
    rm -rf $G
    trap 'for m in $(awk -v t="$B" "index(\$2, t\"/\")==1 {print \$2}" /proc/mounts | sort -r); do umount -l "$m"; done' EXIT
    echo ">>> configure: packages phase (Mint Xfce)"
    PHASE=packages MINT_CODENAME=zena MINT_MIRROR=http://packages.linuxmint.com MINT_KEYRING="$MKR" sh $T/http/scripts/mint-configure.sh $B
    for m in $(awk -v t="$B" 'index($2, t"/")==1 {print $2}' /proc/mounts | sort -r); do umount -l "$m"; done; trap - EXIT
    rm -f "$MKR"
    # nothing machine-specific may remain in the image
    rm -f $B/etc/ssh/ssh_host_* $B/etc/resolv.conf; : > $B/etc/machine-id; rm -f $B/var/lib/dbus/machine-id
    ln -s ../run/systemd/resolve/stub-resolv.conf $B/etc/resolv.conf; : > $B/etc/hostname
    rm -rf $B/var/lib/apt/lists/* $B/var/cache/apt/*.bin $B/var/log/*.log $B/var/log/apt/* $B/tmp/* $B/root/.bash_history
    ;;
  *) echo "unknown image $NAME (debian13, ubuntu2404-server, ubuntu2404-desktop-office|creative|stem|basic, mint-xfce)"; exit 1 ;;
esac

echo ">>> pack (tar + zstd -19, all cores)"
tar -C $B --xattrs --xattrs-include='*' --acls --numeric-owner -cf - . | zstd -19 -T0 -q -o $OUT/$NAME.tar.zst.new
mv -f $OUT/$NAME.tar.zst.new $OUT/$NAME.tar.zst
( cd $OUT && sha256sum $NAME.tar.zst > $NAME.tar.zst.sha256 )
printf 'image=%s profile=%s built=%s size=%s unpacked=%s packages=%s\n' "$NAME" \
  "$( [ "$NAME" = kbserver ] && echo "$SERVER_PROFILE" || echo standard )" "$(date +%F_%T)" "$(du -h $OUT/$NAME.tar.zst | cut -f1)" \
  "$(du -sh $B | cut -f1)" "$(chroot $B dpkg -l 2>/dev/null | grep -c '^ii')" | tee $OUT/$NAME.info
rm -rf --one-file-system $B $B.debootstrap.log
