# make-generic-bcd.ps1 - build KwickBoot's two generic Windows boot stores (BCD) with Microsoft's own bcdedit.
# Run ONCE, elevated (Run as administrator), on any Windows 10/11 PC:
#     powershell -ExecutionPolicy Bypass -File make-generic-bcd.ps1
# Output (next to this script): BCD-uefi and BCD-bios. They contain no disk IDs: the boot manager is on the partition it
# was started from ("boot") and Windows is found by searching for its loader ("locate"), so the same file works on every
# disk KwickBoot installs Windows onto. The KwickBoot RAM OS copies the right one into place after applying install.wim.
$ErrorActionPreference = "Stop"
$out = $PSScriptRoot
function New-Store($file, $loader, $mgr) {
    if (Test-Path $file) { Remove-Item $file -Force }
    bcdedit /createstore $file | Out-Null
    bcdedit /store $file /create '{bootmgr}' /d "Windows Boot Manager" | Out-Null
    bcdedit /store $file /set '{bootmgr}' device boot | Out-Null
    if ($mgr) { bcdedit /store $file /set '{bootmgr}' path $mgr | Out-Null }
    bcdedit /store $file /set '{bootmgr}' timeout 0 | Out-Null
    $id = ((bcdedit /store $file /create /d "Windows" /application osloader) -replace '.*(\{[0-9a-fA-F-]+\}).*', '$1').Trim()
    bcdedit /store $file /set $id device "locate=\Windows\system32\$loader" | Out-Null
    bcdedit /store $file /set $id osdevice "locate=\Windows\system32\$loader" | Out-Null
    bcdedit /store $file /set $id path "\Windows\system32\$loader" | Out-Null
    bcdedit /store $file /set $id systemroot "\Windows" | Out-Null
    bcdedit /store $file /set $id locale "en-US" | Out-Null
    bcdedit /store $file /set $id nx OptIn | Out-Null
    bcdedit /store $file /set '{bootmgr}' default $id | Out-Null
    bcdedit /store $file /set '{bootmgr}' displayorder $id | Out-Null
    bcdedit /store $file /enum all
    # mark it as the SYSTEM store (what bcdboot writes): Windows setup's specialize pass opens the system store and stops with
    # "BCD: File is not system store" / "could not configure Windows to run on this computer's hardware" otherwise
    reg load HKLM\KB_BCD $file | Out-Null
    reg add HKLM\KB_BCD\Description /v System /t REG_DWORD /d 1 /f | Out-Null
    reg add HKLM\KB_BCD\Description /v TreatAsSystem /t REG_DWORD /d 1 /f | Out-Null
    [gc]::Collect(); Start-Sleep -Milliseconds 500
    reg unload HKLM\KB_BCD | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "could not unload the BCD hive ($file) - close regedit and run again" }
}
New-Store (Join-Path $out "BCD-uefi") "winload.efi" "\EFI\Microsoft\Boot\bootmgfw.efi"
New-Store (Join-Path $out "BCD-bios") "winload.exe" $null
Get-FileHash (Join-Path $out "BCD-uefi"), (Join-Path $out "BCD-bios") -Algorithm SHA256 | Format-Table -AutoSize
Write-Host "Done. Tell Claude 'BCD done'."
