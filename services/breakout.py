"""돌파캔들(BOB) 알림 - 들고 있는 포지션과 반대로 돌파캔들(BOB)이 나오면 텔레그램(푸시)

돌파캔들(BOB) 정의 (봉 마감 종가가 둘 다 넘어야)
  BB(20, 2) 종가  : 기준선 = 종가 20개 평균, 밴드 = ± 2 × 표준편차
  BB(4, 4) 시가   : 기준선 = 시가 4개 평균,  밴드 = ± 4 × 표준편차
  상승 돌파 = 종가(진행 중 봉은 현재가) > 두 상단 모두 · 하락 돌파 = < 두 하단 모두
  표준편차는 트레이딩뷰 ta.stdev 와 같은 방식(모집단, n 으로 나눔)

알림 조건
  상승 돌파인데 매도 포지션을 들고 있거나, 하락 돌파인데 매수를 들고 있을 때만 (같은 방향이면 조용히)
  시간봉마다 따로 켜고 끔 (기본: 10분 · 1시간 · 2시간 켬, 2분 · 5분 끔)
  기준: 'close' = 봉이 마감됐을 때 종가로 (기본 - 돌파는 봉 마감으로만 인정) / 'live' = 진행 중인 봉 현재가 (빠르지만 참고용)
  같은 봉·같은 방향은 한 번만

부담
  포지션을 들고 있는 종목만, 켠 시간봉마다 봉 30개만 받음 (20초마다). 포지션이 없으면 아무것도 안 받음
  텔레그램 버튼 처리와는 따로 도는 작업

설정 settings "breakout" (텔레그램 /돌파 에서 바꿈)
"""
from __future__ import annotations

import asyncio
import html
import math
import time
from datetime import datetime, timedelta, timezone

from services import symbols

LOOP_SEC = 20
TFS = [("2m", "M2", 2, "2분"), ("5m", "M5", 5, "5분"), ("10m", "M10", 10, "10분"), ("1h", "H1", 60, "1시간"), ("2h", "H2", 120, "2시간")]
TF_KR = {k: kr for k, _, _, kr in TFS}
DEFAULT_ON = {"2m": False, "5m": False, "10m": True, "1h": True, "2h": True}
_sent: dict[str, float] = {}          # (계좌·종목·시간봉·봉시각·방향) → 보낸 시각


# ── 설정 ──
def cfg() -> dict:
    from services.settings_service import get_settings
    raw = get_settings().get("breakout") or {}
    tfs = dict(DEFAULT_ON)
    tfs.update({k: bool(v) for k, v in (raw.get("tfs") or {}).items() if k in tfs})
    return {"enabled": bool(raw.get("enabled", True)), "tfs": tfs, "mode": raw.get("mode") if raw.get("mode") in ("live", "close") else "close"}


def save_cfg(patch: dict) -> dict:
    from services.settings_service import get_settings, update_settings
    cur = dict(get_settings().get("breakout") or {})
    if "tfs" in patch:
        cur["tfs"] = dict(cur.get("tfs") or {}, **patch.pop("tfs"))
    cur.update(patch)
    update_settings({"breakout": cur})
    return cfg()


# ── 계산 ──
def _bb(vals: list[float], n: int, k: float) -> tuple[float, float, float] | None:
    if len(vals) < n:
        return None
    w = vals[-n:]
    m = sum(w) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in w) / n)
    return m - k * sd, m, m + k * sd


def check_bars(bars: list[dict], use_last: bool, minutes: int | None = None) -> dict | None:
    """bars: 시간순 [{t,o,h,l,c}]. use_last=False 면 아직 안 끝난 봉은 빼고 마지막으로 마감된 봉으로
    (minutes 를 주면 봉 시작 + 길이가 지금보다 뒤인 봉만 뺌 - 새 봉 첫 틱이 오기 전에도 방금 마감된 봉을 바로 봄).
    돌파면 {dir:'up'|'down', t, price, bb20:(lo,mid,up), bb4:(…)}"""
    if not use_last:
        if minutes:
            now = time.time()
            bars = [b for b in bars if b["t"] + minutes * 60 <= now]
        else:
            bars = bars[:-1]
    if len(bars) < 20:
        return None
    closes = [b["c"] for b in bars]
    opens = [b["o"] for b in bars]
    b20, b4 = _bb(closes, 20, 2.0), _bb(opens, 4, 4.0)
    if not b20 or not b4:
        return None
    px = closes[-1]
    if px > b20[2] and px > b4[2]:
        d = "up"
    elif px < b20[0] and px < b4[0]:
        d = "down"
    else:
        return None
    return {"dir": d, "t": bars[-1]["t"], "price": px, "bb20": b20, "bb4": b4}


async def _bars(aid: int, symbol: str, code: str, minutes: int) -> list[dict]:
    from services.worker_client import worker_get
    end = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    start = end - timedelta(minutes=minutes * 40)            # 주말·쉬는 시간이 끼어도 20개 넘게 나오게
    rows = await asyncio.wait_for(worker_get(aid, "/rates", {
        "symbol": symbol, "timeframe": code, "from_ts": start.isoformat(), "to_ts": end.isoformat()}), 10)
    return [{"t": int(r["time"]), "o": float(r["open"]), "h": float(r["high"]), "l": float(r["low"]), "c": float(r["close"])}
            for r in rows][-30:]


def _side(p: dict) -> int:
    t = str(p.get("type")).lower()
    return 0 if t in ("0", "buy") or t.startswith("b") else 1


def _fmt(v: float) -> str:
    return f"{v:,.2f}"


def _label(sym: str) -> str:
    g = symbols.group_of(sym)
    return f"{g.get('icon', '')} {g['label']}".strip() if g else sym


async def _accounts() -> list:
    from sqlalchemy import select
    from db.models.account import Account
    from db.session import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        return list((await db.execute(select(Account).where(Account.lock_enabled == True).order_by(Account.id))).scalars().all())


async def scan(only_held: bool = True) -> list[dict]:
    """포지션 든 종목 × 켠 시간봉 → 결과 목록 [{acc, symbol, tf, hit|None, held:{0:[..],1:[..]}}]"""
    from services.worker_client import worker_get
    c = cfg()
    out = []
    for a in await _accounts():
        try:
            pos = await asyncio.wait_for(worker_get(a.id, "/positions-all"), 5)
        except Exception:
            continue
        by_sym: dict[str, dict[int, list]] = {}
        for p in pos or []:
            by_sym.setdefault(p.get("symbol", ""), {0: [], 1: []})[_side(p)].append(p)
        for sym, held in by_sym.items():
            for tf, code, mins, _ in TFS:
                if not c["tfs"][tf]:
                    continue
                try:
                    bars = await _bars(a.id, sym, code, mins)
                except Exception:
                    continue
                out.append({"acc": a.account_number, "aid": a.id, "symbol": sym, "tf": tf,
                            "hit": check_bars(bars, c["mode"] == "live", mins), "held": held})
    return out


def _held_txt(ps: list[dict]) -> str:
    lots = sum(p.get("volume") or 0 for p in ps)
    pnl = sum((p.get("profit") or 0) + (p.get("swap") or 0) for p in ps)
    return f"{len(ps)}개 {lots:g}랏 ({pnl:+,.2f})"


async def check_once() -> None:
    c = cfg()
    if not c["enabled"] or not any(c["tfs"].values()):
        return
    now = time.time()
    for k in [k for k, v in _sent.items() if now - v > 3 * 86400]:
        _sent.pop(k, None)
    groups: dict[tuple, list] = {}                         # 같은 계좌·종목·방향은 한 메시지로 (여러 시간봉이 한꺼번에 넘을 때)
    for r in await scan():
        h = r["hit"]
        if not h:
            continue
        against = 1 if h["dir"] == "up" else 0                # 상승 돌파면 매도(1)가 반대
        if not r["held"][against]:
            continue
        key = f"{r['aid']}|{r['symbol']}|{r['tf']}|{h['t']}|{h['dir']}"
        if key in _sent:
            continue
        _sent[key] = now
        groups.setdefault((r["acc"], r["symbol"], h["dir"]), []).append(r)
    for (acc, sym, d), rs in groups.items():
        up = d == "up"
        ps = rs[0]["held"][1 if up else 0]
        i = 2 if up else 0
        title = (f"⚠️ {_label(sym)} {'상승' if up else '하락'} 돌파캔들(BOB) ({' · '.join(TF_KR[r['tf']] for r in rs)}) · "
                 f"반대 {'매도' if up else '매수'} 보유")
        lines = [f"{acc} · {sym} {'매도' if up else '매수'} {_held_txt(ps)}"]
        for r in rs:
            h = r["hit"]
            lines.append(f"{TF_KR[r['tf']]}: {'현재가' if c['mode'] == 'live' else datetime.fromtimestamp(h['t'], timezone(timedelta(hours=9))).strftime('%H:%M') + ' 봉 마감 종가'} {_fmt(h['price'])} {'>' if up else '<'} "
                         f"BB20/2 {_fmt(h['bb20'][i])} · BB4/4(시가) {_fmt(h['bb4'][i])}")
        try:
            from services.fcm_service import get_fcm_service
            get_fcm_service().send(title, "\n".join(lines), '{"type":"breakout"}')
        except Exception as e:
            print(f"[breakout] 알림 실패: {e}")


async def loop() -> None:
    await asyncio.sleep(30)
    while True:
        try:
            await check_once()
        except Exception as e:
            print(f"[breakout] 확인 실패: {e}")
        await asyncio.sleep(LOOP_SEC)


# ── 텔레그램 /BOB · /돌파 (/bob · /breakout) ──
def _markup(c: dict) -> dict:
    return {"inline_keyboard": [
        [{"text": ("✅ " if c["tfs"][tf] else "▫️ ") + TF_KR[tf], "callback_data": f"bo:tf:{tf}"} for tf, *_ in TFS],
        [{"text": ("• " if c["mode"] == "close" else "") + "봉 마감 (기본)", "callback_data": "bo:mode:close"},
         {"text": ("• " if c["mode"] == "live" else "") + "진행 중 봉 (참고)", "callback_data": "bo:mode:live"}],
        [{"text": "🔕 알림 끄기" if c["enabled"] else "🔔 알림 켜기", "callback_data": "bo:on:" + ("0" if c["enabled"] else "1")},
         {"text": "🔄 지금 보기", "callback_data": "bo:r"}],
    ]}


async def status_text() -> str:
    c = cfg()
    L = [f"📈 <b>돌파캔들(BOB) 알림</b> · {'켜짐' if c['enabled'] else '꺼짐'}",
         "돌파캔들(BOB) = 봉 마감 종가가 BB(20,2) 종가 + BB(4,4) 시가 두 밴드를 모두 넘은 봉 · 들고 있는 포지션과 반대일 때만 알림",
         f"기준: {'진행 중 봉 현재가 (참고용)' if c['mode'] == 'live' else '봉 마감 종가'} · 시간봉: "
         + (" · ".join(TF_KR[t] for t, *_ in TFS if c["tfs"][t]) or "없음"), ""]
    try:
        rows = await asyncio.wait_for(scan(), 40)
    except Exception as e:
        rows, err = [], str(e)[:150]
        L.append(f"지금 상태를 못 읽음: {html.escape(err)}")
    if not rows and not L[-1].startswith("지금 상태"):
        L.append("들고 있는 포지션이 없어서 볼 종목이 없습니다.")
    seen = set()
    for r in rows:
        k = (r["acc"], r["symbol"])
        if k not in seen:
            seen.add(k)
            b, s = r["held"][0], r["held"][1]
            L.append(f"<b>{html.escape(_label(r['symbol']))}</b> {html.escape(r['symbol'])} · {r['acc']}"
                     + (f" · 매수 {_held_txt(b)}" if b else "") + (f" · 매도 {_held_txt(s)}" if s else ""))
        h = r["hit"]
        if h:
            bad = (h["dir"] == "up" and r["held"][1]) or (h["dir"] == "down" and r["held"][0])
            when = datetime.fromtimestamp(h["t"], timezone(timedelta(hours=9))).strftime("%H:%M")
            L.append(f"   {TF_KR[r['tf']]}: {'⚠️ ' if bad else ''}{'▲ 상승' if h['dir'] == 'up' else '▼ 하락'} BOB "
                     + (f"({when} 봉 마감 {_fmt(h['price'])})" if c["mode"] == "close" else f"중 ({_fmt(h['price'])})"))
        else:
            L.append(f"   {TF_KR[r['tf']]}: BOB 없음")
    return "\n".join(x for x in L if x is not None)[:4000]


async def cmd(live, client, token, cid: str, arg: str = "") -> None:
    await live._send(client, token, cid, await status_text(), reply_markup=_markup(cfg()))


async def button(live, client, token, cid: str, data: str, message_id) -> str:
    p = data.split(":")
    ans = ""
    if len(p) >= 3 and p[1] == "tf" and p[2] in TF_KR:
        on = not cfg()["tfs"][p[2]]
        save_cfg({"tfs": {p[2]: on}})
        ans = f"{TF_KR[p[2]]} {'켬' if on else '끔'}"
    elif len(p) >= 3 and p[1] == "mode" and p[2] in ("live", "close"):
        save_cfg({"mode": p[2]})
        ans = "진행 중 봉 기준" if p[2] == "live" else "봉 마감 기준"
    elif len(p) >= 3 and p[1] == "on":
        save_cfg({"enabled": p[2] == "1"})
        ans = "알림 켬" if p[2] == "1" else "알림 끔"
    text, kb = await status_text(), _markup(cfg())
    r = await live._call(client, token, "editMessageText", chat_id=cid, message_id=message_id,
                         text=text, parse_mode="HTML", reply_markup=kb) if message_id else {}
    if not (r.get("ok") or "not modified" in str(r.get("description", ""))):
        await live._send(client, token, cid, text, reply_markup=kb)
    return ans
