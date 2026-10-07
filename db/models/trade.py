from sqlalchemy import Column, ForeignKey, Integer, String, Float, DateTime
from sqlalchemy.orm import relationship
from db.base import Base
from db.types import TZDateTime

class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, index=True)
    ticket = Column(Integer, unique=True, index=True)
    symbol = Column(String, index=True)
    position = Column(String)
    volume = Column(Float)
    price_open = Column(Float)
    price_close = Column(Float)
    profit = Column(Float)
    open_time = Column(TZDateTime(timezone=True), index=True)
    close_time = Column(TZDateTime(timezone=True))
    commission = Column(Float)
    account_id = Column(Integer, ForeignKey("accounts.id"))

    account = relationship("Account", back_populates="trades")
    note = relationship("TradeNote", back_populates="trade", uselist=False, cascade="all, delete-orphan")
    images = relationship("TradeImage", back_populates="trade", cascade="all, delete-orphan")

    tags = relationship(
        "TradeTagMap",
        back_populates="trade",
        cascade="all, delete"
    )