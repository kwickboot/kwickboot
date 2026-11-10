<#
KwickBoot USB Creator (Windows) - SPDX-License-Identifier: AGPL-3.0-or-later
Makes a KwickBoot USB stick:
  partition 1  KWICKDATA  exFAT, the rest of the stick: kwickboot.cfg (installer answers) + iso\ (drop ISOs here:
                          the installer copies them into the new server's library)
  partition 2  KWICKBOOT  FAT32 boot + system: the verified KwickBoot ISO's files (UEFI, Secure Boot via signed shim)
  MBR partition table + GRUB in the MBR gap: the stick boots in UEFI (Secure Boot) and legacy BIOS mode.
Before writing it checks the release signature (GPG, Jins Tech Corp release key - checked here, no gpg needed) and the
ISO's SHA-256; afterwards it reads every file back from the stick and compares.
Only USB disks are listed; the stick is erased only after you type its serial (or name).

  .\kwickboot-usb.ps1                                   download the latest release, ask for everything
  .\kwickboot-usb.ps1 -Iso D:\kwickboot-2026.11.03f-amd64.iso      use a downloaded ISO (SHA256SUMS + .asc next to it)
  .\kwickboot-usb.ps1 -Update                           new KwickBoot version on an existing stick, data partition kept
  Unattended: -Disk <number> -Confirm <serial or name> [-Host name] [-TimeZone Area/City] [-Static 192.168.1.10/24
              -Gateway 192.168.1.1 -Dns 192.168.1.1] [-InstallDisk ask|only-internal|<serial>] [-Yes]
#>
param(
  [string]$Iso, [string]$Url = "https://kwickboot.com/download/latest/", [switch]$Update,
  [int]$Disk = -1, [string]$Confirm, [string]$ServerName, [string]$TimeZone, [string]$Static, [string]$Gateway, [string]$Dns,
  [string]$InstallDisk, [ValidateSet("", "live", "install")][string]$Start = "", [switch]$Yes, [switch]$AllowVirtual, [int]$BootMB = 2048,
  [switch]$Gui, [switch]$Console, [switch]$Unverified,
  [switch]$NoPxe, [string]$NoPxeImg   # NoPxe: make the tiny no-PXE boot stick (raw-write NoPxeImg) instead of the server/installer stick
)
$ErrorActionPreference = "Stop"
$VERSION = "1.0"
$FPR = "2457EAE1870074A68CC9630A6A315D0AE6383C48"     # Jins Tech Corp KwickBoot Release <release@kwickboot.com>
# the release public key (the same as kwickboot-release-key.asc on the site and in the repository)
$KEY = @'
-----BEGIN PGP PUBLIC KEY BLOCK-----

mDMEauqajxYJKwYBBAHaRw8BAQdA6HpYOsCX9WcCXj6PBWqC3YVS8R/OCVnuqFy6
teRW+Km0OEppbnMgVGVjaCBDb3JwIEt3aWNrQm9vdCBSZWxlYXNlIDxyZWxlYXNl
QGt3aWNrYm9vdC5jb20+iJYEExYKAD4WIQQkV+rhhwB0pozJYwpqMV0K5jg8SAUC
auqajwIbAwUJBaOagAULCQgHAgYVCgkICwIEFgIDAQIeAQIXgAAKCRBqMV0K5jg8
SCbaAQCfQQ8DYpTnwyrAwgSkhiQyREBM5WUVLByt6mKKhbrYHgEA1oOa0ttkNlCn
7xblI5iTIvIYHWarHwznc2rSf27f0Ag=
=lktx
-----END PGP PUBLIC KEY BLOCK-----
'@

function Say($t) { Write-Host "==> $t" -ForegroundColor Cyan }
function Warn($t) { Write-Host "WARNING: $t" -ForegroundColor Yellow }
function Die($t) { Write-Host "ERROR: $t" -ForegroundColor Red; exit 1 }
function Ask($q, $def) { if ($Yes) { return $def }; $a = Read-Host "$q$(if ($def) { " [$def]" })"; if ($a) { $a } else { $def } }

# ---- administrator (partitioning a disk needs it)
$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  Write-Host "The USB creator needs administrator rights (it partitions the stick). Restarting elevated..."
  $a = @("-NoProfile", "-ExecutionPolicy", "Bypass")
  if ($Gui) { $a += @("-WindowStyle", "Hidden") } else { $a += "-NoExit" }   # GUI: no console window
  $a += @("-File", "`"$PSCommandPath`"")
  foreach ($k in $PSBoundParameters.Keys) { $v = $PSBoundParameters[$k]
    if ($v -is [switch]) { if ($v) { $a += "-$k" } } else { $a += "-$k"; $a += "`"$v`"" } }
  Start-Process powershell.exe -Verb RunAs -ArgumentList $a; exit
}

# single-file exe: the ISO (and, when present, SHA256SUMS + SHA256SUMS.asc) are appended to KwickBootUSB.exe so the
# customer downloads one file. The launcher sets KBUSB_EXE to its own path; here (elevated) we extract them to a temp
# folder and use that ISO. Footer at the end of the exe: isolen(int64) namelen(int32) extralen(int32) magic("KBUSBIS1").
function Get-EmbeddedIso($exe) {
  $fs = [IO.File]::OpenRead($exe)
  try {
    $len = $fs.Length; if ($len -lt 40) { return $null }
    $fs.Seek($len - 24, 'Begin') | Out-Null; $foot = New-Object byte[] 24; $null = $fs.Read($foot, 0, 24)
    if ([Text.Encoding]::ASCII.GetString($foot, 16, 8) -ne 'KBUSBIS1') { return $null }
    $isolen = [BitConverter]::ToInt64($foot, 0); $namelen = [BitConverter]::ToInt32($foot, 8); $extralen = [BitConverter]::ToInt32($foot, 12)
    $base = $len - 24 - $extralen - $namelen - $isolen; if ($base -lt 0) { return $null }
    $dir = Join-Path ([IO.Path]::GetTempPath()) ("kwickboot-usb-" + [Guid]::NewGuid().ToString("N")); New-Item -ItemType Directory -Force $dir | Out-Null
    $fs.Seek($base + $isolen, 'Begin') | Out-Null; $nb = New-Object byte[] $namelen; $null = $fs.Read($nb, 0, $namelen); $name = [Text.Encoding]::UTF8.GetString($nb)
    if ($extralen -gt 0) {   # SHA256SUMS + SHA256SUMS.asc as "file\0bytes\0file\0bytes" (len-prefixed) - optional
      $fs.Seek($base + $isolen + $namelen, 'Begin') | Out-Null; $eb = New-Object byte[] $extralen; $null = $fs.Read($eb, 0, $extralen); $o = 0
      while ($o -lt $extralen) { $fnl = [BitConverter]::ToInt32($eb, $o); $o += 4; $fn = [Text.Encoding]::UTF8.GetString($eb, $o, $fnl); $o += $fnl
        $bl = [BitConverter]::ToInt32($eb, $o); $o += 4; [IO.File]::WriteAllBytes((Join-Path $dir $fn), $eb[$o..($o + $bl - 1)]); $o += $bl } }
    $out = Join-Path $dir $name; $fs.Seek($base, 'Begin') | Out-Null
    $w = [IO.File]::Create($out); try { $buf = New-Object byte[] 1048576; $rem = $isolen
      while ($rem -gt 0) { $n = $fs.Read($buf, 0, [Math]::Min($buf.Length, [int][Math]::Min($rem, $buf.Length))); if ($n -le 0) { break }; $w.Write($buf, 0, $n); $rem -= $n } }
    finally { $w.Close() }
    return $out
  } finally { $fs.Close() }
}

Add-Type -TypeDefinition @'
using System; using System.IO; using System.Text; using System.Numerics; using System.Collections.Generic;
using System.Security.Cryptography; using System.Runtime.InteropServices; using Microsoft.Win32.SafeHandles;
public static class KbDisk {
  [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
  static extern SafeFileHandle CreateFile(string name, uint access, uint share, IntPtr sa, uint disp, uint flags, IntPtr tmpl);
  // legacy BIOS boot: GRUB's MBR code into bytes 0-439 of sector 0 (disk signature + partition table stay) and its core
  // from sector 1 - the gap before the first partition, outside every volume. Read back and compared.
  public static void WriteBios(int disk, byte[] mbr, byte[] core) {
    using (var h = CreateFile(@"\\.\PhysicalDrive" + disk, 0xC0000000, 3, IntPtr.Zero, 3, 0, IntPtr.Zero)) {
      if (h.IsInvalid) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
      using (var fs = new FileStream(h, FileAccess.ReadWrite, 1)) {
        int n = (core.Length + 511) / 512; var buf = new byte[512 * (1 + n)];
        fs.Position = 0; if (fs.Read(buf, 0, 512) != 512) throw new IOException("cannot read sector 0");
        Array.Copy(mbr, 0, buf, 0, Math.Min(440, mbr.Length)); Array.Copy(core, 0, buf, 512, core.Length);
        fs.Position = 0; fs.Write(buf, 0, buf.Length); fs.Flush();
        var chk = new byte[buf.Length]; fs.Position = 0; int got = 0;
        while (got < chk.Length) { int r = fs.Read(chk, got, chk.Length - got); if (r <= 0) break; got += r; }
        for (int i = 0; i < buf.Length; i++) if (chk[i] != buf[i]) throw new IOException("BIOS boot sectors differ after writing");
      }
    }
  }
}
public static class KbPgp {
  static readonly BigInteger P = BigInteger.Pow(2, 255) - 19;
  static readonly BigInteger L = BigInteger.Pow(2, 252) + BigInteger.Parse("27742317777372353535851937790883648493");
  static BigInteger Mod(BigInteger a) { a %= P; return a.Sign < 0 ? a + P : a; }
  static BigInteger Inv(BigInteger a) { return BigInteger.ModPow(Mod(a), P - 2, P); }
  static readonly BigInteger D = Mod(new BigInteger(-121665) * Inv(121666));
  static readonly BigInteger I = BigInteger.ModPow(2, (P - 1) / 4, P);
  static BigInteger LE(byte[] b, int off, int len) { var t = new byte[len + 1]; Array.Copy(b, off, t, 0, len); return new BigInteger(t); }
  static BigInteger[] Add(BigInteger[] p, BigInteger[] q) {           // extended twisted Edwards (a = -1), RFC 8032
    var a = Mod((p[1] - p[0]) * (q[1] - q[0])); var b = Mod((p[1] + p[0]) * (q[1] + q[0]));
    var c = Mod(p[3] * 2 * D * q[3]); var d = Mod(p[2] * 2 * q[2]);
    var e = b - a; var f = d - c; var g = d + c; var h = b + a;
    return new[] { Mod(e * f), Mod(g * h), Mod(f * g), Mod(e * h) };
  }
  static BigInteger[] Mul(BigInteger s, BigInteger[] p) {
    var q = new[] { BigInteger.Zero, BigInteger.One, BigInteger.One, BigInteger.Zero };
    while (s > 0) { if (!s.IsEven) q = Add(q, p); p = Add(p, p); s >>= 1; }
    return q;
  }
  static BigInteger[] Decode(byte[] b) {
    if (b.Length != 32) return null;
    var t = (byte[])b.Clone(); int sign = t[31] >> 7; t[31] &= 0x7f;
    var y = LE(t, 0, 32); if (y >= P) return null;
    var xx = Mod(Mod(y * y - 1) * Inv(Mod(D * y * y + 1)));
    var x = BigInteger.ModPow(xx, (P + 3) / 8, P);
    if (Mod(x * x - xx) != 0) x = Mod(x * I);
    if (Mod(x * x - xx) != 0) return null;
    if ((int)(x & 1) != sign) { if (x == 0) return null; x = P - x; }
    return new[] { x, y, BigInteger.One, Mod(x * y) };
  }
  static bool Eq(BigInteger[] p, BigInteger[] q) { return Mod(p[0] * q[2] - q[0] * p[2]) == 0 && Mod(p[1] * q[2] - q[1] * p[2]) == 0; }
  static BigInteger[] Base() {
    var yb = Mod(4 * Inv(5)).ToByteArray(); var b = new byte[32]; Array.Copy(yb, 0, b, 0, Math.Min(32, yb.Length)); return Decode(b);
  }
  public static bool Ed25519(byte[] pub, byte[] msg, byte[] sig) {
    if (sig.Length != 64 || pub.Length != 32) return false;
    var A = Decode(pub); var rb = new byte[32]; Array.Copy(sig, 0, rb, 0, 32); var R = Decode(rb);
    if (A == null || R == null) return false;
    var S = LE(sig, 32, 32); if (S >= L) return false;
    byte[] h; using (var sha = SHA512.Create()) {
      var m = new byte[64 + msg.Length]; Array.Copy(sig, 0, m, 0, 32); Array.Copy(pub, 0, m, 32, 32); Array.Copy(msg, 0, m, 64, msg.Length);
      h = sha.ComputeHash(m); }
    var k = LE(h, 0, 64) % L;
    return Eq(Mul(S, Base()), Add(R, Mul(k, A)));
  }
  public static byte[] Dearmor(string s) {
    var sb = new StringBuilder(); bool body = false, hdr = false;
    foreach (var raw in s.Replace("\r", "").Split('\n')) {
      var ln = raw.Trim();
      if (ln.StartsWith("-----BEGIN")) { body = true; hdr = false; continue; }
      if (ln.StartsWith("-----END")) break;
      if (!body) continue;
      if (!hdr) { if (ln.Length == 0) hdr = true; continue; }
      if (ln.StartsWith("=")) continue;
      sb.Append(ln);
    }
    return Convert.FromBase64String(sb.ToString());
  }
  static List<KeyValuePair<int, byte[]>> Packets(byte[] d) {
    var r = new List<KeyValuePair<int, byte[]>>(); int i = 0;
    while (i < d.Length) {
      int h = d[i++]; if ((h & 0x80) == 0) throw new Exception("not an OpenPGP packet");
      int tag; int len;
      if ((h & 0x40) != 0) {
        tag = h & 0x3f; int l1 = d[i++];
        if (l1 < 192) len = l1; else if (l1 < 224) { len = ((l1 - 192) << 8) + d[i++] + 192; }
        else if (l1 == 255) { len = (d[i] << 24) | (d[i + 1] << 16) | (d[i + 2] << 8) | d[i + 3]; i += 4; }
        else throw new Exception("partial packet lengths are not supported");
      } else {
        tag = (h >> 2) & 0xf; int lt = h & 3;
        if (lt == 0) len = d[i++]; else if (lt == 1) { len = (d[i] << 8) | d[i + 1]; i += 2; }
        else if (lt == 2) { len = (d[i] << 24) | (d[i + 1] << 16) | (d[i + 2] << 8) | d[i + 3]; i += 4; } else len = d.Length - i;
      }
      var b = new byte[len]; Array.Copy(d, i, b, 0, len); i += len; r.Add(new KeyValuePair<int, byte[]>(tag, b));
    }
    return r;
  }
  public static byte[] PubKey(string armored, out string fpr) {
    foreach (var p in Packets(Dearmor(armored))) if (p.Key == 6) {
      var b = p.Value; if (b[0] != 4 || b[5] != 22) throw new Exception("not an EdDSA v4 key");
      int m = 7 + b[6]; int bits = (b[m] << 8) | b[m + 1];
      if ((bits + 7) / 8 != 33 || b[m + 2] != 0x40) throw new Exception("unexpected Ed25519 key encoding");
      var pk = new byte[32]; Array.Copy(b, m + 3, pk, 0, 32);
      using (var sha = SHA1.Create()) {
        var pre = new byte[3 + b.Length]; pre[0] = 0x99; pre[1] = (byte)(b.Length >> 8); pre[2] = (byte)b.Length;
        Array.Copy(b, 0, pre, 3, b.Length); fpr = BitConverter.ToString(sha.ComputeHash(pre)).Replace("-", ""); }
      return pk;
    }
    throw new Exception("no public key in the key file");
  }
  static byte[] Mpi(byte[] b, ref int j) { int n = (((b[j] << 8) | b[j + 1]) + 7) / 8; var r = new byte[n]; Array.Copy(b, j + 2, r, 0, n); j += 2 + n; return r; }
  public static string Verify(byte[] data, string sigArmored, byte[] pub) {
    foreach (var p in Packets(Dearmor(sigArmored))) if (p.Key == 2) {
      var b = p.Value; if (b[0] != 4) return "unsupported signature version";
      if (b[2] != 22) return "not an EdDSA signature"; if (b[1] != 0) return "not a binary-document signature";
      int hashedEnd = 6 + ((b[4] << 8) | b[5]); int j = hashedEnd + 2 + ((b[hashedEnd] << 8) | b[hashedEnd + 1]);
      int left = (b[j] << 8) | b[j + 1]; j += 2;
      var r = Mpi(b, ref j); var s = Mpi(b, ref j);
      HashAlgorithm H = b[3] == 8 ? (HashAlgorithm)SHA256.Create() : b[3] == 10 ? (HashAlgorithm)SHA512.Create() : b[3] == 9 ? (HashAlgorithm)SHA384.Create() : null;
      if (H == null) return "unsupported hash algorithm " + b[3];
      var tr = new byte[] { 4, 0xff, (byte)(hashedEnd >> 24), (byte)(hashedEnd >> 16), (byte)(hashedEnd >> 8), (byte)hashedEnd };
      H.TransformBlock(data, 0, data.Length, null, 0); H.TransformBlock(b, 0, hashedEnd, null, 0); H.TransformFinalBlock(tr, 0, 6);
      var dg = H.Hash; if (((dg[0] << 8) | dg[1]) != left) return "BAD signature (the file was changed)";
      var sig = new byte[64]; Array.Copy(r, 0, sig, 32 - r.Length, r.Length); Array.Copy(s, 0, sig, 64 - s.Length, s.Length);
      return Ed25519(pub, dg, sig) ? "OK" : "BAD signature";
    }
    return "no signature in the file";
  }
}
'@ -ReferencedAssemblies System.Numerics

Write-Host ""
Write-Host "KwickBoot USB Creator $VERSION" -ForegroundColor White
Write-Host "KwickBoot installs, restores and wipes disks. You are solely responsible for your data and its backups."
Write-Host "KwickBoot is free software (GNU AGPL-3.0) and comes with NO WARRANTY. Terms: https://kwickboot.com/terms/"
Write-Host ""

# ---- GUI (default when double-clicked): pick a USB stick from a list, confirm, watch the build. -Console or -Disk = text UI.
function Show-ModeChooser {   # explicit up-front choice so the user knows which stick they are making
  $bg = [System.Drawing.Color]::FromArgb(27, 34, 54); $fg = [System.Drawing.Color]::FromArgb(207, 214, 228); $accent = [System.Drawing.Color]::FromArgb(217, 119, 87)
  $f = New-Object System.Windows.Forms.Form; $f.Text = "KwickBoot USB Creator"; $f.Size = New-Object System.Drawing.Size(560, 300)
  $f.StartPosition = "CenterScreen"; $f.BackColor = $bg; $f.ForeColor = $fg; $f.FormBorderStyle = "FixedDialog"; $f.MaximizeBox = $false; $f.MinimizeBox = $false
  $f.Font = New-Object System.Drawing.Font("Segoe UI", 10)
  $t = New-Object System.Windows.Forms.Label; $t.Text = "What do you want to make?"; $t.AutoSize = $true; $t.Location = New-Object System.Drawing.Point(24, 20)
  $t.ForeColor = [System.Drawing.Color]::White; $t.Font = New-Object System.Drawing.Font("Segoe UI", 15, [System.Drawing.FontStyle]::Bold); $f.Controls.Add($t)
  $mkbtn = { param($text, $desc, $y, $tag)
    $b = New-Object System.Windows.Forms.Button; $b.Text = "$text`r`n$desc"; $b.Location = New-Object System.Drawing.Point(24, $y); $b.Size = New-Object System.Drawing.Size(504, 78)
    $b.TextAlign = "MiddleLeft"; $b.FlatStyle = "Flat"; $b.BackColor = [System.Drawing.Color]::FromArgb(33, 41, 64); $b.ForeColor = $fg
    $b.Font = New-Object System.Drawing.Font("Segoe UI", 11); $b.FlatAppearance.BorderColor = $accent
    # Keep the mode on the button and resolve its owning form when clicked.
    # Capturing $f here is unsafe: PowerShell event handlers run later and the
    # captured variable can resolve to a different object, causing "$f.Tag"
    # to fail even though Windows.Forms.Form itself supports Tag.
    $b.Tag = $tag
    $b.Add_Click({
      $owner = $this.FindForm()
      if ($owner) { $owner.Tag = [string]$this.Tag; $owner.Close() }
    }); $f.Controls.Add($b) }
  & $mkbtn "KwickBoot server / installer stick" "Full bootable USB to install or run KwickBoot (needs the ISO, 8 GB+ stick)." 62 "server"
  & $mkbtn "No-PXE boot stick (tiny)" "For PCs that can't PXE-boot - finds this LAN's KwickBoot server by broadcast. ~2 MB." 154 "nopxe"
  [void]$f.ShowDialog()
  return [string]$f.Tag }

function Show-CreatorGui {
  Add-Type -AssemblyName System.Windows.Forms, System.Drawing
  [System.Windows.Forms.Application]::EnableVisualStyles()
  $mode = Show-ModeChooser
  if (-not $mode) { return }   # chooser closed without a choice
  $self = $PSCommandPath
  $accent = [System.Drawing.Color]::FromArgb(217, 119, 87); $bg = [System.Drawing.Color]::FromArgb(27, 34, 54)
  $panel = [System.Drawing.Color]::FromArgb(33, 41, 64); $fg = [System.Drawing.Color]::FromArgb(207, 214, 228)
  $mk = { param($t, $x, $y, $w, $h) $c = New-Object ("System.Windows.Forms." + $t); if ($x) { $c.Location = New-Object System.Drawing.Point($x, $y) }; if ($w) { $c.Size = New-Object System.Drawing.Size($w, $h) }; $c }

  $form = & $mk Form; $form.Text = "KwickBoot USB Creator"; $form.Size = New-Object System.Drawing.Size(680, 560)
  $form.StartPosition = "CenterScreen"; $form.BackColor = $bg; $form.ForeColor = $fg
  $form.Font = New-Object System.Drawing.Font("Segoe UI", 10); $form.FormBorderStyle = "FixedDialog"; $form.MaximizeBox = $false
  $title = & $mk Label 20 16; $title.Text = "KwickBoot USB Creator"; $title.AutoSize = $true; $title.ForeColor = [System.Drawing.Color]::White
  $title.Font = New-Object System.Drawing.Font("Segoe UI", 16, [System.Drawing.FontStyle]::Bold); $form.Controls.Add($title)
  $sub = & $mk Label 22 52; $sub.Text = "Choose a USB stick - it is erased and turned into a KwickBoot stick."; $sub.AutoSize = $true; $form.Controls.Add($sub)

  $grid = & $mk DataGridView 20 82 624 210; $grid.ReadOnly = $true; $grid.AllowUserToAddRows = $false; $grid.MultiSelect = $false
  $grid.SelectionMode = "FullRowSelect"; $grid.RowHeadersVisible = $false; $grid.AllowUserToResizeRows = $false
  $grid.AutoSizeColumnsMode = "Fill"; $grid.BackgroundColor = $panel; $grid.BorderStyle = "None"; $grid.EnableHeadersVisualStyles = $false
  $grid.ColumnHeadersDefaultCellStyle.BackColor = $panel; $grid.ColumnHeadersDefaultCellStyle.ForeColor = $accent
  $grid.DefaultCellStyle.BackColor = $panel; $grid.DefaultCellStyle.ForeColor = $fg
  $grid.DefaultCellStyle.SelectionBackColor = $accent; $grid.DefaultCellStyle.SelectionForeColor = [System.Drawing.Color]::White
  foreach ($col in @(@("num", "Disk", 10), @("model", "Model", 40), @("size", "Size", 16), @("serial", "Serial", 24), @("contents", "Contents", 28))) {
    $i = $grid.Columns.Add($col[0], $col[1]); $grid.Columns[$i].FillWeight = $col[2] }
  $form.Controls.Add($grid)

  $virt = & $mk CheckBox 20 300; $virt.Text = "Show virtual disks (testing)"; $virt.AutoSize = $true; $virt.Checked = [bool]$AllowVirtual; $form.Controls.Add($virt)
  $refresh = & $mk Button 544 298 100 28; $refresh.Text = "Refresh"; $form.Controls.Add($refresh)
  $isoLbl = & $mk Label 20 340; $isoLbl.Text = "ISO:"; $isoLbl.AutoSize = $true; $form.Controls.Add($isoLbl)
  $isoBox = & $mk TextBox 58 337 470 24; $isoBox.BackColor = $panel; $isoBox.ForeColor = $fg; $form.Controls.Add($isoBox)
  if ($Iso) { $isoBox.Text = $Iso } else { $sib = Get-ChildItem (Split-Path $self) -Filter "kwickboot-*-amd64.iso" -ErrorAction SilentlyContinue | Select-Object -First 1; if ($sib) { $isoBox.Text = $sib.FullName } }
  $browse = & $mk Button 538 336 106 26; $browse.Text = "Browse..."; $form.Controls.Add($browse)
  $rbLive = & $mk RadioButton 58 370; $rbLive.Text = "Run live (default)"; $rbLive.AutoSize = $true; $rbLive.Checked = $true; $form.Controls.Add($rbLive)
  $rbInst = & $mk RadioButton 230 370; $rbInst.Text = "Installer"; $rbInst.AutoSize = $true; $form.Controls.Add($rbInst)

  $bar = & $mk ProgressBar 20 402 624 14; $bar.Style = "Marquee"; $bar.Visible = $false; $form.Controls.Add($bar)
  $log = & $mk TextBox 20 422 624 58; $log.Multiline = $true; $log.ReadOnly = $true; $log.ScrollBars = "Vertical"
  $log.BackColor = [System.Drawing.Color]::Black; $log.ForeColor = [System.Drawing.Color]::FromArgb(170, 205, 170)
  $log.Font = New-Object System.Drawing.Font("Consolas", 9); $log.Visible = $false; $form.Controls.Add($log)
  $create = & $mk Button 432 492 130 32; $create.Text = "Create USB"; $create.BackColor = $accent; $create.ForeColor = [System.Drawing.Color]::White; $create.FlatStyle = "Flat"; $form.Controls.Add($create)
  $close = & $mk Button 568 492 76 32; $close.Text = "Close"; $form.Controls.Add($close)
  $form.AcceptButton = $create
  $form.CancelButton = $close

  $populate = {
    $grid.Rows.Clear()
    $tp = @("USB"); if ($virt.Checked) { $tp += "File Backed Virtual" }
    foreach ($d in @(Get-Disk | Where-Object { $tp -contains [string]$_.BusType -and -not $_.IsBoot -and -not $_.IsSystem } | Sort-Object Number)) {
      $vols = (Get-Partition -DiskNumber $d.Number -ErrorAction SilentlyContinue | Get-Volume -ErrorAction SilentlyContinue | ForEach-Object { "$($_.FileSystemLabel)($($_.FileSystem))" }) -join " "
      $ser = ($d.SerialNumber -replace '\s', ''); if (-not $ser) { $ser = $d.FriendlyName }
      $null = $grid.Rows.Add($d.Number, $d.FriendlyName, ("{0:N1} GB" -f ($d.Size / 1GB)), $ser, $vols) }
    if ($grid.Rows.Count -gt 0) { $grid.Rows[0].Selected = $true } }
  if ($mode -eq 'nopxe') {   # no-PXE mode: the ISO + live/installer options don't apply - hide them so the choice is unambiguous
    $title.Text = "KwickBoot - No-PXE Stick"
    $sub.Text = "No-PXE boot stick (tiny) - the chosen USB is ERASED. It finds the LAN's KwickBoot server by broadcast (like PXE)."
    foreach ($c in @($isoLbl, $isoBox, $browse, $rbLive, $rbInst)) { $c.Visible = $false }
  }
  & $populate
  $refresh.Add_Click($populate); $virt.Add_CheckedChanged($populate)
  $browse.Add_Click({ $o = New-Object System.Windows.Forms.OpenFileDialog; $o.Filter = "KwickBoot ISO|kwickboot-*.iso|ISO files|*.iso"; if ($o.ShowDialog() -eq "OK") { $isoBox.Text = $o.FileName } })
  $close.Add_Click({ $form.Close() })

  $create.Add_Click({
    if ($grid.SelectedRows.Count -lt 1) { [void][System.Windows.Forms.MessageBox]::Show("Pick a USB stick first.", "KwickBoot"); return }
    $r = $grid.SelectedRows[0]; $num = [int]$r.Cells["num"].Value; $id = [string]$r.Cells["serial"].Value
    $esc = { param($s) "'" + ([string]$s -replace "'", "''") + "'" }
    if ($mode -eq 'nopxe') {
      $w = "This ERASES disk ${num} and writes the tiny NO-PXE boot stick:`n`n    $($r.Cells['model'].Value)  ($($r.Cells['size'].Value), serial $id)`n`nContinue?"
      if ([System.Windows.Forms.MessageBox]::Show($w, "Confirm - No-PXE boot stick", "OKCancel", "Warning") -ne "OK") { return }
      $line = "& " + (& $esc $self) + " -Console -Yes -NoPxe -Disk $num -Confirm " + (& $esc $id)
      if ($env:KBUSB_NOPXE_IMG) { $line += " -NoPxeImg " + (& $esc $env:KBUSB_NOPXE_IMG) }
      if ($virt.Checked) { $line += " -AllowVirtual" }
      $line += " *>&1"
      $okMsg = "Done - the no-PXE boot stick is ready.`nStart a PC from it; it finds this LAN's KwickBoot server by broadcast."
    } else {
      $iso = $isoBox.Text.Trim(); if (-not (Test-Path $iso)) { [void][System.Windows.Forms.MessageBox]::Show("Choose a valid ISO file.", "KwickBoot"); return }
      $signed = (Test-Path (Join-Path (Split-Path $iso) "SHA256SUMS")) -and (Test-Path (Join-Path (Split-Path $iso) "SHA256SUMS.asc"))
      $w = "This ERASES disk $num and ALL data on it:`n`n    $($r.Cells['model'].Value)  ($($r.Cells['size'].Value), serial $id)`n`nContinue?"
      if (-not $signed) { $w = "The ISO is not signed - it cannot be verified.`n`n" + $w }
      if ([System.Windows.Forms.MessageBox]::Show($w, "Confirm write", "OKCancel", "Warning") -ne "OK") { return }
      $start = if ($rbInst.Checked) { "install" } else { "live" }
      $line = "& " + (& $esc $self) + " -Console -Yes -Disk $num -Confirm " + (& $esc $id) + " -Iso " + (& $esc $iso) + " -Start $start -BootMB $BootMB"
      if ($virt.Checked) { $line += " -AllowVirtual" }; if (-not $signed) { $line += " -Unverified" }; $line += " *>&1"
      $okMsg = "Done - the KwickBoot stick is ready.`nRemove it and start a PC from it (UEFI boot menu: often F12)."
    }
    foreach ($c in @($create, $browse, $refresh, $grid, $close, $virt, $isoBox)) { $c.Enabled = $false }
    $bar.Visible = $true; $log.Visible = $true; $log.Clear()
    $tmp = [IO.Path]::GetTempFileName(); $cmdf = [IO.Path]::ChangeExtension([IO.Path]::GetTempFileName(), ".ps1")
    Set-Content -Path $cmdf -Value $line -Encoding UTF8
    $proc = Start-Process powershell.exe -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$cmdf`"") -WindowStyle Hidden -RedirectStandardOutput $tmp -PassThru
    $pos = 0
    while (-not $proc.HasExited) {
      try { $t = [IO.File]::ReadAllText($tmp); if ($t.Length -gt $pos) { $log.AppendText($t.Substring($pos)); $pos = $t.Length; $log.SelectionStart = $log.TextLength; $log.ScrollToCaret() } } catch {}
      [System.Windows.Forms.Application]::DoEvents(); Start-Sleep -Milliseconds 150 }
    Start-Sleep -Milliseconds 250
    try { $t = [IO.File]::ReadAllText($tmp); if ($t.Length -gt $pos) { $log.AppendText($t.Substring($pos)) } } catch {}
    Remove-Item $tmp, $cmdf -ErrorAction SilentlyContinue
    $bar.Style = "Continuous"; $bar.Value = 100
    if ($proc.ExitCode -eq 0) { [void][System.Windows.Forms.MessageBox]::Show($okMsg, "KwickBoot USB Creator") }
    else { $bar.ForeColor = [System.Drawing.Color]::Firebrick; [void][System.Windows.Forms.MessageBox]::Show("The build did not finish - see the log below.", "KwickBoot USB Creator", "OK", "Error") }
    foreach ($c in @($create, $browse, $refresh, $grid, $close, $virt, $isoBox)) { $c.Enabled = $true }
    & $populate })

  [void]$form.ShowDialog() }

if (-not $Iso -and $env:KBUSB_EXE -and (Test-Path $env:KBUSB_EXE)) {   # single-file exe: pull the bundled ISO out
  try { $x = Get-EmbeddedIso $env:KBUSB_EXE; if ($x) { $Iso = $x; $script:KbTmpIso = Split-Path $x } } catch {}
}
function Write-NoPxeStick($disk, $img) {   # raw-write the tiny hybrid no-PXE image onto a USB disk (no partitioning needed - it IS a disk image)
  $bytes = [IO.File]::ReadAllBytes($img)
  if ($bytes.Length % 512 -ne 0) { Die "no-PXE image is not sector-aligned ($($bytes.Length) bytes)" }
  Say "Clearing disk $disk"
  "select disk $disk`r`nclean`r`n" | diskpart | Out-Null
  Start-Sleep -Milliseconds 800
  Say "Writing the no-PXE boot image ($([math]::Round($bytes.Length/1MB,1)) MB)"
  $fs = $null
  try {
    $fs = New-Object IO.FileStream("\\.\PhysicalDrive$disk", [IO.FileMode]::Open, [IO.FileAccess]::Write, [IO.FileShare]::ReadWrite)
    $fs.Write($bytes, 0, $bytes.Length); $fs.Flush()
  } catch { Die "raw write to disk ${disk} failed: $($_.Exception.Message)" } finally { if ($fs) { $fs.Close() } }
  "select disk $disk`r`nrescan`r`n" | diskpart | Out-Null
  Say "Done - the no-PXE boot stick is ready on disk $disk."
  Write-Host "    Start a PC from it; it finds this LAN's KwickBoot server by broadcast (like PXE), no fixed IP."
}

if ($Gui -or (-not $PSBoundParameters.ContainsKey('Disk') -and -not $Yes -and -not $Console -and [Environment]::UserInteractive)) {
  Show-CreatorGui
  if ($script:KbTmpIso) { Remove-Item -Recurse -Force $script:KbTmpIso -ErrorAction SilentlyContinue }
  exit
}

# ---- No-PXE boot stick: a separate, simple path (raw image write, no ISO / no partitioning)
if ($NoPxe) {
  if (-not $NoPxeImg) { $NoPxeImg = $env:KBUSB_NOPXE_IMG }
  if (-not $NoPxeImg -or -not (Test-Path $NoPxeImg)) {
    $sib = Join-Path (Split-Path $PSCommandPath) "kwickboot-boot.img"
    if (Test-Path $sib) { $NoPxeImg = $sib } else { Die "no-PXE image not found (pass -NoPxeImg <kwickboot-boot.img>)" }
  }
  $NoPxeImg = (Resolve-Path $NoPxeImg).Path
  $types = @("USB"); if ($AllowVirtual) { $types += "File Backed Virtual" }
  $disks = @(Get-Disk | Where-Object { $types -contains [string]$_.BusType -and -not $_.IsBoot -and -not $_.IsSystem } | Sort-Object Number)
  if (-not $disks) { Die "no USB disk found - plug in a stick and start again" }
  if ($Disk -lt 0) {
    Write-Host ""; Write-Host " USB disks:"
    foreach ($d in $disks) { "  {0,3})  {1,-34} {2,7:N1} GB  serial {3}" -f $d.Number, $d.FriendlyName, ($d.Size / 1GB), ($d.SerialNumber -replace '\s', '') | Write-Host }
    $Disk = [int](Ask "Which disk for the no-PXE stick? (number)" "")
  }
  $d = $disks | Where-Object Number -eq $Disk
  if (-not $d) { Die "disk $Disk is not one of the USB disks above" }
  $ser = ($d.SerialNumber -replace '\s', ''); $id = if ($ser) { $ser } else { $d.FriendlyName }
  Write-Host ""; Warn "EVERYTHING on disk $Disk ($($d.FriendlyName), $([math]::Round($d.Size/1GB,1)) GB) will be ERASED and replaced by the tiny no-PXE boot image."
  if (-not $Confirm) { $Confirm = Read-Host "Type the disk's serial ($id) to confirm" }
  if ($Confirm.Trim() -ne $id) { Die "not confirmed - nothing was changed" }
  Write-NoPxeStick $Disk $NoPxeImg
  if ($script:KbTmpIso) { Remove-Item -Recurse -Force $script:KbTmpIso -ErrorAction SilentlyContinue }
  exit 0
}

# ---- 1. the release: download (or take the given ISO), check signature + SHA-256
$work = Join-Path $env:TEMP "kwickboot-usb"; New-Item -ItemType Directory -Force $work | Out-Null
if (-not $Iso) {
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  $wc = New-Object Net.WebClient; $wc.Headers.Add("User-Agent", "KwickBoot-USB-Creator/$VERSION")
  Say "Downloading the release list from $Url"
  foreach ($f in "SHA256SUMS", "SHA256SUMS.asc") { $wc.DownloadFile($Url + $f, (Join-Path $work $f)) }
  $line = Get-Content (Join-Path $work "SHA256SUMS") | Where-Object { $_ -match '\s\*?(kwickboot-[0-9A-Za-z.-]+-amd64\.iso)$' } | Select-Object -First 1
  if (-not $line) { Die "no ISO in the release list" }
  $name = ($line -split '\s+\*?')[1]; $Iso = Join-Path $work $name
  if (-not (Test-Path $Iso)) {
    Say "Downloading $name (about 800 MB)"
    $wc.DownloadFile($Url + $name, "$Iso.part"); Move-Item -Force "$Iso.part" $Iso
  }
}
if (-not (Test-Path $Iso)) { Die "ISO not found: $Iso" }
$Iso = (Resolve-Path $Iso).Path; $dir = Split-Path $Iso; $name = Split-Path $Iso -Leaf
$sums = Join-Path $dir "SHA256SUMS"; $asc = Join-Path $dir "SHA256SUMS.asc"
$verified = $false
if ((Test-Path $sums) -and (Test-Path $asc)) {
  Say "Checking the release signature"
  $fp = ""; $pub = [KbPgp]::PubKey($KEY, [ref]$fp)
  if ($fp -ne $FPR) { Die "the built-in release key is damaged (fingerprint $fp)" }
  $v = [KbPgp]::Verify([IO.File]::ReadAllBytes($sums), (Get-Content -Raw $asc), $pub)
  if ($v -ne "OK") { Die "SHA256SUMS signature: $v - do not use this download" }
  Write-Host "    Good signature: Jins Tech Corp KwickBoot Release ($($FPR.Substring(0,4)) ... $($FPR.Substring(36)))"
  $want = (Get-Content $sums | Where-Object { $_ -match [regex]::Escape($name) + '$' } | Select-Object -First 1)
  if (-not $want) { Die "$name is not in the signed SHA256SUMS" }
  Say "Checking the ISO's SHA-256 (about half a minute)"
  $have = (Get-FileHash -Algorithm SHA256 $Iso).Hash.ToLower()
  if ($have -ne ($want -split '\s+')[0].ToLower()) { Die "$name is damaged or changed (SHA-256 differs) - download it again" }
  Write-Host "    SHA-256 OK"; $verified = $true
} else {
  Warn "no SHA256SUMS + SHA256SUMS.asc next to $name - the ISO cannot be checked."
  if ($Unverified) { Write-Host "    continuing unverified (as requested)" }
  elseif ((Ask "Type UNVERIFIED to use it anyway" "") -ne "UNVERIFIED") { Die "stopped - put SHA256SUMS and SHA256SUMS.asc next to the ISO" }
}

# ---- 2. the stick: USB disks only, never the system disk
$types = @("USB"); if ($AllowVirtual) { $types += "File Backed Virtual" }       # -AllowVirtual: VHDX test sticks
$disks = @(Get-Disk | Where-Object { $types -contains [string]$_.BusType -and -not $_.IsBoot -and -not $_.IsSystem } | Sort-Object Number)
if (-not $disks) { Die "no USB disk found - plug in the stick (8 GB or more) and start again" }
Write-Host ""; Write-Host " USB disks:"
foreach ($d in $disks) {
  $vols = (Get-Partition -DiskNumber $d.Number -ErrorAction SilentlyContinue | Get-Volume -ErrorAction SilentlyContinue |
           ForEach-Object { "$($_.FileSystemLabel)($($_.FileSystem))" }) -join " "
  "  {0,3})  {1,-34} {2,7:N1} GB  serial {3}  {4}" -f $d.Number, $d.FriendlyName, ($d.Size / 1GB), ($d.SerialNumber -replace '\s',''), $vols | Write-Host
}
if ($Disk -lt 0) { $Disk = [int](Ask "Which disk? (number)" "") }
$d = $disks | Where-Object Number -eq $Disk
if (-not $d) { Die "disk $Disk is not one of the USB disks above" }
$ser = ($d.SerialNumber -replace '\s', ''); $id = if ($ser) { $ser } else { $d.FriendlyName }

$parts = @(Get-Partition -DiskNumber $Disk -ErrorAction SilentlyContinue)
$volOf = { param($lbl) $parts | Where-Object { ($_ | Get-Volume -ErrorAction SilentlyContinue).FileSystemLabel -eq $lbl } | Select-Object -First 1 }
$pData = & $volOf "KWICKDATA"
$pBoot = $parts | Where-Object { $_.MbrType -eq 239 -or $_.GptType -eq "{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}" -or ($_ | Get-Volume -ErrorAction SilentlyContinue).FileSystemLabel -eq "KWICKBOOT" } | Select-Object -First 1
if ($Update) {
  if (-not $pData -or -not $pBoot) { Die "disk $Disk is not a KwickBoot stick (no KWICKDATA + boot partition) - run without -Update" }
  if ($pBoot.Size -lt (Get-Item $Iso).Length * 1.05) { Die "the stick's boot partition is too small for this version - make the stick again (without -Update)" }
  Say "Update: the boot partition is replaced, KWICKDATA (kwickboot.cfg, iso\) is kept"
  if ((Ask "Update disk $Disk ($($d.FriendlyName))? (y/n)" "y") -ne "y") { Die "stopped - nothing was changed" }
} else {
  $min = [math]::Max(4GB, $BootMB * 1MB + 1GB)
  if ($d.Size -lt $min) { Die "the stick is too small ($([math]::Round($d.Size/1GB,1)) GB, needs $([math]::Round($min/1GB)) GB or more)" }
  if ((Get-Item $Iso).Length -gt ($BootMB * 1MB * 0.9)) { Die "boot partition ($BootMB MB) too small for this ISO - use -BootMB" }
  Write-Host ""; Warn "EVERYTHING on disk $Disk ($($d.FriendlyName), $([math]::Round($d.Size/1GB,1)) GB) will be ERASED."
  if (-not $Confirm) { $Confirm = Read-Host "Type the disk's serial ($id) to confirm" }
  if ($Confirm.Trim() -ne $id) { Die "not confirmed - nothing was changed" }
}

# ---- 3. pre-configuration (kwickboot.cfg on KWICKDATA): answers for the installer, no passwords on the stick
$cfg = $null
if (-not $Update) {
  $pre = if ($ServerName -or $Static -or $TimeZone -or $InstallDisk) { "y" } else { Ask "Pre-configure the server (name, network, time zone$(if ($Start -eq 'install') { ', install disk' }))? (y/n)" "n" }
  if (-not $Start) {
    Write-Host "When a PC starts from this stick:"
    Write-Host "    live     - run KwickBoot from the stick (default: nothing is installed; settings and ISOs stay on the stick)"
    Write-Host "    install  - install the KwickBoot server onto the PC's disk"
    $Start = Ask "  Start" "live"; if ($Start -notin "live", "install") { Die "start: live or install" } }
  $L = @("# KwickBoot stick settings (KwickBoot USB Creator $VERSION, $(Get-Date -Format yyyy-MM-dd)). Delete a line to be asked on screen.",
         "# start=live: run KwickBoot from the stick (nothing installed). start=install: install it onto the PC's disk. The menu offers both.",
         "start=$Start",
         "# No passwords here: the first admin is created in the web console with the setup code shown on the server's screen.")
  if ($pre -eq "y") {
    $hn = if ($ServerName) { $ServerName } else { Ask "  Server name" "kwickboot" }
    if ($hn -notmatch '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$') { Die "server name: lowercase letters, digits, '-'" }
    $map = @{ "Central Standard Time" = "America/Chicago"; "Eastern Standard Time" = "America/New_York"; "Pacific Standard Time" = "America/Los_Angeles";
              "Mountain Standard Time" = "America/Denver"; "GMT Standard Time" = "Europe/London"; "W. Europe Standard Time" = "Europe/Berlin";
              "China Standard Time" = "Asia/Shanghai"; "Tokyo Standard Time" = "Asia/Tokyo"; "Taipei Standard Time" = "Asia/Taipei"; "UTC" = "UTC" }
    $tzd = $map[[TimeZoneInfo]::Local.Id]; if (-not $tzd) { $tzd = "UTC" }
    $tz = if ($TimeZone) { $TimeZone } else { Ask "  Time zone (Area/City)" $tzd }
    $L += "host=$hn", "timezone=$tz"
    $st = if ($Static) { $Static } elseif ($Yes) { "" } else { Ask "  Static address with prefix (e.g. 192.168.1.10/24), empty = DHCP" "" }
    if ($st) {
      if ($st -notmatch '^(\d{1,3}\.){3}\d{1,3}/\d{1,2}$') { Die "address: e.g. 192.168.1.10/24" }
      $gw = if ($Gateway) { $Gateway } else { Ask "  Gateway" "" }; $dn = if ($Dns) { $Dns } else { Ask "  DNS servers (comma-separated)" $gw }
      $L += "network=static", "address=$st", "gateway=$gw", "dns=$($dn -replace '\s+', ',')"
    } else { $L += "network=dhcp" }
    $L += "test_lab=no"
    $idk = if ($InstallDisk) { $InstallDisk } elseif ($Start -ne "install") { "ask" } else {
      Write-Host "  Which disk of the server PC to install on:"
      Write-Host "    ask            - choose on the screen (default, safest)"
      Write-Host "    only-internal  - the PC's only internal disk, after a 30-second countdown on screen (no keyboard needed)"
      Write-Host "    <serial>       - exactly that disk"
      Ask "  Install disk" "ask" }
    if ($idk -eq "only-internal") { Warn "this stick ERASES the only internal disk of any PC it starts (30 s countdown, never a disk that already holds a KwickBoot server)." }
    $L += "disk=$idk"
  } else { $L += "# host=kwickboot", "# timezone=America/Chicago", "# network=dhcp", "# disk=ask" }
  $L += "import_isos=yes"
  $cfg = ($L -join "`r`n") + "`r`n"
}

# ---- 4. partition + format (new stick) - KWICKDATA first (what Windows shows), KWICKBOOT at the end
function Letter($p) { $p = Get-Partition -DiskNumber $p.DiskNumber -PartitionNumber $p.PartitionNumber
  if (-not $p.DriveLetter -or $p.DriveLetter -eq [char]0) { $p | Add-PartitionAccessPath -AssignDriveLetter; $p = Get-Partition -DiskNumber $p.DiskNumber -PartitionNumber $p.PartitionNumber }
  return "$($p.DriveLetter):" }
if (-not $Update) {
  Say "Erasing disk $Disk and creating KWICKDATA (exFAT) + KWICKBOOT (FAT32, $BootMB MB)"
  if ($d.PartitionStyle -ne "RAW") { Clear-Disk -Number $Disk -RemoveData -RemoveOEM -Confirm:$false }
  # MBR (not GPT) so the same stick boots in legacy BIOS mode too; KWICKBOOT becomes type EF (EFI system partition) at the end
  Initialize-Disk -Number $Disk -PartitionStyle MBR -ErrorAction SilentlyContinue
  if ((Get-Disk -Number $Disk).PartitionStyle -ne "MBR") { Set-Disk -Number $Disk -PartitionStyle MBR }
  $free = (Get-Disk -Number $Disk).LargestFreeExtent
  $pData = New-Partition -DiskNumber $Disk -Size ($free - $BootMB * 1MB - 2MB) -MbrType IFS
  $pBoot = New-Partition -DiskNumber $Disk -UseMaximumSize -MbrType FAT32 -IsActive
  $null = Format-Volume -Partition $pData -FileSystem exFAT -NewFileSystemLabel KWICKDATA -Confirm:$false
  $null = Format-Volume -Partition $pBoot -FileSystem FAT32 -NewFileSystemLabel KWICKBOOT -Confirm:$false
} else {
  if ($pBoot.MbrType -eq 239) {                                              # EF (EFI) -> FAT32 while it is rewritten
    "select disk $Disk`r`nselect partition $($pBoot.PartitionNumber)`r`nset id=0C override`r`n" | diskpart | Out-Null }
  $null = Format-Volume -Partition (Get-Partition -DiskNumber $Disk -PartitionNumber $pBoot.PartitionNumber) -FileSystem FAT32 -NewFileSystemLabel KWICKBOOT -Confirm:$false
}
$NB = $pBoot.PartitionNumber; $ND = $pData.PartitionNumber
$B = Letter (Get-Partition -DiskNumber $Disk -PartitionNumber $NB)
$Dl = Letter (Get-Partition -DiskNumber $Disk -PartitionNumber $ND)

# ---- 5. the ISO's files onto KWICKBOOT, then read everything back and compare
Say "Copying the KwickBoot files to $B"
$img = Mount-DiskImage -ImagePath $Iso -PassThru; Start-Sleep -Milliseconds 500
$src = "$((Get-Volume -DiskImage $img).DriveLetter):"
try {
  & robocopy "$src\" "$B\" /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
  if ($LASTEXITCODE -ge 8) { Die "copying failed (robocopy $LASTEXITCODE)" }
  $files = Get-ChildItem -Recurse -File "$src\"
  # drop the stick's file cache: dismount + mount the volume, so the comparison reads the stick, not RAM
  $p2 = Get-Partition -DiskNumber $Disk -PartitionNumber $NB; $p2 | Remove-PartitionAccessPath -AccessPath "$B\"
  $B = Letter $p2
  Say "Verifying: reading back $($files.Count) files from the stick"
  $bad = 0
  foreach ($f in $files) {
    $rel = $f.FullName.Substring($src.Length); $t = "$B$rel"
    if (-not (Test-Path $t) -or (Get-FileHash -Algorithm SHA256 $f.FullName).Hash -ne (Get-FileHash -Algorithm SHA256 $t).Hash) { $bad++; Write-Host "    differs: $rel" }
  }
  if ($bad) { Die "$bad file(s) differ on the stick - the stick may be faulty; try another one" }
  Write-Host "    all $($files.Count) files identical"
  # legacy BIOS boot: GRUB in the MBR + the gap before partition 1 (from the ISO: kb-usb-mbr.bin, kb-usb-core.img)
  $mb = "$src\boot\grub\kb-usb-mbr.bin"; $cr = "$src\boot\grub\kb-usb-core.img"
  $first = (Get-Partition -DiskNumber $Disk | Measure-Object -Property Offset -Minimum).Minimum
  if ((Test-Path $mb) -and (Test-Path $cr) -and (Get-Disk -Number $Disk).PartitionStyle -eq "MBR" -and $first -gt ((Get-Item $cr).Length + 1024)) {
    [KbDisk]::WriteBios($Disk, [IO.File]::ReadAllBytes($mb), [IO.File]::ReadAllBytes($cr))
    Write-Host "    legacy BIOS boot written and verified"
  } else { Warn "this stick boots UEFI only (older ISO, or a GPT stick from an older creator - remake it without -Update)" }
} finally { Dismount-DiskImage -ImagePath $Iso | Out-Null }

# ---- 6. KWICKDATA: kwickboot.cfg, iso\, README
New-Item -ItemType Directory -Force "$Dl\iso" | Out-Null
if ($cfg) { [IO.File]::WriteAllText("$Dl\kwickboot.cfg", $cfg) }
@"
KwickBoot USB stick
===================
Start a PC from this stick (UEFI; Secure Boot may stay ON) to install the KwickBoot server.

kwickboot.cfg  answers for the installer (server name, network, time zone, which disk). Delete a line to be asked on
               screen. No passwords: the first admin is created in the web console with the setup code on the screen.
iso\           drop ISO files here: the installer copies them into the new server's library (when the disk has room).

Verify, update or remake the stick with the KwickBoot USB Creator. https://kwickboot.com/
"@ -replace "`n", "`r`n" | Set-Content -Encoding ASCII "$Dl\README.txt"

# ---- 7. boot partition becomes the EFI system partition (firmware boots it; Windows stops showing it)
"select disk $Disk`r`nselect partition $NB`r`nremove letter=$($B.TrimEnd(':')) noerr`r`nset id=EF override`r`n" | diskpart | Out-Null
Write-Host ""
Say "Done: $($d.FriendlyName) is a KwickBoot stick ($name$(if (-not $verified) { ', UNVERIFIED' }))."
Write-Host "    $Dl  KWICKDATA - kwickboot.cfg and iso\ (drop ISOs here)"
Write-Host "    Start the server PC from the stick (UEFI boot menu: often F12, F11 or Esc)."
