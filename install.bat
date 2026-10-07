@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title TraderOS 설치

echo.
echo  TraderOS 설치를 시작합니다. (처음 한 번만, 5~10분 걸릴 수 있습니다)
echo.

rem ── 1) x64 Python 3.12 찾기 (없으면 python.org 에서 받아 설치) ──
rem    MetaTrader5 파이썬 패키지는 x64(AMD64)용만 있다. ARM 윈도우(맥 VMware·Parallels, Surface 등)에
rem    ARM64용 Python 을 깔면 "No matching distribution found for metatrader5" 가 나므로
rem    ARM 윈도우에서도 x64 Python 을 쓴다 (윈도우가 에뮬레이션으로 돌려 줌 - MT5 도 같은 방식으로 돔)
set "PY_URL=https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
call :find_python
if not defined PY (
    echo  x64 Python 3.12 가 없어 python.org 에서 받아 설치합니다...
    rem winget 은 ARM64 Python 이 이미 깔려 있으면 x64 를 따로 설치하지 않아서 설치 파일을 직접 받는다
    curl -L --fail -o "%TEMP%\python-3.12-amd64.exe" "%PY_URL%" || goto :fail
    "%TEMP%\python-3.12-amd64.exe" /quiet InstallAllUsers=0 PrependPath=0 Include_launcher=0 Include_test=0 TargetDir="%LocalAppData%\Programs\Python\Python312" || goto :fail
    del "%TEMP%\python-3.12-amd64.exe" >nul 2>nul
    call :find_python
)
if not defined PY (
    echo.
    echo  [실패] x64 Python 3.12 를 찾지 못했습니다.
    echo  https://www.python.org/downloads/windows/ 에서 "Windows installer (64-bit)" 를 받아 설치한 뒤
    echo  이 파일을 다시 실행하세요. ARM 윈도우라도 ARM64 가 아니라 64-bit^(x64^) 를 받아야 합니다.
    pause
    exit /b 1
)
echo  Python: %PY%

rem ── 2) 가상환경 + 패키지 ──
if exist "venv\Scripts\python.exe" (
    "venv\Scripts\python.exe" -c "import sysconfig,sys; sys.exit(0 if sysconfig.get_platform()=='win-amd64' else 1)" >nul 2>nul || (
        echo  기존 가상환경이 ARM64 Python 으로 만들어져 있어 다시 만듭니다...
        rmdir /s /q venv
    )
)
if not exist "venv\Scripts\python.exe" (
    %PY% -m venv venv || goto :fail
)
"venv\Scripts\python.exe" -m pip install --upgrade pip
"venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :fail

echo.
echo  설치가 끝났습니다. start.bat 을 실행하세요.
echo.
pause
exit /b 0

rem x64 Python 3.12 후보를 차례로 확인
rem   (platform.machine() 은 ARM 윈도우에서 x64 Python 이어도 ARM64 라고 답해서, 파이썬 자체의 빌드 종류 sysconfig.get_platform() 으로 확인)
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
echo  [실패] 설치 중 오류가 났습니다. 위 메시지를 확인하세요.
pause
exit /b 1
