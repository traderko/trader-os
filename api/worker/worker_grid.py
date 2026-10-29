# api/worker/worker_grid.py  (trade_lock_worker.py 가 include_router 하는 파일)
#
# 구간매매 주문 - 첫 진입 시장가 + KTR 간격 리밋 주문, 모두 같은 손절가. (계산은 services/grid.py)
# 이 워커가 붙어 있는 MT5 연결을 그대로 쓴다 (initialize/shutdown 하지 않음).
#
#   POST /grid/place   {symbol, side, ktr, lots[], stop_dist, sl?, magic, comment}  sl 이 있으면 그 손절가 그대로
#   GET  /grid/list    진행 중인 구간매매 (magic 71700000~71799999) - 포지션·대기 주문 묶어서
#   POST /grid/close   {magic, pending_only} 대기 주문 취소 (+ 포지션 청산)

import asyncio
import time

from fastapi import APIRouter, HTTPException, Request

from api.worker.worker_account_router import _resolve_symbol

router = APIRouter(prefix="/grid")

MAGIC_LO, MAGIC_HI = 71_700_000, 71_799_999


def _state(request: Request):
    state = request.app.state
    if getattr(state, "mt5", None) is None or getattr(state, "account_row", None) is None:
        raise HTTPException(503, "MT5 연결 안 됨")
    return state


def _filling(mt5, info):
    """브로커가 받는 체결 방식 (symbol_info.filling_mode 비트: 1 = FOK, 2 = IOC)"""
    fm = getattr(info, "filling_mode", 0) or 0
    if fm & 2:
        return mt5.ORDER_FILLING_IOC
    if fm & 1:
        return mt5.ORDER_FILLING_FOK
    return mt5.ORDER_FILLING_RETURN


def _norm_lot(lot, info):
    step = info.volume_step or 0.01
    lot = max(info.volume_min, min(info.volume_max, lot))
    return round(round(lot / step) * step, 8)


@router.post("/place")
async def place(request: Request, body: dict):
    state = _state(request)
    mt5 = state.mt5

    def run():
        name = _resolve_symbol(mt5, body.get("symbol") or "")
        if not name:
            raise HTTPException(404, f"종목을 찾지 못했습니다: {body.get('symbol')}")
        mt5.symbol_select(name, True)
        info, tick = mt5.symbol_info(name), mt5.symbol_info_tick(name)
        if info is None or tick is None:
            raise HTTPException(502, f"{name} 시세를 받지 못했습니다.")
        buy = str(body.get("side", "")).lower().startswith("b")
        ktr, stop_dist = float(body["ktr"]), float(body["stop_dist"])
        lots = [float(x) for x in body.get("lots") or []]
        magic, comment = int(body["magic"]), str(body.get("comment") or "TraderOS")[:20]
        if not (MAGIC_LO <= magic <= MAGIC_HI) or not lots or ktr <= 0 or stop_dist <= 0:
            raise HTTPException(400, "주문 값이 잘못됐습니다.")
        d = info.digits
        entry = tick.ask if buy else tick.bid
        sgn = -1 if buy else 1
        sl = round(entry + sgn * stop_dist, d)
        if body.get("sl"):                       # 손절가를 직접 넣은 경우 - 그 가격 그대로 (지금 가격 반대쪽이어야 함)
            sl = round(float(body["sl"]), d)
            if (sl >= entry) if buy else (sl <= entry):
                raise HTTPException(400, f"손절가 {sl:.{d}f} 가 지금 가격 {entry:.{d}f} 의 {'아래' if buy else '위'}가 아닙니다.")
            stop_dist = abs(entry - sl)
            lots = [lv for i, lv in enumerate(lots) if i * ktr < stop_dist]   # 시세가 움직여 손절 밖으로 나간 구간은 뺌
        # 손절이 스프레드·브로커 최소 거리보다 가까우면 주문 전에 멈춤
        min_dist = max(info.spread, info.trade_stops_level) * info.point
        if stop_dist <= min_dist:
            raise HTTPException(400, f"손절 거리({stop_dist:.{d}f})가 스프레드·브로커 최소 거리({min_dist:.{d}f})보다 작습니다.")

        res = mt5.order_send({
            "action": mt5.TRADE_ACTION_DEAL, "symbol": name, "volume": _norm_lot(lots[0], info),
            "type": mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL, "price": entry, "sl": sl, "tp": 0.0,
            "deviation": 30, "magic": magic, "comment": f"{comment} 1", "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": _filling(mt5, info)})
        if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
            raise HTTPException(400, f"시장가 주문 실패: {getattr(res, 'retcode', '?')} {getattr(res, 'comment', mt5.last_error())}")
        fill = getattr(res, "price", 0) or entry

        placed, failed = [{"no": 1, "ticket": res.order, "price": fill, "lot": _norm_lot(lots[0], info), "kind": "market"}], []
        for i in range(1, len(lots)):
            price = round(entry + sgn * ktr * i, d)
            r = mt5.order_send({
                "action": mt5.TRADE_ACTION_PENDING, "symbol": name, "volume": _norm_lot(lots[i], info),
                "type": mt5.ORDER_TYPE_BUY_LIMIT if buy else mt5.ORDER_TYPE_SELL_LIMIT, "price": price,
                "sl": sl, "tp": 0.0, "magic": magic, "comment": f"{comment} {i + 1}",
                "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_RETURN})
            if r is not None and r.retcode == mt5.TRADE_RETCODE_DONE:
                placed.append({"no": i + 1, "ticket": r.order, "price": price, "lot": _norm_lot(lots[i], info), "kind": "limit"})
            else:
                failed.append({"no": i + 1, "price": price, "error": f"{getattr(r, 'retcode', '?')} {getattr(r, 'comment', '')}"})
            time.sleep(0.05)
        return {"symbol": name, "side": "buy" if buy else "sell", "entry": entry, "fill": fill, "sl": sl,
                "magic": magic, "placed": placed, "failed": failed, "digits": d}

    return await asyncio.to_thread(run)


@router.get("/list")
async def grid_list(request: Request):
    state = _state(request)
    mt5 = state.mt5

    def run():
        groups: dict[int, dict] = {}

        def g(magic, symbol):
            return groups.setdefault(magic, {"magic": magic, "symbol": symbol, "side": None, "positions": 0, "lots": 0.0,
                                             "profit": 0.0, "pending": 0, "pending_lots": 0.0, "sl": None, "comment": "",
                                             "avg": 0.0, "next_price": None})
        for p in mt5.positions_get() or []:
            if MAGIC_LO <= p.magic <= MAGIC_HI:
                x = g(p.magic, p.symbol)
                x["side"] = "buy" if p.type == 0 else "sell"
                x["avg"] = (x["avg"] * x["lots"] + p.price_open * p.volume) / (x["lots"] + p.volume)
                x["positions"] += 1; x["lots"] += p.volume; x["profit"] += p.profit + p.swap
                x["sl"] = p.sl or x["sl"]; x["comment"] = (p.comment or "").rsplit(" ", 1)[0]
        for o in mt5.orders_get() or []:
            if MAGIC_LO <= o.magic <= MAGIC_HI:
                x = g(o.magic, o.symbol)
                x["side"] = x["side"] or ("buy" if o.type in (2, 4) else "sell")
                x["pending"] += 1; x["pending_lots"] += o.volume_current
                x["sl"] = x["sl"] or o.sl or None; x["comment"] = x["comment"] or (o.comment or "").rsplit(" ", 1)[0]
                # 다음에 체결될 주문 = 시세에 가장 가까운 것 (매수 리밋은 가장 높은 값, 매도 리밋은 가장 낮은 값)
                nxt = x["next_price"]
                if nxt is None or (o.price_open > nxt if x["side"] == "buy" else o.price_open < nxt):
                    x["next_price"] = o.price_open
        return sorted(groups.values(), key=lambda x: -x["magic"])

    return await asyncio.to_thread(run)


@router.post("/close")
async def grid_close(request: Request, body: dict):
    state = _state(request)
    mt5 = state.mt5
    magic, pending_only = int(body.get("magic") or 0), bool(body.get("pending_only"))
    if not (MAGIC_LO <= magic <= MAGIC_HI):
        raise HTTPException(400, "구간매매 주문 번호가 아닙니다.")

    def run():
        cancelled = closed = 0
        errors = []
        for o in mt5.orders_get() or []:
            if o.magic == magic:
                r = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket})
                if r is not None and r.retcode == mt5.TRADE_RETCODE_DONE:
                    cancelled += 1
                else:
                    errors.append(f"주문 #{o.ticket} 취소 실패 {getattr(r, 'retcode', '?')}")
        if not pending_only:
            for p in mt5.positions_get() or []:
                if p.magic != magic:
                    continue
                info, tick = mt5.symbol_info(p.symbol), mt5.symbol_info_tick(p.symbol)
                r = mt5.order_send({
                    "action": mt5.TRADE_ACTION_DEAL, "position": p.ticket, "symbol": p.symbol, "volume": p.volume,
                    "type": mt5.ORDER_TYPE_SELL if p.type == 0 else mt5.ORDER_TYPE_BUY,
                    "price": tick.bid if p.type == 0 else tick.ask, "deviation": 30, "magic": magic,
                    "comment": "TraderOS close", "type_filling": _filling(mt5, info)})
                if r is not None and r.retcode == mt5.TRADE_RETCODE_DONE:
                    closed += 1
                else:
                    errors.append(f"포지션 #{p.ticket} 청산 실패 {getattr(r, 'retcode', '?')}")
        return {"cancelled": cancelled, "closed": closed, "errors": errors}

    return await asyncio.to_thread(run)


@router.get("/detail")
async def grid_detail(request: Request, magic: int):
    """한 구간매매 묶음의 체결 포지션·대기 주문 (화면의 상세 보기)"""
    state = _state(request)
    mt5 = state.mt5

    def run():
        pos = [p for p in (mt5.positions_get() or []) if p.magic == magic]
        ords = [o for o in (mt5.orders_get() or []) if o.magic == magic]
        sym = (pos or ords or [None])[0]
        info = mt5.symbol_info(sym.symbol) if sym else None
        tick = mt5.symbol_info_tick(sym.symbol) if sym else None
        return {
            "symbol": sym.symbol if sym else None, "digits": getattr(info, "digits", None),
            "bid": getattr(tick, "bid", None), "ask": getattr(tick, "ask", None),
            "positions": [{"ticket": p.ticket, "type": p.type, "volume": p.volume, "price_open": p.price_open,
                           "price_current": p.price_current, "profit": p.profit, "swap": p.swap, "sl": p.sl, "tp": p.tp,
                           "time": p.time, "comment": p.comment} for p in pos],
            "orders": [{"ticket": o.ticket, "type": o.type, "volume": o.volume_current, "price": o.price_open,
                        "sl": o.sl, "tp": o.tp, "time": o.time_setup, "comment": o.comment} for o in ords],
        }

    return await asyncio.to_thread(run)


# ── 첫 진입 추가 리밋 (services/addon.py) ─────────────────────────────
#   첫 진입(같은 종목·방향 포지션이 없던 상태에서 들어간 것) 뒤 평균가에서 N KTR 불리한 곳에 같은 랏 리밋 1개.
#   magic 71800000 ~ 71899999 (구간매매 71700000~ 와 따로 - 구간 목록에 안 섞이게)
#   POST /grid/addon         {symbol, side, price, lot, sl, magic, comment}  리밋 주문
#   GET  /grid/addons        걸려 있는 추가 리밋들
#   POST /grid/addon-cancel  {ticket}
#   POST /grid/addon-sl      {ticket, sl}   SL 따라가기
ADDON_LO, ADDON_HI = 71_800_000, 71_899_999


@router.post("/addon")
async def addon_place(request: Request, body: dict):
    state = _state(request)
    mt5 = state.mt5

    def run():
        name = _resolve_symbol(mt5, body.get("symbol") or "")
        if not name:
            raise HTTPException(404, f"종목을 찾지 못했습니다: {body.get('symbol')}")
        mt5.symbol_select(name, True)
        info, tick = mt5.symbol_info(name), mt5.symbol_info_tick(name)
        if info is None or tick is None:
            raise HTTPException(502, f"{name} 시세를 받지 못했습니다.")
        buy = str(body.get("side", "")).lower().startswith("b")
        magic = int(body.get("magic") or 0)
        if not (ADDON_LO <= magic <= ADDON_HI):
            raise HTTPException(400, "추가 리밋 주문 번호가 아닙니다.")
        d = info.digits
        price = round(float(body["price"]), d)
        sl = round(float(body.get("sl") or 0), d)
        lot = _norm_lot(float(body["lot"]), info)
        now = tick.ask if buy else tick.bid
        if (price >= now) if buy else (price <= now):
            raise HTTPException(400, f"리밋 가격 {price:.{d}f} 이 지금 가격 {now:.{d}f} 의 {'아래' if buy else '위'}가 아닙니다 (이미 지나감).")
        if sl and ((sl >= price) if buy else (sl <= price)):
            raise HTTPException(400, f"손절 {sl:.{d}f} 이 리밋 가격 {price:.{d}f} 보다 {'아래' if buy else '위'}여야 합니다.")
        r = mt5.order_send({
            "action": mt5.TRADE_ACTION_PENDING, "symbol": name, "volume": lot,
            "type": mt5.ORDER_TYPE_BUY_LIMIT if buy else mt5.ORDER_TYPE_SELL_LIMIT, "price": price,
            "sl": sl, "tp": 0.0, "magic": magic, "comment": str(body.get("comment") or "TOS addon")[:20],
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_RETURN})
        if r is None or r.retcode != mt5.TRADE_RETCODE_DONE:
            raise HTTPException(400, f"리밋 주문 실패: {getattr(r, 'retcode', '?')} {getattr(r, 'comment', mt5.last_error())}")
        return {"ticket": r.order, "symbol": name, "price": price, "lot": lot, "sl": sl, "digits": d, "magic": magic}

    return await asyncio.to_thread(run)


@router.get("/addons")
async def addon_list(request: Request):
    state = _state(request)
    mt5 = state.mt5

    def run():
        out = []
        for o in mt5.orders_get() or []:
            if ADDON_LO <= o.magic <= ADDON_HI:
                info = mt5.symbol_info(o.symbol)
                out.append({"ticket": o.ticket, "symbol": o.symbol, "type": o.type, "side": "buy" if o.type in (2, 4) else "sell",
                            "volume": o.volume_current, "price": o.price_open, "sl": o.sl, "tp": o.tp, "magic": o.magic,
                            "time": o.time_setup, "digits": getattr(info, "digits", None)})
        return out

    return await asyncio.to_thread(run)


@router.post("/addon-cancel")
async def addon_cancel(request: Request, body: dict):
    state = _state(request)
    mt5 = state.mt5
    ticket = int(body.get("ticket") or 0)

    def run():
        o = next((o for o in (mt5.orders_get(ticket=ticket) or []) if ADDON_LO <= o.magic <= ADDON_HI), None)
        if o is None:
            return {"ok": True, "gone": True}           # 이미 체결됐거나 취소됨
        r = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": ticket})
        if r is None or r.retcode != mt5.TRADE_RETCODE_DONE:
            raise HTTPException(400, f"취소 실패: {getattr(r, 'retcode', '?')} {getattr(r, 'comment', '')}")
        return {"ok": True}

    return await asyncio.to_thread(run)


@router.post("/addon-sl")
async def addon_sl(request: Request, body: dict):
    state = _state(request)
    mt5 = state.mt5
    ticket = int(body.get("ticket") or 0)

    def run():
        o = next((o for o in (mt5.orders_get(ticket=ticket) or []) if ADDON_LO <= o.magic <= ADDON_HI), None)
        if o is None:
            raise HTTPException(404, "주문이 없습니다 (체결 또는 취소됨).")
        info = mt5.symbol_info(o.symbol)
        sl = round(float(body.get("sl") or 0), getattr(info, "digits", 5))
        r = mt5.order_send({"action": mt5.TRADE_ACTION_MODIFY, "order": ticket, "price": o.price_open, "sl": sl,
                            "tp": o.tp, "type_time": o.type_time, "expiration": o.time_expiration})
        if r is None or r.retcode != mt5.TRADE_RETCODE_DONE:
            raise HTTPException(400, f"SL 바꾸기 실패: {getattr(r, 'retcode', '?')} {getattr(r, 'comment', '')}")
        return {"ok": True, "sl": sl}

    return await asyncio.to_thread(run)
