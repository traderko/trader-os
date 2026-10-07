# api/rates.py
#
# GET /trading-api/rates  - 실시간 차트(live.html)가 쓰는 캔들 조회.
# account(계좌번호)를 주면 그 계좌 워커, 없으면 기본 계좌(is_default) 워커로 포워딩한다.
# 시세는 계좌와 무관하지만 심볼 이름이 브로커마다 달라서(XAUUSD / XAUUSD+)
# 웹뷰가 고른 브로커의 계좌로 보내는 게 맞다.

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import get_db
from services.account import get_account_or_default
from services.worker_client import worker_get

router = APIRouter(tags=["rates"])


@router.get("/rates")
async def get_rates(
    symbol: str = Query(..., description="브로커 심볼 (예: XAUUSD+, US100.b)"),
    timeframe: str = Query(..., description="M1, M5, M10, M15, M30, H1, H4, D ..."),
    from_ts: str = Query(..., description="ISO 시각 (예: 2026-09-28T08:00:00.000Z)"),
    to_ts: str = Query(...),
    is_kst: bool = False,
    account: Optional[int] = Query(None, description="계좌번호. 없으면 기본 계좌"),
    db: AsyncSession = Depends(get_db),
):
    try:
        acc = await get_account_or_default(db, account)
    except ValueError as e:
        raise HTTPException(404, str(e))

    return await worker_get(acc.id, "/rates", {
        "symbol": symbol,
        "timeframe": timeframe,
        "from_ts": from_ts,
        "to_ts": to_ts,
        "is_kst": str(is_kst).lower(),
    })
