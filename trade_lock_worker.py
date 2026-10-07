# trade_lock_worker.py
#
# 계좌(터미널) 하나당 이 스크립트를 독립 프로세스로 하나씩 띄운다.
# main.py와 같은 폴더(프로젝트 루트)에 위치해야 db/ 등 import가 정상 동작함.
# 예: python trade_lock_worker.py --account-id 1 --port 9001 \
#         --terminal-path "C:\mt5\12345678\terminal64.exe"
#
# 이 프로세스가 해당 계좌의 MT5 연결을 독점 소유한다:
#   1) 백그라운드 asyncio 태스크: 락 조건(연속손절) 계속 감시해서 DB에 기록
#   2) 로컬 HTTP 엔드포인트: 메인 게이트웨이가 포워딩하는 포지션 조회/청산 요청 처리
#
# MT5 python 모듈 호출은 전부 동기(blocking)라, 이벤트 루프를 막지 않도록
# asyncio.to_thread()로 감싸서 실행한다.

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
import uvicorn
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import selectinload, sessionmaker

from api.worker import worker_account_router, worker_chart, worker_trade
import importlib
from db import init_db
from db.models.account import Account
from db.models.trade_lock import TradeLock
from db.session import AsyncSessionLocal
from mt5.mt5Client import Mt5Client
from services.fcm_service import FCMService, init_firebase
from services.settings_service import lock_cfg
from services.trade_lock_service import current_checkin_block, get_session_window

KST = ZoneInfo("Asia/Seoul")
# 연속손절 기준(횟수, 기간)은 관리 화면 설정(lock_cfg)에서 매번 읽음
POLL_INTERVAL_SEC = 5

_mt5 = None
_account_id: int = None
_terminal_path: str = None
_fcm = FCMService()

def _count_consecutive_losses_sync(mt5) -> int:
    """락 감시 전용 - Mt5Client에 없는 로직이라 여기 그대로 둠.
    Mt5Client._get_broker_offset를 재사용해서 KST->브로커 서버시간 변환 적용
    (이전엔 시스템 로컬시간을 그대로 넘겨서 최대 7시간 시차 오차가 있었음)."""
    now_kst = datetime.now(KST)

    since_kst = now_kst - timedelta(hours=lock_cfg()["consec_loss_lookback_hours"])
 
    def _to_broker_time(kst_dt: datetime) -> datetime:
        utc_dt = kst_dt.astimezone(timezone.utc)
        offset = Mt5Client._get_broker_offset(utc_dt)
        return utc_dt + timedelta(hours=offset)
 
    since_broker = _to_broker_time(since_kst)
    to_broker = _to_broker_time(now_kst)
 
    deals = mt5.history_deals_get(since_broker, to_broker)

    if deals is None:
        return 0
    
    closed = [d for d in deals if d.entry in (1, 2)]
    closed.sort(key=lambda d: d.time)
    consec = 0

    for d in reversed(closed):
        pnl = d.profit + d.swap + d.commission

        if pnl < 0:
            consec += 1
        else:
            break

    return consec
 
 
async def _lock_watch_loop(mt5, account_number: str):
    last_block_start = None
    last_session_start = None   # 집중 구간 진입 알림용 (구간이 바뀌면 키가 달라짐)
    first_loop = True           # 워커 (재)시작 직후엔 이미 진행 중인 구간을 다시 알리지 않음
 
    while True:
        session = None
        session_just_ended = False
        try:
            consec = await asyncio.to_thread(_count_consecutive_losses_sync, mt5)

            now = datetime.now(KST)
 
            async with AsyncSessionLocal() as db:
                result = await db.execute(select(TradeLock).where(TradeLock.account_id == _account_id))

                lock = result.scalar_one_or_none()
                
                if lock is None:
                    lock = TradeLock(account_id=_account_id)

                    db.add(lock)

                    await db.commit()
                    await db.refresh(lock)
 
                should_be_active = consec >= lock_cfg()["consec_loss_threshold"]

                if lock.consec_loss_active != should_be_active:
                    lock.consec_loss_active = should_be_active

                    if should_be_active:
                        lock.consec_loss_since = now.replace(tzinfo=None)
                    else:
                        lock.consec_loss_since = None

                    await db.commit()

                    print(f"[{datetime.now()}] account={_account_id} consec_loss_active={should_be_active}")
 
                    if should_be_active:
                        _fcm.send(
                            "거래 잠금 - 연속손절 감지",
                            f"계좌 {account_number}: 최근 {consec}연속 손절로 거래가 잠겼습니다.",
                            '{"type":"trade_lock_consec_loss","account_number":"%s"}' % account_number,
                        )

                # 아시아장 집중 구간(목·금·지표일) 진입 알림
                session = await get_session_window(db, now)
                session_start = (session.start, session.key) if session else None

                if session is not None and session_start != last_session_start and not first_loop:
                    _fcm.send(
                        f"거래 잠금 - {session.title} 집중 구간",
                        f"계좌 {account_number}: {', '.join(session.causes)} {session.title}입니다 "
                        f"(~{session.end.strftime('%H:%M')}). 전용 문구를 입력하면 {session.unlock_minutes}분간 거래할 수 있습니다.",
                        '{"type":"trade_lock_session","account_number":"%s"}' % account_number,
                    )

                # 구간이 끝나면 정기 재확인으로 돌아가며 다시 잠김 (16:00/17:00은 2시간 블록 경계가 아닐 수 있어 따로 알림)
                session_just_ended = session is None and last_session_start is not None and not first_loop
                if session_just_ended:
                    _fcm.send(
                        "거래 잠금 - 아시아장 집중 구간 종료",
                        f"계좌 {account_number}: 집중 구간이 끝났습니다. 정기 재확인 문구를 입력해야 거래할 수 있습니다.",
                        '{"type":"trade_lock_checkin_block","account_number":"%s"}' % account_number,
                    )

                last_session_start = session_start
                first_loop = False
 
            # 새 재확인 블록으로 넘어갔는지 감지 (00-07은 30분, 그 외는 2시간 단위)
            block_start, block_minutes = current_checkin_block(now)

            # 집중 구간 안에서는 2시간 블록이 바뀌어도 잠금 사유가 그대로라 알리지 않음
            if last_block_start is not None and block_start != last_block_start and session is None and not session_just_ended:
                _fcm.send(
                    "거래 잠금 - 재확인 필요",
                    f"계좌 {account_number}: 새 재확인 구간에 진입해 거래가 잠겼습니다 ({block_minutes}분 단위).",
                    '{"type":"trade_lock_checkin_block","account_number":"%s"}' % account_number,
                )

            last_block_start = block_start
 
        except Exception as e:
            print(f"[lock_watch] account={_account_id} 오류: {e}")
 
        await asyncio.sleep(POLL_INTERVAL_SEC)
 

# ── 일일 손실 한도 (services/loss_limit.py) ──────────────────────────
LOSS_POLL_SEC = 2
LOSS_CONFIRM_SEC = 10   # 차단·청산은 이 시간 동안 계속 넘어 있어야 실행 (스프레드가 순간 튀어서 잘못 청산되지 않게)


def _day_flows_sync(mt5, since_kst: datetime) -> float:
    """거래일 시작 이후 입금 - 출금 (기준 평가금 보정용)"""
    def to_broker(kst_dt: datetime) -> datetime:
        utc_dt = kst_dt.astimezone(timezone.utc)
        return utc_dt + timedelta(hours=Mt5Client._get_broker_offset(utc_dt))
    deals = mt5.history_deals_get(to_broker(since_kst), to_broker(datetime.now(KST) + timedelta(minutes=5)))
    if not deals:
        return 0.0
    return sum(d.profit for d in deals if d.type == mt5.DEAL_TYPE_BALANCE)


def _close_all_sync(mt5) -> tuple[int, list[str]]:
    """모든 포지션 시장가 청산. (닫은 개수, 실패 설명 목록)"""
    from services.loss_limit import LOSS_LIMIT_MAGIC
    closed, failed = 0, []
    for p in mt5.positions_get() or []:
        tick = mt5.symbol_info_tick(p.symbol)
        if tick is None:
            failed.append(f"{p.symbol} #{p.ticket} 시세 없음")
            continue
        req = {
            "action": mt5.TRADE_ACTION_DEAL, "position": p.ticket, "symbol": p.symbol, "volume": p.volume,
            "type": mt5.ORDER_TYPE_SELL if p.type == 0 else mt5.ORDER_TYPE_BUY,
            "price": tick.bid if p.type == 0 else tick.ask, "deviation": 50,
            "magic": LOSS_LIMIT_MAGIC, "comment": "daily loss limit",
        }
        res = None
        # 브로커마다 받는 체결 방식이 달라서 차례로 시도 (10030 = 지원하지 않는 방식)
        for filling in (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN):
            res = mt5.order_send({**req, "type_filling": filling})
            if res is None or res.retcode != 10030:
                break
        if res is not None and res.retcode == mt5.TRADE_RETCODE_DONE:
            closed += 1
        else:
            failed.append(f"{p.symbol} #{p.ticket} ({getattr(res, 'retcode', None)} {getattr(res, 'comment', '') if res else mt5.last_error()})")
    return closed, failed


async def _loss_limit_loop(mt5, account_row):
    from services import loss_limit as LL
    from services import trade_lock_service as lock_svc
    import time as _time

    aid, number = account_row.id, account_row.account_number
    flows, flows_at, saved_at = 0.0, 0.0, 0.0
    close_tries = 0
    over_since: dict[int, float] = {}
    while True:
        try:
            now = datetime.now(KST)
            c = LL.cfg()
            info = await asyncio.to_thread(mt5.account_info)
            if info is None:
                await asyncio.sleep(LOSS_POLL_SEC)
                continue
            st = LL.read_state(aid)
            day = LL.trading_day(now)
            if st.get("day") != day:
                # 새 거래일 - 지금 평가금을 기준으로
                st = {"day": day, "start_equity": info.equity, "start_time": now.isoformat(), "stage": 0}
                flows, flows_at, close_tries = 0.0, 0.0, 0
                LL.write_state(aid, st)
                print(f"[loss_limit] {number} {day} 기준 평가금 {info.equity:,.2f}")

            if _time.time() - flows_at >= 60:
                flows = await asyncio.to_thread(_day_flows_sync, mt5, datetime.fromisoformat(st["start_time"]))
                flows_at = _time.time()

            base = st["start_equity"] + flows
            pct = (base - info.equity) / base * 100 if base > 0 else 0.0
            st.update(base=round(base, 2), equity=round(info.equity, 2), pct=round(pct, 2), flows=round(flows, 2))

            raw = LL.stage_for(pct, c) if c["enabled"] else 0
            prev = st.get("stage", 0)
            # 차단·청산 단계는 LOSS_CONFIRM_SEC 동안 계속 넘어 있을 때만 (경고는 바로)
            t = _time.time()
            for lv in (2, 3):
                if raw >= lv:
                    over_since.setdefault(lv, t)
                else:
                    over_since.pop(lv, None)
            level = max([min(raw, 1)] + [lv for lv, t0 in over_since.items() if t - t0 >= LOSS_CONFIRM_SEC])
            limits = " · ".join(f"{label} {c[k]}%" for k, label in (("warn", "경고"), ("block", "차단"), ("close", "청산")) if c[k])
            until = LL.next_day_start(now)

            if level > prev:
                st["stage"] = level
                LL.write_state(aid, st)
                saved_at = _time.time()
                if level >= 2:
                    async with AsyncSessionLocal() as db:
                        await lock_svc.set_loss_limit_lock(db, aid, until)
                if level == 1:
                    _fcm.send("⚠️ 일일 손실 경고",
                              f"계좌 {number}: 오늘 손실 {pct:.1f}% (기준 {base:,.2f} → 지금 {info.equity:,.2f})\n한도: {limits}",
                              '{"type":"daily_loss_warn","account_number":"%s"}' % number)
                elif level == 2:
                    _fcm.send("⛔ 일일 손실 한도 - 새 진입 차단",
                              f"계좌 {number}: 오늘 손실 {pct:.1f}%\n{until.strftime('%m/%d %H:%M')}까지 새로 들어가는 포지션은 바로 청산됩니다. "
                              f"지금 포지션은 그대로 둡니다. (문구로 풀 수 없음)\n한도: {limits}",
                              '{"type":"daily_loss_block","account_number":"%s"}' % number)

            if st.get("stage", 0) >= 3 and close_tries < 5 and await asyncio.to_thread(lambda: bool(mt5.positions_get())):
                close_tries += 1
                closed, failed = await asyncio.to_thread(_close_all_sync, mt5)
                after = await asyncio.to_thread(mt5.account_info)
                eq = after.equity if after else info.equity
                msg = (f"계좌 {number}: 오늘 손실 {pct:.1f}%로 청산 한도를 넘어 포지션 {closed}개를 청산했습니다.\n"
                       f"평가금 {eq:,.2f} · {until.strftime('%m/%d %H:%M')}까지 거래 잠금 (문구로 풀 수 없음)")
                if failed:
                    msg += f"\n청산 실패 {len(failed)}개 - 다시 시도합니다 ({close_tries}/5): " + ", ".join(failed[:5])
                print(f"[loss_limit] {msg}")
                if close_tries == 1 or not failed or close_tries == 5:
                    _fcm.send("🛑 일일 손실 한도 - 전부 청산", msg,
                              '{"type":"daily_loss_close","account_number":"%s"}' % number)

            if _time.time() - saved_at >= 10:     # 화면 표시용 손실률은 10초마다 저장
                LL.write_state(aid, st)
                saved_at = _time.time()
        except Exception as e:
            print(f"[loss_limit] account={aid} 오류: {e}")
        await asyncio.sleep(LOSS_POLL_SEC)


def _remember_chart_symbol(mt5, account_row) -> None:
    """MT5 시작 차트(tradeLock EA가 붙는 차트)에 쓸 종목을 이 계좌의 실제 종목 이름으로 기록.
    브로커마다 금 종목 이름이 달라서(XAUUSD / XAUUSD+ / GOLD) 없는 이름을 넣으면 차트가 안 열리고 EA도 안 붙는다.
    다음에 MT5를 띄울 때 util/generate_terminal_configs.py 가 읽어 씀 (data/chart_symbols.json)."""
    import json
    import os
    from services import symbols as sym_groups
    from util.generate_terminal_configs import GenerateTerminalConfig
    account_number = account_row.account_number
    all_syms = list(mt5.symbols_get() or [])
    names = [s.name for s in all_syms]
    if not names:
        return
    default = GenerateTerminalConfig._chart_symbol(account_row.broker)
    if default in names:
        pick = default            # 지금 쓰는 이름이 있으면 그대로 (바꾸면 예전 차트 정리 규칙이 어긋남)
    else:
        visible = [s.name for s in all_syms if getattr(s, "visible", False)]
        gold = [n for n in names if (sym_groups.group_of(n) or {}).get("key") == "gold"]
        pick = next((n for n in gold if n in visible), None) or (gold[0] if gold else (visible or names)[0])
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "chart_symbols.json")
    try:
        data = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if data.get(str(account_number)) == pick:
        return
    data[str(account_number)] = pick
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + f".{os.getpid()}.tmp"
    json.dump(data, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    print(f"[worker] {account_number} 시작 차트 종목: {pick}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_firebase()  # 이 워커 프로세스 안에서 최초 1회만 실행됨 (lru_cache)
    
    await init_db.init_db()

    import MetaTrader5 as mt5
    global _mt5
    _mt5 = mt5

    # config.ini(작업 스케줄러가 실행)가 이미 로그인/AutoTrading/EA부착을 끝내놓은
    # 상태라고 가정. login/password/server를 다시 넘기면 이미 인증된 세션에
    # 재인증을 강제로 트리거해서 재접속 충돌(retcode 10027류)이 날 수 있으므로
    # path만 사용해서 "이미 떠있는 터미널에 붙기"만 한다.
    ok = False

    for attempt in range(30):
        ok = await asyncio.to_thread(mt5.initialize, path=_terminal_path)

        if ok:
            break

        print(f"[worker] account={_account_id} 터미널 준비 대기중... ({attempt+1}/30)")

        await asyncio.sleep(2)

    if not ok:
        raise RuntimeError(f"MT5 연결 실패 (account_id={_account_id}): {mt5.last_error()}")

    print(f"[worker] account={_account_id} MT5 연결 성공")

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Account)
            .options(selectinload(Account.broker))
            .where(Account.id == _account_id)
        )

        account_row = result.scalar_one_or_none()

    if account_row is None:
        raise RuntimeError(f"DB에 account_id={_account_id} 계좌가 없습니다.")

    # app.state에 셋 다 넣어야 함 (mt5, mt5_client, account_row)
    app.state.mt5 = mt5
    app.state.mt5_client = Mt5Client()
    app.state.fcm = _fcm
    app.state.account_row = account_row

    try:
        _remember_chart_symbol(mt5, account_row)
    except Exception as e:
        print(f"[worker] account={_account_id} 시작 차트 종목 기록 실패: {e}")

    watch_task = asyncio.create_task(_lock_watch_loop(mt5, account_row.account_number))
    loss_task = asyncio.create_task(_loss_limit_loop(mt5, account_row))

    yield

    watch_task.cancel()
    loss_task.cancel()

    await asyncio.to_thread(mt5.shutdown)


app = FastAPI(lifespan=lifespan)

app.include_router(worker_account_router.router)
# 개인용 기능 (파일이 있을 때만 - 공개판에는 없음)
for _name in ("worker_trade_ktr", "worker_trade_report"):
    try:
        app.include_router(importlib.import_module(f"api.worker.{_name}").router)
    except ModuleNotFoundError as _e:
        if _e.name != f"api.worker.{_name}":
            raise
app.include_router(worker_trade.router)
app.include_router(worker_chart.router)

@app.get("/positions")
async def get_positions():
    if _mt5 is None:
        raise HTTPException(503, "MT5 연결 안 됨")
    positions = await asyncio.to_thread(_mt5.positions_get)
    if positions is None:
        return []
    return [
        {
            "ticket": p.ticket, "symbol": p.symbol, "volume": p.volume,
            "type": p.type, "price_open": p.price_open,
            "sl": p.sl, "tp": p.tp, "profit": p.profit,
        }
        for p in positions
    ]


@app.post("/positions/{ticket}/close")
async def close_position(ticket: int):
    if _mt5 is None:
        raise HTTPException(503, "MT5 연결 안 됨")

    def _do_close():
        pos = _mt5.positions_get(ticket=ticket)
        if not pos:
            return None
        p = pos[0]
        order_type = _mt5.ORDER_TYPE_SELL if p.type == 0 else _mt5.ORDER_TYPE_BUY
        tick = _mt5.symbol_info_tick(p.symbol)
        price = tick.bid if p.type == 0 else tick.ask
        request = {
            "action": _mt5.TRADE_ACTION_DEAL, "position": ticket, "symbol": p.symbol,
            "volume": p.volume, "type": order_type, "price": price, "deviation": 30,
        }
        return _mt5.order_send(request)

    result = await asyncio.to_thread(_do_close)
    if result is None:
        raise HTTPException(404, "포지션 없음")
    if result.retcode != _mt5.TRADE_RETCODE_DONE:
        raise HTTPException(400, f"청산 실패: retcode={result.retcode}")
    return {"status": "closed", "ticket": ticket}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--account-id", type=int, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--terminal-path", type=str, required=True)
    args = parser.parse_args()
    _account_id = args.account_id
    _terminal_path = args.terminal_path
    uvicorn.run(app, host="127.0.0.1", port=args.port)