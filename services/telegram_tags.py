# services/telegram_tags.py
#
# 체결 알림에서 진입·청산 근거 고르기 (태그). 웹 화면 거래 상세의 태그(trade_tags · trade_tag_map)와 같은 것.
#   진입 알림  [🏷 진입 근거]  → 진입 태그 목록 (누를 때마다 켜기·끄기, 바로 저장)
#   청산 알림  [🏷 청산 근거]  → 청산 태그 (손실이면 손실 태그도)
#   [➕ 새 근거] 채팅으로 이름을 보내면 태그를 만들고 바로 선택 · [✏️ 메모] 근거 글(진입/청산 근거 칸) · [✅ 완료]
# 여러 건이 한 알림으로 묶였으면 그 거래들에 같이 저장.
#
# 버튼 데이터 (서버를 재시작해도 그대로 쓰이게 거래 번호를 버튼에 넣음 - 64바이트 안):
#   tg:<o|c>:<거래 번호들>:<show | t.<태그id> | new | memo | done>   번호들 = 12.15-18.20 (연속이면 범위로)
#
# /근거 (/reasons) [12시간|3일]  근거 없는 최근 거래를 묶음별로 + 버튼. 같은 목록을 정해진 시각·주기로 보내는 알림은
#   remind_loop() (관리 화면 텔레그램 탭 '근거 알림', 기본 꺼짐)

import html

from sqlalchemy import delete, select

KIND = {"o": ("진입", ["entry"], "entry_reason"), "c": ("청산", ["exit", "loss"], "exit_reason")}
TYPE_KR = {"entry": "진입", "exit": "청산", "loss": "손실"}
MAX_DATA = 64


def _encode(ids: list[int]) -> str:
    ids = sorted(set(ids))
    segs, i = [], 0
    while i < len(ids):
        j = i
        while j + 1 < len(ids) and ids[j + 1] == ids[j] + 1:
            j += 1
        segs.append(str(ids[i]) if i == j else f"{ids[i]}-{ids[j]}")
        i = j + 1
    return ".".join(segs)


def _decode(tok: str) -> list[int]:
    out = []
    for seg in tok.split("."):
        if "-" in seg:
            a, b = seg.split("-", 1)
            if a.isdigit() and b.isdigit() and int(b) - int(a) < 500:
                out.extend(range(int(a), int(b) + 1))
        elif seg.isdigit():
            out.append(int(seg))
    return out


def ids_token(ids: list[int]) -> str | None:
    """버튼에 넣을 거래 번호들 (연속 번호는 범위로) - 그래도 너무 길면 앞에서부터 들어가는 만큼만"""
    ids = sorted(set(i for i in ids if i))
    out = []
    for i in ids:
        if len(f"tg:c:{_encode(out + [i])}:t.99999") > MAX_DATA:
            break
        out.append(i)
    return _encode(out) if out else None


def button(kind: str, ids: list[int]) -> list[dict] | None:
    """체결 알림 아래에 붙일 버튼 한 줄"""
    tok = ids_token([i for i in ids if i])
    if not tok:
        return None
    return [{"text": f"🏷 {KIND[kind][0]} 근거", "callback_data": f"tg:{kind}:{tok}:show"}]


def _parse(data: str):
    parts = data.split(":")
    kind, tok, act = parts[1], parts[2], ":".join(parts[3:])
    return kind, _decode(tok), tok, act


async def _screen(db, kind: str, ids: list[int], tok: str) -> tuple[str, dict]:
    from db.models.trade import Trade
    from db.models.trade_note import TradeNote
    from db.models.trade_tag import TradeTag
    from db.models.trade_tag_map import TradeTagMap
    label, types, field = KIND[kind]
    trades = [t for t in [await db.get(Trade, i) for i in ids] if t is not None]
    if not trades:
        return "거래 기록을 찾지 못했습니다.", {"inline_keyboard": []}
    t0 = trades[0]
    net = sum((t.profit or 0) + (t.commission or 0) for t in trades if t.close_time)
    loss = kind == "c" and net < 0
    show_types = [x for x in types if x != "loss" or loss]
    from db.models.trade_tag import TagType
    tags = (await db.execute(select(TradeTag).where(TradeTag.type.in_([TagType(x) for x in show_types])).order_by(TradeTag.type, TradeTag.id))).scalars().all()
    chosen = set((await db.execute(select(TradeTagMap.tag_id).where(TradeTagMap.trade_id == t0.id))).scalars().all())
    note = (await db.execute(select(TradeNote).where(TradeNote.trade_id == t0.id))).scalar_one_or_none()
    memo = getattr(note, field, None) if note else None

    vol = sum(t.volume or 0 for t in trades)
    head = (f"🏷 <b>{label} 근거</b> · {html.escape(t0.symbol or '')} {t0.position} {vol:g}lot"
            + (f" ({len(trades)}건)" if len(trades) > 1 else "")
            + (f" · {net:+,.2f}" if kind == "c" else ""))
    picked = [g for g in tags if g.id in chosen]
    lines = [head, "선택: " + (", ".join(html.escape(g.name) for g in picked) if picked else "없음")]
    if memo:
        lines.append(f"메모: {html.escape(memo)}")
    if not tags:
        lines.append(f"\n아직 {label} 근거가 없습니다. [➕ 새 근거] 로 만들면 다음부터 버튼으로 나옵니다.")
    else:
        lines.append("\n누를 때마다 켜고 끕니다 (바로 저장).")

    kb, row = [], []
    for g in tags:
        mark = "✅ " if g.id in chosen else ""
        pre = "🔻" if g.type.value == "loss" else ""
        row.append({"text": f"{mark}{pre}{g.name}"[:40], "callback_data": f"tg:{kind}:{tok}:t.{g.id}"})
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    kb.append([{"text": "➕ 새 근거", "callback_data": f"tg:{kind}:{tok}:new"},
               {"text": "✏️ 메모", "callback_data": f"tg:{kind}:{tok}:memo"},
               {"text": "✅ 완료", "callback_data": f"tg:{kind}:{tok}:done"}])
    if loss:
        lines.append("🔻 = 손실 근거")
    return "\n".join(lines), {"inline_keyboard": kb}


async def _show(live, client, token, chat_id, kind, ids, tok, message_id=None, new=False):
    from db.session import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        text, markup = await _screen(db, kind, ids, tok)
    if message_id and not new:
        r = await live._call(client, token, "editMessageText", chat_id=chat_id, message_id=message_id, text=text,
                             parse_mode="HTML", reply_markup=markup)
        if r.get("ok") or "not modified" in str(r.get("description", "")):
            return message_id
    r = await live._send(client, token, chat_id, text, reply_markup=markup)
    return r.get("result", {}).get("message_id") if r.get("ok") else None


async def button_pressed(live, client, token, chat_id: str, data: str, message_id) -> str:
    from db.models.trade_tag import TradeTag
    from db.models.trade_tag_map import TradeTagMap
    from db.session import AsyncSessionLocal
    kind, ids, tok, act = _parse(data)
    if kind not in KIND or not ids:
        return "잘못된 버튼입니다"
    label, types, field = KIND[kind]
    if act == "show":                          # 체결 알림의 버튼 → 고르는 메시지를 새로 보냄
        await _show(live, client, token, chat_id, kind, ids, tok, new=True)
        return ""
    if act.startswith("t."):
        tag_id = int(act[2:])
        async with AsyncSessionLocal() as db:
            tag = await db.get(TradeTag, tag_id)
            if tag is None:
                return "지워진 근거입니다"
            have = (await db.execute(select(TradeTagMap.trade_id).where(
                TradeTagMap.tag_id == tag_id, TradeTagMap.trade_id.in_(ids)))).scalars().all()
            if ids[0] in have:                 # 켜져 있으면 끄기 (묶음 전체)
                await db.execute(delete(TradeTagMap).where(TradeTagMap.tag_id == tag_id, TradeTagMap.trade_id.in_(ids)))
                msg = f"{tag.name} 뺌"
            else:
                for i in ids:
                    if i not in have:
                        db.add(TradeTagMap(trade_id=i, tag_id=tag_id))
                msg = f"{tag.name} 저장"
            await db.commit()
        await _show(live, client, token, chat_id, kind, ids, tok, message_id)
        return msg
    if act in ("new", "memo"):
        import time
        live.pending[chat_id] = {"type": "tag", "act": act, "kind": kind, "ids": ids, "tok": tok,
                                 "mid": message_id, "at": time.time()}
        await live._send(client, token, chat_id,
                         f"새 {label} 근거 이름을 보내 주세요. (예: 시가박스 상단 돌파 · 그만두려면 /cancel)" if act == "new"
                         else f"{label} 근거 메모를 보내 주세요. 웹 거래 상세의 '{label} 근거' 칸에 들어갑니다. (그만두려면 /cancel)")
        return "채팅으로 보내 주세요"
    if act == "done":
        from db.session import AsyncSessionLocal as S
        async with S() as db:
            text, _ = await _screen(db, kind, ids, tok)
        text = text.split("\n\n")[0].replace("🏷", "✅", 1)
        await live._call(client, token, "editMessageText", chat_id=chat_id, message_id=message_id, text=text, parse_mode="HTML")
        return "저장했습니다"
    return ""


async def text_input(live, client, token, chat_id: str, text: str) -> None:
    """[➕ 새 근거] / [✏️ 메모] 뒤에 보낸 글"""
    from db.models.trade_note import TradeNote
    from db.models.trade_tag import TradeTag
    from db.models.trade_tag_map import TradeTagMap
    from db.session import AsyncSessionLocal
    p = live.pending.pop(chat_id, None) or {}
    kind, ids, tok = p.get("kind"), p.get("ids") or [], p.get("tok")
    if kind not in KIND or not ids:
        return
    label, types, field = KIND[kind]
    text = text.strip()
    async with AsyncSessionLocal() as db:
        if p.get("act") == "new":
            name = text[:40]
            tag = (await db.execute(select(TradeTag).where(TradeTag.name == name))).scalar_one_or_none()
            if tag is not None and tag.type.value not in types:
                live.pending[chat_id] = p
                await live._send(client, token, chat_id,
                                 f"'{html.escape(name)}' 은(는) 이미 {TYPE_KR[tag.type.value]} 근거에 있습니다. 다른 이름을 보내 주세요. (그만두려면 /cancel)")
                return
            if tag is None:
                from db.models.trade_tag import TagType
                tag = TradeTag(name=name, type=TagType(types[0]))
                db.add(tag)
                await db.flush()
            have = set((await db.execute(select(TradeTagMap.trade_id).where(
                TradeTagMap.tag_id == tag.id, TradeTagMap.trade_id.in_(ids)))).scalars().all())
            for i in ids:
                if i not in have:
                    db.add(TradeTagMap(trade_id=i, tag_id=tag.id))
        else:
            for i in ids:
                note = (await db.execute(select(TradeNote).where(TradeNote.trade_id == i))).scalar_one_or_none()
                if note is None:
                    note = TradeNote(trade_id=i)
                    db.add(note)
                setattr(note, field, text)
        await db.commit()
    # 고르는 메시지를 입력한 글 아래로 새로 보냄 (위로 스크롤하지 않아도 되게)
    old = p.get("mid")
    if old:
        await live._call(client, token, "deleteMessage", chat_id=chat_id, message_id=old)
    await _show(live, client, token, chat_id, kind, ids, tok, new=True)


# ── 근거 없는 거래 모아 보기 (/근거 · 근거 알림) ──────────────────────────
def _hours_arg(args: str, default: float) -> float:
    import re
    m = re.search(r"(\d+(?:\.\d+)?)\s*(일|d|시간|h)?", args or "")
    if not m:
        return default
    v = float(m.group(1))
    return v * 24 if (m.group(2) or "일") in ("일", "d") else v


async def untagged_groups(hours: float, gap_min: int = 30) -> list[dict]:
    """최근 hours 시간 안에 진입한 거래 중 근거가 빠진 것 - 같은 계좌·종목·방향으로 gap_min 분 안에 이어 들어간 건 한 묶음"""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from sqlalchemy.orm import selectinload
    from db.models.trade import Trade
    from db.models.trade_tag_map import TradeTagMap
    from db.session import AsyncSessionLocal
    since = (datetime.now(ZoneInfo("Asia/Seoul")) - timedelta(hours=hours))
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(Trade).where(Trade.open_time >= since).order_by(Trade.open_time)
                                 .options(selectinload(Trade.tags).selectinload(TradeTagMap.tag)))).scalars().all()
    groups: list[dict] = []
    last: dict = {}
    for t in rows:
        types = {getattr(m.tag.type, "value", m.tag.type) for m in t.tags if m.tag is not None}
        miss_o = "entry" not in types
        miss_c = bool(t.close_time) and not ({"exit", "loss"} & types)
        if not (miss_o or miss_c):
            continue
        key = (t.account_id, t.symbol, t.position)
        g = last.get(key)
        if g is None or (t.open_time - g["last"]).total_seconds() > gap_min * 60:
            g = {"symbol": t.symbol, "side": t.position, "start": t.open_time, "last": t.open_time,
                 "ids": [], "open": [], "close": [], "vol": 0.0, "net": 0.0, "closed": 0}
            groups.append(g)
            last[key] = g
        g["last"] = t.open_time
        g["ids"].append(t.id)
        g["vol"] += t.volume or 0
        if t.close_time:
            g["closed"] += 1
            g["net"] += (t.profit or 0) + (t.commission or 0)
        if miss_o:
            g["open"].append(t.id)
        if miss_c:
            g["close"].append(t.id)
    return groups


def _period(hours: float) -> str:
    return f"최근 {hours / 24:g}일" if hours >= 24 and hours % 24 == 0 else f"최근 {hours:g}시간"


async def untagged_message(hours: float, limit: int = 10) -> tuple[str, dict] | None:
    """근거 없는 거래 목록 메시지 (없으면 None)"""
    from zoneinfo import ZoneInfo
    groups = await untagged_groups(hours)
    if not groups:
        return None
    n = sum(len(g["ids"]) for g in groups)
    lines = [f"🏷 <b>근거 없는 거래</b> · {_period(hours)} · {n}건 ({len(groups)}묶음)"]
    kb = []
    for i, g in enumerate(groups[-limit:][::-1], 1):          # 최근 것부터
        st = g["start"] if g["start"].tzinfo else g["start"].replace(tzinfo=ZoneInfo("Asia/Seoul"))
        st = st.astimezone(ZoneInfo("Asia/Seoul"))
        miss = " · ".join(x for x in (f"진입 {len(g['open'])}" if g["open"] else "", f"청산 {len(g['close'])}" if g["close"] else "") if x)
        lines.append(f"{i}. {st:%m/%d %H:%M} {html.escape(g['symbol'] or '')} {g['side']} {g['vol']:g}lot"
                     + (f" ({len(g['ids'])}건)" if len(g["ids"]) > 1 else "")
                     + (f" · {g['net']:+,.2f}" if g["closed"] else " · 보유 중")
                     + f"\n    빠진 근거: {miss}")
        row = []
        if g["open"]:
            tok = ids_token(g["open"])
            row.append({"text": f"{i}. 🏷 진입", "callback_data": f"tg:o:{tok}:show"})
        if g["close"]:
            tok = ids_token(g["close"])
            row.append({"text": f"{i}. 🏷 청산", "callback_data": f"tg:c:{tok}:show"})
        kb.append(row)
    if len(groups) > limit:
        lines.append(f"\n외 {len(groups) - limit}묶음 - 웹 거래내역 [🏷 근거 없음] 에서 한꺼번에 달 수 있습니다.")
    return "\n".join(lines), {"inline_keyboard": kb}


async def cmd_reasons(live, client, token, chat_id: str, args: str) -> None:
    """/근거 [12시간|3일] - 기본은 근거 알림 설정의 기간 (없으면 24시간)"""
    from services.settings_service import telegram_cfg
    hours = _hours_arg(args, float((telegram_cfg().get("reason_remind") or {}).get("lookback_hours") or 24))
    r = await untagged_message(hours)
    if r is None:
        await live._send(client, token, chat_id, f"✅ {_period(hours)} 근거 없는 거래가 없습니다.")
        return
    await live._send(client, token, chat_id, r[0], reply_markup=r[1])


# ── 근거 알림 (관리 화면 텔레그램 탭 '근거 알림') ─────────────────────────
def remind_slots(rr: dict) -> list[int]:
    """보낼 시각들 (하루 중 분, 한국 시간)"""
    def mins(hhmm: str) -> int:
        h, m = (hhmm or "00:00").split(":")
        return int(h) * 60 + int(m)
    if rr.get("mode") == "interval":
        start, end = mins(rr.get("start", "08:00")), mins(rr.get("end", "23:00"))
        span = (end - start) % 1440 or 1440               # 끝이 시작보다 이르면 자정을 넘김
        step = max(30, int(float(rr.get("every_hours", 2)) * 60))
        return sorted({(start + k) % 1440 for k in range(0, span + 1, step)})
    return sorted({mins(t) for t in rr.get("times") or []})


async def remind_loop() -> None:
    """정해진 시각(분)이 되면 근거 없는 거래 목록을 보냄. 하나도 없으면 안 보냄. 같은 분에는 한 번만."""
    import asyncio
    import re
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from services.settings_service import telegram_cfg
    from services.telegram_service import TelegramService
    sent: set[str] = set()
    while True:
        try:
            cfg = telegram_cfg()
            rr = cfg.get("reason_remind") or {}
            now = datetime.now(ZoneInfo("Asia/Seoul"))
            key = now.strftime("%Y-%m-%d %H:%M")
            if rr.get("enabled") and key not in sent and now.hour * 60 + now.minute in remind_slots(rr):
                sent.add(key)
                if len(sent) > 200:
                    sent = {key}
                r = await untagged_message(float(rr.get("lookback_hours") or 24))
                if r is not None:
                    text = html.unescape(re.sub(r"<[^>]+>", "", r[0]))      # 알림 전송은 글자 그대로 (굵게 없음)
                    TelegramService().send("", text + "\n\n(근거 알림 · 관리 화면 텔레그램 탭에서 끄기·시각 변경)",
                                           r[1]["inline_keyboard"])
        except Exception as e:
            print(f"[근거 알림] {e}")
        await asyncio.sleep(20)
