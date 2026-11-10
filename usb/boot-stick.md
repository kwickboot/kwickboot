# KwickBoot tiny boot stick (for clients without PXE)

Some PCs can't network-boot: the firmware has no PXE/network-boot option, it's
disabled and locked, or the NIC's PXE ROM is broken. For those, make a **tiny
(~1 MB) KwickBoot boot stick**. It is *not* a server and holds no OS — it just
starts iPXE, which immediately chain-loads this KwickBoot server over the LAN
and shows the same boot menu a PXE client would get (Run-in-RAM, installer,
rescue, wipe, …).

This is different from `kwickboot-usb.ps1` / the **USB Creator**, which builds a
full *portable server* stick (GB-sized). This one is a disposable *client*
helper you can leave plugged into a no-PXE machine.

## Why it works without PXE

The embedded iPXE carries its **own NIC drivers**, so it does not need the
firmware's PXE/UNDI stack. On boot it runs (see `linux/build-ipxe.sh`):

```
dhcp -> chain http://<server>:8080/boot.ipxe?mac=${mac}
```

i.e. identical to a real PXE client, just loaded from USB instead of firmware.
(The old `undionly.kpxe` is **not** usable here — it depends on firmware UNDI.)

## Build the media (on the build box, NOT the production server)

```sh
# produces $OUT/noPXE/  (set OUT + server IP as for the normal iPXE build)
IPXE_SRC=/opt/src/ipxe KB_OUT=/tmp/ipxeout sh linux/build-ipxe.sh <server_ip> 8080
```

Artifacts in `noPXE/`:

| File | For | How to write it |
|---|---|---|
| `kwickboot-boot.img` | **BIOS + UEFI** (one hybrid image) | Rufus in **DD mode**, or `dd if=kwickboot-boot.img of=/dev/sdX bs=1M` |
| `kwickboot-boot-bios.iso` | legacy-BIOS-only machines | Rufus DD mode / `dd` |
| `stick/EFI/BOOT/BOOTX64.EFI` (+ IA32/AA64) | UEFI, **no reformat** | just copy the `EFI/` folder onto any existing **FAT32** stick |

The copy method is the gentlest: a FAT32 stick with `EFI/BOOT/BOOTX64.EFI`
present is UEFI-bootable; nothing else on the stick is touched.

## Notes

- The server IP is baked into the image at build time (the embedded script also
  falls back to the proxyDHCP `next-server`, so a rebuilt-with-wrong-IP stick
  still works on a LAN where KwickBoot answers proxyDHCP).
- Secure Boot: iPXE itself is unsigned, so a target with Secure Boot **on** will
  reject it — either turn Secure Boot off for that one boot, or use the signed
  shim path (firmware PXE / the full ISO). For locked-down fleets, PXE/HTTP boot
  via the signed chain remains the route.
- Multi-arch: build `ipxe.efi` for `i386-efi` / `arm64-efi` too and the script
  drops `BOOTIA32.EFI` / `BOOTAA64.EFI` alongside automatically.
