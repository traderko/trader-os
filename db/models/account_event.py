from sqlalchemy import Column, ForeignKey, Integer, String, Float, DateTime
from db.base import Base
from db.types import TZDateTime

class AccountEvent(Base):
    __tablename__ = "account_events"

    id = Column(Integer, primary_key=True, index=True)
    ticket = Column(Integer, unique=True, index=True)

    time = Column(TZDateTime(timezone=True), index=True)
    event_type = Column(String)  # trade / deposit / withdraw

    amount = Column(Float)
    balance = Column(Float)

    comment = Column(String)

    account_id = Column(Integer, ForeignKey("accounts.id"))