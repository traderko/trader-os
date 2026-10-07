@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title TraderOS 서버

if not exist "venv\Scripts\python.exe" (
    echo  처음 실행이라 설치부터 합니다.
    call install.bat || exit /b 1
)

rem 처음 설치했을 때 바깥 접속은 Tailscale 로만 허용 (관리 화면 '접속' 탭에서 바꿀 수 있음)
if not defined TRADEROS_ACCESS_DEFAULT set "TRADEROS_ACCESS_DEFAULT=tailscale"

echo.
echo  TraderOS 서버를 켭니다. 이 창을 닫으면 서버가 꺼집니다.
echo    관리 화면 : http://127.0.0.1:8100/   (자동으로 열립니다)
echo    웹 화면   : http://127.0.0.1:8000/app/
echo.
"venv\Scripts\python.exe" -m uvicorn main:app --host 0.0.0.0 --port 8000
echo.
echo  서버가 꺼졌습니다.
pause
