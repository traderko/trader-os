"""손절 없는 포지션 자동청산 - 한 종목의 SL 없는 포지션 손실이 잔고의 N% 를 넘으면 카운트다운 뒤 청산

  🔴 긴급 알림: 'NAS100+ 손절 없이 -10.4% · 60초 뒤 청산합니다'  [✋ 이번엔 버티기] [🗡 지금 청산]
    - 아무것도 안 누르면 60초 뒤 그 종목의 SL 없는 포지션만 시장가 청산 (SL 걸린 포지션 · 다른 종목은 그대로)
    - ✋ 버티기: 이번엔 안 자름 → 손실이 다음 단계(+10%p, 예: 20%)에 닿으면 다시 물어봄
    - 🗡 지금 청산: 바로 청산
    - 카운트다운 중 손실이 기준보다 2%p 넘게 줄면 취소 · SL 을 걸면 그 포지션은 빠짐
  손실 = 그 종목 SL 없는 포지션들의 (평가손익 + 스왑) 합 ÷ 지금 잔고
  계좌별 워커(trade_lock_worker.py)가 2초마다 확인하고 청산함. 버튼은 서버(텔레그램)가 받아서 여기 파일에 결정을 적음
  청산 주문 매직 990097 → 거래 기록 청산 이유 '🧯 손절 없음 자동청산'

기본: 꺼짐 (켜는 사람만). 설정 settings "nosl_guard" {enabled, pct, countdown_sec, step}
텔레그램: /손절없음 (/nosl) 켜기 · 끄기 · 10 (%) · 대기 60 (초)
상태: data/nosl_guard/<계좌id>.json  {종목: {"next": 다음에 물을 %, "deadline": ts|None, "decision": None|"keep"|"now", "loss": %}}
"""
from __future__ import annotations

import json
import os

NOSL_MAGIC = 990097
REARM_PP = 2.0
DEFAULTS = {"enabled": False, "pct": 10.0, "countdown_sec": 60, "step": 10.0}


def cfg() -> dict:
    from services.settings_service import get_settings
    raw = get_settings().get("nosl_guard") or {}
    out = dict(DEFAULTS)
    out["enabled"] = bool(raw.get("enabled", DEFAULTS["enabled"]))
    for k, lo, hi in (("pct", 1.0, 90.0), ("step", 1.0, 50.0)):
        try:
            out[k] = min(hi, max(lo, float(raw.get(k, DEFAULTS[k]))))
        except (TypeError, ValueError):
            pass
    try:
        out["countdown_sec"] = min(600, max(10, int(raw.get("countdown_sec", DEFAULTS["countdown_sec"]))))
    except (TypeError, ValueError):
        pass
    return out


def save_cfg(patch: dict) -> dict:
    from services.settings_service import get_settings, update_settings
    cur = dict(get_settings().get("nosl_guard") or {})
    cur.update(patch)
    update_settings({"nosl_guard": cur})
    return cfg()


def _path(account_id: int) -> str:
    from services.settings_service import DATA_DIR
    return os.path.join(DATA_DIR, "nosl_guard", f"{account_id}.json")


def read_state(account_id: int) -> dict:
    try:
        with open(_path(account_id), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_state(account_id: int, st: dict) -> None:
    p = _path(account_id)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(p + ".tmp", p)


def decide(account_id: int, symbol: str, decision: str) -> bool:
    """텔레그램 버튼 → 결정 기록 (워커가 2초 안에 읽음). 카운트다운 중이 아니면 False"""
    st = read_state(account_id)
    s = st.get(symbol)
    if not s or not s.get("deadline"):
        return False
    s["decision"] = decision
    write_state(account_id, st)
    return True


def groups(positions) -> dict[str, dict]:
    """종목 → SL 없는 포지션 묶음 {tickets, loss(양수=손실), volume, side}"""
    out: dict[str, dict] = {}
    for p in positions or []:
        if getattr(p, "sl", 0):
            continue
        g = out.setdefault(p.symbol, {"tickets": [], "pnl": 0.0, "volume": 0.0, "sides": set()})
        g["tickets"].append(p.ticket)
        g["pnl"] += float(p.profit) + float(getattr(p, "swap", 0) or 0)
        g["volume"] += float(p.volume)
        g["sides"].add("BUY" if p.type == 0 else "SELL")
    return out


def step_once(st: dict, grp: dict[str, dict], balance: float, now: float, c: dict) -> list[tuple]:
    """상태 한 번 진행 (MT5 · 텔레그램 없이 - 시험하기 쉽게). 할 일 목록을 돌려줌
       ("ask", 종목, 손실%, 그룹) · ("close", 종목, 손실%, 그룹, 왜) · ("keep", 종목, 다음%) · ("cancel", 종목, 손실%)"""
    acts = []
    for sym in list(st):
        if sym not in grp:                                   # SL 없는 포지션이 없어짐 (청산 · SL 걸음)
            if st[sym].get("deadline"):
                acts.append(("cancel", sym, 0.0))
            del st[sym]
    if balance <= 0:
        return acts
    for sym, g in grp.items():
        loss = max(0.0, -g["pnl"]) / balance * 100
        s = st.setdefault(sym, {"next": c["pct"], "deadline": None, "decision": None})
        s["loss"] = round(loss, 2)
        if s.get("deadline"):
            if s.get("decision") == "keep":
                s.update(next=max(s["next"], loss) + c["step"], deadline=None, decision=None)
                acts.append(("keep", sym, s["next"]))
            elif s.get("decision") == "now" or now >= s["deadline"]:
                why = "now" if s.get("decision") == "now" else "timeout"
                s.update(deadline=None, decision=None, next=c["pct"])
                acts.append(("close", sym, loss, g, why))
            elif loss < s["next"] - REARM_PP:
                s.update(deadline=None, decision=None)
                acts.append(("cancel", sym, loss))
            continue
        if loss < c["pct"] - REARM_PP:
            s["next"] = c["pct"]                             # 손실이 기준 아래로 돌아오면 처음 단계로
        if loss >= s["next"]:
            s.update(deadline=now + c["countdown_sec"], decision=None)
            acts.append(("ask", sym, loss, g))
    return acts


def ask_text(number: str, sym: str, loss: float, g: dict, c: dict) -> tuple[str, str, list]:
    side = "/".join(sorted(g["sides"]))
    title = f"🧯 손절 없는 포지션 손실 {loss:.1f}% · {c['countdown_sec']}초 뒤 청산"
    body = (f"계좌 {number} · {sym} {side} {g['volume']:g}lot (SL 없음 {len(g['tickets'])}개) · 평가손익 {g['pnl']:+,.2f}\n"
            f"{c['countdown_sec']}초 안에 아무것도 안 누르면 이 종목의 SL 없는 포지션을 시장가로 청산합니다.\n"
            f"✋ 버티기를 누르면 이번엔 두고, 손실이 더 커지면(+{c['step']:g}%p) 다시 묻습니다. SL 을 걸면 그 포지션은 빠집니다.")
    return title, body, []


def buttons(account_id: int, sym: str) -> list:
    return [[{"text": "✋ 이번엔 버티기", "callback_data": f"nsl:keep:{account_id}:{sym}"[:64]},
             {"text": "🗡 지금 청산", "callback_data": f"nsl:now:{account_id}:{sym}"[:64]}]]


# ── 텔레그램 /손절없음 (/nosl) ──
async def cmd(live, client, token, cid: str, arg: str = "") -> None:
    w = (arg or "").replace("%", " ").split()
    if w:
        if w[0] in ("켜기", "on"):
            save_cfg({"enabled": True})
        elif w[0] in ("끄기", "off"):
            save_cfg({"enabled": False})
        elif w[0] in ("대기", "wait") and len(w) > 1 and w[1].isdigit():
            save_cfg({"countdown_sec": int(w[1])})
        else:
            try:
                save_cfg({"pct": float(w[0]), "enabled": True})
            except ValueError:
                pass
    c = cfg()
    text = (f"🧯 <b>손절 없는 포지션 자동청산 {'켜짐' if c['enabled'] else '꺼짐'}</b>\n"
            f"한 종목의 SL 없는 포지션 손실이 잔고의 <b>{c['pct']:g}%</b>를 넘으면 🔴 긴급 알림 → "
            f"<b>{c['countdown_sec']}초</b> 안에 안 누르면 그 포지션만 청산\n"
            f"✋ 버티기를 누르면 +{c['step']:g}%p 마다 다시 물어봄\n\n"
            "<i>바꾸기: /손절없음 켜기 · 끄기 · /손절없음 10 (%) · /손절없음 대기 60 (초)</i>")
    await live._send(client, token, cid, text)


async def button(live, client, token, cid: str, data: str) -> str:
    """nsl:keep|now:<계좌id>:<종목>"""
    parts = data.split(":", 3)
    if len(parts) < 4 or not parts[2].isdigit():
        return "잘못된 버튼"
    ok = decide(int(parts[2]), parts[3], "keep" if parts[1] == "keep" else "now")
    if not ok:
        return "이미 끝난 알림이에요 (청산됐거나 취소됨)"
    return "✋ 이번엔 버팁니다 - 더 커지면 다시 물어요" if parts[1] == "keep" else "🗡 지금 청산합니다 (2초 안에)"
