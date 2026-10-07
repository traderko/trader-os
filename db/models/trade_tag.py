from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import Column, DateTime, Enum, Integer, String
from sqlalchemy.orm import relationship
from db.base import Base
from mt5.mt5Client import Mt5Client

import enum
from db.types import TZDateTime

class TagType(str, enum.Enum):
    entry = "entry"
    exit = "exit"
    loss = "loss"

class TradeTag(Base):
    __tablename__ = "trade_tags"

    id = Column(Integer, primary_key=True)

    name = Column(String, unique=True)
    type = Column(
        Enum(TagType, name="tag_type"),
        nullable=False
    )

    created_at = Column(
        TZDateTime(timezone=True),
        default=datetime.now(Mt5Client.KOREA_TIMEZONE)
    )

    trades = relationship(
        "TradeTagMap",
        back_populates="tag",
        cascade="all, delete"
    )