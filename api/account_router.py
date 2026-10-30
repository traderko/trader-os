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


def _parse_time(v) -> datetime | None:
    try:
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, KST)
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=KST)
    except (TypeError, ValueError):
        return None


def _drop_failed_withdrawals(deposits: list, withdrawals: list, window_days: int = 7) -> tuple[list, list, list]:
    """출금 실패: 브로커가 출금(-X)을 넣었다가 같은 금액 입금(+X)으로 되돌려 줌 → 둘 다 없던 일로 뺀다.
    출금 뒤 window_days 안에 들어온 같은 금액(0.01 차이까지) 입금 하나와 짝지음. 잔고에는 영향 없음 (둘이 상쇄)"""
    used_d, pairs = set(), []
    keep_w = []
    ds = sorted(enumerate(deposits), key=lambda x: _parse_time(x[1].get("time")) or datetime.max.replace(tzinfo=KST))
    for w in sorted(withdrawals, key=lambda x: _parse_time(x.get("time")) or datetime.min.replace(tzinfo=KST)):
        wt, wa = _parse_time(w.get("time")), abs(w.get("amount") or 0)
        hit = None
        for i, d in ds:
            if i in used_d or abs(abs(d.get("amount") or 0) - wa) > 0.01:
                continue
            dt_ = _parse_time(d.get("time"))
            if wt and dt_ and not (wt <= dt_ <= wt + timedelta(days=window_days)):
                continue
            hit = (i, d)
            break
        if hit:
            used_d.add(hit[0])
            pairs.append({"withdrawal": w, "refund": hit[1], "amount": wa})
        else:
            keep_w.append(w)
    keep_d = [d for i, d in enumerate(deposits) if i not in used_d]
    return keep_d, keep_w, pairs


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
    deposits, withdrawals, canceled = _drop_failed_withdrawals(balances["deposits"], balances["withdrawals"])
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
        "deposits": deposits, "withdrawals": withdrawals, "canceled_withdrawals": canceled,
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


@router.get("/{account_number}/withdraw-rule")
async def get_withdraw_rule(account_number: int, db: AsyncSession = Depends(get_db)):
    """일일 출금 규칙: 오늘(거래일, 한국 시간 06:00 시작) 순수익의 pct% 까지 뺄 수 있고, 오늘 이미 뺀 출금은 차감.
    오늘 순수익 = 지금 잔고 - 거래일 시작 잔고 - 오늘 입금 + 오늘 출금 (청산된 손익·수수료·스왑, 열린 포지션 평가손익은 빠짐).
    손실인 날은 0. 다음 거래일 06:00에 리셋"""
    from services.settings_service import get_settings
    from services.loss_limit import day_start, next_day_start, trading_day
    account = await get_account_or_default(db, account_number)
    if account is None:
        raise HTTPException(404, f"account not found: {account_number}")
    pct = float((get_settings().get("pnl") or {}).get("withdraw_pct", 50))

    now = datetime.now(KST)
    day, start, end = trading_day(now), day_start(now), next_day_start(now)
    source = "mt5"
    try:
        b0 = await worker_get(account.id, "/balance-at", {"before_time": start.isoformat()})
        b1 = await worker_get(account.id, "/balance-at", {"before_time": end.isoformat()})
        # 출금 실패(출금 -X 뒤 같은 금액 +X 되돌림)는 '이미 출금'에서 뺌. 어제 출금 → 오늘 되돌림도 있어 7일 앞부터 받아 짝지음
        flows = await worker_get(account.id, "/balances", {"start_time": (start - timedelta(days=7)).isoformat(), "end_time": end.isoformat()})
        today = lambda f: (lambda t: t is not None and start <= t < end)(_parse_time(f.get("time")))
        _, wds, canceled = _drop_failed_withdrawals(flows["deposits"], flows["withdrawals"])
        data = {
            "day": day, "start_balance": b0["balance"], "balance": b1["balance"],
            # 순수익 계산용 - 잔고 변화에서 그날 입출금을 전부(실패 포함) 빼야 거래 손익만 남음
            "deposit_total": sum(d["amount"] for d in flows["deposits"] if today(d)),
            "raw_withdrawal_total": sum(abs(w["amount"]) for w in flows["withdrawals"] if today(w)),
            # 규칙에서 차감할 '오늘 실제로 나간 출금'
            "withdrawal_total": sum(abs(w["amount"]) for w in wds if today(w)),
            "canceled_total": sum(c["amount"] for c in canceled if today(c["withdrawal"]) or today(c["refund"])),
        }
        data["net_profit"] = data["balance"] - data["start_balance"] - data["deposit_total"] + data["raw_withdrawal_total"]
        _save_cache(account.id, "withdraw-day", data)
    except HTTPException as e:
        if e.status_code != 503:          # 503 = 워커 없음 (MT5를 안 띄운 계좌)
            raise
        data = _load_cache(account.id, "withdraw-day")
        if data and data.get("day") == day:
            source = "cache"
        else:                             # 오늘 MT5를 한 번도 못 봄 → 저장된 거래로 손익만 (입출금은 모름)
            rows = (await db.execute(
                select(Trade.profit, Trade.commission).where(
                    Trade.account_id == account.id, Trade.close_time != None,
                    Trade.close_time >= start, Trade.close_time < end,
                )
            )).all()
            data = {"day": day, "balance": None, "start_balance": None, "deposit_total": 0, "withdrawal_total": 0,
                    "net_profit": sum((r.profit or 0) - abs(r.commission or 0) for r in rows)}
            source = "trades"

    net, wd = data["net_profit"], data["withdrawal_total"]
    allowed = max(0.0, net) * pct / 100            # 오늘 규칙상 뺄 수 있는 총액
    withdrawable = max(0.0, allowed - wd)
    if data.get("balance") is not None:
        withdrawable = min(withdrawable, max(0.0, data["balance"]))
    r2 = lambda v: None if v is None else round(v, 2)
    return {
        "pct": pct, "available": True, "source": source, "cached_at": data.get("cached_at"),
        "day": day, "reset_at": end.isoformat(),
        "start_balance": r2(data.get("start_balance")), "balance": r2(data.get("balance")),
        "deposit_total": r2(data["deposit_total"]), "withdrawal_total": r2(wd),
        "raw_withdrawal_total": r2(data.get("raw_withdrawal_total", wd)),
        "net_profit": r2(net), "allowed": r2(allowed), "withdrawable": r2(withdrawable),
        "canceled_total": r2(data.get("canceled_total") or 0),      # 오늘 걸친 출금 실패 (계산에서 뺌)
        "over": r2(max(0.0, wd - allowed)),          # 오늘 규칙보다 더 뺀 금액
    }


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
    from services.sltp import apply_sltp       # 많으면 나눠 보내고, 시간 초과 난 건 다시 읽어 확인
    return await apply_sltp(account.id, "sl", body.tickets, body.sl_price)
 
 
@router.post("/{account_number}/positions/set-tp")
async def set_take_profit(account_number: int, body: SetTakeProfitRequest, db: AsyncSession = Depends(get_db)):
    account = await get_account_or_default(db, account_number)
    if account is None:
        raise HTTPException(404, f"account not found: {account_number}")
    if not body.tickets:
        raise HTTPException(400, "대상 티켓이 없습니다.")
    from services.sltp import apply_sltp
    return await apply_sltp(account.id, "tp", body.tickets, body.tp_price)
 
 
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
 
 
@router.get("/{account_number}/simulate")
async def simulate_close(
    account_number: int,
    symbol: str = Query(..., description="브로커 심볼 또는 종목 묶음 key (gold, nasdaq ...)"),
    side: str = Query(..., description="BUY / SELL"),
    volume: float = Query(..., gt=0),
    price_open: float = Query(..., gt=0),
    price_close: float = Query(..., gt=0),
    hold_min: float | None = None,
    db: AsyncSession = Depends(get_db),
):
    """청산 시뮬레이션 - 텔레그램 청산 알림과 같은 모양 + 증거금 (services/simulate.py)"""
    from services.simulate import simulate
    account = await get_account_or_default(db, account_number)
    if account is None:
        raise HTTPException(404, f"account not found: {account_number}")
    return await simulate(db, account, symbol, side, volume, price_open, price_close, hold_min)


@router.post("/{account_number}/simulate-trades")
async def simulate_trades_api(account_number: int, body: dict, db: AsyncSession = Depends(get_db)):
    """거래내역에서 고른 거래들을 2·5·10배 랏으로 했다면 (services/simulate.py)"""
    from services.simulate import simulate_trades
    account = await get_account_or_default(db, account_number)
    if account is None:
        raise HTTPException(404, f"account not found: {account_number}")
    ids = [int(x) for x in (body.get("trade_ids") or [])][:2000]
    try:
        mults = [float(x) for x in (body.get("mults") or [2, 5, 10]) if 0 < float(x) <= 1000][:6]
    except (TypeError, ValueError):
        raise HTTPException(400, "배수는 숫자로 넣어 주세요.")
    if not ids:
        raise HTTPException(400, "거래를 하나 이상 고르세요.")
    return await simulate_trades(db, account, ids, mults)


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

@router.get("/{account_number}/liquidation")
async def get_liquidation(account_number: int, db: AsyncSession = Depends(get_db)):
    """종목별 강제청산(스탑아웃)까지 포인트·가격 (services/risk.py)"""
    account = await get_account_or_default(db, account_number)
    if account is None:
        raise HTTPException(404, f"account not found: {account_number}")
    from services.risk import liquidation, stop_out_room
    info = await worker_get(account.id, "/account-info")
    positions = await worker_get(account.id, "/positions-all")
    r = stop_out_room(info)
    return {"stop_out_pct": r[1] if r else None, "room": r[0] if r else None, "items": liquidation(info, positions)}


@router.get("/default", response_model=AccountOut)
async def get_default_account(db: AsyncSession = Depends(get_db)):
    return await get_account_or_default(db, account_number=None)
 
@router.patch("/{account_number}/default")
async def set_account_as_default(account_number: int, db: AsyncSession = Depends(get_db)):
    account = await set_default_account(db, account_number)
    return {"message": "default account set", "account_number": account.account_number}