from datetime import datetime, timedelta, timezone
import json
from typing import List
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, HTTPException
from db.models.account import Account
from db.models.broker import Broker
from db.models.trade import Trade
from db.models.trade_tag_map import TradeTagMap
from db.session import get_db
from services.account import get_account_or_default
from services.fcm_service import FCMService, get_fcm_service
from mt5.mt5Client import Mt5Client
from schemas.trade import TradeCreate
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy import func, select
from db.upsert import upsert_insert as insert
from services.worker_client import worker_get
from util.parser import parse_dt

router = APIRouter(prefix="/trades")

LOCK_CLOSE_MAGIC = 990099
LOSS_LIMIT_MAGIC = 990098   # services/loss_limit.py - 일일 손실 한도 청산
BATCH_SIZE = 1000  # 11 columns * 1000 = 11000 params, 여유있게 안전

def _iso(dt):
    return dt.isoformat() if dt else None


def _trade_json(t: Trade) -> dict:
    """거래 하나를 JSON으로.
    ORM 객체를 그대로 돌려주면 계좌의 암호화된 비밀번호까지 나가고, 태그도 이름 없이
    연결 정보(tag_id)만 나가서 직접 만든다. 앱(Trade.fromJson)이 읽는 필드 이름은 그대로 유지."""
    acc = t.account
    return {
        "id": t.id, "ticket": t.ticket, "symbol": t.symbol, "position": t.position, "magic": t.magic,
        "volume": t.volume, "price_open": t.price_open, "price_close": t.price_close,
        "profit": t.profit, "commission": t.commission,
        "open_time": _iso(t.open_time), "close_time": _iso(t.close_time),
        "account_id": t.account_id,
        "account": None if acc is None else {
            "id": acc.id, "account_number": acc.account_number, "broker_id": acc.broker_id,
            "broker": None if acc.broker is None else {"id": acc.broker.id, "name": acc.broker.name, "server": acc.broker.server},
        },
        "note": None if t.note is None else {
            "time_frame": t.note.time_frame, "entry_reason": t.note.entry_reason,
            "exit_reason": t.note.exit_reason, "loss_reason": t.note.loss_reason, "memo": t.note.memo,
        },
        "images": [{"id": i.id, "file_path": i.file_path} for i in (t.images or [])],
        "tags": [{"id": m.tag.id, "name": m.tag.name, "type": getattr(m.tag.type, "value", m.tag.type)}
                 for m in (t.tags or []) if m.tag is not None],
    }


# 같은 체결이 두 번 들어오는 경우(tradeLock EA와 예전 api EA를 같이 붙였을 때, EA 재전송 등)를
# 한 번만 처리하기 위한 최근 체결 목록. 두 번 처리하면 수수료가 두 번 더해지고 알림도 두 번 간다.
_recent_deals: dict[tuple, float] = {}
_RECENT_DEAL_SEC = 3600


def _already_seen(data: TradeCreate) -> bool:
    import time as _t
    now = _t.time()
    for k, ts in list(_recent_deals.items()):
        if now - ts > _RECENT_DEAL_SEC:
            del _recent_deals[k]
    # deal 번호가 없으면(예전 api EA) 체결 내용으로 구분
    key = ("deal", data.account_number, data.deal) if data.deal else \
          ("raw", data.account_number, data.ticket, data.entry_type, data.time, round(data.price, 5), round(data.volume, 2))
    raw_key = ("raw", data.account_number, data.ticket, data.entry_type, data.time, round(data.price, 5), round(data.volume, 2))
    return key in _recent_deals or raw_key in _recent_deals


def _mark_seen(data: TradeCreate) -> None:
    """처리에 성공한 뒤에만 기록 - 실패한 요청은 EA가 다시 보내면 다시 처리되게"""
    import time as _t
    now = _t.time()
    raw_key = ("raw", data.account_number, data.ticket, data.entry_type, data.time, round(data.price, 5), round(data.volume, 2))
    _recent_deals[raw_key] = now
    if data.deal:
        _recent_deals[("deal", data.account_number, data.deal)] = now


@router.post("")
async def create_trade(
    data: TradeCreate, 
    db: AsyncSession = Depends(get_db),
    fcm: FCMService = Depends(get_fcm_service)
):
    if _already_seen(data):
        return {"ok": True, "duplicate": True}

    broker_result = await db.execute(
        select(Broker).where(Broker.server == data.broker_server)
    )

    broker = broker_result.scalar_one_or_none()

    if broker is None:
        raise ValueError(f"broker not found. server = {data.broker_server}")

    account_result = await db.execute(
        select(Account).where(
            Account.account_number == str(data.account_number) and
            Account.broker_id == broker.id
        )
    )

    account = account_result.scalar_one_or_none()

    if account is None:
        raise ValueError(f"account not found. account_number = {data.account_number}, broker_id = {broker.id}")

    trade_result = await db.execute(
        select(Trade)
        .where(Trade.ticket == data.ticket)
    )
    trade = trade_result.scalar_one_or_none()

    broker_time = datetime.fromtimestamp(data.time, tz=timezone.utc)
    offset = Mt5Client._get_broker_offset(broker_time)
    utc_time = broker_time - timedelta(hours=offset)
    kst_time = utc_time.astimezone(ZoneInfo(Mt5Client.KOREA_TIMEZONE_STR))

    if data.entry_type == "OPEN":
        if trade is None:
            trade = Trade(
                ticket = data.ticket,
                symbol = data.symbol,
                position = data.position,
                volume = data.volume,
                price_open = data.price,
                open_time = kst_time,
                commission = data.commission,
                account_id = account.id,
                magic = data.magic or None,
            )
            db.add(trade)
        elif data.magic and not trade.magic:
            trade.magic = data.magic

        recent_trade_result = await db.execute(
            select(Trade)
            .where(
                Trade.account_id == account.id,
                Trade.symbol == data.symbol,
                Trade.open_time >= kst_time - timedelta(minutes = 5)
            )
            .order_by(Trade.open_time.desc())
        )

        recent_trades = recent_trade_result.scalars().all()

        same_price_count = 0

        for t in recent_trades:
            price_diff = abs(t.price_open - data.price) / data.price

            if (
                t.position == data.position and
                price_diff <= 0.002
            ):
                same_price_count += 1

        if same_price_count >= 2:
            fcm.send(
                "뇌동매매 경고",
                f"{data.symbol} 비슷한 가격에서 반복 진입 중",
                json.dumps({
                    "type": "revenge_trade"
                })
            )

        last_closed_result = await db.execute(
            select(Trade)
            .where(
                Trade.account_id == account.id,
                Trade.symbol == data.symbol,
                Trade.close_time != None
            )
            .order_by(Trade.close_time.desc())
            .limit(1)
        )

        last_closed = last_closed_result.scalar_one_or_none()

        from services import guidelines
        if (
            not guidelines.rule("revenge") and      # 지침 '손절 직후 재진입'이 켜져 있으면 그쪽에서 자세히 알림
            last_closed and
            last_closed.profit < 0 and
            (kst_time - last_closed.close_time).total_seconds() < 180
        ):
            fcm.send(
                "감정매매 주의",
                f"{data.symbol} 손실 직후 빠른 재진입 감지",
                json.dumps({
                    "type": "loss_reentry"
                })
            )

    elif data.entry_type == "CLOSE":
        if trade:
            trade.price_close = data.price
            trade.profit = data.profit
            trade.close_time = kst_time
            trade.commission += data.commission 
        else:
            trade = Trade(
                ticket = data.ticket,
                symbol = data.symbol,
                position = data.position,
                volume = data.volume,
                profit = data.profit,
                price_close = data.price,
                close_time = kst_time,
                commission = data.commission,
                account_id = account.id
            )
            db.add(trade)

    # 잠금 중에 진입해서 tradeLock EA가 강제청산한 체결 (EA가 청산 주문에 붙이는 매직넘버로 구분)
    forced = data.entry_type == "CLOSE" and getattr(data, "magic", 0) == LOCK_CLOSE_MAGIC
    lock_why = ""
    if forced:
        try:
            from services import trade_lock_service as lock_svc
            st = await lock_svc.get_lock_state(db, account.id)
            r = st.get("reason") or ""
            if r.startswith("session_window"):
                lock_why = "·".join((st.get("session") or {}).get("names") or []) + " 집중 구간"
            else:
                lock_why = {"restricted_window": "정기 재확인", "consec_loss": "연속 손절", "manual": "수동 잠금"}.get(
                    next((k for k in ("restricted_window", "consec_loss", "manual") if r.startswith(k)), ""), r)
        except Exception:
            pass

    await db.commit()
    _mark_seen(data)

    # 트레이딩 지침 (복수매매·과매매·비중 급증·아시아장·첫 진입) - 뒤에서 확인 (services/guidelines.py)
    if data.entry_type == "OPEN":
        import asyncio
        from services import guidelines
        gt = asyncio.create_task(guidelines.on_open(account.id, account.account_number, data.symbol, data.position,
                                                    data.volume, kst_time))
        _bg.add(gt)
        gt.add_done_callback(_bg.discard)

    payload = {
        'trade_id': trade.id
    }

    if forced:
        # 청산 체결의 방향은 반대(BUY 포지션을 닫으면 SELL 체결)라서 진입 방향은 기록된 거래에서 가져옴
        side = trade.position if trade.position and trade.price_open is not None else ("BUY" if data.position == "SELL" else "SELL")
        net = (data.profit or 0) + (data.commission or 0)
        fcm.send(
            "🔒 잠금 중 진입 → 강제청산",
            f"{data.symbol} {side} {data.volume:g}lot · 손익 {net:+,.2f}"
            + (f"\n잠금 사유: {lock_why}" if lock_why else "")
            + "\n거래 잠금 상태에서 들어간 포지션이라 tradeLock EA가 바로 청산했습니다.",
            json.dumps({"type": "trade_lock_forced_close", "symbol": data.symbol, **payload}),
        )
    elif data.entry_type == "CLOSE" and getattr(data, "magic", 0) == LOSS_LIMIT_MAGIC:
        pass   # 일일 손실 한도로 한꺼번에 청산한 것 - 워커가 요약 알림을 따로 보냄
    else:
        # 알림 문구에 워커 조회(같은 종목 포지션)가 들어가서, EA 응답을 늦추지 않게 뒤에서 보냄
        import asyncio
        task = asyncio.create_task(_notify_trade(account.id, account.account_number, trade.id, data, kst_time, fcm, payload))
        _bg.add(task)
        task.add_done_callback(_bg.discard)

    return {"ok": True}


_bg: set = set()


_batches: dict[tuple, dict] = {}    # (계좌, 종목, 진입/청산, 방향) → 묶는 중인 체결들


async def _notify_trade(account_id: int, account_number: str, trade_id: int, data: TradeCreate,
                        kst_time: datetime, fcm: FCMService, payload: dict) -> None:
    """체결 알림. 같은 종목·방향으로 몇 초 안에 나눠 체결되면 하나로 묶고(trade_batch_sec),
    아래에 랏 배수 시뮬레이션('N배였다면')을 붙인다(trade_sim, trade_sim_mult). 설정: 관리 화면 텔레그램 탭"""
    import asyncio
    import time as _t
    from db.session import AsyncSessionLocal
    from services.settings_service import telegram_cfg
    cfg = telegram_cfg()
    items = [(trade_id, data, kst_time)]
    wait = cfg.get("trade_batch_sec", 0)
    if wait > 0:
        key = (account_id, data.symbol, data.entry_type, data.position)
        b = _batches.get(key)
        if b is not None:                 # 이미 모으는 중 - 거기에 넣고 끝 (먼저 온 쪽이 보냄)
            b["items"].append((trade_id, data, kst_time))
            b["last"] = _t.time()
            return
        _batches[key] = b = {"items": items, "last": _t.time()}
        start = _t.time()
        while _t.time() - b["last"] < wait and _t.time() - start < wait * 3:
            await asyncio.sleep(0.3)
        items = _batches.pop(key)["items"]
    try:
        async with AsyncSessionLocal() as db:
            if len(items) == 1:
                trade = await db.get(Trade, trade_id)
                title, body, buttons = await _trade_message(db, account_id, account_number, trade, data, kst_time)
            else:
                title, body, buttons = await _group_message(db, account_id, account_number, items)
            if cfg.get("trade_sim", True) and cfg.get("trade_sim_mult"):
                sim = await _mult_lines(db, account_id, items, cfg["trade_sim_mult"])
                if sim:
                    body += "\n\n" + "\n".join(sim)
    except Exception as e:
        print(f"[trade] 알림 문구 만들기 실패: {e}")
        title, body, buttons = f"[{data.entry_type}] {data.symbol}", f"{data.position} {data.volume:g}lot @{data.price}", None
    fcm.send(title, body, json.dumps(payload), buttons=buttons)


async def _group_message(db: AsyncSession, account_id: int, account_number: str, items: list) -> tuple[str, str, list | None]:
    """나눠 체결된 여러 건을 알림 하나로. 버튼·오늘 누적 등은 마지막 건의 알림에서 가져옴"""
    last_id, last_data, last_time = items[-1]
    last_trade = await db.get(Trade, last_id)
    _, last_body, buttons = await _trade_message(db, account_id, account_number, last_trade, last_data, last_time)
    # 근거 버튼은 묶인 거래 전부에 저장되게
    kind = "o" if last_data.entry_type == "OPEN" else "c"
    group_row = _tag_button(kind, list(dict.fromkeys(tid for tid, _, _ in items)))
    buttons = [r for r in (buttons or []) if not (r and str(r[0].get("callback_data", "")).startswith("tg:"))] + ([group_row] if group_row else [])
    buttons = buttons or None
    trades = [(await db.get(Trade, tid), d, t) for tid, d, t in items]
    n, symbol = len(items), last_data.symbol
    from services.grid import kind_of, MODE_KR
    kinds = {kind_of(d.magic or (t.magic if t is not None else None)) for t, d, _ in trades}
    gtag = f"🧱 {MODE_KR[next(iter(kinds))]} · " if len(kinds) == 1 and None not in kinds else ""
    vol = sum(d.volume for _, d, _ in trades)
    when = last_time.strftime("%H:%M")
    keep = [l for l in last_body.split("\n")[1:] if not l.startswith("손익 ")]

    if last_data.entry_type == "OPEN":
        prices = [d.price for _, d, _ in trades]
        avg = sum(d.price * d.volume for _, d, _ in trades) / vol
        title = f"{gtag}🟢 진입 · {symbol} {last_data.position} {vol:g}lot ({n}건)"
        rng = f" ({_price(min(prices))}~{_price(max(prices))})" if max(prices) != min(prices) else ""
        return title, "\n".join([f"평균가 {_price(avg)}{rng} · {when} · 계좌 {account_number}"] + keep), buttons

    side = next((tr.position for tr, _, _ in trades if tr and tr.price_open is not None),
                "BUY" if last_data.position == "SELL" else "SELL")
    profit = sum(d.profit or 0 for _, d, _ in trades)
    fee = sum((tr.commission if tr and tr.commission is not None else d.commission) or 0 for tr, d, _ in trades)
    net = profit + fee
    nets = [(d.profit or 0) + ((tr.commission if tr and tr.commission is not None else d.commission) or 0) for tr, d, _ in trades]
    wins, losses = sum(1 for x in nets if x > 0), sum(1 for x in nets if x < 0)
    icon, word = ("✅", "익절") if net > 0 else ("🔴", "손절") if net < 0 else ("⚪", "본전")
    mix = f" · 익절 {wins} 손절 {losses}" if wins and losses else ""
    title = f"{gtag}{icon} {word} · {symbol} {side} {vol:g}lot ({n}건{mix})  {net:+,.2f}"
    opened = [(tr, d, t) for tr, d, t in trades if tr and tr.price_open is not None]
    lines = []
    if opened:
        ov = sum(d.volume for _, d, _ in opened)
        ao = sum(tr.price_open * d.volume for tr, d, _ in opened) / ov
        ac = sum(d.price * d.volume for _, d, _ in opened) / ov
        move = (ac - ao) if side == "BUY" else (ao - ac)
        holds = [(t - tr.open_time).total_seconds() for tr, _, t in opened if tr.open_time]
        hold = ""
        if holds:
            a, b = _hold(min(holds)), _hold(max(holds))
            hold = f" · 보유 {a}" if a == b else f" · 보유 {a}~{b}"
        lines.append(f"평균 {_price(ao)} → {_price(ac)} ({move:+,.2f}){hold}")
    lines.append(f"손익 {profit:+,.2f} · 수수료 {fee:+,.2f} · {when} · 계좌 {account_number}")
    return title, "\n".join(lines + [l for l in keep if not l.startswith(("청산가", "평균"))
                                       and "→" not in l]), buttons


async def _mult_lines(db: AsyncSession, account_id: int, items: list, mults: list[float]) -> list[str]:
    """랏 배수 시뮬레이션 - '이 체결을 N배 랏으로 했다면'
    청산: 손익·수수료·잔고 대비 %·필요 증거금·오늘 누적(오늘 거래 전부 N배였다면)
    진입: 필요 증거금·여유 증거금, SL 이 있으면 닿을 때 손익"""
    import asyncio
    rows = [(await db.get(Trade, tid), d, t) for tid, d, t in items]
    d0, t0 = items[-1][1], items[-1][2]
    symbol, vol = d0.symbol, sum(d.volume for _, d, _ in rows)
    try:
        info = await asyncio.wait_for(worker_get(account_id, "/account-info"), 3)
    except Exception:
        info = {}
    bal, free = info.get("balance"), info.get("margin_free")

    async def margin(side: str, lots: float, price: float):
        try:
            c = await asyncio.wait_for(worker_get(account_id, "/calc", {
                "symbol": symbol, "side": side, "volume": lots, "price_open": price}), 3)
            return c.get("margin")
        except Exception:
            return None

    out = []
    if d0.entry_type == "CLOSE":
        side = next((tr.position for tr, _, _ in rows if tr and tr.price_open is not None),
                    "BUY" if d0.position == "SELL" else "SELL")
        profit = sum(d.profit or 0 for _, d, _ in rows)
        fee = sum((tr.commission if tr and tr.commission is not None else d.commission) or 0 for tr, d, _ in rows)
        net = profit + fee
        opened = [(tr, d) for tr, d, _ in rows if tr and tr.price_open is not None]
        ao = (sum(tr.price_open * d.volume for tr, d in opened) / sum(d.volume for _, d in opened)) if opened else d0.price
        base = (bal - net) if bal else None              # 이 청산 전 잔고
        today = None
        try:
            from services.loss_limit import day_start
            r = (await db.execute(select(Trade.profit, Trade.commission).where(
                Trade.account_id == account_id, Trade.close_time != None, Trade.close_time >= day_start(t0)))).all()
            today = sum((p or 0) + (c or 0) for p, c in r)
        except Exception:
            pass
        for k in mults:
            lots = vol * k
            m = await margin(side, lots, ao)
            pct = f" (잔고 {net * k / base * 100:+.2f}%)" if base else ""
            out.append(f"🧪 {k:g}배였다면 {lots:g}lot: {net * k:+,.2f}{pct}")
            sub = [f"손익 {profit * k:+,.2f} · 수수료 {fee * k:+,.2f}"]
            if m is not None:
                sub.append(f"필요 증거금 {m:,.2f}" + (" ⚠ 지금 여유 증거금보다 큼" if free is not None and m > free + (m / k) else ""))
            if today is not None:
                sub.append(f"오늘 누적 {today * k:+,.2f}")
            out.append("   " + " · ".join(sub))
    else:
        side = d0.position
        avg = sum(d.price * d.volume for _, d, _ in rows) / vol
        try:
            ps = await asyncio.wait_for(worker_get(account_id, "/positions-all"), 3)
        except Exception:
            ps = []
        tickets = {d.ticket for _, d, _ in rows}
        mine = [p for p in ps if p.get("ticket") in tickets]
        for k in mults:
            lots = vol * k
            m = await margin(side, lots, avg)
            line = f"🧪 {k:g}배였다면 {lots:g}lot"
            if m is not None:
                line += f": 필요 증거금 {m:,.2f}" + (f" · 여유 {free:,.2f}" if free is not None else "")
                if free is not None and m > free + m / k:
                    line += " ⚠ 증거금 부족"
            w = [p for p in mine if p.get("sl")]
            if w:
                d = 1 if side == "BUY" else -1
                v = sum((p["sl"] - p["price_open"]) * d * p["volume"] * (p.get("contract_size") or 1) for p in w) * k
                line += f"\n   SL 닿으면 {v:+,.2f}" + (f" (잔고 {v / bal * 100:+.2f}%)" if bal else "")
            out.append(line)
    return out


# ── 진입·청산 알림 문구 ──────────────────────────────────────────
def _num(v: float, d: int = 2) -> str:
    return f"{v:,.{d}f}"


def _price(v: float | None) -> str:
    if v is None:
        return "-"
    return _num(v, 2 if abs(v) >= 100 else 5 if abs(v) < 10 else 3)


def _hold(sec: float) -> str:
    sec = max(0, int(sec))
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}일 {h}시간"
    if h:
        return f"{h}시간 {m}분"
    return f"{m}분" if m else f"{sec}초"


async def _symbol_positions(account_id: int, symbol: str) -> list[dict]:
    """같은 종목 지금 포지션 (워커에 물어봄 - 안 되면 빈 목록)"""
    import asyncio
    try:
        rows = await asyncio.wait_for(worker_get(account_id, "/positions-all"), 2)
        return [p for p in rows if p.get("symbol") == symbol]
    except Exception:
        return []


def _side_summary(positions: list[dict]) -> str:
    out = []
    for t, label in ((0, "BUY"), (1, "SELL")):
        side = [p for p in positions if p.get("type") == t]
        if side:
            vol = sum(p["volume"] for p in side)
            avg = sum(p["price_open"] * p["volume"] for p in side) / vol if vol else 0
            out.append(f"{label} {vol:g}lot @{_price(avg)} ({len(side)}개)")
    return " · ".join(out)


async def _trade_message(db: AsyncSession, account_id: int, account_number: str, trade: Trade, data: TradeCreate,
                         kst_time: datetime) -> tuple[str, str, list | None]:
    """
    진입:  🟢 진입 · XAUUSD+ BUY 0.5lot
           가격 4,010.25 · 22:15
           SL 3,990.00 · TP 4,050.00
           같은 종목 보유: BUY 1.5lot @4,005.10 (3개)
    청산:  ✅ 익절 · XAUUSD+ BUY 0.5lot  +512.50
           4,000.00 → 4,010.25 (+10.25) · 보유 45분
           손익 +516.00 · 수수료 -3.50
           오늘 누적 +1,230.00 (5건 · 승 4)
    """
    acc_line = f"계좌 {account_number}"
    when = kst_time.strftime("%H:%M")
    from services.grid import kind_of, MODE_KR
    gk = kind_of(data.magic or (trade.magic if trade is not None else None))
    tag = f"🧱 {MODE_KR[gk]} · " if gk else ""          # 구간매매로 넣은 주문 (직접 거래와 구분)

    if data.entry_type == "OPEN":
        title = f"{tag}🟢 진입 · {data.symbol} {data.position} {data.volume:g}lot"
        lines = [f"가격 {_price(data.price)} · {when} · {acc_line}"]
        positions = await _symbol_positions(account_id, data.symbol)
        me = next((p for p in positions if p.get("ticket") == data.ticket), None)
        if me and (me.get("sl") or me.get("tp")):
            lines.append(f"SL {_price(me.get('sl')) if me.get('sl') else '없음'} · TP {_price(me.get('tp')) if me.get('tp') else '없음'}")
        elif me:
            lines.append("SL 없음")
        others = [p for p in positions if p.get("ticket") != data.ticket]
        if others:
            lines.append("같은 종목 보유: " + _side_summary(positions))
        # 텔레그램 버튼 - 이 포지션의 SL/TP (services/telegram_live.py 가 처리)
        #   callback_data: pos:<sl|tp|be>:<o=이 포지션|a=같은 종목·방향 전체>:<계좌id>:<티켓>
        key = f"{account_id}:{data.ticket}"
        buttons = [[{"text": "🛑 SL", "callback_data": f"pos:sl:o:{key}"},
                    {"text": "🎯 TP", "callback_data": f"pos:tp:o:{key}"},
                    {"text": "⚖️ 본전 SL", "callback_data": f"pos:be:o:{key}"}]]
        same = [p for p in positions if p.get("type") == (me or {}).get("type", 0 if data.position == "BUY" else 1)]
        if len(same) > 1:
            buttons.append([{"text": f"🛑 SL 전체 {len(same)}개", "callback_data": f"pos:sl:a:{key}"},
                            {"text": f"🎯 TP 전체", "callback_data": f"pos:tp:a:{key}"},
                            {"text": "⚖️ 평단 SL", "callback_data": f"pos:be:a:{key}"}])
        tag_row = _tag_button("o", [trade.id] if trade is not None else [])
        if tag_row:
            buttons.append(tag_row)
        return title, "\n".join(lines), buttons

    # CLOSE - 진입 방향·가격은 기록된 거래에서 (청산 체결의 방향은 반대라서)
    side = trade.position if trade.price_open is not None else ("BUY" if data.position == "SELL" else "SELL")
    fee = trade.commission if trade.commission is not None else data.commission   # 진입+청산 수수료 합
    net = (data.profit or 0) + (fee or 0)
    icon, word = ("✅", "익절") if net > 0 else ("🔴", "손절") if net < 0 else ("⚪", "본전")
    title = f"{tag}{icon} {word} · {data.symbol} {side} {data.volume:g}lot  {net:+,.2f}"
    lines = []
    if trade.price_open is not None:
        move = (data.price - trade.price_open) if side == "BUY" else (trade.price_open - data.price)
        hold = f" · 보유 {_hold((kst_time - trade.open_time).total_seconds())}" if trade.open_time else ""
        lines.append(f"{_price(trade.price_open)} → {_price(data.price)} ({move:+,.2f}){hold}")
    else:
        lines.append(f"청산가 {_price(data.price)}")
    lines.append(f"손익 {data.profit:+,.2f} · 수수료 {fee:+,.2f} · {when} · {acc_line}")

    # 오늘(거래일 06:00부터) 누적
    try:
        from services.loss_limit import day_start
        start = day_start(kst_time)
        rows = (await db.execute(
            select(Trade.profit, Trade.commission).where(
                Trade.account_id == account_id, Trade.close_time != None, Trade.close_time >= start)
        )).all()
        if rows:
            total = sum((r.profit or 0) + (r.commission or 0) for r in rows)
            wins = sum(1 for r in rows if (r.profit or 0) + (r.commission or 0) > 0)
            lines.append(f"오늘 누적 {total:+,.2f} ({len(rows)}건 · 승 {wins})")
    except Exception:
        pass

    remain = await _symbol_positions(account_id, data.symbol)
    if remain:
        lines.append("남은 포지션: " + _side_summary(remain))
    tag_row = _tag_button("c", [trade.id] if trade is not None else [])
    return title, "\n".join(lines), ([tag_row] if tag_row else None)


def _tag_button(kind: str, ids: list[int]) -> list[dict] | None:
    """체결 알림의 [🏷 진입/청산 근거] 버튼 (services/telegram_tags.py)"""
    try:
        from services.telegram_tags import button
        return button(kind, ids)
    except Exception:
        return None

@router.get("/annual")
async def get_annual_trades(
    year: int,
    is_all: bool,
    account_number: int,
    db: AsyncSession = Depends(get_db)
):
    if not is_all:
        account = await get_account_or_default(db, account_number)

        if account is None:
            raise ValueError(f"account not found. account_number = {account_number}")

    kst = ZoneInfo(Mt5Client.KOREA_TIMEZONE_STR)

    start = datetime(year, 1, 1, tzinfo=kst)
    end = datetime(year + 1, 1, 1, tzinfo=kst)

    chain = (
        select(Trade)
        .options(selectinload(Trade.note))
        .options(selectinload(Trade.images))
        .options(selectinload(Trade.tags).selectinload(TradeTagMap.tag))
        .options(
            selectinload(Trade.account).selectinload(Account.broker)  # ← 중첩
        )
        .where(
            Trade.open_time >= start,
            Trade.open_time < end
        )
    )

    if not is_all:
        chain = chain.where(
            Trade.account_id == account.id
        )

    chain = chain.order_by(
        Trade.open_time
    )

    result = await db.execute(chain)

    return [_trade_json(t) for t in result.scalars().all()]

@router.get("")
async def get_trades(
    year: int,
    month: int,
    account_number: int,
    db: AsyncSession = Depends(get_db)
):
    account = await get_account_or_default(db, account_number)

    if account is None:
        raise ValueError(f"account not found. account_number = {account_number}")

    kst = ZoneInfo(Mt5Client.KOREA_TIMEZONE_STR)

    start = datetime(year, month, 1, tzinfo=kst)

    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=kst)
    else:
        end = datetime(year, month + 1, 1, tzinfo=kst)

    result = await db.execute(
        select(Trade)
        .options(selectinload(Trade.note))
        .options(selectinload(Trade.images))
        .options(selectinload(Trade.tags).selectinload(TradeTagMap.tag))
        .options(
            selectinload(Trade.account).selectinload(Account.broker)  # ← 중첩
        )
        .where(
            Trade.open_time >= start,
            Trade.open_time < end,
            Trade.account_id == account.id
        )
        .order_by(
            Trade.open_time
        )
    )

    return [_trade_json(t) for t in result.scalars().all()]

@router.get("/collect")
async def collect(
    account_number: int,
    start_year: int = 2025,
    start_month: int = 1,
    start_day: int = 1,
    end_year: int = 2027,
    end_month: int = 1,
    end_day: int = 1,
    db: AsyncSession = Depends(get_db)
):
    account = await get_account_or_default(db, account_number)
    if not account:
        return {"error": "account not found"}
 
    # 실제 MT5 조회(mt5.history_deals_get)는 워커가 담당 - 여기선 결과만 받아서 DB에 upsert
    raw_trades = await worker_get(account.id, "/collect", {
        "start_year": start_year, "start_month": start_month, "start_day": start_day,
        "end_year": end_year, "end_month": end_month, "end_day": end_day,
    })
 
    if not raw_trades:
        return {"trades": [], "total": 0, "inserted": 0, "updated": 0}

    trades: List[Trade] = []

    for t in raw_trades:
        trades.append({
            "ticket": t["ticket"],
            "symbol": t["symbol"],
            "position": t["position"],
            "volume": t["volume"],
            "price_open": t["price_open"],
            "price_close": t["price_close"],
            "profit": t["profit"],
            "open_time": parse_dt(t["open_time"]),
            "close_time": parse_dt(t["close_time"]),
            "commission": t["commission"],
            "account_id": t["account_id"],
            "magic": t.get("magic") or None,
        })

    inserted = 0
    updated = 0

    for i in range(0, len(trades), BATCH_SIZE):
        batch = trades[i:i + BATCH_SIZE]

        # 신규/갱신 개수 계산용: 이 배치 중 이미 DB에 있는 티켓
        # (예전엔 PostgreSQL 전용 xmax로 셌는데, SQLite에서도 되도록 바꿈)
        tickets = [t["ticket"] for t in batch]
        existing = set((await db.execute(select(Trade.ticket).where(Trade.ticket.in_(tickets)))).scalars().all())
        batch_new = sum(1 for t in tickets if t not in existing)
        inserted += batch_new
        updated += len(batch) - batch_new

        stmt = insert(Trade).values(batch)
        stmt = stmt.on_conflict_do_update(
            index_elements=["ticket"],
            set_={
                "volume": stmt.excluded.volume,
                "price_open": stmt.excluded.price_open,
                "price_close": stmt.excluded.price_close,
                "profit": stmt.excluded.profit,
                "open_time": stmt.excluded.open_time,
                "close_time": stmt.excluded.close_time,
                "commission": stmt.excluded.commission,
                "account_id": stmt.excluded.account_id,
                "magic": func.coalesce(stmt.excluded.magic, Trade.magic),
            }
        )

        await db.execute(stmt)

    await db.commit()

    return {
        "trades": trades,
        "total": inserted + updated,
        "inserted": inserted,
        "updated": updated,
    }
 
@router.get("/{trade_id}")
async def get_trades(
    trade_id: int,
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(Trade)
        .options(selectinload(Trade.note))
        .options(selectinload(Trade.images))
        .options(selectinload(Trade.tags).selectinload(TradeTagMap.tag))
        .options(
            selectinload(Trade.account).
            selectinload(Account.broker)  # ← 중첩
        )
        .where(
            Trade.id == trade_id
        )
    )

    trade = result.scalar_one_or_none()
    if trade is None:
        raise HTTPException(404, f"trade not found: {trade_id}")
    return _trade_json(trade)