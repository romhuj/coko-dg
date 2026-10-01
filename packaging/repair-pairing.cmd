@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0repair-pairing.ps1" %*
if errorlevel 1 echo Pairing repair was not applied. Read the error above.
pause
