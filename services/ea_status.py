# services/ea_status.py
#
# 각 계좌의 tradeLock EA가 서버에 잠금 상태를 마지막으로 물어본 시각.
# EA는 3초마다 GET /lock/status 를 부르므로, 최근에 들어온 요청이 있으면
# "MT5 WebRequest 허용 목록에 서버 주소가 있고, EA가 차트에서 돌고 있다"는 뜻이다.
# (허용 목록 자체는 MT5가 암호화해 저장해서 직접 읽을 수 없음 → 실제 요청으로 판단)
# 메인 서버와 관리 화면이 같은 프로세스라 메모리에만 둔다.

import os
import re
import time

FRESH_SEC = 20      # 이 안에 요청이 있었으면 연결됨
GRACE_SEC = 60      # 서버 시작 직후 이 시간 동안은 "기다리는 중"

_seen: dict[int, float] = {}
_ver: dict[int, str] = {}           # 계좌 → EA 가 보낸 버전 (예전 EA 는 안 보냄 → 없음)
_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mt5_ea", "tradeLock.mq5")


def latest_version() -> str | None:
    """mt5_ea/tradeLock.mq5 의 #define EA_VERSION "x.y.z" - 서버가 깔아 주는 EA 의 버전"""
    try:
        with open(_SRC, encoding="utf-8-sig", errors="ignore") as f:
            m = re.search(r'#define\s+EA_VERSION\s+"([^"]+)"', f.read())
        return m.group(1) if m else None
    except OSError:
        return None


def mark(account_id: int, version: str | None = None) -> None:
    _seen[account_id] = time.time()
    _ver[account_id] = (version or "").strip()[:20]


def version_info(account_id: int) -> dict:
    """{"version": EA가 보낸 값 ('' = 예전 EA, None = 아직 요청 없음), "latest", "outdated"}"""
    v, latest = _ver.get(account_id), latest_version()
    return {"version": v, "latest": latest, "outdated": v is not None and bool(latest) and v != latest}


def state(account_id: int, server_started_at: float) -> dict:
    now = time.time()
    last = _seen.get(account_id)
    if last is not None and now - last <= FRESH_SEC:
        return {"state": "ok", "ago_sec": int(now - last), **version_info(account_id)}
    if now - server_started_at < GRACE_SEC:
        return {"state": "waiting", "ago_sec": None if last is None else int(now - last)}
    return {"state": "none", "ago_sec": None if last is None else int(now - last)}
