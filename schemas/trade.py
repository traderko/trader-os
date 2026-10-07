from pydantic import BaseModel
from datetime import datetime

class TradeCreate(BaseModel):
    ticket: int
    position: str
    symbol: str
    volume: float
    price: float
    profit: float
    entry_type: str
    time: int
    commission: float
    account_number: int
    broker_server: str
    magic: int = 0
    deal: int = 0          # MT5 체결(deal) 번호 - 같은 체결이 두 번 오면 한 번만 처리 (tradeLock EA가 보냄)

class TradeResponse(TradeCreate):
    id: int

    class Config:
        from_attributes = True