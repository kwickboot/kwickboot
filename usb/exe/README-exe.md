# KwickBoot USB Creator - single .exe (GUI)

`KwickBootUSB.exe` is the whole Windows creator in one file: the script, a UAC manifest, and (optionally) the ISO
**appended inside the exe**. A customer downloads **one** file, double-clicks it, approves UAC, and a small window
opens to pick a USB stick and build it - no PowerShell, no `.cmd`, no console window, no second download.

## What it does when run
1. Launches with no console window (it is a Windows app, not a console app), self-elevates via its UAC manifest.
2. Extracts the embedded ISO to a temp folder (single-download builds).
3. Shows the GUI: a list of USB sticks, a **Create USB** / **Close** pair, a **Confirm write** dialog before erasing,
   and a live progress log while it builds. The build runs the same engine (`kwickboot-usb.ps1`) hidden underneath.
4. Cleans up the temp ISO when the window closes.

The text/unattended interface still works: `KwickBootUSB.exe -Console -Disk <n> -Confirm <serial> ...` (or run the
`.ps1` directly). `-Gui` forces the window; `-Console` forces text.

## Build
```powershell
# launcher only (customer supplies / downloads the ISO):
powershell -ExecutionPolicy Bypass -File usb\exe\build.ps1

# single file with the ISO (and its SHA256SUMS + SHA256SUMS.asc, if next to it) embedded - ONE download for customers:
powershell -ExecutionPolicy Bypass -File usb\exe\build.ps1 -Iso path\to\kwickboot-<ver>-amd64.iso
```
Output: `usb\exe\KwickBootUSB.exe` (~1 GB with the ISO; **unsigned**). Rebuild after any change to `kwickboot-usb.ps1`
or the ISO. Embed the SIGNED `SHA256SUMS` + `SHA256SUMS.asc` (next to the ISO) so the creator verifies the ISO itself;
with no signature files it builds the stick UNVERIFIED (a warning is shown).

## Sign it (so Windows does not say "unknown publisher")
Windows SmartScreen / UAC show "unknown publisher" until the exe is **Authenticode code-signed** with a certificate
issued to Jins Tech Corp by a public CA (DigiCert, Sectigo, ...). This is separate from the GPG release key that signs
the ISO. A self-signed cert does **not** help for public distribution.
- **OV** cert: cheaper; SmartScreen reputation builds up over the first weeks/downloads.
- **EV** cert (hardware token/HSM): instant SmartScreen trust.

Sign with the Windows SDK `signtool` (always timestamp - the signature outlives the cert). Sign **after** embedding the
ISO (the Authenticode signature then covers the whole file, ISO included):
```powershell
signtool sign /fd SHA256 /f JinsTechCorp-CodeSign.pfx /p <pfx-password> `
  /tr http://timestamp.digicert.com /td SHA256 usb\exe\KwickBootUSB.exe
signtool verify /pa /v usb\exe\KwickBootUSB.exe
```

## What ships to customers
- **`KwickBootUSB.exe`** - Windows: one download, GUI, builds the stick (ISO inside).
- `kwickboot-usb.sh` - Linux: `sudo sh kwickboot-usb.sh` (downloads/verifies the ISO, or `--iso <file>`).
- `kwickboot-<ver>-amd64.iso` - for people who prefer Rufus (DD) / Etcher / `dd`, or booting in a VM.
