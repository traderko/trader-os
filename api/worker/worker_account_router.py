# api/worker/worker_account.py  (trade_lock_worker.py가 include_router 하는 파일)
#
# 워커 프로세스 내부 전용 엔드포인트. 프리픽스 없음 - 이 계좌 하나만 담당하므로
# account_number를 URL로 안 받고, request.app.state에서 이미 연결된
# mt5/mt5_client/account_row를 바로 사용한다.
#
# trade_lock_worker.py(엔트리포인트)를 import하지 않는다 - 순환참조 방지.
# main.py의 account 라우터가 이 엔드포인트들을 httpx로 포워딩해서 호출한다.

import asyncio
from datetime import datetime
from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


def _require_connection(request: Request):
    state = request.app.state
    if getattr(state, "mt5_client", None) is None or getattr(state, "account_row", None) is None:
        raise HTTPException(503, "MT5 연결 안 됨")
    return state


@router.get("/positions")
async def get_positions(request: Request, symbol: str):
    state = _require_connection(request)
    return await asyncio.to_thread(state.mt5_client.get_positions, state.account_row, symbol)


@router.get("/positions-all")
async def get_all_positions(request: Request):
    """모든 종목의 포지션 (텔레그램 실시간 현황용). /positions 는 종목을 꼭 받아서 따로 둠."""
    state = _require_connection(request)
    positions = await asyncio.to_thread(state.mt5.positions_get)
    if positions is None:
        return []
    sizes, digits = {}, {}
    for p in positions:
        if p.symbol not in sizes:
            info = state.mt5.symbol_info(p.symbol)
            sizes[p.symbol] = getattr(info, "trade_contract_size", None) if info else None
            digits[p.symbol] = getattr(info, "digits", None) if info else None
    return [
        {
            "contract_size": sizes.get(p.symbol), "digits": digits.get(p.symbol),
            "ticket": p.ticket, "symbol": p.symbol, "volume": p.volume, "type": p.type,
            "price_open": p.price_open, "price_current": p.price_current, "profit": p.profit,
            "swap": p.swap, "sl": p.sl, "tp": p.tp, "time": p.time,
        }
        for p in positions
    ]


def _resolve_symbol(mt5, s: str) -> str | None:
    """브로커 심볼 그대로, 또는 종목 묶음 key(gold, nasdaq…)면 이 브로커의 실제 심볼로"""
    if s and mt5.symbol_info(s) is not None:
        return s
    from services import symbols
    g = symbols.group_by_key(s) or symbols.group_of(s)
    if not g:
        return None
    names = [symbols.norm(n) for n in g["names"]]
    cands = [x for x in (mt5.symbols_get() or []) if any(symbols.norm(x.name).startswith(n) for n in names)]
    # 시장 감시 창에 있는 것, 거래 가능한 것(trade_mode 4 = FULL), 이름이 짧은 것 순
    cands.sort(key=lambda x: (not getattr(x, "visible", False), getattr(x, "trade_mode", 4) != 4, len(x.name)))
    return cands[0].name if cands else None


@router.get("/calc")
async def calc_trade(request: Request, symbol: str, side: str, volume: float, price_open: float,
                     price_close: float | None = None):
    """MT5 로 직접 계산한 증거금·손익 (환율·계약 크기 포함) - 청산 시뮬레이션·계산기용"""
    state = _require_connection(request)
    mt5 = state.mt5

    def run():
        name = _resolve_symbol(mt5, symbol)
        if not name:
            raise HTTPException(404, f"종목을 찾지 못했습니다: {symbol}")
        mt5.symbol_select(name, True)
        info = mt5.symbol_info(name)
        t = mt5.ORDER_TYPE_BUY if side.upper().startswith("B") else mt5.ORDER_TYPE_SELL
        margin = mt5.order_calc_margin(t, name, volume, price_open)
        profit = mt5.order_calc_profit(t, name, volume, price_open, price_close) if price_close else None
        acc = mt5.account_info()
        tick = mt5.symbol_info_tick(name)
        return {
            "symbol": name, "digits": info.digits, "contract_size": info.trade_contract_size,
            "volume_min": info.volume_min, "volume_max": info.volume_max, "volume_step": info.volume_step,
            "margin": margin, "profit": profit,
            "bid": getattr(tick, "bid", None), "ask": getattr(tick, "ask", None),
            "balance": acc.balance if acc else None, "equity": acc.equity if acc else None,
            "margin_used": acc.margin if acc else None, "margin_free": acc.margin_free if acc else None,
            "leverage": acc.leverage if acc else None, "currency": acc.currency if acc else None,
        }

    return await asyncio.to_thread(run)


@router.get("/account-info")
async def get_account_info(request: Request):
    state = _require_connection(request)
    info = await asyncio.to_thread(state.mt5_client.account_info, state.account_row)
    if info is None:
        raise HTTPException(502, f"account_info 조회 실패: {state.mt5.last_error()}")
    return dict(info._asdict())


@router.post("/positions/set-sl")
async def set_stop_loss(request: Request, body: dict):
    state = _require_connection(request)
    tickets = body.get("tickets") or []
    sl_price = body.get("sl_price")
    if not tickets:
        raise HTTPException(400, "대상 티켓이 없습니다.")
    return await asyncio.to_thread(state.mt5_client.set_stop_loss, state.account_row, tickets, sl_price)

@router.post("/positions/set-tp")
async def set_take_profit(request: Request, body: dict):
    state = _require_connection(request)
    tickets = body.get("tickets") or []
    tp_price = body.get("tp_price")
    if not tickets:
        raise HTTPException(400, "대상 티켓이 없습니다.")
    return await asyncio.to_thread(state.mt5_client.set_take_profit, state.account_row, tickets, tp_price)


@router.get("/balance-at")
async def get_balance_at(request: Request, before_time: str):
    state = _require_connection(request)
    before_dt = datetime.fromisoformat(before_time)
    balance = await asyncio.to_thread(state.mt5_client.get_balance_at, state.account_row, before_dt)
    return {"balance": balance}


@router.get("/balances")
async def get_balances(request: Request, start_time: str, end_time: str):
    state = _require_connection(request)
    start_dt = datetime.fromisoformat(start_time)
    end_dt = datetime.fromisoformat(end_time)
    deposits, withdrawals = await asyncio.to_thread(state.mt5_client.get_balances, state.account_row, start_dt, end_dt)
    return {"deposits": deposits, "withdrawals": withdrawals}


@router.get("/pnl-commission")
async def get_pnl_commission(request: Request, start_time: str, end_time: str):
    state = _require_connection(request)
    start_dt = datetime.fromisoformat(start_time)
    end_dt = datetime.fromisoformat(end_time)
    pnl, commission = await asyncio.to_thread(state.mt5_client.get_trade_pnl_commission, state.account_row, start_dt, end_dt)
    return {"pnl": pnl, "commission": commission}


@router.get("/real-leverage")
async def get_real_leverage(request: Request, equity: float):
    state = _require_connection(request)
    real_leverage = await asyncio.to_thread(state.mt5_client.get_real_leverage, state.account_row, equity)
    return {"real_leverage": real_leverage}


@router.get("/lots")
async def get_lots(request: Request, symbol: str, start_price: float = 0.0, end_price: float = 0.0):
    state = _require_connection(request)
    total_lots = await asyncio.to_thread(state.mt5_client.get_lots, state.account_row, symbol, start_price, end_price)
    return {"total_lots": total_lots}