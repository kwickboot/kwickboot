@echo off
rem KwickBoot: Windows runs this once as SYSTEM at the end of its setup, before the first sign-in.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%WINDIR%\Setup\Scripts\kb-firstboot.ps1" >> "%WINDIR%\Setup\Scripts\kb-firstboot.log" 2>&1
