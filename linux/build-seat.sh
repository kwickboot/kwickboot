#!/bin/bash
# Build the KwickBoot gaming seat: a diskless Debian 13 that boots straight into Steam Big Picture (netbar / LAN-party PCs).
# SPDX-License-Identifier: AGPL-3.0-or-later
#   sudo KB_OUT=/path/http/seat linux/build-seat.sh   -> $KB_OUT/{vmlinuz,initrd.img,filesystem.squashfs,version,SHA256SUMS}
# Boot: shim -> GRUB -> Debian-signed kernel; live-boot fetches the root filesystem into RAM (fetch=http://...), so nothing of the
# OS is on the seat's disk and every reboot starts clean (players' sign-ins, downloads to RAM and settings are gone).
# Games: a local disk labelled KBGAMES (ext4) is mounted at /games and holds the Steam client and its library (sign-ins wiped
# at every boot); without one, Steam and games live in RAM. Steam: Debian steam-installer + Valve bootstrap (pinned sha256).
# Steam's subscriber agreement allows personal use; a commercial netbar needs Valve's cyber-cafe licence - the operator's job.
set -euo pipefail
OUT=${KB_OUT:-/opt/pxe-toolkit/http/seat}
B=${KB_BUILD:-/var/tmp/kbseat-build}
MIRROR=http://deb.debian.org/debian
PROXY=""; curl -s -m 3 -o /dev/null http://127.0.0.1:3142/acng-report.html && PROXY=http://127.0.0.1:3142
command -v debootstrap >/dev/null && command -v mksquashfs >/dev/null || apt-get install -y -q debootstrap squashfs-tools >/dev/null
umount -l $B/sys $B/proc $B/dev/pts $B/dev 2>/dev/null || true; rm -rf --one-file-system $B; mkdir -p $B $OUT

echo ">>> debootstrap trixie (cache: ${PROXY:-none})"
env ${PROXY:+http_proxy=$PROXY} debootstrap --variant=minbase --components=main,contrib,non-free,non-free-firmware \
  --include=systemd-sysv,udev,dbus,ca-certificates trixie $B $MIRROR > /var/tmp/kbseat-debootstrap.log

mount --bind /dev $B/dev; mount --bind /dev/pts $B/dev/pts; mount -t proc proc $B/proc; mount -t sysfs sys $B/sys
trap 'umount -l $B/sys $B/proc $B/dev/pts $B/dev 2>/dev/null || true' EXIT
cp /etc/resolv.conf $B/etc/resolv.conf
chroot $B /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root DEBIAN_FRONTEND=noninteractive KBP="$PROXY" /bin/bash -s <<'CHROOT'
set -e
[ -n "$KBP" ] && echo "Acquire::http::Proxy \"$KBP\";" > /etc/apt/apt.conf.d/01cache
cat > /etc/dpkg/dpkg.cfg.d/01kwickboot-slim <<EOF
path-exclude=/usr/share/doc/*
path-include=/usr/share/doc/*/copyright
path-exclude=/usr/share/man/*
path-exclude=/usr/share/info/*
path-exclude=/usr/share/lintian/*
EOF
cat > /etc/apt/sources.list <<EOF
deb http://deb.debian.org/debian trixie main contrib non-free non-free-firmware
deb http://deb.debian.org/debian trixie-updates main contrib non-free non-free-firmware
deb http://security.debian.org/debian-security trixie-security main contrib non-free non-free-firmware
EOF
dpkg --add-architecture i386                 # Steam and most games are 32-bit clients
apt-get update -q
echo ">>> kernel (Debian-signed) + live-boot"
apt-get install -y -q --no-install-recommends linux-image-amd64 live-boot live-boot-initramfs-tools initramfs-tools \
  firmware-linux-free firmware-realtek firmware-misc-nonfree firmware-iwlwifi firmware-intel-misc firmware-intel-graphics \
  firmware-amd-graphics intel-microcode amd64-microcode >/dev/null
echo ">>> graphics, sound, input (Mesa: Intel, AMD, nouveau; 64 + 32 bit)"
apt-get install -y -q --no-install-recommends \
  xserver-xorg-core xserver-xorg-input-libinput xinit x11-xserver-utils xauth \
  libgl1-mesa-dri libgl1-mesa-dri:i386 libglx-mesa0 libglx-mesa0:i386 libegl-mesa0 libgbm1 \
  mesa-vulkan-drivers mesa-vulkan-drivers:i386 libvulkan1 libvulkan1:i386 mesa-va-drivers \
  pipewire pipewire-pulse pipewire-alsa wireplumber libspa-0.2-bluetooth alsa-ucm-conf \
  dbus-user-session libpam-systemd systemd-resolved openssh-server curl ca-certificates iproute2 procps kmod \
  e2fsprogs fonts-dejavu-core fonts-noto-cjk xdg-utils zenity >/dev/null
echo ">>> Steam (Valve's client via Debian's steam-installer, contrib)"
echo steam steam/question select "I AGREE" | debconf-set-selections
echo steam steam/license note '' | debconf-set-selections
apt-get install -y -q --no-install-recommends steam-installer steam-libs-amd64 steam-libs-i386 >/dev/null
echo ">>> seat user"
useradd -m -u 1000 -s /bin/bash -G audio,video,input,render player
usermod -p '*' player; usermod -p '*' root     # no passwords: auto-login on the seat, SSH by key only
echo "chroot done"
CHROOT

echo ">>> Steam bootstrap (Valve's launcher tarball, URL + SHA-256 pinned by Debian's steam-installer)"
# Exactly what Debian's /usr/games/steam does on first start, done at build time: no "Steam installer" dialog on the seats.
# The client then updates itself on the seat: once onto its games disk (kept), or into RAM at each boot without one.
SL=$B/usr/games/steam
ver=$(sed -n 's/^version="\([0-9.]*\)"$/\1/p' $SL); sum=$(sed -n 's/^sha256="\([0-9a-f]\{64\}\)"$/\1/p' $SL)
[ -n "$ver" ] && [ -n "$sum" ] || { echo "cannot read the pinned Steam version from $SL"; exit 1; }
W=$(mktemp -d); curl -fsSL --proto =https -o $W/steam.tgz "https://repo.steampowered.com/steam/archive/beta/steam_${ver}.tar.gz"
[ "$(sha256sum $W/steam.tgz | cut -d' ' -f1)" = "$sum" ] || { echo "Steam bootstrap CHECKSUM MISMATCH - refused"; exit 1; }
tar -C $W -xzf $W/steam.tgz steam-launcher/bootstraplinux_ubuntu12_32.tar.xz steam-launcher/icons steam-launcher/steam.desktop
SD=$B/usr/share/kb-seat/steam                     # pristine copy; kb-seat-boot puts it where the player's Steam runs
mkdir -p $SD/deb-installer; tar -C $SD -xJf $W/steam-launcher/bootstraplinux_ubuntu12_32.tar.xz
mv $W/steam-launcher/bootstraplinux_ubuntu12_32.tar.xz $SD/bootstrap.tar.xz; mv $W/steam-launcher $SD/deb-installer/
cp $SD/deb-installer/steam-launcher/steam.desktop $SD/deb-installer/steam.desktop; echo "$ver" > $SD/deb-installer/version
rm -rf $W; echo "Steam bootstrap $ver: $(du -sh $SD | cut -f1)"

echo ">>> configure"
echo kb-seat > $B/etc/hostname
printf '127.0.0.1\tlocalhost\n127.0.1.1\tkb-seat\n' > $B/etc/hosts
mkdir -p $B/etc/systemd/network
printf '[Match]\nName=en* eth*\n\n[Network]\nDHCP=yes\n\n[DHCPv4]\nClientIdentifier=mac\n' > $B/etc/systemd/network/20-wired.network
printf 'PermitRootLogin prohibit-password\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n' > $B/etc/ssh/sshd_config.d/10-kwickboot.conf
rm -f $B/etc/ssh/ssh_host_*
install -d $B/usr/local/bin
# boot: server address from the command line, admin SSH keys from the server, per-seat host keys and hostname
cat > $B/usr/local/bin/kb-seat-boot <<'EOF'
#!/bin/sh
for w in $(cat /proc/cmdline); do case "$w" in kb_server=*) S=${w#kb_server=} ;; kb_host=*) H=${w#kb_host=} ;; esac; done
[ -n "$H" ] && { echo "$H" > /etc/hostname; hostname "$H"; }
ssh-keygen -A >/dev/null 2>&1
mkdir -p /root/.ssh; chmod 700 /root/.ssh
[ -n "$S" ] && for i in $(seq 1 15); do curl -sf -m 5 -o /root/.ssh/authorized_keys "http://$S/authorized_keys" && break; sleep 2; done
chmod 600 /root/.ssh/authorized_keys 2>/dev/null || true
# Steam lives on the seat's games disk (label KBGAMES, prepared once by KwickBoot - never any other disk): client updates and
# games stay between boots. Without that disk it runs from RAM. Debian's launcher uses ~/.steam/debian-installation.
H=/home/player/.steam; ST=$H/debian-installation; mkdir -p $H
d=$(blkid -L KBGAMES 2>/dev/null) && mkdir -p /games && mount -o noatime "$d" /games && {
  [ -x /games/Steam/steam.sh ] || { mkdir -p /games/Steam && cp -a /usr/share/kb-seat/steam/. /games/Steam/ && chown -R 1000:1000 /games/Steam; }
  ST=/games/Steam; ln -sfn /games/Steam $H/debian-installation; }
[ "$ST" = /games/Steam ] || { rm -rf $ST; cp -a /usr/share/kb-seat/steam $ST; }
# a seat is shared: nobody may find the previous player still signed in. Steam keeps sign-ins in these files (accounts,
# login tokens, Steam Guard sentry, per-user data, store cookies); remove them at every boot - before anyone can log in
rm -rf $ST/config/loginusers.vdf $ST/config/config.vdf $ST/local.vdf $ST/ssfn* $ST/userdata $ST/config/htmlcache \
       $ST/appcache/httpcache $ST/logs 2>/dev/null
chown -h 1000:1000 $H $H/debian-installation; [ "$ST" = /games/Steam ] || chown -R 1000:1000 $ST
exit 0
EOF
cat > $B/usr/local/bin/kb-seat-session <<'EOF'
#!/bin/sh
# X session of the seat: Steam Big Picture, restarted if it is closed. No desktop behind it.
xset s off -dpms 2>/dev/null; xsetroot -solid black 2>/dev/null
while true; do steam -gamepadui >/dev/null 2>&1; sleep 2; done
EOF
cat > $B/usr/local/bin/kb-seat-netbar <<'EOF'
#!/bin/sh
# Netbar gate: run Steam Big Picture only while the server says this seat has an active session; otherwise a lock screen.
# Fail-OPEN on a server/network glitch (never lock a paying player out over a blip). With netbar billing OFF (the default)
# the server always answers "run", so free diskless gaming is unchanged. The front desk drives sessions from the console.
SRV=$(cat /etc/kb-server 2>/dev/null); [ -n "$SRV" ] || SRV=$(sed -n 's/.*kb_server=\([^ ]*\).*/\1/p' /proc/cmdline)
IFC=$(ip route show default 2>/dev/null | awk '{print $5; exit}')
MAC=$(cat /sys/class/net/${IFC:-eth0}/address 2>/dev/null)
poll() { curl -s -m 5 "http://$SRV/seat/session?mac=$MAC" 2>/dev/null; }
field() { printf '%s' "$1" | sed -n 's/.*"'"$2"'"[^:]*:[ ]*"\{0,1\}\([^",}]*\).*/\1/p'; }
# persistent per-member home: restore on sign-in, save back before the seat locks/wipes (docs + settings follow the member).
restore_home() {
  [ -n "$MAC" ] || return
  code=$(curl -s -m 25 -o /tmp/kbhome.tgz -w '%{http_code}' "http://$SRV/seat/home?mac=$MAC" 2>/dev/null)
  [ "$code" = 200 ] && [ -s /tmp/kbhome.tgz ] && tar xzf /tmp/kbhome.tgz -C /home/player 2>/dev/null
  rm -f /tmp/kbhome.tgz
}
save_home() {
  [ -n "$MAC" ] || return
  # skip the seat's own boot hooks and the big/volatile caches (Steam has its own cloud); keep docs + settings
  tar czf /tmp/kbhome-save.tgz -C /home/player \
    --exclude=.profile --exclude=.bash_logout --exclude=.xinitrc \
    --exclude=.steam --exclude=.local/share/Steam --exclude=.cache --exclude=.steampath --exclude=.steampid . 2>/dev/null || return
  curl -s -m 60 -X POST -H 'Content-Type: application/gzip' --data-binary @/tmp/kbhome-save.tgz "http://$SRV/seat/home?mac=$MAC" >/dev/null 2>&1
  rm -f /tmp/kbhome-save.tgz
}
lock() {
  printf '\033[2J\033[H\033[?25l\n\n\n\n            KwickBoot  -  seat locked\n\n'
  if [ "$1" = timeup ]; then printf "            Time's up - see the front desk to add more time.\n"
  else printf '            Please see the front desk to start your session.\n'; fi
  printf '\n            %s    rate %s %s/hour\n' "$(date '+%H:%M')" "$3" "$2"
}
while true; do
  st=$(poll); state=$(field "$st" state); [ -z "$st" ] && state=run    # server unreachable -> fail open (keep playing)
  if [ "$state" = run ]; then
    restore_home                                                   # the signed-in member's docs + settings follow them here
    ( n=0; while :; do sleep 10; n=$((n + 1)); s=$(poll)
        [ -n "$s" ] && [ "$(field "$s" state)" != run ] && { save_home; pkill -x Xorg 2>/dev/null; break; }
        [ $((n % 30)) -eq 0 ] && save_home                        # periodic save (~5 min) so a hard power-off loses little
      done ) &
    wd=$!
    startx /usr/local/bin/kb-seat-session -- -nolisten tcp vt1 >/dev/null 2>&1   # blocks; the watchdog ends it when time is up
    kill "$wd" 2>/dev/null
  else
    pgrep -x Xorg >/dev/null && { pkill -x Xorg 2>/dev/null; sleep 2; }
    lock "$(field "$st" reason)" "$(field "$st" rate_per_hour)" "$(field "$st" currency)"
    printf '\n            Enter your member code then press Enter (or wait for the front desk): '
    if read -t 12 code 2>/dev/null && [ -n "$code" ]; then
      resp=$(curl -s -m 5 --data-urlencode "code=$code" "http://$SRV/seat/login?mac=$MAC" 2>/dev/null)
      if [ "$(field "$resp" ok)" = true ]; then
        printf '\n            Welcome %s - %s minutes. Starting...\n' "$(field "$resp" name)" "$(field "$resp" minutes)"; sleep 1
      else
        printf '\n            %s\n' "$(field "$resp" error)"; sleep 3
      fi
    fi
  fi
done
EOF
chmod 755 $B/usr/local/bin/kb-seat-boot $B/usr/local/bin/kb-seat-session $B/usr/local/bin/kb-seat-netbar
cat > $B/etc/systemd/system/kb-seat.service <<'EOF'
[Unit]
Description=KwickBoot seat: hostname, admin keys, games disk
Before=ssh.service getty@tty1.service
Wants=network-online.target
After=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/local/bin/kb-seat-boot
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
EOF
mkdir -p $B/etc/systemd/system/getty@tty1.service.d
printf '[Service]\nExecStart=\nExecStart=-/sbin/agetty --autologin player --noclear %%I $TERM\n' > $B/etc/systemd/system/getty@tty1.service.d/autologin.conf
cat >> $B/home/player/.profile <<'EOF'
[ "$(tty)" = /dev/tty1 ] && [ -z "$DISPLAY" ] && exec /usr/local/bin/kb-seat-netbar
EOF
chroot $B chown -R player:player /home/player
chroot $B systemctl enable systemd-networkd systemd-resolved ssh kb-seat >/dev/null 2>&1
sed -i 's/^MODULES=.*/MODULES=netboot/; s/^COMPRESS=.*/COMPRESS=zstd/' $B/etc/initramfs-tools/initramfs.conf
chroot $B update-initramfs -u -k all >/dev/null 2>&1
chroot $B sh -c 'apt-get clean; rm -f /etc/apt/apt.conf.d/01cache; rm -rf /var/lib/apt/lists/* /var/log/*.log /var/log/apt/*' || true
rm -f $B/etc/resolv.conf; ln -s ../run/systemd/resolve/stub-resolv.conf $B/etc/resolv.conf
date +%Y%m%d-%H%M > $B/etc/kwickboot-seat-version

echo ">>> pack"
umount -l $B/sys $B/proc $B/dev/pts $B/dev 2>/dev/null || true; trap - EXIT
K=$(ls $B/boot/vmlinuz-* | sort -V | tail -n1); I=$B/boot/initrd.img-${K##*/vmlinuz-}
cp $K $OUT/vmlinuz.new; cp $I $OUT/initrd.img.new
mksquashfs $B $OUT/filesystem.squashfs.new -comp zstd -Xcompression-level 19 -b 1M -noappend -quiet -processors "$(nproc)" -e boot
for f in vmlinuz initrd.img filesystem.squashfs; do mv -f $OUT/$f.new $OUT/$f; done
cat $B/etc/kwickboot-seat-version > $OUT/version
( cd $OUT && sha256sum vmlinuz initrd.img filesystem.squashfs > SHA256SUMS )
chmod -R u=rwX,go=rX $OUT
rm -rf --one-file-system $B
ls -la $OUT
echo "KwickBoot seat built: kernel ${K##*/vmlinuz-}, $(du -h $OUT/filesystem.squashfs | cut -f1) root filesystem"
