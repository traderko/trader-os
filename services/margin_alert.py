"""증거금 사용률 알림 - 사용률이 정한 단계(기본 10% · 20% · 30%)를 넘을 때마다 텔레그램 경고

  ⚠️ 증거금 사용률 23% · 20% 넘음
  12345678 · 평가금 1,234.00
  NAS100+ BUY 6lot · 🔻52pt(0.2%) 가면 강제청산 · 10분 KTR 0.8개

  - 같은 단계는 한 번만. 사용률이 그 단계보다 2%p 넘게 내려가면 다시 알릴 수 있게 됨
    (예: 20% 알림 뒤 17% 로 내려갔다가 다시 21% → 또 알림)
  - 주문 · 청산에는 손대지 않음 (알림만). TradeLock EA 와 상관없음
  - 20초마다 계좌별 /account-info 한 번 (포지션은 단계를 넘었을 때만 물음)

텔레그램
  /증거금 (/margin)          : 지금 설정 · 계좌별 사용률
  /증거금 알림 끄기 | 켜기
  /증거금 알림 10 20 30      : 단계 바꾸기 (1~100, 최대 6개)
  /증거금 잠금 켜기 | 끄기 | 20 : 증거금 사용률 잠금 (services/margin_lock.py)
설정: settings "margin_alert" {enabled, levels}
"""
from __future__ import annotations

import asyncio
import html
import time
from datetime import datetime, timezone

LOOP_SEC = 20
REARM_PP = 2.0                   # 단계보다 이만큼(%p) 내려가야 다시 알림
DEFAULTS = {"enabled": True, "levels": [10, 20, 30]}
_state: dict[int, set] | None = None   # 계좌 id → 이미 알린 단계 (data/margin_alert.json - 서버를 다시 켜도 같은 단계를 또 알리지 않게)


def _path() -> str:
    import os
    from services.settings_service import DATA_DIR
    return os.path.join(DATA_DIR, "margin_alert.json")


def _st() -> dict[int, set]:
    global _state
    if _state is None:
        import json
        try:
            with open(_path(), encoding="utf-8") as f:
                _state = {int(k): set(v) for k, v in json.load(f).items()}
        except (OSError, ValueError, TypeError):
            _state = {}
    return _state


def _save() -> None:
    import json
    import os
    try:
        tmp = _path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({str(k): sorted(v) for k, v in (_state or {}).items()}, f)
        os.replace(tmp, _path())
    except OSError:
        pass


def cfg() -> dict:
    from services.settings_service import get_settings
    raw = get_settings().get("margin_alert") or {}
    lv = []
    for v in raw.get("levels") or DEFAULTS["levels"]:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if 0 < f <= 100:
            lv.append(f)
    return {"enabled": bool(raw.get("enabled", True)), "levels": sorted(set(lv))[:6] or DEFAULTS["levels"]}


def save_cfg(patch: dict) -> dict:
    from services.settings_service import get_settings, update_settings
    cur = dict(get_settings().get("margin_alert") or {})
    cur.update(patch)
    update_settings({"margin_alert": cur})
    return cfg()


def _fmt_level(v: float) -> str:
    return f"{v:g}%"


def crossed(util: float, levels: list[float], sent: set) -> float | None:
    """이번에 새로 넘은 가장 높은 단계 (없으면 None). sent 는 제자리에서 고침 (내려가면 다시 알릴 수 있게)"""
    for lv in list(sent):
        if util < lv - REARM_PP:
            sent.discard(lv)
    new = [lv for lv in levels if util >= lv and lv not in sent]
    if not new:
        return None
    sent.update(lv for lv in levels if util >= lv)     # 한 번에 여러 단계를 넘으면 가장 높은 것만 알리고 아래도 알린 걸로
    return max(new)


async def _ktr_txt(aid: int, symbol: str, points: float) -> str:
    """강제청산까지 거리를 그 장 10분 KTR 몇 개인지 (못 구하면 빈 글자)"""
    try:
        from services import grid, ktr_brief, symbols
        g = symbols.group_of(symbol)
        if not g:
            return ""
        now = datetime.now(timezone.utc)
        sid = grid.current_session(now)
        k = await asyncio.wait_for(ktr_brief.ktr_on(aid, g["key"], sid, grid.session_open(sid, now), "10m", now), 8)
        return f" · 10분 KTR {points / k:.1f}개" if k else ""
    except Exception:
        return ""


async def message(acc, info: dict, util: float, level: float) -> str:
    from services.risk import liquidation, liq_line
    from services.worker_client import worker_get
    lines = [f"⚠️ <b>증거금 사용률 {util:.0f}%</b> · {_fmt_level(level)} 넘음",
             f"{html.escape(str(acc.account_number))} · 평가금 {float(info.get('equity') or 0):,.2f}"]
    try:
        pos = await asyncio.wait_for(worker_get(acc.id, "/positions-all"), 5)
        for x in liquidation(info, pos or [])[:3]:
            lines.append(html.escape(liq_line(x)) + await _ktr_txt(acc.id, x["symbol"], x["points"]))
    except Exception:
        pass
    return "\n".join(lines)


async def check_once(live, client) -> None:
    from services.withdraw_notice import _accounts, _send
    from services.worker_client import worker_get
    c = cfg()
    if not c["enabled"]:
        return
    for acc in await _accounts():
        try:
            info = await asyncio.wait_for(worker_get(acc.id, "/account-info"), 5)
        except Exception:
            continue                                   # MT5 끊김 - 상태는 그대로 두고 다음에
        eq, used = float(info.get("equity") or 0), float(info.get("margin") or 0)
        util = used / eq * 100 if eq > 0 else 0.0
        sent = _st().setdefault(acc.id, set())
        before = set(sent)
        lv = crossed(util, c["levels"], sent)
        if sent != before:
            _save()
        if lv is not None:
            await _send(live, client, await message(acc, info, util, lv), {})


async def loop(live, client) -> None:
    await asyncio.sleep(30)
    while True:
        try:
            await check_once(live, client)
        except Exception as e:
            print(f"[margin-alert] 확인 실패: {e}")
        await asyncio.sleep(LOOP_SEC)


# ── /증거금 (/margin) ──
async def cmd(live, client, token, cid: str, arg: str) -> None:
    words = (arg or "").replace(",", " ").replace("%", " ").split()
    if words and words[0] in ("잠금", "lock"):
        from services import margin_lock
        v = words[1] if len(words) > 1 else ""
        if v in ("끄기", "off"):
            margin_lock.save_cfg({"enabled": False})
        elif v in ("켜기", "on"):
            margin_lock.save_cfg({"enabled": True})
        else:
            try:
                margin_lock.save_cfg({"pct": float(v), "enabled": True})
            except ValueError:
                pass
        words = []                                       # 아래에서 지금 설정을 보여 줌
        rest = words[1:]
        if rest and rest[0] in ("끄기", "off"):
            save_cfg({"enabled": False})
        elif rest and rest[0] in ("켜기", "on"):
            save_cfg({"enabled": True})
        elif rest:
            nums = []
            for w in rest:
                try:
                    nums.append(float(w))
                except ValueError:
                    pass
            nums = sorted({n for n in nums if 0 < n <= 100})[:6]
            if nums:
                save_cfg({"levels": nums, "enabled": True})
                _st().clear()
                _save()
    from services.withdraw_notice import _accounts
    from services.worker_client import worker_get
    c = cfg()
    from services import margin_lock
    mc = margin_lock.cfg()
    lines = [f"📊 증거금 사용률 알림 <b>{'켜짐' if c['enabled'] else '꺼짐'}</b> · "
             + " · ".join(_fmt_level(v) for v in c["levels"]) + " 넘으면 알림",
             f"🔒 증거금 잠금 <b>{'켜짐' if mc['enabled'] else '꺼짐'}</b> · {mc['pct']:g}% 넘으면 추가 진입 막음 "
             f"({mc['pct'] - margin_lock.REARM_PP:g}% 아래로 내려가면 저절로 풀림)"]
    for acc in await _accounts():
        try:
            info = await asyncio.wait_for(worker_get(acc.id, "/account-info"), 5)
            eq, used = float(info.get("equity") or 0), float(info.get("margin") or 0)
            lines.append(f"{html.escape(str(acc.account_number))}: 지금 {used / eq * 100 if eq > 0 else 0:.1f}%")
        except Exception:
            lines.append(f"{html.escape(str(acc.account_number))}: MT5 연결 안 됨")
    lines.append("<i>바꾸기: /증거금 알림 10 20 30 · /증거금 알림 끄기 · 켜기\n"
                 "/증거금 잠금 20 · /증거금 잠금 끄기 · 켜기</i>")
    await live._send(client, token, cid, "\n".join(lines))
