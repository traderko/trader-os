"""알림 중요도 - 기능별 알림마다 🔴 긴급 · 🟠 중요 · ⚪ 보통 · 🔕 조용

  🔴 긴급 : 맨 위 '🔴🔴🔴 긴급' 머리말 + 줄로 강조, [✅ 확인] 버튼 - 누를 때까지 repeat_min 분마다 다시 (최대 repeat_max 번)
           긴급 전용 대화방(urgent_chat_id)을 정해 두면 거기에도 같이 보냄 → 폰에서 그 대화방만 알림음을 크게 따로 설정
  🟠 중요 : 맨 위 '🟠 중요 ·' 머리말
  ⚪ 보통 : 그대로
  🔕 조용 : 알림음 없이 (disable_notification) - 대화방에는 그대로 남음

알림이 어떤 기능인지(kind)는 보내는 곳이 알려 줌
  - 푸시 + 텔레그램(FCMService.send) : 두 번째 인자 payload 의 "type" (지침은 "guide:<규칙>")
  - 그 밖에 텔레그램으로 바로 보내는 곳(출금 · 장 상태 · 지갑 · KTR 브리핑) : 그 모듈이 kind 를 넘김
  모르는 kind 는 ⚪ 보통

설정: settings "alerts" = {"levels": {kind: level}, "urgent_chat_id": "", "repeat_min": 3, "repeat_max": 5}
  텔레그램 /중요도 (/alerts) · 관리 화면 텔레그램 탭 '알림 중요도'
긴급 다시 알림: 보낸 프로세스(서버 · 계좌별 워커)가 data/urgent/<id>.json 을 남기고, 서버(텔레그램 실시간 현황 작업)가 다시 보냄
"""
from __future__ import annotations

import html
import json
import os
import time
import uuid

LEVELS = ("urgent", "high", "normal", "quiet")
LEVEL_KR = {"urgent": "🔴 긴급", "high": "🟠 중요", "normal": "⚪ 보통", "quiet": "🔕 조용"}
DEFAULTS = {"urgent_chat_id": "", "repeat_min": 3, "repeat_max": 5}
URGENT_HEAD = "🔴🔴🔴 긴급 🔴🔴🔴"
LINE = "━━━━━━━━━━━━━━"
KEEP_SEC = 2 * 3600                       # 긴급 다시 알림을 이보다 오래 붙잡지 않음

# (kind, 이름, 기본 중요도, 묶음)
CATEGORIES: list[tuple[str, str, str, str]] = [
    ("trade_lock_forced_close", "잠금 중 진입 → 강제청산", "urgent", "잠금"),
    ("trade_stopout", "증거금 부족 강제청산 (스탑아웃)", "urgent", "체결"),
    ("daily_loss_block", "일일 손실 한도 - 새 진입 차단", "urgent", "손실 한도"),
    ("daily_loss_close", "일일 손실 한도 - 전부 청산", "urgent", "손실 한도"),
    ("margin_lock", "증거금 잠금 걸림", "urgent", "증거금"),
    ("mt5_down", "MT5 응답 없음", "urgent", "서버 · MT5"),
    ("nosl_guard", "손절 없는 포지션 자동청산 카운트다운", "urgent", "체결"),
    ("nosl_result", "손절 없는 포지션 청산 · 버팀 · 취소", "high", "체결"),
    ("guide:margin_use", "증거금 경고 (10 · 20 · 30%)", "high", "증거금"),
    ("daily_loss_warn", "일일 손실 경고", "high", "손실 한도"),
    ("breakout", "돌파캔들(BOB) · 반대 포지션", "high", "시장"),
    ("trade_lock_consec_loss", "연속 손절 잠금", "high", "잠금"),
    ("loss_reentry", "손실 직후 빠른 재진입", "high", "체결"),
    ("ea_outdated", "EA 예전 버전 · 두 개 실행", "high", "서버 · MT5"),
    ("market", "휴장 · 조기 종료 · 거래 멈춤", "high", "시장"),
    ("market_info", "휴장 예정 - 포지션 없는 종목", "quiet", "시장"),
    ("guide:no_sl", "지침 - 손절 미설정", "high", "지침"),
    ("guide:event_hold", "지침 - 지표 · 장 시작 전 포지션", "high", "지침"),
    ("guide:revenge", "지침 - 손절 직후 재진입", "high", "지침"),
    ("guide", "지침 - 나머지 (과매매 · 비중 급증 등)", "normal", "지침"),
    ("trade_open", "체결 알림 - 진입", "normal", "체결"),
    ("trade_close", "체결 알림 - 청산", "normal", "체결"),
    ("trade_lock_checkin_block", "정기 재확인 잠금", "normal", "잠금"),
    ("trade_lock_session", "집중 구간 잠금", "normal", "잠금"),
    ("trade_lock_manual", "수동 잠금", "normal", "잠금"),
    ("trade_lock_unlocked", "잠금 풀림 · 연속 손절 풀림", "normal", "잠금"),
    ("margin_unlock", "증거금 잠금 풀림", "normal", "증거금"),
    ("daily_loss_release", "손실 한도 잠금 풀림", "normal", "손실 한도"),
    ("mt5_up", "MT5 다시 연결됨", "normal", "서버 · MT5"),
    ("withdraw", "출금 가능 · 출금 확인", "normal", "돈"),
    ("withdraw_big", "큰 수익 출금 알림", "high", "돈"),
    ("wallet", "지갑 입금", "normal", "돈"),
    ("oneway", "원웨이 장", "normal", "시장"),
    ("event", "지표 발표 예정", "normal", "시장"),
    ("grid", "구간매매 시작 · 추가 리밋", "normal", "체결"),
    ("ktr_brief", "장 시작 KTR 브리핑", "quiet", "시장"),
]
_DEFAULT_LEVEL = {k: lv for k, _, lv, _ in CATEGORIES}
# 보내는 곳이 쓰는 이름 → 위 목록의 kind
# 긴급이어도 다시 알림 · 확인 버튼을 붙이지 않는 것 (자기 버튼 · 카운트다운이 있는 알림)
NO_REPEAT = {"nosl_guard"}
ALIAS = {"trade_lock_consec_clear": "trade_lock_unlocked", "addon": "grid", "guideline": "guide"}


# ── 설정 ──
def cfg() -> dict:
    from services.settings_service import get_settings
    raw = get_settings().get("alerts") or {}
    out = dict(DEFAULTS)
    out["urgent_chat_id"] = str(raw.get("urgent_chat_id") or "").strip()
    try:
        out["repeat_min"] = min(60, max(1, int(raw.get("repeat_min", DEFAULTS["repeat_min"]))))
        out["repeat_max"] = min(20, max(0, int(raw.get("repeat_max", DEFAULTS["repeat_max"]))))
    except (TypeError, ValueError):
        pass
    lv = raw.get("levels") or {}
    out["levels"] = {k: (lv.get(k) if lv.get(k) in LEVELS else d) for k, d in _DEFAULT_LEVEL.items()}
    return out


def save(patch: dict) -> dict:
    from services.settings_service import get_settings, update_settings
    cur = dict(get_settings().get("alerts") or {})
    if "levels" in patch:
        lv = dict(cur.get("levels") or {})
        lv.update({k: v for k, v in (patch.pop("levels") or {}).items() if k in _DEFAULT_LEVEL and v in LEVELS})
        cur["levels"] = lv
    cur.update(patch)
    update_settings({"alerts": cur})
    return cfg()


def norm(kind: str | None) -> str | None:
    if not kind:
        return None
    return ALIAS.get(kind, kind)


def level_of(kind: str | None) -> str:
    k = norm(kind)
    if not k:
        return "normal"
    if k.startswith("__test_") and k[7:] in LEVELS:
        return k[7:]
    lv = cfg()["levels"]
    if k in lv:
        return lv[k]
    if k.startswith("guide:"):
        return lv.get("guide", "normal")
    return "normal"


def kind_from_payload(payload: str | None) -> str | None:
    try:
        p = json.loads(payload or "{}")
    except ValueError:
        return None
    t = p.get("type")
    if t == "guideline":
        return f"guide:{p['rule']}" if p.get("rule") else "guide"
    return t


# ── 꾸미기 ──
def decorate_plain(kind: str | None, title: str, body: str) -> tuple[str, str, str]:
    """(레벨, 제목, 본문) - 글자 그대로 보내는 곳(telegram_service)"""
    lv = level_of(kind)
    if lv == "urgent":
        return lv, f"{URGENT_HEAD}\n{title}\n{LINE}", body
    if lv == "high":
        return lv, f"🟠 중요 · {title}", body
    return lv, title, body


def decorate_html(kind: str | None, text: str) -> tuple[str, str]:
    """(레벨, 글) - HTML 로 보내는 곳 (첫 줄을 제목으로 봄)"""
    lv = level_of(kind)
    if lv == "urgent":
        first, _, rest = text.partition("\n")
        return lv, f"<b>{URGENT_HEAD}</b>\n{first}\n{LINE}" + (f"\n{rest}" if rest else "")
    if lv == "high":
        return lv, f"🟠 <b>중요</b> · {text}"
    return lv, text


async def send_html(live, client, kind: str | None, text: str, markup: dict | None = None) -> dict:
    """HTML 알림을 중요도대로 - 메인 대화방 (+긴급이면 긴급 대화방 · 확인 버튼 · 다시 알림). 메인 결과를 돌려줌"""
    from services.settings_service import telegram_cfg
    t = telegram_cfg()
    if not (t["bot_token"] and t["chat_id"]):
        return {"ok": False}
    lv, text = decorate_html(kind, text)
    rows = list((markup or {}).get("inline_keyboard") or [])
    targets = [t["chat_id"]]
    if lv == "urgent":
        aid = register(text, "HTML", rows)
        rows = [[ack_button(aid)]] + rows
        targets = chats() or targets
    extra = {"reply_markup": {"inline_keyboard": rows}} if rows else {}
    if lv == "quiet":
        extra["disable_notification"] = True
    first = {"ok": False}
    for i, cid in enumerate(targets):
        r = await live._send(client, t["bot_token"], cid, text, **extra)
        if not r.get("ok") and extra.get("reply_markup"):          # 오래된 앱 등에서 버튼이 안 되면 버튼 없이라도
            r = await live._send(client, t["bot_token"], cid, text, **{k: v for k, v in extra.items() if k != "reply_markup"})
        if i == 0:
            first = r
    return first


def ack_button(aid: str) -> dict:
    return {"text": "✅ 확인 (다시 알림 멈춤)", "callback_data": f"ua:{aid}"}


# ── 긴급 다시 알림 (프로세스 사이 - 파일로) ──
def _dir() -> str:
    from services.settings_service import DATA_DIR
    return os.path.join(DATA_DIR, "urgent")


def register(text: str, parse_mode: str | None, buttons: list | None) -> str:
    """긴급 알림 기록 - 서버가 확인될 때까지 다시 보냄. id 를 돌려줌 (버튼 callback 'ua:<id>')"""
    aid = uuid.uuid4().hex[:12]
    c = cfg()
    rec = {"id": aid, "text": text, "parse_mode": parse_mode, "buttons": buttons or [], "at": time.time(),
           "next": time.time() + c["repeat_min"] * 60, "count": 0}
    try:
        os.makedirs(_dir(), exist_ok=True)
        p = os.path.join(_dir(), aid + ".json")
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False)
        os.replace(p + ".tmp", p)
    except OSError as e:
        print(f"[alert] 긴급 기록 실패: {e}")
    return aid


def ack(aid: str) -> bool:
    if not aid.isalnum():
        return False
    p = os.path.join(_dir(), aid + ".json")
    try:
        os.remove(p)
        return True
    except OSError:
        return False


def pending() -> list[dict]:
    out = []
    try:
        names = os.listdir(_dir())
    except OSError:
        return []
    for n in names:
        if not n.endswith(".json"):
            continue
        try:
            with open(os.path.join(_dir(), n), encoding="utf-8") as f:
                out.append(json.load(f))
        except (OSError, ValueError):
            continue
    return out


def _update(rec: dict) -> None:
    p = os.path.join(_dir(), rec["id"] + ".json")
    if not os.path.exists(p):
        return                                   # 그사이 확인됨
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False)
    os.replace(p + ".tmp", p)


def chats() -> list[str]:
    """긴급을 보낼 대화방 - 메인 + 긴급 전용(있으면)"""
    from services.settings_service import telegram_cfg
    main = str(telegram_cfg()["chat_id"] or "")
    extra = cfg()["urgent_chat_id"]
    out = [main] if main else []
    if extra and extra != main:
        out.append(extra)
    return out


async def loop(live, client) -> None:
    """서버(텔레그램 실시간 현황 작업)에서 - 확인 안 된 긴급 알림을 다시 보냄"""
    import asyncio
    from services.settings_service import telegram_cfg
    await asyncio.sleep(20)
    while True:
        try:
            c = cfg()
            token = telegram_cfg()["bot_token"]
            now = time.time()
            for rec in pending():
                if now - rec.get("at", now) > KEEP_SEC or rec.get("count", 0) >= c["repeat_max"]:
                    ack(rec["id"])                   # 끝 - 더 안 보냄
                    continue
                if now < rec.get("next", 0) or not token:
                    continue
                rec["count"] = rec.get("count", 0) + 1
                rec["next"] = now + c["repeat_min"] * 60
                _update(rec)
                head = f"🔁 다시 알림 {rec['count']}/{c['repeat_max']} · 확인 버튼을 눌러 주세요\n"
                text = (html.escape(head) if rec.get("parse_mode") == "HTML" else head) + rec["text"]
                kb = [[ack_button(rec["id"])]] + [r for r in rec.get("buttons") or [] if not any(
                    str(b.get("callback_data", "")).startswith("ua:") for b in r)]
                for cid in chats():
                    extra = {"parse_mode": rec["parse_mode"]} if rec.get("parse_mode") else {}
                    await live._call(client, token, "sendMessage", chat_id=cid, text=text, disable_web_page_preview=True,
                                     reply_markup={"inline_keyboard": kb}, **extra)
        except Exception as e:
            print(f"[alert] 긴급 다시 알림 실패: {e}")
        await asyncio.sleep(15)


# ── /중요도 (/alerts) ──
PER = 10


def _view(page: int = 0) -> tuple[str, dict]:
    c = cfg()
    n = len(CATEGORIES)
    pages = (n - 1) // PER + 1
    page = max(0, min(page, pages - 1))
    rows = []
    for k, label, _, grp in CATEGORIES[page * PER:(page + 1) * PER]:
        rows.append([{"text": f"{LEVEL_KR[c['levels'][k]].split()[0]} {label}"[:60], "callback_data": f"al:c:{k}:{page}"}])
    nav = []
    if page > 0:
        nav.append({"text": "◀", "callback_data": f"al:p:{page - 1}"})
    nav.append({"text": f"{page + 1}/{pages}", "callback_data": f"al:p:{page}"})
    if page < pages - 1:
        nav.append({"text": "▶", "callback_data": f"al:p:{page + 1}"})
    rows.append(nav)
    rows.append([{"text": "🔔 긴급 알림 시험", "callback_data": "al:t"}])
    uc = c["urgent_chat_id"]
    text = ("🔔 <b>알림 중요도</b> — 버튼을 누를 때마다 🔴 긴급 → 🟠 중요 → ⚪ 보통 → 🔕 조용 순서로 바뀜\n"
            "🔴 긴급: 큰 머리말 + [✅ 확인] 누를 때까지 "
            f"{c['repeat_min']}분마다 다시 (최대 {c['repeat_max']}번)"
            + (f" · 긴급 대화방 {html.escape(uc)} 에도" if uc else "") + "\n"
            "🟠 중요: 머리말 · ⚪ 보통: 그대로 · 🔕 조용: 알림음 없이\n\n"
            "<i>긴급 전용 대화방: 그룹을 만들어 이 봇을 넣고, 그 그룹에서 /id → /중요도 긴급방 &lt;chat id&gt;\n"
            "다시 알림: /중요도 반복 3 5 (3분마다 최대 5번)</i>")
    return text, {"inline_keyboard": rows}


async def cmd(live, client, token, cid: str, arg: str = "") -> None:
    w = (arg or "").split()
    if w and w[0] in ("긴급방", "urgent"):
        v = w[1] if len(w) > 1 else ""
        save({"urgent_chat_id": "" if v in ("", "끄기", "off") else v})
    elif w and w[0] in ("반복", "repeat"):
        nums = [int(x) for x in w[1:] if x.isdigit()]
        patch = {}
        if nums:
            patch["repeat_min"] = nums[0]
        if len(nums) > 1:
            patch["repeat_max"] = nums[1]
        if patch:
            save(patch)
    text, kb = _view(0)
    await live._send(client, token, cid, text, reply_markup=kb)


async def button(live, client, token, cid: str, data: str, message_id: int | None) -> str:
    parts = data.split(":")
    ans = ""
    page = 0
    if parts[1] == "c" and len(parts) >= 4:
        k, page = ":".join(parts[2:-1]), int(parts[-1])
        cur = cfg()["levels"].get(k, "normal")
        nxt = LEVELS[(LEVELS.index(cur) + 1) % len(LEVELS)]
        save({"levels": {k: nxt}})
        ans = f"{LEVEL_KR[nxt]}"
    elif parts[1] == "p":
        page = int(parts[2])
    elif parts[1] == "t":
        from services.telegram_service import TelegramService
        TelegramService().send("🔔 긴급 알림 시험", "이렇게 보입니다. [✅ 확인] 을 누르면 다시 알림이 멈춥니다.", kind="__test_urgent")
        return "긴급 알림 시험을 보냈어요"
    text, kb = _view(page)
    await live._call(client, token, "editMessageText", chat_id=cid, message_id=message_id, text=text,
                     parse_mode="HTML", reply_markup=kb)
    return ans
