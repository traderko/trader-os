@echo off
rem Commit + push TraderOS.
rem   publish.bat                 commit + push
rem   publish.bat release         also tag the next version -> GitHub builds the Release
rem   publish.bat release 0.3.0   tag this exact version
rem   publish.bat -m "message"    commit message (optional; else publish.msg written by Claude, else made from the changed files)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\publish.ps1" %*
pause
