from sqlalchemy import Column, ForeignKey, Integer, String
from sqlalchemy.orm import relationship
from db.base import Base

class TradeTagMap(Base):
    __tablename__ = "trade_tag_map"

    id = Column(Integer, primary_key=True)
    
    trade_id = Column(Integer, ForeignKey("trades.id"))
    tag_id = Column(Integer, ForeignKey("trade_tags.id"))

    trade = relationship("Trade", back_populates="tags")
    tag = relationship("TradeTag", back_populates="trades")