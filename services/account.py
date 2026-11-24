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
    await ensure_mt5_running(db, account)
    await db.refresh(account)

    return account


async def ensure_mt5_running(db: AsyncSession, account: Account) -> bool:
    """기본 계좌로 바꿀 때 그 계좌의 'MT5 실행(잠금)'이 꺼져 있으면 켜고 워커·MT5 를 띄움 (뒤에서, 관리 화면과 같은 방식).
    켜면 그 계좌에도 거래 잠금이 적용됨. 켰으면 True"""
    if account.lock_enabled:
        return False
    account.lock_enabled = True
    await db.commit()
    try:
        from api.admin_router import _run_in_background
        _run_in_background(account.id, start=True)
    except Exception as e:
        print(f"[account] {account.account_number} MT5 시작 실패: {e}")
    return True


class Mt5ToggleError(Exception):
    pass


async def set_mt5_running(db: AsyncSession, account: Account, on: bool) -> bool:
    """'MT5 실행(잠금)' 켜기 · 끄기 (텔레그램 /계좌 · 웹). 바뀌었으면 True
    끄기는 잠겨 있는 동안 막음 - 끄면 그 계좌 잠금이 멈춰서 잠금을 피해 가는 길이 되므로"""
    if on:
        return await ensure_mt5_running(db, account)
    if not account.lock_enabled:
        return False
    from services import trade_lock_service as lock_svc
    st = await lock_svc.get_lock_state(db, account.id)
    if st.get("locked"):
        raise Mt5ToggleError("잠겨 있는 동안에는 MT5 실행을 끌 수 없습니다 (끄면 잠금이 풀리는 것과 같아서). 잠금이 풀린 뒤에 끄세요.")
    account.lock_enabled = False
    await db.commit()
    try:
        from api.admin_router import _run_in_background
        _run_in_background(account.id, start=False)
    except Exception as e:
        print(f"[account] {account.account_number} MT5 중지 실패: {e}")
    return True
