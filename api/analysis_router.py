# api/analysis_router.py
#
# 거래 분석 (웹 화면 '분석' 탭) - 계산은 services/analysis.py
#   GET /analysis/{account_number}?from_ts=&to_ts=&tf=5m
#     from_ts · to_ts : epoch 초 (진입 시각 기준으로 거래를 고름)
#     tf              : KTR 시간봉 (2m · 5m · 10m · 1h · 2h)
#   처음엔 1분봉을 MT5 에서 받느라 몇 분 걸릴 수 있음 → 뒤에서 계산하고 바로 돌려줌:
#     계산 중이면 {"pending": true, "progress": "…"} - 웹이 몇 초 뒤 다시 물어봄 (nginx 60초 제한에 안 걸리게)
#     끝나면 결과 (10분 동안 기억 - 같은 조건이면 다시 계산하지 않음. MT5 에 못 붙은 결과는 기억하지 않음)
#     refresh=true : 기억한 결과를 버리고 다시 계산 (웹 '다시 계산' 버튼)
#   받은 봉은 data/analysis.db 에 저장 - 다음부터 빠름

import asyncio
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.trade import Trade
from db.session import get_db
from services.account import get_account_or_default

router = APIRouter(prefix="/analysis", tags=["analysis"])
WAIT_SEC = 15          # 한 요청에서 기다리는 시간 (그 안에 끝나면 바로 결과)
KEEP_SEC = 600         # 끝난 결과를 기억하는 시간
_jobs: dict[tuple, dict] = {}     # 조건 → {"task", "progress", "done_at"}


async def _run(job: dict, account_id: int, from_ts: int, to_ts: int, tf: str):
    from services import analysis
    from db.session import AsyncSessionLocal
    t0, t1 = datetime.fromtimestamp(from_ts, timezone.utc), datetime.fromtimestamp(to_ts, timezone.utc)
    job["progress"] = "거래 불러오는 중"
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Trade).where(Trade.account_id == account_id, Trade.open_time >= t0, Trade.open_time < t1,
                                Trade.close_time != None).order_by(Trade.open_time)
        )).scalars().all()
    try:
        return await analysis.analyze(account_id, list(rows), tf, lambda m: job.__setitem__("progress", m))
    finally:
        job["done_at"] = time.time()


@router.get("/{account_number}")
async def get_analysis(account_number: int, from_ts: int, to_ts: int, tf: str = "5m", refresh: bool = False,
                       db: AsyncSession = Depends(get_db)):
    account = await get_account_or_default(db, account_number)
    if account is None:
        raise HTTPException(404, f"account not found: {account_number}")
    now = time.time()
    for k in [k for k, j in _jobs.items() if j.get("done_at") and now - j["done_at"] > KEEP_SEC]:
        _jobs.pop(k, None)
    key = (account.id, from_ts, to_ts, tf)
    job = _jobs.get(key)
    if job is not None and job["task"].done():
        failed = job["task"].exception() is not None or not (job["task"].result() or {}).get("mt5_ok", True)
        if failed or refresh:
            job = None                    # MT5 에 못 붙었던 결과 · '다시 계산' → 새로
    if job is None:
        job = {"progress": "시작", "done_at": None}
        job["task"] = asyncio.create_task(_run(job, account.id, from_ts, to_ts, tf))
        _jobs[key] = job
    try:
        return await asyncio.wait_for(asyncio.shield(job["task"]), WAIT_SEC)
    except asyncio.TimeoutError:
        return {"pending": True, "progress": job.get("progress")}
