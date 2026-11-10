# recipe: Alpine Linux 3.24 - installed with Alpine's own setup-disk from the RAM OS
RECIPE_NAME="Alpine Linux 3.24"
RECIPE_DESC="Tiny, fast Linux for appliances, containers and small servers. Installs in about a minute."
RECIPE_NOTE="DHCP on the first wired port, SSH with your key; no firewall rules by default."
RECIPE_MIN_GB=1
RECIPE_METHOD=self
RECIPE_EFI_LABEL="alpine|UEFI OS|BOOT"

recipe_deploy() {
  apk add -q alpine-conf e2fsprogs dosfstools >/dev/null
  # the RAM OS overlay must not end up in the installed system
  mv /etc/local.d/pxe-agent.start /root/tk/pxe-agent.start.ramos 2>/dev/null || true
  echo "$HOSTNAME" > /etc/hostname
  printf 'auto lo\niface lo inet loopback\n\nauto eth0\niface eth0 inet dhcp\n' > /etc/network/interfaces
  rc-update add sshd default >/dev/null 2>&1; rc-update add networking boot >/dev/null 2>&1
  [ "$BOOT_MODE" = uefi ] && export USE_EFI=1
  rc=0
  ERASE_DISKS="$DISK" BOOTLOADER=grub setup-disk -m sys -s 0 "$DISK" > /root/tk/setup-disk.log 2>&1 || rc=$?
  tail -n 15 /root/tk/setup-disk.log
  mv /root/tk/pxe-agent.start.ramos /etc/local.d/pxe-agent.start 2>/dev/null || true   # keep the RAM OS agent alive
  [ $rc -eq 0 ] || { echo "setup-disk failed rc=$rc"; return 1; }
  # post-install touches on the new system (root = the ext4 partition setup-disk created)
  partprobe "$DISK" 2>/dev/null || true; sleep 1
  root=$(lsblk -lnpo NAME,FSTYPE "$DISK" | awk '$2=="ext4"{print $1}' | tail -n1)
  [ -n "$root" ] || { echo "no ext4 root partition found"; return 1; }
  mkdir -p /mnt/t; mount "$root" /mnt/t
  rm -f /mnt/t/etc/local.d/pxe-agent.start /mnt/t/etc/.default_boot_services
  echo "$HOSTNAME" > /mnt/t/etc/hostname
  mkdir -p /mnt/t/root/.ssh; cp /root/tk/authorized_keys /mnt/t/root/.ssh/authorized_keys 2>/dev/null || wget -qO /mnt/t/root/.ssh/authorized_keys "$SRV/authorized_keys"
  chmod 700 /mnt/t/root/.ssh; chmod 600 /mnt/t/root/.ssh/authorized_keys
  sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/; s/^#\?PermitRootLogin.*/PermitRootLogin prohibit-password/' /mnt/t/etc/ssh/sshd_config
  # no usable password but NOT locked: OpenSSH without PAM (Alpine) refuses even key logins for '!' accounts
  sed -i 's/^root:[^:]*:/root:*:/' /mnt/t/etc/shadow
  echo "alpine installed: $(cat /mnt/t/etc/alpine-release)"
}

recipe_verify() {
  fails=0
  chk() { if eval "$2"; then echo "  OK    $1"; else echo "  FAIL  $1"; fails=$((fails+1)); fi; }
  chk "kernel present"                'ls /mnt/t/boot/vmlinuz-* >/dev/null 2>&1'
  chk "SSH key installed"             '[ -s /mnt/t/root/.ssh/authorized_keys ]'
  chk "sshd enabled"                  '[ -e /mnt/t/etc/runlevels/default/sshd ]'
  chk "root key login possible (account not locked)" '! grep -q "^root:!" /mnt/t/etc/shadow'
  chk "RAM OS agent not copied"       '[ ! -e /mnt/t/etc/local.d/pxe-agent.start ]'
  chk "DHCP network config"           'grep -q "inet dhcp" /mnt/t/etc/network/interfaces'
  umount /mnt/t 2>/dev/null || true
  [ $fails -eq 0 ]
}
