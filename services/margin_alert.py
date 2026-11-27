"""텔레그램 /증거금 (/margin) - 증거금 사용률 알림 · 잠금 설정을 한곳에서

알림은 원래 있던 지침 규칙 '증거금 경고'(services/guidelines.py margin_use)가 보냄 - 이 파일은 따로 보내지 않음
  (관리 화면 '지침' 탭의 증거금 경고와 같은 설정: 켜기·끄기 · 단계)
잠금은 services/margin_lock.py (계좌별 워커가 확인)

  /증거금                      : 지금 설정 · 계좌별 사용률
  /증거금 알림 끄기 | 켜기
  /증거금 알림 10 20 30        : 단계 바꾸기 (1~100, 최대 6개)
  /증거금 잠금 켜기 | 끄기 | 20 : 증거금 사용률 잠금
"""
from __future__ import annotations

import asyncio
import html
import time
from datetime import datetime, timezone

DEFAULT_LEVELS = [10, 20, 30]


def cfg() -> dict:
    """지침 규칙 margin_use 의 켜짐 · 단계"""
    from services.settings_service import get_settings
    g = get_settings().get("guidelines") or {}
    r = (g.get("rules") or {}).get("margin_use") or {}
    lv = []
    for v in r.get("levels") or DEFAULT_LEVELS:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if 0 < f <= 100:
            lv.append(f)
    return {"enabled": bool(r.get("enabled", True)) and bool(g.get("enabled", True)),
            "rule_on": bool(r.get("enabled", True)), "all_on": bool(g.get("enabled", True)),
            "levels": sorted(set(lv))[:6] or DEFAULT_LEVELS}


def save_cfg(patch: dict) -> dict:
    from services.settings_service import get_settings, update_settings
    g = dict(get_settings().get("guidelines") or {})
    rules = dict(g.get("rules") or {})
    r = dict(rules.get("margin_use") or {})
    r.update(patch)
    rules["margin_use"] = r
    update_settings({"guidelines": {**g, "rules": rules}})
    return cfg()


def _fmt_level(v: float) -> str:
    return f"{v:g}%"


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
    if words and words[0] in ("알림", "alert"):
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
    from services.withdraw_notice import _accounts
    from services.worker_client import worker_get
    c = cfg()
    from services import margin_lock
    mc = margin_lock.cfg()
    lines = [f"📊 증거금 사용률 알림 <b>{'켜짐' if c['enabled'] else '꺼짐'}</b> · "
             + " · ".join(_fmt_level(v) for v in c["levels"]) + " 넘으면 알림"
             + ("" if c["all_on"] else " (지침 알림 전체가 꺼져 있어 안 옴 - 관리 화면 지침 탭)"),
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
