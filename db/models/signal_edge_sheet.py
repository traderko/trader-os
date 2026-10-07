# backend/models.py
import enum
from zoneinfo import ZoneInfo
from sqlalchemy import Index, String, Integer, Float, Text, DateTime, JSON, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime, timezone
from db.base import Base
from sqlalchemy import Enum as SAEnum

from mt5.mt5Client import Mt5Client
from db.types import TZDateTime

class AssetId(str, enum.Enum):
    GOLD = "GOLD"
    NASDAQ   = "NASDAQ"
    OIL  = "OIL"
 
class SignalEdgeSheet(Base):
    """
    더캔이지추격깨 1시간봉 기록 테이블

    더·캔·이·지·추·격·깨 7개 신호와 근거를 저장.
    signals / basis 는 JSON 컬럼으로 유연하게 저장.
    """
    __tablename__ = "signal_edge_sheets"

    # ── PK ──
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # ── 식별 키 ──
    asset_id: Mapped[AssetId]  = mapped_column(SAEnum(AssetId, name="asset_id_enum"))

    hour_at: Mapped[datetime] = mapped_column(TZDateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("asset_id", "hour_at", name="uq_asset_hour"),
    )

    # ── 가격 ──
    price:     Mapped[float] = mapped_column(Float)
    day_open:  Mapped[float] = mapped_column(Float)
    day_high:  Mapped[float] = mapped_column(Float)
    day_low:   Mapped[float] = mapped_column(Float)

    # ── 신호 (JSON) ──
    # {"bb":"BUY","can":"SELL","ma":"NEUTRAL","sr":"BUY","trend":"BUY","di":"NEUTRAL","brk":"BUY"}
    signals: Mapped[dict] = mapped_column(JSON)

    # ── 근거 (JSON) ──
    # {"bb":"1σ~2σ 상단 구간","can":"","ma":"골든크로스","sr":"","trend":"","di":"","brk":""}
    basis: Mapped[dict] = mapped_column(JSON, default=dict)

    # ── 메모 ──
    memo: Mapped[str] = mapped_column(Text, default="")

    # ── 스코어 (계산값 캐싱) ──
    bull: Mapped[int] = mapped_column(Integer, default=0)
    bear: Mapped[int] = mapped_column(Integer, default=0)

    # ── 타임스탬프 ──
    saved_at: Mapped[datetime] = mapped_column(
        TZDateTime(timezone=True), 
        default=lambda: datetime.now(Mt5Client.KOREA_TIMEZONE)
    )
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime(timezone=True),
        default=lambda: datetime.now(Mt5Client.KOREA_TIMEZONE), 
        onupdate=lambda: datetime.now(Mt5Client.KOREA_TIMEZONE), 
    )
    
    def __repr__(self):
        return f"<SignalEdgeSheet {self.asset_id} {self.hour_at} {self.bull}:{self.bear}>"
