# services/telegram_events.py
#
# 텔레그램 /지표 (/events) - 웹 '지표' 탭과 같은 이번 주 고영향 USD 지표 + 지표일 집중 구간(잠금)
#   /지표        이번 주 전체 (지난 날은 접어서)
#   버튼: [⭐ 주요만 / 전체] [🔄 새로고침]
# 데이터는 관리 화면·웹과 같은 api/admin_router.week_events

import html
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def _left(sec: float) -> str:
    m = int(sec // 60)
    if m < 60:
        return f"{max(1, m)}분 뒤"
    h, m = divmod(m, 60)
    return f"{h}시간 {m}분 뒤" if h < 24 else f"{h // 24}일 {h % 24}시간 뒤"


async def _data() -> dict:
    from api.admin_router import week_events
    from db.session import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        return await week_events(db)


def render(d: dict, key_only: bool = False) -> tuple[str, dict]:
    now = datetime.now(KST)
    days = d.get("days") or []
    lines = []
    if days:
        a, b = days[0]["broker_date"][5:].replace("-", "/"), days[-1]["broker_date"][5:].replace("-", "/")
        lines.append(f"📅 <b>이번 주 지표</b> ({a}~{b}) · {d.get('total', 0)}개 (⭐ {d.get('key_total', 0)})")
    # 다음 발표
    nxt = None
    for day in days:
        for e in day["events"]:
            t = datetime.fromisoformat(e["time"])
            if t > now and (not key_only or e["key"]):
                nxt = (t, e)
                break
        if nxt:
            break
    if nxt:
        t, e = nxt
        lines.append(f"⏰ 다음: {'⭐ ' if e['key'] else ''}{html.escape(e['name'].replace('[FF] ', ''))} · "
                     f"{t:%m/%d %H:%M} ({_left((t - now).total_seconds())})")
    past_days = 0
    for day in days:
        evs = [e for e in day["events"] if e["key"] or not key_only]
        bd = day["broker_date"]
        today_bd = next((x["broker_date"] for x in days if x["today"]), "")
        is_past = bool(today_bd) and bd < today_bd            # 지난 날은 접음
        if not evs and not day["locks"]:
            continue
        if is_past:
            past_days += 1
            continue
        head = f"\n<b>{bd[5:].replace('-', '/')} ({day['weekday'][0]})</b>" + (" · 오늘" if day["today"] else "")
        lines.append(head)
        for e in evs:
            t = datetime.fromisoformat(e["time"])
            done = t < now
            name = html.escape(e["name"].replace("[FF] ", "")) + (" <i>(FF)</i>" if e["source"] == "ff" else "")
            mark = "⭐ " if e["key"] else ""
            row = f"{'✓' if done else '•'} {t:%H:%M} {mark}{name}"
            lines.append(f"<s>{row}</s>" if done else (f"<b>{row}</b>" if e["key"] else row))
        for lk in day["locks"]:
            s, e_ = datetime.fromisoformat(lk["start"]), datetime.fromisoformat(lk["end"])
            lines.append(f"🔒 {html.escape(lk['name'])} 잠금 {s:%H:%M}~{e_:%H:%M}")
    if past_days:
        lines.append(f"\n(지난 {past_days}일은 접음 - 웹 '지표' 탭에서 전체)")
    if not d.get("total"):
        lines.append("\n이번 주에 저장된 고영향 지표가 없습니다.")
    hl = d.get("health") or {}
    bad = [k for k, v in hl.items() if v.get("error")]
    if bad:
        lines.append(f"\n⚠️ 지표 가져오기 오류: {', '.join(bad)} (관리 화면 지표 탭)")
    kb = [[{"text": "전체 보기" if key_only else "⭐ 주요만", "callback_data": "ev:all" if key_only else "ev:key"},
           {"text": "🔄 새로고침", "callback_data": "ev:key" if key_only else "ev:all"}]]
    return "\n".join(lines), {"inline_keyboard": kb}


async def cmd_events(live, client, token, chat_id: str, args: str = "") -> None:
    try:
        text, markup = render(await _data(), key_only="주요" in (args or "") or "key" in (args or "").lower())
    except Exception as e:
        text, markup = f"지표를 불러오지 못했습니다: {html.escape(str(e))[:200]}", None
    await live._send(client, token, chat_id, text, **({"reply_markup": markup} if markup else {}))


async def button(live, client, token, chat_id: str, data: str, message_id) -> str:
    key_only = data == "ev:key"
    try:
        text, markup = render(await _data(), key_only=key_only)
    except Exception as e:
        return f"실패: {e}"[:190]
    r = await live._call(client, token, "editMessageText", chat_id=chat_id, message_id=message_id, text=text,
                         parse_mode="HTML", disable_web_page_preview=True, reply_markup=markup)
    if not r.get("ok") and "not modified" not in str(r.get("description", "")):
        await live._send(client, token, chat_id, text, reply_markup=markup)
    return "주요 지표만" if key_only else "전체"
