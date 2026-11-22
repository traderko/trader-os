from sqlalchemy import Column, ForeignKey, Integer, String
from sqlalchemy.orm import relationship
from db.base import Base

class TradeTagMap(Base):
    __tablename__ = "trade_tag_map"

    id = Column(Integer, primary_key=True)
    
    trade_id = Column(Integer, ForeignKey("trades.id"))
    tag_id = Column(Integer, ForeignKey("trade_tags.id"))
    auto = Column(Integer, nullable=True)   # 1 = 진입할 때 차트로 추정해서 붙인 근거 (services/tag_guess.py), 직접 고른 건 비어 있음

    trade = relationship("Trade", back_populates="tags")
    tag = relationship("TradeTag", back_populates="trades")