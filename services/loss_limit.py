# services/loss_limit.py
#
# 일일 손실 한도 - 거래일 시작(한국 시간 06:00) 평가금 대비 손실률로 단계별 조치.
#   1) warn  : 텔레그램 경고
#   2) block : 새 진입 차단 - 다음 거래일 06:00까지 '풀 수 없는 잠금'(수동 잠금과 같은 방식)
#              EA는 잠금 순간의 포지션은 두고, 그 뒤에 들어간 포지션만 바로 청산함
#   3) close : 모든 포지션 청산 + 같은 잠금
# 설정: 관리 화면 '잠금 규칙' 탭 (data/settings.json lock.daily_loss_*)
#
# 감시는 계좌별 워커(trade_lock_worker.py)가 2초마다 한다. 이 파일은 워커·메인 서버가 같이 쓰는 도우미.
#
# 기준 평가금: 그 거래일에 워커가 처음 본 평가금 (06:00에 서버가 켜져 있었으면 06:00 평가금).
#   그날 입금·출금은 기준에 더하고 뺀다. 상태는 data/loss_limit/<계좌id>.json 에 저장 (서버를 재시작해도 이어짐)

import json
import os
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from services.settings_service import DATA_DIR, lock_cfg

KST = ZoneInfo("Asia/Seoul")
DAY_START = time(6, 0)
STATE_DIR = os.path.join(DATA_DIR, "loss_limit")
LOSS_LIMIT_MAGIC = 990098      # 손실 한도로 청산한 주문 표시 (잠금 강제청산은 990099)

STAGES = ("warn", "block", "close")


def cfg() -> dict:
    c = lock_cfg()
    return {
        "enabled": bool(c.get("daily_loss_enabled")),
        "warn": c.get("daily_loss_warn_pct") or 0,
        "block": c.get("daily_loss_block_pct") or 0,
        "close": c.get("daily_loss_close_pct") or 0,
    }


def trading_day(now: datetime) -> str:
    return (now.astimezone(KST) - timedelta(hours=DAY_START.hour, minutes=DAY_START.minute)).date().isoformat()


def day_start(now: datetime) -> datetime:
    d = datetime.fromisoformat(trading_day(now)).date()
    return datetime.combine(d, DAY_START, KST)


def next_day_start(now: datetime) -> datetime:
    return day_start(now) + timedelta(days=1)


def stage_for(pct: float, c: dict) -> int:
    """손실률이 넘은 가장 높은 단계 (0: 없음, 1: 경고, 2: 차단, 3: 청산)"""
    level = 0
    for i, key in enumerate(STAGES, start=1):
        if c[key] > 0 and pct >= c[key]:
            level = i
    return level


def _path(account_id: int) -> str:
    return os.path.join(STATE_DIR, f"{account_id}.json")


def read_state(account_id: int) -> dict:
    try:
        with open(_path(account_id), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_state(account_id: int, state: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = _path(account_id) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, _path(account_id))


def public_info(account_id: int) -> dict | None:
    """잠금 상태(/lock/status)에 붙여 보여줄 오늘 손실 정보. 오늘 기록이 없으면 None."""
    c = cfg()
    st = read_state(account_id)
    if st.get("day") != trading_day(datetime.now(KST)):
        return None
    return {
        "enabled": c["enabled"], "pct": st.get("pct", 0.0), "stage": st.get("stage", 0),
        "warn": c["warn"], "block": c["block"], "close": c["close"],
        "base": st.get("base"), "equity": st.get("equity"),
    }


def locked_by_limit(account_id: int, manual_until: datetime | None) -> bool:
    """지금 걸린 수동 잠금이 손실 한도 때문인지 (오늘 차단 단계 이상 + 잠금 끝이 다음 거래일 시작)"""
    if manual_until is None:
        return False
    st = read_state(account_id)
    now = datetime.now(KST)
    if st.get("day") != trading_day(now) or st.get("stage", 0) < 2:
        return False
    until = manual_until if manual_until.tzinfo else manual_until.replace(tzinfo=KST)
    return abs((until - next_day_start(now)).total_seconds()) < 120
