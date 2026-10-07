from pydantic import BaseModel
from db.models.trade_tag import TagType

class TagCreate(BaseModel):
    name: str
    type: TagType

class TradeTagUpdate(BaseModel):
    trade_id: int
    tag_ids: list[int]