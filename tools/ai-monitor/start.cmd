@echo off
rem Tray icon (tray.ps1) starts the server once on port 8770 and keeps it; menu: open / restart / close.
rem Second run only opens the page. /silent - no browser (autostart).
rem Via launch.pyw (pythonw, no console window): with Windows Terminal as default console
rem "powershell -WindowStyle Hidden" leaves a minimized terminal window.
cd /d "%~dp0"
where pythonw >nul 2>&1 && (start "" pythonw "%~dp0launch.pyw" %1 & exit /b)
where pyw >nul 2>&1 && (start "" pyw "%~dp0launch.pyw" %1 & exit /b)
set OPEN=-Open
if /i "%1"=="/silent" set OPEN=
start "" powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0tray.ps1" %OPEN%
