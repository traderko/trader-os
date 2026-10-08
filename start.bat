@echo off
rem TraderOS server launcher.
rem NOTE: keep this file ASCII-only. cmd.exe misreads batch files that contain
rem multi-byte (Korean) text after a long-running command, so messages are English.
setlocal
cd /d "%~dp0"
title TraderOS server

rem start.bat itself was replaced by an update -> swap in the new file and re-run it
if exist "start.bat.new" (move /y "start.bat.new" "start.bat" >nul & call "%~f0" & exit /b)

if not exist "venv\Scripts\python.exe" (
    echo  First run - installing first.
    call install.bat || exit /b 1
)

rem First install: allow outside access only via Tailscale (change it in the admin page, Access tab)
if not defined TRADEROS_ACCESS_DEFAULT set "TRADEROS_ACCESS_DEFAULT=tailscale"

:run
echo.
echo  Starting TraderOS server. Closing this window stops the server.
echo    Admin page : http://127.0.0.1:8100/   (opens automatically)
echo    Web app    : http://127.0.0.1:8000/app/
echo.
"venv\Scripts\python.exe" -m uvicorn main:app --host 0.0.0.0 --port 8000

rem [Update] in the admin page downloads the new version and stops the server -> apply it and restart
if exist "data\update\apply.flag" goto update

echo.
echo  Server stopped.
pause
exit /b 0

:update
echo.
echo  ===== Applying update - do not close this window =====
"venv\Scripts\python.exe" util\apply_update.py
rem the admin page is already open, so do not open another browser tab
set "TRADEROS_OPEN_BROWSER=0"
if exist "start.bat.new" (move /y "start.bat.new" "start.bat" >nul & call "%~f0" & exit /b)
goto run
