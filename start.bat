@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title TraderOS 서버

rem 업데이트로 start.bat 자체가 바뀐 경우: 새 파일로 바꾼 뒤 새로 실행 (실행 중인 배치 파일은 바로 못 바꿔서)
if exist "start.bat.new" (move /y "start.bat.new" "start.bat" >nul & call "%~f0" & exit /b)

if not exist "venv\Scripts\python.exe" (
    echo  처음 실행이라 설치부터 합니다.
    call install.bat || exit /b 1
)

rem 처음 설치했을 때 바깥 접속은 Tailscale 로만 허용 (관리 화면 '접속' 탭에서 바꿀 수 있음)
if not defined TRADEROS_ACCESS_DEFAULT set "TRADEROS_ACCESS_DEFAULT=tailscale"

:run
echo.
echo  TraderOS 서버를 켭니다. 이 창을 닫으면 서버가 꺼집니다.
echo    관리 화면 : http://127.0.0.1:8100/   (자동으로 열립니다)
echo    웹 화면   : http://127.0.0.1:8000/app/
echo.
"venv\Scripts\python.exe" -m uvicorn main:app --host 0.0.0.0 --port 8000

rem 관리 화면에서 [업데이트]를 누르면 서버가 새 버전을 받아 두고 꺼진다 → 파일을 바꾸고 다시 켬
if exist "data\update\apply.flag" (
    echo.
    echo  ===== 업데이트 적용 중 - 창을 닫지 마세요 =====
    "venv\Scripts\python.exe" util\apply_update.py
    rem 관리 화면이 열려 있으니 브라우저를 또 열지 않음
    set "TRADEROS_OPEN_BROWSER=0"
    if exist "start.bat.new" (move /y "start.bat.new" "start.bat" >nul & call "%~f0" & exit /b)
    goto run
)

echo.
echo  서버가 꺼졌습니다.
pause
