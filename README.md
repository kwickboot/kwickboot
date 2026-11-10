<p align="center">
  <a href="https://kwickboot.com">
    <img src="https://kwickboot.com/assets/img/kwickboot-logo.png" alt="KwickBoot" width="680">
  </a>
</p>

# KwickBoot

KwickBoot is a self-hosted network-boot server. PCs boot a small RAM OS from it over the network, report their
hardware, and are installed, restored, wiped or repaired from a web console. It works next to your existing DHCP
server and never hands out IP addresses.

Free software under the GNU AGPL-3.0. © Jins Tech Corp.

## Requirements

| | |
|---|---|
| Server OS | Debian 13 (trixie), x86-64. A fresh minimal install is best. |
| Hardware | 2 CPU cores, 4 GB RAM, 30 GB+ disk (more for ISOs and images). A Celeron mini PC is enough. |
| Network | Wired, a fixed IPv4 address (a DHCP reservation is fine), on the same LAN as the PCs. |
| PCs | UEFI (IPv4 network boot) or legacy BIOS PXE. |
| Time | About 15 minutes to install. |

## Ports

| Port | Use |
|---|---|
| UDP 67, 4011 | proxyDHCP (boot information only; your DHCP server keeps assigning addresses) |
| UDP 69 | TFTP (boot loaders) |
| TCP 8080 | web console and HTTP boot files |
| TCP 8444 | managed machines (encrypted, key-pinned channel) |
| UDP 9000-9199 | multicast (one stream to a whole room) |
| TCP 3142 | package cache (apt-cacher-ng, on by default; `--no-cache` to disable) |

Only the LAN needs these ports. With `--firewall`, the installer writes an nftables firewall that allows exactly these
from the server's LAN (plus SSH) and drops everything else. If the host already has a firewall, the installer prints
the rules to add instead.

## Install

```sh
tar -xzf kwickboot-<version>.tar.gz
cd kwickboot-<version>
sha256sum -c SHA256SUMS --quiet      # optional: install.sh checks this too
sudo sh install.sh --firewall
```

Options: `--iface <nic>` (the NIC facing the PCs; default: the one with the default route), `--firewall`,
`--no-cache` (the local package cache is on by default), `--ai-sdk` (the optional AI pack's SDK, hash-locked), `--offline` (no internet:
the Debian packages python3, python3-cryptography, udpcast, iproute2, cabextract, 7zip and innoextract must already be installed), `--yes`.

The installer creates the first admin account (you type the password; it is never shown or stored in plain text).
With `--yes`, create it later: `sudo pxectl useradd admin admin`.

Upgrading: unpack the new release and run `install.sh` again. Code, console and boot files are replaced; the
database, machines, logs, configuration, keys, images and ISOs are kept.

### Compact and full-offline downloads

The normal release is compact: it contains the complete amd64 PXE/deployment server and rescue tools, but leaves out
optional arm64/i386 Core files and the local RICH WebKit desktop pack. Its offline server image is headless—the web
console is opened from any browser on the LAN, while the server itself retains a normal text console. This is the right
download for most sites and remains fully offline.

Release builders can set `KB_RELEASE_PROFILE=full` for a self-contained, air-gapped mixed-architecture bundle. The
installer ISO follows the same model with `KB_ISO_PROFILE=full`; build its graphical server image with
`KB_SERVER_PROFILE=full`. Its filename ends in `-full.iso`. Experimental CPU architectures are added explicitly with
`KB_ARCHES`, rather than silently increasing every user's download. ISO-embedded releases also reuse the medium's Core
and rescue files rather than storing duplicate copies inside the release archive.

Expected end of the output:

```
==> KwickBoot is running: http://<server-ip>:8080/ui/
```

## First machine

1. Open `http://<server-ip>:8080/ui/` and sign in.
2. **Machines → Discover new machines** opens a time-limited discovery window.
3. Network-boot a PC (firmware boot menu, usually F12: "UEFI IPv4" or "PXE"). It boots the RAM OS (nothing is
   written to its disks) and appears in the console with its hardware: CPU, RAM, disks with serial numbers and
   health, network cards, firmware, Secure Boot, TPM.
4. **Approve** it and give it a name.
5. Open it and choose **Install**: the operating system, the target disk, and type the hostname to confirm. The
   install runs on the PC; the console shows each step, the PC's screen and the log.

Every disk write is tied to the disk's serial number and confirmed by typing; installs take the whole target disk.

## Everyday commands

```sh
sudo pxectl status                  # service and discovery window
sudo pxectl log                     # live server log
sudo pxectl machines                # all machines
sudo pxectl discover 30             # open discovery for 30 minutes
sudo pxectl iso add <https-url> <sha256>    # add an installer ISO from its official source
sudo pxectl useradd <user> viewer|operator|manager|admin
sudo pxectl audit 50                # the last 50 audit log entries
```

`pxectl` with no arguments lists everything.

## Troubleshooting

1. **The PC doesn't get a boot menu / "No boot filename received".** The PC and the server must be on the same LAN
   (or the router must relay DHCP to the server). Check that UDP 67/4011/69 are open on the server and that no other
   PXE server (WDS, another proxyDHCP) answers on the LAN. Discovery must be open for a new machine:
   `sudo pxectl discover 30`.
2. **UEFI PC boots the network but stops with Secure Boot errors.** KwickBoot boots through Microsoft-signed shim;
   if a PC's firmware rejects it, update the firmware or temporarily switch Secure Boot off for the install.
3. **The machine appears but stays "booting".** The RAM OS could not reach TCP 8080 on the server - check the
   firewall and `server_ip` in `/opt/pxe-toolkit/config.json`.
4. **After the install the PC boots the RAM OS again instead of the new system.** KwickBoot keeps the network first
   in the boot order and sends an installed machine to its disk; if its default was changed to the RAM OS, set it back
   to **Local disk** in the console (or `sudo pxectl default <mac> local`). If the hand-off itself fails on a
   particular firmware, try `sudo pxectl localboot <mac> exit` (or `chainload`).
5. **Multicast falls back to direct downloads.** UDP 9000-9199 must be open on the server, and the switch must pass
   multicast (IGMP snooping without a querier can block it). Machines that miss the stream fetch the image directly,
   so the install still completes.

Logs: `/opt/pxe-toolkit/logs/` (server), the console's job log per machine, and `journalctl -u pxe-toolkit`.

## Your data

KwickBoot installs, restores and wipes disks: every install takes the whole target disk. You are solely responsible for
your data and its backups (terms: https://kwickboot.com/terms/). Keep user files on a server share, in the cloud or
on a separate disk you do not select. An admin accepts this notice once in the console; it is recorded in the audit
log.

## Security notes

- The RAM OS holds no secrets. Anything in `http/` can be fetched by any PC that network-boots, so keys and
  passwords never go there.
- Installs, wipes, terminals and screen sessions are written to the audit log with the user's name.
- KwickBoot never redistributes operating systems: it downloads the vendor's official release (or uses your own
  media), verifies the vendor's checksum, and installs it.
- Windows signing status, scope and release controls are documented in [CODE_SIGNING.md](CODE_SIGNING.md).

## Community and contributions

- Ask setup and design questions in [GitHub Discussions](https://github.com/kwickboot/kwickboot/discussions).
- Report reproducible defects with the issue templates and include the firmware, NIC and boot stage involved.
- See [CONTRIBUTING.md](CONTRIBUTING.md) before sending a pull request.
- Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

Hardware compatibility reports from school labs, MSPs, refurbishers and homelabs are especially valuable.

## License

GNU Affero General Public License v3.0 or later. If you run a modified KwickBoot for others over a network, you
must offer them its source.
