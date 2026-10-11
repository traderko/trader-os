# services/guidelines.py
#
# 트레이딩 지침 - 정해진 시간마다가 아니라, 그 상황이 생긴 순간에 맞는 지침을 텔레그램(+앱 푸시)으로 보낸다.
# 설정: 관리 화면 '지침' 탭 → data/settings.json 의 "guidelines" (규칙마다 켜기·끄기, 문구, 기준값)
#
#   진입 체결 때 (api/trade.py → on_open)
#     revenge      손실로 청산한 뒤 N분 안에 다시 진입
#     overtrade    최근 M분 동안 진입이 N번 이상
#     size_jump    이번 진입(2분 안에 나눠 들어간 것 합산)이 평소 진입 크기(최근 N번의 중앙값)의 K배 이상
#     asia_counter 아시아장 시간에 진입 - 정한 요일(기본 목·금) 또는 고영향 지표가 있는 날
#     first_entry  그날(06:00 기준) 첫 진입 - 지침 목록에서 무작위 몇 개
#   주기 확인 (loop, 20초마다)
#     no_sl        진입하고 N초가 지나도 손절(SL)이 없는 포지션
#     event_hold   고영향 지표 발표·미국장 개장 N분 전인데 포지션을 들고 있음
#   워커 (trade_lock_worker.py)
#     loss_warn    일일 손실 한도 '경고' 단계 알림에 지침을 덧붙임
#
# 같은 규칙·계좌(·종목) 알림은 cooldown_min 동안 다시 보내지 않는다.

import asyncio
import random
import statistics
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from services.settings_service import get_settings

KST = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")
ENTRY_GROUP_SEC = 120          # 이 안에 같은 방향으로 나눠 들어간 건 한 번의 진입으로 봄 (통계 탭과 같은 기준)
LOOP_SEC = 20

# 관리 화면이 그리는 규칙 설명 (params: 숫자·요일 등 고칠 수 있는 값)
RULES = [
    {"key": "revenge", "label": "손절 직후 재진입", "desc": "손실로 청산한 뒤 정한 시간 안에 다시 진입하면",
     "params": [{"k": "minutes", "label": "분 안에", "type": "int"}]},
    {"key": "overtrade", "label": "과매매", "desc": "정한 시간 동안 진입이 정한 횟수 이상이면",
     "params": [{"k": "minutes", "label": "분 동안", "type": "int"}, {"k": "count", "label": "번 이상", "type": "int"}]},
    {"key": "size_jump", "label": "비중 급증", "desc": "이번 진입이 평소 진입 크기(최근 진입들의 중앙값)보다 크게 들어가면 · 2분 안에 나눠 들어간 건 합산",
     "params": [{"k": "ratio", "label": "배 이상", "type": "float"}, {"k": "lookback", "label": "최근 진입 수로 평소 크기 계산", "type": "int"}]},
    {"key": "asia_counter", "label": "아시아장 역매매", "desc": "아시아장 집중 구간 시간에 진입하면 - 정한 요일 또는 고영향 지표가 있는 날",
     "params": [{"k": "weekdays", "label": "요일", "type": "weekdays"}, {"k": "event_days", "label": "지표 발표일 포함", "type": "bool"}]},
    {"key": "no_sl", "label": "손절 미설정", "desc": "진입하고 정한 시간이 지나도 손절(SL)이 없으면",
     "params": [{"k": "seconds", "label": "초 뒤", "type": "int"}]},
    {"key": "event_hold", "label": "지표·미장 전 포지션 보유", "desc": "고영향 지표 발표(또는 미국장 개장) 정한 시간 전인데 포지션을 들고 있으면",
     "params": [{"k": "minutes", "label": "분 전", "type": "int"}, {"k": "us_open", "label": "미국장 개장도", "type": "bool"}]},
    {"key": "loss_warn", "label": "일일 손실 경고", "desc": "일일 손실 한도 '경고' 단계 알림에 덧붙임 (잠금 규칙 탭의 일일 손실 한도가 켜져 있어야 함)",
     "params": []},
    {"key": "first_entry", "label": "그날 첫 진입", "desc": "거래일(06:00 기준) 첫 진입 때 지침 목록에서 무작위로 · 문구를 비워 두면 목록에서 고름",
     "params": [{"k": "count", "label": "개", "type": "int"}]},
]

_sent: dict[tuple, float] = {}        # (규칙, 계좌, 세부) → 마지막으로 보낸 시각
_first_seen: dict[tuple, float] = {}  # (계좌, 티켓) → 처음 본 시각 (no_sl)


def cfg() -> dict:
    return get_settings().get("guidelines") or {}


def rule(key: str) -> dict | None:
    """켜져 있는 규칙만 (전체 끄기 포함)"""
    c = cfg()
    if not c.get("enabled", True):
        return None
    r = (c.get("rules") or {}).get(key)
    return r if r and r.get("enabled") else None


def rule_text(key: str) -> str | None:
    r = rule(key)
    return (r.get("text") or "").strip() if r else None


def _cooldown_ok(key: tuple) -> bool:
    cd = int(cfg().get("cooldown_min", 30)) * 60
    now = time.time()
    if now - _sent.get(key, 0) < cd:
        return False
    _sent[key] = now
    return True


def send(text: str, context: str = "", title: str = "📌 트레이딩 지침") -> None:
    body = text.strip() + (f"\n\n{context.strip()}" if context.strip() else "")
    try:
        from services.fcm_service import get_fcm_service
        get_fcm_service().send(title, body, '{"type":"guideline"}')
    except Exception as e:
        print(f"[guideline] 보내기 실패: {e}")


def fire(key: str, account_id: int, detail: str, context: str, text: str | None = None) -> bool:
    r = rule(key)
    if not r:
        return False
    if not _cooldown_ok((key, account_id, detail)):
        return False
    send(text if text is not None else r.get("text") or "", context)
    print(f"[guideline] {key} 계좌{account_id} {detail}")
    return True


# ── 진입 체결 ──────────────────────────────────────────────────────
async def on_open(account_id: int, account_number: str, symbol: str, side: str, volume: float, kst_time: datetime) -> None:
    """진입 체결 기록 직후 (api/trade.py 가 뒤에서 부름). 실패해도 체결 기록에는 영향 없음."""
    if not cfg().get("enabled", True):
        return
    try:
        await _on_open(account_id, account_number, symbol, side, volume, kst_time)
    except Exception as e:
        print(f"[guideline] 진입 확인 실패: {e}")


async def _on_open(account_id, account_number, symbol, side, volume, now):
    from sqlalchemy import func, select
    from db.models.trade import Trade
    from db.session import AsyncSessionLocal

    acc = f"계좌 {account_number}"
    async with AsyncSessionLocal() as db:
        # revenge: 직전 청산이 손실이고 N분 안
        r = rule("revenge")
        if r:
            last = (await db.execute(
                select(Trade).where(Trade.account_id == account_id, Trade.close_time != None, Trade.close_time <= now)
                .order_by(Trade.close_time.desc()).limit(1))).scalar_one_or_none()
            if last and last.close_time:
                net = (last.profit or 0) + (last.commission or 0)
                mins = (now - last.close_time).total_seconds() / 60
                if net < 0 and mins <= r["minutes"]:
                    fire("revenge", account_id, "", f"{acc} · {last.symbol} {net:+,.2f} 손실 청산 {max(1, round(mins))}분 만에 {symbol} {side} 진입")

        # overtrade: 최근 M분 진입 수
        r = rule("overtrade")
        if r:
            n = (await db.execute(select(func.count()).select_from(Trade).where(
                Trade.account_id == account_id, Trade.open_time != None,
                Trade.open_time > now - timedelta(minutes=r["minutes"]), Trade.open_time <= now))).scalar() or 0
            if n >= r["count"]:
                fire("overtrade", account_id, "", f"{acc} · 최근 {r['minutes']}분 동안 진입 {n}번")

        # size_jump: 2분 안에 나눠 들어간 것 합산 → 평소(최근 진입들의 중앙값)와 비교
        r = rule("size_jump")
        if r:
            rows = (await db.execute(
                select(Trade.open_time, Trade.volume, Trade.position).where(
                    Trade.account_id == account_id, Trade.symbol == symbol, Trade.open_time != None,
                    Trade.open_time <= now, Trade.open_time > now - timedelta(days=90))
                .order_by(Trade.open_time))).all()
            groups: list[list] = []      # [시작시각, 방향, 합계]
            for t, v, pos in rows:
                if groups and groups[-1][1] == pos and (t - groups[-1][0]).total_seconds() <= ENTRY_GROUP_SEC:
                    groups[-1][2] += v or 0
                else:
                    groups.append([t, pos, v or 0])
            if len(groups) >= 6:
                cur = groups[-1][2]
                past = [g[2] for g in groups[:-1]][-r["lookback"]:]
                usual = statistics.median(past)
                if usual > 0 and cur >= usual * r["ratio"]:
                    fire("size_jump", account_id, symbol,
                         f"{acc} · {symbol} 평소 {usual:g}lot → 이번 {cur:g}lot ({cur / usual:.1f}배)")

        # asia_counter: 아시아장 구간 시간 + (정한 요일 또는 지표일)
        r = rule("asia_counter")
        if r:
            from services import trade_lock_service as lock_svc
            from services.settings_service import lock_cfg
            sess = next((x for x in lock_cfg()["sessions"] if x.get("id") == "asia"), None)
            start_min, end_min = (sess["start_min"], sess["end_min"]) if sess else (60, 600)
            wall = lock_svc.to_broker_wallclock(now)
            minute = wall.hour * 60 + wall.minute
            if start_min <= minute < end_min:
                why = []
                if wall.weekday() in r.get("weekdays", []):
                    why.append(["월", "화", "수", "목", "금", "토", "일"][wall.weekday()] + "요일")
                if r.get("event_days"):
                    ev = await lock_svc.event_names_on(db, wall.date())
                    if ev:
                        why.append("지표: " + ", ".join(ev[:2]) + (" 외" if len(ev) > 2 else ""))
                if why:
                    fire("asia_counter", account_id, "", f"{acc} · 아시아장 {symbol} {side} 진입 ({' · '.join(why)})")

        # first_entry: 거래일(06:00) 첫 진입
        r = rule("first_entry")
        if r:
            from services.loss_limit import day_start
            start = day_start(now)
            n = (await db.execute(select(func.count()).select_from(Trade).where(
                Trade.account_id == account_id, Trade.open_time >= start, Trade.open_time <= now))).scalar() or 0
            if n <= 1:
                text = (r.get("text") or "").strip()
                if not text:
                    items = cfg().get("items") or []
                    pick = random.sample(items, min(int(r.get("count", 2)), len(items))) if items else []
                    text = "\n".join(f"□ {x}" for x in pick)
                if text:
                    fire("first_entry", account_id, start.date().isoformat(), f"{acc} · 오늘 첫 진입 ({symbol} {side})", text)


# ── 주기 확인 (SL 없음, 지표·미장 전 보유) ──────────────────────────
async def loop() -> None:
    await asyncio.sleep(30)
    while True:
        try:
            if cfg().get("enabled", True) and (rule("no_sl") or rule("event_hold")):
                await _check_positions()
        except Exception as e:
            print(f"[guideline] 주기 확인 실패: {e}")
        await asyncio.sleep(LOOP_SEC)


def _us_open_after(now: datetime) -> datetime:
    """지금 이후 가장 가까운 미국장 개장 (평일 09:30 뉴욕 시간, 서머타임 자동)"""
    ny = now.astimezone(NY)
    d = ny.date()
    for i in range(8):
        day = d + timedelta(days=i)
        if day.weekday() < 5:
            t = datetime(day.year, day.month, day.day, 9, 30, tzinfo=NY)
            if t > now:
                return t.astimezone(KST)
    return now + timedelta(days=7)


async def _check_positions() -> None:
    from sqlalchemy import select
    from db.models.account import Account
    from db.models.economic_event import EconomicEvent
    from db.session import AsyncSessionLocal
    from services.worker_client import worker_get

    now = datetime.now(KST)
    async with AsyncSessionLocal() as db:
        accounts = (await db.execute(select(Account).where(Account.lock_enabled == True))).scalars().all()
        upcoming = []
        r_ev = rule("event_hold")
        if r_ev:
            horizon = now + timedelta(minutes=r_ev["minutes"])
            rows = (await db.execute(select(EconomicEvent).where(
                EconomicEvent.event_time > now, EconomicEvent.event_time <= horizon).order_by(EconomicEvent.event_time))).scalars().all()
            seen = set()
            for e in rows:
                if e.event_time in seen:          # 같은 시각 einfomax·FF 중복
                    continue
                seen.add(e.event_time)
                upcoming.append((e.event_time.astimezone(KST), e.event_name.replace("[FF] ", "")))
            if r_ev.get("us_open"):
                uo = _us_open_after(now)
                if uo <= horizon:
                    upcoming.append((uo, "미국장 개장"))

    r_sl = rule("no_sl")
    alive = set()
    for a in accounts:
        try:
            positions = await asyncio.wait_for(worker_get(a.id, "/positions-all"), timeout=5)
        except Exception:
            continue
        acc = f"계좌 {a.account_number}"
        if r_sl:
            for p in positions:
                k = (a.id, p["ticket"])
                alive.add(k)
                first = _first_seen.setdefault(k, time.time())
                if not p.get("sl") and time.time() - first >= r_sl["seconds"]:
                    side = "BUY" if p.get("type") == 0 else "SELL"
                    fire("no_sl", a.id, str(p["ticket"]),
                         f"{acc} · {p['symbol']} {side} {p['volume']:g}lot 진입 {int((time.time() - first) // 60) or 1}분째 손절(SL) 없음")
        if upcoming and positions:
            for at, name in upcoming:
                mins = max(1, round((at - now).total_seconds() / 60))
                syms = ", ".join(sorted({p["symbol"] for p in positions}))
                fire("event_hold", a.id, f"{name}|{at.isoformat()}",
                     f"{acc} · {at.strftime('%H:%M')} {name} ({mins}분 뒤) · 보유 중: {syms} {len(positions)}개")
    # 없어진 포지션은 잊음
    for k in list(_first_seen):
        if k not in alive and r_sl:
            _first_seen.pop(k, None)


# ── 지침 → 확인 문구 ────────────────────────────────────────────────
async def add_items_as_phrases(db) -> dict:
    """지침 목록을 정기 재확인 문구(checkin)로 넣음. 이미 있는 문구는 건너뜀"""
    from sqlalchemy import select
    from db.models.trade_lock_phrase import TradeLockPhrase
    added = skipped = 0
    for text in cfg().get("items") or []:
        row = (await db.execute(select(TradeLockPhrase).where(TradeLockPhrase.phrase == text))).scalar_one_or_none()
        if row is None:
            db.add(TradeLockPhrase(phrase=text, phrase_type="checkin", is_active=True))
            added += 1
        elif not row.is_active:
            row.is_active, row.phrase_type = True, "checkin"
            added += 1
        else:
            skipped += 1
    await db.commit()
    return {"added": added, "skipped": skipped}
