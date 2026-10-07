# api/worker/worker_chart.py  (trade_lock_worker.py가 include_router 하는 파일)
#
# 실시간 차트(live.html)용 캔들 조회. 이 워커가 이미 붙어 있는 MT5 연결을 그대로 쓴다
# (Mt5Client.rates()처럼 메인 계좌로 재로그인하지 않음).
#
# 시간 처리
#   - MT5는 봉 시간을 "브로커 서버 시각을 UTC인 척한 epoch"로 준다.
#   - 요청 범위(from_ts/to_ts, 실제 UTC)는 브로커 시각으로 옮겨서 copy_rates_range에 넘기고,
#     응답 봉 시간은 다시 실제 UTC로 되돌린 뒤 time(UTC epoch 초) / time_kst(ISO, +09:00)로 준다.
#   - 오프셋은 Mt5Client._get_broker_offset(미국 서머타임 규칙)을 그대로 재사용한다.

import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request

from mt5.mt5Client import Mt5Client

router = APIRouter()

KST = ZoneInfo("Asia/Seoul")

# live.html은 M1/M5/M10/H1/H4/D 를 보냄. 기존 Mt5Client 키(1m, 5m, ...)도 같이 받아준다.
_TF_NAMES = {
    "M1": "TIMEFRAME_M1", "M2": "TIMEFRAME_M2", "M3": "TIMEFRAME_M3", "M5": "TIMEFRAME_M5",
    "M10": "TIMEFRAME_M10", "M15": "TIMEFRAME_M15", "M30": "TIMEFRAME_M30",
    "H1": "TIMEFRAME_H1", "H2": "TIMEFRAME_H2", "H4": "TIMEFRAME_H4",
    "D": "TIMEFRAME_D1", "D1": "TIMEFRAME_D1", "W": "TIMEFRAME_W1", "W1": "TIMEFRAME_W1",
    "MN": "TIMEFRAME_MN1", "MN1": "TIMEFRAME_MN1",
    "1m": "TIMEFRAME_M1", "2m": "TIMEFRAME_M2", "5m": "TIMEFRAME_M5", "10m": "TIMEFRAME_M10",
    "15m": "TIMEFRAME_M15", "30m": "TIMEFRAME_M30", "1h": "TIMEFRAME_H1", "2h": "TIMEFRAME_H2",
    "4h": "TIMEFRAME_H4", "1d": "TIMEFRAME_D1", "1w": "TIMEFRAME_W1", "1M": "TIMEFRAME_MN1",
}

MAX_BARS = 5000  # 실수로 몇 년치를 요청해도 응답이 폭주하지 않게


def _parse_ts(s: str, is_kst: bool) -> datetime:
    """ISO 문자열 → aware UTC datetime. JS toISOString()의 'Z'도 처리.
    타임존이 없는 값은 is_kst에 따라 KST 또는 UTC로 간주."""
    try:
        dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, f"시간 형식 오류: {s}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST if is_kst else timezone.utc)
    return dt.astimezone(timezone.utc)


def _utc_to_broker(utc_dt: datetime) -> datetime:
    return utc_dt + timedelta(hours=Mt5Client._get_broker_offset(utc_dt))


def _broker_epoch_to_utc(t: int) -> datetime:
    # 봉 시간(브로커 시각)으로 오프셋을 구한다. 서머타임 전환 직후 몇 시간만 1시간 오차 가능.
    fake_utc = datetime.fromtimestamp(int(t), tz=timezone.utc)
    return fake_utc - timedelta(hours=Mt5Client._get_broker_offset(fake_utc))


def _fetch_rates(mt5, symbol: str, tf_const: int, from_utc: datetime, to_utc: datetime):
    if not mt5.symbol_select(symbol, True):
        raise HTTPException(404, f"심볼을 찾을 수 없음: {symbol} ({mt5.last_error()})")

    rates = mt5.copy_rates_range(symbol, tf_const, _utc_to_broker(from_utc), _utc_to_broker(to_utc))
    if rates is None:
        raise HTTPException(502, f"copy_rates_range 실패: {mt5.last_error()}")

    out = []
    for r in rates[-MAX_BARS:]:
        utc_dt = _broker_epoch_to_utc(r["time"])
        out.append({
            "time": int(utc_dt.timestamp()),
            "time_kst": utc_dt.astimezone(KST).isoformat(),
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
            "tick_volume": int(r["tick_volume"]),
        })
    return out


@router.get("/rates")
async def get_rates(
    request: Request,
    symbol: str,
    timeframe: str,
    from_ts: str,
    to_ts: str,
    is_kst: bool = False,
):
    mt5 = getattr(request.app.state, "mt5", None)
    if mt5 is None:
        raise HTTPException(503, "MT5 연결 안 됨")

    tf_name = _TF_NAMES.get(timeframe) or _TF_NAMES.get(timeframe.upper())
    if tf_name is None:
        raise HTTPException(400, f"지원하지 않는 timeframe: {timeframe}")
    tf_const = getattr(mt5, tf_name)

    from_utc = _parse_ts(from_ts, is_kst)
    to_utc = _parse_ts(to_ts, is_kst)
    if from_utc >= to_utc:
        raise HTTPException(400, "from_ts가 to_ts보다 늦습니다")

    return await asyncio.to_thread(_fetch_rates, mt5, symbol, tf_const, from_utc, to_utc)
