# services/telegram_tags.py
#
# 체결 알림에서 진입·청산 근거 고르기 (태그). 웹 화면 거래 상세의 태그(trade_tags · trade_tag_map)와 같은 것.
#   진입 알림  [🏷 진입 근거]  → 진입 태그 목록 (누를 때마다 켜기·끄기, 바로 저장)
#   청산 알림  [🏷 청산 근거]  → 청산 태그 (손실이면 손실 태그도)
#   [➕ 새 근거] 채팅으로 이름을 보내면 태그를 만들고 바로 선택 · [✏️ 메모] 근거 글(진입/청산 근거 칸) · [✅ 완료]
# 여러 건이 한 알림으로 묶였으면 그 거래들에 같이 저장.
#
# 버튼 데이터 (서버를 재시작해도 그대로 쓰이게 거래 번호를 버튼에 넣음 - 64바이트 안):
#   tg:<o|c>:<거래id.거래id…>:<show | t.<태그id> | new | memo | done>

import html

from sqlalchemy import delete, select

KIND = {"o": ("진입", ["entry"], "entry_reason"), "c": ("청산", ["exit", "loss"], "exit_reason")}
TYPE_KR = {"entry": "진입", "exit": "청산", "loss": "손실"}
MAX_DATA = 64


def ids_token(ids: list[int]) -> str | None:
    """버튼에 넣을 거래 번호들 - 너무 길면 앞에서부터 들어가는 만큼만 (보통 1~5건)"""
    out = []
    for i in ids:
        cand = ".".join(str(x) for x in out + [i])
        if len(f"tg:c:{cand}:t.99999") > MAX_DATA:
            break
        out.append(i)
    return ".".join(str(x) for x in out) if out else None


def button(kind: str, ids: list[int]) -> list[dict] | None:
    """체결 알림 아래에 붙일 버튼 한 줄"""
    tok = ids_token([i for i in ids if i])
    if not tok:
        return None
    return [{"text": f"🏷 {KIND[kind][0]} 근거", "callback_data": f"tg:{kind}:{tok}:show"}]


def _parse(data: str):
    parts = data.split(":")
    kind, tok, act = parts[1], parts[2], ":".join(parts[3:])
    return kind, [int(x) for x in tok.split(".") if x.isdigit()], tok, act


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
