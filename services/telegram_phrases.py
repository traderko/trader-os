"""텔레그램 /문구 (/phrases) - 잠금 풀기 확인 문구 보기·추가·수정·삭제

  /문구                → 종류 고르기 (정기 재확인 · 연속 손절 · 집중 구간 공통 · 구간별 전용 · 지표 발표일 전용)
  [종류]               → 그 종류 문구 목록 + [✏️ n] [🗑 n] [➕ 추가]
  ✏️ / ➕              → 새 문구를 답장으로 받음 (/cancel 로 그만두기)

버튼 콜백: ph:l (종류 목록) · ph:t:<종류> · ph:a:<종류> · ph:e:<id> · ph:d:<id> · ph:dy:<id> (삭제 확인)
지키는 것
  - 거래가 잠겨 있는 동안에는 바꿀 수 없음 (보기는 됨) - 잠긴 김에 문구를 쉽게 바꿔 푸는 것을 막음
  - 문구는 8~200자, 한 줄
  - 정기 재확인 · 집중 구간 공통의 마지막 문구는 지울 수 없음 (그 잠금을 풀 문구가 없어짐)
"""
from __future__ import annotations

import html
import time

from sqlalchemy import func, select

from db.models.account import Account
from db.models.trade_lock_phrase import TradeLockPhrase
from db.session import AsyncSessionLocal

MIN_LEN, MAX_LEN = 8, 200
KEEP_ONE = ("checkin", "session")        # 마지막 하나는 지울 수 없는 종류


def _types() -> list[tuple[str, str]]:
    from services.settings_service import lock_cfg
    out = [("checkin", "정기 재확인"), ("consec_loss", "연속 손절"), ("session", "집중 구간 공통")]
    for s in lock_cfg().get("sessions") or []:
        out.append((f"session:{s['id']}", f"{s.get('name') or s['id']} 전용"))
    out.append(("session:event", "지표 발표일 전용"))
    return out


def _label(ptype: str) -> str:
    return dict(_types()).get(ptype, ptype)


async def _locked_now() -> str:
    """잠겨 있는 계좌가 있으면 그 설명, 없으면 ''"""
    from services import trade_lock_service as lock_svc
    async with AsyncSessionLocal() as db:
        accs = (await db.execute(select(Account).where(Account.lock_enabled == True))).scalars().all()
        for a in accs:
            try:
                st = await lock_svc.get_lock_state(db, a.id)
            except Exception:
                continue
            if st.get("locked"):
                return a.account_number
    return ""


async def _active(db, ptype: str) -> list[TradeLockPhrase]:
    return (await db.execute(select(TradeLockPhrase).where(
        TradeLockPhrase.is_active == True, TradeLockPhrase.phrase_type == ptype).order_by(TradeLockPhrase.id))).scalars().all()


async def _menu() -> tuple[str, dict]:
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(TradeLockPhrase.phrase_type, func.count()).where(
            TradeLockPhrase.is_active == True).group_by(TradeLockPhrase.phrase_type))).all()
    cnt = dict(rows)
    kb = [[{"text": f"{name} · {cnt.get(t, 0)}개", "callback_data": f"ph:t:{t}"[:64]}] for t, name in _types()]
    return "📝 <b>잠금 풀기 확인 문구</b>\n볼 종류를 고르세요.", {"inline_keyboard": kb}


async def _type_view(ptype: str) -> tuple[str, dict]:
    async with AsyncSessionLocal() as db:
        rows = await _active(db, ptype)
    lines = [f"📝 <b>{html.escape(_label(ptype))}</b> 문구 {len(rows)}개"]
    if ptype == "consec_loss" and not rows:
        lines.append("<i>없으면 정기 재확인 문구로 풉니다.</i>")
    if ptype.startswith("session:") and not rows:
        lines.append("<i>없으면 집중 구간 공통 문구로 풉니다.</i>")
    kb = []
    for i, r in enumerate(rows, 1):
        lines.append(f"\n<b>{i}.</b> {html.escape(r.phrase)}")
        kb.append([{"text": f"✏️ {i} 수정", "callback_data": f"ph:e:{r.id}"},
                   {"text": f"🗑 {i} 삭제", "callback_data": f"ph:d:{r.id}"}])
    kb.append([{"text": "➕ 추가", "callback_data": f"ph:a:{ptype}"[:64]}, {"text": "⬅ 종류", "callback_data": "ph:l"}])
    return "\n".join(lines)[:3900], {"inline_keyboard": kb}


async def _edit_or_send(live, client, token, cid, message_id, text, markup) -> None:
    if message_id:
        r = await live._call(client, token, "editMessageText", chat_id=cid, message_id=message_id,
                             text=text, parse_mode="HTML", reply_markup=markup)
        if r.get("ok") or "not modified" in str(r.get("description", "")):
            return
    await live._send(client, token, cid, text, reply_markup=markup)


async def cmd_phrases(live, client, token, cid: str) -> None:
    text, kb = await _menu()
    await live._send(client, token, cid, text, reply_markup=kb)


async def button(live, client, token, cid: str, data: str, message_id) -> str:
    parts = data.split(":", 2)
    op = parts[1] if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else ""
    if op == "l":
        text, kb = await _menu()
        await _edit_or_send(live, client, token, cid, message_id, text, kb)
        return ""
    if op == "t":
        text, kb = await _type_view(arg)
        await _edit_or_send(live, client, token, cid, message_id, text, kb)
        return ""

    # 여기부터는 바꾸는 일 - 잠금 중이면 안 됨
    locked = await _locked_now()
    if locked:
        await live._send(client, token, cid, f"🔒 지금 거래가 잠겨 있어서({html.escape(locked)}) 문구를 바꿀 수 없습니다. 잠금이 풀린 뒤에 바꾸세요.")
        return "잠금 중에는 못 바꿈"

    if op == "a":
        r = await live._send(client, token, cid,
                             f"➕ <b>{html.escape(_label(arg))}</b> 문구로 넣을 내용을 답장으로 보내세요.\n"
                             f"({MIN_LEN}~{MAX_LEN}자 한 줄 · 쉼표나 특수기호는 빼는 게 입력하기 편합니다 · 그만두려면 /cancel)",
                             reply_markup={"force_reply": True, "input_field_placeholder": "새 문구"})
        if r.get("ok"):
            live.pending[cid] = {"type": "phr", "op": "add", "ptype": arg, "msg": message_id, "at": time.time()}
        return "문구를 보내세요"

    try:
        pid = int(arg)
    except ValueError:
        return "잘못된 버튼"
    async with AsyncSessionLocal() as db:
        row = await db.get(TradeLockPhrase, pid)
        if row is None or not row.is_active:
            return "이미 없는 문구입니다"
        if op == "e":
            r = await live._send(client, token, cid,
                                 f"✏️ 지금 문구:\n<blockquote>{html.escape(row.phrase)}</blockquote>\n바꿀 문구를 답장으로 보내세요. ({MIN_LEN}~{MAX_LEN}자 · 그만두려면 /cancel)",
                                 reply_markup={"force_reply": True, "input_field_placeholder": "바꿀 문구"})
            if r.get("ok"):
                live.pending[cid] = {"type": "phr", "op": "edit", "id": pid, "ptype": row.phrase_type, "msg": message_id, "at": time.time()}
            return "문구를 보내세요"
        if op in ("d", "dy"):
            if row.phrase_type in KEEP_ONE and len(await _active(db, row.phrase_type)) <= 1:
                return f"{_label(row.phrase_type)}의 마지막 문구라 지울 수 없습니다"
            if op == "d":
                await live._send(client, token, cid, f"🗑 이 문구를 지울까요?\n<blockquote>{html.escape(row.phrase)}</blockquote>",
                                 reply_markup={"inline_keyboard": [[{"text": "🗑 지우기", "callback_data": f"ph:dy:{pid}"},
                                                                    {"text": "그대로 두기", "callback_data": f"ph:t:{row.phrase_type}"[:64]}]]})
                return ""
            ptype = row.phrase_type
            row.is_active = False
            await db.commit()
    text, kb = await _type_view(ptype)
    await _edit_or_send(live, client, token, cid, message_id, "🗑 지웠습니다.\n\n" + text, kb)
    return "지웠습니다"


async def text_input(live, client, token, cid: str, raw: str) -> None:
    p = live.pending.get(cid) or {}
    phrase = " ".join(raw.split())
    if "\n" in raw.strip() or not (MIN_LEN <= len(phrase) <= MAX_LEN):
        await live._send(client, token, cid, f"문구는 {MIN_LEN}~{MAX_LEN}자 한 줄로 보내 주세요. (지금 {len(phrase)}자 · 그만두려면 /cancel)")
        return
    if await _locked_now():
        live.pending.pop(cid, None)
        await live._send(client, token, cid, "🔒 그 사이 거래가 잠겨서 문구를 바꾸지 않았습니다.")
        return
    async with AsyncSessionLocal() as db:
        same = (await db.execute(select(TradeLockPhrase).where(TradeLockPhrase.phrase == phrase))).scalar_one_or_none()
        if p.get("op") == "edit":
            row = await db.get(TradeLockPhrase, p.get("id"))
            if row is None or not row.is_active:
                live.pending.pop(cid, None)
                await live._send(client, token, cid, "그 문구는 이미 없어졌습니다.")
                return
            if same is not None and same.id != row.id:
                if same.is_active:
                    await live._send(client, token, cid, "똑같은 문구가 이미 있습니다. 다른 문구로 보내 주세요.")
                    return
                await db.delete(same)                 # 예전에 지운 같은 문구 (이름이 겹치면 저장이 안 되므로 정리)
                await db.flush()
            row.phrase = phrase
            ptype, msg = row.phrase_type, "✏️ 바꿨습니다."
        else:
            ptype = p.get("ptype") or "checkin"
            if same is not None:
                if same.is_active:
                    await live._send(client, token, cid, "똑같은 문구가 이미 있습니다. 다른 문구로 보내 주세요.")
                    return
                same.is_active, same.phrase_type = True, ptype   # 예전에 지운 문구를 다시 살림
            else:
                db.add(TradeLockPhrase(phrase=phrase, phrase_type=ptype, is_active=True))
            msg = "➕ 추가했습니다."
        await db.commit()
    live.pending.pop(cid, None)
    text, kb = await _type_view(ptype)
    await live._send(client, token, cid, msg + "\n\n" + text, reply_markup=kb)
