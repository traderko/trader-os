"""거래별 청산 직전 잔고(trades.balance_before) 미리 채우기 - 웹 거래내역 손익 % 를 바로 보여 주려고

언제: 서버가 켜지고 1분 뒤, 그 뒤 10분마다 (main.py lifespan). 웹이 빈 거래를 발견하면 바로 한 번 더 (/trades/balance-before)
무엇: 계좌마다 balance_before 가 빈 청산 거래를 찾아 MT5 로 한 번에 구해 저장
       → 웹 · 텔레그램은 저장된 값만 읽음 (MT5 를 기다리지 않음)
어떻게: 빈 거래 중 가장 이른 청산 1초 전 잔고(MT5 /balance-at)에서 시작해
       입출금 · 먼저 청산된 거래 손익(수수료 포함)을 청산 순서대로 더함.
       중간에 청산 때 저장된 정확한 값(스왑 포함)이 있으면 거기서 다시 맞춤 (오차가 쌓이지 않게)
MT5 가 안 붙은 계좌는 건너뜀 (다음 번에 다시)
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

FIRST_DELAY_SEC = 60
EVERY_SEC = 600
_locks: dict[int, asyncio.Lock] = {}


async def fill(account_id: int) -> int:
    """이 계좌의 빈 청산 직전 잔고를 채움. 채운 개수 (MT5 가 없거나 빈 게 없으면 0)"""
    lock = _locks.setdefault(account_id, asyncio.Lock())
    if lock.locked():                     # 이미 채우는 중이면 그게 끝나길 기다리기만
        async with lock:
            return 0
    async with lock:
        return await _fill(account_id)


async def _fill(account_id: int) -> int:
    from sqlalchemy import select, update
    from db.models.trade import Trade
    from db.session import AsyncSessionLocal
    from services.worker_client import worker_get
    from api.account_router import _parse_time, _drop_failed_withdrawals

    async with AsyncSessionLocal() as db:
        first = (await db.execute(
            select(Trade.close_time).where(Trade.account_id == account_id, Trade.close_time != None,
                                           Trade.balance_before == None)
            .order_by(Trade.close_time).limit(1))).scalar()
        if first is None:
            return 0
        rows = (await db.execute(
            select(Trade.id, Trade.close_time, Trade.profit, Trade.commission, Trade.balance_before)
            .where(Trade.account_id == account_id, Trade.close_time != None, Trade.close_time >= first)
            .order_by(Trade.close_time, Trade.ticket))).all()
        last = rows[-1].close_time
        start = first - timedelta(seconds=1)
        b0 = await asyncio.wait_for(worker_get(account_id, "/balance-at", {"before_time": start.isoformat()}), 20)
        fl = await asyncio.wait_for(worker_get(account_id, "/balances", {
            "start_time": start.isoformat(), "end_time": (last + timedelta(minutes=1)).isoformat()}), 20)
        deps, wds, _ = _drop_failed_withdrawals(fl.get("deposits") or [], fl.get("withdrawals") or [])
        ev = sorted([(t, abs(f.get("amount") or 0)) for f in deps if (t := _parse_time(f.get("time")))] +
                    [(t, -abs(f.get("amount") or 0)) for f in wds if (t := _parse_time(f.get("time")))])
        bal, fi, new = float(b0["balance"]), 0, {}
        for r in rows:
            while fi < len(ev) and ev[fi][0] <= r.close_time:
                bal += ev[fi][1]
                fi += 1
            if r.balance_before:
                bal = r.balance_before            # 청산 때 받은 정확한 값으로 다시 맞춤
            elif bal > 0:
                new[r.id] = round(bal, 2)
            bal += (r.profit or 0) + (r.commission or 0)
        for tid, v in new.items():
            await db.execute(update(Trade).where(Trade.id == tid, Trade.balance_before == None).values(balance_before=v))
        await db.commit()
    if new:
        print(f"[trade_balance] account={account_id} 청산 직전 잔고 {len(new)}건 채움")
    return len(new)


async def _account_ids() -> list[int]:
    from sqlalchemy import select
    from db.models.account import Account
    from db.session import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        return [a.id for a in (await db.execute(select(Account).order_by(Account.id))).scalars().all()]


async def loop() -> None:
    await asyncio.sleep(FIRST_DELAY_SEC)          # 워커 · MT5 가 뜰 시간
    while True:
        try:
            for aid in await _account_ids():
                try:
                    await fill(aid)
                except Exception as e:
                    detail = getattr(e, "detail", e)
                    if getattr(e, "status_code", None) != 503:      # 503 = MT5 를 안 띄운 계좌 - 조용히 건너뜀
                        print(f"[trade_balance] account={aid} 실패: {detail}")
        except Exception as e:
            print(f"[trade_balance] 확인 실패: {e}")
        await asyncio.sleep(EVERY_SEC)
