# app/db/models/account.py

from sqlalchemy import false, true, Boolean, Column, ForeignKey, Integer, String
from sqlalchemy.orm import relationship
from db.base import Base

class Account(Base):
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, index=True)

    account_number = Column(String, unique=True, index=True)
    password_encrypted = Column(String)
    broker_id = Column(ForeignKey("brokers.id")) 
    is_default = Column(Boolean, default=False, nullable=False, server_default=false())

    lock_enabled = Column(Boolean, default=False, nullable=False, server_default=true())

    broker = relationship("Broker", back_populates="accounts")
    trades = relationship("Trade", back_populates="account")