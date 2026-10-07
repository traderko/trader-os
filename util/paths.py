# util/paths.py
#
# MT5 관련 경로. .env 로 바꿀 수 있다 (없으면 기본값).
#   TRADEROS_MT5_DIR    : 계좌별 MT5를 복사해 둘 폴더 (기본 C:\mt5 → C:\mt5\<계좌번호>\terminal64.exe)
#   TRADEROS_MT5_SOURCE : 복사할 원본 MT5 설치 폴더 (기본: C:\Program Files\MetaTrader 5,
#                         없으면 Program Files 아래에서 terminal64.exe 가 있는 폴더를 찾음 - 브로커 MT5도 됨)

import glob
import os

from dotenv import load_dotenv

load_dotenv()

TERMINAL_BASE_DIR = os.getenv("TRADEROS_MT5_DIR", "").strip() or r"C:\mt5"


def _find_mt5_source() -> str:
    env = os.getenv("TRADEROS_MT5_SOURCE", "").strip()
    if env:
        return env
    default = r"C:\Program Files\MetaTrader 5"
    if os.path.isfile(os.path.join(default, "terminal64.exe")):
        return default
    for pf in (os.getenv("ProgramFiles"), os.getenv("ProgramW6432"), r"C:\Program Files"):
        if not pf:
            continue
        for exe in sorted(glob.glob(os.path.join(pf, "*", "terminal64.exe"))):
            return os.path.dirname(exe)
    return default


SOURCE_MT5_INSTALL_DIR = _find_mt5_source()
