# services/sltp.py
#
# 여러 포지션의 SL/TP 한꺼번에 바꾸기 (웹 포지션 탭 · 텔레그램 🛑 SL / 🎯 TP 버튼이 같이 씀)
#
# MT5 는 포지션 하나씩 주문을 보내야 해서 50개가 넘으면 한 번의 요청이 10초를 넘겨 '시간 초과'가 났다.
# (워커는 시간 초과 뒤에도 계속 바꾸고 있어서, 실제로는 대부분 적용됐는데 '실패'로 보이기도 함)
#   → CHUNK 개씩 나눠 보내고 (요청마다 넉넉한 시간), 진행 상황을 알려 주고,
#     그래도 시간 초과가 난 묶음은 마지막에 포지션을 다시 읽어 실제로 바뀌었는지 확인한다.

import asyncio
from typing import Awaitable, Callable

from services.worker_client import worker_get, worker_post

CHUNK = 10               # 한 번에 보낼 포지션 수
SEC_PER_TICKET = 3.0     # 요청 시간 한도 = 10초 + 포지션당 이만큼


def _same(a, b) -> bool:
    a, b = float(a or 0), float(b or 0)
    return abs(a - b) <= max(1e-9, abs(b) * 1e-7)


async def apply_sltp(account_id: int, field: str, tickets: list[int], price: float,
                     progress: Callable[[int, int], Awaitable[None]] | None = None) -> dict:
    """field: 'sl' | 'tp'. 반환 {"success": [티켓], "failed": [{"ticket", "reason"}]} (워커와 같은 모양)"""
    path = "/positions/set-sl" if field == "sl" else "/positions/set-tp"
    key = "sl_price" if field == "sl" else "tp_price"
    tickets = list(dict.fromkeys(int(t) for t in tickets))
    ok: list[int] = []
    failed: dict[int, str] = {}
    unsure: list[int] = []           # 시간 초과 - 워커가 계속 처리 중일 수 있음
    done = 0
    for i in range(0, len(tickets), CHUNK):
        part = tickets[i:i + CHUNK]
        try:
            res = await worker_post(account_id, path, {"tickets": part, key: price},
                                    timeout=10 + SEC_PER_TICKET * len(part))
            ok += [int(t) for t in res.get("success") or []]
            for f in res.get("failed") or []:
                failed[int(f.get("ticket"))] = str(f.get("reason") or "")
        except Exception as e:
            if _is_timeout(e):
                unsure += part
            else:
                for t in part:
                    failed[t] = _reason(e)
        done += len(part)
        if progress:
            try:
                await progress(done, len(tickets))
            except Exception:
                pass
    if unsure:
        # 늦게 끝난 것까지 기다렸다가 실제 값으로 확인 (최대 약 20초)
        left = set(unsure)
        for wait in (2, 3, 5, 10):
            await asyncio.sleep(wait)
            try:
                rows = await worker_get(account_id, "/positions-all")
            except Exception:
                continue
            now = {int(p["ticket"]): p for p in rows}
            for t in list(left):
                p = now.get(t)
                if p is None:
                    failed[t] = "포지션을 찾을 수 없음 (청산됨?)"
                    left.discard(t)
                elif _same(p.get(field), price):
                    ok.append(t)
                    left.discard(t)
            if not left:
                break
        for t in left:
            failed[t] = "시간 초과 - 적용됐는지 확인 안 됨"
    return {"success": ok, "failed": [{"ticket": t, "reason": r} for t, r in failed.items() if t not in ok]}


def _is_timeout(e: Exception) -> bool:
    import httpx
    return isinstance(e, (httpx.TimeoutException, asyncio.TimeoutError)) or "timeout" in type(e).__name__.lower()


def _reason(e: Exception) -> str:
    d = getattr(e, "detail", None)
    return str(d or e or type(e).__name__)[:200]


# ── SL/TP 를 언제 고쳤는지 ──
# MT5 는 SL/TP 를 고친 시각을 따로 주지 않아서 (포지션의 time_update 는 SL/TP 를 고쳐도 안 바뀜),
# TraderOS 가 포지션을 읽을 때마다(실시간 현황 · BOB 확인 등 몇 초~20초마다) 이전 값과 비교해 바뀐 시각을 적어 둠.
# MT5 에서 직접 고친 것도 잡힘. 서버를 다시 켜면 처음부터 (그 전에 고친 건 모름)
_seen: dict[tuple, dict] = {}


def observe(account_id: int, rows: list) -> None:
    """/positions-all 결과를 받을 때마다 호출 (worker_client 가 부름). 각 포지션에 sl_changed · tp_changed(ms) 를 붙임"""
    import time
    now = int(time.time() * 1000)
    live = set()
    for p in rows or []:
        if not isinstance(p, dict) or "ticket" not in p:
            continue
        k = (account_id, int(p["ticket"]))
        live.add(k)
        prev = _seen.get(k)
        cur = {"sl": float(p.get("sl") or 0), "tp": float(p.get("tp") or 0),
               "sl_at": (prev or {}).get("sl_at", 0), "tp_at": (prev or {}).get("tp_at", 0)}
        if prev:
            for f in ("sl", "tp"):
                if not _same(prev[f], cur[f]):
                    cur[f + "_at"] = now
        _seen[k] = cur
        p["sl_changed"], p["tp_changed"] = cur["sl_at"], cur["tp_at"]
    for k in [k for k in _seen if k[0] == account_id and k not in live]:   # 청산된 포지션
        del _seen[k]


def level_options(ps: list[dict], key: str) -> list[dict]:
    """포지션들에 걸린 key('sl'|'tp') 값들 (0 빼고) - [{value, count, changed(ms)}]
    최근에 고친 값이 먼저, 그다음 많이 걸린 값"""
    opts: dict[float, dict] = {}
    for p in ps:
        v = round(float(p.get(key) or 0), 10)
        if not v:
            continue
        o = opts.setdefault(v, {"value": v, "count": 0, "changed": 0})
        o["count"] += 1
        o["changed"] = max(o["changed"], int(p.get(key + "_changed") or 0))
    return sorted(opts.values(), key=lambda o: (-o["changed"], -o["count"]))


def ref_levels(same_side: list[dict], ticket: int) -> tuple[float, float, list[dict], list[dict]]:
    """새로 들어간 포지션(ticket)에 맞출 '기존' SL/TP 와, 맞춰야 할 포지션들.
    same_side: 같은 종목·같은 방향 포지션 전부 (worker /positions-all 의 dict)
    기준 = ticket 보다 먼저 들어간 포지션들에 걸린 SL/TP 중 가장 최근에 고친 값 (고친 적 없으면 가장 많이 걸린 값, 0 은 빼고). 없으면 0
    돌려줌: (기준 SL, 기준 TP, SL 을 바꿔야 할 포지션들, TP 를 바꿔야 할 포지션들)"""
    older = [p for p in same_side if p.get("ticket", 0) < ticket] or []
    def pick(key):
        o = level_options(older, key)
        return o[0]["value"] if o else 0.0
    sl, tp = pick("sl"), pick("tp")
    need_sl = [p for p in same_side if sl and abs(float(p.get("sl") or 0) - sl) > 1e-9]
    need_tp = [p for p in same_side if tp and abs(float(p.get("tp") or 0) - tp) > 1e-9]
    return sl, tp, need_sl, need_tp
