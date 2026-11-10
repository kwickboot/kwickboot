# recipe: KwickBoot Kiosk - Debian 13 + one full-screen browser (cage Wayland kiosk + Chromium)
# SPDX-License-Identifier: AGPL-3.0-or-later
RECIPE_NAME="Kiosk (one web page, locked down)"
RECIPE_DESC="Boots straight into a full-screen browser: check-in desks, library catalogs, wayfinding, dashboards. Clean again after every reboot."
RECIPE_NOTE="Read-only system (changes vanish at reboot), only allowed sites open, idle reset to the start page, nightly security updates, no shell for users, SSH key login for IT. Secure Boot."
RECIPE_MIN_GB=6
RECIPE_METHOD=rootfs
RECIPE_EFI_LABEL=debian
RECIPE_PARAMS="KIOSK_URL KIOSK_ALLOW KIOSK_IDLE KIOSK_RO"
RECIPE_ENDPOINT=1          # managed from the KwickBoot console (profiles, reboot, screenshot)
SUITE=trixie
MIRROR=http://deb.debian.org/debian
# Parameters (from the KwickBoot UI / pxectl install ... url= allow= idle= ro=):
#   KIOSK_URL_B64    start page (base64)                     KIOSK_ALLOW_B64  extra allowed domains, comma separated (base64)
#   KIOSK_IDLE       minutes without input before reset (0 = never, default 10)
#   KIOSK_RO         1 = read-only system, clean after every reboot (default) | 0 = normal writable system

recipe_requirements() {
  [ "$(( $(lsblk -bdno SIZE "$DISK") / 1000000000 ))" -ge 6 ] || { echo "need >= 6 GB"; return 1; }
  [ "$BOOT_MODE" = uefi ] || { echo "UEFI only"; return 1; }
  URL=$(echo "$KIOSK_URL_B64" | base64 -d 2>/dev/null)
  case "$URL" in http://*|https://*) echo "kiosk URL: $URL" ;; *) echo "kiosk URL missing or not http(s)"; return 1 ;; esac
  case "${KIOSK_IDLE:-10}" in *[!0-9]*) echo "KIOSK_IDLE must be minutes"; return 1 ;; esac
  echo "allowed sites: $(echo "$URL" | sed 's#^[a-z]*://##; s#[/:].*##') $(echo "${KIOSK_ALLOW_B64}" | base64 -d 2>/dev/null)  idle reset: ${KIOSK_IDLE:-10} min  read-only: ${KIOSK_RO:-1}"
}

recipe_deploy() {
  INC=linux-image-amd64,firmware-linux-free,openssh-server,systemd-resolved,nftables,curl,ca-certificates,locales,dosfstools,efibootmgr,grub-efi-amd64,grub-efi-amd64-signed,shim-signed,dbus,iproute2,zram-tools,unattended-upgrades,initramfs-tools
  for p in $EXTRA_PKGS; do INC="$INC,$p"; done
  ${KB_PROXY:+env http_proxy=$KB_PROXY} debootstrap --arch=amd64 --components=main,non-free-firmware --include="$INC" \
    --keyring=/usr/share/keyrings/debian-archive-$SUITE-stable.gpg $SUITE $T $MIRROR > /root/debootstrap.log 2>&1 \
    || { tail -n 30 /root/debootstrap.log; return 1; }
  echo "debootstrap ok: $(du -sh $T | cut -f1)"
}

recipe_configure() {
  HOSTNAME="$HOSTNAME" TZ="$TZ" LAN="$LAN" KEYS="$(cat /root/tk/authorized_keys)" BOOT_MODE="$BOOT_MODE" DISK="$DISK" \
    sh /root/tk/debian13-configure.sh $T || return 1
  URL=$(echo "$KIOSK_URL_B64" | base64 -d)
  ALLOW=$(echo "${KIOSK_ALLOW_B64}" | base64 -d 2>/dev/null | tr -c 'a-zA-Z0-9.,*-' ' ')
  for d in dev dev/pts proc sys run; do mountpoint -q $T/$d || mount --rbind /$d $T/$d 2>/dev/null || true; done
  cp -L /etc/resolv.conf $T/etc/resolv.conf.kb 2>/dev/null; mv -f $T/etc/resolv.conf.kb $T/etc/resolv.conf 2>/dev/null || true
  chroot $T /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin URL="$URL" ALLOW="$ALLOW" IDLE="${KIOSK_IDLE:-10}" RO="${KIOSK_RO:-1}" \
    EPSERVER="${SRV#http://}" EPTOKEN="$EP_TOKEN" \
    KBP="$KB_PROXY" DEBIAN_FRONTEND=noninteractive /bin/bash -s <<'CHROOT'
set -e
[ -n "$KBP" ] && echo "Acquire::http::Proxy \"$KBP\";" > /etc/apt/apt.conf.d/01kwickboot-cache
apt-get install -y -q cage chromium fonts-dejavu fonts-noto-color-emoji jq grim >/dev/null 2>&1 || apt-get install -y -q cage chromium fonts-dejavu jq grim >/dev/null
useradd -m -s /usr/sbin/nologin -G video,render,input kiosk 2>/dev/null || true
passwd -l kiosk >/dev/null
printf '%s\n' "$URL" > /etc/kiosk-url; chmod 644 /etc/kiosk-url

# ---- kiosk settings: /etc/kiosk.json (install settings, or the profile from the KwickBoot console) -> kiosk-apply
jq -n --arg url "$URL" --arg allow "$ALLOW" --argjson idle "$IDLE" \
  '{url: $url, allow: ($allow | split(",") | map(select(length > 0))), idle: $idle}' > /etc/kiosk.install.json
cp /etc/kiosk.install.json /etc/kiosk.json
cat > /usr/local/sbin/kiosk-apply <<'EOF'
#!/bin/sh
# KwickBoot: turn /etc/kiosk.json into the start page + Chromium lockdown policy
set -e
J=/etc/kiosk.json
url=$(jq -r .url $J); host=$(echo "$url" | sed 's#^[a-z]*://##; s#[/:?].*##')
printf '%s\n' "$url" > /etc/kiosk-url
mkdir -p /etc/chromium/policies/managed
jq -n --arg host "$host" --argjson allow "$(jq '.allow // []' $J)" --argjson idle "$(jq '.idle // 10' $J)" '
  {DeveloperToolsAvailability: 2, IncognitoModeAvailability: 1, BrowserSignin: 0, PasswordManagerEnabled: false,
   AutofillAddressEnabled: false, AutofillCreditCardEnabled: false, DownloadRestrictions: 3, PrintingEnabled: false,
   TranslateEnabled: false, DefaultNotificationsSetting: 2, DefaultGeolocationSetting: 2, ExtensionInstallBlocklist: ["*"],
   URLBlocklist: ["*"], URLAllowlist: ([$host] + $allow | unique)}
  + (if $idle > 0 then {IdleTimeout: $idle, IdleTimeoutActions: ["clear_browsing_history", "clear_cookies_and_other_site_data",
     "clear_cached_images_and_files", "clear_autofill", "close_browsers"]} else {} end)' > /etc/chromium/policies/managed/kiosk.json
EOF
chmod 755 /usr/local/sbin/kiosk-apply
/usr/local/sbin/kiosk-apply
list=$(jq -c .URLAllowlist /etc/chromium/policies/managed/kiosk.json)

# ---- endpoint agent: installed by install.sh (shared http/scripts/kb-endpoint.sh, pinned HTTPS): it applies the console's
# kiosk profile (/etc/kiosk.json -> kiosk-apply), reloads the page, takes screenshots, and runs the common commands.

cat > /usr/local/bin/kiosk-session <<'EOF'
#!/bin/sh
# KwickBoot kiosk: one Wayland compositor (cage) running one browser; systemd restarts it when it exits
# (closing, crashing, or the idle reset all bring the start page back)
exec cage -s -- chromium --kiosk --noerrdialogs --disable-infobars --no-first-run --disable-features=TranslateUI \
  --disable-session-crashed-bubble --check-for-update-interval=31536000 --ozone-platform=wayland "$(cat /etc/kiosk-url)"
EOF
chmod 755 /usr/local/bin/kiosk-session
cat > /etc/systemd/system/kiosk.service <<'EOF'
[Unit]
Description=KwickBoot kiosk browser
After=systemd-user-sessions.service network-online.target
Wants=network-online.target
Conflicts=getty@tty1.service

[Service]
User=kiosk
PAMName=login
TTYPath=/dev/tty1
StandardInput=tty
StandardOutput=journal
UtmpIdentifier=tty1
Environment=XDG_SESSION_TYPE=wayland
ExecStartPre=+/bin/sh -c 'rm -rf /home/kiosk/.config/chromium /home/kiosk/.cache/chromium'
ExecStart=/usr/local/bin/kiosk-session
Restart=always
RestartSec=2

[Install]
WantedBy=graphical.target
EOF
mkdir -p /etc/systemd/logind.conf.d; printf '[Login]\nNAutoVTs=0\nReserveVT=0\nHandlePowerKey=ignore\n' > /etc/systemd/logind.conf.d/kiosk.conf
systemctl mask getty@tty2.service getty@tty3.service >/dev/null 2>&1 || true

# ---- security updates
cat > /etc/apt/apt.conf.d/52kwickboot-unattended <<'EOF'
Unattended-Upgrade::Origins-Pattern { "origin=Debian,codename=${distro_codename},label=Debian-Security"; "origin=Debian,codename=${distro_codename}-security,label=Debian-Security"; "origin=Debian,codename=${distro_codename}-updates"; };
Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";
Unattended-Upgrade::Automatic-Reboot "false";
EOF
printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' > /etc/apt/apt.conf.d/20auto-upgrades

if [ "$RO" = 1 ]; then
  # ---- read-only system: real root mounted read-only, all changes go to RAM (overlay) and vanish at reboot
  cat > /etc/initramfs-tools/scripts/init-bottom/kb-overlay <<'EOF'
#!/bin/sh
# KwickBoot kiosk: read-only root + RAM overlay. Disabled with "kb_rw" on the kernel command line.
PREREQ=""
prereqs() { echo "$PREREQ"; }
case "$1" in prereqs) prereqs; exit 0 ;; esac
grep -qw kb_ro=1 /proc/cmdline || exit 0
grep -qw kb_rw /proc/cmdline && exit 0
modprobe overlay 2>/dev/null || exit 0
mkdir -p /kbro/lower /kbro/rw
mount -n -o move "${rootmnt}" /kbro/lower || exit 0
mount -t tmpfs -o mode=0755,size=50% kbrw /kbro/rw
mkdir -p /kbro/rw/upper /kbro/rw/work
if ! mount -t overlay -o lowerdir=/kbro/lower,upperdir=/kbro/rw/upper,workdir=/kbro/rw/work overlay "${rootmnt}"; then
  umount /kbro/rw; mount -n -o move /kbro/lower "${rootmnt}"; exit 0     # never leave the kiosk unbootable
fi
mkdir -p "${rootmnt}/media/kb-ro" "${rootmnt}/media/kb-rw"
mount -n -o move /kbro/lower "${rootmnt}/media/kb-ro"
mount -n -o move /kbro/rw "${rootmnt}/media/kb-rw"
sed -i 's|^\([^#].*[[:space:]]/[[:space:]].*\)$|# kb-ro: root is read-only + overlay\n#\1|' "${rootmnt}/etc/fstab"   # (only in RAM)
echo "KwickBoot kiosk: read-only system, changes are kept in RAM until reboot" > /dev/kmsg
EOF
  chmod 755 /etc/initramfs-tools/scripts/init-bottom/kb-overlay
  grep -q '^overlay' /etc/initramfs-tools/modules || echo overlay >> /etc/initramfs-tools/modules
  sed -i 's/^GRUB_CMDLINE_LINUX_DEFAULT="\(.*\)"/GRUB_CMDLINE_LINUX_DEFAULT="\1 kb_ro=1"/' /etc/default/grub
  # nightly maintenance: unlock the real system, install security updates, lock, reboot (clean start every morning)
  cat > /usr/local/sbin/kiosk-maint <<'EOF'
#!/bin/sh
# KwickBoot kiosk nightly maintenance (read-only mode)
L=/media/kb-ro; log=/var/log/kiosk-maint.log
if mountpoint -q $L; then
  mount -o remount,rw $L && {
    for d in dev dev/pts proc sys run; do mount --bind /$d $L/$d; done
    mount --bind /boot/efi $L/boot/efi 2>/dev/null || true
    chroot $L /bin/sh -c 'apt-get update -q && unattended-upgrade -v' > $log 2>&1 || true
    cp $log $L/var/log/kiosk-maint.log 2>/dev/null || true
    umount $L/boot/efi 2>/dev/null; for d in run sys proc dev/pts dev; do umount $L/$d 2>/dev/null; done
    sync; mount -o remount,ro $L
  }
else
  apt-get update -q && unattended-upgrade -v > $log 2>&1 || true
fi
systemctl reboot
EOF
  chmod 755 /usr/local/sbin/kiosk-maint
  systemctl mask apt-daily.timer apt-daily-upgrade.timer >/dev/null 2>&1 || true   # updates happen in kiosk-maint
  MAINT=/usr/local/sbin/kiosk-maint
else
  MAINT=/usr/bin/systemctl
fi
printf '[Unit]\nDescription=Nightly kiosk maintenance (updates + reboot)\n[Timer]\nOnCalendar=*-*-* 03:30\nRandomizedDelaySec=15m\n[Install]\nWantedBy=timers.target\n' > /etc/systemd/system/kiosk-maint.timer
if [ "$RO" = 1 ]; then printf '[Unit]\nDescription=Nightly kiosk maintenance\n[Service]\nType=oneshot\nExecStart=/usr/local/sbin/kiosk-maint\n' > /etc/systemd/system/kiosk-maint.service
else printf '[Unit]\nDescription=Nightly kiosk reboot\n[Service]\nType=oneshot\nExecStart=/usr/bin/systemctl reboot\n' > /etc/systemd/system/kiosk-maint.service; fi
for u in kiosk.service kiosk-maint.timer; do systemctl enable "$u" >/dev/null 2>&1 || { echo "cannot enable $u"; exit 1; }; done
systemctl enable seatd.service >/dev/null 2>&1 || true      # optional: logind (PAMName=login) already grants the seat
systemctl set-default graphical.target >/dev/null 2>&1
update-initramfs -u -k all >/dev/null 2>&1
update-grub >/dev/null 2>&1
apt-get clean
rm -f /etc/apt/apt.conf.d/01kwickboot-cache
echo "kiosk configured: $(cat /etc/kiosk-url)  allowed: $list  idle: $IDLE min  read-only: $RO"
CHROOT
  ln -sf ../run/systemd/resolve/stub-resolv.conf $T/etc/resolv.conf
}

recipe_bootloader() { :; }

recipe_verify() {
  chk "Secure Boot shim installed"       '[ -f $T/boot/efi/EFI/debian/shimx64.efi ]'
  chk "kiosk service enabled"            '[ -L $T/etc/systemd/system/graphical.target.wants/kiosk.service ]'
  chk "browser + compositor installed"   '[ -x $T/usr/bin/chromium ] && [ -x $T/usr/bin/cage ]'
  chk "kiosk user has no login shell"    'grep -q "^kiosk:.*nologin$" $T/etc/passwd'
  chk "kiosk URL set"                    '[ -s $T/etc/kiosk-url ]'
  [ -n "$EP_TOKEN" ] && chk "managed by KwickBoot (endpoint agent)" '[ -s $T/etc/kb-endpoint.conf ] && [ -L $T/etc/systemd/system/timers.target.wants/kb-endpoint.timer ]'
  chk "only allowed sites (allowlist)"   'tr -d " \n" < $T/etc/chromium/policies/managed/kiosk.json | grep -q "\"URLBlocklist\":\[\"\*\"\]"'
  chk "security updates configured"      '[ -x $T/usr/bin/unattended-upgrade ] && [ -L $T/etc/systemd/system/timers.target.wants/kiosk-maint.timer ]'
  if [ "${KIOSK_IDLE:-10}" -gt 0 ]; then chk "idle reset policy"  'grep -q IdleTimeoutActions $T/etc/chromium/policies/managed/kiosk.json'; fi
  if [ "${KIOSK_RO:-1}" = 1 ]; then
    chk "read-only overlay in initramfs" 'ls $T/boot/initrd.img-* >/dev/null && grep -q kb_ro=1 $T/boot/grub/grub.cfg'
  fi
}
