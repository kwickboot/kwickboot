@echo off
rem KwickBoot USB Creator - double-click to start (asks for administrator rights: it partitions the USB stick)
rem SPDX-License-Identifier: AGPL-3.0-or-later
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0kwickboot-usb.ps1" %*
if errorlevel 1 pause
