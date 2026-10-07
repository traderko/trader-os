
from pydantic import BaseModel

class SetStopLossRequest(BaseModel):
    tickets: list[int]
    sl_price: float

class SetTakeProfitRequest(BaseModel):
    tickets: list[int]
    tp_price: float