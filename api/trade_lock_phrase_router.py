import random
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.trade_lock_phrase import TradeLockPhrase
from db.session import get_db

# checkin: 정기 재확인 해제용 (기본값)
# consec_loss: 연속손절 잠금 해제용 (하나도 없으면 checkin 문구를 대신 보여줌/받음)
# session: 집중 구간(아시아장·유로장 등) 공통 문구
# session:<구간id>: 그 구간 전용 문구 (예: session:asia)
# 앱은 /lock/status 의 phrase_type 과 session.ids 를 보고 골라 씀:
#   GET /trade-lock/phrases/random?type=session&session_ids=asia
PhraseType = Literal["checkin", "session", "consec_loss"]


def _types_for(type: str, session_ids: str | None) -> list[str]:
    if type == "consec_loss":
        return ["consec_loss", "checkin"]   # 아래에서 consec_loss가 없을 때만 checkin 사용
    if type != "session":
        return [type]
    ids = [x.strip() for x in (session_ids or "").split(",") if x.strip()]
    return ["session"] + [f"session:{i}" for i in ids]


def _prefer(phrases: list, type: str) -> list:
    """consec_loss 요청: 전용 문구가 있으면 그것만, 없으면 checkin 문구."""
    if type == "consec_loss":
        own = [p for p in phrases if p.phrase_type == "consec_loss"]
        return own or phrases
    return phrases


class PhraseCreate(BaseModel):
    phrase: str
    phrase_type: str = "checkin"   # checkin | consec_loss | session | session:<구간id>

class PhraseOut(BaseModel):
    id: int = -1
    phrase: str
    is_active: bool
    phrase_type: str = "checkin"

router = APIRouter(prefix="/trade-lock", tags=["trade_lock"])

# 전체 조회
@router.get("/phrases", response_model=list[PhraseOut])
async def list_phrases(
    type: PhraseType = Query("checkin", description="checkin | consec_loss | session"),
    session_ids: str | None = Query(None, description="type=session일 때 지금 걸린 구간 id들 (쉼표 구분)"),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(TradeLockPhrase)
        .where(TradeLockPhrase.is_active == True, TradeLockPhrase.phrase_type.in_(_types_for(type, session_ids)))
    )

    return _prefer(result.scalars().all(), type)

@router.get("/phrases/random", response_model=PhraseOut)
async def get_random_phrase(
    type: PhraseType = Query("checkin", description="checkin | consec_loss | session"),
    session_ids: str | None = Query(None, description="type=session일 때 지금 걸린 구간 id들 (쉼표 구분)"),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(TradeLockPhrase)
        .where(TradeLockPhrase.is_active == True, TradeLockPhrase.phrase_type.in_(_types_for(type, session_ids)))
    )
    phrases = _prefer(result.scalars().all(), type)
    if not phrases:
        raise HTTPException(status_code=404, detail=f"등록된 확인 문구가 없습니다. (type={type})")
    return random.choice(phrases)

# 추가
@router.post("/phrases", response_model=PhraseOut)
async def create_phrase(data: PhraseCreate, db: AsyncSession = Depends(get_db)):
    if not (data.phrase_type in ("checkin", "consec_loss", "session") or data.phrase_type.startswith("session:")):
        raise HTTPException(status_code=400, detail="phrase_type은 checkin, consec_loss, session, session:<구간id> 중 하나여야 합니다.")
    row = TradeLockPhrase(phrase=data.phrase, phrase_type=data.phrase_type)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row

# 삭제(비활성화)
@router.delete("/phrases/{phrase_id}")
async def deactivate_phrase(phrase_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(TradeLockPhrase).where(TradeLockPhrase.id == phrase_id))
    row = result.scalar_one_or_none()

    if row is not None:
        row.is_active = False
        await db.commit()
        return {"deactivated": True}
    else:
        return {"deactivated": False}