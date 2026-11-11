"""텔레그램 /손익 (/pnl) · /통계 (/stats) - 웹 손익·통계 탭을 텔레그램에서 보기

  /손익            → 오늘 손익 (계좌별 순손익·승률·종목별·일별, 오늘은 출금 가능액까지)
  /손익 어제|주|지난주|월|지난달 처럼 기간을 붙여도 됨
  /통계            → 이번 달 통계 (PF·손익비·기대값·최대 연패·최대 낙폭·종목/방향/장/요일/보유 시간별)

아래 버튼으로 기간·계좌·화면(손익↔통계)을 바꾸면 같은 메시지를 고쳐서 보여줌.
버튼 콜백: rp:<p|s>:<기간>:<계좌id 또는 0=전체>

기준은 웹과 같음
  - 하루 = 거래일 (한국 시간 07:00, 미국 서머타임 아닐 때 08:00 에 바뀜 - services/loss_limit.py)
  - 순손익 = 손익 + 수수료 (청산된 거래만, 열린 포지션 평가손익은 빠짐)
  - 계좌: 잠금을 켠 계좌 (없으면 전체)
"""
from __future__ import annotations

import html
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from db.models.account import Account
from db.models.trade import Trade
from db.session import AsyncSessionLocal

KST = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")
WEEK = "월화수목금토일"

PERIODS = [("d0", "오늘"), ("d1", "어제"), ("w0", "이번 주"), ("w1", "지난주"), ("m0", "이번 달"), ("m1", "지난달")]
PERIOD_NAME = dict(PERIODS)
ALIASES = {
    "오늘": "d0", "today": "d0", "어제": "d1", "yesterday": "d1",
    "주": "w0", "이번주": "w0", "week": "w0", "지난주": "w1", "lastweek": "w1",
    "월": "m0", "달": "m0", "이번달": "m0", "month": "m0", "지난달": "m1", "lastmonth": "m1",
}
REASON_KR = {"sl": "SL", "tp": "TP", "manual": "직접", "auto": "자동", "stopout": "강제", "lock": "잠금", "loss_limit": "손실한도"}


# ── 기간 ──
def _tday(now: datetime) -> date:
    from services.loss_limit import trading_day
    return date.fromisoformat(trading_day(now))


def _bounds(d0: date, d1: date) -> tuple[datetime, datetime]:
    """거래일 d0 ~ d1(포함) → [시작, 끝) 한국 시간"""
    a, b = d0 - timedelta(days=1), d1
    return (datetime(a.year, a.month, a.day, 18, 0, tzinfo=NY).astimezone(KST),
            datetime(b.year, b.month, b.day, 18, 0, tzinfo=NY).astimezone(KST))


def period_range(code: str, now: datetime | None = None) -> tuple[date, date]:
    today = _tday(now or datetime.now(KST))
    if code == "d1":
        d = today - timedelta(days=1)
        if d.weekday() >= 5:                         # 어제가 주말이면 금요일
            d -= timedelta(days=d.weekday() - 4)
        return d, d
    if code in ("w0", "w1"):
        mon = today - timedelta(days=today.weekday()) - (timedelta(days=7) if code == "w1" else timedelta())
        return mon, mon + timedelta(days=6)
    if code in ("m0", "m1"):
        first = today.replace(day=1)
        if code == "m1":
            first = (first - timedelta(days=1)).replace(day=1)
        nxt = (first + timedelta(days=32)).replace(day=1)
        return first, nxt - timedelta(days=1)
    return today, today


def _trade_day(t: datetime) -> date:
    return _tday(t)


def _broker_min(t: datetime) -> int:
    b = t.astimezone(NY) + timedelta(hours=7)        # 브로커 시간 = 뉴욕 + 7시간
    return b.hour * 60 + b.minute


# ── 숫자 모양 ──
def _s(v: float, d: int = 2) -> str:
    return f"{'+' if v > 0 else ''}{v:,.{d}f}"


def _m(v: float) -> str:
    return f"{v:,.2f}"


def _dot(v: float) -> str:
    return "🟢" if v > 0 else "🔴" if v < 0 else "⚪"


def _dur(sec: float) -> str:
    sec = int(sec)
    if sec < 3600:
        return f"{sec // 60}분"
    if sec < 86400:
        return f"{sec // 3600}시간 {sec % 3600 // 60}분"
    return f"{sec // 86400}일 {sec % 86400 // 3600}시간"


# ── 자료 ──
async def _accounts(db) -> list[Account]:
    accs = (await db.execute(select(Account).where(Account.lock_enabled == True).order_by(Account.id))).scalars().all()
    return accs or (await db.execute(select(Account).order_by(Account.id))).scalars().all()


async def _load(code: str, acc: int) -> tuple[list[Account], list[Trade], date, date]:
    d0, d1 = period_range(code)
    start, end = _bounds(d0, d1)
    async with AsyncSessionLocal() as db:
        accs = await _accounts(db)
        ids = [a.id for a in accs if not acc or a.id == acc] or [a.id for a in accs]
        rows = (await db.execute(select(Trade).where(
            Trade.account_id.in_(ids), Trade.close_time != None,
            Trade.close_time >= start, Trade.close_time < end).order_by(Trade.close_time))).scalars().all()
    return accs, list(rows), d0, d1


def _net(t: Trade) -> float:
    return (t.profit or 0) + (t.commission or 0)


def _basic(ts: list[Trade]) -> dict:
    nets = [_net(t) for t in ts]
    wins = [v for v in nets if v > 0]
    losses = [v for v in nets if v < 0]
    gw, gl = sum(wins), -sum(losses)
    peak = run = mdd = 0.0
    streak = max_streak = 0
    for v in nets:
        run += v
        peak = max(peak, run)
        mdd = max(mdd, peak - run)
        streak = streak + 1 if v < 0 else 0
        max_streak = max(max_streak, streak)
    aw = gw / len(wins) if wins else 0
    al = gl / len(losses) if losses else 0
    return {
        "n": len(ts), "net": sum(nets), "profit": sum(t.profit or 0 for t in ts),
        "fee": sum(t.commission or 0 for t in ts), "wins": len(wins), "losses": len(losses),
        "wr": len(wins) / len(ts) * 100 if ts else 0,
        "pf": (gw / gl) if gl else None, "aw": aw, "al": al, "rr": (aw / al) if al else None,
        "exp": sum(nets) / len(ts) if ts else 0,
        "lots": sum(t.volume or 0 for t in ts),
        "mdd": mdd, "streak": max_streak,
        "best": max(nets) if nets else 0, "worst": min(nets) if nets else 0,
    }


def _sym_label(sym: str) -> str:
    from services.symbols import group_of
    g = group_of(sym)
    return f"{g.get('icon', '')} {g['label']}".strip() if g else sym


def _period_title(code: str, d0: date, d1: date) -> str:
    if d0 == d1:
        return f"{PERIOD_NAME.get(code, '')} {d0.month}/{d0.day}({WEEK[d0.weekday()]})"
    return f"{PERIOD_NAME.get(code, '')} {d0.month}/{d0.day}~{d1.month}/{d1.day}"


def _acc_name(accs: list[Account], acc: int) -> str:
    if not acc:
        return "전체 계좌" if len(accs) > 1 else (accs[0].account_number if accs else "")
    a = next((x for x in accs if x.id == acc), None)
    return a.account_number if a else str(acc)


async def _withdraw_today(acc_list: list[Account]) -> list[str]:
    """오늘 출금 가능액 (웹 손익 탭 카드와 같은 계산). 실패하면 빈 목록"""
    out = []
    try:
        from api.account_router import get_withdraw_rule
    except Exception:
        return out
    for a in acc_list:
        try:
            async with AsyncSessionLocal() as db:
                r = await get_withdraw_rule(int(a.account_number), db)
        except Exception:
            continue
        line = f"💸 출금 가능 <b>{_m(r['withdrawable'])}</b> (오늘 순수익 {_s(r['net_profit'])}의 {r['pct']:g}%"
        if r.get("withdrawal_total"):
            line += f" - 출금 {_m(r['withdrawal_total'])}"
        line += ")"
        if r.get("over"):
            line += f" ⚠️ {_m(r['over'])} 초과 출금"
        if len(acc_list) > 1:
            line = f"{html.escape(a.account_number)} " + line
        out.append(line)
    return out


# ── 화면 ──
def _markup(view: str, code: str, acc: int, accs: list[Account]) -> dict:
    kb, row = [], []
    for c, name in PERIODS:
        row.append({"text": ("• " if c == code else "") + name, "callback_data": f"rp:{view}:{c}:{acc}"})
        if len(row) == 3:
            kb.append(row)
            row = []
    if len(accs) > 1:
        kb.append([{"text": ("• " if acc == 0 else "") + "전체", "callback_data": f"rp:{view}:{code}:0"}] +
                  [{"text": ("• " if acc == a.id else "") + a.account_number[-4:], "callback_data": f"rp:{view}:{code}:{a.id}"}
                   for a in accs[:4]])
    other = ("s", "📊 통계") if view == "p" else ("p", "📅 손익")
    kb.append([{"text": "🔄 새로고침", "callback_data": f"rp:{view}:{code}:{acc}"},
               {"text": other[1], "callback_data": f"rp:{other[0]}:{code}:{acc}"}])
    return {"inline_keyboard": kb}


async def pnl_view(code: str, acc: int) -> tuple[str, dict]:
    accs, ts, d0, d1 = await _load(code, acc)
    B = _basic(ts)
    L = [f"📅 <b>손익</b> · {_period_title(code, d0, d1)} · {html.escape(_acc_name(accs, acc))}", ""]
    if not ts:
        L.append("청산된 거래가 없습니다.")
    else:
        L.append(f"{_dot(B['net'])} 순손익 <b>{_s(B['net'])}</b>  (손익 {_s(B['profit'])} · 수수료 {_m(B['fee'])})")
        L.append(f"거래 {B['n']}건 · 승 {B['wins']} 패 {B['losses']} · 승률 {B['wr']:.0f}% · {B['lots']:.2f}랏")
        pf = "∞" if B["pf"] is None and B["wins"] else ("-" if B["pf"] is None else f"{B['pf']:.2f}")
        L.append(f"PF {pf} · 평균 이익 {_m(B['aw']) if B['wins'] else '-'} · 평균 손실 {'-' + _m(B['al']) if B['losses'] else '-'}")

    # 오늘이면 출금 가능액
    if code == "d0":
        shown = [a for a in accs if not acc or a.id == acc]
        wl = await _withdraw_today(shown) if shown else []
        if wl:
            L += [""] + wl

    if ts:
        # 계좌별 (전체 보기에서 계좌가 여럿일 때)
        if not acc and len({t.account_id for t in ts}) > 1:
            L += ["", "<b>계좌별</b>"]
            num = {a.id: a.account_number for a in accs}
            for aid in sorted({t.account_id for t in ts}):
                b = _basic([t for t in ts if t.account_id == aid])
                L.append(f"{_dot(b['net'])} {html.escape(num.get(aid, str(aid)))}  {_s(b['net'])} · {b['n']}건 {b['wr']:.0f}%")
        # 종목별
        by = defaultdict(list)
        for t in ts:
            by[_sym_label(t.symbol)].append(t)
        L += ["", "<b>종목별</b>"]
        for k, g in sorted(by.items(), key=lambda kv: -abs(sum(map(_net, kv[1])))):
            b = _basic(g)
            L.append(f"{_dot(b['net'])} {html.escape(k)}  {_s(b['net'])} · {b['n']}건 {b['wr']:.0f}%")
        # 일별 (기간이 하루보다 길 때)
        if d0 != d1:
            days = defaultdict(list)
            for t in ts:
                days[_trade_day(t.close_time)].append(t)
            L += ["", "<b>일별</b>"]
            for d in sorted(days, reverse=True):
                b = _basic(days[d])
                L.append(f"{_dot(b['net'])} {d.month:02d}.{d.day:02d}({WEEK[d.weekday()]})  {_s(b['net'])} · {b['n']}건")
            wd = [_basic(v)["net"] for v in days.values()]
            L.append(f"이긴 날 {sum(1 for v in wd if v > 0)} · 진 날 {sum(1 for v in wd if v < 0)} · "
                     f"최고 {_s(max(wd))} · 최저 {_s(min(wd))}")
        # 하루면 거래 목록 (최근 15건)
        else:
            L += ["", "<b>거래</b>"]
            for t in ts[-15:][::-1]:
                ct = t.close_time.astimezone(KST)
                why = REASON_KR.get(t.close_reason or "", "")
                L.append(f"{_dot(_net(t))} {ct:%H:%M} {html.escape(_sym_label(t.symbol))} {'매수' if (t.position or '').lower() in ('buy', '0') else '매도'} "
                         f"{t.volume or 0:g}랏  {_s(_net(t))}" + (f" · {why}" if why else ""))
            if len(ts) > 15:
                L.append(f"… 외 {len(ts) - 15}건 (웹 거래내역에서 전체)")
    return "\n".join(L)[:4000], _markup("p", code, acc, accs)


def _bucket_lines(groups: dict, order=None) -> list[str]:
    keys = order if order is not None else sorted(groups, key=lambda k: -_basic(groups[k])["net"])
    out = []
    for k in keys:
        if k not in groups:
            continue
        b = _basic(groups[k])
        out.append(f"{_dot(b['net'])} {html.escape(str(k))}  {_s(b['net'])} · {b['n']}건 {b['wr']:.0f}%")
    return out


def _session_of(t: Trade) -> str:
    from services.settings_service import lock_cfg
    m = _broker_min(t.open_time)
    for s in lock_cfg().get("sessions") or []:
        a, b = s.get("start_min", 0), s.get("end_min", 0)
        if (a <= m < b) if a <= b else (m >= a or m < b):
            return s.get("name") or s.get("id")
    kh = t.open_time.astimezone(KST).hour
    return "유럽장 시간" if 16 <= kh < 21 else "미국장 시간" if kh >= 21 or kh < 6 else "그 밖"


def _hold_bucket(t: Trade) -> str:
    if not t.open_time:
        return "?"
    s = (t.close_time - t.open_time).total_seconds()
    return "5분 미만" if s < 300 else "5~30분" if s < 1800 else "30분~2시간" if s < 7200 else "2시간~1일" if s < 86400 else "1일 이상"


async def stats_view(code: str, acc: int) -> tuple[str, dict]:
    accs, ts, d0, d1 = await _load(code, acc)
    B = _basic(ts)
    L = [f"📊 <b>통계</b> · {_period_title(code, d0, d1)} · {html.escape(_acc_name(accs, acc))}", ""]
    if not ts:
        L.append("청산된 거래가 없습니다.")
        return "\n".join(L), _markup("s", code, acc, accs)
    pf = "∞" if B["pf"] is None else f"{B['pf']:.2f}"
    rr = "-" if B["rr"] is None else f"{B['rr']:.2f}"
    lots = B["lots"] or 0
    L += [
        f"{_dot(B['net'])} 순손익 <b>{_s(B['net'])}</b> · {B['n']}건 · 승률 {B['wr']:.0f}%",
        f"PF {pf} · 손익비 {rr}",
        f"평균 이익 {_m(B['aw'])} · 평균 손실 -{_m(B['al'])}",
        f"기대값 {_s(B['exp'])}/건" + (f" · {_s(B['net'] / lots * 0.01)}/0.01랏" if lots else ""),
        f"최대 연패 {B['streak']}번 · 최대 낙폭 -{_m(B['mdd'])}",
        f"최고 {_s(B['best'])} · 최악 {_s(B['worst'])}",
    ]
    holds = [(t.close_time - t.open_time).total_seconds() for t in ts if t.open_time]
    hw = [(t.close_time - t.open_time).total_seconds() for t in ts if t.open_time and _net(t) > 0]
    hl = [(t.close_time - t.open_time).total_seconds() for t in ts if t.open_time and _net(t) < 0]
    if holds:
        L.append(f"평균 보유 {_dur(sum(holds) / len(holds))}"
                 + (f" (이익 {_dur(sum(hw) / len(hw))}" if hw else " (") + (f" · 손실 {_dur(sum(hl) / len(hl))})" if hl else ")"))

    def grp(fn):
        g = defaultdict(list)
        for t in ts:
            g[fn(t)].append(t)
        return g

    L += ["", "<b>종목별</b>"] + _bucket_lines(grp(lambda t: _sym_label(t.symbol)))
    L += ["", "<b>방향</b>"] + _bucket_lines(grp(lambda t: "매수" if (t.position or "").lower() in ("buy", "0") else "매도"))
    L += ["", "<b>장 (진입 시각)</b>"] + _bucket_lines(grp(_session_of))
    if d0 != d1:
        L += ["", "<b>요일</b>"] + _bucket_lines(grp(lambda t: WEEK[_trade_day(t.close_time).weekday()]), order=list(WEEK))
    L += ["", "<b>보유 시간</b>"] + _bucket_lines(grp(_hold_bucket),
                                                order=["5분 미만", "5~30분", "30분~2시간", "2시간~1일", "1일 이상", "?"])
    reasons = grp(lambda t: REASON_KR.get(t.close_reason or "", "기록 없음"))
    if len(reasons) > 1 or "기록 없음" not in reasons:
        L += ["", "<b>청산 이유</b>"] + _bucket_lines(reasons)
    return "\n".join(L)[:4000], _markup("s", code, acc, accs)


# ── 텔레그램 연결 ──
def _parse_arg(arg: str) -> str:
    a = "".join((arg or "").split()).lower()
    return ALIASES.get(a, "")


async def cmd(live, client, token, cid: str, view: str, arg: str = "") -> None:
    code = _parse_arg(arg) or ("d0" if view == "p" else "m0")
    try:
        text, kb = await (pnl_view if view == "p" else stats_view)(code, 0)
    except Exception as e:
        await live._send(client, token, cid, f"불러오기 실패: {html.escape(str(e))[:200]}")
        return
    await live._send(client, token, cid, text, reply_markup=kb)


async def button(live, client, token, cid: str, data: str, message_id) -> str:
    parts = data.split(":")
    view = parts[1] if len(parts) > 1 and parts[1] in ("p", "s") else "p"
    code = parts[2] if len(parts) > 2 and parts[2] in PERIOD_NAME else "d0"
    try:
        acc = int(parts[3]) if len(parts) > 3 else 0
    except ValueError:
        acc = 0
    text, kb = await (pnl_view if view == "p" else stats_view)(code, acc)
    if message_id:
        r = await live._call(client, token, "editMessageText", chat_id=cid, message_id=message_id,
                             text=text, parse_mode="HTML", reply_markup=kb)
        if r.get("ok") or "not modified" in str(r.get("description", "")):
            return ""
    await live._send(client, token, cid, text, reply_markup=kb)
    return ""
