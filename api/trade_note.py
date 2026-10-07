# app/api/trade_note.py

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from db.session import get_db
from db.models.trade_note import TradeNote
from schemas.trade_note import TradeNoteUpdate

router = APIRouter(prefix="/trade-notes")

@router.put("")
async def upsert_note(data: TradeNoteUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(TradeNote).where(TradeNote.trade_id == data.trade_id)
    )
    note = result.scalar_one_or_none()

    if note:
        for key, value in data.model_dump(exclude_unset=True).items():
            setattr(note, key, value)
    else:
        note = TradeNote(**data.model_dump())

        db.add(note)

    await db.commit()
    await db.refresh(note)

    return note
