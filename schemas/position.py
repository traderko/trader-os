from pydantic import BaseModel
from datetime import datetime

class PositionOut(BaseModel):
    ticket: int
    symbol: str
    volume: float
    type: int
    price_open: float
    price_current: float
    profit: float
    swap: float
    sl: float
    tp: float
    time: datetime
    comment: str
    contract_size: float | None = None   # 1랏 계약 크기 (손익 계산용)

    class Config:
        from_attributes = True