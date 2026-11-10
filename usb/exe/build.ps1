# build.ps1 [-Iso <file>] - build the single-file KwickBoot USB Creator exe (GUI, no console; uses the .NET csc that
# ships with Windows, no downloads). With -Iso, the ISO (and SHA256SUMS + SHA256SUMS.asc if next to it) are appended so
# the customer downloads ONE file. Output: usb\exe\KwickBootUSB.exe  (unsigned - sign it with signtool, see README-exe.md)
param([string]$Iso, [string]$NoPxeImg)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$ps1  = Join-Path (Split-Path $here -Parent) "kwickboot-usb.ps1"
if (-not (Test-Path $ps1)) { throw "kwickboot-usb.ps1 not found next to the exe folder" }
if (-not $NoPxeImg) { $NoPxeImg = Join-Path (Split-Path $here -Parent) "no-pxe\kwickboot-boot.img" }   # embed the tiny no-PXE image for the "No-PXE boot stick" mode
$csc = Get-ChildItem "$env:WINDIR\Microsoft.NET\Framework64\v4.0.*\csc.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $csc) { $csc = Get-ChildItem "$env:WINDIR\Microsoft.NET\Framework\v4.0.*\csc.exe" | Select-Object -First 1 }
if (-not $csc) { throw "csc.exe (.NET Framework 4) not found" }
$out = Join-Path $here "KwickBootUSB.exe"
$cscArgs = @("/nologo", "/target:winexe", "/platform:anycpu", "/optimize+",
  "/reference:System.Windows.Forms.dll", "/reference:System.Drawing.dll",
  "/win32manifest:$(Join-Path $here 'app.manifest')", "/resource:$ps1,kwickboot-usb.ps1")
if (Test-Path $NoPxeImg) { $cscArgs += "/resource:$NoPxeImg,kwickboot-boot.img"; Write-Host "  embedding no-PXE image: $NoPxeImg ($([math]::Round((Get-Item $NoPxeImg).Length/1KB)) KB)" }
else { Write-Host "  NOTE: no-PXE image not found ($NoPxeImg) - the exe's No-PXE mode will need -NoPxeImg at runtime" }
$cscArgs += @("/out:$out", (Join-Path $here "Launcher.cs"))
& $csc.FullName @cscArgs
if ($LASTEXITCODE -ne 0) { throw "csc failed ($LASTEXITCODE)" }
$kb = [math]::Round((Get-Item $out).Length / 1KB, 1)

if ($Iso) {
  if (-not (Test-Path $Iso)) { throw "ISO not found: $Iso" }
  $iso = (Resolve-Path $Iso).Path; $name = Split-Path $iso -Leaf
  $nameBytes = [Text.Encoding]::UTF8.GetBytes($name)
  $extra = New-Object System.IO.MemoryStream
  foreach ($f in "SHA256SUMS", "SHA256SUMS.asc") {
    $fp = Join-Path (Split-Path $iso) $f
    if (Test-Path $fp) {
      $fn = [Text.Encoding]::UTF8.GetBytes($f); $data = [IO.File]::ReadAllBytes($fp)
      $extra.Write([BitConverter]::GetBytes([int]$fn.Length), 0, 4); $extra.Write($fn, 0, $fn.Length)
      $extra.Write([BitConverter]::GetBytes([int]$data.Length), 0, 4); $extra.Write($data, 0, $data.Length)
    }
  }
  $extraBytes = $extra.ToArray(); $isolen = (Get-Item $iso).Length
  $fsOut = [IO.File]::Open($out, "Append")
  try {
    $fsIn = [IO.File]::OpenRead($iso)
    try { $buf = New-Object byte[] 1048576; while (($n = $fsIn.Read($buf, 0, $buf.Length)) -gt 0) { $fsOut.Write($buf, 0, $n) } } finally { $fsIn.Close() }
    $fsOut.Write($nameBytes, 0, $nameBytes.Length)
    $fsOut.Write($extraBytes, 0, $extraBytes.Length)
    $fsOut.Write([BitConverter]::GetBytes([int64]$isolen), 0, 8)
    $fsOut.Write([BitConverter]::GetBytes([int]$nameBytes.Length), 0, 4)
    $fsOut.Write([BitConverter]::GetBytes([int]$extraBytes.Length), 0, 4)
    $fsOut.Write([Text.Encoding]::ASCII.GetBytes("KBUSBIS1"), 0, 8)
  } finally { $fsOut.Close() }
  $mb = [math]::Round((Get-Item $out).Length / 1MB, 0)
  "Built: $out  ($mb MB with $name embedded, UNSIGNED)"
} else {
  "Built: $out  ($kb KB launcher only - run with -Iso <file> to embed the ISO, UNSIGNED)"
}
