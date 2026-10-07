# app/db/models/trade_note.py

from zoneinfo import ZoneInfo

from sqlalchemy import Column, Integer, String, Text, ForeignKey, DateTime
from sqlalchemy.orm import relationship
from datetime import datetime

from db.base import Base
from mt5.mt5Client import Mt5Client
from db.types import TZDateTime

class TradeNote(Base):
    __tablename__ = "trade_notes"

    id = Column(Integer, primary_key=True)

    trade_id = Column(Integer, ForeignKey("trades.id"), unique=True)

    time_frame = Column(Integer)
    entry_reason = Column(Text)
    exit_reason = Column(Text)
    loss_reason = Column(Text)
    memo = Column(Text)

    created_at = Column(TZDateTime(timezone=True), default=datetime.now(Mt5Client.KOREA_TIMEZONE))
    updated_at = Column(TZDateTime(timezone=True), default=datetime.now(Mt5Client.KOREA_TIMEZONE))

    trade = relationship("Trade", back_populates="note")