@echo off
rem Tray icon (tray.ps1) starts the server once on port 8770 and keeps it; menu: open / restart / close.
rem Second run only opens the page. /silent - no browser (autostart).
cd /d "%~dp0"
set OPEN=-Open
if /i "%1"=="/silent" set OPEN=
start "" powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0tray.ps1" %OPEN%
