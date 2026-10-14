# services/simulate.py
#
# 청산 시뮬레이션 - "이 가격에 이 랏으로 청산하면" 텔레그램 청산 알림(api/trade.py _trade_message)과 같은 모양으로.
# 웹 화면 계산기(/accounts/{번호}/simulate)와 텔레그램 /sim 이 같이 쓴다.
#
#   손익·증거금 : 워커가 MT5 로 직접 계산 (order_calc_profit / order_calc_margin - 환율·계약 크기 반영)
#   수수료      : 이 계좌·종목 최근 청산 50건의 랏당 평균 수수료 × 랏 (기록이 없으면 0)
#   오늘 누적   : 거래일(06:00) 시작부터 청산된 거래 + 이번 시뮬레이션

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from db.models.trade import Trade
from services.worker_client import worker_get

KST = ZoneInfo("Asia/Seoul")


def _price(v: float | None, digits: int | None = None) -> str:
    if v is None:
        return "-"
    d = digits if isinstance(digits, int) else (2 if abs(v) >= 100 else 5 if abs(v) < 10 else 3)
    return f"{v:,.{d}f}"


def _hold(minutes: float) -> str:
    m = max(0, int(minutes))
    d, rem = divmod(m, 1440)
    h, m = divmod(rem, 60)
    return f"{d}일 {h}시간" if d else f"{h}시간 {m}분" if h else f"{m}분"


async def simulate(db, account, symbol: str, side: str, volume: float, price_open: float,
                   price_close: float, hold_min: float | None = None) -> dict:
    side = "BUY" if side.upper().startswith("B") else "SELL"
    r = await worker_get(account.id, "/calc", {"symbol": symbol, "side": side, "volume": volume,
                                                "price_open": price_open, "price_close": price_close})
    name, digits = r["symbol"], r.get("digits")
    profit = float(r.get("profit") or 0)

    # 수수료: 최근 청산 50건의 랏당 평균
    rows = (await db.execute(
        select(Trade.commission, Trade.volume).where(
            Trade.account_id == account.id, Trade.symbol == name, Trade.close_time != None)
        .order_by(Trade.close_time.desc()).limit(50))).all()
    lots = sum(v or 0 for _, v in rows)
    rate = (sum(c or 0 for c, _ in rows) / lots) if lots else 0.0
    fee = rate * volume
    net = profit + fee

    # 오늘(거래일 06:00~) 누적
    now = datetime.now(KST)
    try:
        from services.loss_limit import day_start
        start = day_start(now)
    except Exception:
        start = (now - timedelta(hours=6)).replace(hour=6, minute=0, second=0, microsecond=0)
    today = (await db.execute(select(Trade.profit, Trade.commission).where(
        Trade.account_id == account.id, Trade.close_time != None, Trade.close_time >= start))).all()
    total = sum((p or 0) + (c or 0) for p, c in today) + net
    wins = sum(1 for p, c in today if (p or 0) + (c or 0) > 0) + (1 if net > 0 else 0)

    icon, word = ("✅", "익절") if net > 0 else ("🔴", "손절") if net < 0 else ("⚪", "본전")
    move = (price_close - price_open) if side == "BUY" else (price_open - price_close)
    title = f"{icon} {word} · {name} {side} {volume:g}lot  {net:+,.2f}"
    lines = [
        f"{_price(price_open, digits)} → {_price(price_close, digits)} ({move:+,.2f})"
        + (f" · 보유 {_hold(hold_min)}" if hold_min else ""),
        f"손익 {profit:+,.2f} · 수수료 {fee:+,.2f} · 계좌 {account.account_number}",
        f"오늘 누적 {total:+,.2f} ({len(today) + 1}건 · 승 {wins})",
    ]
    bal, margin, free = r.get("balance"), r.get("margin"), r.get("margin_free")
    extra = []
    if bal:
        extra.append(f"잔고 {bal:,.2f} → {bal + net:,.2f} ({net / bal * 100:+.2f}%)")
    if margin is not None:
        extra.append(f"필요 증거금 {margin:,.2f}" + (f" · 여유 증거금 {free:,.2f} → 진입 후 {free - margin:,.2f}" if free is not None else "")
                     + (f" · 레버리지 1:{r['leverage']}" if r.get("leverage") else ""))
    if margin and volume and free is not None:
        step = r.get("volume_step") or 0.01
        max_lot = int(free / (margin / volume) / step) * step
        extra.append(f"여유 증거금으로 최대 {max_lot:.2f}랏 (1랏당 증거금 {margin / volume:,.2f})")
    return {
        "symbol": name, "side": side, "volume": volume, "price_open": price_open, "price_close": price_close,
        "profit": profit, "fee": fee, "fee_per_lot": rate, "net": net, "margin": margin,
        "margin_free": free, "balance": bal, "leverage": r.get("leverage"), "digits": digits,
        "contract_size": r.get("contract_size"), "volume_min": r.get("volume_min"), "volume_step": r.get("volume_step"),
        "today_total": total, "title": title, "lines": lines, "extra": extra,
    }


# ── 거래내역에서 고른 거래들을 N배 랏으로 했다면 (웹 화면 거래내역 → 선택 → 시뮬레이션 돌리기) ──
async def simulate_trades(db, account, trade_ids: list[int], mults: list[float]) -> dict:
    """고른 청산 거래들을 배수별로: 순손익, 잔고 대비, 최대 낙폭(청산 순), 가장 큰 손실,
    최대 동시 증거금(같은 시간에 열려 있던 포지션 합 - MT5 로 1랏 증거금을 계산해 곱함)"""
    import asyncio
    rows = (await db.execute(select(Trade).where(Trade.account_id == account.id, Trade.id.in_(trade_ids)))).scalars().all()
    trades = [t for t in rows if t.close_time is not None and t.open_time is not None]
    if not trades:
        return {"count": 0, "results": []}
    trades.sort(key=lambda t: t.close_time)
    net = lambda t: (t.profit or 0) + (t.commission or 0)

    try:
        info = await asyncio.wait_for(worker_get(account.id, "/account-info"), 4)
    except Exception:
        info = {}
    bal, free = info.get("balance"), info.get("margin_free")

    # 종목별 1랏 증거금 (그 종목 평균 진입가로 MT5 계산) - MT5 가 꺼져 있으면 증거금은 비움
    per_lot: dict[str, float | None] = {}
    for sym in {t.symbol for t in trades}:
        ts = [t for t in trades if t.symbol == sym]
        avg = sum(t.price_open * t.volume for t in ts) / sum(t.volume for t in ts)
        side = ts[0].position or "BUY"
        try:
            c = await asyncio.wait_for(worker_get(account.id, "/calc", {
                "symbol": sym, "side": side, "volume": 1, "price_open": avg}), 4)
            per_lot[sym] = c.get("margin")
        except Exception:
            per_lot[sym] = None

    # 같은 시간에 열려 있던 랏·증거금의 최대 (1배 기준)
    events = []
    for t in trades:
        m = (per_lot.get(t.symbol) or 0) * t.volume
        events += [(t.open_time, 1, t.volume, m), (t.close_time, -1, t.volume, m)]
    events.sort(key=lambda e: (e[0], e[1]))            # 같은 시각이면 청산 먼저
    cur_l = cur_m = peak_l = peak_m = 0.0
    for _, d, v, m in events:
        cur_l += d * v; cur_m += d * m
        peak_l, peak_m = max(peak_l, cur_l), max(peak_m, cur_m)
    margin_known = all(per_lot.get(t.symbol) for t in trades)

    # 청산 순 누적 손익의 최대 낙폭 (1배 기준 - 배수를 곱하면 됨)
    eq = peak = dd = 0.0
    for t in trades:
        eq += net(t); peak = max(peak, eq); dd = max(dd, peak - eq)

    total = sum(net(t) for t in trades)
    worst, best = min(net(t) for t in trades), max(net(t) for t in trades)
    results = []
    for k in [1.0] + [float(x) for x in mults if float(x) != 1.0]:
        r = {"k": k, "lots": sum(t.volume for t in trades) * k, "net": total * k,
             "profit": sum(t.profit or 0 for t in trades) * k, "fee": sum(t.commission or 0 for t in trades) * k,
             "max_dd": dd * k, "worst": worst * k, "best": best * k,
             "peak_lots": peak_l * k, "peak_margin": peak_m * k if margin_known else None}
        if bal:
            r["pct"] = total * k / bal * 100
            r["dd_pct"] = dd * k / bal * 100
        if r["peak_margin"] is not None and free is not None:
            r["margin_ok"] = r["peak_margin"] <= (free + (info.get("margin") or 0))
        results.append(r)
    return {
        "count": len(trades), "skipped": len(trade_ids) - len(trades),
        "wins": sum(1 for t in trades if net(t) > 0), "losses": sum(1 for t in trades if net(t) < 0),
        "symbols": sorted({t.symbol for t in trades}),
        "from": trades[0].open_time.isoformat(), "to": trades[-1].close_time.isoformat(),
        "balance": bal, "margin_free": free, "margin_used": info.get("margin"),
        "margin_per_lot": per_lot, "results": results,
    }
