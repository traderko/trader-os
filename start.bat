@echo off
rem TraderOS server launcher.
rem NOTE: keep this file ASCII-only. cmd.exe misreads batch files that contain
rem multi-byte (Korean) text after a long-running command, so messages are English.
setlocal
cd /d "%~dp0"
title TraderOS server

rem Already running? (port 8000 in use) -> just open the admin page, do not start a second server
netstat -an | findstr /c:":8000 " | findstr /c:"LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo  TraderOS is already running. Opening the admin page.
    start "" "http://127.0.0.1:8100/"
    timeout /t 3 >nul
    exit /b 0
)

rem Always run as administrator (MT5 terminals are controlled from here).
rem Set TRADEROS_NO_ADMIN=1 before running to skip this.
if defined TRADEROS_NO_ADMIN goto admin_ok
fltmc >nul 2>&1 && goto admin_ok

rem Registered in Task Scheduler (first run did it) -> start through it, no UAC prompt
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0util\win_task.ps1" -Mode run -Bat "%~f0" >nul 2>&1
if not errorlevel 1 (
    echo  Starting TraderOS as administrator - a new window opens.
    timeout /t 2 >nul
    exit /b 0
)

rem First time: ask once (UAC). The elevated run registers the task for next time.
echo  Requesting administrator rights - click "Yes" in the Windows prompt.
echo  This is asked only once; later starts go through Task Scheduler without the prompt.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -WorkingDirectory '%~dp0' -Verb RunAs" >nul 2>&1
if errorlevel 1 (
    echo.
    echo  [!] Administrator rights were not granted. TraderOS was not started.
    echo      Run start.bat again and click "Yes", or right-click it - Run as administrator.
    pause
)
exit /b
:admin_ok

rem Register (or update, if this folder moved) the Task Scheduler entry. Set TRADEROS_NO_TASK=1 to skip.
if not defined TRADEROS_NO_ADMIN if not defined TRADEROS_NO_TASK (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0util\win_task.ps1" -Mode register -Bat "%~f0"
)

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
