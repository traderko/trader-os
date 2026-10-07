# services/ea_status.py
#
# 각 계좌의 tradeLock EA가 서버에 잠금 상태를 마지막으로 물어본 시각.
# EA는 3초마다 GET /lock/status 를 부르므로, 최근에 들어온 요청이 있으면
# "MT5 WebRequest 허용 목록에 서버 주소가 있고, EA가 차트에서 돌고 있다"는 뜻이다.
# (허용 목록 자체는 MT5가 암호화해 저장해서 직접 읽을 수 없음 → 실제 요청으로 판단)
# 메인 서버와 관리 화면이 같은 프로세스라 메모리에만 둔다.

import time

FRESH_SEC = 20      # 이 안에 요청이 있었으면 연결됨
GRACE_SEC = 60      # 서버 시작 직후 이 시간 동안은 "기다리는 중"

_seen: dict[int, float] = {}


def mark(account_id: int) -> None:
    _seen[account_id] = time.time()


def state(account_id: int, server_started_at: float) -> dict:
    now = time.time()
    last = _seen.get(account_id)
    if last is not None and now - last <= FRESH_SEC:
        return {"state": "ok", "ago_sec": int(now - last)}
    if now - server_started_at < GRACE_SEC:
        return {"state": "waiting", "ago_sec": None if last is None else int(now - last)}
    return {"state": "none", "ago_sec": None if last is None else int(now - last)}
