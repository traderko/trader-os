"""휴장 · 조기 종료 · 시세 멈춤 알림 (텔레그램)

── 1) 거래 시간 (세션 EA) ─────────────────────────────────────────
  MT5 에 붙인 SessionReporter EA(mt5_ea/sessionReporter.mq5, 주문 기능 없음)가 10분마다
  종목별 앞으로 3일 거래 시간을 POST /market/sessions 로 보냄. 브로커가 휴일에 바꿔 둔 시간이 그대로 들어옴.
  평소와 다른 종료(조기 종료 · 휴장)를 찾아서:
    - 그런 종료가 18시간 안에 있으면 하루 한 번 미리 알림 ("오늘 NAS100+ 03:00 ~ 08:00 멈춤")
    - 그 종목을 들고 있으면 60분 전 · 15분 전에 한 번 더
  평소 종료 = 매일 미국 동부 17시 전후 1시간 쉬는 것, 주말 (금요일 동부 16시 이후 ~ 일요일)

── 2) 미국 휴일 (EA 가 없을 때) ──────────────────────────────────
  EA 거래 시간이 없는 계좌는 미국 휴일(추수감사절 · 크리스마스 등)을 날짜로 계산해
  그날 아침(한국 09시) "조기 종료될 수 있음" 정도로만 알림 - 정확한 시각은 브로커마다 달라서 적지 않음

── 2-1) CME 휴일 시간표 · 실제 멈춘 시각 기억 ─────────────────────
  브로커 CFD(지수 · 금 · 유가)는 CME 선물 휴장 시간을 따라감 (2026 추수감사절 NAS100+: 03:00 멈춤 · 08:00 재개 = CME 그대로).
  휴일 유형별 CME 시간(중부 시간, 여러 자료로 확인한 것만 - 아래 cme_schedule)으로 종료 · 재개를 미리 계산해
  EA 거래 시간처럼 미리 알림 · 60분 · 15분 전 알림에 씀 ("CME 기준 예상").
  휴일에 실제로 멈췄다 재개한 시각을 계좌 · 종목 · 휴일별로 data/market_learned.json 에 기억 → 다음 해 같은 휴일엔 그 시각 ("지난번 실제").
  시간을 확인 못 한 휴일(성금요일 · 준틴스 · 독립기념일 등 해마다 다름)은 날짜 알림만.

── 0) MT5 연결 끊김 · 다시 연결 ────────────────────────────────────
  20초마다 계좌 워커에 포지션을 물을 때, 한 번이라도 연결됐던 계좌가 DOWN_SEC 넘게 응답이 없으면
  "⚠️ MT5 응답 없음" (명령어 · 알림이 안 될 수 있음), 다시 응답하면 "✅ 다시 연결됨 (n분 끊김)".
  서버를 처음 켰을 때 아직 안 붙은 계좌 · 원래 안 쓰는 계좌는 알리지 않음 (한 번 연결된 뒤부터)

── 3) 시세 멈춤 · 재개 (보유 종목) ───────────────────────────────
  20초마다 보유 종목의 마지막 시세 시각(워커 /ticks)을 봄.
    - 거래 시간인데 5분 넘게 시세가 없으면 "시세 멈춤" (예정된 종료 · 매일 쉬는 시간 · 주말이면 알리지 않음)
    - 다시 시세가 들어오면 "거래 재개 · 멈추기 전 → 지금 (갭)" + 그 종목 평가손익
      (매일 쉬는 1시간 뒤 재개는 알리지 않음, 주말 · 휴장 · 예정 밖 멈춤 뒤에는 알림)
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from db.session import DATA_DIR

KST = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")
POLL_SEC = 20
DOWN_SEC = 120                  # MT5 가 이만큼 응답이 없으면 끊김 알림
STALE_SEC = 300                 # 거래 시간인데 이만큼 시세가 없으면 멈춤
SESSIONS_FILE = os.path.join(DATA_DIR, "market_sessions.json")
LEARNED_FILE = os.path.join(DATA_DIR, "market_learned.json")

_sessions: dict[str, dict] = {}       # "계좌번호|종목" → {"w": [[시작, 끝], ...] UTC epoch, "at": 받은 시각}
_state: dict[tuple, dict] = {}        # (계좌 id, 종목) → {"t": 마지막 시세 시각, "mid": 가격, "halt": {...} | None}
_sent: dict[str, float] = {}          # 보낸 알림 키 → 시각 (중복 방지)
_health: dict[int, dict] = {}         # 계좌 id → {"ok": 마지막 응답 시각, "down": 처음 실패 시각, "alerted": bool, "err": 마지막 오류}
_loaded = False


# ── 세션 저장 ──
def _load() -> None:
    global _loaded, _sessions
    if _loaded:
        return
    _loaded = True
    try:
        _sessions = json.load(open(SESSIONS_FILE, encoding="utf-8"))
    except (OSError, ValueError):
        _sessions = {}


def _save() -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = SESSIONS_FILE + ".tmp"
        json.dump(_sessions, open(tmp, "w", encoding="utf-8"))
        os.replace(tmp, SESSIONS_FILE)
    except OSError as e:
        print(f"[market_watch] 세션 저장 실패: {e}")


def put_sessions(payload: dict) -> int:
    """EA 가 보낸 거래 시간 저장. 받은 종목 수"""
    _load()
    an = str(payload.get("account_number") or "")
    syms = payload.get("symbols") or {}
    now = time.time()
    for sym, wins in syms.items():
        w = sorted([int(a), int(b)] for a, b in wins if int(b) > int(a))
        _sessions[f"{an}|{sym}"] = {"w": _merge(w), "at": now}
    _save()
    return len(syms)


def _merge(w: list[list[int]]) -> list[list[int]]:
    out: list[list[int]] = []
    for a, b in w:
        if out and a - out[-1][1] <= 60:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def sessions_for(account_number: str, symbol: str) -> list[list[int]] | None:
    _load()
    s = _sessions.get(f"{account_number}|{symbol}")
    if not s or time.time() - s["at"] > 36 * 3600:        # 하루 반 넘게 안 들어온 건 안 믿음
        return None
    return s["w"]


# ── 종료 종류 ──
def _ny(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, NY)


def close_kind(end: int, start_next: int | None) -> str:
    """'daily' (매일 쉬는 시간) · 'weekend' · 'unusual' (조기 종료 · 휴장)"""
    e = _ny(end)
    gap = (start_next - end) if start_next else 10 ** 9
    if gap <= 90 * 60 and 16 <= e.hour + e.minute / 60 <= 18.1:
        return "daily"
    if e.weekday() == 4 and e.hour + e.minute / 60 >= 15.9 and gap <= 3 * 86400:
        return "weekend"                     # 금요일 동부 16시 이후 닫히고 월요일 전에 열림
    if e.weekday() == 4 and gap > 36 * 3600 and e.hour + e.minute / 60 >= 15.9:
        return "weekend"
    return "unusual"


def closes(w: list[list[int]], now: float, ahead: float) -> list[dict]:
    """now ~ now+ahead 사이 종료들 [{end, reopen, kind}]"""
    out = []
    for i, (a, b) in enumerate(w):
        if b < now or b > now + ahead:
            continue
        nxt = w[i + 1][0] if i + 1 < len(w) else None
        if b >= w[-1][1] and nxt is None and _ny(b).weekday() != 4:
            continue                             # 가진 데이터의 마지막 끝 - 다음 시작을 몰라 판단 안 함 (금요일 끝은 주말 앞이라 봄)
        out.append({"end": b, "reopen": nxt, "kind": close_kind(b, nxt)})
    return out


def in_session(w: list[list[int]], ts: float) -> bool | None:
    if not w or ts < w[0][0] - 3600 or ts > w[-1][1]:
        return None                              # 데이터 범위 밖 - 모름
    return any(a <= ts < b for a, b in w)


def heuristic_open(ts: float) -> bool:
    """EA 거래 시간이 없을 때: 주말 · 매일 쉬는 시간(동부 16:55~18:10)이 아니면 열려 있다고 봄"""
    e = _ny(ts)
    h = e.hour + e.minute / 60
    if e.weekday() == 5 or (e.weekday() == 4 and h >= 16.9) or (e.weekday() == 6 and h < 18.2):
        return False
    return not (16.9 <= h < 18.2)


def halt_kind(w: list[list[int]] | None, last_tick: float, now: float) -> str:
    """시세가 멈춘 이유 추정: 'daily' · 'weekend' · 'unusual'(예정된 휴장 · 조기 종료) · 'unexpected'(예정 밖 - 알림)"""
    if w:
        for i, (a, b) in enumerate(w):              # 마지막 시세 무렵에 끝난 세션
            if abs(b - last_tick) <= STALE_SEC + 120 and b <= now:
                return close_kind(b, w[i + 1][0] if i + 1 < len(w) else None)
        inside = in_session(w, now)
        if inside is False:
            return "unusual"                         # 거래 시간 밖 (시세가 원래 뜸한 종목 등)
        if inside is True:
            return "unexpected"
    if heuristic_open(now):
        return "unexpected"
    e = _ny(now)
    h = e.hour + e.minute / 60
    return "weekend" if e.weekday() in (5, 6) or (e.weekday() == 4 and h >= 16.9) else "daily"


# ── 미국 휴일 (EA 가 없을 때 하루 전 · 당일 알림용) ──
def _nth(year: int, month: int, wd: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(wd - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last(year: int, month: int, wd: int) -> date:
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return d - timedelta(days=(d.weekday() - wd) % 7)


def _observed(d: date) -> date:
    return d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1) if d.weekday() == 6 else d


def _easter(y: int) -> date:
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    return date(y, (h + l - 7 * m + 114) // 31, (h + l - 7 * m + 114) % 31 + 1)


def us_holidays(year: int) -> dict[date, str]:
    """미국 증시 · CME 가 쉬거나 일찍 닫는 날 (미국 날짜)"""
    hd = {
        _observed(date(year, 1, 1)): "새해",
        _nth(year, 1, 0, 3): "마틴 루터 킹 데이",
        _nth(year, 2, 0, 3): "대통령의 날",
        _easter(year) - timedelta(days=2): "성금요일",
        _last(year, 5, 0): "메모리얼 데이",
        _observed(date(year, 6, 19)): "준틴스",
        _observed(date(year, 7, 4)): "독립기념일",
        _nth(year, 9, 0, 1): "노동절",
        _nth(year, 11, 3, 4): "추수감사절",
        _observed(date(year, 12, 25)): "크리스마스",
    }
    tg = _nth(year, 11, 3, 4)
    hd[tg + timedelta(days=1)] = "추수감사절 다음 날 (조기 종료)"
    xe = date(year, 12, 24)
    if xe.weekday() < 5 and xe not in hd:
        hd[xe] = "크리스마스 이브 (조기 종료)"
    return hd


# ── CME 휴일 시간표 ──
_CLASS_WORDS = {
    "index": ("NAS", "US100", "USTEC", "NDX", "NQ", "US500", "SPX", "SP500", "ES", "US30", "DJ", "WS30", "US2000", "RUSSELL"),
    "metal": ("XAU", "XAG", "GOLD", "SILVER", "XPT", "XPD"),
    "energy": ("USO", "WTI", "XTI", "CRUDE", "CL", "NGAS", "XNG", "NATGAS"),
}


def asset_class(symbol: str) -> str | None:
    """종목 → 'index' · 'metal' · 'energy' (CME 시간을 따르는 것). 외환 · 코인 · 브렌트 등은 None"""
    s = symbol.upper()
    if any(w in s for w in ("BRENT", "UKO", "XBR", "BTC", "ETH")):
        return None
    for cls, words in _CLASS_WORDS.items():
        if any(s.startswith(w) or w in s for w in words if len(w) >= 3) or any(s.startswith(w) for w in words if len(w) < 3):
            return cls
    return None


def _ct(d: date, h: int, m: int = 0) -> int:
    return int(datetime(d.year, d.month, d.day, h, m, tzinfo=CT).timestamp())


def _reopen_after(d: date) -> int:
    """d 날 저녁 CME 재개 (중부 17:00). 금요일이면 일요일 17:00"""
    return _ct(d + timedelta(days=2), 17) if d.weekday() == 4 else _ct(d, 17)


_MONDAY_HOLIDAYS = ("마틴 루터 킹 데이", "대통령의 날", "메모리얼 데이", "노동절")


def cme_schedule(d: date, cls: str) -> dict | None:
    """미국 날짜 d 가 휴일 · 조기 종료일이면 그 종목군의 {end, reopen, name} (UTC epoch). 시간을 모르면 None
    여러 자료로 확인한 CME 휴일 시간 (중부 시간):
      월요일 휴일 · 추수감사절 : 지수 12:00 멈춤 · 금 · 유가 13:30 멈춤 → 17:00 재개
      추수감사절 다음 날      : 지수 12:15 · 금 · 유가 13:45 종료 → 일요일 17:00
      크리스마스 이브          : 지수 12:15 · 금 · 유가 12:45 종료 → 크리스마스 저녁 17:00 (크리스마스가 금요일이면 일요일)
      크리스마스 · 새해 (종일 휴장): 전날 16:00 평소 마감 뒤 저녁장 없음 → 휴일 저녁 17:00 (금요일이면 일요일)"""
    name = us_holidays(d.year).get(d)
    if not name or cls not in ("index", "metal", "energy"):
        return None
    idx = cls == "index"
    if name in _MONDAY_HOLIDAYS or name == "추수감사절":
        end, reopen = (_ct(d, 12) if idx else _ct(d, 13, 30)), _ct(d, 17)
    elif name.startswith("추수감사절 다음 날"):
        end, reopen = (_ct(d, 12, 15) if idx else _ct(d, 13, 45)), _reopen_after(d)
    elif name.startswith("크리스마스 이브"):
        xmas = _observed(date(d.year, 12, 25))
        end, reopen = (_ct(d, 12, 15) if idx else _ct(d, 12, 45)), _reopen_after(xmas)
    elif name in ("크리스마스", "새해"):
        prev = d - timedelta(days=3 if d.weekday() == 0 else 1)
        if name == "크리스마스" and us_holidays(prev.year).get(prev, "").startswith("크리스마스 이브"):
            return None                              # 이브 조기 종료가 이미 이 휴장을 덮음
        end, reopen = _ct(prev, 16), _reopen_after(d)
    else:
        return None                                  # 성금요일 · 준틴스 · 독립기념일 등 - 해마다 달라 시간 없이 날짜 알림만
    return {"end": end, "reopen": reopen, "name": name}


# ── 실제로 멈춘 시각 기억 (계좌 · 종목 · 휴일) ──
_learned: dict[str, dict] | None = None


def _learned_db() -> dict:
    global _learned
    if _learned is None:
        try:
            _learned = json.load(open(LEARNED_FILE, encoding="utf-8"))
        except (OSError, ValueError):
            _learned = {}
    return _learned


def learn(an: str, symbol: str, halt_ts: int, reopen_ts: int) -> str | None:
    """휴일에 실제로 멈췄다 재개한 시각을 기억 (미국 동부 시각 · 걸린 분). 휴일이 아니면 기억 안 함. 기억한 휴일 이름"""
    d = _ny(halt_ts).date()
    name = us_holidays(d.year).get(d)
    cls = asset_class(symbol)
    if not name and cls:                              # 크리스마스 · 새해 전날 마감처럼 휴일 전날 멈춘 경우
        for dd in (d + timedelta(days=1), d + timedelta(days=3)):
            sch = cme_schedule(dd, cls)
            if sch and abs(sch["end"] - halt_ts) <= 3 * 3600:
                name = sch["name"]
                break
    if not name:
        return None
    h0, r0 = round(halt_ts / 60) * 60, round(reopen_ts / 60) * 60   # 마지막 시세 12:59:59 → 13:00
    e = _ny(h0)
    _learned_db()[f"{an}|{symbol}|{name}"] = {"halt": f"{e.hour:02d}:{e.minute:02d}",
                                              "minutes": int((r0 - h0) // 60), "year": e.year}
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = LEARNED_FILE + ".tmp"
        json.dump(_learned_db(), open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        os.replace(tmp, LEARNED_FILE)
    except OSError as ex:
        print(f"[market_watch] 기억 저장 실패: {ex}")
    return name


def _broker_closes(broker: str, symbol: str, cls: str, now: float, ahead: float) -> list[dict]:
    """브로커 홈페이지 휴장 공지로 찾은 종료 [{end, reopen, name, source: 'broker'}]"""
    if not broker:
        return []
    try:
        from services import broker_hours
        from services.symbols import group_of
        g = group_of(symbol)
        if not g:
            return []
        out = []
        for c in broker_hours.closes_for(broker, g["key"], now, ahead):
            nyd = _ny(c["end"]).date()
            sch = None
            for dd in (nyd, nyd + timedelta(days=1)):
                s = cme_schedule(dd, cls)
                if s and abs(s["end"] - c["end"]) <= 3 * 3600:
                    sch = s
                    break
            out.append({"end": c["end"], "reopen": sch["reopen"] if sch else None, "source": "broker",
                        "name": (sch or {}).get("name") or c["name"] or ("휴장" if c["closed"] else "조기 마감")})
        return out
    except Exception as e:
        print(f"[market_watch] 브로커 공지 확인 실패: {e}")
        return []


def _broker_day_lines(broker: str, d: date, now: float) -> str:
    """미국 날짜 d 근처 브로커 공지 종료 시각 (골드 · 나스닥 · 오일)"""
    if not broker:
        return ""
    try:
        from services import broker_hours
        out = []
        for key, label in (("nasdaq", "나스닥"), ("gold", "골드"), ("oil", "오일")):
            for c in broker_hours.closes_for(broker, key, now - 6 * 3600, 3 * 86400):
                if c["day"] == d.isoformat():               # 공지 날짜 (브로커 날짜 = 미국 날짜)
                    out.append(f"· {label} {_hm(c['end'])} " + ("휴장" if c["closed"] else "조기 마감"))
                    break
        return "\n".join(out)
    except Exception:
        return ""


def predicted_closes(an: str, symbol: str, now: float, ahead: float, broker: str = "") -> list[dict]:
    """앞으로 ahead 초 안 휴일 종료 예상 [{end, reopen, kind: 'unusual', source: 'broker' | 'learned' | 'cme', name}]
    브로커 홈페이지 공지가 있으면 그 시각이 먼저 (같은 날 CME · 지난번 시각은 생략)"""
    cls = asset_class(symbol)
    if not cls:
        return []
    out, seen = [], set()
    for c in _broker_closes(broker, symbol, cls, now, ahead):
        seen.add(c["end"])
        out.append({**c, "kind": "unusual"})
    today = _ny(now).date()
    for k in range(0, 5):
        d = today + timedelta(days=k)
        sch = cme_schedule(d, cls)
        name = us_holidays(d.year).get(d)
        lr = _learned_db().get(f"{an}|{symbol}|{name}") if name else None
        c = None
        if lr:
            base = _ny(sch["end"]).date() if sch else d
            hh, mm = map(int, lr["halt"].split(":"))
            end = int(datetime(base.year, base.month, base.day, hh, mm, tzinfo=NY).timestamp())
            c = {"end": end, "reopen": end + lr["minutes"] * 60, "source": "learned", "name": name}
        elif sch:
            c = {**sch, "source": "cme"}
        if c and any(abs(c["end"] - x) <= 3 * 3600 for x in seen):
            continue                                 # 브로커 공지가 이미 있는 날
        if c and now <= c["end"] <= now + ahead and c["end"] not in seen:
            seen.add(c["end"])
            out.append({**c, "kind": "unusual"})
    return out


# ── 알림 ──
def _send(key: str, title: str, body: str, kind: str = "") -> None:
    if key in _sent:
        return
    _sent[key] = time.time()
    for k in [k for k, v in _sent.items() if time.time() - v > 4 * 86400]:
        _sent.pop(k, None)
    try:
        from services.telegram_service import TelegramService
        kind = kind or ("mt5_down" if key.startswith("down|") else "mt5_up" if key.startswith("up|") else "market")
        TelegramService().send(title, body, kind=kind)          # 알림 중요도 (services/alert_level.py)
    except Exception as e:
        print(f"[market_watch] 텔레그램 실패: {e}")
    print(f"[market_watch] {title} | {body}")


def _hm(ts: float) -> str:
    return datetime.fromtimestamp(ts, KST).strftime("%m/%d %H:%M")


def _pos_line(ps: list[dict]) -> str:
    by: dict[str, float] = {}
    pl = 0.0
    for p in ps:
        side = "BUY" if int(p.get("type", 0)) == 0 else "SELL"
        by[side] = by.get(side, 0) + float(p.get("volume") or 0)
        pl += float(p.get("profit") or 0) + float(p.get("swap") or 0)
    sides = " · ".join(f"{s} {v:g}랏" for s, v in by.items())
    return f"{sides} · 평가 {pl:+,.2f}"


async def _accounts() -> list[tuple[int, str]]:
    from sqlalchemy import select
    from db.models.account import Account
    from db.session import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        return [(a.id, str(a.account_number)) for a in (await db.execute(select(Account).order_by(Account.id))).scalars().all()]


async def _check_account(aid: int, an: str, now: float) -> None:
    from services.worker_client import worker_get
    hl = _health.setdefault(aid, {"ok": None, "down": None, "alerted": False, "err": ""})
    try:
        positions = await asyncio.wait_for(worker_get(aid, "/positions"), 8) or []
    except Exception as e:
        if hl["ok"] is not None:                     # 한 번이라도 연결됐던 계좌만
            hl["down"] = hl["down"] or now
            hl["err"] = str(getattr(e, "detail", "") or e or type(e).__name__)[:120]
            if not hl["alerted"] and now - hl["down"] >= DOWN_SEC:
                hl["alerted"] = True
                _send(f"down|{an}|{int(hl['down'])}", f"⚠️ 계좌 {an} MT5 응답 없음 ({int((now - hl['down']) // 60)}분째)",
                      f"MT5가 꺼졌거나 다시 켜지는 중이에요. 그동안 텔레그램 명령어 · 체결 · 잠금 알림이 안 될 수 있어요.\n"
                      f"({hl['err']})\n다시 연결되면 알려 드릴게요.")
        return                                       # MT5 가 안 붙은 계좌
    if hl["alerted"]:
        mins = int((now - hl["down"]) // 60) if hl["down"] else 0
        _send(f"up|{an}|{int(hl['down'] or now)}", f"✅ 계좌 {an} MT5 다시 연결됨",
              f"약 {mins}분 동안 응답이 없었어요. 그동안 보낸 명령어는 다시 보내 주세요.")
    hl.update({"ok": now, "down": None, "alerted": False})
    held: dict[str, list[dict]] = {}
    for p in positions:
        held.setdefault(p["symbol"], []).append(p)

    # 1) 거래 시간 - 미리 알림 (평소와 다른 종료)
    _load()
    have_sessions = False
    upcoming = []
    for key, s in list(_sessions.items()):
        k_an, sym = key.split("|", 1)
        if k_an != an or now - s["at"] > 36 * 3600:
            continue
        have_sessions = True
        for c in closes(s["w"], now, 18 * 3600):
            if c["kind"] == "unusual" and (sym in held or _recent_symbol(aid, sym)):
                upcoming.append((sym, {**c, "source": "ea"}))
    # 1-1) CME 휴일 시간표 · 지난번 실제 시각 (브로커가 MT5 거래 시간에 휴장을 표시하지 않을 때)
    try:
        from services.withdraw_notice import broker_of
        broker = await broker_of(aid)
    except Exception:
        broker = ""
    for sym in set(held) | (_recent.get(aid, (0, set()))[1]):
        for c in predicted_closes(an, sym, now, 18 * 3600, broker):
            if not any(x == sym and abs(y["end"] - c["end"]) <= 1800 for x, y in upcoming):
                upcoming.append((sym, c))
    fresh = [(sym, c) for sym, c in upcoming if f"plan|{an}|{sym}|{c['end']}" not in _sent]   # 종료마다 한 번만
    if fresh:
        for sym, c in fresh:
            _sent[f"plan|{an}|{sym}|{c['end']}"] = now
        src = {"ea": "", "cme": " · CME 기준 예상", "learned": " · 지난번 실제 시각", "broker": " · 브로커 공지"}
        lines = [f"· {sym} {_hm(c['end'])} 종료 → {_hm(c['reopen']) if c['reopen'] else '?'} 재개"
                 + (f" ({c['name']}{src.get(c.get('source'), '')})" if c.get("name") else src.get(c.get("source"), ""))
                 + (f"\n   보유: {_pos_line(held[sym])}" if sym in held else "") for sym, c in fresh]
        foot = "한국 시간 · " + ("브로커 MT5 거래 시간 기준" if all(c.get("source") == "ea" for _, c in fresh)
                                 else "브로커 홈페이지 휴장 공지 기준" if all(c.get("source") in ("ea", "broker") for _, c in fresh)
                                 else "CME 예상은 브로커와 몇 분 다를 수 있어요")
        _send(f"planmsg|{an}|{now}", f"📅 휴장 · 조기 종료 예정 (계좌 {an})", "\n".join(lines) + "\n" + foot)
    # 1-2) 포지션 · 최근 거래가 없는 종목군도 미리 알림 (중요도 낮게 - market_info, 기본 🔕 조용)
    try:
        from services.symbols import group_of
        mine = {(group_of(x) or {}).get("key") for x, _ in upcoming} | \
               {(group_of(x) or {}).get("key") for x in set(held) | (_recent.get(aid, (0, set()))[1])}
        info = []
        for key, label, rep in (("nasdaq", "나스닥", "NAS100"), ("gold", "골드", "XAUUSD"), ("oil", "오일", "USOIL")):
            if key in mine:
                continue
            for c in predicted_closes(an, rep, now, 18 * 3600, broker):
                k = f"planinfo|{broker or an}|{key}|{c['end']}"     # 같은 브로커 계좌 여러 개여도 한 번
                if k not in _sent:
                    _sent[k] = now
                    info.append((label, c))
        if info:
            src = {"cme": "CME 기준 예상", "learned": "지난번 실제 시각", "broker": "브로커 공지"}
            lines = [f"· {label} {_hm(c['end'])} 종료 → {_hm(c['reopen']) if c['reopen'] else '?'} 재개"
                     + f" ({c.get('name') or ''}{' · ' if c.get('name') else ''}{src.get(c.get('source'), '')})"
                     for label, c in info]
            who = {"infinox": "인피녹스", "hantec": "한텍"}.get(broker, f"계좌 {an}")
            _send(f"planinfomsg|{broker or an}|{now}", f"📅 휴장 · 조기 종료 예정 ({who} · 포지션 없음)",
                  "\n".join(lines) + "\n한국 시간 · 이 종목들은 지금 포지션이 없어요. 진입하면 멈추기 직전 알림도 드려요",
                  kind="market_info")
    except Exception as e:
        print(f"[market_watch] 포지션 없는 종목 휴장 알림 실패: {e}")

    for sym, c in upcoming:
        if sym not in held:
            continue
        left = c["end"] - now
        for mins in (15, 60):                        # 가장 가까운 단계 하나 (60분 전 · 15분 전)
            if left <= mins * 60:
                _send(f"pre|{an}|{sym}|{c['end']}|{mins}",
                      f"⏰ {sym} {int(left // 60) + 1}분 뒤 거래 종료 (계좌 {an})",
                      f"{_hm(c['end'])} 종료 → {_hm(c['reopen']) if c['reopen'] else '?'} 재개\n보유: {_pos_line(held[sym])}\n"
                      f"재개 때 가격이 튈 수 있어요 (손절도 재개 가격으로 체결될 수 있음)")
                break

    # 2) 미국 휴일 (한국 09시 이후, 미국 날짜로 오늘 · 내일)
    #    브로커가 MT5 거래 시간에 휴장을 표시하지 않는 경우가 많음 (2026-11 추수감사절: 한텍 NAS100+ 가 03:00 에 멈췄는데
    #    MT5 거래 시간은 평소대로) → EA 가 있어도 보냄. 그 날짜에 EA 거래 시간으로 찾은 조기 종료가 있으면 그쪽 알림으로 충분해서 생략
    plan_days = {_ny(c["end"]).date() for _, c in upcoming}
    kst = datetime.fromtimestamp(now, KST)
    if kst.hour >= 9:
        ny_today = datetime.fromtimestamp(now, NY).date()
        for d in (ny_today, ny_today + timedelta(days=1)):
            name = us_holidays(d.year).get(d)
            if name and d not in plan_days:
                bl = _broker_day_lines(broker, d, now)
                _send(f"hol|{an}|{d}", f"📅 미국 휴일: {name} ({d:%m/%d}, 미국 날짜)",
                      f"계좌 {an} · 지수 · 금 · 유가가 평소보다 일찍 멈추거나 쉴 수 있어요. "
                      + (f"브로커 공지 (한국 시간):\n{bl}" if bl else "정확한 시간은 브로커 공지를 확인하세요.")
                      + (f"\n보유: " + ", ".join(f"{s} {_pos_line(ps)}" for s, ps in held.items()) if held else "")
                      + ("\n(브로커 MT5 거래 시간에는 이 날 휴장 표시가 없어요 - 멈추면 시세 멈춤 알림이 가요)" if have_sessions
                         else "\n(MT5에 SessionReporter EA를 붙이면 브로커 거래 시간으로 정확히 알려 드려요)"),
                      kind="" if held else "market_info")    # 포지션 없으면 중요도 낮게
                break

    # 3) 시세 멈춤 · 재개 (보유 종목)
    if not held:
        for k in [k for k in _state if k[0] == aid]:
            _state.pop(k, None)
        return
    try:
        ticks = await asyncio.wait_for(worker_get(aid, "/ticks", {"symbols": ",".join(held)}), 8) or {}
    except Exception:
        return
    for sym, ps in held.items():
        tk = ticks.get(sym)
        if not tk:
            continue
        t, mid = int(tk["time"]), (float(tk["bid"]) + float(tk["ask"])) / 2
        st = _state.setdefault((aid, sym), {"t": t, "mid": mid, "halt": None})
        w = sessions_for(an, sym)
        if t > st["t"] and now - t < STALE_SEC:         # 새 시세 (방금 들어온 것)
            h = st["halt"]
            if h and h["kind"] in ("unusual", "unexpected") and now - h["since"] > 10 * 60:
                learned = learn(an, sym, h["since"], t)   # 휴일이면 다음 해를 위해 실제 시각 기억
                if learned:
                    print(f"[market_watch] {an} {sym} {learned} 실제 멈춤 {_hm(h['since'])} ~ {_hm(t)} 기억")
            if h and (h["kind"] != "daily") and now - h["since"] > 10 * 60:
                gap = mid - h["price"]
                pct = gap / h["price"] * 100 if h["price"] else 0
                why = {"weekend": "주말 뒤", "unusual": "휴장 · 조기 종료 뒤", "unexpected": "멈춤 뒤"}.get(h["kind"], "")
                _send(f"re|{an}|{sym}|{h['since']}",
                      f"▶ {sym} 거래 재개 · {why} (계좌 {an})",
                      f"멈추기 전 {h['price']:,.2f} → 지금 {mid:,.2f}  ({gap:+,.2f}, {pct:+.2f}%)\n보유: {_pos_line(ps)}")
            st.update({"t": t, "mid": mid, "halt": None})
            continue
        if t > st["t"]:                                  # 더 새 시세지만 이미 오래됨 (멈추기 직전 가격)
            st.update({"t": t, "mid": mid})
        if now - t < STALE_SEC or st["halt"]:
            continue
        # 시세가 STALE_SEC 넘게 없음 → 멈춤. 예정된 것인지 판단 (CME 휴일 시간표 · 지난번 실제 시각도)
        kind = halt_kind(w, t, now)
        if kind == "unexpected" and any(abs(c["end"] - t) <= STALE_SEC + 600
                                        for c in predicted_closes(an, sym, t - 3600, 2 * 3600, broker)):
            kind = "unusual"
        st["halt"] = {"since": t, "price": st["mid"] or mid, "kind": kind}
        if kind == "unexpected":
            _send(f"halt|{an}|{sym}|{t}", f"⏸ {sym} 시세 멈춤 ({int((now - t) // 60)}분째, 계좌 {an})",
                  f"마지막 시세 {_hm(t)} · {st['mid']:,.2f}\n보유: {_pos_line(ps)}\n"
                  "휴장이거나 브로커 · MT5 연결 문제일 수 있어요. 지금은 청산이 안 될 수 있어요.")


_recent: dict[int, tuple[float, set]] = {}


def _recent_symbol(aid: int, sym: str) -> bool:
    """최근 30일에 거래한 종목 (휴장 미리 알림을 보유하지 않을 때도 보낼지) - 10분마다 새로"""
    hit = _recent.get(aid)
    return bool(hit and sym in hit[1])


async def _refresh_recent(aid: int) -> None:
    hit = _recent.get(aid)
    if hit and time.time() - hit[0] < 600:
        return
    from sqlalchemy import select
    from db.models.trade import Trade
    from db.session import AsyncSessionLocal
    since = datetime.now(timezone.utc) - timedelta(days=30)
    async with AsyncSessionLocal() as db:
        syms = set((await db.execute(select(Trade.symbol).where(Trade.account_id == aid, Trade.open_time >= since)
                                     .distinct())).scalars().all())
    _recent[aid] = (time.time(), syms)


async def loop() -> None:
    await asyncio.sleep(30)
    while True:
        try:
            from services import broker_hours
            await broker_hours.maybe_refresh()       # 6시간마다 인피녹스 · 한텍 휴장 공지
        except Exception as e:
            print(f"[market_watch] 브로커 공지 받기 실패: {e}")
        try:
            now = time.time()
            for aid, an in await _accounts():
                try:
                    await _refresh_recent(aid)
                    await _check_account(aid, an, now)
                except Exception as e:
                    print(f"[market_watch] account={aid} 확인 실패: {e}")
        except Exception as e:
            print(f"[market_watch] 확인 실패: {e}")
        await asyncio.sleep(POLL_SEC)
