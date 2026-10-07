import json
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from mt5.mt5Client import Mt5Client
from schemas.position import PositionOut
from schemas.risk_summary import RiskSummary
from schemas.set_sl_tp import SetStopLossRequest, SetTakeProfitRequest
from services.crypto import CryptoService
from db.models.account import Account
from db.models.trade import Trade
from db.models.broker import Broker
from schemas.account import AccountCreate
from sqlalchemy.ext.asyncio import AsyncSession
from pywinauto import Application, Desktop, keyboard
from services.account import get_account_or_default, set_default_account
from schemas.account import AccountOut
from services.trade_lock_service import KST
from services.worker_client import worker_get, worker_post
from db.session import get_db

router = APIRouter(prefix="/accounts")

@router.post("")
async def create_account(account: AccountCreate, db: AsyncSession = Depends(get_db)):
    db_account = Account(
        account_number=account.account_number,
        password_encrypted=CryptoService().encrypt(account.password),
        broker_id=account.broker_id
    )

    db.add(db_account)
    
    await db.commit()

    return {"message": "account created"}

@router.get("")
async def get_accounts(
    db: AsyncSession = Depends(get_db)
):
    query = select(Account).options(selectinload(Account.broker))

    result = await db.execute(query)

    accounts = result.scalars().all()

    # 비밀번호(암호화된 값)는 내보내지 않음 - Tailscale로 폰에서도 이 API를 부르므로
    return [
        {
            "id": a.id,
            "account_number": a.account_number,
            "broker_id": a.broker_id,
            "is_default": a.is_default,
            "lock_enabled": a.lock_enabled,
            "broker": {"id": a.broker.id, "name": a.broker.name, "server": a.broker.server} if a.broker else None,
        }
        for a in accounts
    ]


# ── 월간·연간 리포트 (잔고·입출금은 MT5에서) ──
# MT5를 띄우지 않는 '조회 전용' 계좌(관리 화면에서 MT5 실행을 끈 계좌)도 손익 화면이 나오게:
#   1) 워커가 있으면 MT5에서 계산하고, 결과를 data/report_cache/ 에 저장
#   2) 워커가 없으면 예전에 저장한 결과 (source="cache")
#   3) 그것도 없으면 DB에 기록된 거래로만 계산 (source="trades" - 입출금·잔고 없음)
REPORT_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "report_cache")


def _cache_path(account_id: int, key: str) -> str:
    return os.path.join(REPORT_CACHE_DIR, str(account_id), f"{key}.json")


def _save_cache(account_id: int, key: str, data: dict) -> None:
    try:
        path = _cache_path(account_id, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({**data, "cached_at": datetime.now(KST).isoformat()}, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def _load_cache(account_id: int, key: str) -> dict | None:
    try:
        with open(_cache_path(account_id, key), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


async def _report_from_trades(db: AsyncSession, account_id: int, start: datetime, end: datetime) -> dict:
    rows = (await db.execute(
        select(Trade.profit, Trade.commission).where(
            Trade.account_id == account_id,
            Trade.close_time != None,
            Trade.close_time >= start,
            Trade.close_time < end,
        )
    )).all()
    return {
        "starting_balance": 0, "ending_balance": None,
        "deposit_total": 0, "withdrawal_total": 0,
        "pnl": sum(r.profit or 0 for r in rows), "commission": sum(r.commission or 0 for r in rows),
        "deposits": [], "withdrawals": [],
    }


async def _report(db: AsyncSession, account: Account, start: datetime, end: datetime, key: str) -> dict:
    try:
        starting = await worker_get(account.id, "/balance-at", {"before_time": start.isoformat()})
    except HTTPException as e:
        if e.status_code != 503:      # 503 = 워커 없음 (MT5를 안 띄운 계좌, 또는 켜는 중)
            raise
        cached = _load_cache(account.id, key)
        if cached:
            return {**cached, "source": "cache"}
        return {**(await _report_from_trades(db, account.id, start, end)), "source": "trades"}

    ending = await worker_get(account.id, "/balance-at", {"before_time": end.isoformat()})
    balances = await worker_get(account.id, "/balances", {
        "start_time": start.isoformat(), "end_time": end.isoformat()
    })
    pnl_data = await worker_get(account.id, "/pnl-commission", {
        "start_time": start.isoformat(), "end_time": end.isoformat()
    })

    starting_balance = starting["balance"]
    ending_balance = ending["balance"]
    deposits, withdrawals = balances["deposits"], balances["withdrawals"]
    deposit_total = sum(d["amount"] for d in deposits)
    withdrawal_total = sum(abs(w["amount"]) for w in withdrawals)
    pnl, commission = pnl_data["pnl"], pnl_data["commission"]

    calc_ending = starting_balance + deposit_total - withdrawal_total + pnl
    if abs(calc_ending - ending_balance) > 0.01:
        raise HTTPException(500, "⚠️ mismatch detected")

    data = {
        "starting_balance": starting_balance,
        "deposit_total": deposit_total, "withdrawal_total": withdrawal_total,
        "pnl": pnl, "commission": commission, "ending_balance": ending_balance,
        "deposits": deposits, "withdrawals": withdrawals,
    }
    _save_cache(account.id, key, data)
    return {**data, "source": "mt5"}


@router.get("/{account_number}/annual-report")
async def get_annual_report(
    account_number: int,
    year: int = 2026,
    db: AsyncSession = Depends(get_db)
):
    account = await get_account_or_default(db, account_number)
    if account is None:
        raise HTTPException(404, f"account not found: {account_number}")

    year_start = datetime(year, 1, 1, tzinfo=KST)
    year_end = datetime(year + 1, 1, 1, tzinfo=KST)
    return {"year": year, **(await _report(db, account, year_start, year_end, f"{year}"))}


@router.get("/{account_number}/monthly-report")
async def get_monthly_report(
    account_number: int,
    year: int = 2026,
    month: int = 1,
    db: AsyncSession = Depends(get_db)
):
    account = await get_account_or_default(db, account_number)
    if account is None:
        raise HTTPException(404, f"account not found: {account_number}")

    month_start = datetime(year, month, 1, tzinfo=KST)
    month_end = datetime(year + 1, 1, 1, tzinfo=KST) if month == 12 else datetime(year, month + 1, 1, tzinfo=KST)
    return {"year": year, "month": month,
            **(await _report(db, account, month_start, month_end, f"{year}-{month:02d}"))}


@router.get("/balance")
async def get_balance_at_endpoint(
    account_number: int,
    year: int = 2026,
    month: int = 1,
    db: AsyncSession = Depends(get_db)
):
    account = await get_account_or_default(db, account_number)
    if account is None:
        raise HTTPException(404, f"account not found: {account_number}")
 
    before_at = datetime(year + 1, 1, 1, tzinfo=KST) if month == 12 else datetime(year, month + 1, 1, tzinfo=KST)
    result = await worker_get(account.id, "/balance-at", {"before_time": before_at.isoformat()})
    return {"balance": result["balance"]}
 
 
@router.get("/balances")
async def get_balances_endpoint(
    account_number: int,
    year: int = 2026,
    month: int = 1,
    db: AsyncSession = Depends(get_db)
):
    account = await get_account_or_default(db, account_number)
    if account is None:
        raise HTTPException(404, f"account not found: {account_number}")
 
    month_start = datetime(year, month, 1, tzinfo=KST)
    month_end = datetime(year + 1, 1, 1, tzinfo=KST) if month == 12 else datetime(year, month + 1, 1, tzinfo=KST)
 
    return await worker_get(account.id, "/balances", {
        "start_time": month_start.isoformat(), "end_time": month_end.isoformat()
    })
 
 
@router.post("/{account_number}/positions/set-sl")
async def set_stop_loss(account_number: int, body: SetStopLossRequest, db: AsyncSession = Depends(get_db)):
    account = await get_account_or_default(db, account_number)
    if account is None:
        raise HTTPException(404, f"account not found: {account_number}")
    if not body.tickets:
        raise HTTPException(400, "대상 티켓이 없습니다.")
    return await worker_post(account.id, "/positions/set-sl", {"tickets": body.tickets, "sl_price": body.sl_price})
 
 
@router.post("/{account_number}/positions/set-tp")
async def set_take_profit(account_number: int, body: SetTakeProfitRequest, db: AsyncSession = Depends(get_db)):
    account = await get_account_or_default(db, account_number)
    if account is None:
        raise HTTPException(404, f"account not found: {account_number}")
    if not body.tickets:
        raise HTTPException(400, "대상 티켓이 없습니다.")
    return await worker_post(account.id, "/positions/set-tp", {"tickets": body.tickets, "tp_price": body.tp_price})
 
 
@router.get("/{account_number}/positions", response_model=list[PositionOut])
async def get_positions(
    account_number: int,
    symbol: str = Query(..., description="브로커 심볼 (예: XAUUSD, US100.b) 또는 종목 묶음 key (gold, nasdaq ... - services/symbols.py)"),
    db: AsyncSession = Depends(get_db)
):
    account = await get_account_or_default(db, account_number)
    if account is None:
        raise HTTPException(404, f"account not found: {account_number}")
 
    raw_positions = await worker_get(account.id, "/positions", params={"symbol": symbol})
 
    result = []
    for p in raw_positions:
        p = dict(p)
        # 워커가 raw unix timestamp로 주므로 여기서 UTC datetime으로 변환
        p["time"] = datetime.fromtimestamp(p["time"], tz=timezone.utc)
        result.append(p)
    return result
 
 
@router.get("/lots")
async def get_lots(
    account_number: int,
    symbol: str = "XAUUSD+",
    start_price: float = 0.0,
    end_price: float = 0.0,
    db: AsyncSession = Depends(get_db)
):
    account = await get_account_or_default(db, account_number)
    if account is None:
        raise HTTPException(404, f"account not found: {account_number}")
    result = await worker_get(account.id, "/lots", {
        "symbol": symbol, "start_price": start_price, "end_price": end_price
    })
    return result["total_lots"]
 
 
def _classify_risk(margin_utilization_pct: float) -> str:
    if margin_utilization_pct < 50:
        return "safe"
    elif margin_utilization_pct < 80:
        return "warning"
    return "danger"
 
 
@router.get("/{account_number}/risk-summary", response_model=RiskSummary)
async def get_risk_summary(account_number: int, db: AsyncSession = Depends(get_db)):
    account = await get_account_or_default(db, account_number)
    if account is None:
        raise HTTPException(404, f"account not found: {account_number}")
 
    info = await worker_get(account.id, "/account-info")
    balance, equity = info["balance"], info["equity"]
    margin, margin_free = info["margin"], info["margin_free"]
    nominal_leverage, profit = info["leverage"], info["profit"]
 
    margin_utilization_pct = (margin / equity * 100) if equity > 0 else 0.0
    margin_level_pct = info["margin_level"] if margin > 0 else None
 
    leverage_result = await worker_get(account.id, "/real-leverage", {"equity": equity})
    real_leverage = leverage_result["real_leverage"]
 
    return RiskSummary(
        balance=balance, equity=equity, margin=margin, margin_free=margin_free,
        margin_utilization_pct=round(margin_utilization_pct, 2),
        margin_level_pct=round(margin_level_pct, 2) if margin_level_pct is not None else None,
        real_leverage=round(real_leverage, 2), nominal_leverage=nominal_leverage,
        risk_level=_classify_risk(margin_utilization_pct), profit=profit,
    )

@router.get("/default", response_model=AccountOut)
async def get_default_account(db: AsyncSession = Depends(get_db)):
    return await get_account_or_default(db, account_number=None)
 
@router.patch("/{account_number}/default")
async def set_account_as_default(account_number: int, db: AsyncSession = Depends(get_db)):
    account = await set_default_account(db, account_number)
    return {"message": "default account set", "account_number": account.account_number}