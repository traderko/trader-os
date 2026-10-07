# db/models/trade_lock_phrase.py
from sqlalchemy import false, true, Column, Integer, String, DateTime, Boolean
from sqlalchemy.sql import func
from db.base import Base

class TradeLockPhrase(Base):
    __tablename__ = "trade_lock_phrases"

    id = Column(Integer, primary_key=True)
    phrase = Column(String, nullable=False, unique=True)
    is_active = Column(Boolean, default=True, server_default=false(), nullable=False)
    # checkin: 정기 재확인·연속손절 해제용 / session: 아시아장 집중 구간(목·금·지표일) 전용
    phrase_type = Column(String, default="checkin", server_default="checkin", nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)