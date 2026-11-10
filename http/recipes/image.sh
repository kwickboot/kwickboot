# recipe: write a ready-made disk image (OpenWrt x86, OPNsense nano, Home Assistant OS, golden images...)
# Job params: IMAGE=<file in http/images/ or https URL>  SHA256=<expected hash, required>
RECIPE_NAME="raw disk image"
RECIPE_METHOD=image
RECIPE_EFI_LABEL="UEFI OS|BOOT"

recipe_requirements() {
  [ -n "$IMAGE" ] || { echo "IMAGE= required"; return 1; }
  case "$SHA256" in [0-9a-f]*) [ ${#SHA256} -eq 64 ] || { echo "SHA256= (64 hex chars) required"; return 1; } ;; *) echo "SHA256= required"; return 1;; esac
}

image_write() {   # stream: download -> SHA-256 + decompress -> disk in one pass (no copy in RAM); bad checksum = disk wiped, job fails
  case "$IMAGE" in http*://*) url="$IMAGE" ;; *) url="$SRV/images/$IMAGE" ;; esac
  # vendor URL: use the server's cached copy of the same file when present (LAN speed; the checksum check is identical)
  case "$IMAGE" in http*://*) wget -q --spider "$SRV/images/${IMAGE##*/}" 2>/dev/null && url="$SRV/images/${IMAGE##*/}" ;; esac
  case "$IMAGE" in
    *.gz)  dec="gzip -dc" ;; *.xz) dec="xz -dc -T0" ;; *.zst) dec="zstd -dc" ;; *.bz2) dec="bzip2 -dc" ;; *) dec="cat" ;;
  esac
  [ "$dec" = "xz -dc -T0" ] && ! xz --help 2>&1 | grep -q -- --threads && dec="xz -dc"   # busybox xz has no -T
  # RAW_BYTES = unpacked image size: refuse a disk that is too small before anything is written
  if [ -n "$RAW_BYTES" ]; then
    have=$(( $(cat /sys/class/block/${DISK#/dev/}/size) * 512 ))
    [ "$have" -ge "$RAW_BYTES" ] || { echo "disk too small: image needs $((RAW_BYTES/1000000000)) GB, $DISK has $((have/1000000000)) GB"; return 1; }
  fi
  echo "downloading + writing $url"
  t=$(date +%s); fifo=/tmp/img.sum.$$; rm -f $fifo; mkfifo $fifo
  sha256sum < $fifo > $fifo.out & sp=$!
  wipefs -a -q "$DISK"* 2>/dev/null || true
  # RAW_SHA256 = the vendor's own checksum of the unpacked image (for images we repack, e.g. zip -> zstd): verify that instead
  if [ -n "$RAW_SHA256" ]; then
    SHA256=$RAW_SHA256
    wget -qO- "$url" | $dec 2>/tmp/img.err | tee $fifo | dd of="$DISK" bs=4M conv=fsync status=none 2>/dev/null
  else
    wget -qO- "$url" | tee $fifo | $dec 2>/tmp/img.err | dd of="$DISK" bs=4M conv=fsync status=none 2>/dev/null
  fi
  wait $sp; got=$(cut -c1-64 $fifo.out); rm -f $fifo $fifo.out; sync
  if [ "$got" != "$SHA256" ]; then
    echo "CHECKSUM MISMATCH or incomplete download (${got:-nothing received}) - wiping the partial image"
    wipefs -a -q "$DISK" 2>/dev/null; dd if=/dev/zero of="$DISK" bs=1M count=16 status=none 2>/dev/null; return 1
  fi
  grep -v -i 'trailing garbage' /tmp/img.err | grep -q . && { echo "decompression error: $(head -c 300 /tmp/img.err)"; return 1; }
  echo "checksum OK, image written in $(( $(date +%s) - t ))s"
  partprobe "$DISK" 2>/dev/null || true
  # GPT images: move the backup header to the real end of the disk. MBR images (BSD, older appliances) stay untouched.
  [ "$(blkid -p -o value -s PTTYPE "$DISK" 2>/dev/null)" = gpt ] && { sgdisk -e "$DISK" >/dev/null 2>&1 || true; partprobe "$DISK" 2>/dev/null || true; }
  echo "image written: $(lsblk -no NAME,SIZE,FSTYPE,LABEL "$DISK" | tr '\n' ';')"
}

cloudinit_seed() {   # add a NoCloud "CIDATA" vfat partition so cloud-init sets hostname + SSH keys on first boot
  cu=${CLOUD_USER:-cloud-user}     # default login the vendor cloud image ships (fedora / rocky / almalinux ...)
  loc=${LOCALE:-en_US.UTF-8}; kmap=${KEYMAP:-us}; tzn=${TZ:-America/Chicago}   # worldwide locale/keyboard/timezone
  [ "$(blkid -p -o value -s PTTYPE "$DISK")" = gpt ] || { echo "expected a GPT cloud image"; return 1; }
  sgdisk -n 0:-64M:0 -t 0:0700 -c 0:cidata "$DISK" >/dev/null || { echo "cannot add the CIDATA partition"; return 1; }
  partprobe "$DISK" 2>/dev/null; sleep 2
  n=$(sgdisk -p "$DISK" | awk '/cidata/ {print $1}' | tail -n1)
  case "$DISK" in *[0-9]) part="${DISK}p$n" ;; *) part="$DISK$n" ;; esac
  [ -b "$part" ] || { echo "CIDATA partition $part not found"; return 1; }
  mkfs.vfat -n CIDATA "$part" >/dev/null 2>/tmp/mkfs.err || { cat /tmp/mkfs.err; return 1; }
  mkdir -p /tmp/cidata && mount "$part" /tmp/cidata || return 1
  printf 'instance-id: kwickboot-%s\nlocal-hostname: %s\n' "$(date +%s)" "$HOSTNAME" > /tmp/cidata/meta-data
  {
    echo "#cloud-config"
    echo "hostname: $HOSTNAME"
    echo "preserve_hostname: false"
    echo "locale: $loc"                # worldwide at install (RTL for Arabic/Hebrew follows the locale in the desktop)
    echo "timezone: $tzn"
    echo "keyboard:"                   # cloud-init keyboard module (>=22.3, present on Fedora/Rocky/Alma/openSUSE)
    echo "  layout: $kmap"
    echo "ssh_pwauth: false"
    echo "users:"
    echo "  - name: $cu"
    echo "    sudo: 'ALL=(ALL) NOPASSWD:ALL'"
    echo "    groups: wheel"
    echo "    shell: /bin/bash"
    echo "    lock_passwd: true"
    echo "    ssh_authorized_keys:"
    grep -E '^(ssh-|ecdsa-)' $TK/authorized_keys | sed 's/^/      - /'
    echo "ssh_authorized_keys:"       # also to the image's default user, whatever the vendor named it
    grep -E '^(ssh-|ecdsa-)' $TK/authorized_keys | sed 's/^/  - /'
    echo "runcmd:"
    echo "  - [ sh, -c, 'install -d -m700 /root/.ssh; cp /home/$cu/.ssh/authorized_keys /root/.ssh/authorized_keys 2>/dev/null || true; chmod 600 /root/.ssh/authorized_keys 2>/dev/null || true' ]"
    echo "  - [ sh, -c, 'command -v localectl >/dev/null && localectl set-x11-keymap \"$kmap\" 2>/dev/null; command -v localectl >/dev/null && localectl set-keymap \"$kmap\" 2>/dev/null; true' ]"
    echo "  - [ sh, -c, 'systemctl enable --now sshd 2>/dev/null; command -v firewall-cmd >/dev/null && { firewall-cmd --permanent --add-service=ssh; firewall-cmd --reload; } 2>/dev/null; true' ]"
  } > /tmp/cidata/user-data
  sync; umount /tmp/cidata
  echo "cloud-init seed: hostname $HOSTNAME, user $cu, locale $loc, keymap $kmap, tz $tzn, $(grep -cE '^(ssh-|ecdsa-)' $TK/authorized_keys) SSH key(s) (CIDATA on $part)"
}

recipe_deploy() { image_write; }

recipe_verify() {
  # probe the disk itself: the RAM OS has no udev, so lsblk shows no filesystem types
  found=""; for x in "$DISK" "$DISK"?*; do [ -b "$x" ] && blkid -p "$x" >/dev/null 2>&1 && found=1; done
  [ -n "$found" ] && echo "  OK    filesystems found on $DISK" || { echo "  FAIL  no filesystems on $DISK"; return 1; }
  if [ "$BOOT_MODE" = uefi ]; then   # a UEFI machine needs an EFI system partition, or it cannot boot the image
    efi=""; for x in "$DISK"?*; do [ -b "$x" ] && [ "$(blkid -p -o value -s TYPE "$x" 2>/dev/null)" = vfat ] && efi=1; done
    [ -n "$efi" ] && echo "  OK    EFI system partition present" || { echo "  FAIL  image has no EFI partition - it cannot boot on this UEFI machine"; return 1; }
  fi
}
