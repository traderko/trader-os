"""텔레그램 /계좌 (/accounts) - 계좌 목록 · 기본 계좌(★) 바꾸기 · MT5 실행 켜기/끄기 (끄기는 잠긴 동안 막음)

기본 계좌 = 계좌 번호 없이 들어오는 요청(EA 일부 · 텔레그램 구간매매 등)이 쓰는 계좌, 웹을 처음 열 때 고르는 계좌.
잠금 · 포지션 · 주문에는 영향 없음. 바꿀 때 확인을 한 번 더 거침. 등록된 대화에서만 (telegram_live 가 확인)
"""
from __future__ import annotations

import html


async def _all() -> list:
    from sqlalchemy import select
    from db.models.account import Account
    from db.session import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        return list((await db.execute(select(Account).order_by(Account.is_default.desc(), Account.id))).scalars().all())


async def _set_default(account_id: int) -> str | None:
    from sqlalchemy import select, update
    from db.models.account import Account
    from db.session import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        acc = (await db.execute(select(Account).where(Account.id == account_id))).scalar_one_or_none()
        if acc is None:
            return None
        await db.execute(update(Account).where(Account.id != acc.id).values(is_default=False))   # 기본은 하나만
        await db.flush()
        acc.is_default = True
        await db.commit()
        from services.account import ensure_mt5_running
        started = await ensure_mt5_running(db, acc)          # MT5 실행이 꺼져 있으면 켜고 워커·MT5 띄움
        return str(acc.account_number) + (" · MT5 실행을 켰습니다 (1~2분 걸림)" if started else "")


async def _view() -> tuple[str, dict]:
    accs = await _all()
    lines = ["🏦 <b>계좌</b>  (★ = 기본 계좌)"]
    for a in accs:
        lines.append(f"{'★' if a.is_default else '·'} {html.escape(str(a.account_number))}"
                     + ("" if a.lock_enabled else "  (MT5 실행 꺼짐)"))
    lines.append("\n<i>기본 계좌 = 계좌 번호 없이 오는 요청 · 텔레그램 구간매매 기본값 · 웹을 처음 열 때 계좌. 잠금·포지션에는 영향 없음</i>")
    kb = []
    for a in accs:
        row = [{"text": ("⏹ MT5 끄기 · " if a.lock_enabled else "▶ MT5 켜기 · ") + str(a.account_number)[-4:],
                "callback_data": f"ac:m5:{a.id}:{0 if a.lock_enabled else 1}"}]
        if not a.is_default:
            row.insert(0, {"text": f"★ 기본으로 · {a.account_number}", "callback_data": f"ac:ask:{a.id}"})
        kb.append(row)
    return "\n".join(lines), {"inline_keyboard": kb}


async def cmd(live, client, token, cid: str, arg: str = "") -> None:
    text, kb = await _view()
    await live._send(client, token, cid, text, reply_markup=kb)


async def _edit(live, client, token, cid, message_id, text, kb=None) -> None:
    r = await live._call(client, token, "editMessageText", chat_id=cid, message_id=message_id, text=text,
                         parse_mode="HTML", **({"reply_markup": kb} if kb else {})) if message_id else {}
    if not r.get("ok"):
        await live._send(client, token, cid, text, **({"reply_markup": kb} if kb else {}))


async def button(live, client, token, cid: str, data: str, message_id) -> str:
    p = data.split(":")
    act = p[1] if len(p) > 1 else ""
    if act == "ask" and len(p) > 2:
        accs = {a.id: a for a in await _all()}
        a = accs.get(int(p[2]))
        if not a:
            return "계좌를 찾을 수 없습니다"
        await _edit(live, client, token, cid, message_id,
                    f"★ <b>{html.escape(str(a.account_number))}</b> 을(를) 기본 계좌로 바꿀까요?"
                    + ("" if a.lock_enabled else "\nMT5 실행이 꺼져 있어서 <b>같이 켭니다</b> (이 계좌에도 거래 잠금이 적용됨)"),
                    {"inline_keyboard": [[{"text": "★ 바꾸기", "callback_data": f"ac:go:{a.id}"},
                                          {"text": "그만두기", "callback_data": "ac:list"}]]})
        return ""
    if act == "m5" and len(p) > 3:             # MT5 실행 켜기 · 끄기 → 확인
        a = {x.id: x for x in await _all()}.get(int(p[2]))
        if not a:
            return "계좌를 찾을 수 없습니다"
        on = p[3] == "1"
        msg = (f"▶ <b>{html.escape(str(a.account_number))}</b> MT5 실행을 켤까요?\n워커와 MT5 가 뜨고(1~2분) 이 계좌에도 거래 잠금이 적용됩니다."
               if on else
               f"⏹ <b>{html.escape(str(a.account_number))}</b> MT5 실행을 끌까요?\n이 계좌의 MT5·워커가 꺼지고 <b>잠금 · 자동 청산 · 실시간 현황이 멈춥니다</b>. 잠겨 있는 동안에는 끌 수 없습니다.")
        await _edit(live, client, token, cid, message_id, msg,
                    {"inline_keyboard": [[{"text": "▶ 켜기" if on else "⏹ 끄기", "callback_data": f"ac:m5go:{a.id}:{p[3]}"},
                                          {"text": "그만두기", "callback_data": "ac:list"}]]})
        return ""
    if act == "m5go" and len(p) > 3:
        from db.models.account import Account
        from db.session import AsyncSessionLocal
        from services.account import Mt5ToggleError, set_mt5_running
        on = p[3] == "1"
        async with AsyncSessionLocal() as db:
            a = await db.get(Account, int(p[2]))
            if not a:
                return "계좌를 찾을 수 없습니다"
            try:
                changed = await set_mt5_running(db, a, on)
                head = (f"{'▶' if on else '⏹'} {a.account_number} MT5 실행을 {'켰습니다 (1~2분 걸림)' if on else '껐습니다'}"
                        if changed else f"{a.account_number} 은(는) 이미 {'켜져' if on else '꺼져'} 있습니다")
            except Mt5ToggleError as e:
                head = f"⚠️ {e}"
        text, kb = await _view()
        await _edit(live, client, token, cid, message_id, html.escape(head) + "\n\n" + text, kb)
        return "켰습니다" if on else "처리했습니다"
    if act == "go" and len(p) > 2:
        num = await _set_default(int(p[2]))
        text, kb = await _view()
        await _edit(live, client, token, cid, message_id, (f"✅ 기본 계좌: {num}\n\n" if num else "") + text, kb)
        return "바꿨습니다" if num else "계좌를 찾을 수 없습니다"
    text, kb = await _view()
    await _edit(live, client, token, cid, message_id, text, kb)
    return ""
