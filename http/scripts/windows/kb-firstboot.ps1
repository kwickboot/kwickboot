# kb-firstboot.ps1 - KwickBoot, end of Windows setup (SetupComplete, runs once as SYSTEM before the first sign-in).
# SPDX-License-Identifier: AGPL-3.0-or-later
#  1. debloat at the level the install chose (off | standard | full) - every removal is logged in kb-firstboot.log
#  2. the local admin account must choose its password at the first sign-in (it was created without one)
#  3. check in with KwickBoot (one-time secret) -> "First boot" verified in the console; the secret is deleted
#  4. the open-source app bundle installs AFTER setup, in the background (one-time scheduled task "KwickBoot apps"),
#     so setup, the sign-in screen and the check-in do not wait for the installers
#  5. Entra ID join (department provisioning package) AFTER the check-in, in a one-time startup task "KwickBoot join":
#     the package restarts the PC (it renames it), so it must not run inside setup; the task survives the restarts,
#     puts KwickBoot's hostname back and reports the join to the console
param([switch]$AppsOnly, [switch]$JoinOnly)
$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"     # PowerShell 5.1 draws a progress bar per network chunk - many times slower
$dir ="$env:WINDIR\Setup\Scripts"
$cfg = @{}
$envFile = if ($AppsOnly) { "$dir\kb-apps.env" } elseif ($JoinOnly) { "$dir\kb-join.env" } else { "$dir\kb.env" }
Get-Content $envFile -ErrorAction SilentlyContinue | ForEach-Object { if ($_ -match '^([A-Z]+)=(.*)$') { $cfg[$Matches[1]] = $Matches[2].Trim() } }
if ($JoinOnly) {
  $script:joinLock = New-Object System.Threading.Mutex($false, "Global\KwickBootJoin")
  if (-not $script:joinLock.WaitOne(0)) { exit 0 }
  "== KwickBoot Entra join $(Get-Date -Format s) (name now $env:COMPUTERNAME, wanted $($cfg.NAME))"
  $pk = "$dir\kb-join.ppkg"; $mark = "$dir\kb-join.applied"
  for ($i = 0; $i -lt 30; $i++) {      # up to 5 min for the network / internet to Microsoft
    try { $t = New-Object Net.Sockets.TcpClient; $t.Connect("login.microsoftonline.com", 443); $t.Close(); break } catch { Start-Sleep 10 }
  }
  if ((Test-Path $pk) -and -not (Test-Path $mark)) {
    # the marker first: the package restarts the PC itself (it sets a device name first, then joins after the restart),
    # and the next start must not apply it again
    Set-Content -Path $mark -Value (Get-Date -Format s)
    try { Install-ProvisioningPackage -PackagePath $pk -ForceInstall -QuietInstall -ErrorAction Stop | Out-Null; "  provisioning package applied - Windows restarts and joins" }
    catch { "  provisioning package FAILED: $($_.Exception.Message)" }
    # if the package restarts the PC now, this task simply runs again after the restart (the marker skips this step)
  }
  Remove-Item $pk -Force -ErrorAction SilentlyContinue
  # Windows joins in the background after the package's restart: wait for it BEFORE anything restarts the PC again
  $st = "notjoined"; $dev = ""
  for ($i = 0; $i -lt 60; $i++) {      # up to 10 min
    $ds = & dsregcmd.exe /status
    if ($ds -match 'AzureAdJoined\s*:\s*YES') { $st = "joined"; $dev = (($ds | Select-String 'DeviceId\s*:\s*(\S+)').Matches | Select-Object -First 1).Groups[1].Value; break }
    Start-Sleep 10
  }
  "  Entra ID: $st $dev"
  # Windows keeps its own copy of every applied package (with the enrollment token): remove it once the join is over
  Get-ChildItem "$env:ProgramData\Microsoft\Provisioning" -Filter "kb-join*.ppkg" -ErrorAction SilentlyContinue | ForEach-Object { Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue; "  removed Windows' copy $($_.Name)" }
  # Windows Configuration Designer insists on a device name: KwickBoot's hostname wins (one more restart, at most twice)
  $pend = (Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\ComputerName\ComputerName" -ErrorAction SilentlyContinue).ComputerName
  $tries = [int](Get-Content "$dir\kb-join.renames" -ErrorAction SilentlyContinue)
  if ($cfg.NAME -and $tries -lt 2 -and (($env:COMPUTERNAME -ne $cfg.NAME) -or ($pend -and $pend -ne $cfg.NAME))) {
    Set-Content -Path "$dir\kb-join.renames" -Value ($tries + 1)
    "  the package named the PC $env:COMPUTERNAME - back to $($cfg.NAME), restarting"
    Rename-Computer -NewName $cfg.NAME -Force -ErrorAction SilentlyContinue
    Restart-Computer -Force -ErrorAction SilentlyContinue; exit 0
  }
  if ($cfg.SERVER -and $cfg.TOKEN) {
    $u = "http://$($cfg.SERVER)/agent/winjoin?mac=$($cfg.MAC)&t=$($cfg.TOKEN)&aad=$st&name=$env:COMPUTERNAME&dev=$dev"
    for ($i = 0; $i -lt 30; $i++) { try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 10 -Uri $u | Out-Null; "  reported to KwickBoot"; break } catch { Start-Sleep 10 } }
  }
  Remove-Item "$dir\kb-join.env", $mark, "$dir\kb-join.renames" -Force -ErrorAction SilentlyContinue
  "== done $(Get-Date -Format s)"
  Unregister-ScheduledTask -TaskName "KwickBoot join" -Confirm:$false -ErrorAction SilentlyContinue   # last: it ends this run
  exit 0
}
if ($AppsOnly) {
  # one run at a time (the startup trigger and the immediate start can overlap; msiexec allows one install anyway)
  $script:appsLock = New-Object System.Threading.Mutex($false, "Global\KwickBootApps")
  if (-not $script:appsLock.WaitOne(0)) { "== app bundle already running - this copy exits"; exit 0 }
  "== KwickBoot app bundle $(Get-Date -Format s)"
  Start-Sleep 60           # let Windows finish its own first-start work first
}
$level = if ($AppsOnly) { "off" } elseif ($cfg.DEBLOAT) { $cfg.DEBLOAT } else { "standard" }   # the apps run touches nothing else
if (-not $AppsOnly) { "== KwickBoot first boot $(Get-Date -Format s), debloat: $level" }

# ---- 0. drive letters for the data volumes IT asked for (label "Data D" -> D:); a DVD drive or anything else holding
#         that letter moves to the next free one. Default installs have none (one large C:).
if ($cfg.DRIVES) {
  foreach ($l in ($cfg.DRIVES -split '\+' | Where-Object { $_ -match '^[D-Z]$' })) {
    $v = Get-Volume -FileSystemLabel "Data $l" -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $v) { "  drive ${l}: volume 'Data $l' not found"; continue }
    if ($v.DriveLetter -eq $l) { "  drive ${l}: ok"; continue }
    $holder = Get-Volume -DriveLetter $l -ErrorAction SilentlyContinue
    if ($holder) {
      $used = (Get-Volume | Where-Object DriveLetter).DriveLetter + $cfg.DRIVES.Split('+')
      $free = [char[]](70..90) | Where-Object { $used -notcontains [string]$_ } | Select-Object -First 1
      $cim = Get-CimInstance Win32_Volume -Filter "DriveLetter='${l}:'"
      if ($cim -and $free) { Set-CimInstance -InputObject $cim -Property @{ DriveLetter = "${free}:" }; "  ${l}: was in use - moved that volume to ${free}:" }
    }
    try { Get-Partition -Volume $v | Set-Partition -NewDriveLetter $l -ErrorAction Stop; "  drive ${l}: assigned" }
    catch { "  drive ${l}: could not assign ($($_.Exception.Message))" }
  }
}

# ---- 1. debloat
# Removed from the image (all users, also future ones). Kept on purpose: Store, Calculator, Notepad, Photos, Terminal,
# Paint, Snipping Tool, security components, drivers' apps.
$standard = @(
  "Microsoft.BingNews", "Microsoft.BingWeather", "Microsoft.BingSearch", "Microsoft.GamingApp", "Microsoft.XboxApp",
  "Microsoft.XboxGamingOverlay", "Microsoft.XboxGameOverlay", "Microsoft.XboxIdentityProvider", "Microsoft.XboxSpeechToTextOverlay",
  "Microsoft.Xbox.TCUI", "Microsoft.MicrosoftSolitaireCollection", "Microsoft.MicrosoftOfficeHub", "Microsoft.GetHelp",
  "Microsoft.Getstarted", "Microsoft.People", "Microsoft.PowerAutomateDesktop", "Microsoft.Todos", "Microsoft.ZuneMusic",
  "Microsoft.ZuneVideo", "Microsoft.YourPhone", "Microsoft.WindowsFeedbackHub", "Microsoft.549981C3F5F10",
  "Clipchamp.Clipchamp", "MicrosoftTeams", "MSTeams", "Microsoft.MixedReality.Portal", "Microsoft.SkypeApp",
  "Microsoft.Microsoft3DViewer", "Microsoft.WindowsMaps", "Microsoft.MicrosoftStickyNotes", "Microsoft.OutlookForWindows",
  "Microsoft.Windows.DevHome", "MicrosoftCorporationII.QuickAssist", "MicrosoftCorporationII.MicrosoftFamily")
$full = $standard + @("Microsoft.Copilot", "Microsoft.Windows.Ai.Copilot.Provider", "MicrosoftWindows.Client.WebExperience",
  "Microsoft.WindowsAlarms", "Microsoft.WindowsSoundRecorder", "Microsoft.WindowsCommunicationsApps")
function Set-Reg($path, $name, $value) {
  if (-not (Test-Path $path)) { New-Item -Path $path -Force | Out-Null }
  New-ItemProperty -Path $path -Name $name -Value $value -PropertyType DWord -Force | Out-Null
  "  policy $path\$name = $value"
}
if ($level -in "standard", "full") {
  $list = if ($level -eq "full") { $full } else { $standard }
  $prov = Get-AppxProvisionedPackage -Online
  foreach ($n in $list) {
    foreach ($p in ($prov | Where-Object { $_.DisplayName -eq $n })) {
      try { Remove-AppxProvisionedPackage -Online -PackageName $p.PackageName -ErrorAction Stop | Out-Null; "  removed $($p.DisplayName)" }
      catch { "  could not remove $($p.DisplayName): $($_.Exception.Message)" }
    }
  }
  # no consumer "suggested" apps, ads or tips; no Bing in Start search
  Set-Reg "HKLM:\SOFTWARE\Policies\Microsoft\Windows\CloudContent" "DisableWindowsConsumerFeatures" 1
  Set-Reg "HKLM:\SOFTWARE\Policies\Microsoft\Windows\CloudContent" "DisableConsumerAccountStateContent" 1
  Set-Reg "HKLM:\SOFTWARE\Policies\Microsoft\Windows\CloudContent" "DisableSoftLanding" 1
  Set-Reg "HKLM:\SOFTWARE\Policies\Microsoft\Windows\Windows Search" "DisableWebSearch" 1
  Set-Reg "HKLM:\SOFTWARE\Policies\Microsoft\Windows\AdvertisingInfo" "DisabledByGroupPolicy" 1
}
if ($level -eq "full") {
  Set-Reg "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsCopilot" "TurnOffWindowsCopilot" 1
  Set-Reg "HKLM:\SOFTWARE\Policies\Microsoft\Dsh" "AllowNewsAndInterests" 0
  Set-Reg "HKLM:\SOFTWARE\Policies\Microsoft\Windows\Explorer" "HideRecommendedSection" 1
  # lowest diagnostic data level the edition allows (0 is honoured only by Enterprise/Education; others fall back to 1)
  Set-Reg "HKLM:\SOFTWARE\Policies\Microsoft\Windows\DataCollection" "AllowTelemetry" 0
}

# ---- 1b. open-source app bundle: installers from the KwickBoot server (each added there from its official URL with the
#          vendor's SHA-256); the hash is checked again here, before anything runs
if ($cfg.APPS -eq "bundle" -and $cfg.SERVER -and -not $AppsOnly) {
  # hand the bundle to a one-time background task (SYSTEM, at startup + now); it keeps only the server address
  # report value for the console = HMAC-SHA256(check-in secret, "apps"); the secret itself is deleted after the check-in
  $tok = ""
  if ($cfg.SECRET) {
    $hm = New-Object System.Security.Cryptography.HMACSHA256 (, [Text.Encoding]::ASCII.GetBytes($cfg.SECRET))
    $tok = -join ($hm.ComputeHash([Text.Encoding]::ASCII.GetBytes("apps")) | ForEach-Object { $_.ToString("x2") })
  }
  "SERVER=$($cfg.SERVER)`r`nAPPS=bundle`r`nMAC=$($cfg.MAC)`r`nTOKEN=$tok" | Set-Content -Encoding ascii "$dir\kb-apps.env"
  $act = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -Command `"& '$dir\kb-firstboot.ps1' -AppsOnly *>&1 | Out-File -Append -Encoding utf8 '$dir\kb-apps.log'`""
  $trg = New-ScheduledTaskTrigger -AtStartup
  Register-ScheduledTask -TaskName "KwickBoot apps" -Action $act -Trigger $trg -User SYSTEM -RunLevel Highest -Force | Out-Null
  Start-ScheduledTask -TaskName "KwickBoot apps" -ErrorAction SilentlyContinue
  "  app bundle: installing in the background after setup (log: $dir\kb-apps.log)"
}
if ($cfg.APPS -eq "bundle" -and $cfg.SERVER -and $AppsOnly) {
  $okN = 0; $failed = @()
  try {
    $cat = (Invoke-WebRequest -UseBasicParsing -TimeoutSec 20 -Uri "http://$($cfg.SERVER)/winapps/catalog.tsv").Content -split "`n" | Where-Object { $_.Trim() }
    foreach ($line in $cat) {
      $f = $line.TrimEnd("`r") -split "`t"; if ($f.Count -lt 5) { continue }
      $name, $file, $sha, $kind, $sargs = $f[0], $f[1], $f[2], $f[3], $f[4]
      $dst = Join-Path $env:TEMP $file
      try {
        # Windows' own curl.exe: full LAN speed (PowerShell 5.1's Invoke-WebRequest crawls and timed out on big files);
        # two tries, each checked against the catalog's SHA-256 before anything runs
        $good = $false
        for ($try = 1; $try -le 2 -and -not $good; $try++) {
          & "$env:WINDIR\System32\curl.exe" -sf --retry 3 --connect-timeout 20 -o $dst "http://$($cfg.SERVER)/winapps/$file"
          $good = (Test-Path $dst) -and (Get-FileHash $dst -Algorithm SHA256).Hash.ToLower() -eq $sha
          if (-not $good) { "  $($name): download $try did not match the checksum" }
        }
        if (-not $good) { "  $($name): CHECKSUM MISMATCH - not installed"; $failed += $name; Remove-Item $dst -Force -ErrorAction SilentlyContinue; continue }
        $p = if ($kind -eq "msi") { Start-Process msiexec.exe -ArgumentList "/i `"$dst`" $sargs" -Wait -PassThru } else { Start-Process $dst -ArgumentList $sargs -Wait -PassThru }
        "  installed $name (exit $($p.ExitCode))"
        if ($p.ExitCode -in 0, 3010, 1641) { $okN++ } else { $failed += $name }      # 3010/1641 = done, restart pending
      } catch { "  $($name): $($_.Exception.Message)"; $failed += $name }
      Remove-Item $dst -Force -ErrorAction SilentlyContinue
    }
  } catch { "  app bundle: catalog not reachable ($($_.Exception.Message))"; "== will retry at the next start"; exit 1 }
  # tell the console (once): how many installed, which failed
  if ($cfg.TOKEN -and $cfg.MAC) {
    $url = "http://$($cfg.SERVER)/agent/winapps?mac=$($cfg.MAC)&t=$($cfg.TOKEN)&ok=$okN&fail=" + [uri]::EscapeDataString(($failed -join ","))
    for ($i = 0; $i -lt 10; $i++) { try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 10 -Uri $url | Out-Null; "  reported to KwickBoot: $okN installed, failed: $($failed -join ', ')"; break } catch { Start-Sleep 15 } }
  }
  # one time only: clean up FIRST - unregistering the task stops this very run (a failed catalog fetch above keeps the
  # task for the next start)
  Remove-Item "$dir\kb-apps.env" -Force -ErrorAction SilentlyContinue
  "== app bundle done $(Get-Date -Format s)"
  Unregister-ScheduledTask -TaskName "KwickBoot apps" -Confirm:$false -ErrorAction SilentlyContinue
  exit 0
}

# ---- 2. the admin account chooses its own password at the first sign-in
if ($cfg.USER) { & net.exe user $cfg.USER /logonpasswordchg:yes | Out-Null; "  $($cfg.USER): password to be set at first sign-in" }

# ---- 2b. KwickBoot stays in control: Windows setup puts "Windows Boot Manager" first in the firmware boot order;
#          network (PXE IPv4) goes first again - the server hands the PC to its disk unless IT chose otherwise
if (-not $AppsOnly -and $cfg.BOOTORDER -eq "os-first") {
  "  boot order: Windows first (this department boots the network only when IT starts a reinstall)"
} elseif (-not $AppsOnly) {
  $fw = (& bcdedit.exe /enum firmware) -join "`n"
  $net = [regex]::Matches($fw, '(?ms)^identifier\s+(\{[0-9a-f-]+\})\s*\r?\n.*?^description\s+([^\r\n]*)') |
    Where-Object { $_.Groups[2].Value -match 'PXE|IPv4|Network|NIC' -and $_.Groups[2].Value -notmatch 'IPv6|HTTP' } | Select-Object -First 1
  if ($net) { & bcdedit.exe /set "{fwbootmgr}" displayorder $net.Groups[1].Value /addfirst | Out-Null; "  boot order: network first ($($net.Groups[2].Value))" }
  else { "  boot order: no network boot entry found in the firmware" }
}

# ---- 2b2. drivers from the KwickBoot library (C:\Drivers): setup already used them for present devices; pnputil adds
#          them all to the driver store (devices that appear later find them too), then the folder goes
if (Test-Path "$env:SystemDrive\Drivers") {
  $o = & pnputil.exe /add-driver "$env:SystemDrive\Drivers\*.inf" /subdirs /install 2>&1
  $added = ($o | Select-String -Pattern 'Driver package added successfully|Published Name' -AllMatches).Count
  $tot = ($o | Select-String -Pattern 'Total driver packages:\s*(\d+)').Matches | Select-Object -First 1
  "  drivers: pnputil processed $(if ($tot) { $tot.Groups[1].Value } else { '?' }) package(s), $added added"
  ($o | Select-String -Pattern 'Failed|error' | Select-Object -First 5) | ForEach-Object { "    $_" }
  Remove-Item "$env:SystemDrive\Drivers" -Recurse -Force -ErrorAction SilentlyContinue
}

# ---- 2b3. support information as Windows shows it (Settings > System > About), for the log
$oi = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\OEMInformation" -ErrorAction SilentlyContinue
if ($oi -and ($oi.Manufacturer -or $oi.SupportPhone -or $oi.SupportURL)) {
  "  support info: $($oi.Manufacturer) | $($oi.Model) | $($oi.SupportPhone) | $($oi.SupportHours) | $($oi.SupportURL)"
}

# ---- 2c. the answer file may hold an offline domain join blob: setup is over, nothing needs it any more
foreach ($f in @("$env:WINDIR\Panther\unattend.xml", "$env:WINDIR\Panther\Unattend\unattend.xml", "$env:WINDIR\System32\Sysprep\unattend.xml")) {
  if (Test-Path $f) { Remove-Item $f -Force -ErrorAction SilentlyContinue; "  removed answer file $f" }
}
$cs = Get-CimInstance Win32_ComputerSystem
$dom = if ($cs.PartOfDomain) { $cs.Domain } else { "" }
if ($dom) { "  joined domain $dom" }

# ---- 2d. Entra ID: the department's provisioning package is applied AFTER the check-in, by a one-time startup task
$aad = ""
if (Test-Path "$dir\kb-join.ppkg") {
  $jt = ""
  if ($cfg.SECRET) {
    $hm = New-Object System.Security.Cryptography.HMACSHA256 (, [Text.Encoding]::ASCII.GetBytes($cfg.SECRET))
    $jt = -join ($hm.ComputeHash([Text.Encoding]::ASCII.GetBytes("join")) | ForEach-Object { $_.ToString("x2") })
  }
  "SERVER=$($cfg.SERVER)`r`nMAC=$($cfg.MAC)`r`nNAME=$env:COMPUTERNAME`r`nTOKEN=$jt" | Set-Content -Encoding ascii "$dir\kb-join.env"
  $act = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -Command `"& '$dir\kb-firstboot.ps1' -JoinOnly *>&1 | Out-File -Append -Encoding utf8 '$dir\kb-join.log'`""
  Register-ScheduledTask -TaskName "KwickBoot join" -Action $act -Trigger (New-ScheduledTaskTrigger -AtStartup) -User SYSTEM -RunLevel Highest -Force | Out-Null
  $aad = "applying"
  "  Entra ID: joins right after the check-in (log: $dir\kb-join.log)"
}

# ---- 3. check in (one-time secret), then remove it from the machine
if ($cfg.SERVER -and $cfg.MAC -and $cfg.SECRET) {
  $url = "http://$($cfg.SERVER)/agent/winok?mac=$($cfg.MAC)&t=$($cfg.SECRET)&dom=$dom&aad=$aad&ver=" + [uri]::EscapeDataString((Get-CimInstance Win32_OperatingSystem).Caption)
  for ($i = 0; $i -lt 30; $i++) {
    try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 10 -Uri $url | Out-Null; "  checked in with KwickBoot"; break } catch { Start-Sleep 10 }
  }
}
Remove-Item "$dir\kb.env" -Force -ErrorAction SilentlyContinue
if ($aad -eq "applying") { Start-ScheduledTask -TaskName "KwickBoot join" -ErrorAction SilentlyContinue }   # after the check-in
"== done $(Get-Date -Format s)"
