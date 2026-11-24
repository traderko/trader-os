"""스스로 잠그기 (수동 잠금) - 정한 시간 동안 거래를 잠금. 확인 문구로도 못 풀고 시간이 지나야만 풀림

텔레그램 /잠금 (/lock) · 실시간 현황의 [🔒] 버튼 · 웹 잠금 칸의 [🔒 스스로 잠그기]
  30분 · 1시간 · 2시간 · 오늘 끝까지(다음 거래일 시작 07:00/08:00) · /잠금 90 처럼 분 직접
  거는 건 항상 확인을 한 번 더 거침 (실수로 눌러 하루를 잠그지 않게)
  잠금 대상: 잠금을 켠 계좌 전부 (/잠금 90 87917358 처럼 계좌 하나만도 가능)
  이미 더 늦게까지 잠겨 있으면(일일 손실 한도 등) 그대로 둠 - 짧게 줄이지 않음
"""
from __future__ import annotations

import math
from datetime import datetime

from services.loss_limit import KST, next_day_start

CHOICES = [("30", "30분"), ("60", "1시간"), ("120", "2시간"), ("day", "오늘 끝까지")]


def minutes_of(choice: str) -> int:
    """'30' · '120' · 'day' → 분"""
    if choice == "day":
        return max(1, math.ceil((next_day_start(datetime.now(KST)) - datetime.now(KST)).total_seconds() / 60))
    return int(choice)


def label_of(choice: str) -> str:
    if choice == "day":
        return "오늘 끝까지"
    m = int(choice)
    return f"{m // 60}시간" + (f" {m % 60}분" if m % 60 else "") if m >= 60 else f"{m}분"


async def _accounts(only: str | None = None) -> list:
    from sqlalchemy import select
    from db.models.account import Account
    from db.session import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        q = select(Account).where(Account.lock_enabled == True).order_by(Account.id)
        accs = list((await db.execute(q)).scalars().all())
    return [a for a in accs if not only or str(a.account_number) == str(only)]


async def lock(choice: str, only: str | None = None) -> list[str]:
    """잠그고 결과 줄들"""
    from db.session import AsyncSessionLocal
    from services import trade_lock_service as svc
    mins = minutes_of(choice)
    out = []
    for a in await _accounts(only):
        async with AsyncSessionLocal() as db:
            try:
                r = await svc.set_manual_lock(db, a.id, str(a.account_number), mins)
                until = datetime.fromisoformat(r["locked_until"])
                out.append(f"🔒 {a.account_number} · {until.strftime('%m/%d %H:%M')}까지" + (" (이미 더 늦게까지 잠겨 있어 그대로)" if r.get("kept") else ""))
            except svc.InvalidLockDurationError as e:
                out.append(f"{a.account_number} · 못 잠금: {e}")
    return out


# ── 텔레그램 ──
def _pick_markup(only: str | None) -> dict:
    o = f":{only}" if only else ""
    return {"inline_keyboard": [
        [{"text": lab, "callback_data": f"lk:ask:{c}{o}"} for c, lab in CHOICES],
        [{"text": "그만두기", "callback_data": "lk:no"}],
    ]}


async def cmd(live, client, token, cid: str, arg: str = "") -> None:
    w = (arg or "").split()
    only = next((x for x in w if x.isdigit() and len(x) >= 6), None)
    mins = next((x for x in w if x.isdigit() and len(x) < 6), None)
    if mins:
        await _ask(live, client, token, cid, None, mins, only)
        return
    accs = await _accounts(only)
    who = ", ".join(str(a.account_number) for a in accs) or "잠금을 켠 계좌 없음"
    await live._send(client, token, cid,
                     f"🔒 <b>스스로 잠그기</b> · {who}\n정한 시간 동안 거래를 잠급니다. <b>확인 문구로도 못 풀고</b> 시간이 지나야만 풀립니다.\n"
                     "<i>분을 직접: /잠금 90</i>", reply_markup=_pick_markup(only))


async def _ask(live, client, token, cid, message_id, choice: str, only: str | None) -> None:
    from services import trade_lock_service as svc
    try:
        mins = minutes_of(choice)
    except ValueError:
        await live._send(client, token, cid, "분을 숫자로 적어 주세요. 예: /잠금 90")
        return
    mx = svc.lock_cfg()["manual_lock_max_minutes"]
    if mins < 1 or mins > mx:
        await live._send(client, token, cid, f"1~{mx}분 사이로 잠글 수 있습니다. (최대는 관리 화면 '수동 잠금 최대 시간')")
        return
    from datetime import timedelta
    until = datetime.now(KST) + timedelta(minutes=mins)
    accs = await _accounts(only)
    o = f":{only}" if only else ""
    text = (f"🔒 <b>{label_of(choice) if choice == 'day' else label_of(str(mins))}</b> 잠글까요? "
            f"({until.strftime('%m/%d %H:%M')}까지)\n{', '.join(str(a.account_number) for a in accs)}\n"
            "⚠️ 그 전에는 확인 문구로도 풀 수 없습니다.")
    kb = {"inline_keyboard": [[{"text": "🔒 잠그기", "callback_data": f"lk:go:{choice}{o}"},
                               {"text": "그만두기", "callback_data": "lk:no"}]]}
    r = await live._call(client, token, "editMessageText", chat_id=cid, message_id=message_id, text=text,
                         parse_mode="HTML", reply_markup=kb) if message_id else {}
    if not r.get("ok"):
        await live._send(client, token, cid, text, reply_markup=kb)


async def button(live, client, token, cid: str, data: str, message_id) -> str:
    p = data.split(":")
    act = p[1] if len(p) > 1 else ""
    only = p[3] if len(p) > 3 else None
    if act == "menu":
        await cmd(live, client, token, cid, "")
        return ""
    if act == "no":
        if message_id:
            await live._call(client, token, "editMessageText", chat_id=cid, message_id=message_id, text="잠그지 않았습니다.")
        return "그만둠"
    if act == "ask" and len(p) > 2:
        await _ask(live, client, token, cid, message_id, p[2], only)
        return ""
    if act == "go" and len(p) > 2:
        lines = await lock(p[2], only)
        text = "\n".join(lines) or "잠글 계좌가 없습니다."
        r = await live._call(client, token, "editMessageText", chat_id=cid, message_id=message_id, text=text) if message_id else {}
        if not r.get("ok"):
            await live._send(client, token, cid, text)
        live.force.set()                      # 실시간 현황 바로 갱신
        return "잠갔습니다"
    return ""
