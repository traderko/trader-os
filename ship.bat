@echo off
rem Commit + push + release in one go, without the y/N question.
rem   ship.bat           next version (minor if a "bump: minor" is pending, else patch)
rem   ship.bat 0.3.0     this exact version
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\publish.ps1" release -y %*
if errorlevel 1 (
  pause
) else (
  timeout /t 5
)
