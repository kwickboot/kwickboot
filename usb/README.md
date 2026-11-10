# KwickBoot USB Creator

Makes a KwickBoot USB stick: a portable KwickBoot server that also installs itself, wipes disks and rescues PCs.

Stick menu (UEFI, Secure Boot may stay ON):

| Entry | What it does |
|---|---|
| **Run from this stick** (default) | The KwickBoot server runs in RAM. Nothing is installed. Settings stay on the stick (`kwickboot\state.img`), and the stick's `iso\` and `images\` folders are the library that PCs network-boot from |
| Install the KwickBoot server onto this PC | The installer (erases the disk you choose, after typed confirmations) |
| Securely wipe this PC's disks | NIST SP 800-88 Rev. 2 (Purge with the strongest IEEE 2883 technique the disk supports), verified. The report is saved in `wipe-reports\` on the stick |
| Rescue shell | Disk, file and hardware tools (smartctl, nvme, ntfs-3g, BitLocker via cryptsetup, partclone, sgdisk, efibootmgr, memtester, stress-ng). Rescued files go to the stick |

`kwickboot.cfg` `start=live` (default) or `start=install` picks what the plain menu entry does.

| Partition | Label | File system | Contents |
|---|---|---|---|
| 1 (most of the stick) | `KWICKDATA` | exFAT | `kwickboot.cfg` (installer answers), `iso\` (drop ISO files here), `README.txt` |
| 2 (2 GB, at the end) | `KWICKBOOT` | FAT32, EFI system partition | the verified KwickBoot ISO's files: signed shim + GRUB (Secure Boot ON works), RAM OS, server image |

What it does that a plain image writer doesn't:

1. **Checks the release** before writing: the GPG signature of `SHA256SUMS` against the built-in Jins Tech Corp
   release key (fingerprint `2457 EAE1 8700 74A6 8CC9 630A 6A31 5D0A E638 3C48`), then the ISO's SHA-256.
   No gpg install needed on Windows.
2. **Lists USB disks only.** It never lists internal disks, and it erases a stick only after you type its serial (or its name).
3. **Pre-configures the server** (`kwickboot.cfg`): name, time zone, DHCP or static address, and which disk to install on.
   With `disk=only-internal` the server installs without a keyboard. There's a 30-second countdown on screen, and a disk that
   already holds a KwickBoot server is never erased automatically. No passwords ever go on the stick: the first admin
   is created in the web console with the setup code shown on the server's screen.
4. **Reads the stick back** and compares every file with the ISO.
5. **Keeps your ISOs**: anything in `iso\` is copied into the new server's library (when its disk has room).
   `-Update` / `--update` puts a new KwickBoot version on the stick and keeps `KWICKDATA`.

## Windows

Double-click `KwickBoot USB Creator.cmd` (it asks for administrator rights: it partitions the stick), or:

```powershell
.\kwickboot-usb.ps1                                         # download the latest release, ask for everything
.\kwickboot-usb.ps1 -Iso D:\kwickboot-<ver>-amd64.iso         # SHA256SUMS + SHA256SUMS.asc next to the ISO
.\kwickboot-usb.ps1 -Update                                 # new version on an existing stick, data kept
```

## Linux

```sh
sudo sh kwickboot-usb.sh                                    # needs util-linux, exfatprogs, dosfstools, gpgv
sudo sh kwickboot-usb.sh --iso kwickboot-<ver>-amd64.iso --image stick.img --size 8G   # an image file (VMs, testing)
```

## Notes

- UEFI only. The KwickBoot server needs UEFI; the plain ISO (written with dd / Rufus DD mode) also boots legacy BIOS.
- Booting the dropped ISOs straight from the stick's menu isn't possible yet. Debian's Secure Boot-signed GRUB can't
  read exFAT. The ISOs go into the server's library and are network-booted from there.
- The settings file on the stick holds the server's database and keys. Whoever has the stick has them, so keep it like a key.
- Wipe reports made on the stick are unsigned. A wipe run by a KwickBoot server (installed or live) over the network
  gets a signed certificate.
- Ready-made OS images (Debian, Ubuntu, Jellyfin, Proxmox: about 5 GB) are not on the ISO. Put them in `images\`
  (with their `.sha256`) for fully offline sites. Windows needs your own ISO in `iso\`.
- `kwickboot.cfg` keys: `start` (`live`/`install`), `host`, `timezone`, `network` (`dhcp`/`static`), `address`, `gateway`, `dns`, `disk`
  (`ask` / `only-internal` / a serial), `test_lab` (`yes`/`no`), `import_isos` (`yes`/`no`). Delete a line to be asked on screen.
