@echo off
rem TraderOS installer (run once). Keep this file ASCII-only (see start.bat).
setlocal
cd /d "%~dp0"
title TraderOS install

echo.
echo  Installing TraderOS. This runs once and can take 5-10 minutes.
echo.

rem 1) Find x64 Python 3.12, or download it from python.org.
rem    The MetaTrader5 package only ships x64 (AMD64) builds, so on ARM Windows
rem    (Mac VMware/Parallels, Surface ...) we still need x64 Python. Windows runs it
rem    under emulation, the same way it runs MT5 itself.
set "PY_URL=https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
call :find_python
if defined PY goto have_python

echo  x64 Python 3.12 not found - downloading it from python.org ...
rem (winget will not install x64 next to an existing ARM64 Python, so download directly)
curl -L --fail -o "%TEMP%\python-3.12-amd64.exe" "%PY_URL%" || goto fail
"%TEMP%\python-3.12-amd64.exe" /quiet InstallAllUsers=0 PrependPath=0 Include_launcher=0 Include_test=0 TargetDir="%LocalAppData%\Programs\Python\Python312" || goto fail
del "%TEMP%\python-3.12-amd64.exe" >nul 2>nul
call :find_python
if defined PY goto have_python

echo.
echo  [FAILED] Could not find x64 Python 3.12.
echo  Download "Windows installer (64-bit)" from https://www.python.org/downloads/windows/
echo  install it, then run this file again. On ARM Windows pick 64-bit (x64), not ARM64.
pause
exit /b 1

:have_python
echo  Python: %PY%

rem 2) Virtual environment + packages
if not exist "venv\Scripts\python.exe" goto make_venv
"venv\Scripts\python.exe" -c "import sysconfig,sys; sys.exit(0 if sysconfig.get_platform()=='win-amd64' else 1)" >nul 2>nul
if not errorlevel 1 goto packages
echo  Existing venv was made with ARM64 Python - recreating it ...
rmdir /s /q venv

:make_venv
%PY% -m venv venv || goto fail

:packages
"venv\Scripts\python.exe" -m pip install --upgrade pip
"venv\Scripts\python.exe" -m pip install -r requirements.txt || goto fail

echo.
echo  Install finished. Run start.bat.
echo.
pause
exit /b 0

rem Try x64 Python 3.12 candidates in order.
rem (platform.machine() reports ARM64 for x64 Python on ARM Windows, so check the
rem  interpreter build itself with sysconfig.get_platform())
:find_python
set "PY="
call :try_py py -V:3.12
if defined PY exit /b 0
call :try_py py -3.12
if defined PY exit /b 0
call :try_py "%LocalAppData%\Programs\Python\Python312\python.exe"
if defined PY exit /b 0
call :try_py "%ProgramFiles%\Python312\python.exe"
exit /b 0

:try_py
%* -c "import sysconfig,sys; sys.exit(0 if sysconfig.get_platform()=='win-amd64' and sys.version_info[:2]==(3,12) else 1)" >nul 2>nul
if not errorlevel 1 set "PY=%*"
exit /b 0

:fail
echo.
echo  [FAILED] Install error - see the messages above.
pause
exit /b 1
