@echo off
REM Install Aither on this Windows PC -- double-click me.
REM Runs the same one line the install guide gives (install-aither-windows.ps1 from
REM Aitherium/awnix). Unsigned on purpose: Windows may say "Windows protected your PC";
REM click "More info", then "Run anyway". Nothing here needs administrator rights;
REM Windows asks once, later, only if WSL has to be turned on.
title Installing Aither
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/Aitherium/awnix/main/.DEPLOYMENT/standalone/bootc/install-aither-windows.ps1 | iex"
if errorlevel 1 pause
