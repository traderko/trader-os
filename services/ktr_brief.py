"""KTR 브리핑 (텔레그램)

── 1) /ktr 최근 [종목] [장] ─────────────────────────────────────────
  최근 7거래일 그 장(아시아 · 유로 · 미장)의 KTR 을 시간봉 5개(2분 · 5분 · 10분 · 1시간 · 2시간)로 목록
  + 7일 평균 · 20일 평균. 평소(20일 평균)보다 5분 KTR 이 1.3배 넘게 큰 날 🔥, 0.7배 안 되는 날 💤
  버튼: 장 바꾸기 · 종목 바꾸기 · 오늘 표(/ktr)
  /ktr 표 아래 [📅 최근 7일] 버튼으로도 열림

── 2) 장 시작 자동 브리핑 ──────────────────────────────────────────
  장마다 10분봉이 끝나면(장 시작 + 10분) 나스닥 · 금의 오늘 KTR(2분 · 5분 · 10분)과 평소(20일 평균) 대비 배수를 보냄.
  1시간 · 2시간 KTR 은 그 봉이 끝나면 같은 메시지를 고쳐서 채움 (메시지가 여러 개 오지 않게).
  봉이 하나도 없으면(휴장) 보내지 않음.
  끄기 · 켜기: /ktr 브리핑 끄기 · /ktr 브리핑 켜기 (data/settings.json "ktr_brief": {"enabled", "groups"})

── 계산 ────────────────────────────────────────────────────────────
  KTR 은 구간 탭 · /ktr 과 같은 계산 (services/grid.py - 장 시작 봉, 아시아장은 직전 + 첫 봉)
  지난 장 KTR 은 data/analysis.db 의 ktr 표에 저장 (종목 묶음 이름으로) → 한 번 구한 날은 다시 묻지 않음
  봉이 없는 지난 날(휴장)은 0 으로 저장해 다시 묻지 않음
  시세: 잠금을 켠 계좌 중 MT5 워커가 응답하는 첫 계좌
"""
from __future__ import annotations

import asyncio
import html
import json
import os
import time
from datetime import datetime, timedelta

from services import grid

KST = grid.KST
TFS = ("2m", "5m", "10m", "1h", "2h")
TF_KR = {"2m": "2분", "5m": "5분", "10m": "10분", "1h": "1시간", "2h": "2시간"}
SHORT = {"asia": "아시아", "euro": "유로", "us": "미장"}
DAYS, AVG_DAYS = 7, 20
HOT, COLD = 1.3, 0.7
LOOP_SEC = 30
BRIEF_AFTER = timedelta(minutes=10)     # 장 시작 뒤 이만큼 (10분봉이 끝나면) 첫 브리핑
BRIEF_UNTIL = timedelta(hours=3)        # 이 안에 1시간 · 2시간 KTR 을 채움


def _state_path() -> str:
    from db.session import DATA_DIR
    return os.path.join(DATA_DIR, "ktr_brief.json")


def cfg() -> dict:
    from services.settings_service import get_settings
    c = get_settings().get("ktr_brief") or {}
    return {"enabled": c.get("enabled", True), "groups": c.get("groups") or ["nasdaq", "gold"]}


def _label(group: str) -> str:
    from services import symbols
    return (symbols.group_by_key(group) or {}).get("label", group)


# ── KTR 한 개 (저장) ──
async def ktr_on(aid: int, group: str, sid: str, opened: datetime, tf: str, now: datetime) -> float | None:
    """그 장 · 그 시간봉의 KTR. 첫 봉이 아직 안 끝났으면 None. 지난 장은 저장해 두고 다시 안 물음"""
    from services import analysis
    if now < opened + timedelta(minutes=grid.TIMEFRAMES[tf][1]):
        return None
    key = (f"group:{group}", tf, int(opened.timestamp()))
    with analysis._lock:
        r = analysis._db().execute("SELECT ktr FROM ktr WHERE symbol=? AND tf=? AND open_ts=?", key).fetchone()
    if r is not None:
        return r[0] or None
    span = timedelta(minutes=grid.TIMEFRAMES[tf][1])
    v = None
    try:
        cs = await grid._candles(aid, group, "mt5", tf, opened - span * 3 - (timedelta(days=3) if sid == "asia" else timedelta()),
                                 opened + span * 2)
        v = grid._ktr_from(cs, sid, opened, tf, "mt5")["ktr"]
    except grid.GridError as e:
        if "MT5 봉을 받지 못했습니다" in str(e):
            raise                                    # 워커 문제 - 저장하지 않음
    except Exception:
        raise
    old = now - opened > timedelta(hours=26)
    if (v and v > 0) or old:                         # 지난 날 봉이 없으면(휴장) 0 으로 저장
        with analysis._lock:
            analysis._db().execute("INSERT OR REPLACE INTO ktr VALUES (?,?,?,?)", (*key, v if v and v > 0 else 0))
            analysis._db().commit()
    return v if v and v > 0 else None


def past_opens(sid: str, now: datetime, n: int) -> list[datetime]:
    """지금 이전 가장 최근의 그 장 시작부터 거꾸로 n 개 (브로커 날짜 월~금)"""
    out, t = [], now
    for _ in range(n * 2 + 4):
        if len(out) >= n:
            break
        try:
            o = grid.session_open(sid, t)
        except grid.GridError:
            break
        out.append(o)
        t = o - timedelta(minutes=1)
    return out


async def history(aid: int, group: str, sid: str, n: int, now: datetime) -> list[tuple[datetime, dict]]:
    """최근 n 개 장 [(시작, {시간봉: KTR})] - 최신 먼저"""
    opens = past_opens(sid, now, n)
    sem = asyncio.Semaphore(4)

    async def one(o, tf):
        async with sem:
            return await ktr_on(aid, group, sid, o, tf, now)
    res = await asyncio.gather(*(one(o, tf) for o in opens for tf in TFS))
    out = []
    for i, o in enumerate(opens):
        out.append((o, {tf: res[i * len(TFS) + j] for j, tf in enumerate(TFS)}))
    return out


def _avg(rows: list[tuple[datetime, dict]], tf: str) -> float | None:
    v = [d[tf] for _, d in rows if d.get(tf)]
    return sum(v) / len(v) if len(v) >= 3 else None


async def _worker_account() -> int | None:
    """잠금을 켠 계좌 중 워커가 응답하는 첫 계좌"""
    from services.oneway import _account_ids
    from services.worker_client import worker_get
    for aid in await _account_ids():
        try:
            await asyncio.wait_for(worker_get(aid, "/account-info"), 5)
            return aid
        except Exception:
            continue
    return None


def _pad(t: str, n: int, right: bool = True) -> str:
    from services.telegram_grid import _pad as p
    return p(t, n, right)


def _k(v) -> str:
    if v is None:
        return "-"
    return f"{v:.0f}" if v >= 1000 else f"{v:.1f}" if v >= 100 else f"{v:.2f}" if v >= 1 else f"{v:.3f}"


def _mark(r: float | None) -> str:
    return "🔥" if r and r >= HOT else "💤" if r and r <= COLD else ""


# ── 1) 최근 7일 ──
async def recent_text(aid: int, group: str, sid: str, now: datetime) -> str:
    rows = await history(aid, group, sid, AVG_DAYS, now)
    week = rows[:DAYS]
    a7 = {tf: _avg(week, tf) for tf in TFS}
    a20 = {tf: _avg(rows, tf) for tf in TFS}
    W = 6
    head = _pad("", 6, False) + "".join(_pad(TF_KR[tf], W) for tf in TFS)
    lines = []
    for o, d in week:
        day = o.astimezone(KST).strftime("%m/%d")
        if not any(d.values()):
            lines.append(_pad(day, 6, False) + "  휴장 · 봉 없음")
            continue
        r = (d.get("5m") / a20["5m"]) if d.get("5m") and a20.get("5m") else None
        lines.append(_pad(day, 6, False) + "".join(_pad(_k(d.get(tf)), W) for tf in TFS) + " " + _mark(r))
    lines.append(_pad("7일", 6, False) + "".join(_pad(_k(a7[tf]), W) for tf in TFS))
    lines.append(_pad("20일", 6, False) + "".join(_pad(_k(a20[tf]), W) for tf in TFS))
    first = week[0][0].astimezone(KST) if week else None
    return (f"📅 <b>{html.escape(_label(group))} {SHORT[sid]} KTR · 최근 {DAYS}거래일</b> (MT5)\n"
            f"<pre>{html.escape(head)}\n" + "\n".join(html.escape(x) for x in lines) + "</pre>"
            f"<i>날짜 = 장 시작(한국 시간){' ' + first.strftime('%H:%M') if first else ''} · 아시아장은 직전+첫 봉 · "
            f"🔥 5분 KTR 이 20일 평균의 {HOT}배 넘음 · 💤 {COLD}배 안 됨 · - = 아직 봉이 안 끝남</i>")


def recent_markup(group: str, sid: str) -> dict:
    from services import symbols
    sess = [{"text": ("✓ " if s == sid else "") + SHORT[s], "callback_data": f"ktr7:{group}:{s}"} for s in ("asia", "euro", "us")]
    grp = [{"text": ("✓ " if g["key"] == group else "") + g.get("label", g["key"]), "callback_data": f"ktr7:{g['key']}:{sid}"}
           for g in symbols.groups()][:5]
    return {"inline_keyboard": [sess, grp, [{"text": "📏 오늘 표", "callback_data": f"ktr:{group}:mt5"},
                                            {"text": "🔄", "callback_data": f"ktr7:{group}:{sid}"}]]}


def _parse(args: str) -> tuple[str | None, str | None, str | None]:
    """'/ktr 최근' 뒤 글자 → (종목 묶음, 장, 브리핑 켜기 · 끄기)"""
    from services import symbols
    from services.telegram_grid import parse
    sym = parse(args)["sym"]
    sid = onoff = None
    for w in args.split():
        w = w.lower()
        if not sym and w in ("금", "골드", "gold", "xau"):
            sym = "gold" if symbols.group_by_key("gold") else None
        elif not sym and w in ("나스닥", "나스", "nasdaq", "nas", "nq"):
            sym = "nasdaq" if symbols.group_by_key("nasdaq") else None
        if w in ("아시아", "아시아장", "asia"):
            sid = "asia"
        elif w in ("유로", "유로장", "유럽", "euro"):
            sid = "euro"
        elif w in ("미장", "미국", "뉴욕", "us"):
            sid = "us"
        elif w in ("켜기", "켬", "on"):
            onoff = "on"
        elif w in ("끄기", "끔", "off"):
            onoff = "off"
    return sym, sid, onoff


async def cmd(live, client, token, chat_id: str, args: str, message_id=None) -> None:
    """/ktr 최근 [종목] [장]  ·  /ktr 브리핑 [켜기|끄기]"""
    sym, sid, onoff = _parse(args)
    if "브리핑" in args or "brief" in args.lower():
        from services.settings_service import update_settings
        c = cfg()
        if onoff:
            update_settings({"ktr_brief": {**c, "enabled": onoff == "on"}})
            c = cfg()
        await live._send(client, token, chat_id,
                         f"📏 장 시작 KTR 브리핑: <b>{'켜짐' if c['enabled'] else '꺼짐'}</b> · "
                         f"{' · '.join(html.escape(_label(g)) for g in c['groups'])}\n"
                         "<i>/ktr 브리핑 끄기 · /ktr 브리핑 켜기</i>")
        return
    group = sym or (cfg()["groups"][0] if cfg()["groups"] else "nasdaq")
    now = datetime.now(KST)
    sid = sid or grid.current_session(now)
    aid = await _worker_account()
    if aid is None:
        text = "❌ MT5 워커가 켜진 계좌가 없어 KTR 을 구하지 못했습니다."
    else:
        if not message_id:
            r = await live._send(client, token, chat_id, f"📅 {html.escape(_label(group))} {SHORT[sid]} 최근 {DAYS}거래일 KTR 계산 중… "
                                 "(처음엔 20일치를 MT5 에서 받느라 조금 걸려요)")
            message_id = ((r or {}).get("result") or {}).get("message_id")
        try:
            text = await recent_text(aid, group, sid, now)
        except Exception as e:
            text = f"❌ KTR 을 구하지 못했습니다: {html.escape(str(getattr(e, 'detail', e)))}"
    markup = recent_markup(group, sid)
    if message_id:
        await live._call(client, token, "editMessageText", chat_id=chat_id, message_id=message_id, text=text,
                         parse_mode="HTML", reply_markup=markup)
    else:
        await live._send(client, token, chat_id, text, reply_markup=markup)


async def button(live, client, token, chat_id: str, data: str, message_id) -> str:
    """ktr7:<종목>:<장>"""
    p = data.split(":")
    await cmd(live, client, token, chat_id, f"{p[1] if len(p) > 1 else ''} {p[2] if len(p) > 2 else ''}", message_id)
    return "최근 KTR"


# ── 2) 장 시작 자동 브리핑 ──
def _tg(method: str, **params) -> dict:
    import requests
    from services.settings_service import telegram_cfg
    c = telegram_cfg()
    if not (c["bot_token"] and c["chat_id"]):
        return {}
    r = requests.post(f"https://api.telegram.org/bot{c['bot_token']}/{method}",
                      json={"chat_id": c["chat_id"], **params}, timeout=10)
    try:
        return r.json()
    except ValueError:
        return {}


def _load_state() -> dict:
    try:
        return json.load(open(_state_path(), encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(st: dict) -> None:
    try:
        tmp = _state_path() + ".tmp"
        json.dump(st, open(tmp, "w", encoding="utf-8"))
        os.replace(tmp, _state_path())
    except OSError:
        pass


def brief_text(sid: str, opened: datetime, now: datetime, data: dict) -> str:
    """data: {group: {"today": {tf: ktr}, "avg": {tf: ktr}}}"""
    out = [f"📏 <b>{SHORT[sid]} 시작 KTR</b> · {opened.astimezone(KST):%m/%d %H:%M}"]
    for g, d in data.items():
        lines = [_pad(_label(g), 7, False) + _pad("오늘", 8) + _pad("평소", 8) + _pad("배", 6)]
        for tf in TFS:
            t, a = d["today"].get(tf), d["avg"].get(tf)
            if t is None:
                due = opened + timedelta(minutes=grid.TIMEFRAMES[tf][1])
                lines.append(_pad(TF_KR[tf], 7, False) + _pad("…", 8) + _pad(_k(a), 8) + _pad(f"{due.astimezone(KST):%H:%M}", 7))
                continue
            r = t / a if a else None
            lines.append(_pad(TF_KR[tf], 7, False) + _pad(_k(t), 8) + _pad(_k(a), 8)
                         + _pad(f"{r:.1f}" if r else "-", 6) + _mark(r))
        out.append("<pre>" + "\n".join(html.escape(x) for x in lines) + "</pre>")
    out.append(f"<i>평소 = 지난 {AVG_DAYS}거래일 같은 장 평균 · 🔥 {HOT}배 넘음 · 💤 {COLD}배 안 됨 · … = 그 봉이 끝나는 시각에 채움</i>")
    return "\n".join(out)


async def _brief_once(now: datetime, st: dict) -> None:
    c = cfg()
    if not c["enabled"] or not c["groups"]:
        return
    for sid in ("asia", "euro", "us"):
        try:
            opened = grid.session_open(sid, now)
        except grid.GridError:
            continue
        if not (opened + BRIEF_AFTER <= now <= opened + BRIEF_UNTIL):
            continue
        key = f"{sid}|{opened.isoformat()}"
        s = st.get(key) or {}
        if s.get("done"):
            continue
        aid = await _worker_account()
        if aid is None:
            return
        data = {}
        for g in c["groups"]:
            today = {}
            for tf in TFS:
                try:
                    today[tf] = await ktr_on(aid, g, sid, opened, tf, now)
                except Exception:
                    today[tf] = None
            hist = await history(aid, g, sid, AVG_DAYS + 1, now)
            past = [(o, d) for o, d in hist if o < opened][:AVG_DAYS]
            data[g] = {"today": today, "avg": {tf: _avg(past, tf) for tf in TFS}}
        if not any(v for d in data.values() for v in d["today"].values()):
            continue                                 # 봉이 하나도 없음 (휴장)
        filled = sorted(tf for d in data.values() for tf, v in d["today"].items() if v)
        if s.get("filled") == filled:
            continue
        text = brief_text(sid, opened, now, data)
        markup = {"inline_keyboard": [[{"text": f"📅 {_label(g)} 최근 {DAYS}일", "callback_data": f"ktr7:{g}:{sid}"}
                                       for g in c["groups"]]]}
        if s.get("mid"):
            await asyncio.to_thread(_tg, "editMessageText", message_id=s["mid"], text=text, parse_mode="HTML",
                                    reply_markup=markup, disable_web_page_preview=True)
        else:
            r = await asyncio.to_thread(_tg, "sendMessage", text=text, parse_mode="HTML", reply_markup=markup,
                                        disable_web_page_preview=True)
            s["mid"] = ((r or {}).get("result") or {}).get("message_id")
        s["filled"] = filled
        s["done"] = all(d["today"].get(tf) for d in data.values() for tf in TFS)
        st[key] = s
        _save_state(st)


async def loop() -> None:
    await asyncio.sleep(40)
    st = _load_state()
    while True:
        try:
            now = datetime.now(KST)
            await _brief_once(now, st)
            old = (now - timedelta(days=3)).isoformat()
            for k in [k for k in st if k.split("|", 1)[-1] < old]:
                st.pop(k, None)
        except Exception as e:
            print(f"[ktr_brief] 브리핑 실패: {e}")
        await asyncio.sleep(LOOP_SEC)
