"""브로커 휴장 · 조기 마감 공지 (인피녹스 · 한텍 홈페이지)

- 인피녹스: https://infinox.com/global/ko/market-hours 의 '휴장 일정' 구글 시트 (CSV 로 받음)
- 한텍: https://hmarkets.com/ko/tools/markets-holiday-calendar/ 페이지 안 JSON (connected_entries)
두 곳 모두 시간은 브로커 서버 시간 (미국 동부 + 7시간, 미국 서머타임에 맞춰 GMT+2/+3)
6시간마다 받아서 data/broker_hours.json 에 저장. 못 받으면 저장된 것 · CME 시간표로 대신함
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import re
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from db.session import DATA_DIR

NY = ZoneInfo("America/New_York")
FILE = os.path.join(DATA_DIR, "broker_hours.json")
REFRESH_SEC = 6 * 3600
INFINOX_CSV = ("https://docs.google.com/spreadsheets/d/12dBVzveFUC1S-IicrrA_YNRDenhmJSFUTXg2AaVmpUU"
               "/gviz/tq?tqx=out:csv")
HANTEC_URL = "https://hmarkets.com/ko/tools/markets-holiday-calendar/"
PAGES = {"infinox": "https://infinox.com/global/ko/market-hours", "hantec": HANTEC_URL}

# 종목군 → 공지 표의 이름 (앞에서부터 찾음)
NAMES = {
    "infinox": {"gold": ["GOLD"], "nasdaq": ["NAS100"], "oil": ["USOUSD", "CL-OIL"]},
    "hantec": {"gold": ["XAU/XAG", "XAU / XAG", "GOLD FUTURES"], "nasdaq": ["US100", "INDICES FUTURES"],
               "oil": ["USOIL", "USOIL FUTURES"]},
}
_MON = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}

_db: dict | None = None      # {broker: {"at": ts, "days": {"YYYY-MM-DD": {"name": str, "rows": {NAME: status}}}}}


def _key(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def load() -> dict:
    global _db
    if _db is None:
        try:
            _db = json.load(open(FILE, encoding="utf-8"))
        except (OSError, ValueError):
            _db = {}
    return _db


def _save() -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = FILE + ".tmp"
        json.dump(load(), open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        os.replace(tmp, FILE)
    except OSError as e:
        print(f"[broker_hours] 저장 실패: {e}")


# ── 읽기 ──
def _year_for(mon: int, day: int, today: date) -> int:
    """연도 없는 '27 Nov' → 오늘과 가장 가까운 해"""
    best = None
    for y in (today.year - 1, today.year, today.year + 1):
        try:
            d = date(y, mon, day)
        except ValueError:
            continue
        if best is None or abs((d - today).days) < abs((best - today).days):
            best = d
    return best.year if best else today.year


def parse_infinox(text: str, today: date | None = None) -> dict:
    today = today or datetime.now(NY).date()
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 3:
        return {}
    days: dict[str, dict] = {}
    cols: list[tuple[int, str]] = []
    for i, h in enumerate(rows[0][1:], 1):
        m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})", h or "")
        if m and m.group(2).upper() in _MON:
            mon, dd = _MON[m.group(2).upper()], int(m.group(1))
            d = date(_year_for(mon, dd, today), mon, dd).isoformat()
            name = (rows[1][i] if i < len(rows[1]) else "").strip()
            days[d] = {"name": name, "rows": {}}
            cols.append((i, d))
    for r in rows[2:]:
        if not r or not r[0].strip():
            continue
        for i, d in cols:
            if i < len(r) and r[i].strip():
                days[d]["rows"][_key(r[0])] = r[i].strip()
    return days


def parse_hantec(html: str) -> dict:
    h = html.replace('\\\\\\"', '"').replace('\\"', '"')
    days: dict[str, dict] = {}
    pos = 0
    while True:
        m = re.compile(r'"connected_entries"\s*:\s*\[').search(h, pos)
        if not m:
            break
        j0 = m.end() - 1
        depth, j = 0, j0
        for j in range(j0, len(h)):
            c = h[j]
            if c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    break
        pos = j + 1
        try:
            arr = json.loads(h[j0:j + 1])
        except ValueError:
            continue
        for e in arr:
            ds = str(e.get("date") or "")
            if not re.fullmatch(r"\d{8}", ds) or not e.get("instruments"):
                continue
            d = f"{ds[:4]}-{ds[4:6]}-{ds[6:]}"
            day = days.setdefault(d, {"name": (e.get("holiday_name") or "").strip(), "rows": {}})
            for x in e["instruments"]:
                if x.get("name") and x.get("status"):
                    day["rows"][_key(x["name"])] = str(x["status"]).strip()
    return days


def parse_status(s: str) -> dict | None:
    """'Early Close 20:15' · '21:40 조기 마감' · '조기 마감 19:55' → {"close": "20:15"}
       'Closed' · '휴장' · '닫은' · '운영 종료' → {"closed": True}. 정상 · 운영 시간 표 · 늦은 개장만 → None"""
    t = (s or "").strip()
    low = t.lower()
    m = re.search(r"(?:early\s+closed?|조기\s*마감)\D{0,3}(\d{1,2}):(\d{2})", low) \
        or re.search(r"(\d{1,2}):(\d{2})\s*조기\s*마감", low)
    if m:
        return {"close": f"{int(m.group(1)):02d}:{m.group(2)}"}
    if low in ("closed", "휴장", "닫은", "운영 종료", "휴장일"):
        return {"closed": True}
    return None


def _broker_ts(d: date, hh: int, mm: int) -> int:
    """브로커 서버 시각 (미국 동부 + 7시간) → UTC epoch"""
    base = datetime(d.year, d.month, d.day, tzinfo=NY) + timedelta(hours=hh - 7, minutes=mm)
    # 미국 동부 날짜를 하루 넘나드는 경우 (브로커 00:00~06:59 = 전날 동부) 도 위 덧셈이 처리
    return int(datetime(base.year, base.month, base.day, base.hour, base.minute, tzinfo=NY).timestamp())


def closes_for(broker: str, cls: str, now: float, ahead: float) -> list[dict]:
    """앞으로 ahead 초 안 공지된 종료 [{end, day, name, closed}] (cls: gold/nasdaq/oil 종목군 key)"""
    b = load().get(broker) or {}
    want = [_key(n) for n in NAMES.get(broker, {}).get(cls, [])]
    if not want:
        return []
    out = []
    for ds, day in (b.get("days") or {}).items():
        try:
            d = date.fromisoformat(ds)
        except ValueError:
            continue
        st = next((day["rows"][k] for k in want if k in day.get("rows", {})), None)
        p = parse_status(st) if st else None
        if not p:
            continue
        if p.get("closed"):
            end = _broker_ts(d, 0, 0)               # 브로커 날짜 하루 종일 휴장 → 그 날 00:00 (전날 마감) 부터
        else:
            hh, mm = map(int, p["close"].split(":"))
            end = _broker_ts(d, hh, mm)
        if now <= end <= now + ahead:
            out.append({"end": end, "day": ds, "name": day.get("name") or "", "closed": bool(p.get("closed")),
                        "status": st})
    return sorted(out, key=lambda c: c["end"])


def age(broker: str) -> float | None:
    at = (load().get(broker) or {}).get("at")
    return time.time() - at if at else None


# ── 받기 ──
def _get(url: str) -> str:
    import httpx
    with httpx.Client(timeout=20, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                             "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
                               "Accept-Language": "ko,en;q=0.8"}) as c:
        r = c.get(url)
        r.raise_for_status()
        return r.text


async def refresh() -> dict[str, str]:
    """두 브로커 공지를 받아 저장. {broker: 'ok n일' | '실패: …'}"""
    res = {}
    for broker, url, parse in (("infinox", INFINOX_CSV, parse_infinox), ("hantec", HANTEC_URL, parse_hantec)):
        try:
            text = await asyncio.to_thread(_get, url)   # TLS 준비가 느려서 이벤트 루프 밖에서
            days = parse(text)
            if not days:
                raise ValueError("표를 찾지 못함")
            cur = load().setdefault(broker, {})
            merged = {**(cur.get("days") or {}), **days}
            cutoff = (datetime.now(NY).date() - timedelta(days=40)).isoformat()
            cur.update({"at": time.time(), "days": {k: v for k, v in merged.items() if k >= cutoff}, "err": ""})
            res[broker] = f"ok {len(days)}일"
        except Exception as e:
            load().setdefault(broker, {})["err"] = f"{type(e).__name__}: {e}"[:200]
            res[broker] = f"실패: {e}"[:200]
            print(f"[broker_hours] {broker} 받기 실패: {e}")
    _save()
    return res


_last = 0.0


async def maybe_refresh() -> None:
    global _last
    if time.time() - _last >= REFRESH_SEC:
        _last = time.time()
        await refresh()
