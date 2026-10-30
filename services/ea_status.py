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


_src: dict[int, dict[tuple, float]] = {}   # 계좌 → {(버전, 보낸 곳 IP): 마지막 시각} - EA 가 두 군데서 돌면 둘 다 보임
_first: dict[int, float] = {}               # 계좌 → 이 서버에서 처음 요청을 받은 시각
_warned: dict[int, float] = {}              # 계좌 → 예전 EA 알림 보낸 시각 (서버를 켜 있는 동안 한 번, 10분 넘게 정상이면 다시 가능)
_bad_at: dict[int, float] = {}              # 계좌 → 예전 EA 요청을 마지막으로 본 시각
ACTIVE_SEC = 30                             # 이 안에 요청이 있었던 EA 만 '돌고 있음'
SETTLE_SEC = 15                             # 서버 시작 직후 새 EA·예전 EA 가 다 한 번씩 물어볼 때까지 기다림


def _active(account_id: int) -> dict[tuple, float]:
    now = time.time()
    return {k: t for k, t in (_src.get(account_id) or {}).items() if now - t <= ACTIVE_SEC}


def mark(account_id: int, version: str | None = None, account_number: str = "", notify: bool = True,
         ip: str = "") -> None:
    now = time.time()
    v = (version or "").strip()[:20]
    _seen[account_id] = now
    _ver[account_id] = v
    _first.setdefault(account_id, now)
    _src.setdefault(account_id, {})[(v, ip or "")] = now
    info = version_info(account_id)
    if info["outdated"]:
        _bad_at[account_id] = now
    elif now - _bad_at.get(account_id, 0) > 600:
        _warned.pop(account_id, None)      # 10분 넘게 예전 EA 요청이 없었음 → 다음에 또 생기면 다시 알림
    if not (notify and info["outdated"]) or account_id in _warned or now - _first[account_id] < SETTLE_SEC:
        return
    _warned[account_id] = now
    try:
        from services.fcm_service import get_fcm_service
        num = account_number or account_id
        if info["dup"]:
            where = " · ".join(f"{'v' + x['version'] if x['version'] else '예전 것(버전 표시 없음)'} ← {x['ip'] or '?'}"
                               for x in info["sources"])
            title = "⚠️ 같은 계좌에서 EA 가 두 개 돌고 있습니다"
            body = (f"계좌 {num}: 새 EA v{info['latest']} 와 예전 EA 가 같이 서버에 물어보고 있습니다.\n{where}\n"
                    "MT5 를 재시작해도 이 알림이 오면, 다른 차트나 다른 PC 의 MT5 에 예전 tradeLock 이 붙어 있는 것입니다. "
                    "예전 EA 도 잠금 때 강제청산을 하니 그 차트에서 EA 를 빼 주세요 (IP 가 이 PC 와 같으면 같은 MT5 의 다른 차트).")
        else:
            title = "⚠️ EA 예전 버전 - MT5 재시작 필요"
            body = (f"계좌 {num}: MT5 에서 tradeLock EA {'v' + info['version'] if info['version'] else '(버전 표시 없는 예전 것)'} 가 돌고 있습니다."
                    f"\n새 EA v{info['latest']} 는 설치돼 있으니 MT5 를 재시작하면 바뀝니다 (관리 화면 계좌 탭 [MT5 재시작] 또는 MT5 를 직접 껐다 켜기).")
        get_fcm_service().send(title, body, '{"type":"ea_outdated"}',
                               buttons=[[{"text": "🔁 MT5 재시작", "callback_data": f"mt5:ask:{account_id}"}]])
    except Exception as e:
        print(f"[ea] 예전 EA 알림 실패: {e}")


def version_info(account_id: int) -> dict:
    """{"version": 마지막으로 받은 값 ('' = 예전 EA, None = 아직 요청 없음), "latest", "outdated",
        "dup": 새 EA 와 예전 EA 가 같이 돌고 있음, "sources": [{version, ip}] 최근 30초 안에 물어본 EA 들}"""
    latest = latest_version()
    act = _active(account_id)
    if act:
        versions = {v for v, _ in act}
        srcs = [{"version": v, "ip": ip} for (v, ip) in sorted(act, key=lambda k: -act[k])]
        old = bool(latest) and any(v != latest for v in versions)
        return {"version": _ver.get(account_id), "latest": latest, "outdated": old,
                "dup": old and latest in versions, "sources": srcs}
    v = _ver.get(account_id)
    return {"version": v, "latest": latest, "outdated": v is not None and bool(latest) and v != latest,
            "dup": False, "sources": []}


def state(account_id: int, server_started_at: float) -> dict:
    now = time.time()
    last = _seen.get(account_id)
    if last is not None and now - last <= FRESH_SEC:
        return {"state": "ok", "ago_sec": int(now - last), **version_info(account_id)}
    if now - server_started_at < GRACE_SEC:
        return {"state": "waiting", "ago_sec": None if last is None else int(now - last)}
    return {"state": "none", "ago_sec": None if last is None else int(now - last)}
