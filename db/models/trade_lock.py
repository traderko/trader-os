# db/models/trade_lock.py
from sqlalchemy import false, true, Column, Integer, String, DateTime, Boolean, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from db.base import Base

class TradeLock(Base):
    __tablename__ = "trade_locks"

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), unique=True, nullable=False)

    unlocked_until = Column(DateTime, nullable=True)
    active_reason = Column(String, nullable=True)

    # trade_lock_worker.py가 지속 연결을 유지하며 갱신하는 필드.
    # True면 연속손절 임계치 충족 상태, 승리 거래로 스트릭이 끊기면 다시 False.
    consec_loss_active = Column(Boolean, default=False, server_default=false(), nullable=False)

    # consec_loss_active가 False->True로 바뀐 "그 순간"(KST). EA가 이 시점 이후에
    # 열린 포지션만 청산 대상으로 삼을 수 있게 해주는 정확한 기준시각.
    consec_loss_since = Column(DateTime, nullable=True)

    # 사용자가 스스로 건 수동 락 - 확인 문구로도 못 풀림, 시간 지나야만 자동 해제됨.
    # (다른 사유와 달리 "무력화 불가능한 자기구속"이 목적이라 확인 로직 자체를 안 태움)
    manual_lock_since = Column(DateTime, nullable=True)
    manual_lock_until = Column(DateTime, nullable=True)

    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    account = relationship("Account", backref="trade_lock")