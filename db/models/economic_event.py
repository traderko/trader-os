# db/models/economic_event.py
#
# 고영향(impact=3) USD 경제지표 일정. EconomicEventService가 10분마다 받아와 upsert한다.
# 거래 잠금(아시아장 집중 구간)에서 "오늘 거래일에 지표가 있는지" 판단할 때 쓴다.
# 메모리가 아니라 DB에 두는 이유: 서버 재시작 직후에도, 그리고 계좌별 워커 프로세스에서도
# 같은 일정을 바로 볼 수 있어야 해서.

from sqlalchemy import Column, Date, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.sql import func

from db.base import Base
from db.types import TZDateTime


class EconomicEvent(Base):
    __tablename__ = "economic_events"
    __table_args__ = (UniqueConstraint("event_name", "event_time", name="uq_economic_event_name_time"),)

    id = Column(Integer, primary_key=True)
    event_name = Column(String, nullable=False)
    currency = Column(String, nullable=False)
    impact = Column(Integer, nullable=False)

    event_time = Column(TZDateTime(timezone=True), nullable=False)  # 실제 발표 시각 (UTC 저장)

    # 이 지표가 속한 거래일 = 브로커 서버시간 기준 날짜.
    # 예) 목 21:30 KST 실업수당 → 목요일, 목 03:00 KST FOMC → 수요일
    broker_date = Column(Date, nullable=False, index=True)

    created_at = Column(DateTime, server_default=func.now(), nullable=False)
