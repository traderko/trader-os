from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import APIRouter, Depends
from sqlalchemy import delete, select
from db.models.trade_tag import TradeTag
from db.models.trade_tag_map import TradeTagMap
from db.session import get_db
from schemas.trade_tag import TagCreate, TradeTagUpdate

router = APIRouter(prefix="/trade-tags")

@router.get("")
async def get_tags(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(TradeTag))
    tags = result.scalars().all()
    return tags

@router.post("")
async def create_tag(data: TagCreate, db: AsyncSession = Depends(get_db)):
    tag = TradeTag(**data.model_dump())
    db.add(tag)
    await db.commit()
    await db.refresh(tag)
    return tag

@router.put("")
async def update_trade_tags(data: TradeTagUpdate, db: AsyncSession = Depends(get_db)):
    await db.execute(
        delete(TradeTagMap).where(TradeTagMap.trade_id == data.trade_id)
    )

    for tag_id in data.tag_ids:
        db.add(TradeTagMap(trade_id=data.trade_id, tag_id=tag_id))

    await db.commit()

    return {"ok": True}

@router.post("/apply")
async def apply_tags(data: dict, db: AsyncSession = Depends(get_db)):
    """여러 거래에 근거 태그를 더함 (이미 붙은 다른 태그는 그대로) - 웹 거래내역 선택 → [🏷 근거 달기]"""
    trade_ids = [int(x) for x in data.get("trade_ids") or []]
    tag_ids = [int(x) for x in data.get("tag_ids") or []]
    have = set((await db.execute(select(TradeTagMap.trade_id, TradeTagMap.tag_id).where(
        TradeTagMap.trade_id.in_(trade_ids), TradeTagMap.tag_id.in_(tag_ids)))).all())
    added = 0
    for t in trade_ids:
        for g in tag_ids:
            if (t, g) not in have:
                db.add(TradeTagMap(trade_id=t, tag_id=g))
                added += 1
    await db.commit()
    return {"ok": True, "added": added}

@router.put("/{tag_id}")
async def update_tag(tag_id: int, data: TagCreate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(TradeTag).where(TradeTag.id == tag_id)
    )
    tag = result.scalar_one()

    tag.name = data.name
    tag.type = data.type

    await db.commit()
    return tag

@router.delete("/{tag_id}")
async def delete_tag(tag_id: int, db: AsyncSession = Depends(get_db)):
    await db.execute(delete(TradeTagMap).where(TradeTagMap.tag_id == tag_id))   # 거래에 붙은 것도 같이 (남으면 빈 태그가 됨)
    await db.execute(delete(TradeTag).where(TradeTag.id == tag_id))
    await db.commit()
    return {"ok": True}
