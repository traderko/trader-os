"""출금 알림 (반자동) - 브로커에 출금 API 가 없어서 출금 자체는 직접, 금액 계산과 알림만 자동

  - 장이 바뀌기 '알림 시각(기본 30분 전)'마다 계좌별 오늘 출금 가능액을 텔레그램으로 보냄
    (유로장 시작 전 · 미장 시작 전 · 거래일 끝나기 전. 0 이거나 지난 알림과 같은 금액이면 안 보냄)
    버튼: [📋 금액 복사] [💸 출금 페이지 (브로커 회원 페이지)]
  - 알림 뒤 그 거래일이 끝날 때까지 2분마다 입출금 기록을 보고, 출금이 잡히면 '출금 확인됨 · 남은 출금 가능' 알림
  - /출금 (/withdraw) : 지금 바로 계좌마다 출금 가능액 + 버튼
    /출금 알림 30      : 장 바뀌기 30분 전에 알림 (10~300)
    /출금 알림 끄기|켜기
  - 큰 수익 알림: 오늘 실현 수익(청산된 손익)이 거래일 시작 잔고의 20% · 40% · 60% … 를 넘을 때마다
    바로 출금 가능액 알림 (번 돈이 다시 매매에 들어가기 전에)
    /출금 큰수익 30 : 30% 마다 · /출금 큰수익 끄기|켜기

출금 가능액 계산은 웹 손익 탭 출금 카드와 같음 (api/account_router.get_withdraw_rule).
주의: 거래일이 바뀐 뒤(한국 07시, 서머타임 아닐 때 08시)에 출금하면 다음 거래일 몫에서 빠짐.
브로커는 MT5 계좌 정보의 회사·서버 이름으로 구분 (한텍 · 인피녹스). 출금 페이지 주소는 settings "withdraw_notice.links" 로 바꿀 수 있음.
상태는 data/withdraw_notice.json (재시작해도 같은 날 알림을 또 보내지 않게)
"""
from __future__ import annotations

import asyncio
import html
import json
import os
import time
from datetime import datetime, timedelta

from sqlalchemy import select

from db.models.account import Account
from db.session import AsyncSessionLocal

LOOP_SEC = 60
CHECK_SEC = 120                 # 알림 뒤 출금 확인 간격
LINKS = {
    "infinox": ("인피녹스", "https://myaccount.infinox.com/ko/funds/withdraw"),
    "hantec": ("한텍", "https://portal-mu.hmarkets.com/#walletbal"),
}
DEFAULTS = {"enabled": True, "lead_min": 30, "big_enabled": True, "big_pct": 20}
BIG_RECHECK_SEC = 300         # 큰 수익 같지만 확인해 보니 아니면(입금 등) 이만큼 뒤에 다시 확인


# ── 설정 · 상태 ──
def cfg() -> dict:
    from services.settings_service import get_settings
    raw = get_settings().get("withdraw_notice") or {}
    try:
        lead = min(300, max(10, int(raw.get("lead_min", DEFAULTS["lead_min"]))))
    except (TypeError, ValueError):
        lead = DEFAULTS["lead_min"]
    links = dict(LINKS)
    for k, url in (raw.get("links") or {}).items():
        if k in links and isinstance(url, str) and url.startswith("https://"):
            links[k] = (links[k][0], url)
    try:
        big = min(500, max(5, float(raw.get("big_pct", DEFAULTS["big_pct"]))))
    except (TypeError, ValueError):
        big = DEFAULTS["big_pct"]
    return {"enabled": bool(raw.get("enabled", True)), "lead_min": lead, "links": links,
            "big_enabled": bool(raw.get("big_enabled", True)), "big_pct": big}


def save_cfg(patch: dict) -> None:
    from services.settings_service import get_settings, update_settings
    cur = dict(get_settings().get("withdraw_notice") or {})
    cur.update(patch)
    update_settings({"withdraw_notice": cur})


def _path() -> str:
    from services.settings_service import DATA_DIR
    return os.path.join(DATA_DIR, "withdraw_notice.json")


def _load() -> dict:
    try:
        with open(_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(st: dict) -> None:
    try:
        tmp = _path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
        os.replace(tmp, _path())
    except OSError as e:
        print(f"[withdraw-notice] 상태 저장 실패: {e}")


# ── 브로커 ──
_broker_cache: dict[int, str] = {}


async def broker_of(aid: int) -> str:
    """'infinox' | 'hantec' | '' - MT5 계좌 정보의 회사·서버 이름으로"""
    if aid in _broker_cache:
        return _broker_cache[aid]
    from services.worker_client import worker_get
    try:
        info = await asyncio.wait_for(worker_get(aid, "/account-info"), 5)
    except Exception:
        return ""
    name = f"{info.get('company') or ''} {info.get('server') or ''}".lower()
    b = "infinox" if ("infinox" in name or "ix capital" in name) else "hantec" if "hantec" in name else ""
    _broker_cache[aid] = b
    return b


# ── 계산 · 메시지 ──
async def _rule(acc: Account) -> dict | None:
    from api.account_router import get_withdraw_rule
    try:
        async with AsyncSessionLocal() as db:
            r = await asyncio.wait_for(get_withdraw_rule(int(acc.account_number), db), 20)
    except Exception as e:
        print(f"[withdraw-notice] {acc.account_number} 계산 실패: {e}")
        return None
    return r if r.get("available", True) else None


def _f(v: float) -> str:
    return f"{v:,.2f}"


def _deadline_txt(now: datetime | None = None) -> str:
    from services.loss_limit import next_day_start, KST
    end = next_day_start(now or datetime.now(KST)).astimezone(KST)
    return f"{end:%H:%M}"


async def _message(acc: Account, r: dict, head: str = "💸 오늘 출금 가능") -> tuple[str, dict]:
    c = cfg()
    b = await broker_of(acc.id)
    base = "재입금 뒤 순수익" if r.get("reset") else "오늘 순수익"
    lines = [f"{head} · <b>{html.escape(acc.account_number)}</b>" + (f" ({c['links'][b][0]})" if b else ""),
             f"<b>{_f(r['withdrawable'])}</b>  ({base} {r['net_profit']:+,.2f}의 {r['pct']:g}%"
             + (f" − 출금 {_f(r['withdrawal_total'])}" if r.get("withdrawal_total") else "") + ")"]
    if r.get("over"):
        lines.append(f"⚠️ 오늘 규칙보다 {_f(r['over'])} 더 출금했습니다")
    lines.append(f"<i>{_deadline_txt()} 전에 출금해야 오늘 몫으로 계산됩니다 (그 뒤면 다음 거래일 몫에서 빠짐)</i>")
    kb = []
    row = []
    if r["withdrawable"] > 0:
        row.append({"text": "📋 금액 복사", "copy_text": {"text": f"{r['withdrawable']:.2f}"}})
    if b:
        row.append({"text": f"💸 {c['links'][b][0]} 출금 페이지", "url": c["links"][b][1]})
    if row:
        kb.append(row)
    return "\n".join(lines), ({"inline_keyboard": kb} if kb else {})


async def _accounts() -> list[Account]:
    async with AsyncSessionLocal() as db:
        return list((await db.execute(select(Account).where(Account.lock_enabled == True).order_by(Account.id))).scalars().all())


async def _send(live, client, text: str, markup: dict, kind: str = "withdraw") -> None:
    """알림 중요도(services/alert_level.py)대로 - 복사 버튼이 안 되는 앱이면 버튼 없이라도"""
    from services import alert_level
    await alert_level.send_html(live, client, kind, text, markup)


# ── 정기 알림 ──
def boundaries(now: datetime) -> list[tuple[str, str, datetime]]:
    """이 거래일의 장 바뀌는 시각 [(키, 이름, 시각)] - 유로장 시작 · 미장 시작 · 거래일 끝"""
    from services import grid
    from services.loss_limit import KST, next_day_start
    end = next_day_start(now).astimezone(KST)
    ref = end - timedelta(seconds=1)
    return [("euro", "유로장 시작", grid.session_open("euro", ref).astimezone(KST)),
            ("us", "미장 시작", grid.session_open("us", ref).astimezone(KST)),
            ("end", "거래일 끝", end)]


async def check_once(live, client, now: datetime | None = None) -> None:
    from services.loss_limit import KST, trading_day
    c = cfg()
    if not (c["enabled"] or c["big_enabled"]):
        return
    now = now or datetime.now(KST)
    day = trading_day(now)
    if datetime.fromisoformat(day).weekday() >= 5:      # 주말 거래일은 없음
        return
    lead = timedelta(minutes=c["lead_min"])
    due = [(k, name, t) for k, name, t in boundaries(now) if t - lead <= now < t]
    st = _load()
    changed = False
    for acc in await _accounts():
        key = str(acc.id)
        rec = st.get(key) or {}
        if rec.get("day") != day:                       # 새 거래일 - 지난 기록 비움
            rec = {"day": day, "sent": [], "last": None, "wd": None, "checked": 0}
            st[key] = rec
            changed = True
        # 0) 큰 수익 알림 (오늘 실현 수익이 시작 잔고의 N% 단계를 넘을 때마다)
        if c["big_enabled"] and await _big_profit(live, client, acc, rec, c):
            changed = True
        if not c["enabled"]:
            continue
        # 1) 장 바뀌기 전 알림 (그 시점마다 한 번)
        todo = [(k, name, t) for k, name, t in due if k not in rec["sent"]]
        if todo:
            k, name, t = todo[-1]
            r = await _rule(acc)
            if r is None:
                continue                                # MT5 끊김 - 다음 1분에 다시
            rec["sent"] = list(set(rec["sent"]) | {x[0] for x in todo})
            if rec.get("wd") is None:
                rec["wd"] = r.get("withdrawal_total") or 0
            changed = True
            amt = round(r["withdrawable"], 2)
            if amt > 0 and amt != rec.get("last"):      # 0 이거나 지난번과 같은 금액이면 조용히
                rec["last"] = amt
                mins = max(1, round((t - now).total_seconds() / 60))
                text, kb = await _message(acc, r, head=f"💸 {name} {mins}분 전 · 출금 가능")
                await _send(live, client, text, kb)
            continue
        # 2) 첫 알림 뒤 - 출금이 잡히면 확인 알림
        if rec.get("wd") is not None and time.time() - rec.get("checked", 0) >= CHECK_SEC:
            rec["checked"] = time.time()
            changed = True
            r = await _rule(acc)
            if r is None:
                continue
            wd = r.get("withdrawal_total") or 0
            if wd > (rec.get("wd") or 0) + 0.005:
                rec["wd"] = wd
                rec["last"] = round(r["withdrawable"], 2)
                text = (f"✅ 출금 확인됨 · <b>{html.escape(acc.account_number)}</b>\n"
                        f"오늘 출금 {_f(wd)} · 남은 출금 가능 <b>{_f(r['withdrawable'])}</b>"
                        + (f"\n⚠️ 오늘 규칙보다 {_f(r['over'])} 더 출금했습니다" if r.get("over") else ""))
                await _send(live, client, text, {})
    if changed:
        _save(st)


async def _big_profit(live, client, acc: Account, rec: dict, c: dict) -> bool:
    """오늘 실현 수익이 시작 잔고의 big_pct 단계를 새로 넘었으면 알림. 기록이 바뀌었으면 True
    매분 잔고(/account-info)만 보고, 넘은 것 같을 때만 정확한 계산(출금 규칙 - 입출금 반영)으로 확인"""
    from services.worker_client import worker_get
    if rec.get("start") is None:
        if time.time() - rec.get("big_checked", 0) < 60:
            return False
        rec["big_checked"] = time.time()
        r = await _rule(acc)
        if r is None or not r.get("start_balance"):
            return True
        rec["start"] = float(r["start_balance"])
        return True
    try:
        info = await asyncio.wait_for(worker_get(acc.id, "/account-info"), 5)
    except Exception:
        return False
    start, step = rec["start"], c["big_pct"]
    if start <= 0:
        return False
    k = int((float(info.get("balance") or 0) - start) / start * 100 // step)
    if k < 1 or k <= rec.get("big", 0) or time.time() - rec.get("big_checked", 0) < BIG_RECHECK_SEC:
        return False
    rec["big_checked"] = time.time()
    r = await _rule(acc)
    if r is None:
        return True
    base = float(r.get("start_balance") or start)
    real = r["net_profit"] / base * 100 if base > 0 else 0
    k2 = int(real // step)
    if k2 >= 1 and k2 > rec.get("big", 0):
        rec["big"] = k2
        text, kb = await _message(acc, r, head=f"🎉 오늘 실현 수익 {real:+.0f}% (시작 잔고 {_f(base)}) · 출금 가능")
        await _send(live, client, text, kb, kind="withdraw_big")
    return True


async def loop(live, client) -> None:
    await asyncio.sleep(60)
    while True:
        try:
            await check_once(live, client)
        except Exception as e:
            print(f"[withdraw-notice] 확인 실패: {e}")
        await asyncio.sleep(LOOP_SEC)


# ── /출금 (/withdraw) ──
async def cmd(live, client, token, cid: str, arg: str) -> None:
    words = (arg or "").replace("%", " ").split()
    if words and words[0] in ("큰수익", "big"):
        v = words[1] if len(words) > 1 else ""
        if v in ("끄기", "off"):
            save_cfg({"big_enabled": False})
        elif v in ("켜기", "on"):
            save_cfg({"big_enabled": True})
        elif v.replace(".", "", 1).isdigit():
            save_cfg({"big_pct": float(v), "big_enabled": True})
        c = cfg()
        await live._send(client, token, cid,
                         f"🎉 큰 수익 출금 알림 {'켜짐' if c['big_enabled'] else '꺼짐'} · 오늘 실현 수익이 시작 잔고의 "
                         f"{c['big_pct']:g}% · {c['big_pct'] * 2:g}% · {c['big_pct'] * 3:g}% … 넘을 때마다\n"
                         "<i>바꾸기: /출금 큰수익 30 · /출금 큰수익 끄기 · /출금 큰수익 켜기</i>")
        return
    if words and words[0] in ("알림", "notice"):
        v = words[1] if len(words) > 1 else ""
        if v in ("끄기", "off"):
            save_cfg({"enabled": False})
        elif v in ("켜기", "on"):
            save_cfg({"enabled": True})
        elif v.isdigit():
            save_cfg({"lead_min": int(v), "enabled": True})
        c = cfg()
        from services.loss_limit import KST
        when = " · ".join(f"{name} {(t - timedelta(minutes=c['lead_min'])):%H:%M}" for _, name, t in boundaries(datetime.now(KST)))
        await live._send(client, token, cid,
                         f"💸 출금 알림 {'켜짐' if c['enabled'] else '꺼짐'} · 장 바뀌기 {c['lead_min']}분 전\n{when}\n"
                         f"🎉 큰 수익 알림 {'켜짐' if c['big_enabled'] else '꺼짐'} · {c['big_pct']:g}% 마다\n"
                         "<i>바꾸기: /출금 알림 20 · /출금 알림 끄기 · /출금 알림 켜기 · /출금 큰수익 30 · /출금 큰수익 끄기</i>")
        return
    accs = await _accounts()
    if not accs:
        await live._send(client, token, cid, "잠금을 켠 계좌가 없습니다.")
        return
    for acc in accs:
        r = await _rule(acc)
        if r is None:
            await live._send(client, token, cid, f"{html.escape(acc.account_number)}: 출금 가능액을 구하지 못했습니다 (MT5 연결 확인)")
            continue
        text, kb = await _message(acc, r, head="💸 지금 출금 가능")
        extra = {"reply_markup": kb} if kb else {}
        res = await live._send(client, token, cid, text, **extra)
        if not res.get("ok") and kb:
            await live._send(client, token, cid, text)
