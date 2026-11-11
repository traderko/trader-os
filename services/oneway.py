"""원웨이 장 알림 - 장 시작 뒤 되돌림 없이 한 방향으로 N KTR 이상 가면 텔레그램(푸시)으로 알림

── 재는 법 ──────────────────────────────────────────────────────────
  KTR      : 지금 장(아시아·유로·미장)의 KTR (services/grid.py 와 같은 계산)
             2분 · 5분 · 10분 · 1시간 · 2시간 KTR 을 각각 따로 봄 (시간봉마다 단계·되돌림 허용이 따로)
  한 방향 다리 : 장 시작부터 2분봉 고가·저가로
               상승 다리 = 기준 저점 → 그 뒤 최고점. 최고점에서 '되돌림 허용(그 시간봉 KTR 배수)'보다 더 내려오면
                          그 자리에서 새 다리를 시작 (그 전까지 간 거리는 버림)
               하락 다리 = 반대
             그래서 '올랐다 내렸다 하며 폭만 큰 장'은 안 잡히고, 한쪽으로 쭉 간 것만 잡힘
  알림     : 시간봉마다 다리 길이가 단계를 넘을 때 한 번씩. 같은 순간 여러 시간봉이 넘으면 한 메시지로 묶음
             주 단계 이상 갔던 다리가 되돌림 허용을 넘게 꺾이면 '멈춤' 알림 한 번
             그 방향과 반대인 포지션(원웨이 상승인데 매도 보유)이 있으면 ⚠️ 로 같이 알려줌
  장마다 새로 셈 (장이 바뀌면 그 장의 KTR로 처음부터). 1시간·2시간 KTR은 장 시작 뒤 그 봉이 끝나야 셈

  부담: KTR은 장마다 시간봉별로 한 번만 구하고(캐시), 1분마다 종목당 2분봉 한 번만 받아 모든 시간봉을 같이 계산.
        텔레그램 버튼 처리와는 따로 도는 작업이라 버튼이 느려지지 않음

── 설정 ─ data/settings.json "oneway" (텔레그램 /원웨이 에서 바꿈) ──
  enabled · groups(["gold","nasdaq"]) · tfs: {시간봉: {on, levels, main, retrace}}
시세는 MT5 (잠금을 켠 계좌 중 워커가 켜진 첫 계좌). 상태는 data/oneway.json (재시작해도 같은 알림을 또 보내지 않게)
"""
from __future__ import annotations

import asyncio
import html
import json
import os
import time
from datetime import datetime, timedelta

from services import grid, symbols

KST = grid.KST
LOOP_SEC = 60
PATH_TF = "2m"
TFS = ["2m", "5m", "10m", "1h", "2h"]
TF_KR = {"2m": "2분", "5m": "5분", "10m": "10분", "1h": "1시간", "2h": "2시간"}
_MIN = {"on": True, "levels": [3, 5, 7], "main": 5, "retrace": 2.0}
_HOUR = {"on": True, "levels": [3, 4], "main": 3, "retrace": 1.0}
DEFAULTS = {"enabled": True, "groups": ["gold", "nasdaq"],
            "tfs": {"2m": dict(_MIN, on=False), "5m": dict(_MIN), "10m": dict(_MIN), "1h": dict(_HOUR), "2h": dict(_HOUR)}}


# ── 설정 ──
def _tf_cfg(base: dict, raw) -> dict:
    t = dict(base)
    if isinstance(raw, dict):
        t.update({k: raw[k] for k in base if k in raw})
    try:
        t["levels"] = sorted({float(x) for x in t["levels"] if float(x) > 0})[:5] or base["levels"]
        t["retrace"] = max(0.3, float(t["retrace"]))
        t["main"] = float(t["main"])
        t["on"] = bool(t["on"])
    except (TypeError, ValueError):
        t = dict(base)
    return t


def cfg() -> dict:
    from services.settings_service import get_settings
    raw = get_settings().get("oneway") or {}
    c = {"enabled": bool(raw.get("enabled", True)),
         "groups": [g for g in (raw.get("groups") or DEFAULTS["groups"]) if symbols.group_by_key(g)] or DEFAULTS["groups"],
         "tfs": {tf: _tf_cfg(DEFAULTS["tfs"][tf], (raw.get("tfs") or {}).get(tf)) for tf in TFS}}
    return c


def save_cfg(patch: dict, tf: str | None = None) -> dict:
    from services.settings_service import get_settings, update_settings
    cur = dict(get_settings().get("oneway") or {})
    if tf:
        tfs = dict(cur.get("tfs") or {})
        tfs[tf] = dict(tfs.get(tf) or {}, **patch)
        cur["tfs"] = tfs
    else:
        cur.update(patch)
    update_settings({"oneway": cur})
    return cfg()


# ── 상태 (보낸 알림) ──
def _state_path() -> str:
    from services.settings_service import DATA_DIR
    return os.path.join(DATA_DIR, "oneway.json")


_state: dict | None = None


def _load() -> dict:
    global _state
    if _state is None:
        try:
            with open(_state_path(), encoding="utf-8") as f:
                _state = json.load(f)
        except (OSError, ValueError):
            _state = {}
    return _state


def _save() -> None:
    try:
        # 오래된 장 기록은 정리 (3일)
        cut = time.time() - 3 * 86400
        for k in [k for k, v in _state.items() if v.get("at", 0) < cut]:
            _state.pop(k, None)
        tmp = _state_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_state, f, ensure_ascii=False)
        os.replace(tmp, _state_path())
    except OSError as e:
        print(f"[oneway] 상태 저장 실패: {e}")


# ── 다리 계산 ──
def legs(candles: list[dict], th: float) -> dict:
    """2분봉(시간순) → 지금 살아 있는 상승·하락 다리.
    th: 되돌림 허용 (가격). 다리 끝에서 th 넘게 되돌리면 그 자리에서 새 다리 시작.
    반환 {"up": {...}, "down": {...}} - start(시작가) · end(끝 가격) · len · pull(지금까지 되돌림) · t0 · t1"""
    up = down = None
    for c in candles:
        h, l, t = c["h"], c["l"], c["t"]
        if up is None:
            up = {"start": l, "end": l, "pull": 0.0, "t0": t, "t1": t}
            down = {"start": h, "end": h, "pull": 0.0, "t0": t, "t1": t}
        # 상승 다리
        if l < up["start"]:                                    # 시작보다 더 내려가면 시작점을 내림
            up.update(start=l, end=l, pull=0.0, t0=t, t1=t)
        else:
            if h > up["end"]:
                up.update(end=h, t1=t, pull=0.0)
            up["pull"] = max(up["pull"], up["end"] - l)
            if up["pull"] > th:                                # 꺾임 → 되돌린 저점에서 새로
                up.update(start=l, end=l, pull=0.0, t0=t, t1=t)
        # 하락 다리
        if h > down["start"]:
            down.update(start=h, end=h, pull=0.0, t0=t, t1=t)
        else:
            if l < down["end"]:
                down.update(end=l, t1=t, pull=0.0)
            down["pull"] = max(down["pull"], h - down["end"])
            if down["pull"] > th:
                down.update(start=h, end=h, pull=0.0, t0=t, t1=t)
    out = {}
    for k, d in (("up", up), ("down", down)):
        if d:
            out[k] = dict(d, len=abs(d["end"] - d["start"]))
    return out


# ── 시세 ──
_ktr_cache: dict = {}


async def _account_ids() -> list[int]:
    from sqlalchemy import select
    from db.models.account import Account
    from db.session import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        return list((await db.execute(select(Account.id).where(Account.lock_enabled == True).order_by(Account.id))).scalars().all())


async def _ktrs(aid: int, group: str, sid: str, opened: datetime, now: datetime) -> dict:
    """{tf: ktr} - 장 시작 봉이 다 만들어진 시간봉만. 장마다 시간봉별로 한 번 구해 둠"""
    out = {}
    for tf in TFS:
        key = (group, sid, opened.isoformat(), tf)
        if key in _ktr_cache:
            out[tf] = _ktr_cache[key]
            continue
        if now < opened + timedelta(minutes=grid.TIMEFRAMES[tf][1]):
            continue                                  # 첫 봉이 아직 안 끝남
        try:
            start, end = grid._window([opened], tf, now)
            candles = await grid._candles(aid, group, "mt5", tf, start, end)
            k = grid._ktr_from(candles, sid, opened, tf, "mt5")["ktr"]
        except grid.GridError:
            continue
        if k > 0:
            _ktr_cache[key] = out[tf] = k
    old = (now - timedelta(days=2)).isoformat()
    for k in [k for k in _ktr_cache if k[2] < old]:
        _ktr_cache.pop(k, None)
    return out


async def measure(aid: int, group: str, now: datetime | None = None, c: dict | None = None) -> dict | None:
    """지금 장의 원웨이 상태 - 시간봉마다 {ktr, legs}. KTR을 하나도 못 구했으면 None"""
    c = c or cfg()
    now = now or datetime.now(KST)
    sid = grid.current_session(now)
    opened = grid.session_open(sid, now)
    ktrs = await _ktrs(aid, group, sid, opened, now)
    if not ktrs:
        return None
    candles = await grid._candles(aid, group, "mt5", PATH_TF, opened, now + timedelta(minutes=2))
    candles = [x for x in candles if x["t"] >= int(opened.timestamp())]
    if not candles:
        return None
    per = {tf: {"ktr": k, "legs": legs(candles, c["tfs"][tf]["retrace"] * k)} for tf, k in ktrs.items()}
    return {"group": group, "sid": sid, "session": grid.SESSIONS[sid][0], "opened": opened, "tfs": per,
            "last_t": candles[-1]["t"], "lo": min(x["l"] for x in candles), "hi": max(x["h"] for x in candles)}


# ── 알림 ──
def _p(v: float) -> str:
    return f"{v:,.2f}"


async def _counter_positions(group: str, direction: str) -> str:
    """그 종목에서 원웨이 반대 방향 포지션 요약 ('' 이면 없음)"""
    from services.worker_client import worker_get
    want = 1 if direction == "up" else 0          # 상승이면 매도(1)가 역방향
    n = lots = pnl = 0
    for aid in await _account_ids():
        try:
            rows = await asyncio.wait_for(worker_get(aid, "/positions-all"), 5)
        except Exception:
            continue
        for p in rows:
            g = symbols.group_of(p.get("symbol", ""))
            side = 0 if str(p.get("type")).lower() in ("0", "buy") or str(p.get("type")).lower().startswith("b") else 1
            if g and g.get("key") == group and side == want:   # type 이 0/1 이든 "BUY"/"SELL" 이든
                n += 1
                lots += p.get("volume") or 0
                pnl += (p.get("profit") or 0) + (p.get("swap") or 0)
    if not n:
        return ""
    return f"⚠️ 역방향 {'매도' if want == 1 else '매수'} {n}개 {lots:g}랏 보유 중 ({pnl:+,.2f})"


def _label(group: str) -> str:
    g = symbols.group_by_key(group) or {}
    return f"{g.get('icon', '')} {g.get('label', group)}".strip()


def _grade(x: float, t: dict) -> str:
    return "강한 원웨이" if x >= t["main"] and t["levels"] and x >= max(t["levels"]) and max(t["levels"]) > t["main"] \
        else "원웨이" if x >= t["main"] else "조짐"


def _notify(title: str, body: str) -> None:
    try:
        from services.fcm_service import get_fcm_service
        get_fcm_service().send(title, body, '{"type":"oneway"}')
    except Exception as e:
        print(f"[oneway] 알림 실패: {e}")


def _all_line(m: dict, d: str) -> str:
    """방향 d 의 다리를 시간봉마다 KTR 배수로 한 줄"""
    out = []
    for tf in TFS:
        x = m["tfs"].get(tf)
        if x and x["legs"].get(d):
            out.append(f"{TF_KR[tf]} {x['legs'][d]['len'] / x['ktr']:.1f}")
    return " · ".join(out) + " KTR"


# 지금 살아 있는 원웨이(조짐 이상) - 텔레그램 실시간 현황 메시지 맨 위에 계속 보여 줌 (알림이 다른 알림에 묻혀도 보이게)
ACTIVE: dict[str, dict] = {}


def live_lines(positions: list[dict] | None = None) -> list[str]:
    """실시간 현황에 넣을 줄. positions: 모든 계좌의 /positions-all (역방향 보유 표시용)"""
    out = []
    for group, a in list(ACTIVE.items()):
        if time.time() - a["at"] > 5 * 60:                    # 5분 넘게 갱신이 없으면 (워커 끊김 등) 안 보임
            continue
        for d in ("up", "down"):
            hs = a["dirs"].get(d)
            if not hs:
                continue
            grade = "원웨이" if any(k >= t_main for _, k, t_main in hs) else "조짐"
            arrow, word = ("▲", "상승") if d == "up" else ("▼", "하락")
            name = (symbols.group_by_key(group) or {}).get("label", group)   # 실시간 현황은 이모지 없이 (경고 ⚠️ 만)
            line = (f"{arrow} {html.escape(name)} <b>{word} {grade}</b> · "
                    + " · ".join(f"{TF_KR[tf]} {k:.1f}" for tf, k, _ in hs) + f" KTR · {a['session']}")
            if positions:
                want = 1 if d == "up" else 0
                ps = [p for p in positions if (symbols.group_of(p.get("symbol", "")) or {}).get("key") == group
                      and (0 if str(p.get("type")).lower() in ("0", "buy") or str(p.get("type")).lower().startswith("b") else 1) == want]
                if ps:
                    pnl = sum((p.get("profit") or 0) + (p.get("swap") or 0) for p in ps)
                    line += (f"\n      ⚠️ <b>역방향 {'매도' if want else '매수'} {len(ps)}개 "
                             f"{sum(p.get('volume') or 0 for p in ps):g}랏</b> ({pnl:+,.2f})")
            out.append(line)
    return out


async def check_once(now: datetime | None = None) -> None:
    c = cfg()
    if not c["enabled"]:
        ACTIVE.clear()
        return
    now = now or datetime.now(KST)
    ids = await _account_ids()
    st = _load()
    changed = False
    for group in c["groups"]:
        m = None
        for aid in ids:
            try:
                m = await asyncio.wait_for(measure(aid, group, now, c), 30)
                break
            except Exception:
                continue
        if not m or time.time() - m["last_t"] > 30 * 60:      # 장이 쉬는 중 (주말 등)
            ACTIVE.pop(group, None)
            continue
        dirs = {"up": [], "down": []}                         # 실시간 현황용: 켠 시간봉 중 첫 단계 이상인 다리
        for tf, x in m["tfs"].items():
            t = c["tfs"][tf]
            if not t["on"]:
                continue
            for d, leg in x["legs"].items():
                k = leg["len"] / x["ktr"]
                if t["levels"] and k >= min(t["levels"]):
                    dirs[d].append((tf, k, t["main"]))
        if dirs["up"] or dirs["down"]:
            ACTIVE[group] = {"at": time.time(), "session": m["session"], "dirs": dirs}
        else:
            ACTIVE.pop(group, None)
        skey = f"{group}|{m['opened'].isoformat()}"
        rec = st.setdefault(skey, {"at": time.time(), "legs": {}})
        hits = {"up": [], "down": []}                         # 이번에 단계를 넘은 시간봉
        live_keys = set()
        for tf, x in m["tfs"].items():
            t = c["tfs"][tf]
            for d, leg in x["legs"].items():
                lkey = f"{tf}|{d}|{leg['t0']}|{leg['start']}"
                live_keys.add(lkey)
                info = rec["legs"].get(lkey)
                k = leg["len"] / x["ktr"]
                if info:
                    info["max"] = max(info.get("max", 0), k)
                if not t["on"]:
                    continue
                sent = info.get("lv", 0) if info else 0
                hit = [lv for lv in t["levels"] if k >= lv and lv > sent]
                if hit:
                    rec["legs"][lkey] = dict(info or {}, lv=max(hit), max=k, d=d, tf=tf, main=t["main"])
                    hits[d].append((tf, k, t, leg, x["ktr"]))
                    changed = True
        for d, hs in hits.items():
            if not hs:
                continue
            arrow, word = ("🚀", "상승") if d == "up" else ("🔻", "하락")
            top = max(hs, key=lambda h: (h[1] >= h[2]["main"], TFS.index(h[0])))      # 큰 시간봉 · 주 단계 넘은 것 우선
            title = f"{arrow} {_label(group)} {word} " + " · ".join(f"{TF_KR[tf]} {k:.1f}" for tf, k, *_ in hs) + " KTR"
            title += f" ({_grade(top[1], top[2])})"
            leg, ktr = top[3], top[4]
            body = (f"{m['session']} · {TF_KR[top[0]]} KTR {_p(ktr)}\n"
                    f"{_p(leg['start'])} → {_p(leg['end'])} ({leg['len']:,.2f} 움직임 · 되돌림 {leg['pull'] / ktr:.1f} / 허용 {top[2]['retrace']:g} KTR)\n"
                    f"전체: {_all_line(m, d)}")
            warn = await _counter_positions(group, d)
            if warn:
                body += "\n" + warn
            _notify(title, body)
        # 주 단계 이상 갔던 다리가 꺾였으면 멈춤 알림 한 번 (같은 방향은 가장 큰 시간봉 하나만)
        ended = {}
        for lkey, info in rec["legs"].items():
            if lkey not in live_keys and not info.get("ended") and info.get("lv", 0) >= info.get("main", 99):
                info["ended"] = True
                changed = True
                d = info.get("d")
                if d not in ended or TFS.index(info.get("tf", "2m")) > TFS.index(ended[d].get("tf", "2m")):
                    ended[d] = info
        for d, info in ended.items():
            t = c["tfs"].get(info.get("tf"), _MIN)
            _notify(f"⏸ {_label(group)} 원웨이 {'상승' if d == 'up' else '하락'} 멈춤",
                    f"{m['session']} · {TF_KR.get(info.get('tf'), '')} 최대 {info.get('max', 0):.1f} KTR 간 뒤 {t['retrace']:g} KTR 넘게 되돌림")
    if changed:
        _save()


async def loop() -> None:
    await asyncio.sleep(45)
    while True:
        try:
            await check_once()
        except Exception as e:
            print(f"[oneway] 확인 실패: {e}")
        await asyncio.sleep(LOOP_SEC)


# ── 지난 날짜 확인 (/원웨이 11/5) ──
def max_legs(candles: list[dict], th: float) -> dict:
    """legs() 와 같은 규칙으로 걸으면서, 그 구간에서 가장 길었던 상승·하락 다리 (끝난 다리 포함)"""
    best = {"up": None, "down": None}
    up = down = None
    for c in candles:
        h, l, t = c["h"], c["l"], c["t"]
        if up is None:
            up = {"start": l, "end": l, "pull": 0.0, "t0": t, "t1": t}
            down = {"start": h, "end": h, "pull": 0.0, "t0": t, "t1": t}
        if l < up["start"]:
            up.update(start=l, end=l, pull=0.0, t0=t, t1=t)
        else:
            if h > up["end"]:
                up.update(end=h, t1=t, pull=0.0)
                if best["up"] is None or up["end"] - up["start"] > best["up"]["len"]:
                    best["up"] = dict(up, len=up["end"] - up["start"])
            up["pull"] = max(up["pull"], up["end"] - l)
            if up["pull"] > th:
                up.update(start=l, end=l, pull=0.0, t0=t, t1=t)
        if h > down["start"]:
            down.update(start=h, end=h, pull=0.0, t0=t, t1=t)
        else:
            if l < down["end"]:
                down.update(end=l, t1=t, pull=0.0)
                if best["down"] is None or down["start"] - down["end"] > best["down"]["len"]:
                    best["down"] = dict(down, len=down["start"] - down["end"])
            down["pull"] = max(down["pull"], h - down["end"])
            if down["pull"] > th:
                down.update(start=h, end=h, pull=0.0, t0=t, t1=t)
    return {k: v for k, v in best.items() if v}


def parse_day(word: str, now: datetime | None = None):
    """'11/5' '11.5' '1105' '2026-11-05' '어제' '오늘' → date (거래일). 못 읽으면 None"""
    import re
    from datetime import date
    from services.loss_limit import trading_day
    now = now or datetime.now(KST)
    today = date.fromisoformat(trading_day(now))
    w = (word or "").strip().lower()
    if w in ("오늘", "today"):
        return today
    if w in ("어제", "yesterday"):
        d = today - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d
    m = re.fullmatch(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})", w)
    try:
        if m:
            return date(int(m[1]), int(m[2]), int(m[3]))
        m = re.fullmatch(r"(\d{1,2})[-./](\d{1,2})", w) or re.fullmatch(r"(\d{2})(\d{2})", w)
        if m:
            d = date(today.year, int(m[1]), int(m[2]))
            return d if d <= today else date(today.year - 1, d.month, d.day)
    except ValueError:
        return None
    return None


def _day_sessions(day) -> list[tuple[str, datetime, datetime]]:
    """거래일 day 의 (장 id, 시작, 끝) - 끝 = 다음 장 시작 (미장은 거래일 끝)"""
    from zoneinfo import ZoneInfo
    NY = ZoneInfo("America/New_York")
    end = datetime(day.year, day.month, day.day, 18, 0, tzinfo=NY).astimezone(KST)
    ref = end - timedelta(seconds=1)
    opens = [(sid, grid.session_open(sid, ref)) for sid in ("asia", "euro", "us")]
    out = []
    for i, (sid, o) in enumerate(opens):
        e = opens[i + 1][1] if i + 1 < len(opens) else end
        out.append((sid, o, e))
    return out


async def day_report(day, groups: list[str] | None = None, brief: bool = False) -> str:
    """brief=True: 끝난 장의 판정 줄만 (지금 /원웨이 화면 아래 '오늘 지난 장'용)"""
    c = cfg()
    groups = groups or c["groups"]
    WEEK = "월화수목금토일"
    L = [f"📏 <b>원웨이 확인</b> · {day.month}/{day.day}({WEEK[day.weekday()]}) 거래일",
         "<i>장마다 그 장 KTR로, 되돌림 허용을 넘지 않고 간 가장 긴 다리 · 🔥 = 원웨이 기준 이상</i>"]
    if day.weekday() >= 5:
        return "\n".join(L + ["", "주말이라 장이 없습니다."])
    sess = _day_sessions(day)
    now = datetime.now(KST)
    if sess[0][1] > now:
        return "\n".join(L + ["", "아직 오지 않은 날입니다."])
    ids = await _account_ids()
    if brief:
        L = []
        sess = [x for x in sess if x[2] <= now]          # 끝난 장만
        if not sess:
            return ""
    for group in groups:
        if not brief:
            L.append("")
            L.append(f"<b>{html.escape(_label(group))}</b>")
        aid, err = None, ""
        # KTR: 시간봉마다 봉을 한 번 받아서 세 장 모두 계산
        ktr = {sid: {} for sid, _, _ in sess}
        for tf in TFS:
            got = False
            for a in ([aid] if aid else ids):
                try:
                    start, end = grid._window([o for _, o, _ in sess], tf, now)
                    candles = await asyncio.wait_for(grid._candles(a, group, "mt5", tf, start, end), 30)
                except Exception as e:
                    err = str(getattr(e, "detail", e))[:150]
                    continue
                aid, got = a, True
                for sid, o, _ in sess:
                    try:
                        ktr[sid][tf] = grid._ktr_from(candles, sid, o, tf, "mt5")["ktr"]
                    except grid.GridError:
                        pass
                break
            if not got and not aid:
                break
        if not aid:
            if not brief:
                L.append(html.escape(err or "MT5 연결 안 됨"))
            continue
        try:
            path = await asyncio.wait_for(grid._candles(aid, group, "mt5", PATH_TF, sess[0][1], min(now, sess[-1][2])), 30)
        except Exception as e:
            if not brief:
                L.append(html.escape(str(getattr(e, "detail", e))[:150]))
            continue
        for sid, o, e in sess:
            cs = [x for x in path if int(o.timestamp()) <= x["t"] < int(e.timestamp())]
            name = grid.SESSIONS[sid][0]
            if not cs:
                L.append(f"· {name}: 봉 없음" + (" (아직 안 열림)" if o > now else ""))
                continue
            width = max(x["h"] for x in cs) - min(x["l"] for x in cs)
            rows, hits = [], {"up": [], "down": []}
            for tf in TFS:
                k = ktr[sid].get(tf)
                if not k:
                    continue
                t = c["tfs"][tf]
                best = max_legs(cs, t["retrace"] * k)
                parts = []
                for d in ("up", "down"):
                    b = best.get(d)
                    if not b:
                        continue
                    x = b["len"] / k
                    if x >= t["main"] and t["on"]:             # 판정은 알림을 켠 시간봉만 (꺼진 건 줄에만 표시)
                        hits[d].append(f"{TF_KR[tf]} {x:.1f}")
                    parts.append(f"{'🔥' if x >= t['main'] else ('↗' if d == 'up' else '↘')}{x:.1f}")
                rows.append(f"   {TF_KR[tf]} {_p(k)}: " + "  ".join(parts))
            verdict = []
            for d, word in (("up", "상승"), ("down", "하락")):
                if hits[d]:
                    verdict.append(f"원웨이 {word} ({' · '.join(hits[d])})")
            L.append(f"· <b>{name}</b> {o:%H:%M}~{e:%H:%M} · 폭 {_p(width)} → " + (" / ".join(verdict) if verdict else "원웨이 아님"))
            # 가장 긴 다리 시각 (원웨이였으면)
            for d in ("up", "down"):
                if hits[d]:
                    tf0 = next(tf for tf in reversed(TFS) if ktr[sid].get(tf) and any(h.startswith(TF_KR[tf] + " ") for h in hits[d]))
                    b = max_legs(cs, c["tfs"][tf0]["retrace"] * ktr[sid][tf0])[d]
                    t0 = datetime.fromtimestamp(b["t0"], KST)
                    t1 = datetime.fromtimestamp(b["t1"], KST)
                    L.append(f"   {'↗' if d == 'up' else '↘'} {t0:%H:%M}~{t1:%H:%M} {_p(b['start'])} → {_p(b['end'])}")
            if not brief:
                L += rows
    return "\n".join(L)[:4000]


def _day_markup(day, groups: list[str] | None) -> dict:
    from services.loss_limit import trading_day
    prev, nxt = day - timedelta(days=1), day + timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    g = ",".join(groups or [])
    row = [{"text": f"◀ {prev.month}/{prev.day}", "callback_data": f"ow:d:{prev.isoformat()}:{g}"[:64]}]
    if nxt.isoformat() <= trading_day(datetime.now(KST)):
        row.append({"text": f"{nxt.month}/{nxt.day} ▶", "callback_data": f"ow:d:{nxt.isoformat()}:{g}"[:64]})
    return {"inline_keyboard": [row]}


# ── 텔레그램 /원웨이 (/oneway) ──
def _status_markup(c: dict) -> dict:
    return {"inline_keyboard": [
        [{"text": ("✅ " if c["tfs"][tf]["on"] else "▫️ ") + TF_KR[tf], "callback_data": f"ow:tf:{tf}"} for tf in TFS],
        [{"text": "🔕 알림 끄기" if c["enabled"] else "🔔 알림 켜기", "callback_data": "ow:on:" + ("0" if c["enabled"] else "1")},
         {"text": "🔄 새로고침", "callback_data": "ow:r"}],
    ]}


async def status_text() -> str:
    c = cfg()
    L = [f"📏 <b>원웨이</b> · 알림 {'켜짐' if c['enabled'] else '꺼짐'} · 되돌림 허용 넘으면 새로 셈"]
    for tf in TFS:
        t = c["tfs"][tf]
        L.append(f"{'✅' if t['on'] else '▫️'} {TF_KR[tf]}: 단계 {' · '.join(f'{x:g}' for x in t['levels'])} (원웨이 {t['main']:g}) · 되돌림 {t['retrace']:g}")
    ids = await _account_ids()
    for group in c["groups"]:
        m, err = None, ""
        for aid in ids:
            try:
                m = await asyncio.wait_for(measure(aid, group, None, c), 30)
                break
            except Exception as e:
                err = str(getattr(e, "detail", e))[:120]
        L.append("")
        if not m:
            L.append(f"{html.escape(_label(group))}: {html.escape(err) or 'KTR을 아직 못 구함 (장 시작 첫 봉 전이거나 MT5 연결 안 됨)'}")
            continue
        L.append(f"<b>{html.escape(_label(group))}</b> · {m['session']} · 장 폭 {_p(m['hi'] - m['lo'])}")
        for tf in TFS:
            x = m["tfs"].get(tf)
            if not x:
                L.append(f"   {TF_KR[tf]}: 장 시작 봉이 아직 안 끝남")
                continue
            t = c["tfs"][tf]
            parts = []
            for d in ("up", "down"):
                leg = x["legs"].get(d)
                if leg:
                    k = leg["len"] / x["ktr"]
                    parts.append(f"{'🔥' if k >= t['main'] else ('↗' if d == 'up' else '↘')}{k:.1f}")
            L.append(f"   {TF_KR[tf]} KTR {_p(x['ktr'])}: " + "  ".join(parts))
        # 오늘 이미 끝난 장도 원웨이였는지 (지금 장만 보면 아침 아시아장 원웨이를 놓쳐 보임)
        try:
            from datetime import date
            from services.loss_limit import trading_day
            done = await asyncio.wait_for(day_report(date.fromisoformat(trading_day(datetime.now(KST))), [group], brief=True), 40)
        except Exception:
            done = ""
        if done:
            L.append("   <i>오늘 지난 장</i>")
            L += ["   " + x for x in done.splitlines()]
    L += ["", "<i>위 버튼: 시간봉별 알림 켜기/끄기\n지난 날: /원웨이 11/5 (또는 어제 · /원웨이 11/5 gold)\n바꾸기: /원웨이 1h 단계 3 4 · /원웨이 1h 원웨이 3 · /원웨이 5m 되돌림 2 · /원웨이 종목 gold nasdaq</i>"]
    return "\n".join(L)[:4000]


async def cmd(live, client, token, cid: str, arg: str) -> None:
    words = (arg or "").replace(",", " ").split()
    day = parse_day(words[0]) if words else None
    if words and words[0] in ("날짜", "day") and len(words) > 1:
        day, words = parse_day(words[1]), words[1:]
    if day:                                           # /원웨이 11/5 [gold] → 그날 원웨이였는지
        groups = [w for w in words[1:] if symbols.group_by_key(w)] or None
        await live._send(client, token, cid, "⏳ " + f"{day.month}/{day.day} 시세를 불러오는 중…")
        try:
            text = await day_report(day, groups)
        except Exception as e:
            text = f"확인 실패: {html.escape(str(e))[:200]}"
        await live._send(client, token, cid, text, reply_markup=_day_markup(day, groups))
        return
    if words:
        tf = None
        if words[0].lower() in TFS:
            tf, words = words[0].lower(), words[1:]
        k, vals = (words[0], words[1:]) if words else ("", [])
        try:
            if k in ("단계", "levels") and vals and tf:
                save_cfg({"levels": sorted({float(v) for v in vals})}, tf)
            elif k in ("원웨이", "주단계", "main") and vals and tf:
                save_cfg({"main": float(vals[0])}, tf)
            elif k in ("되돌림", "retrace") and vals and tf:
                save_cfg({"retrace": max(0.3, float(vals[0]))}, tf)
            elif k in ("켜기", "on"):
                save_cfg({"on": True}, tf) if tf else save_cfg({"enabled": True})
            elif k in ("끄기", "off"):
                save_cfg({"on": False}, tf) if tf else save_cfg({"enabled": False})
            elif k in ("종목", "groups") and vals:
                ok = [v for v in vals if symbols.group_by_key(v)]
                if not ok:
                    raise ValueError("종목 key: " + ", ".join(g["key"] for g in symbols.groups()))
                save_cfg({"groups": ok})
            else:
                raise ValueError("예) /원웨이 1h 단계 3 4 · /원웨이 5m 되돌림 2 (시간봉: " + " ".join(TFS) + ")")
        except ValueError as e:
            await live._send(client, token, cid, f"설정을 못 바꿨습니다: {html.escape(str(e))}")
            return
    await live._send(client, token, cid, await status_text(), reply_markup=_status_markup(cfg()))


async def button(live, client, token, cid: str, data: str, message_id) -> str:
    parts = data.split(":")
    ans = ""
    if len(parts) >= 3 and parts[1] == "d":           # 지난 날짜 ◀ ▶
        from datetime import date
        try:
            day = date.fromisoformat(parts[2])
        except ValueError:
            return "잘못된 날짜"
        groups = [g for g in (parts[3].split(",") if len(parts) > 3 else []) if g] or None
        text = await day_report(day, groups)
        kb = _day_markup(day, groups)
        r = await live._call(client, token, "editMessageText", chat_id=cid, message_id=message_id,
                             text=text, parse_mode="HTML", reply_markup=kb) if message_id else {}
        if not (r.get("ok") or "not modified" in str(r.get("description", ""))):
            await live._send(client, token, cid, text, reply_markup=kb)
        return ""
    if len(parts) >= 3 and parts[1] == "tf" and parts[2] in TFS:
        on = not cfg()["tfs"][parts[2]]["on"]
        save_cfg({"on": on}, parts[2])
        ans = f"{TF_KR[parts[2]]} {'켬' if on else '끔'}"
    elif len(parts) >= 3 and parts[1] == "on":
        save_cfg({"enabled": parts[2] == "1"})
        ans = "알림 켬" if parts[2] == "1" else "알림 끔"
    text, kb = await status_text(), _status_markup(cfg())
    r = await live._call(client, token, "editMessageText", chat_id=cid, message_id=message_id,
                         text=text, parse_mode="HTML", reply_markup=kb) if message_id else {}
    if not (r.get("ok") or "not modified" in str(r.get("description", ""))):
        await live._send(client, token, cid, text, reply_markup=kb)
    return ans
