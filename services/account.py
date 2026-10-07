# services/account.py
from typing import Optional
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from db.models.account import Account

async def get_account_or_default(
    db: AsyncSession,
    account_number: Optional[int] = None
) -> Account:
    query = select(Account).options(selectinload(Account.broker))

    if account_number is not None:
        query = query.where(Account.account_number == str(account_number))
    else:
        query = query.where(Account.is_default == True)

    result = await db.execute(query)
    account = result.scalar_one_or_none()

    if account is None:
        if account_number is not None:
            raise ValueError(f"account not found: {account_number}")
        raise ValueError("no default account set")

    return account

async def set_default_account(db: AsyncSession, account_number: int) -> Account:
    # 1. 기존 default 해제
    await db.execute(
        Account.__table__.update()
        .where(Account.is_default == True)
        .values(is_default=False)
    )
    await db.flush()

    # 2. 대상 계좌 조회 후 default로 설정
    result = await db.execute(
        select(Account).where(Account.account_number == str(account_number))
    )
    account = result.scalar_one_or_none()

    if account is None:
        raise ValueError(f"account not found: {account_number}")

    account.is_default = True
    await db.commit()
    await db.refresh(account)

    return account