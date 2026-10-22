from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import APIRouter, Depends, HTTPException
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
    name = data.name.strip()
    if not name:
        raise HTTPException(400, "근거 이름을 넣으세요.")
    if (await db.execute(select(TradeTag.id).where(TradeTag.name == name))).first():
        raise HTTPException(400, f"'{name}' 근거가 이미 있습니다.")
    tag = TradeTag(name=name, type=data.type)
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
    tag = result.scalar_one_or_none()
    if tag is None:
        raise HTTPException(404, "근거를 찾지 못했습니다.")
    name = data.name.strip()
    if not name:
        raise HTTPException(400, "근거 이름을 넣으세요.")
    if (await db.execute(select(TradeTag.id).where(TradeTag.name == name, TradeTag.id != tag_id))).first():
        raise HTTPException(400, f"'{name}' 근거가 이미 있습니다.")
    # 이름을 바꾸면 이미 이 근거가 붙은 거래들에도 새 이름으로 보임 (거래에는 근거 번호로 연결)
    tag.name = name
    tag.type = data.type

    await db.commit()
    return tag

async def remove_tag(db: AsyncSession, tag_id: int, merge_into: int | None = None) -> int:
    """근거 지우기. merge_into 를 주면 이 근거가 붙은 거래들을 그 근거로 옮긴 뒤 지움 (이미 붙어 있으면 그대로).
    돌려주는 값: 옮긴(또는 빠진) 거래 수. 텔레그램 근거 관리(services/telegram_tags.py)도 이걸 씀"""
    trade_ids = list((await db.execute(select(TradeTagMap.trade_id).where(TradeTagMap.tag_id == tag_id))).scalars().all())
    if merge_into and merge_into != tag_id:
        if await db.get(TradeTag, merge_into) is None:
            raise HTTPException(404, "옮길 근거를 찾지 못했습니다.")
        have = set((await db.execute(select(TradeTagMap.trade_id).where(
            TradeTagMap.tag_id == merge_into, TradeTagMap.trade_id.in_(trade_ids)))).scalars().all())
        for t in set(trade_ids) - have:
            db.add(TradeTagMap(trade_id=t, tag_id=merge_into))
    await db.execute(delete(TradeTagMap).where(TradeTagMap.tag_id == tag_id))   # 거래에 붙은 것도 같이 (남으면 빈 태그가 됨)
    await db.execute(delete(TradeTag).where(TradeTag.id == tag_id))
    await db.commit()
    return len(set(trade_ids))


async def usage_counts(db: AsyncSession) -> dict[int, int]:
    from sqlalchemy import func
    rows = (await db.execute(select(TradeTagMap.tag_id, func.count(func.distinct(TradeTagMap.trade_id)))
                             .group_by(TradeTagMap.tag_id))).all()
    return {tid: n for tid, n in rows}


@router.get("/usage")
async def tag_usage(db: AsyncSession = Depends(get_db)):
    """근거마다 붙은 거래 수 {근거id: 건수} - 지우기 전에 보여 줌"""
    return {str(k): v for k, v in (await usage_counts(db)).items()}


@router.delete("/{tag_id}")
async def delete_tag(tag_id: int, merge_into: int | None = None, db: AsyncSession = Depends(get_db)):
    n = await remove_tag(db, tag_id, merge_into)
    return {"ok": True, "trades": n, "merged": bool(merge_into)}
