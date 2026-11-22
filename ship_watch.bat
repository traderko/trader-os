@echo off
rem TraderOS ship watcher - leave this window open (minimized is fine).
rem Every 30 seconds: if a file named ship.now is in this folder, delete it and run
rem tools\publish.ps1 release -y  (same as ship.bat: commit + push + release, no question).
rem Claude drops ship.now into the folder after applying changes, so "apply it" also ships it.
rem Output goes to this window and to logs\ship_watch.log
title TraderOS ship watcher
cd /d "%~dp0"
if not exist logs mkdir logs
echo TraderOS ship watcher started %date% %time%
echo Waiting for ship.now ... (close this window to stop)
:loop
if not exist ship.now goto wait
del /q ship.now
echo.
echo [%date% %time%] ship.now found - publishing
echo [%date% %time%] ship start>> logs\ship_watch.log
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\publish.ps1" release -y >> logs\ship_watch.log 2>&1
if errorlevel 1 goto failed
echo [%date% %time%] done
echo [%date% %time%] ship done>> logs\ship_watch.log
goto wait
:failed
echo [%date% %time%] FAILED - see logs\ship_watch.log
echo [%date% %time%] ship FAILED>> logs\ship_watch.log
:wait
timeout /t 30 /nobreak >nul
goto loop
