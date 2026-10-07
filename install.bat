@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title TraderOS 설치

echo.
echo  TraderOS 설치를 시작합니다. (처음 한 번만, 5~10분 걸릴 수 있습니다)
echo.

rem ── 1) Python 3.12 찾기 (없으면 winget 으로 설치) ──
set "PY="
call :find_python
if not defined PY (
    echo  Python 3.12 가 없어 설치합니다...
    winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
    call :find_python
)
if not defined PY (
    echo.
    echo  [실패] Python 을 찾지 못했습니다.
    echo  https://www.python.org/downloads/ 에서 Python 3.12 를 설치한 뒤 이 파일을 다시 실행하세요.
    echo  설치할 때 "Add python.exe to PATH" 에 체크하세요.
    pause
    exit /b 1
)
echo  Python: %PY%

rem ── 2) 가상환경 + 패키지 ──
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

:find_python
py -3.12 -V >nul 2>nul && (set "PY=py -3.12" & exit /b 0)
if exist "%LocalAppData%\Programs\Python\Python312\python.exe" (set "PY="%LocalAppData%\Programs\Python\Python312\python.exe"" & exit /b 0)
if exist "%ProgramFiles%\Python312\python.exe" (set "PY="%ProgramFiles%\Python312\python.exe"" & exit /b 0)
exit /b 0

:fail
echo.
echo  [실패] 설치 중 오류가 났습니다. 위 메시지를 확인하세요.
pause
exit /b 1
