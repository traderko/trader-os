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
