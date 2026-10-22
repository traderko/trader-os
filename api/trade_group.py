# api/trade_group.py
#
# 거래 묶음 (웹 거래내역 ☑ 선택 → [📦 묶기]). trades.group_id 가 같은 거래끼리 한 묶음.
# 묶음에 근거를 달면 묶음 안 거래 전부에 같이 붙는다 (웹 묶음 창 · 텔레그램 근거 고르기).
#
#   POST   /trade-groups            {trade_ids}  묶기 - 고른 거래가 이미 다른 묶음에 있으면 그 묶음들까지 하나로 합침
#   POST   /trade-groups/remove     {trade_ids}  고른 거래만 묶음에서 빼기 (1건만 남은 묶음은 저절로 풀림)
#   DELETE /trade-groups/{group_id}              묶음 풀기
#   GET    /trade-groups/{group_id}              묶음 안 거래 번호

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.trade import Trade
from db.session import get_db

router = APIRouter(prefix="/trade-groups")


async def group_members(db: AsyncSession, trade_ids: list[int]) -> list[int]:
    """거래들 + 그 거래들이 속한 묶음의 다른 거래 (근거를 묶음 전체에 쓰기 위해 - 텔레그램도 씀)"""
    ids = [int(i) for i in trade_ids]
    gids = [g for g in (await db.execute(select(Trade.group_id).where(Trade.id.in_(ids)))).scalars().all() if g]
    if not gids:
        return ids
    more = (await db.execute(select(Trade.id).where(Trade.group_id.in_(gids)))).scalars().all()
    return sorted(set(ids) | set(more))


async def _tidy(db: AsyncSession, gids: set[int]) -> None:
    """1건만 남은 묶음은 풂"""
    for g in gids:
        n = (await db.execute(select(func.count()).select_from(Trade).where(Trade.group_id == g))).scalar()
        if n is not None and n < 2:
            await db.execute(update(Trade).where(Trade.group_id == g).values(group_id=None))


@router.post("")
async def make_group(data: dict, db: AsyncSession = Depends(get_db)):
    ids = sorted({int(i) for i in data.get("trade_ids") or []})
    if len(ids) < 2:
        raise HTTPException(400, "2건 이상 골라야 묶을 수 있습니다.")
    members = await group_members(db, ids)          # 이미 묶음에 있던 거래들까지 합침
    gid = min(members)                               # 묶음 번호 = 안에서 가장 작은 거래 번호 (따로 번호표 없이)
    old = {g for g in (await db.execute(select(Trade.group_id).where(Trade.id.in_(members)))).scalars().all() if g}
    await db.execute(update(Trade).where(Trade.id.in_(members)).values(group_id=gid))
    await db.commit()
    return {"ok": True, "group_id": gid, "trade_ids": members, "merged": len(old - {gid})}


@router.post("/remove")
async def remove_from_group(data: dict, db: AsyncSession = Depends(get_db)):
    ids = [int(i) for i in data.get("trade_ids") or []]
    gids = {g for g in (await db.execute(select(Trade.group_id).where(Trade.id.in_(ids)))).scalars().all() if g}
    await db.execute(update(Trade).where(Trade.id.in_(ids)).values(group_id=None))
    await _tidy(db, gids)
    await db.commit()
    return {"ok": True}


@router.get("/{group_id}")
async def get_group(group_id: int, db: AsyncSession = Depends(get_db)):
    ids = (await db.execute(select(Trade.id).where(Trade.group_id == group_id).order_by(Trade.open_time))).scalars().all()
    return {"group_id": group_id, "trade_ids": ids}


@router.delete("/{group_id}")
async def delete_group(group_id: int, db: AsyncSession = Depends(get_db)):
    r = await db.execute(update(Trade).where(Trade.group_id == group_id).values(group_id=None))
    await db.commit()
    return {"ok": True, "trades": r.rowcount}
