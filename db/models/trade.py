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
    magic = Column(Integer, nullable=True)   # MT5 매직넘버 - 71700000~71799999 는 구간매매 (services/grid.py), 없으면 직접 거래
    group_id = Column(Integer, nullable=True, index=True)   # 직접 지정한 묶음 (웹 거래내역 [📦 묶기]) - 같은 번호끼리 한 묶음, 근거를 같이 씀
    account_id = Column(Integer, ForeignKey("accounts.id"))

    account = relationship("Account", back_populates="trades")
    note = relationship("TradeNote", back_populates="trade", uselist=False, cascade="all, delete-orphan")
    images = relationship("TradeImage", back_populates="trade", cascade="all, delete-orphan")

    tags = relationship(
        "TradeTagMap",
        back_populates="trade",
        cascade="all, delete"
    )