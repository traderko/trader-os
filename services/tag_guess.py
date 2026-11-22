"""진입 근거 추정 - 진입하면 그때 차트를 보고 근거 태그를 '추정'(trade_tag_map.auto = 1)으로 조용히 붙여 둠

왜: 진입하는 순간 근거 태그를 누르는 게 너무 번거로워서 안 달게 됨 → 묻지도 알리지도 않고 차트로 맞힐 수 있는 것만 붙임.
    틀린 건 웹·텔레그램 근거 화면에서 끄면 됨 (추정 태그는 선택된 상태로 보임). 웹에서 근거를 저장하면 그때부터 직접 고른 근거가 됨.
    통계 '근거별 성과'에는 같이 집계되고, 거래 목록에서는 '추정'으로 구분해서 보임.

이미 있는 근거 이름(trade_tags, 진입)만 씀 - 이름이 없으면 그 태그는 건너뜀. 근거를 직접 단 거래에는 안 붙임.
같은 종목·방향으로 잠깐 사이 나눠 들어간 건 한 번만 계산해서 같이 붙임.

판정 (진입 시각 기준, 마감된 봉 = 진입 전에 끝난 봉)
  5분BOB · 2분BOB    직전 마감 5분(2분)봉이 돌파캔들(BOB)이고 진입 방향과 같음 (services/breakout.py 정의)
  기본더블비         10분·1시간·2시간 중 하나: 20이평 우상향(3봉 전보다 위)인데 BB(20,2) 종가 · BB(4,4) 시가 하단 중 하나라도 닿고 매수
                     (우하향 · 상단 · 매도). 닿음 = 진입 봉이나 직전 마감 봉의 저가(고가)가 그 밴드까지 감
  5분기본더블비      같은 것을 5분봉으로 (10분 이상에서 안 맞고 5분에서만 맞을 때)
  시가박스돌파       지금 장(아시아·유로·미장) 첫 1시간봉 고가 위에서 매수 / 저가 아래에서 매도, 최근 10분봉 6개 안에 그 선을 넘음
  2시간20이평        진입가가 2시간봉 20이평 ±0.1% 안 · 이평 위에서 매수 / 아래에서 매도 (지지·저항)
  1시간장기이평      같은 것을 1시간봉 120이평으로
  시간봉음봉도지     직전 마감 1시간봉이 음봉이고 몸통이 전체 길이의 30% 이하
  망치형             5분·10분·1시간·2시간 직전 마감 봉 중 하나: 아래꼬리 ≥ 몸통×2 · 위꼬리 ≤ 몸통이면 매수
                     (반대 모양이면 매도)
  추세매매           1시간 · 2시간 BOB 방향이 둘 다 진입 방향 (반대 BOB 가 나올 때까지 유지되는 방향)
  조정거래           추세매매 조건인데 10분 BOB 방향은 반대 (되돌림에서 추세 쪽으로)
  돌파거래           진입가가 직전 마감 10분봉 20개의 최고가 위(매수) / 최저가 아래(매도)
  돌파실패           최근 10분봉 6개 안에 20봉 최고가를 넘었다가 다시 안으로 마감한 뒤 매도 (저가 쪽이면 매수)
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

from services import breakout

_recent: dict[tuple, dict] = {}       # (계좌, 종목, 방향) → {"at", "task", "ids"}
WINDOW = 90                           # 이 시간 안에 같은 종목·방향으로 들어간 건 같은 근거로 봄
_bg: set = set()


def on_open(account_id: int, trade_id: int, symbol: str, side: str, price: float, magic: int = 0) -> None:
    if magic and 71700000 <= int(magic) <= 71799999:      # 구간매매(그리드) 주문은 건너뜀
        return
    key = (account_id, symbol, str(side).upper()[:1])
    now = time.time()
    r = _recent.get(key)
    if r and now - r["at"] < WINDOW:
        r["ids"].add(trade_id)
        if r.get("tags") is not None:                      # 이미 계산 끝 - 바로 붙임
            t = asyncio.create_task(_apply({trade_id}, r["tags"]))
            _bg.add(t)
            t.add_done_callback(_bg.discard)
        return
    r = _recent[key] = {"at": now, "ids": {trade_id}, "tags": None}
    t = asyncio.create_task(_run(account_id, symbol, key[2] == "B", price, r))
    _bg.add(t)
    t.add_done_callback(_bg.discard)
    for k in [k for k, v in _recent.items() if now - v["at"] > WINDOW * 3]:
        _recent.pop(k, None)


async def _run(aid: int, symbol: str, buy: bool, price: float, r: dict) -> None:
    try:
        names = await guess(aid, symbol, buy, price)
    except Exception as e:
        print(f"[tag_guess] {symbol} 추정 실패: {e}")
        names = []
    r["tags"] = names
    if names:
        await _apply(set(r["ids"]), names)


# ── 차트 ──
async def _bars(aid: int, symbol: str, tf: str, n: int) -> list[dict]:
    from services.grid import TIMEFRAMES
    from services.worker_client import worker_get
    code, mins = TIMEFRAMES[tf]
    end = datetime.now(timezone.utc) + timedelta(minutes=mins)
    start = end - timedelta(minutes=mins * (n * 2 + 60))
    rows = await asyncio.wait_for(worker_get(aid, "/rates", {
        "symbol": symbol, "timeframe": code, "from_ts": start.isoformat(), "to_ts": end.isoformat()}), 15)
    return [{"t": int(x["time"]), "o": float(x["open"]), "h": float(x["high"]), "l": float(x["low"]), "c": float(x["close"])}
            for x in rows][-n:]


def _split(bars: list[dict], mins: int) -> tuple[list[dict], dict | None]:
    """(마감된 봉들, 진입한 봉) - 진입 시각 = 지금"""
    now = time.time()
    closed = [b for b in bars if b["t"] + mins * 60 <= now]
    cur = bars[-1] if bars and bars[-1]["t"] + mins * 60 > now else None
    return closed, cur


def _ma(vals: list[float], n: int) -> float | None:
    return sum(vals[-n:]) / n if len(vals) >= n else None


def _double_touch(closed: list[dict], cur: dict | None, buy: bool) -> bool:
    if len(closed) < 24:
        return False
    closes = [b["c"] for b in closed]
    m_now, m_old = _ma(closes, 20), _ma(closes[:-3], 20)
    if m_now is None or m_old is None:
        return False
    b20 = breakout._bb(closes, 20, 2.0)
    opens = [b["o"] for b in closed] + ([cur["o"]] if cur else [])
    b4 = breakout._bb(opens, 4, 4.0)
    last = [closed[-1]] + ([cur] if cur else [])
    if buy:
        return m_now > m_old and min(b["l"] for b in last) <= max(b20[0], b4[0])     # 둘 중 하나라도 닿으면
    return m_now < m_old and max(b["h"] for b in last) >= min(b20[2], b4[2])


def _hammer(b: dict, buy: bool) -> bool:
    body = abs(b["c"] - b["o"])
    rng = b["h"] - b["l"]
    if rng <= 0:
        return False
    body = max(body, rng * 0.02)
    lower, upper = min(b["o"], b["c"]) - b["l"], b["h"] - max(b["o"], b["c"])
    return (lower >= body * 2 and upper <= body) if buy else (upper >= body * 2 and lower <= body)


async def guess(aid: int, symbol: str, buy: bool, price: float) -> list[str]:
    from services import grid, symbols
    out: list[str] = []
    bars = {}
    for tf, n in (("2m", 30), ("5m", 40), ("10m", 40), ("1h", 130), ("2h", 40)):
        try:
            bars[tf] = await _bars(aid, symbol, tf, n)
        except Exception:
            bars[tf] = []
    split = {tf: _split(b, grid.TIMEFRAMES[tf][1]) for tf, b in bars.items()}

    # BOB
    for tf, name in (("5m", "5분BOB"), ("2m", "2분BOB")):
        closed, _ = split[tf]
        h = breakout.bob_at(closed) if closed else None
        if h and (h["dir"] == "up") == buy:
            out.append(name)

    # 기본더블비
    if any(_double_touch(*split[tf], buy) for tf in ("10m", "1h", "2h")):
        out.append("기본더블비")
    elif _double_touch(*split["5m"], buy):
        out.append("5분기본더블비")

    # 시가박스돌파 - 지금 장 첫 1시간봉
    try:
        now = datetime.now(grid.KST)
        opened = grid.session_open(grid.current_session(now), now)
        box = next((b for b in split["1h"][0] if b["t"] <= opened.timestamp() < b["t"] + 3600), None)
        c10 = split["10m"][0][-6:]
        if box and c10:
            line = box["h"] if buy else box["l"]
            crossed = any((b["h"] > line) if buy else (b["l"] < line) for b in c10)
            if crossed and ((price > line) if buy else (price < line)):
                out.append("시가박스돌파")
    except Exception:
        pass

    # 이평 지지·저항
    for tf, n, name in (("2h", 20, "2시간20이평"), ("1h", 120, "1시간장기이평")):
        closed, _ = split[tf]
        m = _ma([b["c"] for b in closed], n)
        if m and abs(price - m) / m <= 0.001 and ((price >= m) if buy else (price <= m)):
            out.append(name)

    # 시간봉음봉도지
    h1 = split["1h"][0]
    if h1:
        b = h1[-1]
        rng = b["h"] - b["l"]
        if rng > 0 and b["c"] < b["o"] and (b["o"] - b["c"]) / rng <= 0.3:
            out.append("시간봉음봉도지")

    # 망치형
    if any(split[tf][0] and _hammer(split[tf][0][-1], buy) for tf in ("5m", "10m", "1h", "2h")):
        out.append("망치형")

    # 추세 · 조정 - BOB 방향 (반대 BOB 가 나올 때까지 유지)
    g = symbols.group_of(symbol)
    st = breakout._load()
    key = g["key"] if g else symbol
    d = {tf: (st.get(f"{key}|{tf}") or {}).get("dir") for tf in ("10m", "1h", "2h")}
    want = "up" if buy else "down"
    if d["1h"] == want and d["2h"] == want:
        out.append("조정거래" if d["10m"] and d["10m"] != want else "추세매매")

    # 돌파 · 돌파실패 - 10분봉 20개
    c10 = split["10m"][0]
    if len(c10) >= 26:
        ref = c10[-26:-6]
        hi, lo = max(b["h"] for b in ref), min(b["l"] for b in ref)
        recent = c10[-6:]
        prev20 = c10[-20:]
        if (price > max(b["h"] for b in prev20)) if buy else (price < min(b["l"] for b in prev20)):
            out.append("돌파거래")
        elif not buy and any(b["h"] > hi for b in recent) and recent[-1]["c"] < hi:
            out.append("돌파실패")
        elif buy and any(b["l"] < lo for b in recent) and recent[-1]["c"] > lo:
            out.append("돌파실패")
    return out


# ── 저장 ──
async def _apply(ids: set, names: list[str]) -> None:
    from sqlalchemy import select
    from db.models.trade_tag import TradeTag
    from db.models.trade_tag_map import TradeTagMap
    from db.session import AsyncSessionLocal
    if not ids or not names:
        return
    async with AsyncSessionLocal() as db:
        tags = (await db.execute(select(TradeTag).where(TradeTag.name.in_(names)))).scalars().all()
        tags = [t for t in tags if getattr(t.type, "value", t.type) == "entry"]
        if not tags:
            return
        rows = (await db.execute(select(TradeTagMap, TradeTag).join(TradeTag, TradeTag.id == TradeTagMap.tag_id)
                                 .where(TradeTagMap.trade_id.in_(list(ids))))).all()
        have: dict[int, set] = {}
        manual = set()
        for m, t in rows:
            have.setdefault(m.trade_id, set()).add(m.tag_id)
            if getattr(t.type, "value", t.type) == "entry" and not m.auto:
                manual.add(m.trade_id)
        for i in ids:
            if i in manual:                      # 직접 단 근거가 있으면 건드리지 않음
                continue
            for t in tags:
                if t.id not in have.get(i, set()):
                    db.add(TradeTagMap(trade_id=i, tag_id=t.id, auto=1))
        await db.commit()
    print(f"[tag_guess] 거래 {sorted(ids)} 추정 근거: {', '.join(t.name for t in tags)}")
