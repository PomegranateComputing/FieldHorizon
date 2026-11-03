@echo off
rem Field Horizon -- double-click launcher. Same arguments as FieldHorizon.ps1 (start / stop / status / open / restart).
rem PowerShell 7 when installed, Windows PowerShell otherwise. The execution-policy bypass applies to this one
rem process only; nothing is changed system-wide.
where pwsh.exe >nul 2>nul
if %ERRORLEVEL%==0 (
  pwsh.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0FieldHorizon.ps1" %*
) else (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0FieldHorizon.ps1" %*
)
exit /b %ERRORLEVEL%
