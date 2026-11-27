"""증거금 사용률 잠금 - 사용률이 한도(기본 20%)를 넘으면 추가 진입을 막고, 내려가면 저절로 풂

동작 (계좌별 워커 trade_lock_worker.py 가 2초마다 확인)
  - 사용률 ≥ 한도가 CONFIRM_SEC 동안 이어지면 잠금. 수동 잠금과 같은 칸을 씀
    → 잠근 순간 들고 있던 포지션은 그대로, 그 뒤에 새로 들어간 포지션은 TradeLock EA 가 바로 청산 (기존 잠금 동작 그대로)
    → 대기 주문(리밋 · 스탑)도 취소 (체결되면 증거금이 더 늘어나므로)
  - 잠금은 HOLD_MIN 분 뒤까지로 걸고, 넘어 있는 동안 계속 늘림 (워커가 꺼져도 오래 갇히지 않게)
  - 사용률 < 한도 - REARM_PP 가 CONFIRM_SEC 동안 이어지면 이 잠금만 풂 (사람이 건 수동 잠금 · 손실 한도 잠금은 건드리지 않음)
  - 이미 더 늦게까지 걸린 잠금(수동 · 손실 한도)이 있으면 그대로 둠
기본: 꺼짐 (켜는 사람만). 설정 settings "margin_lock" {enabled, pct}
텔레그램: /증거금 잠금 켜기 | 끄기 | 20
상태: data/margin_lock/<계좌id>.json {"locked": bool, "until": iso, "since": iso}
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
CONFIRM_SEC = 4
REARM_PP = 2.0
HOLD_MIN = 30
DEFAULTS = {"enabled": False, "pct": 20.0}


def cfg() -> dict:
    from services.settings_service import get_settings
    raw = get_settings().get("margin_lock") or {}
    try:
        pct = min(100.0, max(1.0, float(raw.get("pct", DEFAULTS["pct"]))))
    except (TypeError, ValueError):
        pct = DEFAULTS["pct"]
    return {"enabled": bool(raw.get("enabled", DEFAULTS["enabled"])), "pct": pct}


def save_cfg(patch: dict) -> dict:
    from services.settings_service import get_settings, update_settings
    cur = dict(get_settings().get("margin_lock") or {})
    cur.update(patch)
    update_settings({"margin_lock": cur})
    return cfg()


def _dir() -> str:
    from services.settings_service import DATA_DIR
    return os.path.join(DATA_DIR, "margin_lock")


def read_state(account_id: int) -> dict:
    try:
        with open(os.path.join(_dir(), f"{account_id}.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_state(account_id: int, st: dict) -> None:
    os.makedirs(_dir(), exist_ok=True)
    p = os.path.join(_dir(), f"{account_id}.json")
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(p + ".tmp", p)


def _same(a: datetime | None, b: str | None) -> bool:
    if a is None or not b:
        return False
    a = a if a.tzinfo else a.replace(tzinfo=KST)
    return abs((a - datetime.fromisoformat(b)).total_seconds()) < 120


def locked_by_margin(account_id: int, manual_until: datetime | None) -> bool:
    """지금 걸린 수동 잠금이 이 증거금 잠금인지"""
    st = read_state(account_id)
    return bool(st.get("locked")) and _same(manual_until, st.get("until"))


async def hold(db, account_id: int) -> tuple[bool, datetime]:
    """잠금을 HOLD_MIN 분 뒤까지로 걸거나 늘림. (이 잠금이 걸려 있는지, until)
    더 늦게까지 걸린 다른 잠금이 있으면 건드리지 않고 (False, 그 시각)"""
    from services.trade_lock_service import _get_or_create_lock
    now = datetime.now(KST)
    until = now + timedelta(minutes=HOLD_MIN)
    lock = await _get_or_create_lock(db, account_id)
    cur = lock.manual_lock_until.replace(tzinfo=KST) if lock.manual_lock_until else None
    st = read_state(account_id)
    mine = bool(st.get("locked")) and _same(cur, st.get("until"))
    if cur is not None and cur > now and not mine and cur >= until:
        return False, cur                                  # 다른 잠금이 더 김
    if not mine:                                           # 처음 걸 때 - 더 짧은 다른 잠금이 있었으면 풀 때 되돌림
        st["prev_until"] = cur.isoformat() if cur is not None and cur > now else None
        st["prev_since"] = lock.manual_lock_since.isoformat() if st["prev_until"] and lock.manual_lock_since else None
    if cur is None or cur <= now:
        lock.manual_lock_since = now.replace(tzinfo=None)
    lock.manual_lock_until = until.replace(tzinfo=None)
    await db.commit()
    st.update(locked=True, until=until.isoformat(), since=st.get("since") if mine else now.isoformat())
    write_state(account_id, st)
    return True, until


async def release(db, account_id: int) -> bool:
    """이 증거금 잠금만 풂. 사람이 건 잠금 · 손실 한도 잠금이면 그대로 두고 False"""
    from services.trade_lock_service import _get_or_create_lock
    lock = await _get_or_create_lock(db, account_id)
    st = read_state(account_id)
    if not (st.get("locked") and _same(lock.manual_lock_until, st.get("until"))):
        return False
    prev = st.get("prev_until")
    if prev and datetime.fromisoformat(prev) > datetime.now(KST):   # 원래 걸려 있던 더 짧은 잠금은 그대로 남김
        lock.manual_lock_until = datetime.fromisoformat(prev).astimezone(KST).replace(tzinfo=None)
        if st.get("prev_since"):
            lock.manual_lock_since = datetime.fromisoformat(st["prev_since"]).replace(tzinfo=None)
    else:
        lock.manual_lock_until = None
        lock.manual_lock_since = None
    await db.commit()
    write_state(account_id, {"locked": False})
    return True
