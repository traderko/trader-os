from pydantic import BaseModel

class AccountCreate(BaseModel):
    account_number: str
    password: str
    broker_id: int

class AccountOut(BaseModel):
    account_number: str
    broker_id: int
    is_default: bool

    class Config:
        from_attributes = True