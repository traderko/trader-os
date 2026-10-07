# app/schemas/trade_note.py

from pydantic import BaseModel
from typing import Optional

class TradeNoteUpdate(BaseModel):
    trade_id: int
    time_frame: int
    entry_reason: Optional[str] = None
    exit_reason: Optional[str] = None
    loss_reason: Optional[str] = None
    memo: Optional[str] = None