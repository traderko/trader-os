# api/analysis_router.py
#
# 거래 분석 (웹 화면 '분석' 탭) - 계산은 services/analysis.py
#   GET /analysis/{account_number}?from_ts=&to_ts=&tf=5m
#     from_ts · to_ts : epoch 초 (진입 시각 기준으로 거래를 고름)
#     tf              : KTR 시간봉 (2m · 5m · 10m · 1h · 2h)
#   처음엔 1분봉을 MT5 에서 받느라 오래 걸릴 수 있음 (받은 봉은 data/analysis.db 에 저장 - 다음부터 빠름)

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from services.account import get_account_or_default
from db.models.trade import Trade
from db.session import get_db

router = APIRouter(prefix="/analysis", tags=["analysis"])
_running: dict[tuple, asyncio.Task] = {}


@router.get("/{account_number}")
async def get_analysis(account_number: int, from_ts: int, to_ts: int, tf: str = "5m",
                       db: AsyncSession = Depends(get_db)):
    from services import analysis
    account = await get_account_or_default(db, account_number)
    if account is None:
        raise HTTPException(404, f"account not found: {account_number}")
    t0, t1 = datetime.fromtimestamp(from_ts, timezone.utc), datetime.fromtimestamp(to_ts, timezone.utc)
    rows = (await db.execute(
        select(Trade).where(Trade.account_id == account.id, Trade.open_time >= t0, Trade.open_time < t1,
                            Trade.close_time != None).order_by(Trade.open_time)
    )).scalars().all()
    # 같은 요청이 겹치면 (새로고침 연타 등) 하나만 계산
    key = (account.id, from_ts, to_ts, tf)
    task = _running.get(key)
    if task is None or task.done():
        task = asyncio.create_task(analysis.analyze(account.id, list(rows), tf))
        _running[key] = task
    try:
        return await asyncio.shield(task)
    finally:
        if task.done():
            _running.pop(key, None)
