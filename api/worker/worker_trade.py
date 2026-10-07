# api/worker/worker_trade.py  (trade_lock_worker.py가 include_router 하는 파일)
#
# 이 계좌의 MT5 히스토리에서 거래를 수집하는 엔드포인트.
# DB 저장(upsert)은 여기서 안 하고 원시 trades 리스트만 반환 - 저장은
# main.py의 /trades/collect가 받아서 처리 (DB 쓰기는 게이트웨이 쪽 책임 유지).

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from fastapi import APIRouter, HTTPException, Request

from services.trade_lock_service import KST

router = APIRouter()

def _require_connection(request: Request):
    state = request.app.state
    if getattr(state, "mt5_client", None) is None or getattr(state, "account_row", None) is None:
        raise HTTPException(503, "MT5 연결 안 됨")
    return state


@router.get("/collect")
async def collect_trades(
    request: Request,
    start_year: int = 2025,
    start_month: int = 1,
    start_day: int = 1,
    end_year: int = 2027,
    end_month: int = 1,
    end_day: int = 1,
):
    state = _require_connection(request)
    from_dt = datetime(start_year, start_month, start_day, tzinfo=KST)
    to_dt = datetime(end_year, end_month, end_day, tzinfo=KST)

    trades = await asyncio.to_thread(
        state.mt5_client.collect_trade, state.account_row, from_dt, to_dt
    )
    return trades