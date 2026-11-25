"""텔레그램 /지침 (/guide) - 지침 목록 · 중요도(★1~5) 바꾸기

문구 버튼을 누를 때마다 ★ 가 하나씩 올라감 (★5 다음은 ★1). 바로 저장 (관리 화면 지침 탭과 같은 설정)
★ 가 많을수록 '무작위 지침' · '그날 첫 진입'에서 자주 나옴 (services/guidelines.py item_weight)
한 화면에 10개씩, ◀ ▶ 로 넘김. 문구 자체를 고치거나 더하는 건 관리 화면에서
"""
from __future__ import annotations

import html

PER = 10


def _items() -> tuple[list[str], dict]:
    from services.settings_service import get_settings
    g = get_settings().get("guidelines") or {}
    return [x for x in (g.get("items") or []) if str(x).strip()], dict(g.get("item_weights") or {})


def _stars(w: int) -> str:
    return "★" * w + "☆" * (5 - w)


def _view(page: int = 0) -> tuple[str, dict]:
    items, ws = _items()
    if not items:
        return "📌 <b>지침</b>\n지침 목록이 비어 있습니다. 관리 화면 '지침' 탭에서 넣어 주세요.", {"inline_keyboard": []}
    total = sum(max(1, min(5, int(ws.get(t, 1)))) for t in items)
    pages = (len(items) - 1) // PER + 1
    page = max(0, min(page, pages - 1))
    lines = [f"📌 <b>지침 중요도</b> · {len(items)}개" + (f" ({page + 1}/{pages})" if pages > 1 else ""),
             "문구를 누를 때마다 ★ 가 하나씩 올라갑니다 (★5 다음은 ★1). ★ 가 많을수록 자주 나옵니다.", ""]
    kb = []
    for i in range(page * PER, min(len(items), (page + 1) * PER)):
        t = items[i]
        w = max(1, min(5, int(ws.get(t, 1))))
        pct = round(w / total * 100)
        lines.append(f"{_stars(w)} {pct}% · {html.escape(t)}")
        short = t if len(t) <= 22 else t[:21] + "…"
        kb.append([{"text": f"{_stars(w)} {short}", "callback_data": f"gw:t:{i}:{page}"}])
    nav = []
    if page > 0:
        nav.append({"text": "◀", "callback_data": f"gw:p:{page - 1}"})
    if page < pages - 1:
        nav.append({"text": "▶", "callback_data": f"gw:p:{page + 1}"})
    if nav:
        kb.append(nav)
    return "\n".join(lines)[:4000], {"inline_keyboard": kb}


async def cmd(live, client, token, cid: str, arg: str = "") -> None:
    text, kb = _view(0)
    await live._send(client, token, cid, text, reply_markup=kb)


async def button(live, client, token, cid: str, data: str, message_id) -> str:
    p = data.split(":")
    ans = ""
    page = 0
    if len(p) >= 4 and p[1] == "t":
        from services.settings_service import update_settings
        i, page = int(p[2]), int(p[3])
        items, ws = _items()
        if not (0 <= i < len(items)):
            ans = "목록이 바뀌었습니다. 다시 불러왔습니다"
        else:
            t = items[i]
            w = max(1, min(5, int(ws.get(t, 1))))
            nw = 1 if w >= 5 else w + 1
            update_settings({"guidelines": {"item_weights": {t: nw}}})
            ans = f"{_stars(nw)} 저장"
    elif len(p) >= 3 and p[1] == "p":
        page = int(p[2])
    text, kb = _view(page)
    r = await live._call(client, token, "editMessageText", chat_id=cid, message_id=message_id,
                         text=text, parse_mode="HTML", reply_markup=kb) if message_id else {}
    if not (r.get("ok") or "not modified" in str(r.get("description", ""))):
        await live._send(client, token, cid, text, reply_markup=kb)
    return ans
