# services/telegram_mt5.py
#
# 텔레그램에서 MT5 재시작 (관리 화면 계좌 탭 [MT5 재시작]과 같은 동작) - 실시간 현황의 [🔁 MT5 재시작] 버튼
#   mt5:ask[:<계좌id>]  → 확인 메시지 (계좌가 여럿이면 고르기)
#   mt5:go:<계좌id>     → 재시작, 끝나면 결과 알림
#
# 안전장치
#   - 등록된 대화(chat id)에서만 (telegram_live 에서 확인)
#   - 한 번 더 확인 버튼을 눌러야 실행
#   - 거래 잠금 중에는 거절: 재시작 직후 EA 가 다시 붙기 전 몇 초 동안 들어간 포지션은 EA 가 '잠금 전 포지션'으로
#     보호해 버려서, 잠금을 피해 거래하는 틈이 될 수 있음
#   - 이미 켜는 중이면 거절
#   주문을 넣거나 포지션을 건드리지는 않음 (MT5 를 정상 종료 → 다시 실행 → 워커·EA 다시 연결)

import asyncio
import html

_bg: set = set()


async def _accounts():
    from sqlalchemy import select
    from db.models.account import Account
    from db.session import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(Account).where(Account.lock_enabled == True).order_by(Account.id))).scalars().all()


async def _lock_reason(aid: int) -> str:
    try:
        from db.session import AsyncSessionLocal
        from services import trade_lock_service as lock_svc
        async with AsyncSessionLocal() as db:
            st = await lock_svc.get_lock_state(db, aid)
        if not st.get("locked"):
            return ""
        r = str(st.get("reason") or "")
        names = {"session_window": "집중 구간", "restricted_window": "정기 재확인", "consec_loss": "연속 손절",
                 "manual": "수동/손실 한도 잠금"}
        return next((v for k, v in names.items() if r.startswith(k)), r or "잠금")
    except Exception as e:
        return f"잠금 상태 확인 실패 ({str(e)[:60]})"


def button_row(aid: int | None = None) -> list[dict]:
    return [{"text": "🔁 MT5 재시작", "callback_data": f"mt5:ask:{aid}" if aid else "mt5:ask"}]


async def button(live, client, token, chat_id: str, data: str, message_id) -> str:
    parts = data.split(":")
    act = parts[1] if len(parts) > 1 else ""
    accs = await _accounts()
    if act == "ask":
        aid = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
        targets = [a for a in accs if aid is None or a.id == aid]
        if not targets:
            return "MT5 실행이 켜진 계좌가 없습니다"
        kb = [[{"text": f"✅ {a.account_number} 재시작", "callback_data": f"mt5:go:{a.id}"}] for a in targets]
        kb.append([{"text": "취소", "callback_data": "mt5:no"}])
        await live._send(client, token, chat_id,
                         "🔁 <b>MT5 재시작</b>\nMT5 를 정상 종료한 뒤 다시 띄우고 tradeLock EA·워커를 다시 붙입니다 (30초~1분)."
                         "\n포지션·주문은 그대로입니다. 잠금 중에는 할 수 없습니다.", reply_markup={"inline_keyboard": kb})
        return ""
    if act == "no":
        await live._call(client, token, "editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                         reply_markup={"inline_keyboard": []})
        return "취소했습니다"
    if act == "go":
        aid = int(parts[2])
        acc = next((a for a in accs if a.id == aid), None)
        if acc is None:
            return "MT5 실행이 켜진 계좌가 아닙니다"
        why = await _lock_reason(aid)
        if why:
            return f"잠금 중이라 재시작하지 않습니다 ({why[:40]}) - 풀린 뒤 다시"[:190]
        from api.admin_router import RUNTIME, _run_in_background
        if RUNTIME.get("restart_terminal") is None:
            return "이 서버에서는 MT5 재시작을 쓸 수 없습니다"
        if aid in RUNTIME["starting"]:
            return "이미 켜는 중입니다"
        _run_in_background(aid, start=True, action="restart_terminal")
        await live._call(client, token, "editMessageText", chat_id=chat_id, message_id=message_id, parse_mode="HTML",
                         text=f"🔁 <b>{html.escape(str(acc.account_number))}</b> MT5 재시작 중… 끝나면 알려 드립니다.")
        t = asyncio.create_task(_follow(live, client, token, chat_id, aid, str(acc.account_number)))
        _bg.add(t)
        t.add_done_callback(_bg.discard)
        return "재시작합니다"
    return ""


async def _follow(live, client, token, chat_id, aid: int, num: str) -> None:
    """재시작이 끝나길 기다렸다가 결과 + EA 버전"""
    from api.admin_router import RUNTIME
    from services import ea_status
    await asyncio.sleep(3)
    for _ in range(60):                       # 최대 3분
        if aid not in RUNTIME["starting"]:
            break
        await asyncio.sleep(3)
    err = RUNTIME["start_errors"].get(aid)
    if err:
        await live._send(client, token, chat_id, f"❌ {html.escape(num)} MT5 재시작 실패: {html.escape(str(err))[:300]}")
        return
    await asyncio.sleep(15)                   # EA 가 붙어서 서버에 물어볼 시간
    vi = ea_status.version_info(aid)
    st = ea_status.state(aid, RUNTIME.get("started_at", 0))
    ea = ("EA 연결됨" if st["state"] == "ok" else "EA 아직 연결 안 됨 (조금 더 기다려 보세요)")
    if vi.get("version"):
        ea += f" · v{vi['version']}" + (f" (최신 v{vi['latest']} 아님)" if vi["outdated"] and not vi.get("dup") else "")
    if vi.get("dup"):
        ea += " · ⚠️ 예전 EA 도 같이 돌고 있음 (다른 차트나 다른 PC 의 MT5 - 그 EA 를 빼야 함)"
    await live._send(client, token, chat_id, f"✅ {html.escape(num)} MT5 재시작 끝 · {html.escape(ea)}")
    live.force.set()
