# services/guidelines.py
#
# 트레이딩 지침 - 정해진 시간마다가 아니라, 그 상황이 생긴 순간에 맞는 지침을 텔레그램(+앱 푸시)으로 보낸다.
# 설정: 관리 화면 '지침' 탭 → data/settings.json 의 "guidelines" (규칙마다 켜기·끄기, 문구, 기준값)
#
#   진입 체결 때 (api/trade.py → on_open)
#     ※ '한 번의 진입' = 같은 종목·방향 체결이 직전 체결에서 entry_gap_sec(기본 60초) 안에 이어진 것 전부
#        (0.5랏씩 다다다 들어가도 하나로 - 이어지는 동안은 시간이 길어져도 계속 묶임)
#     revenge      손실로 청산한 뒤 N분 안에 다시 진입
#     overtrade    최근 M분 동안 진입(위 기준으로 묶은 것)이 N번 이상
#     size_jump    이번 진입(묶은 것 합산)이 평소 진입 크기(최근 N번의 중앙값)의 K배 이상
#     margin_use   진입하자마자 증거금 사용률이 단계를 넘었으면 '비중 경고'로 바로 (주기 확인보다 먼저, 이번 진입 크기와 같이)
#     asia_counter 아시아장 시간에 진입 - 정한 요일(기본 목·금) 또는 고영향 지표가 있는 날
#     first_entry  그날(06:00 기준) 첫 진입 - 지침 목록에서 무작위 몇 개
#   주기 확인 (loop, 20초마다)
#     no_sl        진입하고 N초가 지나도 손절(SL)이 없는 포지션 - 포지션마다 한 번 (repeat 분마다 다시, 0 = 한 번만)
#                  같은 계좌에서 한꺼번에 걸린 포지션들은 메시지 하나로 모아서
#     event_hold   고영향 지표 발표·장 시작(아시아·유로·미국) N분 전인데 포지션을 들고 있음
#     margin_use   증거금 사용률(사용 증거금 ÷ 평가금)이 정한 단계(예: 10·20·30%)를 넘음 - 단계마다 한 번,
#                  그 단계 아래로 내려갔다가 다시 넘으면 또 (repeat 분마다 다시, 0 = 한 번만)
#     random_items 지침 목록에서 무작위 시각(gap_min~gap_max 분 간격)마다 N개 - 정한 시간대·요일만
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
def entry_gap() -> int:
    """직전 체결에서 이 시간(초) 안에 같은 종목·방향으로 이어진 체결은 한 번의 진입"""
    return int(cfg().get("entry_gap_sec", 60) or 60)


def entry_groups(rows, gap: int | None = None) -> list[list]:
    """(시각, 종목, 방향, 랏) 들 → [처음 시각, 마지막 시각, 종목, 방향, 합계 랏, 체결 수] (시각 순)
    같은 종목·방향의 직전 체결과 gap 초 안이면 이어 붙임 - 다른 종목이 사이에 끼어도 됨"""
    gap = entry_gap() if gap is None else gap
    open_g: dict[tuple, list] = {}
    out: list[list] = []
    for t, sym, pos, v in sorted(rows, key=lambda x: x[0]):
        k = (sym, pos)
        g = open_g.get(k)
        if g is not None and (t - g[1]).total_seconds() <= gap:
            g[1] = t
            g[4] += v or 0
            g[5] += 1
        else:
            g = [t, t, sym, pos, v or 0, 1]
            open_g[k] = g
            out.append(g)
    return out
LOOP_SEC = 20

# 관리 화면이 그리는 규칙 설명 (params: 숫자·요일 등 고칠 수 있는 값)
RULES = [
    {"key": "revenge", "label": "손절 직후 재진입", "desc": "손실로 청산한 뒤 정한 시간 안에 다시 진입하면",
     "params": [{"k": "minutes", "label": "분 안에", "type": "int"}]},
    {"key": "overtrade", "label": "과매매", "desc": "정한 시간 동안 진입이 정한 횟수 이상이면 · 나눠 들어간 건 위 '나눠 진입 묶기' 기준으로 한 번",
     "params": [{"k": "minutes", "label": "분 동안", "type": "int"}, {"k": "count", "label": "번 이상", "type": "int"}]},
    {"key": "size_jump", "label": "비중 급증", "desc": "이번 진입이 평소 진입 크기(최근 진입들의 중앙값)보다 크게 들어가면 · 나눠 들어간 건 위 '나눠 진입 묶기' 기준으로 합산",
     "params": [{"k": "ratio", "label": "배 이상", "type": "float"}, {"k": "lookback", "label": "최근 진입 수로 평소 크기 계산", "type": "int"}]},
    {"key": "asia_counter", "label": "아시아장 역매매", "desc": "아시아장 집중 구간 시간에 진입하면 - 정한 요일 또는 고영향 지표가 있는 날",
     "params": [{"k": "weekdays", "label": "요일", "type": "weekdays"}, {"k": "event_days", "label": "지표 발표일 포함", "type": "bool"}]},
    {"key": "no_sl", "label": "손절 미설정", "desc": "진입하고 정한 시간이 지나도 손절(SL)이 없으면 · 포지션마다 한 번, 여러 개면 한 메시지로 모아서",
     "params": [{"k": "seconds", "label": "초 뒤", "type": "int"},
                {"k": "repeat", "label": "분마다 다시 (SL 걸 때까지 · 0 = 한 번만)", "type": "int"}]},
    {"key": "event_hold", "label": "지표·장 시작 전 포지션 보유",
     "desc": "고영향 지표 발표(또는 체크한 장 시작) 정한 시간 전인데 포지션을 들고 있으면 · 장 시작은 서머타임 자동 "
             "(아시아 07:00/08:00 · 유로 16:00/17:00 · 미국 22:30/23:30, 서머타임/아닐 때)",
     "params": [{"k": "minutes", "label": "분 전", "type": "int"},
                {"k": "repeat", "label": "분마다 다시 (정리할 때까지 · 0 = 한 번만)", "type": "int"},
                {"k": "asia_open", "label": "아시아장 시작", "type": "bool"},
                {"k": "euro_open", "label": "유로장 시작", "type": "bool"}, {"k": "us_open", "label": "미국장 개장", "type": "bool"}]},
    {"key": "margin_use", "label": "증거금 경고",
     "desc": "증거금 사용률(사용 증거금 ÷ 평가금, 실시간 현황의 '증거금 %')이 정한 단계를 넘으면 · 진입으로 넘었으면 체결 즉시 '비중 경고'로 (이번 진입 크기와 같이) · 단계마다 한 번, 내려갔다 다시 넘으면 또",
     "params": [{"k": "levels", "label": "% 단계 (쉼표로)", "type": "levels"},
                {"k": "repeat", "label": "분마다 다시 (그 단계 위에 있는 동안 · 0 = 한 번만)", "type": "int"}]},
    {"key": "loss_warn", "label": "일일 손실 경고", "desc": "일일 손실 한도 '경고' 단계 알림에 덧붙임 (잠금 규칙 탭의 일일 손실 한도가 켜져 있어야 함)",
     "params": []},
    {"key": "random_items", "label": "무작위 지침", "notext": True,
     "desc": "지침 목록에서 무작위 시각마다 몇 개씩 · 정한 시간대·요일에만 · 목록을 한 바퀴 다 돌 때까지 같은 지침은 다시 안 나옴",
     "params": [{"k": "count", "label": "개씩", "type": "int"},
                {"k": "gap_min", "label": "분 ~", "type": "int"}, {"k": "gap_max", "label": "분 사이 무작위 간격", "type": "int"},
                {"k": "start", "label": "부터", "type": "hhmm"}, {"k": "end", "label": "까지", "type": "hhmm"},
                {"k": "weekdays", "label": "요일", "type": "weekdays"}]},
    {"key": "first_entry", "label": "그날 첫 진입", "desc": "거래일(06:00 기준) 첫 진입 때 지침 목록에서 무작위로 · 문구를 비워 두면 목록에서 고름",
     "params": [{"k": "count", "label": "개", "type": "int"}]},
]

_sent: dict[tuple, float] = {}        # (규칙, 계좌, 세부) → 마지막으로 보낸 시각
_first_seen: dict[tuple, float] = {}  # (계좌, 티켓) → 처음 본 시각 (no_sl)
_sl_sent: dict[tuple, float] = {}     # (계좌, 티켓) → SL 없음 알림을 마지막으로 보낸 시각 (no_sl)
_hold_count: dict[tuple, int] = {}    # (계좌, 지표·장|시각) → 보낸 횟수 (event_hold 반복)
_rand_next: dict = {"at": None, "sig": None}   # 무작위 지침: 다음에 보낼 시각 (설정이 바뀌면 다시 잡음)
_rand_bag: list[str] = []                       # 아직 안 보낸 지침 (다 쓰면 섞어서 다시)
_margin_lvl: dict[int, tuple] = {}    # 계좌 → (알린 단계 %, 마지막으로 보낸 시각) (margin_use)
MARGIN_HYST = 1.0                     # 단계 아래로 이만큼(%p) 더 내려가야 '내려갔다'로 봄 (경계에서 왔다갔다 할 때 알림 폭주 방지)


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


def _cooldown_ok(key: tuple, cd: float | None = None) -> bool:
    if cd is None:
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


def fire(key: str, account_id: int, detail: str, context: str, text: str | None = None,
         cooldown_sec: float | None = None, title: str | None = None) -> bool:
    r = rule(key)
    if not r:
        return False
    if not _cooldown_ok((key, account_id, detail), cooldown_sec):
        return False
    send(text if text is not None else r.get("text") or "", context, **({"title": title} if title else {}))
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

        # 최근 체결들을 '한 번의 진입' 단위로 묶음 (overtrade · size_jump · 비중 경고가 같이 씀)
        rows = (await db.execute(
            select(Trade.open_time, Trade.symbol, Trade.position, Trade.volume).where(
                Trade.account_id == account_id, Trade.open_time != None,
                Trade.open_time <= now, Trade.open_time > now - timedelta(days=90))
            .order_by(Trade.open_time))).all()
        groups = entry_groups(rows)
        mine = [g for g in groups if g[2] == symbol]                 # 이 종목 진입들
        this = mine[-1] if mine else None                            # 이번 진입 (방금 체결이 붙은 묶음)

        # overtrade: 최근 M분 동안 진입 수 (묶은 단위)
        r = rule("overtrade")
        if r:
            since = now - timedelta(minutes=r["minutes"])
            n = sum(1 for g in groups if g[1] > since)
            if n >= r["count"]:
                fire("overtrade", account_id, "", f"{acc} · 최근 {r['minutes']}분 동안 진입 {n}번"
                     + (f" (체결 {sum(g[5] for g in groups if g[1] > since)}건)" if any(g[5] > 1 for g in groups if g[1] > since) else ""))

        # size_jump: 이번 진입(나눠 들어간 것 합산) → 평소(이 종목 최근 진입들의 중앙값)와 비교
        r = rule("size_jump")
        if r and this and len(mine) >= 6:
            cur = this[4]
            past = [g[4] for g in mine[:-1]][-r["lookback"]:]
            usual = statistics.median(past)
            if usual > 0 and cur >= usual * r["ratio"]:
                fire("size_jump", account_id, symbol,
                     f"{acc} · {symbol} 평소 {usual:g}lot → 이번 {cur:g}lot ({cur / usual:.1f}배"
                     + (f" · {this[5]}번 나눠 진입)" if this[5] > 1 else ")"))

    # 비중 경고: 이번 진입으로 증거금 사용률이 단계를 넘었으면 바로 (주기 확인과 같은 기록이라 두 번 오지 않음)
    r = rule("margin_use")
    if r:
        from types import SimpleNamespace
        what = f"{symbol} {side} {this[4]:g}lot" + (f" ({this[5]}번 나눠)" if this[5] > 1 else "") if this else f"{symbol} {side} {volume:g}lot"
        await _check_margin(SimpleNamespace(id=account_id), acc, r, entry=what)

    async with AsyncSessionLocal() as db:
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


# ── 주기 확인 (SL 없음, 지표·장 시작 전 보유) ──────────────────────────
async def loop() -> None:
    await asyncio.sleep(30)
    while True:
        try:
            if cfg().get("enabled", True) and (rule("no_sl") or rule("event_hold") or rule("margin_use")):
                await _check_positions()
        except Exception as e:
            print(f"[guideline] 주기 확인 실패: {e}")
        try:
            _tick_random()
        except Exception as e:
            print(f"[guideline] 무작위 지침 실패: {e}")
        await asyncio.sleep(LOOP_SEC)


# 장 시작 시각 - 브로커 서버시간(뉴욕 + 7시간, 서머타임 자동) 기준, 브로커 날짜 월~금
#   아시아 01:00 → KST 07:00 (서머타임) / 08:00
#   유로   10:00 → KST 16:00 / 17:00
#   미국   16:30 → KST 22:30 / 23:30 (= 뉴욕 09:30)
MARKET_OPENS = [("asia_open", "아시아장 시작", 1, 0), ("euro_open", "유로장 시작", 10, 0), ("us_open", "미국장 개장", 16, 30)]


def _open_after(now: datetime, hh: int, mm: int) -> datetime:
    """지금 이후 가장 가까운 장 시작 (브로커 시간 hh:mm, 브로커 날짜 평일)"""
    b_now = now.astimezone(NY).replace(tzinfo=None) + timedelta(hours=7)
    for i in range(9):
        day = (b_now + timedelta(days=i)).date()
        if day.weekday() >= 5:
            continue
        t = (datetime(day.year, day.month, day.day, hh, mm) - timedelta(hours=7)).replace(tzinfo=NY)
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
                from services.key_events import star
                upcoming.append((e.event_time.astimezone(KST), star(e.event_name.replace("[FF] ", ""))))
            for key, name, hh, mm in MARKET_OPENS:
                if r_ev.get(key, True):
                    at = _open_after(now, hh, mm)
                    if at <= horizon:
                        upcoming.append((at, name))

    r_sl, r_mg = rule("no_sl"), rule("margin_use")
    alive, checked = set(), set()
    for a in accounts:
        try:
            positions = await asyncio.wait_for(worker_get(a.id, "/positions-all"), timeout=5)
        except Exception:
            continue
        checked.add(a.id)
        acc = f"계좌 {a.account_number}"
        if r_mg:
            await _check_margin(a, acc, r_mg)
        if r_sl:
            # 포지션마다 한 번만 (repeat 분이 있으면 SL 걸 때까지 그 간격으로 다시). 걸린 포지션들은 한 메시지로
            rep_sec = int(r_sl.get("repeat", 0) or 0) * 60
            now_ts, due, soon = time.time(), [], []
            for p in positions:
                k = (a.id, p["ticket"])
                alive.add(k)
                first = _first_seen.setdefault(k, now_ts)
                if p.get("sl"):
                    _sl_sent.pop(k, None)          # SL 걸었다가 다시 빼면 다시 알림
                    continue
                if now_ts - first < r_sl["seconds"]:
                    continue
                last = _sl_sent.get(k)
                if last is None or (rep_sec and now_ts - last >= rep_sec - 5):
                    due.append((p, first))
                elif rep_sec and now_ts - last >= rep_sec / 2:
                    soon.append((p, first))
            if due and soon:                       # 다시 알릴 때 곧 차례가 올 포지션도 같이 - 메시지가 따로따로 오지 않게
                due = sorted(due + soon, key=lambda x: x[1])
            if due:
                def line(p, first):
                    side = "BUY" if p.get("type") == 0 else "SELL"
                    return f"{p['symbol']} {side} {p['volume']:g}lot 진입 {int((now_ts - first) // 60) or 1}분째"
                if len(due) == 1:
                    ctx = f"{acc} · {line(*due[0])} 손절(SL) 없음"
                else:
                    ctx = (f"{acc} · 손절(SL) 없는 포지션 {len(due)}개\n" + "\n".join(f"· {line(p, f)}" for p, f in due[:10])
                           + (f"\n· 외 {len(due) - 10}개" if len(due) > 10 else ""))
                ctx += "\n(" + (f"SL을 걸 때까지 {rep_sec // 60}분마다 다시 알림" if rep_sec else "같은 포지션은 다시 알리지 않음") + ")"
                if fire("no_sl", a.id, "batch", ctx, cooldown_sec=0):
                    for p, _ in due:
                        _sl_sent[(a.id, p["ticket"])] = now_ts
        if upcoming and positions:
            for at, name in upcoming:
                mins = max(1, round((at - now).total_seconds() / 60))
                syms = ", ".join(sorted({p["symbol"] for p in positions}))
                # repeat 분마다 다시 (정리할 때까지) · 0 이면 한 번만
                rep = int(r_ev.get("repeat", 10) or 0)
                dkey = (a.id, f"{name}|{at.isoformat()}")
                n = _hold_count.get(dkey, 0)
                if fire("event_hold", a.id, dkey[1],
                        f"{acc} · {at.strftime('%H:%M')} {name} ({mins}분 뒤) · 보유 중: {syms} {len(positions)}개"
                        + (f"\n⏰ 아직 정리 안 됨 ({n + 1}번째 알림)" if n else ""),
                        cooldown_sec=rep * 60 - 5 if rep else 86400):
                    _hold_count[dkey] = n + 1
    # 지난 지표·장 시작 반복 횟수는 잊음
    for k in [k for k in _hold_count if k[1].split("|", 1)[1] < (now - timedelta(hours=1)).isoformat()]:
        _hold_count.pop(k, None)
    # 없어진 포지션은 잊음 (이번에 포지션을 받아 온 계좌만 - 잠깐 못 받아 온 계좌의 기록은 그대로)
    if r_sl:
        for d in (_first_seen, _sl_sent):
            for k in [k for k in d if k[0] in checked and k not in alive]:
                d.pop(k, None)


# ── 무작위 지침 ──────────────────────────────────────────────────────
def _hm(v: str) -> int:
    h, m = str(v).split(":")[:2]
    return int(h) * 60 + int(m)


def _in_window(t: datetime, r: dict) -> bool:
    """시간대(자정을 넘겨도 됨, 예 22:00~02:00)·요일 안인지. 자정을 넘긴 시간대의 새벽은 전날 요일로 봄"""
    s, e, m = _hm(r.get("start", "08:00")), _hm(r.get("end", "23:00")), t.hour * 60 + t.minute
    if s == e:
        inside, day = True, t
    elif s < e:
        inside, day = s <= m < e, t
    else:
        inside, day = (m >= s or m < e), (t - timedelta(days=1) if m < e else t)
    wd = r.get("weekdays")
    return inside and (not wd or day.weekday() in wd)


def _next_slot(after: datetime, r: dict) -> datetime:
    """after 에서 무작위 간격 뒤 - 시간대 밖이면 다음 시간대 시작 + 조금 (바로 시작 시각에 딱 맞춰 오지 않게)"""
    lo, hi = int(r.get("gap_min", 60)), int(r.get("gap_max", 180))
    t = after + timedelta(minutes=random.uniform(lo, max(lo, hi)))
    if _in_window(t, r):
        return t
    for i in range(1, 8 * 24 * 4):          # 15분씩 앞으로 가며 다음 시간대 찾기 (최대 8일)
        c = t + timedelta(minutes=15 * i)
        if _in_window(c, r):
            return c + timedelta(minutes=random.uniform(0, min(lo, 30)))
    return t


def _pick_items(n: int) -> list[str]:
    items = [x for x in (cfg().get("items") or []) if x.strip()]
    global _rand_bag
    _rand_bag = [x for x in _rand_bag if x in items]        # 목록에서 지운 지침은 빼기
    out = []
    while len(out) < min(n, len(items)):
        if not _rand_bag:
            _rand_bag = random.sample(items, len(items))
        x = _rand_bag.pop()
        if x not in out:
            out.append(x)
    return out


def _tick_random() -> None:
    r = rule("random_items")
    if not r:
        _rand_next.update(at=None, sig=None)
        return
    now = datetime.now(KST)
    sig = tuple(str(r.get(k)) for k in ("gap_min", "gap_max", "start", "end", "weekdays"))
    if _rand_next["at"] is None or _rand_next["sig"] != sig:     # 처음이거나 설정을 바꿨으면 다시 잡음
        _rand_next.update(at=_next_slot(now, r), sig=sig)
        print(f"[guideline] 무작위 지침 다음: {_rand_next['at']:%m-%d %H:%M}")
        return
    if now < _rand_next["at"]:
        return
    if _in_window(now, r):
        pick = _pick_items(int(r.get("count", 1)))
        if pick:
            send("\n".join(f"□ {x}" for x in pick))
            print(f"[guideline] random_items {len(pick)}개")
    _rand_next["at"] = _next_slot(now, r)


async def _check_margin(a, acc: str, r: dict, entry: str | None = None) -> None:
    """증거금 사용률이 단계를 넘으면 경고 (계좌마다 알린 단계를 기억 - 같은 단계는 한 번)"""
    from services.worker_client import worker_get
    try:
        info = await asyncio.wait_for(worker_get(a.id, "/account-info"), timeout=5)
    except Exception:
        return
    eq, used = float(info.get("equity") or 0), float(info.get("margin") or 0)
    if eq <= 0:
        return
    util = used / eq * 100
    levels = sorted(float(x) for x in (r.get("levels") or []))
    cur = max((x for x in levels if util >= x), default=0.0)
    told, last = _margin_lvl.get(a.id, (0.0, 0.0))
    if cur < told and util < told - MARGIN_HYST:          # 내려감 → 다시 넘으면 또 알림
        told = cur
        _margin_lvl[a.id] = (told, last)
    if not cur:
        return
    rep_sec = int(r.get("repeat", 0) or 0) * 60
    if cur > told or (cur == told and rep_sec and time.time() - last >= rep_sec - 5):
        top = cur == levels[-1]
        nxt = next((x for x in levels if x > cur), None)
        level = f"증거금 {util:.1f}% ({cur:g}% 넘음{' · 마지막 단계' if top else f' · 다음 경고 {nxt:g}%'})"
        ctx = ((f"{acc} · 이번 진입 {entry} 으로\n{level}\n" if entry else f"{acc} · {level}\n")
               + f"평가금 {eq:,.2f} · 사용 증거금 {used:,.2f} · 여유 {float(info.get('margin_free') or 0):,.2f}"
               + (f" · 마진 레벨 {float(info.get('margin_level') or 0):,.0f}%" if info.get("margin_level") else ""))
        kind = "비중 경고" if entry else "증거금 경고"
        if fire("margin_use", a.id, f"{cur:g}", ctx, cooldown_sec=0,
                title=f"🚨 {kind}" if top else f"⚠️ {kind}"):
            _margin_lvl[a.id] = (cur, time.time())


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
