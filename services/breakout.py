"""돌파캔들(BOB) - 보는 종목의 시간봉마다 '지금 BOB 방향'을 들고 있다가, 텔레그램 실시간 현황 · 알림으로 보여줌

돌파캔들(BOB) 정의 (봉 마감 종가가 둘 다 넘어야)
  BB(20, 2) 종가  : 기준선 = 종가 20개 평균, 밴드 = ± 2 × 표준편차
  BB(4, 4) 시가   : 기준선 = 시가 4개 평균,  밴드 = ± 4 × 표준편차
  상승 BOB = 종가 > 두 상단 모두 · 하락 BOB = 종가 < 두 하단 모두
  표준편차는 트레이딩뷰 ta.stdev 와 같은 방식(모집단, n 으로 나눔)

BOB 상태 (종목 · 시간봉마다)
  상승 BOB 가 나오면 '상승' - 그 뒤로 반대 방향(하락) BOB 가 나올 때까지 계속 상승으로 둠 (중간 봉들과 상관없이)
  상태는 data/bob_state.json 에 저장 (서버를 다시 켜도 이어짐). 처음엔 최근 봉 40개 안에서 마지막 BOB 를 찾아 시작

보는 종목
  설정한 종목(기본 골드 · 나스닥) + 포지션을 들고 있는 종목. 포지션이 없어도 실시간 현황에 보임

알림
  새 BOB 가 나왔는데 그 반대 방향 포지션을 들고 있으면 ⚠️ 알림 (기본)
  '모든 BOB 알림'을 켜면 포지션과 상관없이 새 BOB 마다 알림
  같은 봉은 한 번만 · 여러 시간봉이 한꺼번에 나오면 한 메시지

부담
  20초마다 (보는 종목 × 켠 시간봉) 만큼 봉 60개씩 - 종목 2개 × 시간봉 3개면 작은 요청 6개. 텔레그램 버튼 처리와는 따로 돎

설정 settings "breakout" (텔레그램 /bob 에서 바꿈): enabled · tfs · groups · alert_all
"""
from __future__ import annotations

import asyncio
import html
import json
import math
import os
import time
from datetime import datetime, timedelta, timezone

from services import symbols

LOOP_SEC = 20
TFS = [("2m", "M2", 2, "2분"), ("5m", "M5", 5, "5분"), ("10m", "M10", 10, "10분"), ("1h", "H1", 60, "1시간"), ("2h", "H2", 120, "2시간")]
TF_KR = {k: kr for k, _, _, kr in TFS}
TF_ORDER = [k for k, *_ in TFS]
DEFAULT_ON = {"2m": False, "5m": False, "10m": True, "1h": True, "2h": True}
KST = timezone(timedelta(hours=9))
UP, DN = "↑", "↓"                     # 상승 · 하락 표시
LOOKBACK = 40                         # 처음 상태를 찾을 때 거슬러 보는 마감 봉 수
_state: dict | None = None            # {"gold|10m": {dir, t, price, bb20, bb4}}
_view: dict = {"at": 0, "markets": []}  # 마지막 확인 결과 (실시간 현황 · /bob 화면)


# ── 설정 ──
def cfg() -> dict:
    from services.settings_service import get_settings
    raw = get_settings().get("breakout") or {}
    tfs = dict(DEFAULT_ON)
    tfs.update({k: bool(v) for k, v in (raw.get("tfs") or {}).items() if k in tfs})
    groups = [g for g in (raw.get("groups") or ["gold", "nasdaq"]) if symbols.group_by_key(g)]
    return {"enabled": bool(raw.get("enabled", True)), "tfs": tfs, "groups": groups, "alert_all": bool(raw.get("alert_all", False))}


def save_cfg(patch: dict) -> dict:
    from services.settings_service import get_settings, update_settings
    cur = dict(get_settings().get("breakout") or {})
    if "tfs" in patch:
        cur["tfs"] = dict(cur.get("tfs") or {}, **patch.pop("tfs"))
    cur.update(patch)
    update_settings({"breakout": cur})
    return cfg()


# ── 상태 파일 ──
def _path() -> str:
    from services.settings_service import DATA_DIR
    return os.path.join(DATA_DIR, "bob_state.json")


def _load() -> dict:
    global _state
    if _state is None:
        try:
            with open(_path(), encoding="utf-8") as f:
                _state = json.load(f)
        except (OSError, ValueError):
            _state = {}
    return _state


def _save() -> None:
    try:
        tmp = _path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_state, f)
        os.replace(tmp, _path())
    except OSError:
        pass


# ── 계산 ──
def _bb(vals: list[float], n: int, k: float) -> tuple[float, float, float] | None:
    if len(vals) < n:
        return None
    w = vals[-n:]
    m = sum(w) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in w) / n)
    return m - k * sd, m, m + k * sd


def bob_at(bars: list[dict]) -> dict | None:
    """bars 의 마지막 봉(마감된 봉)이 BOB 면 {dir, t, price, bb20, bb4}"""
    if len(bars) < 20:
        return None
    b20, b4 = _bb([b["c"] for b in bars], 20, 2.0), _bb([b["o"] for b in bars], 4, 4.0)
    px = bars[-1]["c"]
    if px > b20[2] and px > b4[2]:
        d = "up"
    elif px < b20[0] and px < b4[0]:
        d = "down"
    else:
        return None
    return {"dir": d, "t": bars[-1]["t"], "price": px, "bb20": list(b20), "bb4": list(b4)}


def last_bob(bars: list[dict], minutes: int, since: int = 0) -> tuple[dict | None, int]:
    """마감된 봉들 중 가장 최근 BOB (since 보다 뒤 봉만) · 그리고 마지막 마감 봉 시각"""
    now = time.time()
    closed = [b for b in bars if b["t"] + minutes * 60 <= now]
    if not closed:
        return None, 0
    for i in range(len(closed) - 1, max(18, len(closed) - LOOKBACK - 1), -1):
        if closed[i]["t"] <= since:
            break
        h = bob_at(closed[: i + 1])
        if h:
            return h, closed[-1]["t"]
    return None, closed[-1]["t"]


async def _bars(aid: int, symbol: str, code: str, minutes: int) -> list[dict]:
    from services.worker_client import worker_get
    end = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    start = end - timedelta(minutes=minutes * 90)            # 주말·쉬는 시간이 끼어도 60개 넘게 나오게
    rows = await asyncio.wait_for(worker_get(aid, "/rates", {
        "symbol": symbol, "timeframe": code, "from_ts": start.isoformat(), "to_ts": end.isoformat()}), 10)
    return [{"t": int(r["time"]), "o": float(r["open"]), "h": float(r["high"]), "l": float(r["low"]), "c": float(r["close"])}
            for r in rows][-(LOOKBACK + 22):]


def _side(p: dict) -> int:
    t = str(p.get("type")).lower()
    return 0 if t in ("0", "buy") or t.startswith("b") else 1


def _fmt(v: float) -> str:
    return f"{v:,.2f}"


def _held_txt(ps: list[dict]) -> str:
    lots = sum(p.get("volume") or 0 for p in ps)
    pnl = sum((p.get("profit") or 0) + (p.get("swap") or 0) for p in ps)
    return f"{len(ps)}개 {lots:g}랏 ({pnl:+,.2f})"


def _when(t: int) -> str:
    d = datetime.fromtimestamp(t, KST)
    return d.strftime("%H:%M") if d.date() == datetime.now(KST).date() else d.strftime("%m/%d %H:%M")


async def _accounts() -> list:
    from sqlalchemy import select
    from db.models.account import Account
    from db.session import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        return list((await db.execute(select(Account).where(Account.lock_enabled == True).order_by(Account.id))).scalars().all())


async def scan() -> tuple[list[dict], list]:
    """보는 종목 × 켠 시간봉의 BOB 상태를 갱신.
    반환: (markets [{key, label, tfs:{tf: state|None}, held:{0:[..],1:[..]}, accs}], new [(market, tf, state, fresh, changed)])"""
    from services.worker_client import worker_get
    c = cfg()
    st = _load()
    markets: dict[str, dict] = {}
    aid0 = None
    for a in await _accounts():
        try:
            pos = await asyncio.wait_for(worker_get(a.id, "/positions-all"), 5)
        except Exception:
            continue
        aid0 = aid0 or a.id
        for p in pos or []:
            g = symbols.group_of(p.get("symbol", ""))
            key = g["key"] if g else p.get("symbol", "")
            m = markets.setdefault(key, {"key": key, "label": f"{g.get('icon', '')} {g['label']}".strip() if g else key,
                                         "aid": a.id, "held": {0: [], 1: []}, "accs": set()})
            m["held"][_side(p)].append(p)
            m["accs"].add(a.account_number)
    if aid0 is None:
        return [], []
    for g in c["groups"]:
        if g not in markets:
            gg = symbols.group_by_key(g) or {}
            markets[g] = {"key": g, "label": f"{gg.get('icon', '')} {gg.get('label', g)}".strip(), "aid": aid0,
                          "held": {0: [], 1: []}, "accs": set()}
    new = []
    for m in markets.values():
        m["tfs"] = {}
        for tf, code, mins, _ in TFS:
            if not c["tfs"][tf]:
                continue
            k = f"{m['key']}|{tf}"
            prev = st.get(k)
            try:
                bars = await _bars(m["aid"], m["key"], code, mins)
            except Exception:
                m["tfs"][tf] = prev                       # 못 받으면 이전 상태 그대로
                continue
            h, last_t = last_bob(bars, mins, since=(prev or {}).get("t", 0))
            if h and (not prev or h["t"] > prev["t"]):
                changed = not prev or prev["dir"] != h["dir"]
                st[k] = h
                new.append((m, tf, h, h["t"] == last_t, changed))   # fresh = 방금 마감된 봉
            m["tfs"][tf] = st.get(k)
    _save()
    out = sorted(markets.values(), key=lambda m: (not (m["held"][0] or m["held"][1]), m["key"]))
    return out, new


async def check_once() -> None:
    c = cfg()
    if not c["enabled"] or not any(c["tfs"].values()):
        _view.update(at=time.time(), markets=[])
        return
    markets, new = await scan()
    _view.update(at=time.time(), markets=markets)
    groups: dict[tuple, list] = {}
    for m, tf, h, fresh, _ in new:
        if not fresh:                                     # 처음 시작할 때 찾은 지난 BOB 은 알리지 않음
            continue
        against = m["held"][1 if h["dir"] == "up" else 0]
        if against or c["alert_all"]:
            groups.setdefault((m["key"], h["dir"]), []).append((m, tf, h))
    for (key, d), rs in groups.items():
        m = rs[0][0]
        up = d == "up"
        against = m["held"][1 if up else 0]
        tfs = " · ".join(TF_KR[tf] for _, tf, _ in sorted(rs, key=lambda x: TF_ORDER.index(x[1])))
        title = (f"⚠️ {m['label']} {'상승' if up else '하락'} 돌파캔들(BOB) ({tfs}) · 반대 {'매도' if up else '매수'} 보유" if against
                 else f"{UP if up else DN} {m['label']} {'상승' if up else '하락'} 돌파캔들(BOB) ({tfs})")
        i = 2 if up else 0
        lines = []
        if against:
            lines.append(f"{', '.join(sorted(m['accs']))} · {'매도' if up else '매수'} {_held_txt(against)}")
        for _, tf, h in rs:
            lines.append(f"{TF_KR[tf]}: {_when(h['t'])} 봉 마감 종가 {_fmt(h['price'])} {'>' if up else '<'} "
                         f"BB20/2 {_fmt(h['bb20'][i])} · BB4/4(시가) {_fmt(h['bb4'][i])}")
        try:
            from services.fcm_service import get_fcm_service
            get_fcm_service().send(title, "\n".join(lines), '{"type":"breakout"}')
        except Exception as e:
            print(f"[breakout] 알림 실패: {e}")


def _market_line(m: dict) -> list[str]:
    parts, warn = [], []
    for tf in TF_ORDER:
        if tf not in (m.get("tfs") or {}):
            continue
        s = m["tfs"][tf]
        if not s:
            parts.append(f"{TF_KR[tf]} -")
            continue
        up = s["dir"] == "up"
        parts.append(f"{TF_KR[tf]} {UP if up else DN}{_when(s['t'])}")
        if m["held"][1 if up else 0]:
            warn.append((tf, up))
    out = [f"{html.escape(m['label'])}  " + " · ".join(parts)]
    for up in (True, False):
        tfs = [TF_KR[t] for t, u in warn if u == up]
        if tfs:
            ps = m["held"][1 if up else 0]
            out.append(f"      ⚠️ <b>{'상승' if up else '하락'} BOB({' · '.join(tfs)}) 반대 {'매도' if up else '매수'} {_held_txt(ps)}</b>")
    return out


def live_lines() -> list[str]:
    """텔레그램 실시간 현황용 - 종목마다 시간봉별 BOB 방향(↑ 상승 · ↓ 하락)과 그 BOB 봉 시각. 반대 포지션이면 ⚠️"""
    if time.time() - _view["at"] > 120:
        return []
    out = []
    for m in _view["markets"]:
        if m.get("tfs"):
            out += _market_line(m)
    return out


async def loop() -> None:
    await asyncio.sleep(30)
    while True:
        try:
            await check_once()
        except Exception as e:
            print(f"[breakout] 확인 실패: {e}")
        await asyncio.sleep(LOOP_SEC)


# ── 텔레그램 /bob · /돌파 ──
def _markup(c: dict) -> dict:
    return {"inline_keyboard": [
        [{"text": ("✅ " if c["tfs"][tf] else "▫️ ") + TF_KR[tf], "callback_data": f"bo:tf:{tf}"} for tf in TF_ORDER],
        [{"text": ("• " if not c["alert_all"] else "") + "반대 포지션일 때만 알림", "callback_data": "bo:all:0"},
         {"text": ("• " if c["alert_all"] else "") + "모든 BOB 알림", "callback_data": "bo:all:1"}],
        [{"text": "🔕 끄기" if c["enabled"] else "🔔 켜기", "callback_data": "bo:on:" + ("0" if c["enabled"] else "1")},
         {"text": "🔄 지금 보기", "callback_data": "bo:r"}],
    ]}


async def status_text() -> str:
    c = cfg()
    L = [f"📈 <b>돌파캔들(BOB)</b> · {'켜짐' if c['enabled'] else '꺼짐'}",
         "BOB = 봉 마감 종가가 BB(20,2) 종가 + BB(4,4) 시가 두 밴드를 모두 넘은 봉. 반대 방향 BOB 가 나올 때까지 그 방향 유지",
         f"시간봉: {' · '.join(TF_KR[t] for t in TF_ORDER if c['tfs'][t]) or '없음'} · "
         f"알림: {'모든 BOB' if c['alert_all'] else '반대 포지션 보유 때만'} · 보는 종목: 포지션 든 종목 + "
         + " · ".join((symbols.group_by_key(g) or {}).get("label", g) for g in c["groups"]), ""]
    try:
        markets, _ = await asyncio.wait_for(scan(), 60)
        _view.update(at=time.time(), markets=markets)
    except Exception as e:
        markets = []
        L.append(f"지금 상태를 못 읽음: {html.escape(str(e)[:150])}")
    for m in markets:
        L += _market_line(m)
        b, s = m["held"][0], m["held"][1]
        if b or s:
            L.append("      " + " · ".join(x for x in ((f"매수 {_held_txt(b)}" if b else ""), (f"매도 {_held_txt(s)}" if s else "")) if x))
    if not markets and len(L) == 4:
        L.append("MT5 가 연결된 계좌가 없습니다.")
    L.append(f"\n<i>{UP} 상승 · {DN} 하락 · 옆 시각은 그 BOB 봉 · 종목 바꾸기: /bob 종목 gold nasdaq</i>")
    return "\n".join(L)[:4000]


async def cmd(live, client, token, cid: str, arg: str = "") -> None:
    w = (arg or "").split()
    if w and w[0] in ("종목", "groups") and len(w) > 1:
        ok = [x for x in w[1:] if symbols.group_by_key(x)]
        if ok:
            save_cfg({"groups": ok})
    await live._send(client, token, cid, await status_text(), reply_markup=_markup(cfg()))


async def button(live, client, token, cid: str, data: str, message_id) -> str:
    p = data.split(":")
    ans = ""
    if len(p) >= 3 and p[1] == "tf" and p[2] in TF_KR:
        on = not cfg()["tfs"][p[2]]
        save_cfg({"tfs": {p[2]: on}})
        ans = f"{TF_KR[p[2]]} {'켬' if on else '끔'}"
    elif len(p) >= 3 and p[1] == "all":
        save_cfg({"alert_all": p[2] == "1"})
        ans = "모든 BOB 알림" if p[2] == "1" else "반대 포지션일 때만 알림"
    elif len(p) >= 3 and p[1] == "on":
        save_cfg({"enabled": p[2] == "1"})
        ans = "켬" if p[2] == "1" else "끔"
    text, kb = await status_text(), _markup(cfg())
    r = await live._call(client, token, "editMessageText", chat_id=cid, message_id=message_id,
                         text=text, parse_mode="HTML", reply_markup=kb) if message_id else {}
    if not (r.get("ok") or "not modified" in str(r.get("description", ""))):
        await live._send(client, token, cid, text, reply_markup=kb)
    return ans
