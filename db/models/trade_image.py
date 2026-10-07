# app/db/models/trade_image.py
from zoneinfo import ZoneInfo
from sqlalchemy import Column, Integer, String, ForeignKey, DateTime
from datetime import datetime
from sqlalchemy.orm import relationship
from db.base import Base
from mt5.mt5Client import Mt5Client
from db.types import TZDateTime

class TradeImage(Base):
    __tablename__ = "trade_images"

    id = Column(Integer, primary_key=True)
    trade_id = Column(Integer, ForeignKey("trades.id"))

    file_path = Column(String)
    created_at = Column(
        TZDateTime(timezone=True), 
        default=datetime.now(Mt5Client.KOREA_TIMEZONE)
    )

    trade = relationship("Trade", back_populates="images")