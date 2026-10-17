# services/telegram_grid.py
#
# 텔레그램 구간매매 (services/telegram_live.py 가 명령·버튼을 넘겨줌). 계산·주문은 services/grid.py - 웹 '구간' 탭과 같음.
#
#   /ktr [종목] [oanda]        장 3개 × 시간봉 5개 KTR 표 (종목·시세 버튼으로 바꾸기)
#   /grid 나스닥 매수 5% 5구간              KTR 거래 (손실 %로 랏 계산, 손절 = 마지막 구간 뒤 1 KTR - '여유0.5' 처럼 바꿈)
#   /grid 골드 매도 물타기 0.1랏 5구간 손절6  일반 물타기 (손절 KTR 배수) - '손실5%' 로 쓰면 손실 %로 손절 자동
#     물타기는 구간 수를 빼면 자동 (손절선과 최소 여유를 두고 증거금 안에서 가장 많이)
#     손절가20430 - 주요 고점·저점을 손절가로 (KTR 거래·물타기 둘 다), 구간 수 자동
#     더 넣을 수 있는 것: 아시아/유로/미장, 2분/5분/10분/1시간/2시간 (기본: 지금 장 · 2분), 차등, oanda, ktr=62.4, #계좌번호
#   → 미리보기 + [주문 실행] 버튼 (지금 시세로 다시 계산해서 주문, 거래 잠금 중이면 거절)
#   /grids                    진행 중인 구간매매 + [대기 주문 취소] [전부 청산]

import html
import re
import secrets
import time

from services import grid, symbols

PENDING: dict[str, dict] = {}        # 미리보기 id → 주문 내용 (10분 동안)
PENDING_TTL = 600
TF_WORDS = {"2분": "2m", "5분": "5m", "10분": "10m", "1시간": "1h", "2시간": "2h", "60분": "1h", "120분": "2h",
            "2m": "2m", "5m": "5m", "10m": "10m", "1h": "1h", "2h": "2h", "m2": "2m", "m5": "5m", "m10": "10m", "h1": "1h", "h2": "2h"}
SESS_WORDS = {"아시아": "asia", "아시아장": "asia", "asia": "asia", "유로": "euro", "유로장": "euro", "유럽": "euro", "euro": "euro",
              "미장": "us", "미국": "us", "미국장": "us", "뉴욕": "us", "us": "us"}
TF_KR = {"2m": "2분", "5m": "5분", "10m": "10분", "1h": "1시간", "2h": "2시간"}

HELP = ("<b>/grid 구간매매</b>\n"
        "• <code>/grid 나스닥 매수 5% 5구간</code> - KTR 거래: 손실 5% 안에서 구간마다 랏 계산\n"
        "• <code>/grid 나스닥 매수 5% 5구간 여유0.5</code> - 손절을 마지막 구간에서 0.5 KTR 뒤로 (안 쓰면 기본 1)\n"
        "• <code>/grid 골드 매도 물타기 0.1랏 5구간 손절6</code> - 일반 물타기: 같은 랏, 손절 6 KTR\n"
        "• <code>/grid 골드 매도 물타기 0.1랏 5구간 손실5%</code> - 손절을 손실 %로 자동\n"
        "• <code>/grid 골드 매도 물타기 0.1랏 손실5%</code> - 구간 수도 자동 (손절선과 여유를 두고 가장 많이)\n"
        "• <code>/grid 나스닥 매수 1% 손절가20430</code> - 주요 고점·저점을 손절가로, 그 안에 들어가는 구간 수 자동 (물타기도 됨)\n"
        "더 넣을 수 있는 것: <code>아시아/유로/미장</code> <code>2분/5분/10분/1시간/2시간</code> (기본: 지금 장 · 2분), "
        "<code>차등</code> (KTR 거래 랏 배분), <code>oanda</code>, <code>ktr=62.4</code> (직접), <code>#계좌번호</code>\n"
        "미리보기를 보고 <b>주문 실행</b>을 눌러야 주문합니다. <code>/grid</code> 만 보내면 버튼으로 하나씩 고릅니다.\n"
        "• <code>/ktr 나스닥</code> - KTR 표   • <code>/grids</code> - 진행 중인 구간매매")


def _group(word: str) -> str | None:
    w = word.lower()
    for g in symbols.groups():
        names = [g["key"], g.get("label", "")] + list(g.get("names") or [])
        if any(w == str(x).lower() for x in names if x) or symbols.group_of(word) is g:
            return g["key"]
    return None


def _w(t: str) -> int:
    """고정폭 글꼴에서 보이는 너비 (한글은 2칸)"""
    return sum(2 if ord(c) > 0x1100 else 1 for c in t)


def _pad(t: str, n: int, right: bool = True) -> str:
    sp = " " * max(0, n - _w(t))
    return sp + t if right else t + sp


def _fmt(v, d=2) -> str:
    return "-" if v is None else f"{v:,.{d}f}"


def _k(v) -> str:
    return "-" if v is None else f"{v:,.{1 if v >= 100 else 2 if v >= 1 else 4}f}"


async def _account(number: str | None):
    from db.session import AsyncSessionLocal
    from services.account import get_account_or_default
    async with AsyncSessionLocal() as db:
        return await get_account_or_default(db, int(number) if number else None)


def parse(args: str) -> dict:
    """'/grid' 뒤 글자 → {body, sym, tf, sess, src, ktr, account}"""
    o = {"body": {"side": None, "mode": "ktr", "method": "equal", "stop_mode": "ktr"},
         "sym": None, "tf": "2m", "sess": None, "src": "mt5", "ktr": None, "account": None, "bad": []}
    b = o["body"]
    pct_seen = None
    for raw in args.replace(",", " ").split():
        w = raw.strip().lower()
        if not w:
            continue
        if w.startswith("#") and w[1:].isdigit():
            o["account"] = w[1:]
        elif w in ("매수", "buy", "b", "롱", "long"):
            b["side"] = "buy"
        elif w in ("매도", "sell", "s", "숏", "short"):
            b["side"] = "sell"
        elif w in ("물타기", "avg", "일반", "일반물타기"):
            b["mode"] = "avg"
        elif w in ("ktr", "ktr거래"):
            b["mode"] = "ktr"
        elif w in ("차등", "차등손실", "half"):
            b["method"] = "half"
        elif w in ("동일", "동일손실", "equal"):
            b["method"] = "equal"
        elif w in ("oanda", "오안다"):
            o["src"] = "oanda"
        elif w in ("mt5",):
            o["src"] = "mt5"
        elif w in TF_WORDS:
            o["tf"] = TF_WORDS[w]
        elif w in SESS_WORDS:
            o["sess"] = SESS_WORDS[w]
        elif m := re.fullmatch(r"ktr[=:]?(\d+(?:\.\d+)?)", w):
            o["ktr"] = float(m.group(1))
        elif m := re.fullmatch(r"(?:손실|risk)?(\d+(?:\.\d+)?)%", w):
            pct_seen = float(m.group(1))
            if w.startswith("손실"):
                b["stop_mode"] = "pct"
        elif m := re.fullmatch(r"(\d+)(?:구간|개|번|n)", w) or re.fullmatch(r"n(\d+)", w):
            b["n"] = int(m.group(1))
        elif m := re.fullmatch(r"(\d*\.?\d+)(?:랏|lot|lots)", w):
            b["lot"] = float(m.group(1))
        elif m := re.fullmatch(r"(?:손절가|sl=|@)(\d*\.?\d+)", w):
            b["stop_price"] = float(m.group(1))
            b["stop_mode"] = "price"
        elif m := re.fullmatch(r"(?:손절여유|여유|gap)(\d*\.?\d+)(?:ktr)?", w):
            b["stop_gap"] = float(m.group(1))
        elif m := re.fullmatch(r"(?:손절|sl|stop)(\d+(?:\.\d+)?)(?:ktr)?", w):
            b["stop_ktr"] = float(m.group(1))
            b["stop_mode"] = "ktr"
        elif (g := _group(raw)) is not None:
            o["sym"] = g
        else:
            o["bad"].append(raw)
    if pct_seen is not None:
        b["risk_pct"] = pct_seen
        if b["mode"] == "avg" and "stop_ktr" not in b and b.get("stop_mode") != "price":
            b["stop_mode"] = "pct"
    return o


def _opts_text(p: dict) -> str:
    """일반 물타기 구간 수별 비교 (자동 N 근처만)"""
    o = p.get("options") or []
    if not o:
        return ""
    cur = p.get("n") if p.get("levels") else None
    c = (cur or p.get("n_auto") or len(o)) - 1
    lo = max(0, min(c - 3, len(o) - 7)) if len(o) > 7 else 0
    rows = [" " + _pad("N", 3) + _pad("손절KTR", 8) + _pad("여유", 6) + _pad("총랏", 6) + _pad("손실", 9)]
    for r in o[lo:lo + 7]:
        mark = "▶" if r["n"] == cur else ("*" if r["n"] == p.get("n_auto") else " ")
        if not r["ok"]:
            rows.append(mark + _pad(str(r["n"]), 3) + "  손절선 안쪽 - 불가")
            continue
        rows.append(mark + _pad(str(r["n"]), 3) + _pad(f"{r['stop_ktr']:.2f}", 8) + _pad(f"{r['gap']:.2f}" + ("" if r["gap_ok"] else "!"), 6)
                    + _pad(f"{r['total_lot']:g}", 6) + _pad(f"{-r['total_loss']:,.0f}", 9) + ("" if r["margin_ok"] else " 증거금X"))
    return (f"구간 수별 (* 자동 · ! 여유 {p.get('min_gap', 1):g} KTR 미만)\n<pre>" + html.escape("\n".join(rows)) + "</pre>")


def _plan_text(p: dict, info: str) -> str:
    d = p.get("digits") or 2
    rows = [f"{lv['no']:>2} {_fmt(lv['price'], d):>11} {lv['lot']:>5.2f}lot {-lv['loss']:>9,.2f}" for lv in p["levels"]]
    lines = [f"🧱 <b>구간매매 미리보기</b> · {grid.MODE_KR.get(p['mode'], p['mode'])}"
             + (" (차등손실)" if p["mode"] == "ktr" and p.get("method") == "half" else ""),
             f"<b>{html.escape(p['symbol'])} {'매수' if p['side'] == 'buy' else '매도'}</b> · KTR {_k(p['ktr'])} {html.escape(info)} × {p['n']}구간"
             + (" (자동)" if p.get("n_is_auto") else ""),
             "<pre>" + html.escape(" #" + _pad("가격", 12) + _pad("랏", 9) + _pad("손절손실", 10)) + "\n" + "\n".join(rows) + "</pre>",
             f"🛑 손절{'가' if p.get('stop_price') else ''} <b>{_fmt(p['sl'], d)}</b> (첫 진입에서 {p['stop_ktr']:.2f} KTR"
             + (f" · 마지막 구간과 {p['stop_ktr'] - (p['n'] - 1):.2f} KTR)" if p.get("stop_price")
                else f" · 마지막 구간 뒤 {p['stop_gap']:g} KTR)" if p.get("stop_gap") else ")"),
             f"다 체결되고 손절이면 <b>{-p['total_loss']:,.2f}</b>"
             + (f" ({-p['total_loss_pct']:.2f}%)" if p.get("total_loss_pct") is not None else "")
             + f" · 총 {p['total_lot']:g}lot · 평단 {_fmt(p['levels'][-1]['avg'], d)}",
             f"필요 증거금 {_fmt(p.get('margin_total'))} / 여유 {_fmt(p.get('margin_free'))}"
             + (f" · 손실 예산 {_fmt(p.get('budget'))}" if p["mode"] == "ktr" else "")]
    lines += [f"⚠ {html.escape(w)}" for w in p.get("warnings") or []]
    if p.get("options"):
        lines.append(_opts_text(p))
    lines.append(f"<i>지금 {'매수가' if p['side'] == 'buy' else '매도가'} {_fmt(p['entry'], d)} 기준 · 주문할 때 지금 시세로 다시 계산 · 10분 안에 눌러야 합니다</i>")
    return "\n".join(lines)


async def cmd_grid(live, client, token, chat_id: str, args: str) -> None:
    if not args.strip():
        await wiz_start(live, client, token, chat_id)          # 단계별 설정
        return
    if args.strip() in ("?", "도움말", "help"):
        await live._send(client, token, chat_id, HELP)
        return
    o = parse(args)
    b = o["body"]
    miss = []
    if not o["sym"]:
        miss.append("종목 (예: 나스닥)")
    if not b["side"]:
        miss.append("매수/매도")
    if not b.get("n") and b["mode"] != "avg" and b.get("stop_mode") != "price":   # 물타기·손절가는 빼면 자동
        miss.append("구간 수 (예: 5구간)")
    if b["mode"] == "ktr" and not b.get("risk_pct"):
        miss.append("손실 % (예: 5%)")
    if b["mode"] == "avg":
        if not b.get("lot"):
            miss.append("랏 (예: 0.1랏)")
        if b["stop_mode"] == "ktr" and not b.get("stop_ktr"):
            miss.append("손절 (예: 손절6, 손실5%, 손절가20430)")
    if miss or o["bad"]:
        msg = ("빠진 것: " + ", ".join(miss) + "\n" if miss else "") + \
              (f"모르는 말: {html.escape(' '.join(o['bad']))}\n" if o["bad"] else "")
        await live._send(client, token, chat_id, msg + "\n" + HELP)
        return
    acc = await _account(o["account"])
    if acc is None:
        await live._send(client, token, chat_id, "계좌를 찾지 못했습니다.")
        return
    b["symbol"] = o["sym"]
    try:
        if o["ktr"]:
            b["ktr"], info = o["ktr"], "(직접)"
        else:
            k = await grid.compute_ktr(acc.id, o["sym"], o["sym"], o["src"], o["tf"], o["sess"])
            b["ktr"] = k["ktr"]
            info = f"({k['session_name']} {TF_KR[o['tf']]}봉{' · OANDA' if o['src'] == 'oanda' else ''})"
        p = await grid.make_plan(acc.id, b)
    except grid.GridPlanInvalid as e:
        await live._send(client, token, chat_id, f"❌ {html.escape(str(e))}\n" + _opts_text(e.data)
                         + "\n<i>구간 수를 빼면 자동으로 정합니다.</i>")
        return
    except grid.GridError as e:
        await live._send(client, token, chat_id, f"❌ {html.escape(str(e))}")
        return
    now = time.time()
    for k in [k for k, v in PENDING.items() if now - v["at"] > PENDING_TTL]:
        PENDING.pop(k, None)
    pid = secrets.token_hex(4)
    PENDING[pid] = {"body": dict(b), "account": acc.account_number, "at": now}
    await live._send(client, token, chat_id, f"계좌 {html.escape(str(acc.account_number))}\n" + _plan_text(p, info),
                     reply_markup={"inline_keyboard": [[
                         {"text": "✅ 주문 실행", "callback_data": f"grid:go:{pid}"},
                         {"text": "취소", "callback_data": f"grid:no:{pid}"}]]})


def _table_text(t: dict) -> str:
    short = {"asia": "아시아", "euro": "유로", "us": "미장"}
    head = _pad("", 8, False) + "".join(_pad(TF_KR[tf], 8) for tf in t["tfs"])
    rows = []
    for x in t["sessions"]:
        cells = t["cells"].get(x["id"], {})
        name = short.get(x["id"], x["name"]) + ("*" if x["id"] == t["current"] else "")
        rows.append(_pad(name, 8, False) + "".join(_pad(_k(cells.get(tf, {}).get("ktr")), 8) for tf in t["tfs"]))
    from datetime import datetime
    opens = " · ".join(f"{x['name']} {datetime.fromisoformat(x['open']):%m/%d %H:%M}" for x in t["sessions"])
    errs = "".join(f"\n⚠ {html.escape(v)}" for v in list((t.get("errors") or {}).values())[:1])
    label = (symbols.group_by_key(t["symbol"]) or {}).get("label", t["symbol"])
    return (f"📏 <b>{html.escape(label)} KTR</b> ({'OANDA' if t['source'] == 'oanda' else 'MT5'}) · * 지금 장\n"
            f"<pre>{html.escape(head)}\n" + "\n".join(html.escape(r) for r in rows) + "</pre>"
            f"<i>{html.escape(opens)} 시작 · 아시아장은 직전+첫 봉</i>{errs}")


def _table_markup(sym: str, src: str) -> dict:
    row = [{"text": ("✓ " if g["key"] == sym else "") + g.get("label", g["key"]), "callback_data": f"ktr:{g['key']}:{src}"}
           for g in symbols.groups()][:5]
    other = "oanda" if src == "mt5" else "mt5"
    return {"inline_keyboard": [row, [{"text": f"시세: {'OANDA' if other == 'oanda' else 'MT5'}로 보기", "callback_data": f"ktr:{sym}:{other}"},
                                      {"text": "🔄", "callback_data": f"ktr:{sym}:{src}"}]]}


async def cmd_ktr(live, client, token, chat_id: str, args: str, message_id=None) -> None:
    o = parse(args)
    sym = o["sym"] or "nasdaq"
    if not symbols.group_by_key(sym):
        sym = symbols.groups()[0]["key"]
    acc = await _account(o["account"])
    if acc is None:
        await live._send(client, token, chat_id, "계좌를 찾지 못했습니다.")
        return
    try:
        t = await grid.ktr_table(acc.id, sym, o["src"])
        text = _table_text(t)
    except grid.GridError as e:
        text = f"❌ {html.escape(str(e))}"
    markup = _table_markup(sym, o["src"])
    if message_id:
        await live._call(client, token, "editMessageText", chat_id=chat_id, message_id=message_id, text=text,
                         parse_mode="HTML", reply_markup=markup)
    else:
        await live._send(client, token, chat_id, text, reply_markup=markup)


async def cmd_grids(live, client, token, chat_id: str) -> None:
    from sqlalchemy import select
    from db.models.account import Account
    from db.session import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        accs = (await db.execute(select(Account).where(Account.lock_enabled.is_(True)).order_by(Account.id))).scalars().all()
    sent = False
    for a in accs:
        try:
            items = await grid.grids(a.id)
        except grid.GridError:
            continue
        for x in items:
            side = "매수" if x["side"] == "buy" else "매도"
            kind = {"KTR": "KTR 거래", "AVG": "일반 물타기"}.get(x.get("comment"), x.get("comment") or "")
            text = (f"🧱 <b>{html.escape(x['symbol'])} {side}</b> · {html.escape(kind)} · 계좌 {html.escape(str(a.account_number))}\n"
                    f"체결 {x['positions']}개 {x['lots']:g}lot" + (f" · 평단 {_fmt(x['avg'])} · <b>{x['profit']:+,.2f}</b>" if x["positions"] else "")
                    + f"\n대기 {x['pending']}개 {x['pending_lots']:g}lot" + (f" · 다음 {_fmt(x['next_price'])}" if x.get("next_price") else "")
                    + (f" · 손절 {_fmt(x['sl'])}" if x.get("sl") else ""))
            btns = [{"text": "📋 상세", "callback_data": f"grid:detail:{a.id}:{x['magic']}"}]
            if x["pending"]:
                btns.append({"text": "대기 주문 취소", "callback_data": f"grid:pend:{a.id}:{x['magic']}"})
            btns.append({"text": "전부 청산", "callback_data": f"grid:close:{a.id}:{x['magic']}"})
            await live._send(client, token, chat_id, text, reply_markup={"inline_keyboard": [btns]})
            sent = True
    if not sent:
        await live._send(client, token, chat_id, "진행 중인 구간매매가 없습니다.")


async def button(live, client, token, chat_id: str, data: str, message_id) -> str:
    """ktr:<종목>:<시세> / grid:go|no:<id> / grid:detail|pend|close|closeok:<계좌id>:<magic>"""
    parts = data.split(":")
    if parts[0] == "ktr":
        await cmd_ktr(live, client, token, chat_id, f"{parts[1]} {parts[2] if len(parts) > 2 else 'mt5'}", message_id)
        return "다시 계산했습니다"
    action = parts[1] if len(parts) > 1 else ""

    async def edit(text):
        if message_id:
            await live._call(client, token, "editMessageText", chat_id=chat_id, message_id=message_id,
                             text=text, parse_mode="HTML")

    if action in ("go", "no"):
        p = PENDING.pop(parts[2] if len(parts) > 2 else "", None)
        if action == "no":
            await edit("구간매매를 취소했습니다.")
            return "취소했습니다"
        if not p or time.time() - p["at"] > PENDING_TTL:
            await edit("미리보기가 오래돼 주문하지 않았습니다. /grid 로 다시 해 주세요.")
            return "시간이 지났습니다"
        from db.session import AsyncSessionLocal
        from services.account import get_account_or_default
        async with AsyncSessionLocal() as db:
            acc = await get_account_or_default(db, int(p["account"]))
            try:
                r = await grid.execute(db, acc, p["body"], "텔레그램")
            except grid.GridError as e:
                await edit(f"❌ 주문하지 않았습니다: {html.escape(str(e))}")
                return str(e)[:190]
        res = r["result"]
        await edit("✅ <b>주문했습니다</b>\n" + "\n".join(html.escape(x) for x in grid.result_lines(acc.account_number, r["plan"], res)))
        return "주문했습니다"

    try:
        aid, magic = int(parts[2]), int(parts[3])
    except (IndexError, ValueError):
        return "잘못된 버튼입니다"
    if action in ("detail", "detailr"):     # detailr = 상세 메시지의 [새로고침] - 그 메시지를 고침
        from db.session import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            try:
                d = await grid.detail(db, aid, magic)
            except grid.GridError as e:
                return str(e)[:190]
        btns = [{"text": "🔄 새로고침", "callback_data": f"grid:detailr:{aid}:{magic}"}]
        if d["count"]["pending"]:
            btns.append({"text": "대기 주문 취소", "callback_data": f"grid:pend:{aid}:{magic}"})
        if d["count"]["open"] + d["count"]["pending"]:
            btns.append({"text": "전부 청산", "callback_data": f"grid:close:{aid}:{magic}"})
        text, markup = detail_text(d), {"inline_keyboard": [btns]}
        if action == "detailr" and message_id:
            await live._call(client, token, "editMessageText", chat_id=chat_id, message_id=message_id,
                             text=text, parse_mode="HTML", reply_markup=markup)
            return "새로 불러왔습니다"
        await live._send(client, token, chat_id, text, reply_markup=markup)
        return "상세 내역"
    if action == "close":
        await live._send(client, token, chat_id, "<b>이 구간매매를 전부 청산할까요?</b>\n대기 주문을 취소하고 체결된 포지션을 모두 시장가로 닫습니다.",
                         reply_markup={"inline_keyboard": [[{"text": "🛑 전부 청산", "callback_data": f"grid:closeok:{aid}:{magic}"},
                                                            {"text": "그만두기", "callback_data": "grid:no:-"}]]})
        return "한 번 더 확인해 주세요"
    if action in ("pend", "closeok"):
        try:
            r = await grid.close(aid, magic, action == "pend")
        except grid.GridError as e:
            return str(e)[:190]
        msg = f"취소 {r['cancelled']}개 · 청산 {r['closed']}개" + (f" · 실패 {len(r['errors'])}개" if r["errors"] else "")
        await edit(("🧹 대기 주문 취소: " if action == "pend" else "🛑 전부 청산: ") + html.escape(msg))
        live.force.set()
        return msg
    return ""


def detail_text(d: dict) -> str:
    """구간매매 묶음 상세 - 요약 몇 줄 + 구간 표 (고정폭)"""
    dg = d.get("digits")
    P = (lambda v: "-" if v is None else (f"{v:,.{dg}f}" if dg is not None else _fmt(v)))
    S = (lambda v: "-" if v is None else f"{v:+,.2f}")
    side = "매수" if d["side"] == "buy" else "매도"
    c = d["count"]
    head = [f"🧱📋 <b>{html.escape(d['symbol'])} {side}</b> · {html.escape(d['kind_name'])} #{d['magic'] % 100000}",
            f"체결 {c['open']} · 대기 {c['pending']}" + (f" · 청산 {c['closed']}" if c["closed"] else "")
            + (f" · 간격 {P(d['ktr'])}" if d.get("ktr") else ""),
            f"보유 {d['lots']:g}lot" + (f" · 평단 {P(d['avg'])} · 평가 <b>{S(d['profit_open'])}</b>" if c["open"] else "")]
    if c["closed"]:
        head.append(f"청산 손익 {S(d['profit_closed'])}")
    if d.get("sl"):
        head.append(f"손절 {P(d['sl'])} · 지금 손절되면 {S(d['at_sl_now'])}"
                    + (f" · 다 체결 후 {S(d['at_sl_all'])}" if c["pending"] else ""))
    if d.get("next_price") is not None:
        head.append(f"다음 구간 {P(d['next_price'])}" + (f" · 도달 시 {S(d['at_next'])}" if d.get("at_next") is not None else ""))
    st = {"open": "보유", "pending": "대기", "closed": "청산"}
    rows, cum = [], 0.0
    for lv in d["levels"]:
        if lv["status"] != "closed":
            cum += lv["lot"]
        mark = "▶" if lv["status"] == "pending" and lv["price"] == d.get("next_price") else " "
        rows.append(mark + _pad(str(lv["no"]), 2) + " " + _pad(st[lv["status"]], 4, False) + " " + _pad(P(lv["price"]), 10)
                    + " " + _pad(f"{lv['lot']:g}", 5) + " " + _pad("" if lv["status"] == "closed" else f"{cum:g}", 5)
                    + " " + _pad("" if lv.get("profit") is None else f"{lv['profit']:+,.0f}", 7))
    table = " " + _pad("#", 2) + " " + _pad("상태", 4, False) + " " + _pad("가격", 10) + " " + _pad("랏", 5) \
        + " " + _pad("누적", 5) + " " + _pad("손익", 7) + "\n" + "\n".join(rows)
    return "\n".join(head) + "\n<pre>" + html.escape(table) + "</pre>"


# ── 단계별 설정 (실시간 현황의 [🧱 구간매매] 버튼) ─────────────────────────
#   한 메시지를 고쳐 가며: (계좌) → 종목 → 방향 → 방식 → KTR 표에서 칸 고르기 → 손실%/랏 → 구간 수 → 배분/손절 → 미리보기 → 주문
#   버튼에 없는 값은 [직접 입력] 후 채팅으로 숫자를 보냄. 30분 지나면 처음부터.
#   버튼 데이터: gw:<id>:<항목>:<값>
WIZ: dict[str, dict] = {}
WIZ_TTL = 1800
RISK_CHOICES = [1, 5, 10, 33]                 # 손실 % 기본 버튼 (KTR 거래 손실 %, 물타기 손절 손실 %)
N_CHOICES = [2, 3, 4, 5, 6, 8, 10]
LOT_CHOICES = [0.01, 0.05, 0.1, 0.2, 0.5, 1]
FIELD_KR = {"risk": "손실 %", "lot": "구간마다 랏", "n": "구간 수", "stop_ktr": "손절 (KTR 배수)", "stop_pct": "손실 % (손절 자동)", "stop_price": "손절가 (주요 고점·저점·매물대)",
            "ktr": "KTR 값"}


def _steps(w: dict) -> list[str]:
    s = (["acc"] if w.get("multi") else []) + ["sym", "side", "mode", "ktr"]
    if w.get("mode") == "avg":
        s += ["lot", "stop", "n"]              # 손절을 먼저 정해야 구간 수 자동·비교가 됨
    else:
        s += ["risk", "n", "method"]
    return s + ["preview"]


def _done(w: dict, step: str) -> bool:
    if step == "stop":
        return ((w.get("stop_mode") == "ktr" and bool(w.get("stop_ktr"))) or (w.get("stop_mode") == "pct" and bool(w.get("stop_pct")))
                or (w.get("stop_mode") == "price" and bool(w.get("stop_price"))))
    if step == "preview":
        return False
    return w.get(step) is not None


def _next(w: dict) -> str:
    return next(s for s in _steps(w) if not _done(w, s))


def _summary(w: dict) -> str:
    bits = []
    if w.get("acc"):
        bits.append(f"계좌 {w['acc']}")
    if w.get("sym"):
        bits.append((symbols.group_by_key(w["sym"]) or {}).get("label", w["sym"]))
    if w.get("side"):
        bits.append("매수" if w["side"] == "buy" else "매도")
    if w.get("mode"):
        bits.append(grid.MODE_KR[w["mode"]])
    if w.get("ktr") is not None:
        bits.append(f"KTR {_k(w['ktr'])}" + (f" ({w['ktr_info']})" if w.get("ktr_info") else ""))
    if w.get("risk") is not None:
        bits.append(f"손실 {w['risk']:g}%")
    if w.get("lot") is not None:
        bits.append(f"{w['lot']:g}랏씩")
    if w.get("n") is not None:
        bits.append("구간 수 자동" if w["n"] == "auto" else f"{w['n']}구간")
    if w.get("method"):
        bits.append("차등손실" if w["method"] == "half" else "동일손실")
    if w.get("stop_mode") == "ktr" and w.get("stop_ktr"):
        bits.append(f"손절 {w['stop_ktr']:g} KTR")
    if w.get("stop_mode") == "price" and w.get("stop_price"):
        bits.append(f"손절가 {w['stop_price']:g}")
    if w.get("stop_mode") == "pct" and w.get("stop_pct"):
        bits.append(f"손절: 손실 {w['stop_pct']:g}%")
    return " · ".join(bits)


def _b(wid: str, text: str, field: str, value="") -> dict:
    return {"text": text, "callback_data": f"gw:{wid}:{field}:{value}"[:64]}


def _rows(btns: list[dict], per: int) -> list[list[dict]]:
    return [btns[i:i + per] for i in range(0, len(btns), per)]


async def _wiz_screen(wid: str) -> tuple[str, dict]:
    w = WIZ[wid]
    step = _next(w)
    head = "🧱 <b>구간매매 설정</b>" + (f"\n{html.escape(_summary(w))}" if _summary(w) else "")
    nav = [_b(wid, "◀ 뒤로", "back"), _b(wid, "✖ 그만", "quit")]
    kb: list[list[dict]] = []
    if step == "acc":
        q = "어느 계좌로 할까요?"
        kb = _rows([_b(wid, str(n), "acc", n) for n in w["accounts"]], 3)
    elif step == "sym":
        q = "종목을 고르세요."
        kb = _rows([_b(wid, g.get("label", g["key"]), "sym", g["key"]) for g in symbols.groups()], 4)
    elif step == "side":
        q = "방향을 고르세요."
        kb = [[_b(wid, "📈 매수 (아래로 분할 진입)", "side", "buy"), _b(wid, "📉 매도 (위로 분할 진입)", "side", "sell")]]
    elif step == "mode":
        q = ("방식을 고르세요.\n• <b>KTR 거래</b>: 손실 %를 먼저 정하면 구간마다 랏을 계산 (손절 = 마지막 구간 뒤 기본 여유, 미리보기에서 바꿈)\n"
             "• <b>일반 물타기</b>: 구간마다 같은 랏, 손절은 KTR 배수 또는 손실 %")
        kb = [[_b(wid, "KTR 거래", "mode", "ktr"), _b(wid, "일반 물타기", "mode", "avg")]]
    elif step == "ktr":
        acc = await _account(w.get("acc"))
        try:
            t = await grid.ktr_table(acc.id, w["sym"], w.get("src", "mt5"))
            w["table"] = t
            q = "KTR 을 고르세요 (장 × 시간봉). 숫자 버튼을 누르면 그 값을 씁니다.\n" + _table_text(t)
        except grid.GridError as e:
            t = None
            q = f"❌ {html.escape(str(e))}\n[직접 입력]으로 KTR 값을 넣을 수 있습니다."
        if t:
            short = {"asia": "아시아", "euro": "유로", "us": "미장"}
            kb.append([_b(wid, "·", "noop")] + [_b(wid, TF_KR[tf], "noop") for tf in t["tfs"]])
            for x in t["sessions"]:
                cells = t["cells"].get(x["id"], {})
                row = [_b(wid, short[x["id"]] + ("*" if x["id"] == t["current"] else ""), "noop")]
                for tf in t["tfs"]:
                    v = cells.get(tf, {}).get("ktr")
                    row.append(_b(wid, _k(v), "cell", f"{x['id']}.{tf}") if v is not None else _b(wid, "-", "noop"))
                kb.append(row)
        other = "oanda" if w.get("src", "mt5") == "mt5" else "mt5"
        kb.append([_b(wid, f"시세: {'OANDA' if other == 'oanda' else 'MT5'}", "src", other), _b(wid, "✏️ 직접 입력", "input", "ktr")])
    elif step == "risk":
        q = "손실 %를 고르세요 (잔고 대비, 다 체결되고 손절이면 이만큼 잃음)."
        kb = _rows([_b(wid, f"{v:g}%", "risk", v) for v in RISK_CHOICES], 4) + [[_b(wid, "✏️ 직접 입력", "input", "risk")]]
    elif step == "lot":
        q = "구간마다 넣을 랏을 고르세요."
        kb = _rows([_b(wid, f"{v:g}", "lot", v) for v in LOT_CHOICES], 6) + [[_b(wid, "✏️ 직접 입력", "input", "lot")]]
    elif step == "n" and (w.get("mode") == "avg" or w.get("stop_mode") == "price"):
        acc = await _account(w.get("acc"))
        try:
            p = await grid.make_plan(acc.id, _wiz_body(w, n_auto=True))
        except grid.GridPlanInvalid as e:
            p = e.data
            q = f"❌ {html.escape(str(e))}\n" + _opts_text(p) + "\n◀ 뒤로 가서 랏·손절을 바꾸세요."
        except grid.GridError as e:
            p, q = {}, f"❌ {html.escape(str(e))}"
        else:
            q = (f"구간 수를 고르세요. <b>자동이면 {p['n']}구간</b> ("
                 + (f"손절가 {_fmt(p['sl'], p.get('digits') or 2)} = " if p.get("stop_price") else "손절 ")
                 + f"{p['stop_ktr']:.2f} KTR, 마지막 구간과 여유 {p.get('min_gap', 1):g} KTR 이상, 증거금 안).\n"
                 + _opts_text({**p, "levels": None}))      # 아직 고르기 전이라 ▶ 대신 * (자동)만
        ok = [r["n"] for r in p.get("options") or [] if r["ok"]]
        if p.get("n_auto"):
            kb.append([_b(wid, f"✅ 자동 ({p['n_auto']}구간)", "n", "auto")])
        if ok:
            c = (p.get("n_auto") or ok[-1])
            pick = [v for v in ok if abs(v - c) <= 3][:7]
            kb += _rows([_b(wid, str(v) + ("*" if v == p.get("n_auto") else ""), "n", v) for v in pick], 7)
        kb.append([_b(wid, "✏️ 직접 입력", "input", "n")])
    elif step == "n":
        q = ("구간 수 (진입 횟수)를 고르세요. 첫 진입은 시장가, 나머지는 KTR 간격 리밋입니다.\n"
             "주요 고점·저점을 손절로 보려면 <b>📍 손절가로</b> - 그 안에 들어가는 구간 수를 자동으로 계산합니다.")
        kb = _rows([_b(wid, str(v), "n", v) for v in N_CHOICES], 7) + [[_b(wid, "✏️ 직접 입력", "input", "n"),
                                                                       _b(wid, "📍 손절가로", "input", "stop_price")]]
    elif step == "method":
        q = "랏 배분을 고르세요.\n• <b>동일손실</b>: 진입마다 손절 손실이 같게\n• <b>차등손실</b>: 남은 예산의 절반씩 (앞 진입이 크게)"
        kb = [[_b(wid, "동일손실", "method", "equal"), _b(wid, "차등손실", "method", "half")]]
    elif step == "stop":
        q = ("손절을 정하세요. 다음 단계에서 구간 수를 자동으로 정하거나 고릅니다.\n• <b>KTR 배수</b>: 첫 진입에서 몇 KTR\n"
             "• <b>손실 %</b>: 다 체결되고 손절이면 잔고의 이만큼 잃도록 손절선 자동")
        kb = [[_b(wid, f"{v} KTR", "stop", f"k{v}") for v in (3, 4, 5, 6, 8, 10)],
              [_b(wid, f"손실 {v:g}%", "stop", f"p{v}") for v in RISK_CHOICES],
              [_b(wid, "✏️ KTR 배수", "input", "stop_ktr"), _b(wid, "✏️ 손실 %", "input", "stop_pct")],
              [_b(wid, "📍 손절가 (주요 고점·저점)", "input", "stop_price")]]
    else:  # preview
        acc = await _account(w.get("acc"))
        body = _wiz_body(w)
        try:
            p = await grid.make_plan(acc.id, body)
        except grid.GridPlanInvalid as e:
            return (head + f"\n\n❌ {html.escape(str(e))}\n" + _opts_text(e.data) + "\n◀ 뒤로 가서 구간 수를 바꾸세요.",
                    {"inline_keyboard": [nav]})
        except grid.GridError as e:
            return (head + f"\n\n❌ {html.escape(str(e))}\n값을 바꿔 주세요.",
                    {"inline_keyboard": [nav]})
        w["body"], w["acc"] = body, w.get("acc") or acc.account_number
        q = _plan_text(p, w.get("ktr_info") or "")
        gaps = []
        if w["mode"] == "ktr":                 # 손절 여유 (마지막 구간 뒤 KTR) 바로 바꾸기
            cur = p.get("stop_gap")
            gaps = [[_b(wid, "최소 여유" if w.get("stop_mode") == "price" else "손절 여유", "noop")] + [_b(wid, ("✓ " if cur == v else "") + f"{v:g}", "gap", v) for v in (0.5, 1, 1.5)]]
        return (f"계좌 {html.escape(str(w['acc']))}\n" + q,
                {"inline_keyboard": gaps + [[_b(wid, "✅ 주문 실행", "go")], [_b(wid, "◀ 뒤로", "back"), _b(wid, "↻ 다시 계산", "noop2"), _b(wid, "✖ 그만", "quit")]]})
    return head + "\n\n" + q, {"inline_keyboard": kb + [nav]}


def _wiz_body(w: dict, n_auto: bool = False) -> dict:
    auto = n_auto or w.get("n") == "auto"
    return {"symbol": w["sym"], "side": w["side"], "mode": w["mode"], "ktr": w["ktr"],
            "n": None if auto else w.get("n"), "n_auto": auto and w["mode"] == "avg",
            "method": w.get("method") or "equal", "lot": w.get("lot"),
            "risk_pct": w.get("risk") if w["mode"] == "ktr" else w.get("stop_pct"),
            "stop_mode": w.get("stop_mode") or "ktr", "stop_ktr": w.get("stop_ktr"),
            "stop_gap": w.get("stop_gap") if w["mode"] == "ktr" else None,
            "stop_price": w.get("stop_price") if w.get("stop_mode") == "price" else None}


async def _wiz_show(live, client, token, chat_id, wid: str) -> None:
    w = WIZ[wid]
    text, markup = await _wiz_screen(wid)
    if w.get("mid"):
        r = await live._call(client, token, "editMessageText", chat_id=chat_id, message_id=w["mid"], text=text,
                             parse_mode="HTML", reply_markup=markup)
        if r.get("ok") or "not modified" in str(r.get("description", "")):
            return
    r = await live._send(client, token, chat_id, text, reply_markup=markup)
    if r.get("ok"):
        w["mid"] = r["result"]["message_id"]


async def wiz_start(live, client, token, chat_id: str) -> None:
    from sqlalchemy import select
    from db.models.account import Account
    from db.session import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        accs = (await db.execute(select(Account).where(Account.lock_enabled.is_(True)).order_by(Account.id))).scalars().all()
    if not accs:
        await live._send(client, token, chat_id, "MT5 가 연결된 계좌가 없습니다.")
        return
    now = time.time()
    for k in [k for k, v in WIZ.items() if now - v["at"] > WIZ_TTL]:
        WIZ.pop(k, None)
    wid = secrets.token_hex(3)
    WIZ[wid] = {"at": now, "src": "mt5", "accounts": [a.account_number for a in accs], "multi": len(accs) > 1,
                "acc": accs[0].account_number if len(accs) == 1 else None, "hist": []}
    await _wiz_show(live, client, token, chat_id, wid)


def _num(v) -> float | None:
    try:
        return float(str(v).replace(",", "").replace("%", "").replace("랏", "").strip())
    except ValueError:
        return None


async def wiz_button(live, client, token, chat_id: str, data: str, message_id) -> str:
    if data == "gw:new":
        await wiz_start(live, client, token, chat_id)
        return "구간매매 설정을 시작합니다"
    parts = data.split(":", 3)
    wid, field, value = parts[1], parts[2] if len(parts) > 2 else "", parts[3] if len(parts) > 3 else ""
    w = WIZ.get(wid)
    if not w or time.time() - w["at"] > WIZ_TTL:
        if message_id:
            await live._call(client, token, "editMessageText", chat_id=chat_id, message_id=message_id,
                             text="설정 시간이 지났습니다. 실시간 현황의 [🧱 구간매매] 를 다시 눌러 주세요.")
        return "시간이 지났습니다"
    w["at"] = time.time()
    w["mid"] = w.get("mid") or message_id
    if field in ("noop",):
        return ""
    if field == "quit":
        WIZ.pop(wid, None)
        live.pending.pop(chat_id, None)
        await live._call(client, token, "editMessageText", chat_id=chat_id, message_id=w["mid"], text="구간매매 설정을 그만뒀습니다.")
        return "그만뒀습니다"
    if field == "back":
        if w["hist"]:
            for k in w["hist"].pop():
                w.pop(k, None)
        await _wiz_show(live, client, token, chat_id, wid)
        return ""
    if field == "input":
        live.pending[chat_id] = {"type": "grid", "wid": wid, "field": value, "at": time.time()}
        await live._send(client, token, chat_id, f"<b>{FIELD_KR.get(value, value)}</b> 값을 숫자로 보내 주세요. (그만두려면 /cancel)")
        return "숫자를 보내 주세요"
    if field == "go":
        body = w.get("body")
        if not body:
            return "미리보기를 먼저 보세요"
        from db.session import AsyncSessionLocal
        from services.account import get_account_or_default
        async with AsyncSessionLocal() as db:
            acc = await get_account_or_default(db, int(w["acc"]) if w.get("acc") else None)
            try:
                r = await grid.execute(db, acc, body, "텔레그램")
            except grid.GridError as e:
                return str(e)[:190]
        WIZ.pop(wid, None)
        await live._call(client, token, "editMessageText", chat_id=chat_id, message_id=w["mid"], parse_mode="HTML",
                         text="✅ <b>주문했습니다</b>\n" + "\n".join(html.escape(x) for x in grid.result_lines(acc.account_number, r["plan"], r["result"])))
        live.force.set()
        return "주문했습니다"
    if field == "noop2":                       # 미리보기 다시 계산
        await _wiz_show(live, client, token, chat_id, wid)
        return "다시 계산했습니다"
    if field == "gap":                         # 미리보기에서 손절 여유 바꾸기 (단계 아님)
        w["stop_gap"] = float(value)
        await _wiz_show(live, client, token, chat_id, wid)
        return f"손절 여유 {float(value):g} KTR"
    if field == "src":
        w["src"] = value
        await _wiz_show(live, client, token, chat_id, wid)
        return ""
    # 값 고르기
    changed = []
    if field == "cell":
        sid, tf = value.split(".")
        c = ((w.get("table") or {}).get("cells", {}).get(sid) or {}).get(tf) or {}
        if c.get("ktr") is None:
            return "값이 없는 칸입니다"
        names = {"asia": "아시아장", "euro": "유로장", "us": "미장"}
        w["ktr"], w["ktr_info"] = c["ktr"], f"{names[sid]} {TF_KR[tf]}봉" + (" · OANDA" if w.get("src") == "oanda" else "")
        changed = ["ktr", "ktr_info"]
    elif field == "stop":
        if value.startswith("k"):
            w["stop_mode"], w["stop_ktr"] = "ktr", float(value[1:])
            changed = ["stop_mode", "stop_ktr"]
        else:
            w["stop_mode"], w["stop_pct"] = "pct", float(value[1:])
            changed = ["stop_mode", "stop_pct"]
    elif field in ("acc", "sym", "side", "mode", "method"):
        w[field] = value
        changed = [field]
        if field == "sym":
            w.pop("table", None)
    elif field in ("risk", "lot", "n"):
        w[field] = "auto" if value == "auto" else (int(float(value)) if field == "n" else float(value))
        changed = [field]
    else:
        return ""
    w["hist"].append(changed)
    await _wiz_show(live, client, token, chat_id, wid)
    return ""


async def wiz_text(live, client, token, chat_id: str, text: str) -> None:
    """[직접 입력] 뒤에 보낸 숫자"""
    p = live.pending.pop(chat_id, None) or {}
    w = WIZ.get(p.get("wid", ""))
    if not w:
        await live._send(client, token, chat_id, "설정 시간이 지났습니다. 실시간 현황의 [🧱 구간매매] 를 다시 눌러 주세요.")
        return
    v = _num(text)
    field = p.get("field")
    if v is None or v <= 0 or (field == "n" and not (1 <= v <= grid.MAX_LEVELS)):
        live.pending[chat_id] = p
        await live._send(client, token, chat_id, "0보다 큰 숫자를 보내 주세요." + (f" (구간 수는 1~{grid.MAX_LEVELS})" if field == "n" else ""))
        return
    if field == "ktr":
        w["ktr"], w["ktr_info"] = v, "직접"
        changed = ["ktr", "ktr_info"]
    elif field == "stop_ktr":
        w["stop_mode"], w["stop_ktr"] = "ktr", v
        changed = ["stop_mode", "stop_ktr"]
    elif field == "stop_pct":
        w["stop_mode"], w["stop_pct"] = "pct", v
        changed = ["stop_mode", "stop_pct"]
    elif field == "stop_price":
        w["stop_mode"], w["stop_price"] = "price", v
        changed = ["stop_mode", "stop_price"]
    else:
        w[field] = int(v) if field == "n" else v
        changed = [field]
    w["hist"].append(changed)
    w["at"] = time.time()
    w.pop("mid_stale", None)
    # 입력한 숫자 아래로 설정 메시지를 새로 보냄 (위로 스크롤하지 않아도 되게)
    old = w.pop("mid", None)
    if old:
        await live._call(client, token, "deleteMessage", chat_id=chat_id, message_id=old)
    await _wiz_show(live, client, token, chat_id, p["wid"])
