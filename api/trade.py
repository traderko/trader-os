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
from sqlalchemy import select
from db.upsert import upsert_insert as insert
from services.worker_client import worker_get
from util.parser import parse_dt

router = APIRouter(prefix="/trades")

LOCK_CLOSE_MAGIC = 990099
BATCH_SIZE = 1000  # 11 columns * 1000 = 11000 params, 여유있게 안전

def _iso(dt):
    return dt.isoformat() if dt else None


def _trade_json(t: Trade) -> dict:
    """거래 하나를 JSON으로.
    ORM 객체를 그대로 돌려주면 계좌의 암호화된 비밀번호까지 나가고, 태그도 이름 없이
    연결 정보(tag_id)만 나가서 직접 만든다. 앱(Trade.fromJson)이 읽는 필드 이름은 그대로 유지."""
    acc = t.account
    return {
        "id": t.id, "ticket": t.ticket, "symbol": t.symbol, "position": t.position,
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
                account_id = account.id
            )
            db.add(trade)

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

        if (
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
    else:
        fcm.send(
            f"[{data.entry_type}]",
            f"{data.symbol}",
            json.dumps(payload)
        )

    return {"ok": True}

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